"""Write mode (docs/PROTOCOL.md "Write mode", docs/WRITE_PROPOSAL.md): device interlocks, the client's no-resend rule,
block writes with verification, plans (only-changed, 1:1, skip-bad, conflicts), resume and the CLI."""

import json
import struct

import pytest
from fake_device import FakeDevice, Fault

from nand_tool import cli as cli_mod
from nand_tool.cli import main
from nand_tool.client import Client, DeviceError, LostResponse, WriteOpError
from nand_tool.protocol import ARM_TOKEN, PAGE_LEN, Cmd, Status
from nand_tool.write import (
    ERASED_PAGE,
    MAP_SKIP_BAD,
    Image,
    WriteError,
    build_plan,
    check_backup,
    run_plan,
    sidecar_path,
    write_block,
)

PPB = 64


@pytest.fixture
def dev() -> FakeDevice:
    return FakeDevice()


@pytest.fixture
def client(dev: FakeDevice) -> Client:
    return Client(dev, timeout=0.05, retries=3, quiet_s=0.005)


def page_of(n: int) -> bytes:
    """A distinct, mostly-zero test page (only bits 1 -> 0 relative to erased), good-block marker kept."""
    d = bytearray(PAGE_LEN)
    d[0:4] = struct.pack("<I", n)
    d[2048] = 0xFF
    return bytes(d)


def make_image(path, dev: FakeDevice, blocks: range, edit=None) -> Image:
    """Dump of the fake chip's blocks, optionally edited: edit(pages_by_index) mutates a list of bytearrays."""
    pages = [bytearray(dev.chip_page(p)) for b in blocks for p in range(b * PPB, (b + 1) * PPB)]
    if edit:
        edit(pages)
    path.write_bytes(b"".join(pages))
    return Image.open(path, blocks.start * PPB)


def writes(dev: FakeDevice) -> list[tuple[str, int]]:
    return list(dev.ops)


# ---- device interlocks ------------------------------------------------------------------------------------------


def test_erase_needs_arming(client, dev):
    with pytest.raises(DeviceError) as e:
        client.erase_block(3)
    assert e.value.status == Status.ERR_NOT_ARMED
    with pytest.raises(DeviceError) as e:
        client.program_page(3 * PPB, page_of(1))
    assert e.value.status == Status.ERR_NOT_ARMED
    assert dev.ops == []


def test_arm_range_and_disarm(client, dev):
    client.arm_write(5, 6)
    with pytest.raises(DeviceError, match="ERR_NOT_ARMED"):
        client.erase_block(7)
    r = client.erase_block(6)
    assert (r.sr, r.busy_ns) == (0xE0, 3_500_000)
    assert all(dev.chip_page(p) == ERASED_PAGE for p in range(6 * PPB, 7 * PPB))
    client.disarm()
    with pytest.raises(DeviceError, match="ERR_NOT_ARMED"):
        client.erase_block(6)
    assert dev.ops == [("erase", 6)]


@pytest.mark.parametrize(
    "args",
    [
        (0x12345678, 1, 1, 10),  # wrong token
        (ARM_TOKEN, 2, 1, 10),  # first > last
        (ARM_TOKEN, 1, 2048, 10),  # past the chip
        (ARM_TOKEN, 1, 1, 0),  # idle 0
        (ARM_TOKEN, 1, 1, 61),  # idle > 60 s
    ],
)
def test_arm_rejects_bad_args_and_keeps_state(client, dev, args):
    with pytest.raises(DeviceError, match="ERR_BAD_ARGS"):
        client.call(Cmd.ARM_WRITE, struct.pack("<IHHH", *args))
    assert dev.armed is None


def test_idle_timeout_and_reset_disarm(client, dev):
    now = [100.0]
    dev.clock = lambda: now[0]
    client.arm_write(1, 1, idle_timeout_s=10)
    now[0] += 9
    client.erase_block(1)  # resets the idle timer
    now[0] += 10.5
    with pytest.raises(DeviceError, match="ERR_NOT_ARMED"):
        client.erase_block(1)
    client.arm_write(1, 1)
    client.reset()
    with pytest.raises(DeviceError, match="ERR_NOT_ARMED"):
        client.erase_block(1)


def test_program_ands_bits_and_counts_nop(client, dev):
    client.arm_write(2, 2)
    client.erase_block(2)
    p = 2 * PPB + 5
    client.program_page(p, page_of(7))
    assert dev.chip_page(p) == page_of(7)
    assert dev.nop[p] == 1
    # NAND can only clear bits: programming FFh over it changes nothing
    client.program_page(p, ERASED_PAGE)
    assert dev.chip_page(p) == page_of(7) and dev.nop[p] == 2


def test_op_failure_reports_sr_and_disarms(client, dev):
    dev.program_fail = {10 * PPB}
    client.arm_write(10, 10)
    client.erase_block(10)
    with pytest.raises(WriteOpError) as e:
        client.program_page(10 * PPB, page_of(1))
    assert (e.value.status, e.value.sr, e.value.page) == (Status.ERR_OP_FAILED, 0xE1, 10 * PPB)
    with pytest.raises(DeviceError, match="ERR_NOT_ARMED"):
        client.program_page(10 * PPB + 1, page_of(1))


def test_wp_stuck_changes_nothing(client, dev):
    dev.wp_stuck = True
    before = dev.chip_page(0)
    client.arm_write(0, 0)
    with pytest.raises(WriteOpError) as e:
        client.erase_block(0)
    assert e.value.status == Status.ERR_WP_STUCK and e.value.sr == 0x60
    assert dev.chip_page(0) == before and dev.armed is None


def test_bad_block_marker_refused_unless_overridden(client, dev):
    dev.bad_blocks = {7}
    client.arm_write(7, 7)
    with pytest.raises(DeviceError, match="ERR_BAD_BLOCK"):
        client.erase_block(7)
    assert dev.ops == [] and dev.armed is not None  # a refusal is not a failure: still armed
    client.erase_block(7, ignore_bad_marker=True)
    assert dev.ops == [("erase", 7)]


def test_erase_timeout_aborts_and_disarms(client, dev):
    dev.erase_timeout = {4}
    client.arm_write(4, 4)
    with pytest.raises(DeviceError, match="ERR_RB_TIMEOUT"):
        client.erase_block(4)
    assert dev.armed is None and dev.ops == [("erase-aborted", 4)]


def test_lost_write_response_is_never_resent(client, dev):
    client.arm_write(3, 3)
    client.erase_block(3)
    dev.cmd_faults[Cmd.PROGRAM_PAGE] = [Fault.DROP]
    with pytest.raises(LostResponse):
        client.program_page(3 * PPB, page_of(1))
    assert [op for op in dev.ops if op[0] == "program"] == [("program", 3 * PPB)]  # ran once, not re-sent


def test_corrupt_write_request_is_retried(client, dev):
    client.arm_write(3, 3)
    client.erase_block(3)
    dev.cmd_faults[Cmd.PROGRAM_PAGE] = [Fault.CORRUPT_REQUEST]  # ERR_CRC: nothing ran, safe to send again
    client.program_page(3 * PPB, page_of(1))
    assert [op for op in dev.ops if op[0] == "program"] == [("program", 3 * PPB)]


def test_write_ops_get_busy_during_a_stream(client, dev):
    stream = client.read_pages(0, 4)
    next(stream)
    resp = client.request(Cmd.DISARM)
    assert resp.status == Status.ERR_BUSY
    stream.close()


# ---- write_block ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("verify", [False, True])
def test_write_block_programs_non_blank_pages(client, dev, verify):
    pages = [page_of(i) if i % 3 else ERASED_PAGE for i in range(PPB)]
    dev.page_reads.clear()
    n = write_block(client, 9, pages, verify=verify)
    assert n == sum(1 for p in pages if p != ERASED_PAGE)
    assert [dev.chip_page(9 * PPB + i) for i in range(PPB)] == pages
    assert dev.ops[0] == ("erase", 9) and len(dev.ops) == 1 + n
    assert dev.armed is None and not dev.nop_violations
    # Only the erase's marker check (pages 0, 1, 63) reads the array, unless the block is read back.
    read_back = [p for p in dev.page_reads if p not in (9 * PPB, 9 * PPB + 1, 10 * PPB - 1)]
    assert len(read_back) == (PPB - 3 if verify else 0)


def test_write_block_redoes_the_block_after_a_lost_response(client, dev):
    pages = [page_of(i) for i in range(PPB)]
    dev.cmd_faults[Cmd.PROGRAM_PAGE] = [Fault.DROP]
    write_block(client, 9, pages)
    assert [op for op in dev.ops if op[0] == "erase"] == [("erase", 9), ("erase", 9)]
    assert [dev.chip_page(9 * PPB + i) for i in range(PPB)] == pages
    assert max(dev.nop.values()) == 1  # the re-erase reset NOP: no page was programmed twice without an erase


def test_write_block_gives_up_after_repeated_losses(client, dev):
    dev.cmd_faults[Cmd.ERASE_BLOCK] = [Fault.DROP] * 3
    with pytest.raises(WriteError, match="response lost on 3 attempts"):
        write_block(client, 9, [ERASED_PAGE] * PPB)


def test_write_block_verify_failure_stops(client, dev):
    dev.flaky_bits = {9 * PPB + 2: [(100, 0x04)]}
    dev.flip_on = lambda page, n: True
    with pytest.raises(WriteError, match=r"verify FAILED on 1 page\(s\) \(578\).*offset 100"):
        write_block(client, 9, [page_of(i) for i in range(PPB)], verify=True)


def test_write_block_without_verify_does_not_read_back(client, dev):
    dev.flaky_bits = {9 * PPB + 2: [(100, 0x04)]}
    dev.flip_on = lambda page, n: True
    write_block(client, 9, [page_of(i) for i in range(PPB)])  # the bad readback is never seen: no error
    assert dev.armed is None


def test_write_block_op_failure_propagates(client, dev):
    dev.erase_fail = {9}
    with pytest.raises(WriteOpError, match="ERR_OP_FAILED"):
        write_block(client, 9, [page_of(i) for i in range(PPB)])
    assert dev.armed is None


# ---- plans --------------------------------------------------------------------------------------------------------


def test_only_changed_writes_just_the_patched_block(client, dev, tmp_path):
    def patch(pages):
        pages[2 * PPB + 5][10] = 0x00 if pages[2 * PPB + 5][10] else 0x01

    img = make_image(tmp_path / "img.bin", dev, range(0, 4), patch)
    plan = build_plan(client, img)
    assert [(e.image_block, e.action) for e in plan.entries] == [(0, "same"), (1, "same"), (2, "write"), (3, "same")]
    assert run_plan(client, plan) == 1
    assert [op for op in dev.ops if op[0] == "erase"] == [("erase", 2)]
    assert [dev.chip_page(p) for p in range(4 * PPB)] == [img.block(b)[i] for b in range(4) for i in range(PPB)]
    meta = json.loads(sidecar_path(img.path).read_text())
    assert meta["complete"] and meta["blocks_written"] == [2] and meta["in_progress"] is None
    # a second run finds nothing to do
    assert build_plan(client, img).to_write == []


def test_all_mode_rewrites_everything_without_extra_programs(client, dev, tmp_path):
    img = make_image(tmp_path / "img.bin", dev, range(0, 3))
    plan = build_plan(client, img, only_changed=False)
    assert [e.action for e in plan.entries] == ["write"] * 3
    run_plan(client, plan)
    blanks = sum(1 for b in range(3) for p in img.block(b) if p == ERASED_PAGE)
    assert len([op for op in dev.ops if op[0] == "program"]) == 3 * PPB - blanks  # all-FFh pages are not programmed
    assert not dev.nop_violations


def test_conflict_with_bad_target_refuses_before_any_erase(client, dev, tmp_path):
    img = make_image(tmp_path / "img.bin", dev, range(0, 3))  # made before the target block went bad
    dev.bad_blocks = {1}
    plan = build_plan(client, img)
    assert [e.action for e in plan.entries] == ["same", "conflict", "same"]
    with pytest.raises(WriteError, match="nothing was written"):
        run_plan(client, plan)
    assert dev.ops == []


def test_blank_image_block_on_bad_target_is_skipped(client, dev, tmp_path):
    def blank(pages):
        for i in range(PPB, 2 * PPB):
            pages[i][:] = ERASED_PAGE

    img = make_image(tmp_path / "img.bin", dev, range(0, 3), blank)
    dev.bad_blocks = {1}
    plan = build_plan(client, img, only_changed=False)
    assert [e.action for e in plan.entries] == ["write", "target-bad", "write"]


def test_image_bad_block_is_not_written(client, dev, tmp_path):
    def mark(pages):
        pages[PPB + 1][2048] = 0x00  # block 1, page 1 marker

    img = make_image(tmp_path / "img.bin", dev, range(0, 3), mark)
    plan = build_plan(client, img, only_changed=False)
    assert [e.action for e in plan.entries] == ["write", "image-bad", "write"]
    assert plan.image_bad == {1: {1: 0x00}}


def test_skip_bad_shifts_past_bad_target_blocks(client, dev, tmp_path):
    img = make_image(tmp_path / "img.bin", dev, range(0, 3))
    dev.bad_blocks = {1}
    plan = build_plan(client, img, mapping=MAP_SKIP_BAD, only_changed=False)
    assert [(e.image_block, e.target_block, e.action) for e in plan.entries] == [
        (0, 0, "write"), (1, 2, "write"), (2, 3, "write")]
    run_plan(client, plan)
    assert [dev.chip_page(2 * PPB + i) for i in range(PPB)] == img.block(1)
    assert ("erase", 1) not in dev.ops


def test_interrupted_block_is_rewritten_on_the_next_run(client, dev, tmp_path):
    def patch(pages):
        for b in (1, 2):
            pages[b * PPB][0] ^= 0xFF

    img = make_image(tmp_path / "img.bin", dev, range(0, 3), patch)
    dev.program_fail = {2 * PPB}
    with pytest.raises(WriteOpError):
        run_plan(client, build_plan(client, img))
    meta = json.loads(sidecar_path(img.path).read_text())
    assert meta["in_progress"] == 2 and not meta["complete"] and meta["blocks_written"] == [1]
    assert meta["result"].startswith("error")

    # even if block 2 happened to read back identical, the next run re-does it
    dev.program_fail = set()
    for i in range(PPB):
        dev.pages[2 * PPB + i] = img.block(2)[i]
    plan = build_plan(client, img)
    assert plan.resumed_block == 2
    assert [(e.image_block, e.action) for e in plan.entries] == [(0, "same"), (1, "same"), (2, "write")]
    run_plan(client, plan)
    assert json.loads(sidecar_path(img.path).read_text())["complete"]


def test_saved_target_markers_survive_into_the_next_run(client, dev, tmp_path):
    img = make_image(tmp_path / "img.bin", dev, range(0, 2))
    sidecar_path(img.path).write_text(json.dumps({
        "format": "pico-nand-tool write v1", "complete": False, "in_progress": None,
        "target_bad": {"1": {"0": 0}},
    }))
    plan = build_plan(client, img)
    assert plan.target_bad == {1: {0: 0}} and plan.entries[1].action == "conflict"
    assert build_plan(client, img, fresh=True).entries[1].action == "same"


def test_plan_refuses_wrong_chip(client, dev, tmp_path):
    img = make_image(tmp_path / "img.bin", dev, range(0, 1))
    dev.chip_id = b"\xff" * 5
    with pytest.raises(WriteError, match="refusing to write"):
        build_plan(client, img)


def test_image_must_be_whole_blocks(tmp_path):
    p = tmp_path / "x.bin"
    p.write_bytes(bytes(PAGE_LEN * 3))
    with pytest.raises(WriteError, match="whole number of blocks"):
        Image.open(p)


def test_backup_check(client, dev, tmp_path):
    good = make_image(tmp_path / "b.bin", dev, range(0, 4))
    assert check_backup(client, good.path, samples=16, seed=1).ok

    def change(pages):
        for p in pages:
            p[1] ^= 0x01

    other = make_image(tmp_path / "o.bin", dev, range(0, 4), change)
    bc = check_backup(client, other.path, samples=16, seed=1)
    assert not bc.ok and len(bc.mismatched) == len(bc.sampled) == 16


# ---- CLI --------------------------------------------------------------------------------------------------------


def run(dev: FakeDevice, *argv: str) -> int:
    return main(["--timeout", "0.05", *argv], transport_factory=lambda port: dev)


def test_cli_erase_with_yes(dev, capsys):
    assert run(dev, "erase", "--block", "5", "--count", "2", "--yes") == 0
    out = capsys.readouterr().out
    assert "verify   : off" in out and "result   : PASS" in out
    assert "block 5   : erased (SR E0h, busy 3500 us)\n" in out
    assert dev.ops == [("erase", 5), ("erase", 6)] and dev.armed is None


def test_cli_erase_verify(dev, capsys):
    assert run(dev, "erase", "--block", "5", "--yes", "--verify") == 0
    out = capsys.readouterr().out
    assert "verify   : off" not in out
    assert "block 5   : erased (SR E0h, busy 3500 us), verified all FFh" in out and "result   : PASS" in out


def test_cli_erase_needs_confirmation(dev, capsys, monkeypatch):
    assert run(dev, "erase", "--block", "5") == 1  # stdin is not a terminal under pytest
    assert "pass --yes" in capsys.readouterr().err and dev.ops == []
    monkeypatch.setattr(cli_mod, "_ask", lambda prompt: "ERASE 2 BLOCK")
    assert run(dev, "erase", "--block", "5", "--count", "2") == 1
    assert "did not match" in capsys.readouterr().out and dev.ops == []
    monkeypatch.setattr(cli_mod, "_ask", lambda prompt: "ERASE 2 BLOCKS")
    assert run(dev, "erase", "--block", "5", "--count", "2") == 0
    assert dev.ops == [("erase", 5), ("erase", 6)]


def test_cli_erase_refuses_bad_blocks(dev, capsys):
    dev.bad_blocks = {6}
    assert run(dev, "erase", "--block", "5", "--count", "2", "--yes") == 1
    captured = capsys.readouterr()
    assert "bad      : block 6: page 0: 00h" in captured.out and "nothing was erased" in captured.err
    assert dev.ops == []


def test_cli_program(dev, tmp_path, capsys):
    f = tmp_path / "p.bin"
    f.write_bytes(page_of(42))
    assert run(dev, "program", "--page", "130", "--in", str(f)) == 1  # page 130 holds data
    assert "is not erased" in capsys.readouterr().err
    dev.erase(2)
    assert run(dev, "program", "--page", "130", "--in", str(f)) == 0
    out = capsys.readouterr().out
    assert "status   : E0h" in out and "verify   : off" in out and "result   : PASS" in out
    assert dev.chip_page(130) == page_of(42) and dev.armed is None
    f.write_bytes(page_of(43))
    assert run(dev, "program", "--page", "131", "--in", str(f), "--verify") == 0
    out = capsys.readouterr().out
    assert "readback identical" in out and "result   : PASS" in out


def test_cli_write_dry_run_then_write(dev, tmp_path, capsys):
    img = make_image(tmp_path / "img.bin", dev, range(0, 3), lambda pages: pages[PPB].__setitem__(0, 0))
    assert run(dev, "write", str(img.path), "--start", "0", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "plan     : 1 block(s) to erase + program" in out and "dry run  : nothing was erased" in out
    assert dev.ops == []
    assert run(dev, "write", str(img.path), "--start", "0", "--yes") == 0
    out = capsys.readouterr().out
    assert "WARNING  : no --backup given" in out and "result   : PASS" in out
    assert "verify   : off" in out and "not read back (no --verify)" in out
    assert json.loads((tmp_path / "img.bin.write.json").read_text())["verify"] is False
    assert [op for op in dev.ops if op[0] == "erase"] == [("erase", 1)]
    assert run(dev, "write", str(img.path), "--start", "0", "--yes") == 0
    assert "nothing to write" in capsys.readouterr().out


def test_cli_write_verify(dev, tmp_path, capsys):
    img = make_image(tmp_path / "img.bin", dev, range(0, 2), lambda pages: pages[0].__setitem__(0, 0))
    assert run(dev, "write", str(img.path), "--start", "0", "--yes", "--verify") == 0
    out = capsys.readouterr().out
    assert "verify   : off" not in out and "written (" in out and ", verified  [1/1" in out
    assert "every one verified by readback" in out and "result   : PASS" in out
    assert json.loads((tmp_path / "img.bin.write.json").read_text())["verify"] is True


def test_cli_write_verify_mismatch_fails(dev, tmp_path, capsys):
    img = make_image(tmp_path / "img.bin", dev, range(0, 2), lambda pages: pages[0].__setitem__(0, 0))
    dev.flaky_bits = {5: [(100, 0x04)]}
    dev.flip_on = lambda page, n: True
    assert run(dev, "write", str(img.path), "--start", "0", "--yes", "--verify") == 1
    assert "verify FAILED" in capsys.readouterr().err


def test_cli_write_conflict_exits_1(dev, tmp_path, capsys):
    img = make_image(tmp_path / "img.bin", dev, range(0, 2))
    dev.bad_blocks = {1}
    assert run(dev, "write", str(img.path), "--start", "0", "--all", "--yes") == 1
    assert "CONFLICT : image block 1" in capsys.readouterr().out and dev.ops == []


def test_cli_write_with_backup(dev, tmp_path, capsys):
    backup = make_image(tmp_path / "backup.bin", dev, range(0, 2))
    img = make_image(tmp_path / "img.bin", dev, range(0, 2), lambda pages: pages[0].__setitem__(0, 0))
    assert run(dev, "write", str(img.path), "--start", "0", "--backup", str(backup.path), "--yes") == 0
    assert "random non-blank pages match the chip" in capsys.readouterr().out


def test_cli_interlock_test(dev, capsys, monkeypatch):
    now = [0.0]
    dev.clock = lambda: now[0]
    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    assert run(dev, "interlock-test", "--block", "3") == 1  # block 3 holds data
    assert "is not blank" in capsys.readouterr().err
    dev.erase(3)
    assert run(dev, "interlock-test", "--block", "3") == 0
    out = capsys.readouterr().out
    assert out.count("ERR_NOT_ARMED  OK") == 8 and "ERR_BAD_ARGS  OK" in out
    assert "still blank  OK" in out and "result   : PASS" in out
    assert [op for op in dev.ops if op[0] != "erase"] == [] and dev.ops == []


def test_cli_interlock_test_catches_a_broken_interlock(dev, capsys, monkeypatch):
    now = [0.0]
    dev.clock = lambda: now[0]
    monkeypatch.setattr(cli_mod.time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
    dev.erase(3)
    dev._armed_for = lambda block: True  # a firmware that ignores the arm state
    assert run(dev, "interlock-test", "--block", "3") == 1
    out = capsys.readouterr().out
    assert "FAIL: THE COMMAND RAN" in out and "CHANGED  FAIL" in out and "result   : FAIL" in out


def test_cli_badblocks_device_and_blank(dev, tmp_path, capsys):
    dev.bad_blocks = {3}
    assert run(dev, "badblocks", "--device", "--first-block", "0", "--count", "6") == 0
    out = capsys.readouterr().out
    assert "bad      : block 3: page 0: 00h" in out and "result   : 1 bad block: 3" in out
    dev.erase(1)
    dev.erase(2)
    img = make_image(tmp_path / "img.bin", dev, range(0, 4))
    assert run(dev, "badblocks", str(img.path), "--start", "0", "--blank") == 0
    assert "blank    : 2 blocks all FFh (data and spare): 1..2" in capsys.readouterr().out
