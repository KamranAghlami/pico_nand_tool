"""`nandtool` command line. M0 scope: ping, timing. Further subcommands arrive with their milestones (docs/SPEC.md)."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from . import __version__
from .client import Client, DeviceError, ProtocolMismatch, TransportError
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
    return p


def main(argv: list[str] | None = None, transport_factory: Callable[[str | None], Transport] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        transport = (transport_factory or open_transport)(args.port)
    except Exception as e:  # port missing / permission denied / several candidates
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        return args.func(Client(transport, timeout=args.timeout), args)
    except (DeviceError, TransportError, ProtocolMismatch) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        transport.close()


if __name__ == "__main__":
    sys.exit(main())
