"""Chip facts for the S34ML02G100 (2 Gb SLC, x8) and decoders for its ID bytes and status register.

All references are to datasheet doc 002-00676 Rev *W (docs/datasheet.pdf).
"""

from __future__ import annotations

EXPECTED_ID = bytes.fromhex("01DA909544")  # Table 14, 2 Gb x8 3.3 V
ONFI_SIGNATURE = b"ONFI"  # §3.18: 90h + 20h, four bytes
ID_ADDR = 0x00  # §3.16
ONFI_ADDR = 0x20  # §3.18

SR_AFTER_RESET = 0x60  # §3.12: 60h with WP# low (E0h with WP# high)

# Status register bits (§3.11, Table 13)
SR_FAIL = 0x01  # last program/erase failed (N page)
SR_CACHE_FAIL = 0x02  # cache program: N-1 page failed
SR_IDLE = 0x20  # 1 = no internal data operation active
SR_READY = 0x40  # 1 = ready
SR_NOT_PROTECTED = 0x80  # 0 = write protected (WP# low)


def decode_status(sr: int) -> list[str]:
    """One line per meaningful SR bit (Table 13). Bits 2-4 are unused and reported only if set."""
    lines = [
        f"bit 7 write protect : {'NOT protected (WP# high!)' if sr & SR_NOT_PROTECTED else 'protected (WP# low)'}",
        f"bit 6 ready/busy    : {'ready' if sr & SR_READY else 'busy'}",
        f"bit 5 data op       : {'idle' if sr & SR_IDLE else 'internal operation active'}",
        f"bit 1 cache p/f     : {'fail' if sr & SR_CACHE_FAIL else 'pass'}",
        f"bit 0 pass/fail     : {'fail' if sr & SR_FAIL else 'pass'}",
    ]
    if sr & 0x1C:
        lines.append(f"bits 4-2 (unused)   : {(sr >> 2) & 7:03b} (expected 000)")
    return lines


def decode_id(idb: bytes) -> list[str]:
    """Decode Read ID bytes 1-5 (Tables 14, 16, 3.2, 3.3). Missing bytes are skipped."""
    lines = []
    if len(idb) >= 1:
        maker = "Spansion/Cypress" if idb[0] == 0x01 else "unknown"
        lines.append(f"maker    : {idb[0]:02X}h ({maker})")
    if len(idb) >= 2:
        dev = {0xF1: "1 Gb x8", 0xDA: "2 Gb x8", 0xDC: "4 Gb x8", 0xC1: "1 Gb x16", 0xCA: "2 Gb x16", 0xCC: "4 Gb x16"}
        lines.append(f"device   : {idb[1]:02X}h ({dev.get(idb[1], 'unknown')})")
    if len(idb) >= 3:
        b = idb[2]  # Table 16
        lines.append(
            f"byte 3   : {b:02X}h  {1 << (b & 3)} die, {2 << ((b >> 2) & 3)}-level cell, "
            f"{1 << ((b >> 4) & 3)} pages programmed at once, "
            f"interleave {'yes' if b & 0x40 else 'no'}, cache program {'yes' if b & 0x80 else 'no'}"
        )
    if len(idb) >= 4:
        b = idb[3]  # Table 3.2 (S34ML02G1)
        access = {(0, 0): "50/30 ns", (1, 0): "25 ns"}.get(((b >> 7) & 1, (b >> 3) & 1), "reserved")
        lines.append(
            f"byte 4   : {b:02X}h  {1 << (b & 3)} KB page, {64 << ((b >> 4) & 3)} KB block, "
            f"{8 << ((b >> 2) & 1)} B spare per 512 B, {access} serial access, {'x16' if b & 0x40 else 'x8'}"
        )
    if len(idb) >= 5:
        b = idb[4]  # Table 3.3 (S34ML02G1)
        plane_mb = 64 << ((b >> 4) & 7)
        size = f"{plane_mb // 1024} Gb" if plane_mb >= 1024 else f"{plane_mb} Mb"
        lines.append(f"byte 5   : {b:02X}h  {1 << ((b >> 2) & 3)} planes x {size}")
    return lines
