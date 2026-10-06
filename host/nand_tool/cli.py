"""`nandtool` command line: ping, timing, id, status, param, read, dump, compare, reconcile, split, badblocks, and
write mode: erase, program, write, interlock-test (docs/SPEC.md). bus-test (M1) is not implemented: M1 was skipped.

Structure: build_parser() declares every subcommand and binds it to a handler with set_defaults(func=cmd_xxx).
main() parses argv, opens the device only if the command needs one (`needs_device`; compare and split work on files
only), and calls handler(client, args). Each cmd_xxx() returns the exit status: 0 = pass, 1 = fail. The real work
lives in the library modules (dump.py, write.py, ...); handlers mostly print. main() turns any NandToolError into a
one-line `error: ...`, and takes a transport_factory argument so the tests can run the real CLI against
tests/fake_device.py.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from pathlib import Path
from collections import Counter
from collections.abc import Callable

from . import __version__
from .client import Client, DeviceError
from .compare import CompareError, diff_pages, page_count
from .dump import DEFAULT_RETRIES, Progress, dump, load_meta
from .errors import NandToolError
from .geometry import (
    EXPECTED_ID,
    EXPECTED_PARAM,
    ONFI_ADDR,
    ONFI_SIGNATURE,
    PAGE_DATA,
    PAGE_SIZE,
    PAGES_PER_BLOCK,
    PARAM_CRC_BYTES,
    SR_AFTER_RESET,
    SR_NOT_PROTECTED,
    TOTAL_PAGES,
    decode_id,
    decode_status,
)
from .onfi import PARAM_PAGE_LEN, ParamPage, split_copies
from .postproc import MARKER_OFFSET, scan_bad_blocks, split
from .reconcile import DEFAULT_READS, default_min_agree, reconcile
from .protocol import PROTO_VERSION, Cmd, Status, TimingMode
from .transport import Transport, open_transport
from .write import (
    ARM_IDLE_S,
    BLOCKS,
    ERASED_PAGE,
    MAP_1TO1,
    MAP_SKIP_BAD,
    BlockDone,
    Image,
    build_plan,
    check_backup,
    disarm_quietly,
    erase_block,
    read_block,
    read_markers,
    read_range,
    run_plan,
    sidecar_path,
)


def cmd_ping(client: Client, args: argparse.Namespace) -> int:
    info = client.ping()
    major, minor, patch = info.fw_version
    print(f"firmware : {info.version_string}")
    print(f"version  : {major}.{minor}.{patch}, protocol v{info.proto_version}")
    print(f"clk_sys  : {info.clk_hz / 1e6:.3f} MHz")
    return 0


def cmd_timing(client: Client, args: argparse.Namespace) -> int:
    clk = client.ping().clk_hz
    if args.slow:
        mode, t = client.set_timing(TimingMode.SLOW)
    elif args.default:
        mode, t = client.set_timing(TimingMode.DEFAULT)
    else:
        mode, t = client.get_timing()
    print(f"active timing: {mode.name} (clk_sys {clk / 1e6:.3f} MHz)")
    print(t.describe(clk))
    return 0


def _hex(b: bytes) -> str:
    return b.hex(" ").upper()


def cmd_id(client: Client, args: argparse.Namespace) -> int:
    reads = Counter(client.read_id() for _ in range(args.repeat))
    first = next(iter(reads))
    print(f"ID       : {_hex(first)}")
    for line in decode_id(first):
        print(line)
    ok = True
    if args.repeat > 1:
        if len(reads) == 1:
            print(f"repeat   : {args.repeat}/{args.repeat} reads identical")
        else:
            ok = False
            print(f"repeat   : NOT stable, {len(reads)} different values in {args.repeat} reads:")
            for value, count in reads.most_common():
                print(f"           {_hex(value)}  x{count}")
    if set(reads) == {EXPECTED_ID}:
        print(f"expected : {_hex(EXPECTED_ID)} (S34ML02G1 x8): match")
    else:
        ok = False
        print(f"expected : {_hex(EXPECTED_ID)} (S34ML02G1 x8): MISMATCH")
    if args.onfi:
        sig = client.read_id(ONFI_ADDR, len(ONFI_SIGNATURE))
        match = sig == ONFI_SIGNATURE
        ok = ok and match
        print(f"ONFI sig : {_hex(sig)} {sig.decode('ascii', errors='replace')!r}: {'match' if match else 'MISMATCH'}")
    return 0 if ok else 1


def cmd_status(client: Client, args: argparse.Namespace) -> int:
    if args.reset:
        busy_ns = client.reset()
        if busy_ns:
            print(f"reset    : OK, R/B# busy {busy_ns / 1000:.2f} us")
        else:
            # Normal on this chip: a reset of an idle chip ends before the first R/B# sample (tWB + poll latency).
            print("reset    : OK (finished before the first R/B# sample)")
    sr = client.read_status()
    note = ""
    if args.reset:
        expected = f"{SR_AFTER_RESET:02X}h after reset"
        note = f" (as expected: {expected})" if sr == SR_AFTER_RESET else f" (expected {expected})"
    print(f"status   : {sr:02X}h{note}")
    for line in decode_status(sr):
        print(f"  {line}")
    if sr & SR_NOT_PROTECTED:
        print(
            "WARNING: the chip reports WP# HIGH (not write protected). WP# must be low: stop and check GP13, "
            "its 10k pull-down and ball C3.",
            file=sys.stderr,
        )
        return 1
    return 1 if args.reset and sr != SR_AFTER_RESET else 0


def _hexdump(data: bytes, base: int = 0) -> None:
    for off in range(0, len(data), 16):
        row = data[off : off + 16]
        text = "".join(chr(b) if 32 <= b < 127 else "." for b in row)
        print(f"  {base + off:04X}  {row.hex(' ').upper():<47}  {text}")


def cmd_param(client: Client, args: argparse.Namespace) -> int:
    data = client.read_param()
    copies = [ParamPage.parse(c) for c in split_copies(data)]
    ok = True
    for i, pp in enumerate(copies):
        sig = "OK" if pp.signature_ok else f"BAD ({_hex(pp.signature)})"
        crc = f"stored {_hex(pp.crc_bytes)}, computed {pp.crc_computed:04X}h: {'OK' if pp.crc_ok else 'BAD'}"
        expected = "" if pp.crc_bytes == PARAM_CRC_BYTES else f" (datasheet: {_hex(PARAM_CRC_BYTES)})"
        print(f"copy {i}   : signature {sig}, CRC {crc}{expected}")
        ok = ok and pp.valid and pp.crc_bytes == PARAM_CRC_BYTES
    distinct = {pp.raw for pp in copies}
    if len(distinct) == 1:
        print("copies   : all 3 identical")
    else:
        diffs = [sum(a != b for a, b in zip(copies[0].raw, pp.raw)) for pp in copies[1:]]
        print(f"copies   : NOT identical (copy 1 differs from copy 0 in {diffs[0]} bytes, copy 2 in {diffs[1]})")
    if not copies[0].valid and any(pp.valid for pp in copies[1:]):
        print(
            "hint     : copy 0 is bad but a later copy is good. Reading may have started before tR ended: "
            "check R/B# (GP14, ball C8)."
        )

    pp = next((c for c in copies if c.valid), copies[0])
    tag = "" if pp.valid else "  (UNVERIFIED: no copy passed)"
    onfi = "ONFI 1.0" if pp.revision == 0x0002 else f"ONFI revision bits {pp.revision:04X}h"
    print(f"chip     : {pp.manufacturer} {pp.model}, JEDEC {pp.jedec_id:02X}h, {onfi}{tag}")
    print(f"page     : {pp.page_data_bytes} + {pp.page_spare_bytes} B, partial {pp.partial_data_bytes} + "
          f"{pp.partial_spare_bytes} B, {pp.programs_per_page} programs/page")
    print(f"array    : {pp.pages_per_block} pages/block, {pp.blocks_per_lun} blocks/LUN, {pp.luns} LUN, "
          f"{pp.bits_per_cell} bit/cell, address cycles {pp.address_cycles:02X}h "
          f"({pp.address_cycles >> 4} column + {pp.address_cycles & 0xF} row)")
    print(f"quality  : max {pp.bad_blocks_max} bad blocks/LUN, endurance {pp.block_endurance} cycles, first "
          f"{pp.guaranteed_blocks} block(s) guaranteed for {pp.guaranteed_endurance} cycles, ECC {pp.ecc_bits} bit")
    print(f"timing   : tR {pp.t_r_us} us, tPROG {pp.t_prog_us} us, tBERS {pp.t_bers_us} us, tCCS {pp.t_ccs_ns} ns, "
          f"timing modes {pp.timing_modes:04X}h")
    wrong = {k: (getattr(pp, k), v) for k, v in EXPECTED_PARAM.items() if getattr(pp, k) != v}
    if wrong:
        ok = False
        for k, (got, want) in wrong.items():
            print(f"MISMATCH : {k} = {got}, SPEC expects {want}")
    else:
        print("geometry : matches the SPEC")

    if args.hexdump:
        for i, c in enumerate(copies):
            print(f"copy {i} raw:")
            _hexdump(c.raw, i * PARAM_PAGE_LEN)
    print("result   : PASS" if ok else "result   : FAIL")
    return 0 if ok else 1


def _describe_unstable(page: int, reads: list[bytes], limit: int) -> list[str]:
    """Offsets where the reads of one page disagree: the values seen (with counts) and which bits differ."""
    lines = []
    diff_offsets = [i for i in range(len(reads[0])) if len({r[i] for r in reads}) > 1]
    for off in diff_offsets[:limit]:
        values = Counter(r[off] for r in reads)
        bits = 0
        for v in values:
            bits |= v ^ reads[0][off]
        area = "data" if off < PAGE_DATA else f"spare+{off - PAGE_DATA}"
        seen = ", ".join(f"{v:02X}h x{n}" for v, n in values.most_common())
        lines.append(f"  page {page} offset {off} ({area}): {seen}; bits {bits:08b}")
    if len(diff_offsets) > limit:
        lines.append(f"  page {page}: ... {len(diff_offsets) - limit} more differing bytes")
    return lines


def cmd_read(client: Client, args: argparse.Namespace) -> int:
    if args.block is not None:
        start, count = args.block * PAGES_PER_BLOCK, args.count or PAGES_PER_BLOCK
    else:
        start, count = args.page, args.count or 1
    if start + count > TOTAL_PAGES:
        print(f"error: pages {start}..{start + count - 1} run past the last page ({TOTAL_PAGES - 1})", file=sys.stderr)
        return 1
    last = start + count - 1
    b0, b1 = start // PAGES_PER_BLOCK, last // PAGES_PER_BLOCK
    pages = f"pages {start}..{last}" if count > 1 else f"page {start}"
    blocks = f"blocks {b0}..{b1}" if b1 > b0 else f"block {b0}"
    print(f"read     : {pages} ({blocks}), {args.repeat} pass{'es' if args.repeat > 1 else ''}")

    reads: dict[int, list[bytes]] = {p: [] for p in range(start, start + count)}
    timeouts: Counter[int] = Counter()
    total_bytes, total_s = 0, 0.0
    for _ in range(args.repeat):
        t0 = time.monotonic()
        for page, data in client.read_pages(start, count):
            if data is None:
                timeouts[page] += 1
            else:
                reads[page].append(data)
                total_bytes += len(data)
        total_s += time.monotonic() - t0
    if total_s > 0:
        print(f"speed    : {total_bytes / total_s / 1024:.0f} KiB/s ({total_bytes} bytes in {total_s:.2f} s)")

    unstable = [p for p, r in reads.items() if len(set(r)) > 1]
    erased = sum(1 for r in reads.values() if r and r[0] == b"\xff" * len(r[0]))
    ok = not unstable and not timeouts
    if timeouts:
        pages = ", ".join(f"{p} x{n}" for p, n in sorted(timeouts.items()))
        print(f"R/B#     : {sum(timeouts.values())} timeouts (page x count): {pages}")
    else:
        print("R/B#     : no timeouts")
    if args.repeat > 1:
        if unstable:
            print(f"stable   : NO, {len(unstable)} of {count} pages differ between reads:")
            for p in unstable[:20]:
                for line in _describe_unstable(p, reads[p], limit=8):
                    print(line)
        else:
            print(f"stable   : all {count} page{'s' if count > 1 else ''} identical across {args.repeat} reads")
    print(f"erased   : {erased} of {count} page{'s' if count > 1 else ''} all FFh")
    if args.hexdump:
        for p, r in reads.items():
            if r:
                print(f"page {p} (first read):")
                _hexdump(r[0])
    print("result   : PASS" if ok else "result   : FAIL")
    return 0 if ok else 1


def _duration(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _print_progress(p: Progress) -> None:
    eta = _duration(p.eta_s) if p.eta_s is not None else "?"
    line = (f"  {p.done}/{p.total} pages ({100 * p.done / p.total:5.1f}%)  {p.rate_bps / 1024:6.0f} KiB/s  "
            f"elapsed {_duration(p.elapsed_s)}  ETA {eta}")
    if sys.stderr.isatty():
        print("\r" + line, end="", file=sys.stderr, flush=True)
    else:
        print(line, file=sys.stderr, flush=True)


def cmd_dump(client: Client, args: argparse.Namespace) -> int:
    client.retries = max(client.retries, args.retries)
    out = Path(args.out)
    count = args.count if args.count is not None else TOTAL_PAGES - args.start
    print(f"dump     : pages {args.start}..{args.start + count - 1} -> {out}"
          f"{' (resume)' if args.resume else ''}", file=sys.stderr)
    try:
        r = dump(client, out, start=args.start, count=count, resume=args.resume, force=args.force,
                 retries=args.retries, progress=_print_progress)
    finally:
        if sys.stderr.isatty():
            print(file=sys.stderr)
    rate = r.pages_read * PAGE_SIZE / r.elapsed_s / 1024 if r.elapsed_s > 0 else 0
    print(f"file     : {out} ({r.count} pages, {r.count * PAGE_SIZE} bytes)")
    if r.resumed_at:
        print(f"resumed  : at page {r.start + r.resumed_at}")
    print(f"read     : {r.pages_read} pages in {_duration(r.elapsed_s)} ({rate:.0f} KiB/s)")
    print(f"retries  : {r.rb_retries} R/B# re-reads, {r.stream_restarts} stream restarts after transport errors")
    print(f"sha256   : {r.sha256}")
    print(f"sidecar  : {out.name}.meta.json")
    return 0


def _start_page(path: Path, override: int | None) -> int:
    if override is not None:
        return override
    meta = load_meta(path)
    return meta["start"] if meta else 0


def cmd_compare(client: Client | None, args: argparse.Namespace) -> int:
    a, b = Path(args.a), Path(args.b)
    try:
        na, nb = page_count(a), page_count(b)
    except (CompareError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    start = _start_page(a, args.start)
    if na != nb:
        print(f"size     : DIFFERENT, {a} has {na} pages, {b} has {nb}; comparing the first {min(na, nb)}")
    pages = bytes_ = bits = 0
    for d in diff_pages(a, b):
        pages += 1
        offs = d.offsets
        bytes_ += len(offs)
        bits += d.bit_count
        if pages <= args.max_pages:
            page = start + d.index
            print(f"page {page} (block {page // PAGES_PER_BLOCK}, page {page % PAGES_PER_BLOCK}): "
                  f"{len(offs)} bytes, {d.bit_count} bits differ")
            for off in offs[: args.max_bytes]:
                area = "data" if off < PAGE_DATA else f"spare+{off - PAGE_DATA}"
                print(f"  offset {off:4d} ({area}): {d.a[off]:02X}h vs {d.b[off]:02X}h, bits {d.a[off] ^ d.b[off]:08b}")
            if len(offs) > args.max_bytes:
                print(f"  ... {len(offs) - args.max_bytes} more bytes")
    if pages > args.max_pages:
        print(f"... {pages - args.max_pages} more pages")
    n = min(na, nb)
    if pages == 0:
        print(f"result   : identical ({n} pages{', sizes differ' if na != nb else ''})")
    else:
        print(f"result   : {pages} of {n} pages differ ({bytes_} bytes, {bits} bits)")
    return 0 if pages == 0 and na == nb else 1


def cmd_reconcile(client: Client, args: argparse.Namespace) -> int:
    a, b, out = Path(args.a), Path(args.b), Path(args.out)
    report = Path(args.report) if args.report else out.with_name(out.name + ".report.json")
    start = _start_page(a, args.start)
    meta_a, meta_b = load_meta(a), load_meta(b)
    if args.start is None and meta_a and meta_b and meta_a["start"] != meta_b["start"]:
        print("error: the two dumps' sidecars give different start pages", file=sys.stderr)
        return 1
    r = reconcile(client, a, b, out, report, start=start, reads=args.reads, min_agree=args.min_agree,
                  force=args.force)
    counts = Counter(o.status for o in r.outcomes)
    min_agree = args.min_agree or default_min_agree(args.reads)
    print(f"pages    : {r.pages}, {len(r.outcomes)} differ between the two passes")
    if r.outcomes:
        print(f"re-read  : {args.reads}x each, a bit needs {min_agree} of {args.reads} votes")
        print(f"outcome  : {counts['consistent']} consistent re-reads, {counts['corrected']} corrected by majority, "
              f"{counts['unstable']} UNSTABLE")
        print(f"bits     : {sum(len(o.bits) for o in r.outcomes)} non-unanimous bits listed in the report")
    for o in r.unstable:
        print(f"UNSTABLE : page {o.page} (block {o.page // PAGES_PER_BLOCK}): no clear majority; flagged in the report")
    print(f"output   : {out}")
    print(f"report   : {report}")
    return 1 if r.unstable else 0


def cmd_split(client: Client | None, args: argparse.Namespace) -> int:
    image = Path(args.image)
    outdir = Path(args.outdir) if args.outdir else image.parent
    data = Path(args.data) if args.data else outdir / "data.bin"
    oob = Path(args.oob) if args.oob else outdir / "oob.bin"
    n = split(image, data, oob, force=args.force)
    print(f"pages    : {n}")
    print(f"data     : {data} ({n * PAGE_DATA} bytes, {PAGE_DATA} per page)")
    print(f"oob      : {oob} ({n * (PAGE_SIZE - PAGE_DATA)} bytes, {PAGE_SIZE - PAGE_DATA} per page)")
    return 0


def _ranges(nums: list[int]) -> str:
    """[1, 2, 3, 7] -> "1..3 7"."""
    out = []
    for n in nums:
        if out and out[-1][1] == n - 1:
            out[-1][1] = n
        else:
            out.append([n, n])
    return " ".join(f"{a}..{b}" if b > a else str(a) for a, b in out)


def _badblocks_device(client: Client, args: argparse.Namespace) -> int:
    first = args.first_block or 0
    count = args.count if args.count is not None else BLOCKS - first
    if first + count > BLOCKS:
        print(f"error: blocks {first}..{first + count - 1} run past the last block ({BLOCKS - 1})", file=sys.stderr)
        return 1
    idb = client.read_id()
    if idb != EXPECTED_ID:
        print(f"error: READ_ID is {_hex(idb)}, expected {_hex(EXPECTED_ID)} (check the wiring)", file=sys.stderr)
        return 1
    print(f"scanned  : chip blocks {first}..{first + count - 1} ({count} blocks); rule: datasheet §9.2, spare byte 0 "
          f"(offset {MARKER_OFFSET}) of pages 0, 1 and {PAGES_PER_BLOCK - 1} of each block must be FFh")
    bad = []
    for k, b in enumerate(range(first, first + count)):
        m = read_markers(client, b)
        if m:
            bad.append(b)
            marks = ", ".join(f"page {p}: {v:02X}h" for p, v in sorted(m.items()))
            print(("\r" if sys.stderr.isatty() else "") + f"bad      : block {b}: {marks}")
        if sys.stderr.isatty() and k % 16 == 0:
            print(f"\r  {k}/{count} blocks", end="", file=sys.stderr, flush=True)
    if sys.stderr.isatty():
        print("\r" + " " * 30 + "\r", end="", file=sys.stderr)
    print(f"result   : {len(bad)} bad block{'s' if len(bad) != 1 else ''}{': ' + _ranges(bad) if bad else ''}")
    return 0


def _blank_blocks(image: Path, start: int) -> int:
    img = Image.open(image, start)
    blank = [b for b in range(img.first_block, img.first_block + img.blocks)
             if all(p == ERASED_PAGE for p in img.block(b))]
    print(f"scanned  : blocks {img.first_block}..{img.first_block + img.blocks - 1} ({img.blocks} blocks) of {image}")
    print(f"blank    : {len(blank)} block{'s' if len(blank) != 1 else ''} all FFh (data and spare)"
          f"{': ' + _ranges(blank) if blank else ''}")
    return 0


def cmd_badblocks(client: Client | None, args: argparse.Namespace) -> int:
    if args.device:
        if args.image or args.blank:
            print("error: --device scans the chip; it takes no IMAGE and no --blank", file=sys.stderr)
            return 1
        assert client is not None
        return _badblocks_device(client, args)
    if not args.image:
        print("error: give an IMAGE, or --device to scan the chip", file=sys.stderr)
        return 1
    image = Path(args.image)
    start = _start_page(image, args.start)
    if args.blank:
        return _blank_blocks(image, start)
    scan = scan_bad_blocks(image, start)
    if scan.blocks == 0:
        print("error: the image holds no whole block", file=sys.stderr)
        return 1
    last = scan.first_block + scan.blocks - 1
    print(f"scanned  : blocks {scan.first_block}..{last} ({scan.blocks} blocks); rule: datasheet §9.2, spare byte 0 "
          f"(offset {MARKER_OFFSET}) of pages 0, 1 and {PAGES_PER_BLOCK - 1} of each block must be FFh")
    if scan.skipped_pages:
        print(f"skipped  : {scan.skipped_pages} pages outside whole blocks")
    for b in scan.bad:
        marks = ", ".join(f"page {p}: {v:02X}h" for p, v in sorted(b.markers.items()))
        first = b.block * PAGES_PER_BLOCK
        print(f"bad      : block {b.block} (pages {first}..{first + PAGES_PER_BLOCK - 1}): {marks}")
    print(f"result   : {len(scan.bad)} bad block{'s' if len(scan.bad) != 1 else ''}"
          f"{': ' + ' '.join(str(b.block) for b in scan.bad) if scan.bad else ''}")
    max_bad = EXPECTED_PARAM["bad_blocks_max"]
    if scan.first_block == 0 and any(b.block == 0 for b in scan.bad):
        print("WARNING  : block 0 is guaranteed good (§9.2); its marker is probably the previous system's data")
    if len(scan.bad) > max_bad:
        print(f"WARNING  : more than the {max_bad} bad blocks the chip allows (param page bytes 103-104). On a used "
              "chip the previous system may have written spare byte 0, so these are not all factory marks.")
    return 0


# ---- write mode (docs/WRITE_PROPOSAL.md §5) ---------------------------------------------------------------------


def _ask(prompt: str) -> str | None:
    """Read one line from the user, or None when stdin is not a terminal (scripts must pass --yes)."""
    if not sys.stdin.isatty():
        return None
    try:
        return input(prompt)
    except EOFError:
        return ""


def _confirm(args: argparse.Namespace, phrase: str) -> bool:
    if args.yes:
        print(f"confirm  : --yes given (instead of typing {phrase!r})")
        return True
    answer = _ask(f'confirm  : type "{phrase}" to continue: ')
    if answer is None:
        print("error: stdin is not a terminal; pass --yes to confirm. Nothing was erased.", file=sys.stderr)
        return False
    if answer.strip() != phrase:
        print("aborted  : confirmation did not match. Nothing was erased.")
        return False
    return True


def _check_chip(client: Client) -> bool:
    idb = client.read_id()
    if idb != EXPECTED_ID:
        print(f"error: READ_ID is {_hex(idb)}, expected {_hex(EXPECTED_ID)}: refusing to write (check the wiring)",
              file=sys.stderr)
        return False
    return True


def _blocks_word(n: int) -> str:
    return f"{n} BLOCK{'S' if n != 1 else ''}"


def cmd_erase(client: Client, args: argparse.Namespace) -> int:
    first, count = args.block, args.count
    if first + count > BLOCKS:
        print(f"error: blocks {first}..{first + count - 1} run past the last block ({BLOCKS - 1})", file=sys.stderr)
        return 1
    if not _check_chip(client):
        return 1
    blocks = range(first, first + count)
    print(f"erase    : block{'s' if count > 1 else ''} {first}{f'..{blocks[-1]}' if count > 1 else ''} "
          f"(pages {first * PAGES_PER_BLOCK}..{(blocks[-1] + 1) * PAGES_PER_BLOCK - 1})")
    bad = {b: m for b in blocks if (m := read_markers(client, b))}  # §9.2: before any erase
    for b, m in bad.items():
        marks = ", ".join(f"page {p}: {v:02X}h" for p, v in sorted(m.items()))
        print(f"bad      : block {b}: {marks}")
    if bad and not args.ignore_bad_marker:
        print("error: the range holds bad blocks (§9.2); nothing was erased. --ignore-bad-marker erases them anyway, "
              "destroying the factory marking.", file=sys.stderr)
        return 1
    print("WARNING  : erasing destroys the data in these blocks. There is no undo; keep a dump.")
    if not args.verify:
        print("verify   : off (the chip's status is checked; --verify also reads every block back)")
    if not _confirm(args, f"ERASE {_blocks_word(count)}"):
        return 1
    for b in blocks:
        r = erase_block(client, b, ignore_bad_marker=args.ignore_bad_marker)
        # An erase of a blank block reads back the same either way; SR bit 7 and the busy time show it really ran.
        done = f"erased (SR {r.sr:02X}h, busy {r.busy_ns / 1000:.0f} us)"
        if not args.verify:
            print(f"block {b:<4}: {done}")
            continue
        left = sum(1 for p in read_block(client, b) if p != ERASED_PAGE)
        if left:
            print(f"block {b:<4}: {done}, but {left} page(s) do not read all FFh: VERIFY FAILED")
            print("result   : FAIL")
            return 1
        print(f"block {b:<4}: {done}, verified all FFh")
    print("result   : PASS")
    return 0


def cmd_program(client: Client, args: argparse.Namespace) -> int:
    data = Path(args.infile).read_bytes()
    if len(data) != PAGE_SIZE:
        print(f"error: {args.infile} is {len(data)} bytes; a page is {PAGE_SIZE} (2048 data + 64 spare)",
              file=sys.stderr)
        return 1
    if not _check_chip(client):
        return 1
    page = args.page
    block = page // PAGES_PER_BLOCK
    print(f"program  : page {page} (block {block}, page {page % PAGES_PER_BLOCK}) from {args.infile}")
    if read_range(client, page, 1)[0] != ERASED_PAGE:
        print(f"error: page {page} is not erased (not all FFh). Erase block {block} first; nothing was written.",
              file=sys.stderr)
        return 1
    if data == ERASED_PAGE:
        print("result   : nothing to do (the data is all FFh, which an erased page already holds)")
        return 0
    client.arm_write(block, block, ARM_IDLE_S)
    try:
        r = client.program_page(page, data)
    finally:
        disarm_quietly(client)
    print(f"status   : {r.sr:02X}h, busy {r.busy_ns / 1000:.0f} us")
    if not args.verify:
        print("verify   : off (--verify reads the page back)")
        print("result   : PASS")
        return 0
    back = read_range(client, page, 1)[0]
    if back != data:
        diff = [o for o in range(PAGE_SIZE) if back[o] != data[o]]
        print(f"verify   : FAILED, {len(diff)} bytes differ (first at offset {diff[0]})")
        print("result   : FAIL")
        return 1
    print("verify   : readback identical")
    print("result   : PASS")
    return 0


def cmd_write(client: Client, args: argparse.Namespace) -> int:
    image = Image.open(Path(args.image), args.start)
    first = args.first_block if args.first_block is not None else image.first_block
    count = args.count if args.count is not None else image.first_block + image.blocks - first
    mode = "all blocks" if args.all else "only-changed"
    print(f"image    : {image.path} (blocks {image.first_block}..{image.first_block + image.blocks - 1})")
    print(f"range    : image blocks {first}..{first + count - 1} ({count} block{'s' if count != 1 else ''}), {mode}, "
          f"map {args.map}")
    if not _check_chip(client):
        return 1

    def scan_progress(k: int, n: int) -> None:
        if sys.stderr.isatty():
            end = "\n" if k == n else ""
            print(f"\r  planning: {k}/{n} blocks read", end=end, file=sys.stderr, flush=True)

    plan = build_plan(client, image, first_block=first, count=count, mapping=args.map, only_changed=not args.all,
                      fresh=args.fresh, progress=scan_progress)
    if plan.resumed_block is not None:
        print(f"resume   : an earlier run of this image stopped in block {plan.resumed_block}; it is re-written "
              f"(--fresh ignores the earlier run's sidecar {sidecar_path(image.path).name})")
    tb = sorted(plan.target_bad)
    print(f"target   : {len(tb)} bad block{'s' if len(tb) != 1 else ''}{': ' + _ranges(tb) if tb else ''} "
          f"(markers of {plan.scanned_blocks} blocks read before any erase)")
    if plan.image_bad:
        print(f"image    : {len(plan.image_bad)} block(s) marked bad in the image: {_ranges(sorted(plan.image_bad))} "
              "(not written)")
    writes = plan.to_write
    pages = sum(e.pages for e in writes)
    print(f"plan     : {len(writes)} block(s) to erase + program ({pages} pages; all-FFh pages are not programmed), "
          f"{len(plan.by_action('same'))} identical (skipped), {len(plan.by_action('target-bad'))} blank on a bad "
          f"target (skipped)")
    if args.map == MAP_SKIP_BAD:
        shifted = [e for e in plan.entries if e.target_block is not None and e.target_block != e.image_block]
        if shifted:
            e = shifted[0]
            print(f"map      : {len(shifted)} image block(s) shifted past bad target blocks, starting with image "
                  f"block {e.image_block} -> target block {e.target_block}")
    for e in plan.conflicts:
        print(f"CONFLICT : image block {e.image_block} has data ({e.pages} pages) but target block {e.target_block} "
              "is bad")
    if plan.conflicts:
        print(f"error: {len(plan.conflicts)} conflict(s) with --map {MAP_1TO1}; nothing was written. "
              f"--map {MAP_SKIP_BAD} shifts the image past bad blocks, if the original system expects that.",
              file=sys.stderr)
        return 1
    if not writes:
        print("result   : nothing to write; the chip already matches the image")
        return 0
    if args.dry_run:
        for e in writes[:50]:
            print(f"  would write image block {e.image_block} -> target block {e.target_block} ({e.pages} pages)"
                  f"{' [' + e.reason + ']' if e.reason else ''}")
        if len(writes) > 50:
            print(f"  ... {len(writes) - 50} more")
        print("dry run  : nothing was erased or written")
        return 0

    if args.backup:
        bc = check_backup(client, Path(args.backup))
        if bc.ok:
            print(f"backup   : {args.backup}: {len(bc.sampled)} random non-blank pages match the chip")
        else:
            print(f"WARNING  : backup {args.backup}: {len(bc.mismatched)} of {len(bc.sampled)} sampled pages do NOT "
                  "match the chip. It may not be a backup of THIS chip (or the chip was already changed).")
    else:
        print("WARNING  : no --backup given. Erased data cannot be recovered without a dump of this chip.")
    print(f"WARNING  : {len(writes)} block(s) will be erased and rewritten. There is no undo.")
    if not args.verify:
        print("verify   : off: written blocks are not read back, only the chip's status is checked. --verify reads "
              "each one back; `write IMAGE --dry-run` afterwards compares the whole range.")
    if not _confirm(args, f"ERASE {_blocks_word(len(writes))}"):
        return 1

    tty = sys.stderr.isatty()
    t0 = time.monotonic()

    checked = ", verified" if args.verify else ""

    def block_progress(d: BlockDone) -> None:
        line = (f"block {d.entry.target_block:<4}: written ({d.programmed} pages){checked}  [{d.index}/{d.total}, "
                f"{_duration(time.monotonic() - t0)}]")
        if tty:
            print("\r" + line, end="\n" if d.index == d.total else "", file=sys.stderr, flush=True)
        else:
            print(line, flush=True)

    n = run_plan(client, plan, verify=args.verify, progress=block_progress)
    how = "every one verified by readback" if args.verify else "not read back (no --verify)"
    print(f"written  : {n} block(s) in {_duration(time.monotonic() - t0)}, {how}")
    print(f"sidecar  : {sidecar_path(image.path).name}")
    print("result   : PASS")
    return 0


def _expect(label: str, fn: Callable[[], object], status: Status) -> bool:
    try:
        fn()
    except DeviceError as e:
        ok = e.status == status
        try:
            got = Status(e.status).name
        except ValueError:
            got = f"0x{e.status:02X}"
        print(f"{label:<44}: {got}{'' if ok else f' (expected {status.name})'}  {'OK' if ok else 'FAIL'}")
        return ok
    print(f"{label:<44}: OK (expected {status.name})  FAIL: THE COMMAND RAN")
    return False


def cmd_interlock_test(client: Client, args: argparse.Namespace) -> int:
    """W2: every erase/program here must be refused before it reaches the chip. Only a blank block is used, so even
    a broken interlock loses no data.

    A good example of testing a safety mechanism on real hardware: each case tries one forbidden thing and expects
    one specific refusal (ERR_NOT_ARMED / ERR_BAD_ARGS). Then two independent checks confirm nothing slipped through:
    the status register still says WP# is low, and the block still reads all FFh. The all-00h test page would show
    up there if it had been programmed."""
    b = args.block
    if not _check_chip(client):
        return 1
    first = b * PAGES_PER_BLOCK
    if any(p != ERASED_PAGE for p in read_block(client, b)):
        print(f"error: block {b} is not blank (all FFh). The test only uses a blank block, so a broken interlock "
              "cannot destroy data. Pick one with `badblocks IMAGE --blank`.", file=sys.stderr)
        return 1
    other = b + 1 if b + 1 < BLOCKS else b - 1
    page = bytes(PAGE_SIZE)  # all 00h: would be visible if it were ever programmed
    print(f"interlock: block {b} (blank), every request below must be refused")
    NOT_ARMED, BAD_ARGS = Status.ERR_NOT_ARMED, Status.ERR_BAD_ARGS
    results = []
    client.disarm()
    results.append(_expect("erase, never armed", lambda: client.erase_block(b), NOT_ARMED))
    results.append(_expect("program, never armed", lambda: client.program_page(first, page), NOT_ARMED))
    client.arm_write(other, other, ARM_IDLE_S)
    results.append(_expect(f"erase, armed for block {other} only", lambda: client.erase_block(b), NOT_ARMED))
    results.append(_expect(f"program, armed for block {other} only", lambda: client.program_page(first, page),
                           NOT_ARMED))
    client.disarm()
    results.append(_expect("erase after DISARM", lambda: client.erase_block(b), NOT_ARMED))
    client.arm_write(b, b, ARM_IDLE_S)
    client.reset()
    results.append(_expect("erase after RESET", lambda: client.erase_block(b), NOT_ARMED))
    client.arm_write(b, b, 1)
    time.sleep(1.5)
    results.append(_expect("erase after the 1 s idle timeout", lambda: client.erase_block(b), NOT_ARMED))
    bad_token = struct.pack("<IHHH", 0x12345678, b, b, ARM_IDLE_S)
    results.append(_expect("ARM_WRITE with a wrong token", lambda: client.call(Cmd.ARM_WRITE, bad_token), BAD_ARGS))
    results.append(_expect("erase after the wrong-token ARM_WRITE", lambda: client.erase_block(b), NOT_ARMED))
    client.disarm()
    sr = client.read_status()
    wp_ok = not sr & SR_NOT_PROTECTED
    wp = "low (protected)  OK" if wp_ok else "HIGH  FAIL"
    print(f"{'status register':<44}: {sr:02X}h, WP# {wp}")
    blank = all(p == ERASED_PAGE for p in read_block(client, b))
    print(f"{f'block {b} afterwards':<44}: {'still blank' if blank else 'CHANGED'}  {'OK' if blank else 'FAIL'}")
    ok = all(results) and wp_ok and blank
    print("result   : PASS" if ok else "result   : FAIL")
    return 0 if ok else 1


def _positive_int(text: str) -> int:
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


def _ranged_int(text: str, limit: int, what: str) -> int:
    n = int(text, 0)
    if not 0 <= n < limit:
        raise argparse.ArgumentTypeError(f"{what} must be 0..{limit - 1}")
    return n


def _nonneg_int(text: str) -> int:
    n = int(text)
    if n < 0:
        raise argparse.ArgumentTypeError("must be >= 0")
    return n


def _page_index(text: str) -> int:
    return _ranged_int(text, TOTAL_PAGES, "page")


def _block_index(text: str) -> int:
    return _ranged_int(text, TOTAL_PAGES // PAGES_PER_BLOCK, "block")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="nandtool", description="Pico NAND Tool host (raw NAND dumper and writer)")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__} (protocol v{PROTO_VERSION})")
    p.add_argument("--port", help="serial port (default: auto-detect by USB VID:PID 2E8A:000A)")
    p.add_argument("--timeout", type=float, default=1.0, help="per-response timeout in seconds (default 1.0)")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("ping", help="liveness check; show firmware version")
    sp.set_defaults(func=cmd_ping)

    sp = sub.add_parser("timing", help="show or select bus timing (DEFAULT or SLOW for a logic analyzer)")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--slow", action="store_true", help="SLOW preset: 1 us per phase")
    g.add_argument("--default", action="store_true", help="DEFAULT preset")
    sp.set_defaults(func=cmd_timing)

    sp = sub.add_parser("id", help="read the chip ID (90h 00h) and check it against 01 DA 90 95 44")
    sp.add_argument("--repeat", type=_positive_int, default=1, metavar="N", help="read N times, check all identical")
    sp.add_argument("--onfi", action="store_true", help="also read the ONFI signature (90h 20h)")
    sp.set_defaults(func=cmd_id)

    sp = sub.add_parser("status", help="read and decode the status register (00h, 70h)")
    sp.add_argument("--reset", action="store_true", help="issue FFh first; exit 1 unless the status is then 60h")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser(
        "param", help="read the 3 ONFI parameter page copies, check signature + CRC, decode geometry"
    )
    sp.add_argument("--hexdump", action="store_true", help="also print all 768 raw bytes")
    sp.set_defaults(func=cmd_param)

    sp = sub.add_parser("read", help="read pages (2112 B each) K times and check every read is identical")
    g = sp.add_mutually_exclusive_group(required=True)
    g.add_argument("--page", type=_page_index, metavar="N", help="first page (0..131071)")
    g.add_argument("--block", type=_block_index, metavar="B", help="whole block B (pages B*64 .. B*64+63)")
    sp.add_argument("--count", type=_positive_int, metavar="M", help="number of pages (default 1, or 64 with --block)")
    sp.add_argument("--repeat", type=_positive_int, default=1, metavar="K", help="read the range K times (default 1)")
    sp.add_argument("--hexdump", action="store_true", help="print the first read of every page")
    sp.set_defaults(func=cmd_read)

    sp = sub.add_parser("dump", help="dump pages (data + spare) to a raw file, with a .meta.json sidecar")
    sp.add_argument("--out", required=True, metavar="FILE", help="output image (2112 bytes per page)")
    sp.add_argument("--start", type=_page_index, default=0, metavar="N", help="first page (default 0)")
    sp.add_argument("--count", type=_positive_int, metavar="M", help="number of pages (default: to the end)")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--resume", action="store_true", help="continue an interrupted dump of the same range")
    g.add_argument("--force", action="store_true", help="overwrite an existing FILE")
    sp.add_argument("--retries", type=_nonneg_int, default=DEFAULT_RETRIES, metavar="R",
                    help=f"re-reads of a page with an R/B# timeout before stopping (default {DEFAULT_RETRIES})")
    sp.set_defaults(func=cmd_dump)

    sp = sub.add_parser("compare", help="compare two dumps page by page (no device needed)")
    sp.add_argument("a")
    sp.add_argument("b")
    sp.add_argument("--start", type=_page_index, metavar="N", help="chip page of the first file page (default: "
                    "from A's sidecar, else 0)")
    sp.add_argument("--max-pages", type=_positive_int, default=100, metavar="N", help="pages to list (default 100)")
    sp.add_argument("--max-bytes", type=_positive_int, default=16, metavar="N",
                    help="differing bytes to list per page (default 16)")
    sp.set_defaults(func=cmd_compare, needs_device=False)

    sp = sub.add_parser("reconcile", help="re-read pages that differ between two dumps, majority-vote each bit")
    sp.add_argument("a")
    sp.add_argument("b")
    sp.add_argument("--out", required=True, metavar="FINAL", help="reconciled image (must not exist, or --force)")
    sp.add_argument("--report", metavar="FILE", help="JSON report (default FINAL.report.json)")
    sp.add_argument("--reads", type=_positive_int, default=DEFAULT_READS, metavar="K",
                    help=f"re-reads per differing page (default {DEFAULT_READS})")
    sp.add_argument("--min-agree", type=_positive_int, metavar="M",
                    help="votes a bit needs to count as settled (default: 2/3 of K, at least a majority)")
    sp.add_argument("--start", type=_page_index, metavar="N", help="chip page of the first file page (default: "
                    "from the sidecars, else 0)")
    sp.add_argument("--force", action="store_true", help="overwrite FINAL and the report")
    sp.set_defaults(func=cmd_reconcile)

    sp = sub.add_parser("split", help="split an image into data.bin (2048 B/page) and oob.bin (64 B/page)")
    sp.add_argument("image")
    sp.add_argument("--outdir", metavar="DIR", help="where data.bin and oob.bin go (default: next to IMAGE)")
    sp.add_argument("--data", metavar="FILE", help="data output (overrides --outdir)")
    sp.add_argument("--oob", metavar="FILE", help="OOB output (overrides --outdir)")
    sp.add_argument("--force", action="store_true", help="overwrite existing outputs")
    sp.set_defaults(func=cmd_split, needs_device=False)

    sp = sub.add_parser("badblocks", help="factory bad-block scan of an image or of the chip (datasheet §9.2)")
    sp.add_argument("image", nargs="?")
    sp.add_argument("--start", type=_page_index, metavar="N", help="chip page of the first image page (default: "
                    "from the sidecar, else 0)")
    sp.add_argument("--blank", action="store_true", help="list the image's blocks that are all FFh instead")
    sp.add_argument("--device", action="store_true", help="scan the chip's markers live instead of an image")
    sp.add_argument("--first-block", type=_block_index, metavar="B", help="with --device: first block (default 0)")
    sp.add_argument("--count", type=_positive_int, metavar="N", help="with --device: number of blocks")
    sp.set_defaults(func=cmd_badblocks, needs_device=lambda a: a.device)

    # ---- write mode ----
    sp = sub.add_parser("erase", help="erase blocks (typed confirmation); --verify checks they read all FFh")
    sp.add_argument("--block", type=_block_index, required=True, metavar="B", help="first block (0..2047)")
    sp.add_argument("--count", type=_positive_int, default=1, metavar="N", help="number of blocks (default 1)")
    sp.add_argument("--ignore-bad-marker", action="store_true",
                    help="erase blocks marked bad too (destroys the factory marking, §9.2)")
    sp.add_argument("--yes", action="store_true", help="skip the typed confirmation (for scripts)")
    sp.add_argument("--verify", action="store_true", help="read every erased block back and check it is all FFh")
    sp.set_defaults(func=cmd_erase)

    sp = sub.add_parser("program", help="program one erased page from a 2112-byte file; --verify reads it back")
    sp.add_argument("--page", type=_page_index, required=True, metavar="N", help="page (0..131071)")
    sp.add_argument("--in", dest="infile", required=True, metavar="FILE", help="2112 bytes: 2048 data + 64 spare")
    sp.add_argument("--verify", action="store_true", help="read the page back and compare it with FILE")
    sp.set_defaults(func=cmd_program)

    sp = sub.add_parser("write", help="write a raw image: erase + program changed blocks; --verify reads each back")
    sp.add_argument("image")
    sp.add_argument("--start", type=_page_index, metavar="N", help="chip page of the first image page (default: "
                    "from the sidecar, else 0)")
    sp.add_argument("--first-block", type=_block_index, metavar="B", help="first image block to write (chip "
                    "numbering; default: the image's first)")
    sp.add_argument("--count", type=_positive_int, metavar="N", help="number of image blocks (default: to the end)")
    sp.add_argument("--map", choices=(MAP_1TO1, MAP_SKIP_BAD), default=MAP_1TO1,
                    help="image block -> target block: 1:1 (default; refuses if a bad target block would get data) "
                    "or skip-bad (shift past bad target blocks)")
    sp.add_argument("--all", action="store_true", help="rewrite every block, not only the ones that differ")
    sp.add_argument("--backup", metavar="DUMP", help="a dump of this chip; random pages are checked against it")
    sp.add_argument("--dry-run", action="store_true", help="read and plan, then stop without erasing anything")
    sp.add_argument("--fresh", action="store_true", help="ignore an unfinished earlier run's sidecar (e.g. a "
                    "different chip)")
    sp.add_argument("--yes", action="store_true", help="skip the typed confirmation (for scripts)")
    sp.add_argument("--verify", action="store_true", help="read every written block back and compare it with the "
                    "image; stop at the first mismatch")
    sp.set_defaults(func=cmd_write)

    sp = sub.add_parser("interlock-test", help="W2: check that erase/program are refused unless armed (uses one "
                        "blank block)")
    sp.add_argument("--block", type=_block_index, required=True, metavar="B", help="a block that is all FFh")
    sp.set_defaults(func=cmd_interlock_test)
    return p


def main(argv: list[str] | None = None, transport_factory: Callable[[str | None], Transport] | None = None) -> int:
    args = build_parser().parse_args(argv)
    transport = None
    try:
        # needs_device is True (default), False, or a function of the args (badblocks needs one only with --device).
        needs_device = getattr(args, "needs_device", True)
        if not (needs_device(args) if callable(needs_device) else needs_device):
            return args.func(None, args)
        transport = (transport_factory or open_transport)(args.port)
        return args.func(Client(transport, timeout=args.timeout), args)
    except NandToolError as e:  # port, transport, framing, device-status and version errors
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    finally:
        if transport is not None:
            transport.close()


if __name__ == "__main__":
    sys.exit(main())
