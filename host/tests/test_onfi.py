import pytest
from golden_param import GOLDEN_PARAM, GOLDEN_PARAM_X3

from nand_tool.geometry import EXPECTED_PARAM, PARAM_CRC_BYTES
from nand_tool.onfi import ParamPage, onfi_crc16, split_copies


def test_golden_page_crc_is_the_datasheet_value():
    # Table 3.4: S34ML02G100 (x8) integrity CRC = 3Bh C5h (little-endian 0xC53B)
    assert onfi_crc16(GOLDEN_PARAM[:254]) == 0xC53B
    assert GOLDEN_PARAM[254:256] == PARAM_CRC_BYTES == b"\x3b\xc5"


def test_crc_algorithm():
    assert onfi_crc16(b"") == 0x4F4E  # init value, no final XOR
    # CRC-16/BUYPASS-style update (poly 0x8005, MSB first): continuing from 0 over "123456789" gives 0xFEE8
    assert onfi_crc16(b"123456789", crc=0) == 0xFEE8


def test_golden_page_decodes_to_the_spec_geometry():
    pp = ParamPage.parse(GOLDEN_PARAM)
    assert pp.valid
    assert (pp.manufacturer, pp.model, pp.jedec_id) == ("SPANSION", "S34ML02G1", 0x01)
    for field, value in EXPECTED_PARAM.items():
        assert getattr(pp, field) == value, field
    assert (pp.partial_data_bytes, pp.partial_spare_bytes) == (512, 16)
    assert (pp.block_endurance, pp.guaranteed_endurance) == (100_000, 1_000)
    assert (pp.t_prog_us, pp.t_bers_us, pp.t_ccs_ns, pp.timing_modes) == (700, 10_000, 100, 0x1F)
    assert pp.interleaved_address_bits == 1


@pytest.mark.parametrize("offset", [0, 80, 101, 253, 254, 255])
def test_any_single_bitflip_is_detected(offset):
    bad = bytearray(GOLDEN_PARAM)
    bad[offset] ^= 0x04
    assert not ParamPage.parse(bytes(bad)).valid


def test_stuck_data_line_is_detected():
    stuck = bytes(b | 0x10 for b in GOLDEN_PARAM)  # IO4 stuck high
    pp = ParamPage.parse(stuck)
    assert not pp.signature_ok and not pp.crc_ok


def test_split_copies():
    assert split_copies(GOLDEN_PARAM_X3) == [GOLDEN_PARAM] * 3
    with pytest.raises(ValueError):
        split_copies(GOLDEN_PARAM)
    with pytest.raises(ValueError):
        ParamPage.parse(GOLDEN_PARAM[:255])
