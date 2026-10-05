"""CLI dashboard enrollment and browser session administration."""

import argparse
import json
import sys
from typing import Any

from .. import fleet_client_enrollment as client
from .fleet import _safe_table_cell


def register(fleet_sub: Any, sessions_parser: argparse.ArgumentParser) -> None:
    parser = fleet_sub.add_parser("enroll", help="Print a five-minute read-only dashboard enrollment URL (admin only).")
    parser.add_argument("--label", help="Device label, at most 64 characters.")
    parser.add_argument("--base-url", help="Browser-facing HTTPS origin or loopback HTTP origin.")
    parser.set_defaults(func=dispatch_enroll)
    sessions_parser.add_argument(
        "--dashboard", action="store_true", help="List read-only browser sessions (admin only)."
    )
    sessions_parser.add_argument(
        "--revoke", metavar="SESSION_ID", help="Revoke one browser session, requires --dashboard."
    )
    sessions_parser.add_argument("--after", metavar="SESSION_ID", help="Dashboard page cursor, requires --dashboard.")


def dispatch_enroll(args: argparse.Namespace) -> int:
    try:
        url = client.enroll(base_url=args.base_url, label=args.label)
    except client.DashboardClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(url)
    return 0


def dispatch_sessions(args: argparse.Namespace) -> int | None:
    dashboard = bool(getattr(args, "dashboard", False))
    revoke = getattr(args, "revoke", None)
    after = getattr(args, "after", None)
    if (revoke is not None or after is not None) and not dashboard:
        print("error: --revoke and --after require --dashboard for browser sessions", file=sys.stderr)
        return 2
    if not dashboard:
        return None
    if revoke is not None and (args.all or after is not None):
        print("error: browser session revocation cannot combine --all or --after", file=sys.stderr)
        return 2
    try:
        result = client.revoke(revoke) if revoke is not None else client.sessions(include_all=args.all, after=after)
    except client.DashboardClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif revoke is not None:
        print(f"revoked browser session {_safe_table_cell(result['session_id'])}")
    else:
        for session in result["sessions"]:
            print(
                f"{_safe_table_cell(session['session_id'])}  {_safe_table_cell(session['label'] or '-')}  "
                f"{_safe_table_cell(session['scope'])}  expires {_safe_table_cell(session['expires_at'])}  "
                f"revoked {_safe_table_cell(session['revoked_at'] or '-')}"
            )
        if not result["sessions"]:
            print("(no dashboard sessions)")
        if result["next_after"]:
            print(
                f"next page: brigade fleet sessions --dashboard{' --all' if args.all else ''} "
                f"--after {_safe_table_cell(result['next_after'])}"
            )
    return 0
