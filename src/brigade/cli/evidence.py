"""brigade evidence command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p_evidence = sub.add_parser(
        "evidence",
        help="Inspect and plan MiseLedger evidence station health (crawl + receipt export).",
    )
    evidence_sub = p_evidence.add_subparsers(dest="evidence_command", metavar="<evidence-command>")
    evidence_sub.required = True

    p_status = evidence_sub.add_parser("status", help="Show MiseLedger install and archive health.")
    p_status.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_doctor = evidence_sub.add_parser(
        "doctor",
        help="Advisory evidence health. Exits 1 on miseledger fail/incomplete/timeout.",
    )
    p_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_crawl = evidence_sub.add_parser("crawl", help="Plan miseledger crawl without executing it.")
    crawl_sub = p_crawl.add_subparsers(dest="evidence_crawl_command", metavar="<crawl-command>")
    crawl_sub.required = True
    p_crawl_plan = crawl_sub.add_parser("plan", help="Plan miseledger init/crawl/doctor commands.")
    p_crawl_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace for plan paths and --write."
    )
    p_crawl_plan.add_argument("--write", action="store_true", help="Write plan under .brigade/evidence/plans/.")
    p_crawl_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_export = evidence_sub.add_parser("export", help="Plan receipt export into MiseLedger without executing it.")
    export_sub = p_export.add_subparsers(dest="evidence_export_command", metavar="<export-command>")
    export_sub.required = True
    p_export_plan = export_sub.add_parser("plan", help="Plan brigade receipts export miseledger --new-only --import.")
    p_export_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace for plan paths and --write."
    )
    p_export_plan.add_argument("--write", action="store_true", help="Write plan under .brigade/evidence/plans/.")
    p_export_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_evidence.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import evidence_cmd

    if args.evidence_command == "status":
        return evidence_cmd.status(target=args.target, json_output=args.json)
    if args.evidence_command == "doctor":
        return evidence_cmd.doctor(target=args.target, json_output=args.json)
    if args.evidence_command == "crawl":
        if args.evidence_crawl_command == "plan":
            return evidence_cmd.crawl_plan(target=args.target, write=args.write, json_output=args.json)
        args._brigade_parser.error(f"unknown evidence crawl command: {args.evidence_crawl_command}")
        return 2
    if args.evidence_command == "export":
        if args.evidence_export_command == "plan":
            return evidence_cmd.export_plan(target=args.target, write=args.write, json_output=args.json)
        args._brigade_parser.error(f"unknown evidence export command: {args.evidence_export_command}")
        return 2
    args._brigade_parser.error(f"unknown evidence command: {args.evidence_command}")
    return 2
