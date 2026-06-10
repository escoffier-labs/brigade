"""brigade budgets command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # budgets
    p_budgets = sub.add_parser("budgets", help="Inspect Brigade's canonical operator budgets.")
    budgets_sub = p_budgets.add_subparsers(dest="budgets_command", metavar="<budgets-command>")
    budgets_sub.required = True
    p_budgets_show = budgets_sub.add_parser("show", help="Show canonical size and staleness budgets.")
    p_budgets_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_budgets_check = budgets_sub.add_parser("check", help="Check local bootstrap files against budgets.")
    p_budgets_check.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_budgets_check.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_budgets.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import budgets_cmd

    if args.budgets_command == "show":
        return budgets_cmd.show(json_output=args.json)
    if args.budgets_command == "check":
        return budgets_cmd.check(target=args.target, json_output=args.json)
    args._brigade_parser.error(f"unknown budgets command: {args.budgets_command}")
    return 2
