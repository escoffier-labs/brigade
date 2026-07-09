"""brigade tokens command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    p_tokens = sub.add_parser(
        "tokens",
        help="Inspect and plan Token Glace + usage-tracker station health.",
    )
    tokens_sub = p_tokens.add_subparsers(dest="tokens_command", metavar="<tokens-command>")
    tokens_sub.required = True

    p_status = tokens_sub.add_parser("status", help="Show Token Glace and usage-tracker install health.")
    p_status.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_doctor = tokens_sub.add_parser(
        "doctor",
        help="Advisory tokens health. Exits 1 on fail/incomplete/timeout.",
    )
    p_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_wire = tokens_sub.add_parser("wire", help="Plan token-glace install without executing it.")
    wire_sub = p_wire.add_subparsers(dest="tokens_wire_command", metavar="<wire-command>")
    wire_sub.required = True
    p_wire_plan = wire_sub.add_parser("plan", help="Plan token-glace host hooks and optional usage export.")
    p_wire_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace for plan paths and --write."
    )
    p_wire_plan.add_argument("--write", action="store_true", help="Write plan under .brigade/tokens/plans/.")
    p_wire_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    p_tokens.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import tokens_cmd

    if args.tokens_command == "status":
        return tokens_cmd.status(target=args.target, json_output=args.json)
    if args.tokens_command == "doctor":
        return tokens_cmd.doctor(target=args.target, json_output=args.json)
    if args.tokens_command == "wire":
        if args.tokens_wire_command == "plan":
            return tokens_cmd.wire_plan(target=args.target, write=args.write, json_output=args.json)
        args._brigade_parser.error(f"unknown tokens wire command: {args.tokens_wire_command}")
        return 2
    args._brigade_parser.error(f"unknown tokens command: {args.tokens_command}")
    return 2
