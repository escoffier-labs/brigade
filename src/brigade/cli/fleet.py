"""brigade fleet command group (issues #1123, #1125): hub serve, status, spool flush, claims."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_PORT = 3774
DEFAULT_DB_REL_PATH = Path(".brigade") / "fleet-hub.db"


def _format_age(ts: object, *, now: datetime | None = None) -> str:
    """Human-readable age of an ISO-8601 timestamp ("42s", "3m", "2h", "5d")."""
    if not isinstance(ts, str) or not ts:
        return "-"
    try:
        then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return "-"
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    seconds = max(0, int((current - then).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def register(sub: argparse._SubParsersAction) -> None:
    p_fleet = sub.add_parser(
        "fleet",
        help="Fleet sync: report run events to a central hub over Tailscale.",
    )
    fleet_sub = p_fleet.add_subparsers(dest="fleet_command", metavar="<fleet-command>")
    fleet_sub.required = True

    p_serve = fleet_sub.add_parser(
        "serve",
        help="Run the central fleet hub HTTP service on this host.",
    )
    p_serve.add_argument("--host", required=True, help="Interface to bind (required; never all interfaces by default).")
    p_serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"TCP port (default {DEFAULT_PORT}).")
    p_serve.add_argument(
        "--db", type=Path, default=None, help="SQLite database path (default ~/.brigade/fleet-hub.db)."
    )
    p_serve.add_argument(
        "--token-file", type=Path, default=None, help="Bearer token file (else BRIGADE_FLEET_TOKEN env)."
    )
    p_serve.set_defaults(func=_dispatch_serve)

    p_status = fleet_sub.add_parser("status", help="Show latest run state per node from the fleet hub.")
    p_status.add_argument("--all", action="store_true", help="Include terminal runs.")
    p_status.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    p_status.set_defaults(func=_dispatch_status)

    p_flush = fleet_sub.add_parser("flush", help="Re-POST locally spooled events to the fleet hub.")
    p_flush.set_defaults(func=_dispatch_flush)

    p_claims = fleet_sub.add_parser("claims", help="List active repo claims held on the fleet hub.")
    p_claims.add_argument("--all", action="store_true", help="Include expired claims.")
    p_claims.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    p_claims.set_defaults(func=_dispatch_claims)

    p_export = fleet_sub.add_parser(
        "export",
        help="Stream hub events or claims deterministically as JSONL or CSV.",
    )
    incremental = p_export.add_mutually_exclusive_group()
    incremental.add_argument(
        "--since", default=None, help="Only include events at or after this ISO-8601 timestamp (naive means UTC)."
    )
    incremental.add_argument(
        "--since-received",
        default=None,
        help="Only include events received by the hub at or after this ISO-8601 timestamp.",
    )
    p_export.add_argument("--format", choices=("jsonl", "csv"), default="jsonl", help="Output format (default jsonl).")
    p_export.add_argument(
        "--claims", action="store_true", help="Stream the current claims snapshot instead of the event history."
    )
    p_export.add_argument(
        "--db", type=Path, default=None, help="Hub SQLite database path (default ~/.brigade/fleet-hub.db)."
    )
    p_export.add_argument("--out", type=Path, default=None, help="Write to this file instead of stdout.")
    p_export.set_defaults(func=_dispatch_export)

    p_sink = fleet_sub.add_parser(
        "sink",
        help="Run one optional Dolt sink pass (a documented no-op unless [fleet.sink] enabled = true).",
    )
    p_sink.add_argument(
        "--db", type=Path, default=None, help="Hub SQLite database path (default ~/.brigade/fleet-hub.db)."
    )
    p_sink.set_defaults(func=_dispatch_sink)


def _dispatch_serve(args: argparse.Namespace) -> int:
    from .. import fleet_hub

    db_path = args.db if args.db is not None else (Path.home() / DEFAULT_DB_REL_PATH)
    return fleet_hub.run(host=args.host, port=args.port, db_path=db_path, token_file=args.token_file)


def _dispatch_export(args: argparse.Namespace) -> int:
    from .. import fleet_export

    return fleet_export.dispatch_export(args)


def _dispatch_sink(args: argparse.Namespace) -> int:
    from .. import fleet_export

    return fleet_export.dispatch_sink(args)


def _dispatch_status(args: argparse.Namespace) -> int:
    import json as _json

    from .. import fleet_client

    try:
        runs = fleet_client.fetch_status(include_all=args.all)
    except fleet_client.FleetClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(_json.dumps({"runs": runs}, indent=2, sort_keys=True))
        return 0
    headers = ("node", "repo", "run_id", "seat/harness", "state", "age")
    rows = []
    for run_row in runs:
        seat = "/".join(filter(None, [run_row.get("seat"), run_row.get("harness")])) or "-"
        rows.append(
            [
                str(run_row.get("node_id") or "-")[:12],
                str(run_row.get("repo") or "-"),
                str(run_row.get("run_id") or "-"),
                seat,
                str(run_row.get("state") or "-"),
                _format_age(run_row.get("ts")),
            ]
        )
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)))
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)))
    if not rows:
        print("(no active fleet runs)")
    return 0


def _dispatch_claims(args: argparse.Namespace) -> int:
    import json as _json

    from .. import fleet_client

    try:
        claims = fleet_client.fetch_claims(include_all=args.all)
    except fleet_client.FleetClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(_json.dumps({"claims": claims}, indent=2, sort_keys=True))
        return 0
    headers = ("target", "node", "conductor", "acquired", "expires")
    rows = []
    for claim in claims:
        expires = str(claim.get("expires_at") or "-")
        if claim.get("expired"):
            expires += " (expired)"
        rows.append(
            [
                str(claim.get("target") or "-"),
                str(claim.get("owner_node") or "-")[:12],
                str(claim.get("owner_conductor") or "-"),
                _format_age(claim.get("acquired_at")),
                expires,
            ]
        )
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)))
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)))
    if not rows:
        print("(no active fleet claims)")
    return 0


def _dispatch_flush(args: argparse.Namespace) -> int:
    from .. import fleet_client

    node_id = fleet_client.resolve_node_id()
    flushed = fleet_client.flush_spool(node_id)
    print(f"flushed {flushed} spooled event(s) for node {node_id}")
    return 0
