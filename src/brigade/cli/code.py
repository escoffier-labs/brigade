"""Executable GraphTrail facade commands."""

from __future__ import annotations

import argparse


def register(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("code", help="Run GraphTrail graph commands through Brigade.")
    commands = parser.add_subparsers(dest="code_command", metavar="<code-command>")
    commands.required = True
    for verb in ("sync", "context", "impact"):
        command = commands.add_parser(verb, help=f"Run `graphtrail {verb}`.")
        command.add_argument("engine_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    parser.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import code_cmd

    return code_cmd.run(args.code_command, args.engine_args)
