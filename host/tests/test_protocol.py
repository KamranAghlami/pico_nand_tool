import re
import struct
from pathlib import Path

import pytest

from nand_tool import protocol as P
from nand_tool.protocol import Cmd, FrameError, PingInfo, Status, Timing, TimingMode

PROTOCOL_DEFS_H = Path(__file__).resolve().parents[2] / "firmware" / "src" / "protocol_defs.h"
CLK = 125_000_000


def test_crc32_check_value():
    assert P.crc32(b"123456789") == 0xCBF43926  # docs/PROTOCOL.md


def test_encode_request_layout():
    f = P.encode_request(Cmd.READ_PAGES, 7, b"\x10\x20\x30")
    assert f[:5] == bytes((0xA5, 0x08, 7, 3, 0))  # u16 arg_len (v2)
    assert f[5:8] == b"\x10\x20\x30"
    assert struct.unpack("<I", f[8:]) == (P.crc32(f[:8]),)
    assert len(P.encode_request(Cmd.PING, 1)) == 9
    big = P.encode_request(Cmd.PROGRAM_PAGE, 1, bytes(2116))
    assert big[3:5] == bytes((0x44, 0x08)) and len(big) == 5 + 2116 + 4
    with pytest.raises(ValueError):
        P.encode_request(Cmd.PING, 1, bytes(2117))


def test_response_roundtrip_and_layout():
    f = P.encode_response(0x88, 0x42, Status.ERR_ABORTED, 0x01020304, b"xy")
    assert f[:10] == bytes((0x5A, 0x88, 0x42, 0x05, 0x04, 0x03, 0x02, 0x01, 0x02, 0x00))
    r = P.decode_response(f)
    assert (r.cmd, r.seq, r.status, r.page, r.payload) == (0x88, 0x42, 5, 0x01020304, b"xy")
    assert r.is_end and not r.ok
    full = P.encode_response(Cmd.READ_PAGES, 1, Status.OK, 5, bytes(2112))
    assert len(full) == 2126  # PROPOSAL §3.4


def test_decode_rejects_corruption():
    f = bytearray(P.encode_response(Cmd.PING, 1, Status.OK, payload=b"abc"))
    with pytest.raises(FrameError, match="magic"):
        P.decode_response(bytes([0x00]) + bytes(f[1:]))
    f[11] ^= 1
    with pytest.raises(FrameError, match="CRC"):
        P.decode_response(bytes(f))
    with pytest.raises(FrameError, match="length"):
        P.parse_response_header(bytes((0x5A, 1, 1, 0, 0, 0, 0, 0)) + struct.pack("<H", 2113))


def test_timing_wire_format():
    t = Timing.default()
    assert len(t.pack()) == P.TIMING_WIRE_LEN == 30
    assert t.pack()[:2] == b"\x06\x00" and t.pack()[-4:] == struct.pack("<I", 1000)
    assert (t.t_adl, t.t_ww) == (18, 25) and t.pack()[22:26] == bytes((18, 0, 25, 0))
    assert Timing.unpack(t.pack()) == t
    with pytest.raises(FrameError):
        Timing.unpack(t.pack()[:-1])


def test_timing_presets_and_floors():
    assert Timing.default().meets_floors(CLK)
    assert Timing.slow().meets_floors(CLK)
    for field, bad, good in [("t_wp", 1, 2), ("t_rea", 4, 5), ("t_whr", 7, 8), ("t_rhw", 12, 13), ("t_ceh", 3, 4),
                            ("t_adl", 8, 9), ("t_ww", 12, 13)]:
        t = Timing.default()
        setattr(t, field, bad)
        assert not t.meets_floors(CLK), field
        setattr(t, field, good)
        assert t.meets_floors(CLK), field
    t = Timing.default()
    t.rb_timeout_us = 0
    assert not t.meets_floors(CLK)
    t.rb_timeout_us = P.TIMING_RB_TIMEOUT_MAX_US + 1
    assert not t.meets_floors(CLK)


def test_ping_info_unpack():
    payload = struct.pack("<BBBBI", 2, 0, 2, 0, CLK) + b"pico-nand-tool 0.2.0 (g123)"
    info = PingInfo.unpack(payload)
    assert info == PingInfo(2, (0, 2, 0), CLK, "pico-nand-tool 0.2.0 (g123)")


# ---- firmware/host constant sync ---------------------------------------------------------------------------

def _c_defines() -> dict[str, str]:
    text = PROTOCOL_DEFS_H.read_text()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)  # drop C comments
    return dict(re.findall(r"^#define\s+(PROTO_\w+)\s+(.+?)\s*$", text, flags=re.M))


def _c_value(raw: str):
    if raw.startswith("{"):
        return tuple(int(x, 0) for x in raw.strip("{} ").split(","))
    return int(raw, 0)


TIMING_MODE_NAMES = {"TIMING_QUERY", "TIMING_DEFAULT", "TIMING_SLOW", "TIMING_CUSTOM"}


def _py_value(c_name: str):
    name = c_name.removeprefix("PROTO_")
    if name.startswith("CMD_"):
        return Cmd[name[4:]]
    if name.startswith("ST_"):
        return Status[name[3:]]
    if name in TIMING_MODE_NAMES:
        return TimingMode[name.removeprefix("TIMING_")]
    return getattr(P, c_name if hasattr(P, c_name) else name)


def test_constants_match_firmware_header():
    defines = _c_defines()
    assert len(defines) > 45
    for c_name, raw in defines.items():
        assert _py_value(c_name) == _c_value(raw), c_name
    # and nothing on the Python side is missing from C
    assert {f"PROTO_CMD_{c.name}" for c in Cmd} <= defines.keys()
    assert {f"PROTO_ST_{s.name}" for s in Status} <= defines.keys()
    assert {f"PROTO_TIMING_{m.name}" for m in TimingMode} <= defines.keys()
