"""brigade workflow command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p_workflow = sub.add_parser("workflow", help="Mine local receipts for repeatable workflow candidates.")
    workflow_sub = p_workflow.add_subparsers(dest="workflow_command", metavar="<workflow-command>")
    workflow_sub.required = True

    p_workflow_scan = workflow_sub.add_parser("scan", help="Scan verify and daily receipts for workflow candidates.")
    p_workflow_scan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_workflow_scan.add_argument(
        "--days",
        type=int,
        default=30,
        help="Only scan receipts from the last N days.",
    )
    p_workflow_scan.add_argument(
        "--min-count",
        type=int,
        default=2,
        help="Minimum observations needed to emit a candidate.",
    )
    p_workflow_scan.add_argument(
        "--min-steps",
        type=int,
        default=1,
        help="Minimum command count in a sequence.",
    )
    p_workflow_scan.add_argument(
        "--max-files",
        type=int,
        default=None,
        help="Maximum receipt files to read from each source.",
    )
    p_workflow_scan.add_argument(
        "--import-candidates",
        action="store_true",
        help="Append candidates to the Brigade work import inbox for review.",
    )
    p_workflow_scan.add_argument("--dry-run", action="store_true", help="Report without writing artifacts or imports.")
    p_workflow_scan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_workflow_scan.set_defaults(func=dispatch)

    p_workflow_show = workflow_sub.add_parser("show", help="Show the latest workflow scan results.")
    p_workflow_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_workflow_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_workflow_show.set_defaults(func=dispatch)

    p_workflow_propose_runbook = workflow_sub.add_parser(
        "propose-runbook", help="Write a reviewed runbook draft from a workflow candidate."
    )
    p_workflow_propose_runbook.add_argument("candidate_id", help="Workflow candidate id or unique prefix.")
    p_workflow_propose_runbook.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_workflow_propose_runbook.add_argument(
        "--dry-run", action="store_true", help="Preview generated runbook without writing."
    )
    p_workflow_propose_runbook.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_workflow_propose_runbook.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import workflow_cmd

    if args.workflow_command == "scan":
        return workflow_cmd.scan(
            target=args.target,
            days=args.days,
            min_count=args.min_count,
            min_steps=args.min_steps,
            max_files=args.max_files,
            import_candidates=args.import_candidates,
            dry_run=args.dry_run,
            json_output=args.json,
        )
    if args.workflow_command == "show":
        return workflow_cmd.show(target=args.target, json_output=args.json)
    if args.workflow_command == "propose-runbook":
        return workflow_cmd.propose_runbook(
            target=args.target,
            candidate_id=args.candidate_id,
            dry_run=args.dry_run,
            json_output=args.json,
        )
    parser = getattr(args, "_brigade_parser", None)
    if parser is not None:
        parser.error(f"unknown workflow command: {args.workflow_command}")
    return 2
