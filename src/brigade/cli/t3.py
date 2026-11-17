"""brigade t3 command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p_t3 = sub.add_parser("t3", help="Enroll Brigade batches for governed native T3 delegation.")
    t3_sub = p_t3.add_subparsers(dest="t3_command", metavar="<t3-command>")
    t3_sub.required = True
    p_open = t3_sub.add_parser(
        "open",
        help="Enroll a validated batch for a T3 parent to delegate. Never launches work.",
    )
    p_open.add_argument("batch_file", type=Path, help="Versioned brigade.run_batch.v1 batch JSON file.")
    p_open.add_argument(
        "--target",
        "-t",
        type=Path,
        default=Path("."),
        help="Workspace to enroll against. Its clean HEAD becomes the source revision.",
    )
    p_open.add_argument(
        "--roster",
        type=Path,
        default=None,
        help="Roster TOML path. Defaults to the roster that applies to --target.",
    )
    p_t3.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import native_t3_enrollment

    if args.t3_command == "open":
        return native_t3_enrollment.open_cli(args)
    return 2
