"""brigade receipts command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p_receipts = sub.add_parser("receipts", help="Verify local Brigade receipt digests.")
    receipts_sub = p_receipts.add_subparsers(dest="receipts_command", metavar="<receipts-command>")
    receipts_sub.required = True

    p_verify = receipts_sub.add_parser("verify", help="Verify receipt and outcome digest chains.")
    p_verify.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_verify.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_verify.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import receipts_cmd

    if args.receipts_command == "verify":
        return receipts_cmd.verify(target=args.target, json_output=args.json)
    args._brigade_parser.error(f"unknown receipts command: {args.receipts_command}")
    return 2
