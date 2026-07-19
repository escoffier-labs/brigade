"""brigade version command group."""

from __future__ import annotations

import argparse


def register(sub: argparse._SubParsersAction) -> None:
    p_version = sub.add_parser("version", help="Show Brigade version.")
    p_version.add_argument(
        "--components",
        action="store_true",
        help="Report managed native component installation state.",
    )
    p_version.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    p_version.set_defaults(func=dispatch)


def dispatch(args) -> int:
    if args.json and not args.components:
        args._brigade_parser.error("--json requires --components")
    from .. import version_cmd

    return version_cmd.run(components=args.components, json_output=args.json)
