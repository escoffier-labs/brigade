"""``brigade fleet mcp report``: publish a bounded MCP servers summary to the Hub."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def register(parser: argparse.ArgumentParser) -> None:
    """Add ``report`` under the existing ``brigade fleet mcp`` command, which still serves MCP when given no subcommand."""
    commands = parser.add_subparsers(dest="fleet_mcp_command", metavar="<mcp-command>")
    commands.required = False
    report = commands.add_parser(
        "report",
        help="Post which MCP servers each harness has here. No env values, headers, arguments, URLs, or paths are sent.",
    )
    report.add_argument("--target", type=Path, default=Path.cwd(), help="Workspace whose MCP catalog is summarised.")
    report.add_argument("--dry-run", action="store_true", dest="dry_run", help="Build the payload without sending it.")
    report.add_argument(
        "--include-account-connectors",
        action="store_true",
        dest="include_account_connectors",
        help=(
            "Also run `claude mcp list` and report claude.ai account connectors (name, host, health only). "
            "Off by default; [fleet.mcp] include_account_connectors = true in ~/.brigade/fleet.toml enables it."
        ),
    )
    report.add_argument("--json", action="store_true", help="Emit JSON.")
    report.set_defaults(func=_dispatch_report)


def _dispatch_report(args: argparse.Namespace) -> int:
    from .. import fleet_client_policy, fleet_mcp_report

    try:
        payload = fleet_mcp_report.build_report(
            args.target.expanduser().resolve(),
            include_account_connectors=args.include_account_connectors or fleet_mcp_report.account_connectors_enabled(),
        )
        if args.dry_run:
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        hub, token = fleet_client_policy._hub_and_token(admin=False)
        status, response = fleet_client_policy._request("POST", "/mcp/report", token=token, hub_url=hub, body=payload)
        if status != 200:
            raise fleet_client_policy._classify(status, response, f"hub returned HTTP {status}")
    except Exception as exc:
        print(f"error: {_message(exc)}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(response, sort_keys=True))
    else:
        print(f"mcp report sent: servers={len(payload['servers'])} harnesses={len(payload['harnesses'])}")
    return 0


def _message(exc: Exception) -> str:
    return "".join(ch for ch in str(exc) if ch.isprintable())[:300] or exc.__class__.__name__
