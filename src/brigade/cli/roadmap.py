"""brigade roadmap command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # roadmap
    p_roadmap = sub.add_parser("roadmap", help="Inspect roadmap completion state.")
    roadmap_sub = p_roadmap.add_subparsers(dest="roadmap_command", metavar="<roadmap-command>")
    roadmap_sub.required = True
    p_roadmap_audit = roadmap_sub.add_parser("audit", help="Audit ROADMAP.md and documented command coverage.")
    p_roadmap_audit.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_roadmap_audit.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_roadmap_audit.add_argument(
        "--import-issues", action="store_true", help="Import roadmap audit issues into the work inbox."
    )
    p_roadmap_patterns = roadmap_sub.add_parser("patterns", help="Show neutral inspiration pattern coverage.")
    p_roadmap_patterns.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_roadmap_patterns.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_roadmap_archive = roadmap_sub.add_parser(
        "archive", help="Show archived roadmap items that left the active queue."
    )
    p_roadmap_archive.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_roadmap_archive.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_roadmap_commands = roadmap_sub.add_parser("commands", help="Show parser-derived command documentation coverage.")
    p_roadmap_commands.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_roadmap_commands.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_roadmap_commands.add_argument(
        "--write", action="store_true", help="Write docs/command-inventory.md from the CLI parser."
    )
    p_roadmap_commands.add_argument(
        "--check", action="store_true", help="Fail when docs/command-inventory.md is missing or stale."
    )
    p_roadmap.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import roadmap_cmd

    if args.roadmap_command == "audit":
        return roadmap_cmd.audit(target=args.target, json_output=args.json, import_issues=args.import_issues)
    if args.roadmap_command == "patterns":
        return roadmap_cmd.patterns(target=args.target, json_output=args.json)
    if args.roadmap_command == "archive":
        return roadmap_cmd.archive(target=args.target, json_output=args.json)
    if args.roadmap_command == "commands":
        return roadmap_cmd.commands(
            target=args.target, json_output=args.json, write_inventory=args.write, check_inventory=args.check
        )
    args._brigade_parser.error(f"unknown roadmap command: {args.roadmap_command}")
    return 2
