"""brigade center command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # center
    p_center = sub.add_parser("center", help="Read local operator-center summaries.")
    center_sub = p_center.add_subparsers(dest="center_command", metavar="<center-command>")
    center_sub.required = True
    for name in ("status", "activity", "reviews", "templates"):
        p_center_action = center_sub.add_parser(name, help=f"Show local operator-center {name}.")
        p_center_action.add_argument(
            "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
        )
        p_center_action.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
        if name in {"activity", "reviews"}:
            p_center_action.add_argument("--limit", type=int, default=50, help="Maximum rows to show.")
    p_center_schema = center_sub.add_parser("schema", help="Show local operator-center JSON schema manifest.")
    p_center_schema.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_center_schema.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_readiness = center_sub.add_parser("readiness", help="Plan and close out local operator readiness.")
    center_readiness_sub = p_center_readiness.add_subparsers(
        dest="center_readiness_command", metavar="<center-readiness-command>"
    )
    center_readiness_sub.required = True
    p_center_readiness_plan = center_readiness_sub.add_parser(
        "plan", help="Plan local operator readiness without writing a receipt."
    )
    p_center_readiness_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_readiness_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_readiness_closeout = center_readiness_sub.add_parser(
        "closeout", help="Write a local operator readiness closeout."
    )
    p_center_readiness_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_center_readiness_closeout.add_argument(
        "--status", choices=["reviewed", "deferred", "blocked", "archived"], default="reviewed"
    )
    p_center_readiness_closeout.add_argument("--reason", default=None, help="Review or waiver reason.")
    p_center_readiness_closeout.add_argument(
        "--waive", action="append", default=[], help="Readiness finding id to waive. May be repeated."
    )
    p_center_readiness_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_readiness_list = center_readiness_sub.add_parser("list", help="List local operator readiness closeouts.")
    p_center_readiness_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_readiness_list.add_argument("--limit", type=int, default=20, help="Maximum closeouts to list.")
    p_center_readiness_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_readiness_show = center_readiness_sub.add_parser(
        "show", help="Show one local operator readiness closeout."
    )
    p_center_readiness_show.add_argument(
        "readiness_id", nargs="?", default="latest", help="Readiness id, unique prefix, or latest."
    )
    p_center_readiness_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_readiness_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_readiness_import = center_readiness_sub.add_parser(
        "import-issues", help="Import unresolved readiness issues into the work inbox."
    )
    p_center_readiness_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_center_readiness_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_center_readiness_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report = center_sub.add_parser("report", help="Plan, build, and inspect local operator report bundles.")
    center_report_sub = p_center_report.add_subparsers(dest="center_report_command", metavar="<center-report-command>")
    center_report_sub.required = True
    p_center_report_plan = center_report_sub.add_parser("plan", help="Plan a local operator report without writing it.")
    p_center_report_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_report_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_build = center_report_sub.add_parser("build", help="Build a local operator report bundle.")
    p_center_report_build.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_center_report_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_list = center_report_sub.add_parser("list", help="List local operator report bundles.")
    p_center_report_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_report_list.add_argument("--limit", type=int, default=20, help="Maximum reports to list.")
    p_center_report_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_show = center_report_sub.add_parser("show", help="Show one local operator report bundle.")
    p_center_report_show.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_center_report_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_report_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_archive = center_report_sub.add_parser("archive", help="Archive one local operator report bundle.")
    p_center_report_archive.add_argument("report_id", help="Report id, unique prefix, or latest.")
    p_center_report_archive.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_center_report_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_review = center_report_sub.add_parser(
        "review", help="Review one local operator report action plan."
    )
    p_center_report_review.add_argument(
        "report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest."
    )
    p_center_report_review.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_report_review.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_compare = center_report_sub.add_parser(
        "compare", help="Compare one operator report against current local state."
    )
    p_center_report_compare.add_argument(
        "report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest."
    )
    p_center_report_compare.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_report_compare.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_diff = center_report_sub.add_parser("diff", help="Diff two local operator reports.")
    p_center_report_diff.add_argument("base_report_id", help="Older report id, unique prefix, or latest.")
    p_center_report_diff.add_argument("compare_report_id", help="Newer report id or unique prefix.")
    p_center_report_diff.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect or update."
    )
    p_center_report_diff.add_argument("--record", action="store_true", help="Write a local report diff receipt.")
    p_center_report_diff.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_report_closeout = center_report_sub.add_parser("closeout", help="Mark one operator report review state.")
    p_center_report_closeout.add_argument(
        "report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest."
    )
    p_center_report_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_center_report_closeout.add_argument(
        "--status", choices=["reviewed", "deferred", "superseded", "archived"], default="reviewed"
    )
    p_center_report_closeout.add_argument("--reason", default=None, help="Review reason.")
    p_center_report_closeout.add_argument(
        "--defer-item", action="append", default=[], help="Deferred report item id. May be repeated."
    )
    p_center_report_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions = center_sub.add_parser("actions", help="Plan and manage local daily operator actions.")
    center_actions_sub = p_center_actions.add_subparsers(
        dest="center_actions_command", metavar="<center-actions-command>"
    )
    center_actions_sub.required = True
    p_center_actions_plan = center_actions_sub.add_parser("plan", help="Plan daily actions from an operator report.")
    p_center_actions_plan.add_argument(
        "report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest."
    )
    p_center_actions_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_actions_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_build = center_actions_sub.add_parser(
        "build", help="Build a daily action queue from an operator report."
    )
    p_center_actions_build.add_argument(
        "report_id", nargs="?", default="latest", help="Report id, unique prefix, or latest."
    )
    p_center_actions_build.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_center_actions_build.add_argument(
        "--allow-unreviewed", action="store_true", help="Build from an unclosed report."
    )
    p_center_actions_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_list = center_actions_sub.add_parser("list", help="List local daily operator actions.")
    p_center_actions_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_actions_list.add_argument("--limit", type=int, default=50, help="Maximum actions to list.")
    p_center_actions_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_show = center_actions_sub.add_parser("show", help="Show one local daily operator action.")
    p_center_actions_show.add_argument("action_id", help="Action id or unique prefix.")
    p_center_actions_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_actions_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_doctor = center_actions_sub.add_parser(
        "doctor", help="Check local daily operator action aging policy."
    )
    p_center_actions_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_center_actions_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_import = center_actions_sub.add_parser(
        "import-issues", help="Import stale operator action issues into the work inbox."
    )
    p_center_actions_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_center_actions_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_center_actions_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    for name in ("start", "done"):
        p_center_actions_state = center_actions_sub.add_parser(name, help=f"Mark one action {name}.")
        p_center_actions_state.add_argument("action_id", help="Action id or unique prefix.")
        p_center_actions_state.add_argument(
            "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
        )
        p_center_actions_state.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_defer = center_actions_sub.add_parser("defer", help="Defer one local daily operator action.")
    p_center_actions_defer.add_argument("action_id", help="Action id or unique prefix.")
    p_center_actions_defer.add_argument("--reason", required=True, help="Deferral reason.")
    p_center_actions_defer.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_center_actions_defer.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center_actions_archive = center_actions_sub.add_parser(
        "archive", help="Archive completed local daily operator actions."
    )
    p_center_actions_archive.add_argument(
        "--completed", action="store_true", required=True, help="Archive completed actions."
    )
    p_center_actions_archive.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_center_actions_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_center.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import center_cmd

    if args.center_command == "status":
        return center_cmd.status(target=args.target, json_output=args.json)
    if args.center_command == "activity":
        return center_cmd.activity(target=args.target, limit=args.limit, json_output=args.json)
    if args.center_command == "reviews":
        return center_cmd.reviews(target=args.target, limit=args.limit, json_output=args.json)
    if args.center_command == "templates":
        return center_cmd.templates(target=args.target, json_output=args.json)
    if args.center_command == "schema":
        return center_cmd.schema(target=args.target, json_output=args.json)
    if args.center_command == "readiness":
        if args.center_readiness_command == "plan":
            return center_cmd.readiness_plan(target=args.target, json_output=args.json)
        if args.center_readiness_command == "closeout":
            return center_cmd.readiness_closeout(
                target=args.target,
                status=args.status,
                reason=args.reason,
                waive_finding_ids=args.waive,
                json_output=args.json,
            )
        if args.center_readiness_command == "list":
            return center_cmd.readiness_list(target=args.target, limit=args.limit, json_output=args.json)
        if args.center_readiness_command == "show":
            return center_cmd.readiness_show(target=args.target, readiness_id=args.readiness_id, json_output=args.json)
        if args.center_readiness_command == "import-issues":
            return center_cmd.readiness_import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
        args._brigade_parser.error(f"unknown center readiness command: {args.center_readiness_command}")
        return 2
    if args.center_command == "report":
        if args.center_report_command == "plan":
            return center_cmd.report_plan(target=args.target, json_output=args.json)
        if args.center_report_command == "build":
            return center_cmd.report_build(target=args.target, json_output=args.json)
        if args.center_report_command == "list":
            return center_cmd.report_list(target=args.target, limit=args.limit, json_output=args.json)
        if args.center_report_command == "show":
            return center_cmd.report_show(target=args.target, report_id=args.report_id, json_output=args.json)
        if args.center_report_command == "archive":
            return center_cmd.report_archive(target=args.target, report_id=args.report_id, json_output=args.json)
        if args.center_report_command == "review":
            return center_cmd.report_review(target=args.target, report_id=args.report_id, json_output=args.json)
        if args.center_report_command == "compare":
            return center_cmd.report_compare(target=args.target, report_id=args.report_id, json_output=args.json)
        if args.center_report_command == "diff":
            return center_cmd.report_diff(
                target=args.target,
                base_report_id=args.base_report_id,
                compare_report_id=args.compare_report_id,
                record=args.record,
                json_output=args.json,
            )
        if args.center_report_command == "closeout":
            return center_cmd.report_closeout(
                target=args.target,
                report_id=args.report_id,
                status=args.status,
                reason=args.reason,
                deferred_item_ids=args.defer_item,
                json_output=args.json,
            )
        args._brigade_parser.error(f"unknown center report command: {args.center_report_command}")
        return 2
    if args.center_command == "actions":
        if args.center_actions_command == "plan":
            return center_cmd.actions_plan(target=args.target, report_id=args.report_id, json_output=args.json)
        if args.center_actions_command == "build":
            return center_cmd.actions_build(
                target=args.target,
                report_id=args.report_id,
                allow_unreviewed=args.allow_unreviewed,
                json_output=args.json,
            )
        if args.center_actions_command == "list":
            return center_cmd.actions_list(target=args.target, limit=args.limit, json_output=args.json)
        if args.center_actions_command == "show":
            return center_cmd.actions_show(target=args.target, action_id=args.action_id, json_output=args.json)
        if args.center_actions_command == "doctor":
            return center_cmd.actions_doctor(target=args.target, json_output=args.json)
        if args.center_actions_command == "import-issues":
            return center_cmd.actions_import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
        if args.center_actions_command == "start":
            return center_cmd.actions_start(target=args.target, action_id=args.action_id, json_output=args.json)
        if args.center_actions_command == "done":
            return center_cmd.actions_done(target=args.target, action_id=args.action_id, json_output=args.json)
        if args.center_actions_command == "defer":
            return center_cmd.actions_defer(
                target=args.target, action_id=args.action_id, reason=args.reason, json_output=args.json
            )
        if args.center_actions_command == "archive":
            return center_cmd.actions_archive_completed(target=args.target, json_output=args.json)
        args._brigade_parser.error(f"unknown center actions command: {args.center_actions_command}")
        return 2
    args._brigade_parser.error(f"unknown center command: {args.center_command}")
    return 2
