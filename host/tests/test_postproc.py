import pytest
from fake_device import page_data

from nand_tool.cli import main
from nand_tool.dump import meta_path
from nand_tool.geometry import PAGE_DATA, PAGE_SIZE
from nand_tool.postproc import PostprocError, scan_bad_blocks, split


def make_image(path, start, count, marks=()):
    """Fake-image pages; marks = [(page, value)] sets spare byte 0 of that chip page."""
    pages = {p: bytearray(page_data(p)) for p in range(start, start + count)}
    for p, v in marks:
        pages[p][PAGE_DATA] = v
    path.write_bytes(b"".join(pages[p] for p in range(start, start + count)))
    return path


def test_split(tmp_path):
    img = make_image(tmp_path / "img.bin", 0, 70)
    n = split(img, tmp_path / "data.bin", tmp_path / "oob.bin")
    assert n == 70
    data, oob = (tmp_path / "data.bin").read_bytes(), (tmp_path / "oob.bin").read_bytes()
    assert data == b"".join(page_data(p)[:2048] for p in range(70))
    assert oob == b"".join(page_data(p)[2048:] for p in range(70))
    assert not list(tmp_path.glob("*.partial"))


def test_split_refusals(tmp_path):
    img = make_image(tmp_path / "img.bin", 0, 2)
    (tmp_path / "oob.bin").write_bytes(b"keep")
    with pytest.raises(PostprocError, match="exists"):
        split(img, tmp_path / "data.bin", tmp_path / "oob.bin")
    assert (tmp_path / "oob.bin").read_bytes() == b"keep" and not (tmp_path / "data.bin").exists()
    with pytest.raises(PostprocError, match="three different"):
        split(img, img, tmp_path / "x.bin", force=True)
    (tmp_path / "torn.bin").write_bytes(bytes(PAGE_SIZE + 1))
    with pytest.raises(PostprocError, match="whole number"):
        split(tmp_path / "torn.bin", tmp_path / "d", tmp_path / "o")


def test_badblocks_rule(tmp_path):
    # block 2: 1st page marked; block 5: 2nd page; block 7: last page; block 9: a middle page (not a marker page)
    marks = [(2 * 64, 0x00), (5 * 64 + 1, 0xF0), (7 * 64 + 63, 0x00), (9 * 64 + 10, 0x00)]
    img = make_image(tmp_path / "img.bin", 0, 640, marks)
    scan = scan_bad_blocks(img)
    assert (scan.first_block, scan.blocks, scan.skipped_pages) == (0, 10, 0)
    assert [(b.block, b.markers) for b in scan.bad] == [(2, {0: 0x00}), (5, {1: 0xF0}), (7, {63: 0x00})]


def test_badblocks_unaligned_start(tmp_path):
    img = make_image(tmp_path / "img.bin", 100, 200, [(192, 0x00)])  # pages 100..299: whole blocks 2, 3
    scan = scan_bad_blocks(img, start_page=100)
    assert (scan.first_block, scan.blocks, scan.skipped_pages) == (2, 2, 72)
    assert [b.block for b in scan.bad] == [3]


def test_badblocks_cli(tmp_path, capsys):
    img = make_image(tmp_path / "img.bin", 0, 192, [(64, 0x00)])
    assert main(["badblocks", str(img)]) == 0
    out = capsys.readouterr().out
    assert "blocks 0..2 (3 blocks)" in out
    assert "bad      : block 1 (pages 64..127): page 0: 00h" in out
    assert "result   : 1 bad block: 1" in out and "WARNING" not in out


def test_badblocks_cli_warnings(tmp_path, capsys):
    marks = [(b * 64, 0x00) for b in range(41)]  # 41 > 40 allowed, including block 0
    img = make_image(tmp_path / "img.bin", 0, 41 * 64, marks)
    assert main(["badblocks", str(img)]) == 0
    out = capsys.readouterr().out
    assert "41 bad blocks" in out and "block 0 is guaranteed good" in out and "more than the 40" in out


def test_badblocks_cli_uses_sidecar_start(tmp_path, capsys):
    img = make_image(tmp_path / "img.bin", 128, 64, [(128, 0x00)])
    meta_path(img).write_text('{"format": "pico-nand-tool raw dump v1", "start": 128}')
    assert main(["badblocks", str(img)]) == 0
    assert "bad      : block 2 (pages 128..191)" in capsys.readouterr().out


def test_split_cli(tmp_path, capsys):
    img = make_image(tmp_path / "img.bin", 0, 3)
    assert main(["split", str(img)]) == 0
    assert (tmp_path / "data.bin").stat().st_size == 3 * 2048 and (tmp_path / "oob.bin").stat().st_size == 3 * 64
    assert main(["split", str(img)]) == 1
    assert "exists" in capsys.readouterr().err
