"""Fake Pico NAND Tool: simulates the firmware's wire protocol in-process and implements the Transport interface.

Mirrors the firmware as of protocol v2 (write mode): everything except BUS_TEST (which answers ERR_UNKNOWN_CMD, like
the real firmware). READ_PAGES streams lazily: page frames are produced as the host reads them, so ABORT and ERR_BUSY interleave
between frames as on the real device.

The simulated chip is an S34ML02G100 with WP# low. Hardware faults: chip_id (e.g. all FFh = no chip), wp_high
(SR bit 7 set), rb_stuck_low (RESET times out), rb_never_low (RESET reports busy_ns = 0), id_glitches (a list of
IDs returned by the next READ_IDs at addr 00h), param (the 768 bytes READ_PARAM returns; default: the Table 3.4
reference page, three times).

NAND image: page_data(p) is deterministic synthetic data (every 5th page erased = all FFh). Data faults: flaky_bits
{page: [(offset, mask), ...]} XORs those bits on the reads of that page where flip_on(page, n) is true (n = 1 for
the first read; default: every 2nd read); rb_timeouts {page: n} makes the next n
reads of that page report ERR_RB_TIMEOUT; page_faults {page: Fault} corrupts that page's frame in transit, once.

Write mode (docs/PROTOCOL.md "Write mode"): the chip is a real NAND state model. Pages live in `pages` (unset pages
read as page_data(p)); erase sets a block to FFh; program ANDs the data in (bits only go 1 -> 0) and counts programs
per page since the last erase (`nop`; more than 4 is recorded in `nop_violations`). `bad_blocks` is the set of
factory bad blocks (spare byte 0 of page 0 reads 00h). Arming follows the firmware (token, range, idle timeout on
`clock()`, disarm on errors / RESET). `ops` logs every erase and program that reached the "chip". Faults:
erase_fail / program_fail (blocks / pages that end with SR fail), erase_timeout / program_timeout (R/B# timeout),
wp_stuck (SR bit 7 stays 0, nothing changes).

Transport faults can be injected with inject(). Each queued fault applies to the next non-ABORT request received
(ABORTs are the client's own recovery traffic, docs/PROTOCOL.md "Host recovery rule"). cmd_faults[cmd] does the same
for the next requests with that command only.
"""

from __future__ import annotations

import random
import struct
import time
from dataclasses import dataclass
from enum import Enum, auto

from golden_param import GOLDEN_PARAM_X3

from nand_tool.protocol import (
    ARM_IDLE_MAX_S,
    ARM_LEN,
    ARM_TOKEN,
    BLOCKS,
    CRC_LEN,
    ERASE_IGNORE_BAD_MARKER,
    ERASE_LEN,
    END_FLAG,
    MAGIC_REQ,
    MAX_ARGS,
    PAGE_LEN,
    PAGE_NONE,
    PAGES_PER_BLOCK,
    PROTO_VERSION,
    REQ_HDR_LEN,
    TIMING_WIRE_LEN,
    TOTAL_PAGES,
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


def page_data(page: int) -> bytes:
    """The fake NAND image: every 5th page erased, the rest pseudo-random with a good-block marker (spare byte 0)."""
    if page % 5 == 4:
        return b"\xff" * PAGE_LEN
    data = bytearray(random.Random(page).randbytes(PAGE_LEN))
    data[2048] = 0xFF
    return bytes(data)


@dataclass
class _Stream:
    seq: int
    nxt: int
    end: int
    first_fault: Fault | None
    sent: int = 0
    failed: int = 0
    abort_seq: int | None = None


class FakeDevice:
    def __init__(
        self,
        *,
        clk_hz: int = 125_000_000,
        fw_version: tuple[int, int, int] = (0, 2, 0),
        version_string: str = "pico-nand-tool 0.2.0 (fake)",
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
        self.param = GOLDEN_PARAM_X3
        self.flaky_bits: dict[int, list[tuple[int, int]]] = {}
        self.rb_timeouts: dict[int, int] = {}
        self.page_faults: dict[int, Fault] = {}
        self.page_reads: dict[int, int] = {}  # page -> number of times read from the array
        self.flip_on = lambda page, n: n % 2 == 0
        # write mode
        self.pages: dict[int, bytes] = {}
        self.bad_blocks: set[int] = set()
        self.nop: dict[int, int] = {}
        self.nop_violations: list[int] = []
        self.ops: list[tuple[str, int]] = []
        self.erase_fail: set[int] = set()
        self.program_fail: set[int] = set()
        self.erase_timeout: set[int] = set()
        self.program_timeout: set[int] = set()
        self.wp_stuck = False
        self.armed: tuple[int, int, float] | None = None  # (first_block, last_block, idle_s)
        self.last_use = 0.0
        self.clock = time.monotonic
        self._stream: _Stream | None = None
        self.sr = self._sr_after_reset()  # power-on state equals the reset state (§3.12)
        self._rx = bytearray()
        self._tx = bytearray()
        self._faults: list[Fault] = []
        self.cmd_faults: dict[int, list[Fault]] = {}  # cmd -> faults for the next requests with that cmd

    def inject(self, *faults: Fault) -> None:
        self._faults.extend(faults)

    # ---- Transport interface -----------------------------------------------------------------------------

    def write(self, data: bytes) -> None:
        self._rx += data
        self._process()

    def read(self, n: int) -> bytes:
        while self._stream is not None and len(self._tx) < n:
            self._stream_step()
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
            arg_len = self._rx[3] | self._rx[4] << 8
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
        cmd, seq = frame[1], frame[2]
        arg_len = frame[3] | frame[4] << 8
        body = frame[: REQ_HDR_LEN + arg_len]
        (crc,) = struct.unpack("<I", frame[-CRC_LEN:])
        fault = self._faults.pop(0) if self._faults and cmd != Cmd.ABORT else None
        if fault is None and self.cmd_faults.get(cmd):
            fault = self.cmd_faults[cmd].pop(0)

        if fault is Fault.CORRUPT_REQUEST or crc32(body) != crc:
            self._tx += encode_response(cmd, seq, Status.ERR_CRC)
            return
        args = body[REQ_HDR_LEN:]
        self.requests.append((cmd, seq, args))
        st = self._stream
        if st is not None:  # same rules as the firmware's stream_on_req()
            if cmd == Cmd.ABORT and not args and st.abort_seq is None:
                st.abort_seq = seq
            else:
                self._emit(encode_response(cmd, seq, Status.ERR_BUSY), fault, seq)
            return
        if cmd == Cmd.READ_PAGES:
            self._start_stream(seq, args, fault)
            return
        self._emit(self._execute(cmd, seq, args), fault, seq)

    # ---- READ_PAGES stream ----------------------------------------------------------------------------------

    def _start_stream(self, seq: int, args: bytes, fault: Fault | None) -> None:
        start, count = struct.unpack("<II", args) if len(args) == 8 else (0, 0)
        if count == 0 or start >= TOTAL_PAGES or count > TOTAL_PAGES - start:
            self._emit(encode_response(Cmd.READ_PAGES, seq, Status.ERR_BAD_ARGS), fault, seq)
            return
        self._stream = _Stream(seq, start, start + count, fault)

    def chip_page(self, page: int) -> bytes:
        """What the array holds, without read faults."""
        if page in self.pages:
            return self.pages[page]
        data = page_data(page)
        if page // PAGES_PER_BLOCK in self.bad_blocks and page % PAGES_PER_BLOCK == 0:
            data = data[:2048] + b"\x00" + data[2049:]  # factory bad-block marker (§9.2)
        return data

    def erase(self, block: int) -> None:
        """Chip-model erase (also usable by tests to prepare blank blocks)."""
        for p in range(block * PAGES_PER_BLOCK, (block + 1) * PAGES_PER_BLOCK):
            self.pages[p] = b"\xff" * PAGE_LEN
            self.nop[p] = 0

    def read_page(self, page: int) -> bytes:
        n = self.page_reads[page] = self.page_reads.get(page, 0) + 1
        data = bytearray(self.chip_page(page))
        if self.flip_on(page, n):
            for offset, mask in self.flaky_bits.get(page, []):
                data[offset] ^= mask
        return bytes(data)

    def _stream_step(self) -> None:
        st = self._stream
        assert st is not None
        if st.abort_seq is not None or st.nxt == st.end:
            status = Status.OK if st.abort_seq is None else Status.ERR_ABORTED
            end = struct.pack("<II", st.sent, st.failed)
            self._tx += encode_response(Cmd.READ_PAGES | END_FLAG, st.seq, status, st.nxt, end)
            if st.abort_seq is not None:
                self._tx += encode_response(Cmd.ABORT, st.abort_seq, Status.OK)
            self._stream = None
            return
        p = st.nxt
        st.nxt += 1
        st.sent += 1
        if self.rb_timeouts.get(p, 0) > 0:
            self.rb_timeouts[p] -= 1
            st.failed += 1
            frame = encode_response(Cmd.READ_PAGES, st.seq, Status.ERR_RB_TIMEOUT, p)
        else:
            frame = encode_response(Cmd.READ_PAGES, st.seq, Status.OK, p, self.read_page(p))
        fault = self.page_faults.pop(p, None) or st.first_fault
        st.first_fault = None
        self._emit(frame, fault, st.seq)

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
            self.armed = None  # RESET disarms
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

        if cmd == Cmd.READ_PARAM:
            if args:
                return reply(Status.ERR_BAD_ARGS)
            if self.rb_stuck_low:
                return reply(Status.ERR_RB_TIMEOUT)
            self.sr = self._sr_after_reset()  # the firmware issues FFh first (§3.19 note)
            return reply(Status.OK, self.param)

        if cmd == Cmd.READ_STATUS:
            if args:
                return reply(Status.ERR_BAD_ARGS)
            return reply(Status.OK, bytes((self.sr,)))

        if cmd == Cmd.ABORT:
            return reply(Status.ERR_BAD_ARGS if args else Status.OK)

        if cmd == Cmd.ARM_WRITE:
            if len(args) != ARM_LEN:
                return reply(Status.ERR_BAD_ARGS)
            token, first, last, idle = struct.unpack("<IHHH", args)
            if token != ARM_TOKEN or first > last or last >= BLOCKS or not 1 <= idle <= ARM_IDLE_MAX_S:
                return reply(Status.ERR_BAD_ARGS)
            self.armed = (first, last, idle)
            self.last_use = self.clock()
            return reply(Status.OK)

        if cmd == Cmd.DISARM:
            if args:
                return reply(Status.ERR_BAD_ARGS)
            self.armed = None
            return reply(Status.OK)

        if cmd == Cmd.ERASE_BLOCK:
            block, flags = struct.unpack("<HB", args) if len(args) == ERASE_LEN else (BLOCKS, 0)
            if block >= BLOCKS or flags & ~ERASE_IGNORE_BAD_MARKER:
                return reply(Status.ERR_BAD_ARGS)
            first_page = block * PAGES_PER_BLOCK
            if not self._armed_for(block):
                return encode_response(cmd, seq, Status.ERR_NOT_ARMED, first_page)
            if not flags & ERASE_IGNORE_BAD_MARKER:
                if any(self.chip_page(first_page + p)[2048] != 0xFF for p in (0, 1, PAGES_PER_BLOCK - 1)):
                    return encode_response(cmd, seq, Status.ERR_BAD_BLOCK, first_page)
            return self._write_op(cmd, seq, first_page, "erase", block)

        if cmd == Cmd.PROGRAM_PAGE:
            if len(args) != 4 + PAGE_LEN or struct.unpack("<I", args[:4])[0] >= TOTAL_PAGES:
                return reply(Status.ERR_BAD_ARGS)
            page = struct.unpack("<I", args[:4])[0]
            if not self._armed_for(page // PAGES_PER_BLOCK):
                return encode_response(cmd, seq, Status.ERR_NOT_ARMED, page)
            return self._write_op(cmd, seq, page, "program", page, args[4:])

        return reply(Status.ERR_UNKNOWN_CMD)

    def _armed_for(self, block: int) -> bool:
        if self.armed and self.clock() - self.last_use > self.armed[2]:
            self.armed = None  # idle timeout
        return self.armed is not None and self.armed[0] <= block <= self.armed[1]

    def _write_op(self, cmd: int, seq: int, page: int, kind: str, target: int, data: bytes = b"") -> bytes:
        """The firmware's finish(): run the op, then SR checks (bit 7, then bits 6/0). Failures disarm."""
        if self.wp_stuck:  # WP# never rose: the chip ignored the command
            self.armed = None
            self.sr = 0x60
            return encode_response(cmd, seq, Status.ERR_WP_STUCK, page, struct.pack("<BI", 0x60, 0))
        timeouts = self.erase_timeout if kind == "erase" else self.program_timeout
        if target in timeouts:  # WP# low aborts the op: content undefined until the next erase (§4.3)
            self.armed = None
            self.ops.append((kind + "-aborted", target))
            for p in range(page, page + (PAGES_PER_BLOCK if kind == "erase" else 1)):
                self.pages[p] = random.Random(p ^ 0x5A5A).randbytes(PAGE_LEN)
            self.sr = self._sr_after_reset()
            return encode_response(cmd, seq, Status.ERR_RB_TIMEOUT, page)
        self.ops.append((kind, target))
        if kind == "erase":
            self.erase(target)
            busy_ns = 3_500_000
        else:
            old = self.chip_page(target)
            self.pages[target] = bytes(a & b for a, b in zip(old, data))
            self.nop[target] = self.nop.get(target, 0) + 1
            if self.nop[target] > 4:  # Table 23: NOP = 4
                self.nop_violations.append(target)
            busy_ns = 200_000
        fails = self.erase_fail if kind == "erase" else self.program_fail
        if target in fails:
            self.armed = None
            self.sr = 0x61
            return encode_response(cmd, seq, Status.ERR_OP_FAILED, page, struct.pack("<BI", 0xE1, busy_ns))
        self.last_use = self.clock()
        self.sr = 0x60  # READ_STATUS afterwards: WP# low again
        return encode_response(cmd, seq, Status.OK, page, struct.pack("<BI", 0xE0, busy_ns))
