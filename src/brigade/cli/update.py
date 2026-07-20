"""brigade update command group."""

from __future__ import annotations

import argparse


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("update", help="Update Brigade through an immutable stable or beta channel.")
    parser.add_argument("--channel", choices=("stable", "beta"), default="stable")
    parser.add_argument(
        "--dry-run", action="store_true", help="Resolve and report the immutable update without writing."
    )
    parser.add_argument(
        "--switch-channel",
        action="store_true",
        help="Explicitly transfer user-global update ownership to the selected channel.",
    )
    parser.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from ..update_cmd import run_update

    return run_update(channel=args.channel, dry_run=args.dry_run, switch_channel=args.switch_channel)
