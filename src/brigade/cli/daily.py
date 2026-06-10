"""brigade daily command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # daily
    p_daily = sub.add_parser("daily", help="Run the personal daily operator loop.")
    daily_sub = p_daily.add_subparsers(dest="daily_command", metavar="<daily-command>")
    daily_sub.required = True
    for name in ("status", "review", "schema", "doctor"):
        p_daily_action = daily_sub.add_parser(name, help=f"Show daily {name}.")
        p_daily_action.add_argument(
            "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
        )
        p_daily_action.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_init = daily_sub.add_parser("init", help="Write local daily driver defaults.")
    p_daily_init.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_daily_init.add_argument("--force", action="store_true", help="Overwrite an existing daily config.")
    p_daily_init.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_plan = daily_sub.add_parser("plan", help="Create the ranked daily plan.")
    p_daily_plan.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_daily_plan.add_argument("--record", action="store_true", help="Write a local daily plan receipt.")
    p_daily_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_run = daily_sub.add_parser("run", help="Run one bounded safe daily action.")
    p_daily_run.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_daily_run.add_argument(
        "--approved", action="store_true", help="Allow the selected action when it requires explicit approval."
    )
    p_daily_run.add_argument("--approval", default=None, help="Run using an approved daily approval request.")
    p_daily_run.add_argument("--plan-id", default=None, help="Run from a recorded daily plan id or latest.")
    p_daily_run.add_argument(
        "--replan", action="store_true", help="Ignore a stale or supplied plan and choose a fresh action."
    )
    p_daily_run.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    for name, help_text in (
        ("resume", "Resume or explain recovery for the latest daily run."),
        ("repair", "Inspect daily driver state and write local repair metadata."),
        ("unblock", "Create local unblock metadata, imports, or approval requests."),
        ("protocol", "Print the wrapper-facing daily agent protocol."),
    ):
        p_daily_extra = daily_sub.add_parser(name, help=help_text)
        p_daily_extra.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
        if name == "unblock":
            p_daily_extra.add_argument("--dry-run", action="store_true", help="Preview unblock writes.")
        p_daily_extra.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_telemetry = daily_sub.add_parser("telemetry", help="Summarize local daily driver telemetry.")
    p_daily_telemetry.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_daily_telemetry.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    telemetry_sub = p_daily_telemetry.add_subparsers(dest="daily_telemetry_command", metavar="<telemetry-command>")
    p_daily_telemetry_doctor = telemetry_sub.add_parser("doctor", help="Check daily telemetry health.")
    p_daily_telemetry_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_daily_telemetry_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_hardening = daily_sub.add_parser("hardening", help="Plan and audit daily production hardening.")
    hardening_sub = p_daily_hardening.add_subparsers(dest="daily_hardening_command", metavar="<hardening-command>")
    hardening_sub.required = True
    for name in ("plan", "audit"):
        p_daily_hardening_action = hardening_sub.add_parser(name, help=f"Run daily hardening {name}.")
        p_daily_hardening_action.add_argument(
            "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
        )
        p_daily_hardening_action.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_hardening_import = hardening_sub.add_parser(
        "import-issues", help="Route hardening findings into the work inbox."
    )
    p_daily_hardening_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_daily_hardening_import.add_argument("--dry-run", action="store_true", help="Preview imports without writing.")
    p_daily_hardening_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_hardening_closeout = hardening_sub.add_parser("closeout", help="Write a local hardening closeout receipt.")
    p_daily_hardening_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_daily_hardening_closeout.add_argument(
        "--status", choices=["reviewed", "deferred", "blocked", "archived"], default="reviewed"
    )
    p_daily_hardening_closeout.add_argument("--reason", default=None, help="Closeout reason.")
    p_daily_hardening_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_approvals = daily_sub.add_parser("approvals", help="Review daily approval requests.")
    approvals_sub = p_daily_approvals.add_subparsers(dest="daily_approval_command", metavar="<approval-command>")
    approvals_sub.required = True
    p_daily_approvals_list = approvals_sub.add_parser("list", help="List daily approval requests.")
    p_daily_approvals_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_daily_approvals_list.add_argument("--limit", type=int, default=50, help="Maximum approvals to show.")
    p_daily_approvals_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_approvals_show = approvals_sub.add_parser("show", help="Show a daily approval request.")
    p_daily_approvals_show.add_argument("approval_id")
    p_daily_approvals_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_daily_approvals_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    for name in ("approve", "reject", "hold"):
        p_daily_approval_review = approvals_sub.add_parser(name, help=f"{name.title()} a daily approval request.")
        p_daily_approval_review.add_argument("approval_id")
        p_daily_approval_review.add_argument(
            "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
        )
        if name in {"reject", "hold"}:
            p_daily_approval_review.add_argument("--reason", required=True, help="Review reason.")
        else:
            p_daily_approval_review.add_argument("--reason", default=None, help="Optional review reason.")
        p_daily_approval_review.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_approvals_compare = approvals_sub.add_parser(
        "compare", help="Compare a daily approval request with current evidence."
    )
    p_daily_approvals_compare.add_argument("approval_id")
    p_daily_approvals_compare.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_daily_approvals_compare.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_approvals_archive = approvals_sub.add_parser("archive", help="Archive closed daily approval requests.")
    p_daily_approvals_archive.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_daily_approvals_archive.add_argument(
        "--consumed", action="store_true", help="Archive consumed, rejected, or superseded approvals."
    )
    p_daily_approvals_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_history = daily_sub.add_parser("history", help="List local daily receipts.")
    p_daily_history.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_daily_history.add_argument("--limit", type=int, default=20, help="Maximum receipts to show.")
    p_daily_history.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_show = daily_sub.add_parser("show", help="Show a daily run receipt.")
    p_daily_show.add_argument("run_id", nargs="?", default="latest", help="Run id or latest.")
    p_daily_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_daily_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily_closeout = daily_sub.add_parser("closeout", help="Close out the latest daily run.")
    p_daily_closeout.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_daily_closeout.add_argument(
        "--status", choices=["reviewed", "deferred", "blocked", "archived"], default="reviewed"
    )
    p_daily_closeout.add_argument("--reason", default=None, help="Closeout reason.")
    p_daily_closeout.add_argument("--handoff", action="store_true", help="Write and lint a Memory Handoff draft.")
    p_daily_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_daily.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import daily_cmd

    if args.daily_command == "init":
        return daily_cmd.init(target=args.target, force=args.force, json_output=args.json)
    if args.daily_command == "status":
        return daily_cmd.status(target=args.target, json_output=args.json)
    if args.daily_command == "plan":
        return daily_cmd.plan(target=args.target, record=args.record, json_output=args.json)
    if args.daily_command == "review":
        return daily_cmd.review(target=args.target, json_output=args.json)
    if args.daily_command == "schema":
        return daily_cmd.schema(target=args.target, json_output=args.json)
    if args.daily_command == "protocol":
        return daily_cmd.protocol(target=args.target, json_output=args.json)
    if args.daily_command == "resume":
        return daily_cmd.resume(target=args.target, json_output=args.json)
    if args.daily_command == "repair":
        return daily_cmd.repair(target=args.target, json_output=args.json)
    if args.daily_command == "unblock":
        return daily_cmd.unblock(target=args.target, dry_run=args.dry_run, json_output=args.json)
    if args.daily_command == "telemetry":
        if getattr(args, "daily_telemetry_command", None) == "doctor":
            return daily_cmd.telemetry_doctor(target=args.target, json_output=args.json)
        return daily_cmd.telemetry(target=args.target, json_output=args.json)
    if args.daily_command == "hardening":
        if args.daily_hardening_command == "plan":
            return daily_cmd.hardening_plan(target=args.target, json_output=args.json)
        if args.daily_hardening_command == "audit":
            return daily_cmd.hardening_audit(target=args.target, json_output=args.json)
        if args.daily_hardening_command == "import-issues":
            return daily_cmd.hardening_import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
        if args.daily_hardening_command == "closeout":
            return daily_cmd.hardening_closeout(
                target=args.target, status=args.status, reason=args.reason, json_output=args.json
            )
        args._brigade_parser.error(f"unknown daily hardening command: {args.daily_hardening_command}")
        return 2
    if args.daily_command == "history":
        return daily_cmd.history(target=args.target, limit=args.limit, json_output=args.json)
    if args.daily_command == "show":
        return daily_cmd.show(target=args.target, run_id=args.run_id, json_output=args.json)
    if args.daily_command == "doctor":
        return daily_cmd.doctor(target=args.target, json_output=args.json)
    if args.daily_command == "approvals":
        if args.daily_approval_command == "list":
            return daily_cmd.approvals_list(target=args.target, limit=args.limit, json_output=args.json)
        if args.daily_approval_command == "show":
            return daily_cmd.approvals_show(target=args.target, approval_id=args.approval_id, json_output=args.json)
        if args.daily_approval_command == "approve":
            return daily_cmd.approvals_approve(target=args.target, approval_id=args.approval_id, json_output=args.json)
        if args.daily_approval_command == "reject":
            return daily_cmd.approvals_reject(
                target=args.target, approval_id=args.approval_id, reason=args.reason, json_output=args.json
            )
        if args.daily_approval_command == "hold":
            return daily_cmd.approvals_hold(
                target=args.target, approval_id=args.approval_id, reason=args.reason, json_output=args.json
            )
        if args.daily_approval_command == "compare":
            return daily_cmd.approvals_compare(target=args.target, approval_id=args.approval_id, json_output=args.json)
        if args.daily_approval_command == "archive":
            return daily_cmd.approvals_archive(target=args.target, consumed=args.consumed, json_output=args.json)
        args._brigade_parser.error(f"unknown daily approvals command: {args.daily_approval_command}")
        return 2
    if args.daily_command == "run":
        return daily_cmd.run(
            target=args.target,
            approved=args.approved,
            approval_id=args.approval,
            plan_id=args.plan_id,
            replan=args.replan,
            json_output=args.json,
        )
    if args.daily_command == "closeout":
        return daily_cmd.closeout(
            target=args.target, status=args.status, reason=args.reason, handoff=args.handoff, json_output=args.json
        )
    args._brigade_parser.error(f"unknown daily command: {args.daily_command}")
    return 2
