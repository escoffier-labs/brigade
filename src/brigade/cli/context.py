"""brigade context command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # context
    p_context = sub.add_parser("context", help="Plan and build local context engineering packs.")
    context_sub = p_context.add_subparsers(dest="context_command", metavar="<context-command>")
    context_sub.required = True
    for name in ("plan", "build"):
        p_context_action = context_sub.add_parser(name, help=f"{name.title()} a local context pack.")
        p_context_action.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace.")
        p_context_action.add_argument("--kind", choices=["task", "repo", "release", "tool-use"], default="repo")
        p_context_action.add_argument("--task-id", default=None, help="Task id for task context packs.")
        p_context_action.add_argument("--tool-id", default=None, help="Tool id for tool-use context packs.")
        p_context_action.add_argument("--release-id", default=None, help="Release candidate or readiness id.")
        p_context_action.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_context_list = context_sub.add_parser("list", help="List local context packs.")
    p_context_list.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_context_list.add_argument("--limit", type=int, default=20, help="Maximum packs to list.")
    p_context_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_context_show = context_sub.add_parser("show", help="Show one local context pack.")
    p_context_show.add_argument("pack_id", help="Pack id or unique prefix.")
    p_context_show.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_context_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_context_archive = context_sub.add_parser("archive", help="Archive one local context pack.")
    p_context_archive.add_argument("pack_id", help="Pack id or unique prefix.")
    p_context_archive.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_context_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_context_sync = context_sub.add_parser("sync", help="Plan context pack sync into configured harness destinations.")
    p_context_sync.add_argument(
        "sync_command", choices=["plan", "record"], help="Plan or record a read-only sync plan."
    )
    p_context_sync.add_argument("pack_id", nargs="?", default="latest", help="Pack id, unique prefix, or latest.")
    p_context_sync.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_context_sync.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_context_doctor = context_sub.add_parser("doctor", help="Check context pack freshness and references.")
    p_context_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_context_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_context_import = context_sub.add_parser("import-issues", help="Import context pack issues into the work inbox.")
    p_context_import.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update.")
    p_context_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_context.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import context_cmd

    if args.context_command == "plan":
        return context_cmd.plan(
            target=args.target,
            kind=args.kind,
            task_id=args.task_id,
            tool_id=args.tool_id,
            release_id=args.release_id,
            json_output=args.json,
        )
    if args.context_command == "build":
        return context_cmd.build(
            target=args.target,
            kind=args.kind,
            task_id=args.task_id,
            tool_id=args.tool_id,
            release_id=args.release_id,
            json_output=args.json,
        )
    if args.context_command == "list":
        return context_cmd.list_packs(target=args.target, limit=args.limit, json_output=args.json)
    if args.context_command == "show":
        return context_cmd.show(target=args.target, pack_id=args.pack_id, json_output=args.json)
    if args.context_command == "archive":
        return context_cmd.archive(target=args.target, pack_id=args.pack_id, json_output=args.json)
    if args.context_command == "sync":
        if args.sync_command == "plan":
            return context_cmd.sync_plan(target=args.target, pack_id=args.pack_id, json_output=args.json)
        if args.sync_command == "record":
            return context_cmd.sync_record(target=args.target, pack_id=args.pack_id, json_output=args.json)
    if args.context_command == "doctor":
        return context_cmd.doctor(target=args.target, json_output=args.json)
    if args.context_command == "import-issues":
        return context_cmd.import_issues(target=args.target, json_output=args.json)
    args._brigade_parser.error(f"unknown context command: {args.context_command}")
    return 2
