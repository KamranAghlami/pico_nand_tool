"""Wire protocol: constants and pure encode/decode (no I/O).

Canonical definition: docs/PROTOCOL.md. Mirrors firmware/src/protocol_defs.h; tests/test_protocol.py checks that
the two agree.
"""

from __future__ import annotations

import binascii
import struct
from dataclasses import astuple, dataclass, fields
from enum import IntEnum
from math import ceil

PROTO_VERSION = 1

MAGIC_REQ = 0xA5
MAGIC_RESP = 0x5A

REQ_HDR_LEN = 4
RESP_HDR_LEN = 10
CRC_LEN = 4
MAX_ARGS = 32
MAX_PAYLOAD = 2112
REQ_TIMEOUT_MS = 100

PAGE_NONE = 0xFFFFFFFF
END_FLAG = 0x80


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


class Status(IntEnum):
    OK = 0x00
    ERR_CRC = 0x01
    ERR_UNKNOWN_CMD = 0x02
    ERR_BAD_ARGS = 0x03
    ERR_RB_TIMEOUT = 0x04
    ERR_ABORTED = 0x05
    ERR_BUSY = 0x06
    ERR_TIMING_FLOOR = 0x07


class TimingMode(IntEnum):
    QUERY = 0
    DEFAULT = 1
    SLOW = 2
    CUSTOM = 3


TIMING_FIELDS = 11
TIMING_WIRE_LEN = 26
TIMING_DEFAULT_CYCLES = (6, 3, 4, 3, 15, 8, 3, 25, 25, 6, 8)
TIMING_SLOW_CYCLES = 125
TIMING_RB_TIMEOUT_US = 1000
TIMING_RB_TIMEOUT_MAX_US = 100000

_RESP_HDR = struct.Struct("<BBBBIH")
_TIMING = struct.Struct("<11HI")
assert _TIMING.size == TIMING_WIRE_LEN


def crc32(data: bytes) -> int:
    """IEEE 802.3 / zlib CRC-32."""
    return binascii.crc32(data) & 0xFFFFFFFF


def encode_request(cmd: int, seq: int, args: bytes = b"") -> bytes:
    if len(args) > MAX_ARGS:
        raise ValueError(f"args too long ({len(args)} > {MAX_ARGS})")
    body = bytes((MAGIC_REQ, cmd & 0xFF, seq & 0xFF, len(args))) + args
    return body + struct.pack("<I", crc32(body))


def encode_response(cmd: int, seq: int, status: int, page: int = PAGE_NONE, payload: bytes = b"") -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("payload too long")
    body = _RESP_HDR.pack(MAGIC_RESP, cmd, seq, status, page, len(payload)) + payload
    return body + struct.pack("<I", crc32(body))


class FrameError(Exception):
    """Received bytes do not form a valid response frame (bad magic, length or CRC)."""


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
    """Validate a 10-byte response header; return (cmd, seq, status, page, payload_len)."""
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


INPUT_SYNC_CYCLES = 2


@dataclass
class Timing:
    """timing_t: delays in clk_sys cycles (each a minimum), then the R/B# timeout. Field order is wire order."""

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
    rb_timeout_us: int

    @classmethod
    def default(cls) -> Timing:
        return cls(*TIMING_DEFAULT_CYCLES, TIMING_RB_TIMEOUT_US)

    @classmethod
    def slow(cls) -> Timing:
        return cls(*([TIMING_SLOW_CYCLES] * TIMING_FIELDS), TIMING_RB_TIMEOUT_US)

    @classmethod
    def unpack(cls, data: bytes) -> Timing:
        return cls(*_TIMING.unpack(data))

    def pack(self) -> bytes:
        return _TIMING.pack(*astuple(self))

    def meets_floors(self, clk_hz: int) -> bool:
        """Mirror of firmware timing_meets_floors() (docs/PROTOCOL.md floors table)."""
        c = lambda ns: _cycles(ns, clk_hz)  # noqa: E731
        t = self
        return (
            t.t_cs + t.t_setup + t.t_wp >= c(20)
            and t.t_cs >= c(10)
            and t.t_setup + t.t_wp >= c(10)
            and t.t_wp >= c(12)
            and t.t_wh >= c(5)
            and t.t_wh + t.t_setup >= c(10)
            and t.t_setup + t.t_wp + t.t_wh >= c(25)
            and t.t_whr >= c(60)
            and t.t_rea >= c(20) + INPUT_SYNC_CYCLES
            and t.t_reh >= c(10)
            and t.t_rea + t.t_reh >= c(25)
            and t.t_rhw >= c(100)
            and t.t_wb >= c(100)
            and t.t_rr >= c(20)
            and t.t_ceh >= c(30)
            and 25 <= t.rb_timeout_us <= TIMING_RB_TIMEOUT_MAX_US
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
        return cls(proto, (major, minor, patch), clk, payload[8:].decode("ascii", errors="replace"))
