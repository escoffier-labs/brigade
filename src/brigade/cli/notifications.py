"""brigade notifications command group."""

from __future__ import annotations

import argparse
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    # notifications
    p_notifications = sub.add_parser("notifications", help="Inspect and plan operator notification wiring.")
    notifications_sub = p_notifications.add_subparsers(dest="notifications_command", metavar="<notifications-command>")
    notifications_sub.required = True
    p_notifications_status = notifications_sub.add_parser("status", help="Show agent-notify status.")
    p_notifications_status.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_notifications_status.add_argument("--profile", default=None, help="agent-notify profile to inspect.")
    p_notifications_status.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_notifications_setup = notifications_sub.add_parser("setup", help="Plan notification setup without applying it.")
    notifications_setup_sub = p_notifications_setup.add_subparsers(
        dest="notifications_setup_command", metavar="<setup-command>"
    )
    notifications_setup_sub.required = True
    p_notifications_setup_plan = notifications_setup_sub.add_parser(
        "plan", help="Print reviewed hook snippets and setup commands."
    )
    p_notifications_setup_plan.add_argument(
        "--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect."
    )
    p_notifications_setup_plan.add_argument(
        "--profile", default="operator", help="agent-notify profile to use in snippets."
    )
    p_notifications_setup_plan.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_notifications_event = notifications_sub.add_parser(
        "event", help="Plan or record explicit operator notification events."
    )
    notifications_event_sub = p_notifications_event.add_subparsers(
        dest="notifications_event_command", metavar="<event-command>"
    )
    notifications_event_sub.required = True
    notification_event_types = (
        "ci-green",
        "ci-failed",
        "handoff-waiting",
        "handoff-ingested",
        "release-ready",
        "operator-alert",
    )
    for event_command in ("plan", "record"):
        p_event = notifications_event_sub.add_parser(
            event_command, help=f"{event_command.title()} an explicit notification event."
        )
        p_event.add_argument("--target", "-t", type=Path, default=Path("."), help="Repo or workspace to inspect.")
        p_event.add_argument("--type", choices=notification_event_types, required=True, help="Notification event type.")
        p_event.add_argument("--title", required=True, help="Safe notification title.")
        p_event.add_argument("--message", required=True, help="Safe notification message.")
        p_event.add_argument(
            "--level", choices=["info", "success", "warning", "error"], default="info", help="Safe event level."
        )
        p_event.add_argument("--profile", default=None, help="agent-notify profile to use.")
        p_event.add_argument("--source", default=None, help="Safe source label, such as ci or handoff.")
        p_event.add_argument(
            "--no-evidence",
            action="store_true",
            help="Do not attach bounded local receipt summaries to the payload.",
        )
        if event_command == "record":
            p_event.add_argument("--send", action="store_true", help="Also invoke agent-notify explicitly.")
        p_event.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    p_notifications.set_defaults(func=dispatch)


def dispatch(args) -> int:
    from .. import notifications_cmd

    if args.notifications_command == "status":
        return notifications_cmd.status(target=args.target, profile=args.profile, json_output=args.json)
    if args.notifications_command == "setup":
        if args.notifications_setup_command == "plan":
            return notifications_cmd.setup_plan(target=args.target, profile=args.profile, json_output=args.json)
        args._brigade_parser.error(f"unknown notifications setup command: {args.notifications_setup_command}")
        return 2
    if args.notifications_command == "event":
        if args.notifications_event_command == "plan":
            return notifications_cmd.event_plan(
                target=args.target,
                event_type=args.type,
                title=args.title,
                message=args.message,
                level=args.level,
                profile=args.profile,
                source=args.source,
                evidence=not args.no_evidence,
                json_output=args.json,
            )
        if args.notifications_event_command == "record":
            return notifications_cmd.event_record(
                target=args.target,
                event_type=args.type,
                title=args.title,
                message=args.message,
                level=args.level,
                profile=args.profile,
                source=args.source,
                send=args.send,
                evidence=not args.no_evidence,
                json_output=args.json,
            )
        args._brigade_parser.error(f"unknown notifications event command: {args.notifications_event_command}")
        return 2
    args._brigade_parser.error(f"unknown notifications command: {args.notifications_command}")
    return 2
