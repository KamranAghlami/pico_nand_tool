"""High-level device API on top of a Transport (framing, seq matching, retries, resync).

Study notes (docs/LEARNING_GUIDE.md §4-5):

- Every request carries a sequence number (`seq`, 0..255, wrapping). The device echoes it, so a response is matched
  to its request by (cmd, seq). Anything else that arrives (a late answer to a request we gave up on) is skipped.
- `request()` is the workhorse for single-response commands: send, read one frame, retry on error. After a lost or
  corrupt frame it first runs `resync()`: send ABORT (stops any stream still running on the device), then throw
  input away until the line goes quiet. Only then is the stream of bytes known to be at a frame boundary again.
- Retrying is only safe when repeating the request is harmless. Reads are. Erase and program are not: if their
  answer is lost, they may already have run. `request(resend=False)` raises LostResponse instead, and write.py
  re-does the whole block (erase, then program), which gives the right result whether or not the lost one ran.
- `read_pages()` is a generator. It yields pages as the stream delivers them, so a 277 MB dump never sits in memory,
  and it resumes the stream from the first page not yet yielded after a transport error.
- `program_pages()` pipelines: it keeps two PROGRAM_PAGE requests in flight, so the USB link is busy sending the next
  page while the device programs the current one (writes are limited by host->device USB bandwidth).
"""

from __future__ import annotations

import random
import struct
import time
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass

from .errors import FrameError, NandToolError, TransportError
from .protocol import (
    ARM_TOKEN,
    BLOCKS,
    CRC_LEN,
    ERASE_IGNORE_BAD_MARKER,
    END_FLAG,
    END_LEN,
    PAGE_LEN,
    PAGE_NONE,
    PARAM_LEN,
    PROTO_VERSION,
    RESP_HDR_LEN,
    TIMING_WIRE_LEN,
    TOTAL_PAGES,
    WRITE_RESP_LEN,
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

__all__ = [
    "Client",
    "DeviceError",
    "FrameError",
    "LostResponse",
    "ProtocolMismatch",
    "TransportError",
    "WriteOpError",
    "WriteResult",
]


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


class WriteOpError(DeviceError):
    """ERASE_BLOCK / PROGRAM_PAGE ran and failed: ERR_OP_FAILED (SR bit 0) or ERR_WP_STUCK (SR bit 7 = 0)."""

    def __init__(self, cmd: int, status: int, sr: int, page: int):
        self.sr = sr
        self.page = page
        super().__init__(cmd, status)
        self.args = (f"{self}: SR {sr:02X}h at page {page}",)


class LostResponse(TransportError):
    """An erase/program request was sent but no valid response came back: it may or may not have run. Never re-send
    it blindly (docs/PROTOCOL.md "Write mode"); re-do the whole block instead."""


class ProtocolMismatch(NandToolError):
    pass


# PROGRAM_PAGE requests in flight in program_pages() (docs/PROTOCOL.md "Write mode"). Two keep the USB OUT pipe busy
# while the device programs and answers; measured on hardware, more gain nothing.
PROGRAM_DEPTH = 2


@dataclass(frozen=True)
class WriteResult:
    sr: int
    busy_ns: int


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
        self.stream_restarts = 0  # READ_PAGES streams re-issued after a frame error (statistics)

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
        """Read exactly one frame. Raises FrameError (corrupt) or TransportError (timeout).

        Two reads: the fixed 10-byte header first, because its `len` field is the only way to know how long the rest
        (payload + 4-byte CRC) is."""
        hdr = self._read_exact(RESP_HDR_LEN, deadline)
        _, _, _, _, length = parse_response_header(hdr)
        rest = self._read_exact(length + CRC_LEN, deadline)
        return decode_response(hdr + rest)

    def resync(self) -> None:
        """Host recovery rule (docs/PROTOCOL.md): send ABORT (ends any running stream), then discard input until
        the line has been quiet for quiet_s. Raises TransportError if it never goes quiet within resync_max_s.

        Why "quiet" and not "find the next magic byte": a 5Ah byte can occur anywhere inside page data, so there is no
        reliable way to find a frame boundary in the middle of a stream. Silence is the one unambiguous boundary."""
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

    def request(self, cmd: int, args: bytes = b"", *, resend: bool = True) -> Response:
        """Send one request and return its single response, retrying on transport errors and ERR_CRC.

        Only for single-response commands (every command except READ_PAGES). resend=False is for erase/program, which
        are not safe to repeat: a lost or corrupted response raises LostResponse instead of re-sending (ERR_CRC is
        still retried, since then nothing ran).
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
                if not resend:
                    raise LostResponse(
                        f"{_name(Cmd, cmd)}: no valid response ({e}); the operation may or may not have run"
                    ) from e
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

    # ---- write mode (docs/PROTOCOL.md "Write mode") -------------------------------------------------------------

    def arm_write(self, first_block: int, last_block: int, idle_timeout_s: int = 10) -> None:
        """Arm erase/program for blocks [first_block, last_block] until idle_timeout_s pass without one."""
        if not 0 <= first_block <= last_block < BLOCKS:
            raise ValueError(f"block range {first_block}..{last_block} outside 0..{BLOCKS - 1}")
        self.call(Cmd.ARM_WRITE, struct.pack("<IHHH", ARM_TOKEN, first_block, last_block, idle_timeout_s))

    def disarm(self) -> None:
        self.call(Cmd.DISARM)

    def erase_block(self, block: int, *, ignore_bad_marker: bool = False) -> WriteResult:
        flags = ERASE_IGNORE_BAD_MARKER if ignore_bad_marker else 0
        return self._write_op(Cmd.ERASE_BLOCK, struct.pack("<HB", block, flags))

    def program_page(self, page: int, data: bytes) -> WriteResult:
        if len(data) != PAGE_LEN:
            raise ValueError(f"a page is {PAGE_LEN} bytes, got {len(data)}")
        return self._write_op(Cmd.PROGRAM_PAGE, struct.pack("<I", page) + data)

    def program_pages(self, pages: Sequence[tuple[int, bytes]], *, depth: int = PROGRAM_DEPTH) -> list[WriteResult]:
        """Program (page, data) pairs in order with up to `depth` requests in flight (docs/PROTOCOL.md "Write mode").

        The device handles requests strictly in order, and a failed program disarms it, so a request already in
        flight is refused (ERR_NOT_ARMED) without touching the bus. A failure raises WriteOpError/DeviceError for the
        first failed page, after the replies still in flight are read. A lost or garbled reply, or ERR_CRC, raises
        LostResponse after a resync: the pages in flight may or may not have run, so the caller re-does the whole
        block. Nothing is ever re-sent here."""
        for _, data in pages:
            if len(data) != PAGE_LEN:
                raise ValueError(f"a page is {PAGE_LEN} bytes, got {len(data)}")
        results: list[WriteResult] = []
        inflight: deque[tuple[int, int]] = deque()  # (seq, page), oldest first
        failure: DeviceError | None = None
        nxt = 0
        # With depth 2 the timeline looks like this (S = send request, R = read its reply):
        #   S(p0) S(p1) R(p0) S(p2) R(p1) S(p3) R(p2) ...
        # so while the device programs p0 and answers, p1's 2116 bytes are already on their way over USB.
        try:
            while inflight or (failure is None and nxt < len(pages)):
                while failure is None and nxt < len(pages) and len(inflight) < depth:
                    page, data = pages[nxt]
                    seq = self._next_seq()
                    self.t.write(encode_request(Cmd.PROGRAM_PAGE, seq, struct.pack("<I", page) + data))
                    inflight.append((seq, page))
                    nxt += 1
                seq, page = inflight[0]
                deadline = time.monotonic() + self.timeout
                while True:
                    resp = self.read_response(deadline)
                    if resp.status == Status.ERR_CRC:
                        raise FrameError("device reported request CRC error")
                    if resp.cmd == Cmd.PROGRAM_PAGE and resp.seq == seq:
                        break
                    if resp.cmd == Cmd.PROGRAM_PAGE and any(s == resp.seq for s, _ in inflight):
                        raise FrameError(f"reply for a later page came first: the reply for page {page} was lost")
                    # stale frame from an earlier, abandoned request: skip it (the deadline still applies)
                inflight.popleft()
                if failure is None:
                    try:
                        results.append(self._write_result(Cmd.PROGRAM_PAGE, resp))
                    except DeviceError as e:
                        failure = e  # stop sending; read what is still in flight (ERR_NOT_ARMED: never ran)
        except (FrameError, TransportError) as e:
            self.resync()
            if failure is not None:
                raise failure from e
            raise LostResponse(
                f"PROGRAM_PAGE: no valid response for page {page} ({e}); it and the pages after it may or may not "
                "have run"
            ) from e
        if failure is not None:
            raise failure
        return results

    def _write_op(self, cmd: int, args: bytes) -> WriteResult:
        return self._write_result(cmd, self.request(cmd, args, resend=False))

    @staticmethod
    def _write_result(cmd: int, resp: Response) -> WriteResult:
        if resp.status not in (Status.OK, Status.ERR_OP_FAILED, Status.ERR_WP_STUCK):
            raise DeviceError(cmd, resp.status)
        if len(resp.payload) != WRITE_RESP_LEN:
            raise FrameError(f"{_name(Cmd, cmd)} reply is {len(resp.payload)} bytes, expected {WRITE_RESP_LEN}")
        sr, busy_ns = struct.unpack("<BI", resp.payload)
        if resp.status != Status.OK:
            raise WriteOpError(cmd, resp.status, sr, resp.page)
        return WriteResult(sr, busy_ns)

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
        # True while the device may still be sending this stream's frames. If we leave with it set (error, or the
        # caller stopped iterating, which raises GeneratorExit at the `yield`), the device must be told to stop, or
        # the next request would be answered with ERR_BUSY and its reply buried among page frames.
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
                        failures = 0  # progress was made: the retry budget is per stuck page, not per stream
                        yield resp.page, data
                except (FrameError, TransportError) as e:
                    last_err = e
                    failures += 1
                    self.stream_restarts += 1
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
