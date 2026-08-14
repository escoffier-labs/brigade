"""brigade projection command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("projection", help="Recover multi-file projection operations.")
    projection_sub = parser.add_subparsers(dest="projection_command", metavar="<projection-command>")
    projection_sub.required = True

    recover = projection_sub.add_parser(
        "recover",
        help="Restore destinations for an unfinished projection operation.",
    )
    recover.add_argument("operation_id", help="Operation id from the projection receipt or journal.")
    recover.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to operate on.")
    recover.add_argument(
        "--force",
        action="store_true",
        help="Restore even when a destination changed after the failed operation.",
    )
    recover.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    recover.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import projection_cmd

    if args.projection_command == "recover":
        return projection_cmd.recover(
            operation_id=args.operation_id,
            target=args.target,
            force=args.force,
            json_output=args.json,
        )
    args._brigade_parser.error(f"unknown projection command: {args.projection_command}")
    return 2
