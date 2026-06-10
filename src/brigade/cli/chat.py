"""brigade chat command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # chat
    p_chat = sub.add_parser("chat", help="Inspect and import local chat surface exports.")
    chat_sub = p_chat.add_subparsers(dest="chat_command", metavar="<chat-command>")
    chat_sub.required = True
    p_chat_surfaces = chat_sub.add_parser("surfaces", help="Manage local chat surface export config.")
    surfaces_sub = p_chat_surfaces.add_subparsers(dest="surfaces_command", metavar="<surfaces-command>")
    surfaces_sub.required = True
    p_chat_surfaces_init = surfaces_sub.add_parser("init", help="Write a starter .brigade/chat-surfaces.toml.")
    p_chat_surfaces_init.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_chat_surfaces_init.add_argument("--force", action="store_true", help="Overwrite an existing config.")
    p_chat_surfaces_init.add_argument("--no-gitignore", action="store_true", help="Do not update managed .gitignore.")
    p_chat_surfaces_list = surfaces_sub.add_parser("list", help="List configured chat surfaces.")
    p_chat_surfaces_list.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_chat_surfaces_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_chat_surfaces_show = surfaces_sub.add_parser("show", help="Show one chat surface.")
    p_chat_surfaces_show.add_argument("surface_id", help="Surface id.")
    p_chat_surfaces_show.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_chat_surfaces_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_chat_surfaces_doctor = surfaces_sub.add_parser("doctor", help="Check chat surface config health.")
    p_chat_surfaces_doctor.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_chat_surfaces_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_chat_sweep = chat_sub.add_parser("sweep", help="Validate, ingest, or import local chat sweep exports.")
    sweep_sub = p_chat_sweep.add_subparsers(dest="sweep_command", metavar="<sweep-command>")
    sweep_sub.required = True
    p_chat_sweep_validate = sweep_sub.add_parser("validate", help="Validate a chat export finding file.")
    p_chat_sweep_validate.add_argument("input_path", type=Path, help="Chat export JSON or JSONL file.")
    p_chat_sweep_validate.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace context."
    )
    p_chat_sweep_validate.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_chat_sweep_ingest = sweep_sub.add_parser("ingest", help="Normalize one configured chat surface export.")
    p_chat_sweep_ingest.add_argument("surface_id", help="Surface id.")
    p_chat_sweep_ingest.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_chat_sweep_ingest.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_chat_sweep_import = sweep_sub.add_parser("import-issues", help="Import normalized chat sweep issues.")
    p_chat_sweep_import.add_argument("surface_id", help="Surface id.")
    p_chat_sweep_import.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update."
    )
    p_chat_sweep_import.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_chat.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import chat_cmd

    if args.chat_command == "surfaces":
        if args.surfaces_command == "init":
            return chat_cmd.surfaces_init(
                target=args.target,
                force=args.force,
                update_gitignore=not args.no_gitignore,
            )
        if args.surfaces_command == "list":
            return chat_cmd.surfaces_list(target=args.target, json_output=args.json)
        if args.surfaces_command == "show":
            return chat_cmd.surfaces_show(target=args.target, surface_id=args.surface_id, json_output=args.json)
        if args.surfaces_command == "doctor":
            return chat_cmd.surfaces_doctor(target=args.target, json_output=args.json)
        args._brigade_parser.error(f"unknown chat surfaces command: {args.surfaces_command}")
        return 2
    if args.chat_command == "sweep":
        if args.sweep_command == "validate":
            return chat_cmd.sweep_validate(target=args.target, input_path=args.input_path, json_output=args.json)
        if args.sweep_command == "ingest":
            return chat_cmd.sweep_ingest(target=args.target, surface_id=args.surface_id, json_output=args.json)
        if args.sweep_command == "import-issues":
            return chat_cmd.sweep_import_issues(target=args.target, surface_id=args.surface_id, json_output=args.json)
        args._brigade_parser.error(f"unknown chat sweep command: {args.sweep_command}")
        return 2
    args._brigade_parser.error(f"unknown chat command: {args.chat_command}")
    return 2
