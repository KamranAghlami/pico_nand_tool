import json

import pytest
from fake_device import FakeDevice, page_data

from nand_tool.cli import main
from nand_tool.client import Client
from nand_tool.dump import dump
from nand_tool.geometry import PAGE_SIZE
from nand_tool.reconcile import ReconcileError, default_min_agree, reconcile, vote_page


def run(dev, *argv):
    return main(["--timeout", "0.05", *argv], transport_factory=lambda port: dev)


def two_passes(tmp_path, dev, start=50, count=20):
    c = Client(dev, timeout=0.05, quiet_s=0.005)
    a, b = tmp_path / "a.bin", tmp_path / "b.bin"
    dump(c, a, start=start, count=count)
    dump(c, b, start=start, count=count)
    return c, a, b


# ---- compare --------------------------------------------------------------------------------------------------


def test_compare_identical(tmp_path, capsys):
    _, a, b = two_passes(tmp_path, FakeDevice())
    assert main(["compare", str(a), str(b)]) == 0
    assert "identical (20 pages)" in capsys.readouterr().out


def test_compare_lists_bytes_and_bits(tmp_path, capsys):
    dev = FakeDevice()
    dev.flaky_bits = {55: [(10, 0x01), (2049, 0x80)]}  # flips on the 2nd read = pass B
    _, a, b = two_passes(tmp_path, dev)
    assert main(["compare", str(a), str(b)]) == 1
    out = capsys.readouterr().out
    assert "page 55 (block 0, page 55): 2 bytes, 2 bits differ" in out  # chip page from A's sidecar
    assert "offset   10 (data)" in out and "bits 00000001" in out
    assert "offset 2049 (spare+1)" in out and "bits 10000000" in out
    assert "result   : 1 of 20 pages differ (2 bytes, 2 bits)" in out


def test_compare_size_problems(tmp_path, capsys):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_bytes(bytes(PAGE_SIZE * 2))
    b.write_bytes(bytes(PAGE_SIZE))
    assert main(["compare", str(a), str(b)]) == 1
    assert "DIFFERENT" in capsys.readouterr().out
    b.write_bytes(bytes(100))
    assert main(["compare", str(a), str(b)]) == 1
    assert "not a whole number" in capsys.readouterr().err


# ---- reconcile ------------------------------------------------------------------------------------------------


def test_vote_page_unit():
    a = bytes([0b0000_0001, 0x00])
    b = bytes([0b0000_0000, 0x00])
    reads = [bytes([1, 0])] * 4 + [bytes([0, 0])]
    final, status, bits = vote_page(a, b, reads, min_agree=4)
    assert final == bytes([1, 0]) and status == "corrected"
    assert bits == [{"offset": 0, "area": "data", "bit": 0, "votes_0": 1, "votes_1": 4, "a": 1, "b": 0, "final": 1}]
    final, status, _ = vote_page(a, b, reads[2:], min_agree=3)  # 2 vs 1 of 3: below min_agree
    assert status == "unstable" and final == bytes([1, 0])


def test_default_min_agree():
    assert [default_min_agree(k) for k in (1, 3, 5, 7, 9)] == [1, 2, 4, 5, 6]


@pytest.mark.parametrize(
    "flip_reads,status",
    [
        ({2}, "consistent"),  # only pass B flipped; all 5 re-reads (reads 3-7) agree
        ({2, 4}, "corrected"),  # one re-read flipped: 4 of 5
        ({2, 4, 6}, "unstable"),  # two re-reads flipped: 3 of 5 < 4
    ],
)
def test_reconcile_outcomes(tmp_path, flip_reads, status):
    dev = FakeDevice()
    dev.flaky_bits = {60: [(100, 0x10)]}
    dev.flip_on = lambda page, n: n in flip_reads
    c, a, b = two_passes(tmp_path, dev)
    out, report = tmp_path / "final.bin", tmp_path / "final.json"
    r = reconcile(c, a, b, out, report, start=50)
    assert [(o.page, o.status) for o in r.outcomes] == [(60, status)]
    assert dev.page_reads[60] == 7  # 2 passes + 5 re-reads; no other page re-read
    assert dev.page_reads[59] == 2
    assert out.read_bytes() == b"".join(page_data(p) for p in range(50, 70))  # majority = the true data
    doc = json.loads(report.read_text())
    (pr,) = doc["page_results"]
    (bit,) = pr["bits"]
    flipped_rereads = len(flip_reads - {2})
    assert bit["offset"] == 100 and bit["bit"] == 4 and bit["a"] != bit["b"]
    assert bit["votes_1"] + bit["votes_0"] == 5 and min(bit["votes_0"], bit["votes_1"]) == flipped_rereads
    assert doc["pages_unstable"] == ([60] if status == "unstable" else [])


def test_reconcile_identical_inputs_needs_no_device(tmp_path):
    dev = FakeDevice()
    c, a, b = two_passes(tmp_path, dev)
    n = len(dev.requests)
    r = reconcile(c, a, b, tmp_path / "f.bin", tmp_path / "f.json")
    assert r.outcomes == [] and len(dev.requests) == n
    assert (tmp_path / "f.bin").read_bytes() == a.read_bytes()


def test_reconcile_refusals(tmp_path):
    c, a, b = two_passes(tmp_path, FakeDevice())
    out = tmp_path / "f.bin"
    out.write_bytes(b"keep")
    with pytest.raises(ReconcileError, match="exists"):
        reconcile(c, a, b, out, tmp_path / "f.json")
    assert out.read_bytes() == b"keep"
    with pytest.raises(ReconcileError, match="new files"):
        reconcile(c, a, b, a, tmp_path / "f.json", force=True)
    with pytest.raises(ReconcileError, match="new files"):
        reconcile(c, a, b, tmp_path / "g.bin", b, force=True)
    with pytest.raises(ReconcileError, match="majority"):
        reconcile(c, a, b, tmp_path / "g.bin", tmp_path / "g.json", reads=5, min_agree=2)
    b.write_bytes(b.read_bytes()[:PAGE_SIZE])
    with pytest.raises(ReconcileError, match="different sizes"):
        reconcile(c, a, b, tmp_path / "g.bin", tmp_path / "g.json")


def test_reconcile_cli(tmp_path, capsys):
    dev = FakeDevice()
    dev.flaky_bits = {52: [(7, 0x02)], 61: [(9, 0x04)]}
    dev.flip_on = lambda page, n: n == 2 or (page == 61 and n in (4, 6))
    _, a, b = two_passes(tmp_path, dev)
    out = tmp_path / "final.bin"
    assert run(dev, "reconcile", str(a), str(b), "--out", str(out)) == 1  # page 61 is unstable
    text = capsys.readouterr().out
    assert "20, 2 differ between the two passes" in text
    assert "1 consistent re-reads, 0 corrected by majority, 1 UNSTABLE" in text
    assert "UNSTABLE : page 61 (block 0)" in text
    assert (tmp_path / "final.bin.report.json").exists()


def test_reconcile_cli_start_pages(tmp_path, capsys):
    from nand_tool.dump import meta_path

    dev = FakeDevice()
    _, a, b = two_passes(tmp_path, dev)
    meta_path(b).unlink()  # B without a sidecar: A's start page applies
    assert run(dev, "reconcile", str(a), str(b), "--out", str(tmp_path / "f1.bin")) == 0
    _, c, d = two_passes(tmp_path / "x", dev, start=10)
    assert run(dev, "reconcile", str(a), str(d), "--out", str(tmp_path / "f2.bin")) == 1
    assert "different start pages" in capsys.readouterr().err
