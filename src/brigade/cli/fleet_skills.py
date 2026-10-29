"""``brigade fleet skills report``: publish a bounded skills summary to the Hub."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def register(sub: argparse._SubParsersAction) -> None:
    group = sub.add_parser("skills", help="Publish this machine's skills summary for the Hub skills review.")
    commands = group.add_subparsers(dest="fleet_skills_command", metavar="<skills-command>")
    commands.required = True
    report = commands.add_parser(
        "report",
        help="Post copy status, conflicts, and outcome scores. No skill contents, credentials, or paths are sent.",
    )
    report.add_argument("--target", type=Path, default=Path.cwd(), help="Workspace whose skills are summarised.")
    report.add_argument("--dry-run", action="store_true", dest="dry_run", help="Build the payload without sending it.")
    report.add_argument("--json", action="store_true", help="Emit JSON.")
    report.set_defaults(func=_dispatch_report)


def _dispatch_report(args: argparse.Namespace) -> int:
    from .. import fleet_client_policy, fleet_skills_report

    try:
        payload = fleet_skills_report.build_report(args.target.expanduser().resolve())
        if args.dry_run:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        hub, token = fleet_client_policy._hub_and_token(admin=False)
        status, response = fleet_client_policy._request(
            "POST", "/skills/report", token=token, hub_url=hub, body=payload
        )
        if status != 200:
            raise fleet_client_policy._classify(status, response, f"hub returned HTTP {status}")
    except Exception as exc:
        print(f"error: {fleet_client_policy_message(exc)}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(response, sort_keys=True))
    else:
        print(f"skills report sent: copies={len(payload['copies'])} scores={len(payload['scores'])}")
    return 0


def fleet_client_policy_message(exc: Exception) -> str:
    return "".join(ch for ch in str(exc) if ch.isprintable())[:300] or exc.__class__.__name__
