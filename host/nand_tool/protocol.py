"""Wire protocol: constants and pure encode/decode (no I/O).

Canonical definition: docs/PROTOCOL.md. Mirrors firmware/src/protocol_defs.h; tests/test_protocol.py checks that
the two agree.

Frame layouts (all integers little-endian):

    request   A5 | cmd | seq | arg_len u16 | args[arg_len]                       | crc32 u32
    response  5A | cmd | seq | status | page u32 | len u16 | payload[len]          | crc32 u32

The `struct` format strings below spell these out: "<" = little-endian with no padding, B = u8, H = u16, I = u32.
So "<BBBBIH" is magic, cmd, seq, status (4 x u8), page (u32), len (u16): the 10-byte response header.

This module does no I/O, which makes it easy to unit-test and to share between the real client and the fake device.
"""

from __future__ import annotations

import binascii
import struct
from dataclasses import astuple, dataclass, fields
from enum import IntEnum
from math import ceil

from .errors import FrameError

PROTO_VERSION = 2

MAGIC_REQ = 0xA5
MAGIC_RESP = 0x5A

REQ_HDR_LEN = 5
RESP_HDR_LEN = 10
CRC_LEN = 4
MAX_ARGS = 2116  # PROGRAM_PAGE: u32 page + 2112 bytes
MAX_PAYLOAD = 2112
REQ_TIMEOUT_MS = 100

PAGE_NONE = 0xFFFFFFFF
END_FLAG = 0x80

PARAM_LEN = 768  # READ_PARAM payload: 3 x 256-byte parameter page copies
PAGE_LEN = 2112  # READ_PAGES page frame payload: 2048 data + 64 spare
TOTAL_PAGES = 131072  # READ_PAGES: start + count must not exceed this
END_LEN = 8  # READ_PAGES end frame payload: u32 pages_sent, u32 pages_failed
PAGES_PER_BLOCK = 64
BLOCKS = 2048

# Write mode (docs/PROTOCOL.md "Write mode")
ARM_TOKEN = 0x4D524157  # bytes 57 41 52 4D, "WARM"
ARM_IDLE_MAX_S = 60
ARM_LEN = 10  # ARM_WRITE args: u32 token, u16 first_block, u16 last_block, u16 idle_timeout_s
ERASE_LEN = 3  # ERASE_BLOCK args: u16 block, u8 flags
ERASE_IGNORE_BAD_MARKER = 0x01
WRITE_RESP_LEN = 5  # ERASE_BLOCK / PROGRAM_PAGE payload: u8 sr, u32 busy_ns
PROGRAM_TIMEOUT_US = 2000
ERASE_TIMEOUT_US = 20000


# IntEnum members are real ints (Cmd.PING == 1, so they pack straight into bytes), and still print by name in
# messages (Cmd(1).name == "PING").
class Cmd(IntEnum):
    PING = 0x01
    BUS_TEST = 0x02
    SET_TIMING = 0x03
    RESET = 0x04
    READ_ID = 0x05
    READ_STATUS = 0x06
    READ_PARAM = 0x07
    READ_PAGES = 0x08
    ABORT = 0x09
    ARM_WRITE = 0x0A
    DISARM = 0x0B
    ERASE_BLOCK = 0x0C
    PROGRAM_PAGE = 0x0D


class Status(IntEnum):
    OK = 0x00
    ERR_CRC = 0x01
    ERR_UNKNOWN_CMD = 0x02
    ERR_BAD_ARGS = 0x03
    ERR_RB_TIMEOUT = 0x04
    ERR_ABORTED = 0x05
    ERR_BUSY = 0x06
    ERR_TIMING_FLOOR = 0x07
    ERR_NOT_ARMED = 0x08
    ERR_BAD_BLOCK = 0x09
    ERR_OP_FAILED = 0x0A
    ERR_WP_STUCK = 0x0B


class TimingMode(IntEnum):
    QUERY = 0
    DEFAULT = 1
    SLOW = 2
    CUSTOM = 3


TIMING_FIELDS = 13
TIMING_WIRE_LEN = 30
TIMING_DEFAULT_CYCLES = (6, 3, 4, 3, 15, 8, 3, 25, 25, 6, 8, 18, 25)
TIMING_SLOW_CYCLES = 125
TIMING_RB_TIMEOUT_US = 1000
TIMING_RB_TIMEOUT_MAX_US = 100000

# SET_TIMING floors: datasheet Table 20 (ns), checked against sums of timing_t phases (docs/PROTOCOL.md)
FLOOR_TCS_NS = 20
FLOOR_TCR_NS = 10
FLOOR_TSETUP_NS = 10
FLOOR_TWP_NS = 12
FLOOR_THOLD_NS = 5
FLOOR_TWH_NS = 10
FLOOR_TWC_NS = 25
FLOOR_TWHR_NS = 60
FLOOR_TREA_NS = 20
FLOOR_TREH_NS = 10
FLOOR_TRC_NS = 25
FLOOR_TRHW_NS = 100
FLOOR_TWB_NS = 100
FLOOR_TRR_NS = 20
FLOOR_TCHZ_NS = 30
FLOOR_TADL_NS = 70
FLOOR_TWW_NS = 100
FLOOR_INPUT_SYNC_CYCLES = 2
FLOOR_RB_TIMEOUT_US = 25

_RESP_HDR = struct.Struct("<BBBBIH")
_TIMING = struct.Struct("<13HI")
assert _TIMING.size == TIMING_WIRE_LEN


def crc32(data: bytes) -> int:
    """IEEE 802.3 / zlib CRC-32 (the same function as firmware/src/crc32.c; check value crc32(b"123456789") is
    0xCBF43926). The & 0xFFFFFFFF keeps the result an unsigned 32-bit value, as on the wire."""
    return binascii.crc32(data) & 0xFFFFFFFF


def encode_request(cmd: int, seq: int, args: bytes = b"") -> bytes:
    if len(args) > MAX_ARGS:
        raise ValueError(f"args too long ({len(args)} > {MAX_ARGS})")
    body = struct.pack("<BBBH", MAGIC_REQ, cmd & 0xFF, seq & 0xFF, len(args)) + args
    return body + struct.pack("<I", crc32(body))


def encode_response(cmd: int, seq: int, status: int, page: int = PAGE_NONE, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload too long")
    body = _RESP_HDR.pack(MAGIC_RESP, cmd, seq, status, page, len(payload)) + payload
    return body + struct.pack("<I", crc32(body))


@dataclass(frozen=True)
class Response:
    cmd: int
    seq: int
    status: int
    page: int
    payload: bytes

    @property
    def is_end(self) -> bool:
        return bool(self.cmd & END_FLAG)

    @property
    def ok(self) -> bool:
        return self.status == Status.OK


def parse_response_header(hdr: bytes) -> tuple[int, int, int, int, int]:
    """Validate a 10-byte response header; return (cmd, seq, status, page, payload_len).

    Split out from decode_response() because a reader needs the header first: only its `len` field says how many more
    bytes make up the frame (Client.read_response)."""
    magic, cmd, seq, status, page, length = _RESP_HDR.unpack(hdr)
    if magic != MAGIC_RESP:
        raise FrameError(f"bad magic 0x{magic:02X}")
    if length > MAX_PAYLOAD:
        raise FrameError(f"payload length {length} > {MAX_PAYLOAD}")
    return cmd, seq, status, page, length


def decode_response(frame: bytes) -> Response:
    """Decode one complete response frame (header + payload + CRC)."""
    if len(frame) < RESP_HDR_LEN + CRC_LEN:
        raise FrameError("frame too short")
    cmd, seq, status, page, length = parse_response_header(frame[:RESP_HDR_LEN])
    if len(frame) != RESP_HDR_LEN + length + CRC_LEN:
        raise FrameError("frame length does not match header")
    body, (crc,) = frame[:-CRC_LEN], struct.unpack("<I", frame[-CRC_LEN:])
    if crc32(body) != crc:
        raise FrameError("CRC mismatch")
    return Response(cmd, seq, status, page, bytes(body[RESP_HDR_LEN:]))


def _cycles(ns: int, clk_hz: int) -> int:
    return ceil(ns * clk_hz / 1_000_000_000)



@dataclass
class Timing:
    """timing_t: delays in clk_sys cycles (each a minimum), then the R/B# timeout. Field order is wire order.

    What each field times is documented on the C struct (firmware/src/timing.h). Because field order is wire order,
    pack()/unpack() can use dataclasses.astuple() and the "<13HI" struct (13 x u16, then u32)."""

    t_cs: int
    t_setup: int
    t_wp: int
    t_wh: int
    t_whr: int
    t_rea: int
    t_reh: int
    t_rhw: int
    t_wb: int
    t_rr: int
    t_ceh: int
    t_adl: int
    t_ww: int
    rb_timeout_us: int

    @classmethod
    def default(cls) -> Timing:
        return cls(*TIMING_DEFAULT_CYCLES, TIMING_RB_TIMEOUT_US)

    @classmethod
    def slow(cls) -> Timing:
        return cls(*([TIMING_SLOW_CYCLES] * TIMING_FIELDS), TIMING_RB_TIMEOUT_US)

    @classmethod
    def unpack(cls, data: bytes) -> Timing:
        if len(data) != TIMING_WIRE_LEN:
            raise FrameError(f"timing_t is {len(data)} bytes, expected {TIMING_WIRE_LEN}")
        return cls(*_TIMING.unpack(data))

    def pack(self) -> bytes:
        return _TIMING.pack(*astuple(self))

    def meets_floors(self, clk_hz: int) -> bool:
        """Mirror of firmware timing_meets_floors() (docs/PROTOCOL.md floors table)."""
        c = lambda ns: _cycles(ns, clk_hz)  # noqa: E731
        t = self
        return (
            t.t_cs + t.t_setup + t.t_wp >= c(FLOOR_TCS_NS)
            and t.t_cs >= c(FLOOR_TCR_NS)
            and t.t_setup + t.t_wp >= c(FLOOR_TSETUP_NS)
            and t.t_wp >= c(FLOOR_TWP_NS)
            and t.t_wh >= c(FLOOR_THOLD_NS)
            and t.t_wh + t.t_setup >= c(FLOOR_TWH_NS)
            and t.t_setup + t.t_wp + t.t_wh >= c(FLOOR_TWC_NS)
            and t.t_whr >= c(FLOOR_TWHR_NS)
            and t.t_rea >= c(FLOOR_TREA_NS) + FLOOR_INPUT_SYNC_CYCLES
            and t.t_reh >= c(FLOOR_TREH_NS)
            and t.t_rea + t.t_reh >= c(FLOOR_TRC_NS)
            and t.t_rhw >= c(FLOOR_TRHW_NS)
            and t.t_wb >= c(FLOOR_TWB_NS)
            and t.t_rr >= c(FLOOR_TRR_NS)
            and t.t_ceh >= c(FLOOR_TCHZ_NS)
            and t.t_adl >= c(FLOOR_TADL_NS)
            and t.t_ww >= c(FLOOR_TWW_NS)
            and FLOOR_RB_TIMEOUT_US <= t.rb_timeout_us <= TIMING_RB_TIMEOUT_MAX_US
        )

    def describe(self, clk_hz: int) -> str:
        ns = 1e9 / clk_hz
        rows = [
            f"  {f.name:<8} {getattr(self, f.name):>5} cyc  {getattr(self, f.name) * ns:>8.0f} ns"
            for f in fields(self)
            if f.name != "rb_timeout_us"
        ]
        rows.append(f"  {'rb_timeout':<8} {self.rb_timeout_us:>5} us")
        return "\n".join(rows)


@dataclass(frozen=True)
class PingInfo:
    proto_version: int
    fw_version: tuple[int, int, int]
    clk_hz: int
    version_string: str

    @classmethod
    def unpack(cls, payload: bytes) -> PingInfo:
        if len(payload) < 8:
            raise FrameError("PING payload too short")
        proto, major, minor, patch, clk = struct.unpack("<BBBBI", payload[:8])
        if clk == 0:
            raise FrameError("PING reports clk_sys = 0")
        return cls(proto, (major, minor, patch), clk, payload[8:].decode("ascii", errors="replace"))
