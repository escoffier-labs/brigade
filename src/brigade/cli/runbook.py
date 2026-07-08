"""brigade runbook command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # runbook
    p_runbook = sub.add_parser("runbook", help="Plan, run, resume, and close out explicit local runbooks.")
    runbook_sub = p_runbook.add_subparsers(dest="runbook_command", metavar="<runbook-command>")
    runbook_sub.required = True
    p_runbook_plan = runbook_sub.add_parser("plan", help="Inspect a runbook without executing it.")
    p_runbook_plan.add_argument("runbook", type=Path, help="Runbook JSON file.")
    p_runbook_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace command target."
    )
    p_runbook_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_runbook_pin = runbook_sub.add_parser("pin", help="Write or refresh optional binary pins for a runbook.")
    p_runbook_pin.add_argument("runbook", type=Path, help="Runbook JSON file.")
    p_runbook_pin.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace command target.")
    p_runbook_pin.add_argument("--dry-run", action="store_true", help="Show pins without writing the runbook file.")
    p_runbook_pin.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_runbook_run = runbook_sub.add_parser("run", help="Run a reviewed runbook and write a receipt.")
    p_runbook_run.add_argument("runbook", nargs="?", type=Path, help="Runbook JSON file.")
    p_runbook_run.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace command target.")
    p_runbook_run.add_argument("--approved", action="store_true", help="Approve this explicit runbook execution.")
    p_runbook_run.add_argument("--dry-run", action="store_true", help="Validate and show steps without executing.")
    p_runbook_run.add_argument(
        "--allow-pin-mismatch",
        action="store_true",
        help="Proceed when a configured runbook pin is missing or has a different sha256.",
    )
    p_runbook_run.add_argument(
        "--resume", dest="resume_run_id", default=None, help="Retry from the first failed step of a previous run."
    )
    p_runbook_run.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_runbook_resume = runbook_sub.add_parser("resume", help="Show resume information for a runbook run.")
    p_runbook_resume.add_argument("run_id", nargs="?", default="latest", help="Run id, unique prefix, or latest.")
    p_runbook_resume.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_runbook_resume.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_runbook_closeout = runbook_sub.add_parser("closeout", help="Mark a runbook run reviewed or deferred.")
    p_runbook_closeout.add_argument("run_id", nargs="?", default="latest", help="Run id, unique prefix, or latest.")
    p_runbook_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_runbook_closeout.add_argument(
        "--status", choices=["reviewed", "deferred", "blocked", "archived"], default="reviewed"
    )
    p_runbook_closeout.add_argument("--reason", default=None, help="Closeout reason.")
    p_runbook_closeout.add_argument(
        "--import-issues",
        dest="import_issues",
        action="store_true",
        help="Route each failed step into the work import inbox for review.",
    )
    p_runbook_closeout.add_argument(
        "--dry-run", action="store_true", help="With --import-issues, report records without writing imports."
    )
    p_runbook_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_runbook.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import runbook_cmd

    if args.runbook_command == "plan":
        return runbook_cmd.plan(target=args.target, runbook=args.runbook, json_output=args.json)
    if args.runbook_command == "pin":
        return runbook_cmd.pin(target=args.target, runbook=args.runbook, dry_run=args.dry_run, json_output=args.json)
    if args.runbook_command == "run":
        if args.resume_run_id:
            return runbook_cmd.retry(
                target=args.target,
                run_id=args.resume_run_id,
                approved=args.approved,
                dry_run=args.dry_run,
                json_output=args.json,
                allow_pin_mismatch=args.allow_pin_mismatch,
            )
        if args.runbook is None:
            args._brigade_parser.error("runbook run requires a runbook path unless --resume is used")
            return 2
        return runbook_cmd.run(
            target=args.target,
            runbook=args.runbook,
            approved=args.approved,
            dry_run=args.dry_run,
            json_output=args.json,
            allow_pin_mismatch=args.allow_pin_mismatch,
        )
    if args.runbook_command == "resume":
        return runbook_cmd.resume(target=args.target, run_id=args.run_id, json_output=args.json)
    if args.runbook_command == "closeout":
        return runbook_cmd.closeout(
            target=args.target,
            run_id=args.run_id,
            status=args.status,
            reason=args.reason,
            import_issues=args.import_issues,
            dry_run=args.dry_run,
            json_output=args.json,
        )
    args._brigade_parser.error(f"unknown runbook command: {args.runbook_command}")
    return 2
