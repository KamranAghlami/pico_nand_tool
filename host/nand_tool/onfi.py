"""ONFI parameter page: CRC-16 and decoding (datasheet §3.19, Table 3.4)."""

from __future__ import annotations

import struct
from dataclasses import dataclass

PARAM_PAGE_LEN = 256
PARAM_COPIES = 3
SIGNATURE = b"ONFI"
CRC_BASE = 0x4F4E  # ONFI CRC-16 init value
CRC_LEN_COVERED = 254  # bytes 0-253; the CRC itself is stored little-endian in bytes 254-255


def onfi_crc16(data: bytes, crc: int = CRC_BASE) -> int:
    """ONFI CRC-16: poly 0x8005, MSB first, no final XOR. Port of onfi_crc16() in Linux
    drivers/mtd/nand/raw/nand_onfi.c."""
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x8005 if crc & 0x8000 else 0)) & 0xFFFF
    return crc


def _ascii(b: bytes) -> str:
    return b.decode("ascii", errors="replace").rstrip(" ")


def _endurance(value: int, exponent: int) -> int:
    return value * 10**exponent  # Table 3.4 bytes 105-106 / 108-109: value, then power of ten


@dataclass(frozen=True)
class ParamPage:
    """The Table 3.4 fields this tool uses. Multi-byte fields are little-endian."""

    raw: bytes
    signature: bytes
    revision: int
    features: int
    optional_commands: int
    manufacturer: str
    model: str
    jedec_id: int
    page_data_bytes: int
    page_spare_bytes: int
    partial_data_bytes: int
    partial_spare_bytes: int
    pages_per_block: int
    blocks_per_lun: int
    luns: int
    address_cycles: int
    bits_per_cell: int
    bad_blocks_max: int
    block_endurance: int
    guaranteed_blocks: int
    guaranteed_endurance: int
    programs_per_page: int
    ecc_bits: int
    interleaved_address_bits: int
    timing_modes: int
    t_prog_us: int
    t_bers_us: int
    t_r_us: int
    t_ccs_ns: int
    crc_stored: int
    crc_computed: int

    @classmethod
    def parse(cls, raw: bytes) -> ParamPage:
        if len(raw) != PARAM_PAGE_LEN:
            raise ValueError(f"parameter page is {len(raw)} bytes, expected {PARAM_PAGE_LEN}")
        u16 = lambda off: struct.unpack_from("<H", raw, off)[0]  # noqa: E731
        u32 = lambda off: struct.unpack_from("<I", raw, off)[0]  # noqa: E731
        return cls(
            raw=bytes(raw),
            signature=bytes(raw[0:4]),
            revision=u16(4),
            features=u16(6),
            optional_commands=u16(8),
            manufacturer=_ascii(raw[32:44]),
            model=_ascii(raw[44:64]),
            jedec_id=raw[64],
            page_data_bytes=u32(80),
            page_spare_bytes=u16(84),
            partial_data_bytes=u32(86),
            partial_spare_bytes=u16(90),
            pages_per_block=u32(92),
            blocks_per_lun=u32(96),
            luns=raw[100],
            address_cycles=raw[101],
            bits_per_cell=raw[102],
            bad_blocks_max=u16(103),
            block_endurance=_endurance(raw[105], raw[106]),
            guaranteed_blocks=raw[107],
            guaranteed_endurance=_endurance(raw[108], raw[109]),
            programs_per_page=raw[110],
            ecc_bits=raw[112],
            interleaved_address_bits=raw[113] & 0x0F,
            timing_modes=u16(129),
            t_prog_us=u16(133),
            t_bers_us=u16(135),
            t_r_us=u16(137),
            t_ccs_ns=u16(139),
            crc_stored=u16(254),
            crc_computed=onfi_crc16(raw[:CRC_LEN_COVERED]),
        )

    @property
    def signature_ok(self) -> bool:
        return self.signature == SIGNATURE

    @property
    def crc_ok(self) -> bool:
        return self.crc_stored == self.crc_computed

    @property
    def valid(self) -> bool:
        return self.signature_ok and self.crc_ok

    @property
    def crc_bytes(self) -> bytes:
        return self.raw[254:256]


def split_copies(data: bytes) -> list[bytes]:
    if len(data) != PARAM_PAGE_LEN * PARAM_COPIES:
        raise ValueError(f"READ_PARAM data is {len(data)} bytes, expected {PARAM_PAGE_LEN * PARAM_COPIES}")
    return [data[i : i + PARAM_PAGE_LEN] for i in range(0, len(data), PARAM_PAGE_LEN)]
