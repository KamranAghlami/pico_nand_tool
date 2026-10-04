"""`nandtool` command line. M4 scope: ping, timing, id, status, param, read. Further subcommands arrive with their
milestones (docs/SPEC.md)."""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from collections.abc import Callable

from . import __version__
from .client import Client
from .errors import NandToolError
from .geometry import (
    EXPECTED_ID,
    EXPECTED_PARAM,
    ONFI_ADDR,
    ONFI_SIGNATURE,
    PAGE_DATA,
    PAGES_PER_BLOCK,
    PARAM_CRC_BYTES,
    SR_AFTER_RESET,
    SR_NOT_PROTECTED,
    TOTAL_PAGES,
    decode_id,
    decode_status,
)
from .onfi import PARAM_PAGE_LEN, ParamPage, split_copies
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
    return p


def main(argv: list[str] | None = None, transport_factory: Callable[[str | None], Transport] | None = None) -> int:
    args = build_parser().parse_args(argv)
    transport = None
    try:
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
