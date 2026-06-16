"""brigade projects command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    from .. import projects_cmd

    # projects
    p_projects = sub.add_parser("projects", help="Audit local side-project consolidation decisions.")
    projects_sub = p_projects.add_subparsers(dest="projects_command", metavar="<projects-command>")
    projects_sub.required = True
    p_projects_audit = projects_sub.add_parser("audit", help="Audit configured project consolidation records.")
    p_projects_audit.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_projects_audit.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_doctor = projects_sub.add_parser("doctor", help="Check local project consolidation health.")
    p_projects_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_projects_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_import = projects_sub.add_parser(
        "import-issues", help="Import project consolidation issues into the work inbox."
    )
    p_projects_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_projects_import.add_argument("--dry-run", action="store_true", help="Report without writing imports.")
    p_projects_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_closeout = projects_sub.add_parser(
        "closeout", help="Write a reviewed project migration closeout receipt."
    )
    p_projects_closeout.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_projects_closeout.add_argument(
        "--status", choices=sorted(projects_cmd.PROJECT_CLOSEOUT_STATUSES), required=True, help="Closeout status."
    )
    p_projects_closeout.add_argument("--reason", required=True, help="Review reason.")
    p_projects_closeout.add_argument("--project-id", default=None, help="Close out one blocked project.")
    p_projects_closeout.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_closeouts = projects_sub.add_parser("closeouts", help="List project migration closeout receipts.")
    p_projects_closeouts.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_projects_closeouts.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_closeout_show = projects_sub.add_parser(
        "closeout-show", help="Show one project migration closeout receipt."
    )
    p_projects_closeout_show.add_argument("closeout_id", help="Closeout id or latest.")
    p_projects_closeout_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_projects_closeout_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_readiness = projects_sub.add_parser(
        "readiness", help="Plan and record project migration readiness receipts."
    )
    projects_readiness_sub = p_projects_readiness.add_subparsers(
        dest="projects_readiness_command", metavar="<projects-readiness-command>"
    )
    projects_readiness_sub.required = True
    p_projects_readiness_plan = projects_readiness_sub.add_parser(
        "plan", help="Plan project migration readiness without writing a receipt."
    )
    p_projects_readiness_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_projects_readiness_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_readiness_record = projects_readiness_sub.add_parser(
        "record", help="Write a local project migration readiness receipt."
    )
    p_projects_readiness_record.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_projects_readiness_record.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_readiness_list = projects_readiness_sub.add_parser(
        "list", help="List local project migration readiness receipts."
    )
    p_projects_readiness_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_projects_readiness_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects_readiness_show = projects_readiness_sub.add_parser(
        "show", help="Show a local project migration readiness receipt."
    )
    p_projects_readiness_show.add_argument("readiness_id", help="Readiness receipt id or latest.")
    p_projects_readiness_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_projects_readiness_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_projects.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import projects_cmd

    if args.projects_command == "audit":
        return projects_cmd.audit(target=args.target, json_output=args.json)
    if args.projects_command == "doctor":
        return projects_cmd.doctor(target=args.target, json_output=args.json)
    if args.projects_command == "import-issues":
        return projects_cmd.import_issues(target=args.target, dry_run=args.dry_run, json_output=args.json)
    if args.projects_command == "closeout":
        return projects_cmd.closeout(
            target=args.target,
            status=args.status,
            reason=args.reason,
            project_id=args.project_id,
            json_output=args.json,
        )
    if args.projects_command == "closeouts":
        return projects_cmd.closeouts(target=args.target, json_output=args.json)
    if args.projects_command == "closeout-show":
        return projects_cmd.closeout_show(target=args.target, closeout_id=args.closeout_id, json_output=args.json)
    if args.projects_command == "readiness":
        if args.projects_readiness_command == "plan":
            return projects_cmd.readiness_plan(target=args.target, json_output=args.json)
        if args.projects_readiness_command == "record":
            return projects_cmd.readiness_record(target=args.target, json_output=args.json)
        if args.projects_readiness_command == "list":
            return projects_cmd.readiness_list(target=args.target, json_output=args.json)
        if args.projects_readiness_command == "show":
            return projects_cmd.readiness_show(
                target=args.target, readiness_id=args.readiness_id, json_output=args.json
            )
        args._brigade_parser.error(f"unknown projects readiness command: {args.projects_readiness_command}")
        return 2
    args._brigade_parser.error(f"unknown projects command: {args.projects_command}")
    return 2
