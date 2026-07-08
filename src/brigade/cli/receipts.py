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

    p_export = receipts_sub.add_parser("export", help="Export receipts for external ingest.")
    export_sub = p_export.add_subparsers(dest="receipts_export_command", metavar="<export-target>")
    export_sub.required = True
    p_miseledger = export_sub.add_parser("miseledger", help="Export receipts as miseledger.adapter.v1 JSONL.")
    p_miseledger.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_miseledger.add_argument("--out", default="-", help="Output path, or '-' for stdout.")
    p_miseledger.add_argument("--limit", type=int, default=0, help="Maximum records to export; 0 means all.")
    p_miseledger.add_argument("--new-only", action="store_true", help="Export only receipt items not in the cursor.")
    p_miseledger.add_argument(
        "--import", dest="import_miseledger", action="store_true", help="Import the JSONL with miseledger."
    )
    p_miseledger.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import receipts_cmd

    if args.receipts_command == "verify":
        return receipts_cmd.verify(target=args.target, json_output=args.json)
    if args.receipts_command == "export" and args.receipts_export_command == "miseledger":
        return receipts_cmd.export_miseledger(
            target=args.target,
            out=args.out,
            limit=args.limit,
            new_only=args.new_only,
            import_miseledger=args.import_miseledger,
        )
    args._brigade_parser.error(f"unknown receipts command: {args.receipts_command}")
    return 2
