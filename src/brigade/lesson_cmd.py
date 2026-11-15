"""CLI for scheduled lesson review and its off switch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import lesson_activity as activity, lesson_review, localio


def register(sub: argparse._SubParsersAction) -> None:
    group = sub.add_parser("lessons", help="Review idle work sessions automatically through a read-only seat.")
    commands = group.add_subparsers(dest="lesson_command", required=True)
    for name in ("run", "status", "enable", "disable"):
        command = commands.add_parser(name, help=f"{name.capitalize()} local lesson review.")
        command.add_argument("--target", "-t", type=Path, default=Path("."))
        command.add_argument("--json", action="store_true")


def dispatch(args: argparse.Namespace) -> int:
    target = args.target.expanduser().resolve()
    try:
        cfg = activity.config(target)
        if args.lesson_command in {"enable", "disable"}:
            cfg["enabled"] = args.lesson_command == "enable"
            localio.write_json(target / ".brigade/lesson-review.json", cfg)
            payload = {"status": "enabled" if cfg["enabled"] else "disabled"}
        elif args.lesson_command == "run":
            payload = lesson_review.run(target)
        else:
            state = activity.load(target)
            payload = {
                "status": "enabled" if cfg["enabled"] else "disabled",
                "pending_calls": state.get("calls", 0) - state.get("reviewed_calls", 0),
                "threshold": cfg["threshold"],
                "idle_seconds": cfg["idle_seconds"],
                "seat": cfg["seat"],
            }
    except (OSError, ValueError) as exc:
        payload = {"status": "rejected", "reason": str(exc)}
    print(json.dumps(payload, indent=2) if args.json else f"lesson review: {payload}")
    return 1 if payload["status"] in {"rejected", "retry"} else 0
