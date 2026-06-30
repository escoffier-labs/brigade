"""brigade profiles command group."""

from __future__ import annotations

import argparse


def register(sub: argparse._SubParsersAction) -> None:
    p_profiles = sub.add_parser("profiles", help="Inspect built-in station profiles.")
    profiles_sub = p_profiles.add_subparsers(dest="profiles_command", metavar="<profiles-command>")
    profiles_sub.required = True
    p_profiles_list = profiles_sub.add_parser("list", help="List built-in station profiles.")
    p_profiles_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_profiles_show = profiles_sub.add_parser("show", help="Show one built-in station profile.")
    p_profiles_show.add_argument("profile", help="Profile name or alias.")
    p_profiles_show.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_profiles.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import profiles_cmd

    if args.profiles_command == "list":
        return profiles_cmd.list_profiles(json_output=args.json)
    if args.profiles_command == "show":
        return profiles_cmd.show_profile(args.profile, json_output=args.json)
    args._brigade_parser.error(f"unknown profiles command: {args.profiles_command}")
    return 2
