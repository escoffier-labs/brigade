"""CI observation commands. Credentials come from existing node settings."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .. import fleet_ci_node as node, fleet_client


def register(sub: argparse._SubParsersAction) -> None:
    group = sub.add_parser("ci", help="Pull relay observations or reconcile CI through the node read budget.")
    commands = group.add_subparsers(dest="fleet_ci_command", required=True)
    pull = commands.add_parser("pull", help="Pull one bounded relay page and acknowledge it after Hub persistence.")
    pull.add_argument("--limit", type=int, default=100)
    reconcile = commands.add_parser(
        "reconcile", help="Repair hourly-due repository CI facts with bounded GET requests."
    )
    reconcile.add_argument("--target", type=Path, default=Path.cwd(), help="Workspace containing .brigade/repos.toml.")
    reconcile.add_argument(
        "--operator-plan",
        action="store_true",
        help="Use the configured admin bearer for private policy inventory discovery.",
    )
    for parser in (pull, reconcile):
        parser.add_argument("--state", type=Path, help="Private local cache path (default Brigade home / ci-node.db).")
        parser.add_argument("--json", action="store_true")
        parser.set_defaults(func=_dispatch)


def _dispatch(args: argparse.Namespace) -> int:
    try:
        settings = fleet_client.load_fleet_settings()
        if not settings["hub_url"] or not settings["node_token"]:
            raise ValueError("CI observation commands require a Hub URL and this node token")
        state = node.NodeState(args.state or fleet_client.brigade_home() / "ci-node.db")
        common = dict(hub_url=settings["hub_url"], node_token=settings["node_token"])
        if args.fleet_ci_command == "pull":
            relay_url = os.environ.get("BRIGADE_CI_RELAY_URL", "")
            relay_token = os.environ.get("BRIGADE_CI_RELAY_TOKEN", "")
            if not relay_url or not relay_token:
                raise ValueError("configure BRIGADE_CI_RELAY_URL and BRIGADE_CI_RELAY_TOKEN")
            result = node.pull(state, relay_url=relay_url, relay_token=relay_token, limit=args.limit, **common)
        else:
            admin = settings["admin_token"] if args.operator_plan else None
            if args.operator_plan and not admin:
                raise ValueError("operator plan discovery requires the configured admin bearer")
            result = node.reconcile(state, target=args.target, admin_token=admin, **common)
        print(
            json.dumps(result, sort_keys=True)
            if args.json
            else "CI: " + ", ".join(f"{k}={v}" for k, v in result.items())
        )
        return 1 if args.fleet_ci_command == "reconcile" and result.get("incomplete") else 0
    except sqlite3.Error:
        print("error: CI local storage unavailable", file=sys.stderr)
        return 2
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
