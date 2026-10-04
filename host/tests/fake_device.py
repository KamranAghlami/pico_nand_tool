"""Fake Pico NAND Tool: simulates the firmware's wire protocol in-process and implements the Transport interface.

Mirrors the firmware as of milestone M2: PING, SET_TIMING, ABORT, RESET, READ_ID and READ_STATUS are implemented,
and every other command answers ERR_UNKNOWN_CMD, exactly like the real M2 firmware. It grows with the firmware,
milestone by milestone (the parameter page, NAND image, bitflips and READ_PAGES streaming come at M3-M5).

The simulated chip is an S34ML02G100 with WP# low. Hardware faults: chip_id (e.g. all FFh = no chip), wp_high
(SR bit 7 set), rb_stuck_low (RESET times out), rb_never_low (RESET reports busy_ns = 0), id_glitches (a list of
IDs returned by the next READ_IDs at addr 00h).

Transport faults can be injected with inject(). Each queued fault applies to the next non-ABORT request received
(ABORTs are the client's own recovery traffic, docs/PROTOCOL.md "Host recovery rule").
"""

from __future__ import annotations

import struct
from enum import Enum, auto

from nand_tool.protocol import (
    CRC_LEN,
    MAGIC_REQ,
    MAX_ARGS,
    PAGE_NONE,
    PROTO_VERSION,
    REQ_HDR_LEN,
    TIMING_WIRE_LEN,
    Cmd,
    Status,
    Timing,
    TimingMode,
    crc32,
    encode_response,
)


class Fault(Enum):
    DROP = auto()             # response is lost
    FLIP = auto()             # one response byte is corrupted
    TRUNCATE = auto()         # only the first half of the response arrives
    GARBAGE_PREFIX = auto()   # junk bytes arrive before the response
    STALE_FRAME = auto()      # a valid frame with an old seq arrives before the response
    CORRUPT_REQUEST = auto()  # the request is corrupted on the way in -> device answers ERR_CRC


class FakeDevice:
    def __init__(
        self,
        *,
        clk_hz: int = 125_000_000,
        fw_version: tuple[int, int, int] = (0, 1, 0),
        version_string: str = "pico-nand-tool 0.1.0 (fake)",
        proto_version: int = PROTO_VERSION,
        chip_id: bytes = bytes.fromhex("01DA909544"),
        onfi: bytes = b"ONFI",
        wp_high: bool = False,
        rb_stuck_low: bool = False,
        rb_never_low: bool = False,
        reset_busy_ns: int = 3200,
    ):
        self.clk_hz = clk_hz
        self.fw_version = fw_version
        self.version_string = version_string
        self.proto_version = proto_version
        self.timing = Timing.default()
        self.timing_mode = TimingMode.DEFAULT
        self.requests: list[tuple[int, int, bytes]] = []  # (cmd, seq, args) of every CRC-valid request
        self.closed = False
        self.chip_id = chip_id
        self.onfi = onfi
        self.wp_high = wp_high
        self.rb_stuck_low = rb_stuck_low
        self.rb_never_low = rb_never_low
        self.reset_busy_ns = reset_busy_ns
        self.id_glitches: list[bytes] = []
        self.sr = self._sr_after_reset()  # power-on state equals the reset state (§3.12)
        self._rx = bytearray()
        self._tx = bytearray()
        self._faults: list[Fault] = []

    def inject(self, *faults: Fault) -> None:
        self._faults.extend(faults)

    # ---- Transport interface -----------------------------------------------------------------------------

    def write(self, data: bytes) -> None:
        self._rx += data
        self._process()

    def read(self, n: int) -> bytes:
        out = bytes(self._tx[:n])
        del self._tx[:n]
        return out

    def reset_input(self) -> None:
        self._tx.clear()

    def close(self) -> None:
        self.closed = True

    # ---- chip model -----------------------------------------------------------------------------------------

    def _sr_after_reset(self) -> int:
        return 0xE0 if self.wp_high else 0x60  # §3.12

    def _id_bytes(self, addr: int, n: int) -> bytes:
        if addr == 0x20:
            base = self.onfi  # §3.18: beyond 4 bytes is indeterminate; the fake repeats the signature
        elif self.id_glitches:
            base = self.id_glitches.pop(0)
        else:
            base = self.chip_id
        return (base * (n // len(base) + 1))[:n]

    # ---- firmware model ----------------------------------------------------------------------------------

    def _process(self) -> None:
        """Same framing rules as firmware/src/frame.c."""
        while True:
            i = self._rx.find(MAGIC_REQ)
            if i < 0:
                self._rx.clear()
                return
            del self._rx[:i]
            if len(self._rx) < REQ_HDR_LEN:
                return
            arg_len = self._rx[3]
            if arg_len > MAX_ARGS:  # false magic: rescan from the next byte
                del self._rx[:1]
                continue
            total = REQ_HDR_LEN + arg_len + CRC_LEN
            if len(self._rx) < total:
                return
            frame = bytes(self._rx[:total])
            del self._rx[:total]
            self._handle_frame(frame)

    def _handle_frame(self, frame: bytes) -> None:
        cmd, seq, arg_len = frame[1], frame[2], frame[3]
        body = frame[: REQ_HDR_LEN + arg_len]
        (crc,) = struct.unpack("<I", frame[-CRC_LEN:])
        fault = self._faults.pop(0) if self._faults and cmd != Cmd.ABORT else None

        if fault is Fault.CORRUPT_REQUEST or crc32(body) != crc:
            self._tx += encode_response(cmd, seq, Status.ERR_CRC)
            return
        args = body[REQ_HDR_LEN:]
        self.requests.append((cmd, seq, args))
        self._emit(self._execute(cmd, seq, args), fault, seq)

    def _emit(self, frame: bytes, fault: Fault | None, seq: int) -> None:
        if fault is Fault.DROP:
            return
        if fault is Fault.FLIP:
            b = bytearray(frame)
            b[len(b) // 2] ^= 0x40
            frame = bytes(b)
        elif fault is Fault.TRUNCATE:
            frame = frame[: len(frame) // 2]
        elif fault is Fault.GARBAGE_PREFIX:
            frame = b"\x00\x13\x5a\xff" + frame
        elif fault is Fault.STALE_FRAME:
            frame = encode_response(Cmd.PING, (seq - 1) & 0xFF, Status.OK) + frame
        self._tx += frame

    def _execute(self, cmd: int, seq: int, args: bytes) -> bytes:
        def reply(status: int, payload: bytes = b"") -> bytes:
            return encode_response(cmd, seq, status, PAGE_NONE, payload)

        if cmd == Cmd.PING:
            if args:
                return reply(Status.ERR_BAD_ARGS)
            major, minor, patch = self.fw_version
            head = struct.pack("<BBBBI", self.proto_version, major, minor, patch, self.clk_hz)
            return reply(Status.OK, head + self.version_string.encode("ascii"))

        if cmd == Cmd.SET_TIMING:
            mode = args[0] if args else None
            if len(args) == 1 and mode == TimingMode.DEFAULT:
                self.timing, self.timing_mode = Timing.default(), TimingMode.DEFAULT
            elif len(args) == 1 and mode == TimingMode.SLOW:
                self.timing, self.timing_mode = Timing.slow(), TimingMode.SLOW
            elif len(args) == 1 + TIMING_WIRE_LEN and mode == TimingMode.CUSTOM:
                t = Timing.unpack(args[1:])
                if not t.meets_floors(self.clk_hz):
                    return reply(Status.ERR_TIMING_FLOOR)
                self.timing, self.timing_mode = t, TimingMode.CUSTOM
            elif not (len(args) == 1 and mode == TimingMode.QUERY):
                return reply(Status.ERR_BAD_ARGS)
            return reply(Status.OK, bytes((self.timing_mode,)) + self.timing.pack())

        if cmd == Cmd.RESET:
            if args:
                return reply(Status.ERR_BAD_ARGS)
            if self.rb_stuck_low:
                return reply(Status.ERR_RB_TIMEOUT)
            self.sr = self._sr_after_reset()
            return reply(Status.OK, struct.pack("<I", 0 if self.rb_never_low else self.reset_busy_ns))

        if cmd == Cmd.READ_ID:
            if len(args) not in (0, 2):
                return reply(Status.ERR_BAD_ARGS)
            addr, n = (args[0], args[1]) if args else (0x00, 5)
            if addr not in (0x00, 0x20) or not 1 <= n <= 8:
                return reply(Status.ERR_BAD_ARGS)
            return reply(Status.OK, self._id_bytes(addr, n))

        if cmd == Cmd.READ_STATUS:
            if args:
                return reply(Status.ERR_BAD_ARGS)
            return reply(Status.OK, bytes((self.sr,)))

        if cmd == Cmd.ABORT:
            return reply(Status.ERR_BAD_ARGS if args else Status.OK)

        return reply(Status.ERR_UNKNOWN_CMD)
