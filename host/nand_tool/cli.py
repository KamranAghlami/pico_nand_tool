"""`nandtool` command line. M2 scope: ping, timing, id, status. Further subcommands arrive with their milestones
(docs/SPEC.md)."""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from collections.abc import Callable

from . import __version__
from .client import Client
from .errors import NandToolError
from .geometry import (
    EXPECTED_ID,
    ONFI_ADDR,
    ONFI_SIGNATURE,
    SR_AFTER_RESET,
    SR_NOT_PROTECTED,
    decode_id,
    decode_status,
)
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


def _positive_int(text: str) -> int:
    n = int(text)
    if n < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return n


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
    sp.add_argument("--reset", action="store_true", help="issue RESET (FFh) first; exit 1 unless the status is then 60h")
    sp.set_defaults(func=cmd_status)
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
