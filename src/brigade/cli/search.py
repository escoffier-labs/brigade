"""brigade search command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p_search = sub.add_parser(
        "search",
        help="Inspect and plan GraphTrail + code-search station health.",
    )
    search_sub = p_search.add_subparsers(dest="search_command", metavar="<search-command>")
    search_sub.required = True

    p_status = search_sub.add_parser("status", help="Show GraphTrail and code-search install health.")
    p_status.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_doctor = search_sub.add_parser(
        "doctor",
        help="Advisory search health. Exits 1 on fail/incomplete/timeout.",
    )
    p_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_sync = search_sub.add_parser("sync", help="Plan graphtrail/code-search setup without executing it.")
    sync_sub = p_sync.add_subparsers(dest="search_sync_command", metavar="<sync-command>")
    sync_sub.required = True
    p_sync_plan = sync_sub.add_parser("plan", help="Plan graphtrail sync and optional code-search serve.")
    p_sync_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace for plan paths and --write."
    )
    p_sync_plan.add_argument("--write", action="store_true", help="Write plan under .brigade/search/plans/.")
    p_sync_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_search.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import search_cmd

    if args.search_command == "status":
        return search_cmd.status(target=args.target, json_output=args.json)
    if args.search_command == "doctor":
        return search_cmd.doctor(target=args.target, json_output=args.json)
    if args.search_command == "sync":
        if args.search_sync_command == "plan":
            return search_cmd.sync_plan(target=args.target, write=args.write, json_output=args.json)
        args._brigade_parser.error(f"unknown search sync command: {args.search_sync_command}")
        return 2
    args._brigade_parser.error(f"unknown search command: {args.search_command}")
    return 2
