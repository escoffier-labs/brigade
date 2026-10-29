"""``brigade fleet projects sync``: publish a bounded GitHub Projects board snapshot to the Hub."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone


def register(sub: argparse._SubParsersAction) -> None:
    group = sub.add_parser("projects", help="Publish a GitHub Projects board snapshot for the Hub Projects page.")
    commands = group.add_subparsers(dest="fleet_projects_command", metavar="<projects-command>")
    commands.required = True
    sync = commands.add_parser(
        "sync",
        help="Read one GitHub Projects board (read-only GraphQL, one point per 100 items) and post it to the Hub.",
    )
    sync.add_argument("--owner", default="escoffier-labs", help="Organization that owns the board.")
    sync.add_argument("--number", type=int, default=1, help="Project number within the organization.")
    sync.add_argument("--dry-run", action="store_true", dest="dry_run", help="Query GitHub and print; send nothing.")
    sync.add_argument("--force", action="store_true", help="Skip the five-minute minimum interval between syncs.")
    sync.add_argument("--json", action="store_true", help="Emit JSON (the full snapshot with --dry-run).")
    sync.set_defaults(func=_dispatch_sync)


def _message(exc: Exception) -> str:
    return "".join(ch for ch in str(exc) if ch.isprintable())[:300] or exc.__class__.__name__


def _dispatch_sync(args: argparse.Namespace) -> int:
    from .. import fleet_client, fleet_client_policy, fleet_project_board_collect as collector

    home = fleet_client.brigade_home()
    now = datetime.now(timezone.utc)
    try:
        if not args.dry_run and not args.force:
            wait = collector.too_soon(home, args.owner, args.number, now)
            if wait is not None:
                print(
                    f"skipped: last successful sync was under 5 minutes ago; retry in {int(wait.total_seconds())}s "
                    "or pass --force",
                    file=sys.stderr,
                )
                return 0
        snapshot = collector.collect(args.owner, args.number, now=now)
        if args.dry_run:
            print(
                json.dumps(snapshot, indent=2, sort_keys=True)
                if args.json
                else f"projects dry run: items={len(snapshot['items'])} complete={snapshot['complete']} "
                f"cost={snapshot.get('rate_limit', {}).get('cost', 0)}"
            )
            return 0
        hub, token = fleet_client_policy._hub_and_token(admin=False)
        status, response = fleet_client_policy._request(
            "POST", "/projects/board", token=token, hub_url=hub, body=snapshot
        )
        if status != 200:
            raise fleet_client_policy._classify(status, response, f"hub returned HTTP {status}")
        collector.record_success(home, args.owner, args.number, now)
    except Exception as exc:
        print(f"error: {_message(exc)}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(response, sort_keys=True))
    else:
        print(
            f"projects sync sent: items={len(snapshot['items'])} complete={snapshot['complete']} "
            f"cost={snapshot.get('rate_limit', {}).get('cost', 0)}"
        )
    return 0
