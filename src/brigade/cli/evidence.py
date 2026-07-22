"""brigade evidence command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p_evidence = sub.add_parser(
        "evidence",
        help="Inspect and query the evidence ledger: receipts, search, crawl and export plans.",
    )
    evidence_sub = p_evidence.add_subparsers(dest="evidence_command", metavar="<evidence-command>")
    evidence_sub.required = True

    p_status = evidence_sub.add_parser("status", help="Show evidence engine install and archive health.")
    p_status.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_doctor = evidence_sub.add_parser(
        "doctor",
        help="Advisory evidence health. Exits 1 on engine fail/incomplete/timeout.",
    )
    p_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_crawl = evidence_sub.add_parser(
        "crawl", help="Crawl sources into the evidence ledger, or use `crawl plan` to preview it."
    )
    # Executable crawl arguments are intentionally opaque. `crawl plan` is
    # parsed in dispatch to preserve the established plan command contract.
    p_crawl.add_argument("engine_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p_crawl.set_defaults(_brigade_command_contract_leaf=True, _brigade_legacy_plan=True)

    p_search = evidence_sub.add_parser("search", help="Search the evidence ledger.")
    p_search.add_argument("engine_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    p_show = evidence_sub.add_parser("show", help="Show one evidence item by id.")
    p_show.add_argument("engine_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    p_explain = evidence_sub.add_parser("explain", help="Explain how an evidence item was ingested and linked.")
    p_explain.add_argument("engine_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    p_stats = evidence_sub.add_parser("stats", help="Show evidence ledger statistics.")
    p_stats.add_argument("engine_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    p_export = evidence_sub.add_parser(
        "export", help="Plan receipt export into the evidence ledger without executing it."
    )
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
        if args.engine_args and args.engine_args[0] == "plan":
            plan_args = _crawl_plan_parser().parse_args(args.engine_args[1:])
            return evidence_cmd.crawl_plan(target=plan_args.target, write=plan_args.write, json_output=plan_args.json)
        return evidence_cmd.run_engine("crawl", args.engine_args)
    if args.evidence_command == "search":
        return evidence_cmd.run_engine("search", args.engine_args)
    if args.evidence_command in ("show", "explain", "stats"):
        return evidence_cmd.run_engine(args.evidence_command, args.engine_args)
    if args.evidence_command == "export":
        if args.evidence_export_command == "plan":
            return evidence_cmd.export_plan(target=args.target, write=args.write, json_output=args.json)
        args._brigade_parser.error(f"unknown evidence export command: {args.evidence_export_command}")
        return 2
    args._brigade_parser.error(f"unknown evidence command: {args.evidence_command}")
    return 2


def _crawl_plan_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brigade evidence crawl plan")
    parser.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace for plan paths and --write."
    )
    parser.add_argument("--write", action="store_true", help="Write plan under .brigade/evidence/plans/.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser
