"""brigade roster command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # roster
    p_roster = sub.add_parser("roster", help="Create and check aboyeur rosters.")
    roster_sub = p_roster.add_subparsers(dest="roster_command", metavar="<roster-command>")
    roster_sub.required = True
    p_roster_init = roster_sub.add_parser("init", help="Write a starter .brigade/roster.toml.")
    p_roster_init.add_argument("--target", "-t", type=Path, default=Path("."))
    p_roster_init.add_argument("--force", action="store_true", help="Overwrite an existing roster.")
    p_roster_init.add_argument(
        "--ollama-model",
        default=None,
        help="Local researcher model for the starter roster (default: a small model; "
        "brigade never auto-pulls, so pick one you have already pulled).",
    )
    p_roster_init.add_argument("--max-workers", type=int, default=4)
    p_roster_init.add_argument(
        "--review-model",
        default=None,
        help="Add a reviewer seat pinned to this model (e.g. gpt-5.3-codex-spark) so review "
        "independence is structural: the reviewer runs a different model than the coder.",
    )
    p_roster_doctor = roster_sub.add_parser("doctor", help="Validate roster syntax and installed CLIs.")
    p_roster_doctor.add_argument("--target", "-t", type=Path, default=Path("."))
    p_roster_doctor.add_argument(
        "--roster",
        type=Path,
        default=None,
        help="Path to roster.toml. Defaults to .brigade/roster.toml under --target.",
    )
    p_roster.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import roster_cmd

    if args.roster_command == "init":
        return roster_cmd.init(
            target=args.target,
            force=args.force,
            ollama_model=args.ollama_model if args.ollama_model is not None else roster_cmd.DEFAULT_OLLAMA_MODEL,
            max_workers=args.max_workers,
            review_model=args.review_model,
        )
    if args.roster_command == "doctor":
        return roster_cmd.doctor(target=args.target, roster_path=args.roster)
    args._brigade_parser.error(f"unknown roster command: {args.roster_command}")
    return 2
