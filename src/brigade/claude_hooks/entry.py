"""``brigade-hook``: direct entry point for one managed Claude hook event.

Equivalent to ``brigade work hook-run`` but never imports ``brigade.cli``, which
skips building the full argparse tree on every tool call. Flags, validation
messages, and exit codes match ``hook-run``; the event choices come from the
same ``MANAGED_EVENTS`` constant the CLI registers.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .package import MANAGED_EVENTS


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brigade work hook-run")
    parser.add_argument(
        "--event",
        required=True,
        choices=list(MANAGED_EVENTS),
        help="Claude hook event to handle.",
    )
    parser.add_argument("--package", required=True, help="Managed hook package id and version.")
    parser.add_argument(
        "--target",
        "-t",
        type=Path,
        default=None,
        help="Pin hook work to this wired workspace. Sessions outside it no-op.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args, extra = parser.parse_known_args(sys.argv[1:] if argv is None else argv)
    if extra:
        # The full CLI reports leftovers from its root parser; keep that text.
        print("usage: brigade [-h] [--version] <command> ...", file=sys.stderr)
        print(f"brigade: error: unrecognized arguments: {' '.join(extra)}", file=sys.stderr)
        raise SystemExit(2)
    from .runtime import hook_run

    if args.target is None:
        return hook_run(event=args.event, package=args.package)
    return hook_run(event=args.event, package=args.package, target=args.target)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
