"""brigade runs command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # runs
    p_runs = sub.add_parser("runs", help="Inspect Brigade run artifacts.")
    runs_sub = p_runs.add_subparsers(dest="runs_command", metavar="<runs-command>")
    runs_sub.required = True
    p_runs_list = runs_sub.add_parser("list", help="List recent Brigade run directories.")
    p_runs_list.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be listed.",
    )
    p_runs_list.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_list.add_argument("--limit", type=int, default=10, help="Maximum number of runs to show.")
    p_runs_latest = runs_sub.add_parser("latest", help="Show the most recent Brigade run.")
    p_runs_latest.add_argument(
        "--cwd",
        type=Path,
        default=Path("."),
        help="Workspace whose default .brigade/runs directory should be inspected.",
    )
    p_runs_latest.add_argument(
        "--runs-dir",
        type=Path,
        default=None,
        help="Explicit runs directory. Defaults to .brigade/runs under --cwd.",
    )
    p_runs_show = runs_sub.add_parser("show", help="Show a readable summary of one run directory.")
    p_runs_show.add_argument("run_dir", type=Path, help="Path to a Brigade run artifact directory.")
    p_runs.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import runs_cmd

    if args.runs_command == "list":
        return runs_cmd.list_runs(cwd=args.cwd, runs_dir=args.runs_dir, limit=args.limit)
    if args.runs_command == "latest":
        return runs_cmd.show_latest(cwd=args.cwd, runs_dir=args.runs_dir)
    if args.runs_command == "show":
        return runs_cmd.show(args.run_dir)
    args._brigade_parser.error(f"unknown runs command: {args.runs_command}")
    return 2
