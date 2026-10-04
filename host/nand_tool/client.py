"""High-level device API on top of a Transport (framing, seq matching, retries, resync)."""

from __future__ import annotations

import random
import struct
import time
from collections.abc import Iterator

from .errors import FrameError, NandToolError, TransportError
from .protocol import (
    CRC_LEN,
    END_FLAG,
    END_LEN,
    PAGE_LEN,
    PAGE_NONE,
    PARAM_LEN,
    PROTO_VERSION,
    RESP_HDR_LEN,
    TIMING_WIRE_LEN,
    TOTAL_PAGES,
    Cmd,
    PingInfo,
    Response,
    Status,
    Timing,
    TimingMode,
    decode_response,
    encode_request,
    parse_response_header,
)
from .transport import Transport

__all__ = ["Client", "DeviceError", "FrameError", "ProtocolMismatch", "TransportError"]


def _name(enum, value: int) -> str:
    try:
        return enum(value).name
    except ValueError:
        return f"0x{value:02X}"


class DeviceError(NandToolError):
    """The device answered with a non-OK status."""

    def __init__(self, cmd: int, status: int):
        self.cmd = cmd
        self.status = status
        super().__init__(f"{_name(Cmd, cmd)}: {_name(Status, status)}")


class ProtocolMismatch(NandToolError):
    pass


class Client:
    def __init__(
        self,
        transport: Transport,
        *,
        timeout: float = 1.0,
        retries: int = 3,
        quiet_s: float = 0.25,
        resync_max_s: float = 3.0,
    ):
        self.t = transport
        self.timeout = timeout
        self.retries = retries
        self.quiet_s = quiet_s  # silence that ends a resync drain (docs/PROTOCOL.md "Host recovery rule")
        self.resync_max_s = resync_max_s  # give up if the line never goes quiet (not our device / runaway stream)
        # Random start: a leftover reply from an earlier process can't match our first (cmd, seq).
        self._seq = random.randrange(256)

    # ---- framing -----------------------------------------------------------------------------------------

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFF
        return self._seq

    def _read_exact(self, n: int, deadline: float) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            # Checked on every pass, so a device that never stops sending still hits the deadline.
            if time.monotonic() > deadline:
                raise TransportError(f"timeout: got {len(buf)} of {n} bytes")
            buf += self.t.read(n - len(buf))
        return bytes(buf)

    def read_response(self, deadline: float) -> Response:
        """Read exactly one frame. Raises FrameError (corrupt) or TransportError (timeout)."""
        hdr = self._read_exact(RESP_HDR_LEN, deadline)
        _, _, _, _, length = parse_response_header(hdr)
        rest = self._read_exact(length + CRC_LEN, deadline)
        return decode_response(hdr + rest)

    def resync(self) -> None:
        """Host recovery rule (docs/PROTOCOL.md): send ABORT (ends any running stream), then discard input until
        the line has been quiet for quiet_s. Raises TransportError if it never goes quiet within resync_max_s."""
        self.t.write(encode_request(Cmd.ABORT, self._next_seq()))
        self.t.reset_input()
        start = last = time.monotonic()
        while (now := time.monotonic()) - last < self.quiet_s:
            if now - start > self.resync_max_s:
                raise TransportError(
                    f"line never went quiet within {self.resync_max_s:.1f} s: the device keeps sending data it "
                    "doesn't frame correctly (is this really a Pico NAND Tool?)"
                )
            if self.t.read(4096):
                last = time.monotonic()

    def request(self, cmd: int, args: bytes = b"") -> Response:
        """Send one request and return its single response, retrying on transport errors and ERR_CRC.

        Only for single-response commands (every command except READ_PAGES); all of them are idempotent.
        """
        last_err: Exception | None = None
        for _ in range(self.retries + 1):
            seq = self._next_seq()
            self.t.write(encode_request(cmd, seq, args))
            deadline = time.monotonic() + self.timeout
            try:
                while True:
                    resp = self.read_response(deadline)
                    if resp.seq == seq and resp.cmd == cmd:
                        break
                    # stale frame from an earlier, abandoned request: skip it (the deadline still applies)
            except (FrameError, TransportError) as e:
                last_err = e
                self.resync()
                continue
            if resp.status == Status.ERR_CRC:
                last_err = FrameError("device reported request CRC error")
                continue
            return resp
        raise TransportError(f"{_name(Cmd, cmd)}: no valid response after {self.retries + 1} attempts: {last_err}")

    def call(self, cmd: int, args: bytes = b"") -> bytes:
        """request() and require status OK; return the payload."""
        resp = self.request(cmd, args)
        if not resp.ok:
            raise DeviceError(cmd, resp.status)
        return resp.payload

    # ---- commands ----------------------------------------------------------------------------------------

    def ping(self, *, check_version: bool = True) -> PingInfo:
        info = PingInfo.unpack(self.call(Cmd.PING))
        if check_version and info.proto_version != PROTO_VERSION:
            raise ProtocolMismatch(
                f"device speaks protocol v{info.proto_version}, this host tool v{PROTO_VERSION}"
            )
        return info

    def set_timing(self, mode: TimingMode, timing: Timing | None = None) -> tuple[TimingMode, Timing]:
        args = bytes((mode,))
        if mode == TimingMode.CUSTOM:
            if timing is None:
                raise ValueError("CUSTOM needs a Timing")
            args += timing.pack()
        payload = self.call(Cmd.SET_TIMING, args)
        if len(payload) != 1 + TIMING_WIRE_LEN or payload[0] not in (1, 2, 3):
            raise FrameError(f"malformed SET_TIMING reply ({len(payload)} bytes)")
        return TimingMode(payload[0]), Timing.unpack(payload[1:])

    def get_timing(self) -> tuple[TimingMode, Timing]:
        return self.set_timing(TimingMode.QUERY)

    def reset(self) -> int:
        """FFh and wait ready. Returns the measured R/B# busy time in ns (0 = R/B# was never seen low)."""
        payload = self.call(Cmd.RESET)
        if len(payload) != 4:
            raise FrameError(f"malformed RESET reply ({len(payload)} bytes)")
        return struct.unpack("<I", payload)[0]

    def read_id(self, addr: int = 0x00, n: int = 5) -> bytes:
        """90h + addr (00h = ID, 20h = ONFI signature), n bytes (1..8)."""
        args = b"" if (addr, n) == (0x00, 5) else bytes((addr, n))
        payload = self.call(Cmd.READ_ID, args)
        if len(payload) != n:
            raise FrameError(f"READ_ID returned {len(payload)} bytes, expected {n}")
        return payload

    def read_param(self) -> bytes:
        """FFh, then ECh 00h: the three 256-byte parameter page copies (768 bytes), unverified."""
        payload = self.call(Cmd.READ_PARAM)
        if len(payload) != PARAM_LEN:
            raise FrameError(f"READ_PARAM returned {len(payload)} bytes, expected {PARAM_LEN}")
        return payload

    def read_status(self) -> int:
        payload = self.call(Cmd.READ_STATUS)
        if len(payload) != 1:
            raise FrameError(f"malformed READ_STATUS reply ({len(payload)} bytes)")
        return payload[0]

    def read_pages(self, start: int, count: int) -> Iterator[tuple[int, bytes | None]]:
        """Stream pages [start, start + count): yields (page, 2112 bytes), or (page, None) when the device reported
        an R/B# timeout for that page (the caller decides whether to re-read it).

        Every page is yielded exactly once, in order. On a frame error the host recovery rule applies
        (docs/PROTOCOL.md): ABORT, drain, then re-issue READ_PAGES from the first page not yet yielded. Bytes are never
        spliced across an error. Gives up after `retries` consecutive restarts without progress. If the caller stops
        iterating early, the running stream is aborted.
        """
        if count < 1 or start < 0 or start + count > TOTAL_PAGES:
            raise ValueError(f"page range {start}+{count} outside 0..{TOTAL_PAGES}")
        end = start + count
        nxt = start  # first page not yet yielded
        failures = 0
        last_err: Exception | None = None
        streaming = False
        try:
            while nxt < end:
                if failures > self.retries:
                    raise TransportError(
                        f"READ_PAGES: no progress at page {nxt} after {failures} attempts: {last_err}"
                    )
                seq = self._next_seq()
                self.t.write(encode_request(Cmd.READ_PAGES, seq, struct.pack("<II", nxt, end - nxt)))
                streaming = True
                try:
                    while True:
                        resp = self.read_response(time.monotonic() + self.timeout)
                        if resp.seq != seq or (resp.cmd & ~END_FLAG) != Cmd.READ_PAGES:
                            continue  # stale frame, or ERR_BUSY for someone else's request
                        if resp.is_end:
                            streaming = False
                            self._check_end(resp, nxt, end)
                            break
                        if resp.status == Status.ERR_CRC:
                            streaming = False  # the request was corrupted: nothing started
                            raise FrameError("device reported request CRC error")
                        if resp.status == Status.ERR_BUSY:
                            # another stream is still running (e.g. left over from an earlier process): resync aborts
                            # it, then retry
                            raise FrameError("device busy with another stream")
                        if resp.page == PAGE_NONE and resp.status != Status.OK:
                            streaming = False  # rejected request: a single status frame, no stream
                            raise DeviceError(Cmd.READ_PAGES, resp.status)
                        if resp.page != nxt:
                            raise FrameError(f"page {resp.page} out of order, expected {nxt}")
                        if resp.status == Status.OK and len(resp.payload) == PAGE_LEN:
                            data = resp.payload
                        elif resp.status == Status.ERR_RB_TIMEOUT and not resp.payload:
                            data = None
                        else:
                            raise FrameError(f"bad page frame: status {resp.status}, {len(resp.payload)} bytes")
                        nxt += 1
                        failures = 0
                        yield resp.page, data
                except (FrameError, TransportError) as e:
                    last_err = e
                    failures += 1
                    if streaming:
                        self.resync()
                        streaming = False
        finally:
            if streaming:  # the caller stopped early (or an unexpected error): stop the device's stream
                self.resync()

    @staticmethod
    def _check_end(resp: Response, nxt: int, end: int) -> None:
        if len(resp.payload) != END_LEN:
            raise FrameError(f"end frame payload is {len(resp.payload)} bytes, expected {END_LEN}")
        if resp.status != Status.OK:
            raise FrameError(f"stream ended early: {_name(Status, resp.status)} at page {resp.page}")
        if resp.page != nxt or nxt != end:
            raise FrameError(f"end frame at page {resp.page}, expected {end} (next unread {nxt})")
