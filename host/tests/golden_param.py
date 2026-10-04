"""Reference parameter page for the S34ML02G100 (x8), rebuilt byte by byte from datasheet Table 3.4.

Built independently of nand_tool.onfi (literal offsets, no decoder), so the tests check one against the other. The
datasheet's CRC for this page is 3Bh C5h (bytes 254-255).
"""

from __future__ import annotations


def golden_param_page() -> bytes:
    p = bytearray(256)

    def put(off: int, *values: int) -> None:
        p[off : off + len(values)] = bytes(values)

    put(0, 0x4F, 0x4E, 0x46, 0x49)  # signature "ONFI"
    put(4, 0x02, 0x00)  # revision: ONFI 1.0
    put(6, 0x1C, 0x00)  # features (S34ML02G100 x8)
    put(8, 0x1B, 0x00)  # optional commands (S34ML02G1)
    p[32:44] = b"SPANSION    "  # manufacturer, 12 ASCII
    p[44:64] = b"S34ML02G1".ljust(20)  # model, 20 ASCII
    put(64, 0x01)  # JEDEC manufacturer ID
    put(80, 0x00, 0x08, 0x00, 0x00)  # data bytes per page: 2048
    put(84, 0x40, 0x00)  # spare bytes per page: 64
    put(86, 0x00, 0x02, 0x00, 0x00)  # data bytes per partial page: 512
    put(90, 0x10, 0x00)  # spare bytes per partial page: 16
    put(92, 0x40, 0x00, 0x00, 0x00)  # pages per block: 64
    put(96, 0x00, 0x08, 0x00, 0x00)  # blocks per LUN: 2048 (S34ML02G1)
    put(100, 0x01)  # LUNs
    put(101, 0x23)  # address cycles: 2 column, 3 row (S34ML02G1)
    put(102, 0x01)  # bits per cell
    put(103, 0x28, 0x00)  # bad blocks max per LUN: 40 (S34ML02G1)
    put(105, 0x01, 0x05)  # block endurance: 1 x 10^5
    put(107, 0x01)  # guaranteed valid blocks at the beginning of the target
    put(108, 0x01, 0x03)  # endurance for guaranteed valid blocks: 1 x 10^3
    put(110, 0x04)  # programs per page
    put(111, 0x00)  # partial programming attributes
    put(112, 0x01)  # bits of ECC correctability
    put(113, 0x01)  # interleaved address bits (S34ML02G1)
    put(114, 0x04)  # interleaved operation attributes (S34ML02G1)
    put(128, 0x0A)  # I/O pin capacitance
    put(129, 0x1F, 0x00)  # timing modes 0-4
    put(131, 0x1F, 0x00)  # program cache timing modes 0-4
    put(133, 0xBC, 0x02)  # tPROG max 700 us
    put(135, 0x10, 0x27)  # tBERS max 10000 us (S34ML02G1)
    put(137, 0x19, 0x00)  # tR max 25 us
    put(139, 0x64, 0x00)  # tCCS min 100 ns
    put(164, 0x00, 0x00)  # vendor revision
    put(254, 0x3B, 0xC5)  # integrity CRC (S34ML02G100 x8)
    return bytes(p)


GOLDEN_PARAM = golden_param_page()
GOLDEN_PARAM_X3 = GOLDEN_PARAM * 3
