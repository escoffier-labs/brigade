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

    p_sync = search_sub.add_parser("sync", help="Run `graphtrail sync`, or use `sync plan` to preview setup.")
    # The opaque remainder lets `search sync <path>` remain a direct alias.
    # `sync plan` is parsed in dispatch to preserve its established spelling.
    p_sync.add_argument("engine_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    p_sync.set_defaults(_brigade_command_contract_leaf=True, _brigade_legacy_plan=True)

    for verb in ("context", "impact"):
        command = search_sub.add_parser(verb, help=f"Compatibility alias for `brigade code {verb}`.")
        command.add_argument("engine_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)

    p_search.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import search_cmd

    if args.search_command == "status":
        return search_cmd.status(target=args.target, json_output=args.json)
    if args.search_command == "doctor":
        return search_cmd.doctor(target=args.target, json_output=args.json)
    if args.search_command == "sync":
        if args.engine_args and args.engine_args[0] == "plan":
            plan_args = _sync_plan_parser().parse_args(args.engine_args[1:])
            return search_cmd.sync_plan(target=plan_args.target, write=plan_args.write, json_output=plan_args.json)
        return _run_code_alias("sync", args.engine_args)
    if args.search_command in ("context", "impact"):
        return _run_code_alias(args.search_command, args.engine_args)
    args._brigade_parser.error(f"unknown search command: {args.search_command}")
    return 2


def _sync_plan_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="brigade search sync plan")
    parser.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace for plan paths and --write."
    )
    parser.add_argument("--write", action="store_true", help="Write plan under .brigade/search/plans/.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    return parser


def _run_code_alias(verb: str, arguments: list[str]) -> int:
    from .. import code_cmd

    return code_cmd.run(verb, arguments)
