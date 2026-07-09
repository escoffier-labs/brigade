"""brigade pantry command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # pantry
    p_pantry = sub.add_parser("pantry", help="Inspect and plan agentpantry session-auth sync.")
    pantry_sub = p_pantry.add_subparsers(dest="pantry_command", metavar="<pantry-command>")
    pantry_sub.required = True
    p_pantry_status = pantry_sub.add_parser("status", help="Show agentpantry status and advisory health.")
    p_pantry_status.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_pantry_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_pantry_doctor = pantry_sub.add_parser(
        "doctor",
        help="Advisory pantry health (install, config, agentpantry doctor). Exits 1 only on fail_count.",
    )
    p_pantry_doctor.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
    p_pantry_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_expiry = pantry_sub.add_parser(
        "expiry-alert", help="Check near-expiry Agent Pantry sessions and optionally notify."
    )
    p_expiry.add_argument("--expiry-days", type=int, default=14, help="Near-expiry window in days.")
    p_expiry.add_argument("--profile", default="agent-stop", help="agent-notify profile to use when --send is passed.")
    p_expiry.add_argument("--send", action="store_true", help="Invoke agent-notify when near-expiry sessions exist.")
    p_expiry.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_pantry_setup = pantry_sub.add_parser("setup", help="Plan source or sink setup without applying it.")
    pantry_setup_sub = p_pantry_setup.add_subparsers(dest="pantry_setup_command", metavar="<setup-command>")
    pantry_setup_sub.required = True
    p_pantry_setup_plan = pantry_setup_sub.add_parser("plan", help="Plan agentpantry source or sink setup.")
    p_pantry_setup_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update when --write is used."
    )
    p_pantry_setup_plan.add_argument(
        "--role", choices=["source", "sink"], required=True, help="agentpantry role to plan."
    )
    p_pantry_setup_plan.add_argument(
        "--peer", default="127.0.0.1:8787", help="Peer/bind address to document in the plan."
    )
    p_pantry_setup_plan.add_argument(
        "--config-path", default="~/.config/agentpantry/config.toml", help="agentpantry config path to document."
    )
    p_pantry_setup_plan.add_argument(
        "--key-path", default="~/.config/agentpantry/psk.key", help="agentpantry PSK path to document."
    )
    p_pantry_setup_plan.add_argument(
        "--write", action="store_true", help="Write a local reviewed plan under .brigade/pantry/plans/."
    )
    p_pantry_setup_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_pantry_service = pantry_sub.add_parser("service", help="Plan service setup without starting services.")
    pantry_service_sub = p_pantry_service.add_subparsers(dest="pantry_service_command", metavar="<service-command>")
    pantry_service_sub.required = True
    p_pantry_service_plan = pantry_service_sub.add_parser("plan", help="Plan agentpantry service installation.")
    p_pantry_service_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to update when --write is used."
    )
    p_pantry_service_plan.add_argument(
        "--role", choices=["source", "sink"], required=True, help="agentpantry role to document."
    )
    p_pantry_service_plan.add_argument(
        "--config-path", default="~/.config/agentpantry/config.toml", help="agentpantry config path to document."
    )
    p_pantry_service_plan.add_argument(
        "--write", action="store_true", help="Write a local reviewed plan under .brigade/pantry/plans/."
    )
    p_pantry_service_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_pantry.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import pantry_cmd

    if args.pantry_command == "status":
        return pantry_cmd.status(target=args.target, json_output=args.json)
    if args.pantry_command == "doctor":
        return pantry_cmd.doctor(target=args.target, json_output=args.json)
    if args.pantry_command == "expiry-alert":
        return pantry_cmd.expiry_alert(
            expiry_days=args.expiry_days,
            profile=args.profile,
            send=args.send,
            json_output=args.json,
        )
    if args.pantry_command == "setup":
        if args.pantry_setup_command == "plan":
            return pantry_cmd.setup_plan(
                target=args.target,
                role=args.role,
                peer=args.peer,
                config_path=args.config_path,
                key_path=args.key_path,
                write=args.write,
                json_output=args.json,
            )
        args._brigade_parser.error(f"unknown pantry setup command: {args.pantry_setup_command}")
        return 2
    if args.pantry_command == "service":
        if args.pantry_service_command == "plan":
            return pantry_cmd.service_plan(
                target=args.target,
                role=args.role,
                config_path=args.config_path,
                write=args.write,
                json_output=args.json,
            )
        args._brigade_parser.error(f"unknown pantry service command: {args.pantry_service_command}")
        return 2
    args._brigade_parser.error(f"unknown pantry command: {args.pantry_command}")
    return 2
