"""`nandtool` command line. M5 scope: ping, timing, id, status, param, read, dump, compare, reconcile. Further
subcommands arrive with their milestones (docs/SPEC.md)."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from collections import Counter
from collections.abc import Callable

from . import __version__
from .client import Client
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
from .reconcile import DEFAULT_READS, default_min_agree, reconcile
from .protocol import PROTO_VERSION, TimingMode
from .transport import Transport, open_transport


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
            print("reset    : OK, but R/B# was never seen low (check R/B#, ball C8)")
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
    p = argparse.ArgumentParser(prog="nandtool", description="Pico NAND Tool host (read-only NAND dumper)")
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
    return p


def main(argv: list[str] | None = None, transport_factory: Callable[[str | None], Transport] | None = None) -> int:
    args = build_parser().parse_args(argv)
    transport = None
    try:
        if not getattr(args, "needs_device", True):
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
