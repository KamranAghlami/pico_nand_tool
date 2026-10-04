import json

import pytest
from fake_device import FakeDevice, Fault, page_data

from nand_tool.client import Client
from nand_tool.dump import DumpError, dump, load_meta, meta_path, sha256_file
from nand_tool.geometry import PAGE_SIZE


def expected(start, count):
    return b"".join(page_data(p) for p in range(start, start + count))


def test_dump_range(client, dev, tmp_path):
    out = tmp_path / "a.bin"
    seen = []
    r = dump(client, out, start=100, count=300, chunk=128, progress=lambda p: seen.append(p.done))
    assert out.read_bytes() == expected(100, 300)
    assert seen == [128, 256, 300]
    assert (r.pages_read, r.resumed_at, r.rb_retries, r.stream_restarts) == (300, 0, 0, 0)
    meta = load_meta(out)
    assert meta["complete"] and meta["pages_done"] == 300
    assert meta["sha256"] == r.sha256 == sha256_file(out)
    assert (meta["start"], meta["count"], meta["chip_id"]) == (100, 300, "01 DA 90 95 44")
    (s,) = meta["sessions"]
    assert s["result"] == "complete" and s["timing_mode"] == "DEFAULT" and s["firmware"].startswith("pico-nand-tool")
    # READ_ID is checked before the dump and between chunks
    assert sum(1 for c, _, _ in dev.requests if c == 0x05) == 3


def test_refuses_to_overwrite(client, tmp_path):
    out = tmp_path / "a.bin"
    out.write_bytes(b"precious")
    with pytest.raises(DumpError, match="exists"):
        dump(client, out, count=10)
    assert out.read_bytes() == b"precious"
    out.unlink()
    meta_path(out).write_text("{}")
    with pytest.raises(DumpError, match="exists"):
        dump(client, out, count=10)  # a lone sidecar is protected too
    assert not out.exists()


def test_force_overwrites(client, tmp_path):
    out = tmp_path / "a.bin"
    out.write_bytes(b"old")
    dump(client, out, count=10, force=True)
    assert out.read_bytes() == expected(0, 10)


def test_wrong_id_refuses_before_touching_files(tmp_path):
    c = Client(FakeDevice(chip_id=b"\xff" * 5), timeout=0.05, quiet_s=0.005)
    out = tmp_path / "a.bin"
    with pytest.raises(DumpError, match="FF FF FF FF FF"):
        dump(c, out, count=10)
    assert not out.exists() and not meta_path(out).exists()


def test_rb_timeout_retried(client, dev, tmp_path):
    dev.rb_timeouts = {5: 2}
    r = dump(client, tmp_path / "a.bin", count=10, retries=5)
    assert r.rb_retries == 2
    assert (tmp_path / "a.bin").read_bytes() == expected(0, 10)


def test_persistent_rb_timeout_stops_then_resumes(client, dev, tmp_path):
    out = tmp_path / "a.bin"
    dev.rb_timeouts = {37: 99}
    with pytest.raises(DumpError, match="page 37: R/B# timeout on 3 reads"):
        dump(client, out, count=100, chunk=32, retries=2)
    assert out.read_bytes() == expected(0, 37)  # everything before the bad page, nothing after, nothing filled in
    meta = load_meta(out)
    assert meta["pages_done"] == 37 and not meta["complete"]
    assert meta["sessions"][0]["result"].startswith("error: page 37")

    dev.rb_timeouts = {}
    with open(out, "ab") as f:
        f.write(b"\x00" * 100)  # a torn last page from a crash: must be dropped, not kept
    r = dump(client, out, count=100, chunk=32, resume=True)
    assert r.resumed_at == 37 and r.pages_read == 63
    assert out.read_bytes() == expected(0, 100)
    meta = load_meta(out)
    assert meta["complete"] and [s["result"] for s in meta["sessions"]] == [meta["sessions"][0]["result"], "complete"]
    assert meta["sessions"][1]["first_page"] == 37


def test_resume_checks(client, tmp_path):
    out = tmp_path / "a.bin"
    with pytest.raises(DumpError, match="missing"):
        dump(client, out, count=10, resume=True)
    dump(client, out, count=10)
    with pytest.raises(DumpError, match="sidecar says"):
        dump(client, out, start=1, count=10, resume=True)
    with pytest.raises(DumpError, match="exclude"):
        dump(client, out, count=10, resume=True, force=True)
    with pytest.raises(DumpError, match="sidecar says"):
        dump(client, out, count=5, resume=True)  # range mismatch is caught first...
    meta = json.loads(meta_path(out).read_text())
    meta["count"] = 5
    meta_path(out).write_text(json.dumps(meta))
    with pytest.raises(DumpError, match="more than 5 pages"):
        dump(client, out, count=5, resume=True)  # ...and an oversized file is never truncated
    assert out.stat().st_size == 10 * PAGE_SIZE


def test_resume_of_complete_dump_is_a_no_op(client, dev, tmp_path):
    out = tmp_path / "a.bin"
    dump(client, out, count=10)
    reads = dict(dev.page_reads)
    r = dump(client, out, count=10, resume=True)
    assert r.pages_read == 0 and dev.page_reads == reads
    assert load_meta(out)["complete"]


@pytest.mark.parametrize("fault", [Fault.FLIP, Fault.DROP, Fault.TRUNCATE, Fault.GARBAGE_PREFIX])
def test_transport_faults_are_retried(client, dev, tmp_path, fault):
    dev.page_faults = {3: fault, 40: fault}
    r = dump(client, tmp_path / "a.bin", count=64, chunk=32)
    assert r.stream_restarts == 2
    assert (tmp_path / "a.bin").read_bytes() == expected(0, 64)


def test_id_change_between_chunks_stops(client, dev, tmp_path):
    out = tmp_path / "a.bin"

    def lose_contact(p):
        dev.chip_id = b"\x00" * 5

    with pytest.raises(DumpError, match="READ_ID changed to 00 00 00 00 00 before page 16"):
        dump(client, out, count=64, chunk=16, progress=lose_contact)
    assert out.read_bytes() == expected(0, 16)


def test_interrupt_saves_progress(client, dev, tmp_path):
    out = tmp_path / "a.bin"

    def ctrl_c(p):
        if p.done == 32:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        dump(client, out, count=64, chunk=16, progress=ctrl_c)
    meta = load_meta(out)
    assert meta["pages_done"] == 32 and meta["sessions"][0]["result"] == "interrupted"
    dump(client, out, count=64, chunk=16, resume=True)
    assert out.read_bytes() == expected(0, 64)


def test_range_checks(client, tmp_path):
    with pytest.raises(DumpError):
        dump(client, tmp_path / "a.bin", start=131071, count=2)
