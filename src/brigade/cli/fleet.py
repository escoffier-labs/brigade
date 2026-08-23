"""brigade fleet command group (issue #1123): hub serve, status, spool flush."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


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
    p_serve.add_argument("--port", type=int, default=3774, help="TCP port (default 3774).")
    p_serve.add_argument("--db", type=Path, default=None, help="SQLite database path (default ~/.brigade/fleet-hub.db).")
    p_serve.add_argument("--token-file", type=Path, default=None, help="Bearer token file (else BRIGADE_FLEET_TOKEN env).")
    p_serve.set_defaults(func=_dispatch_serve)

    p_status = fleet_sub.add_parser("status", help="Show latest run state per node from the fleet hub.")
    p_status.add_argument("--all", action="store_true", help="Include terminal runs.")
    p_status.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    p_status.set_defaults(func=_dispatch_status)

    p_flush = fleet_sub.add_parser("flush", help="Re-POST locally spooled events to the fleet hub.")
    p_flush.set_defaults(func=_dispatch_flush)


def _dispatch_serve(args: argparse.Namespace) -> int:
    from .. import fleet_hub

    db_path = args.db if args.db is not None else Path("~/.brigade/fleet-hub.db").expanduser()
    return fleet_hub.run(host=args.host, port=args.port, db_path=db_path, token_file=args.token_file)


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
                str(run_row.get("ts") or "-"),
            ]
        )
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths)))
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths)))
    if not rows:
        print("(no active fleet runs)")
    return 0


def _dispatch_flush(args: argparse.Namespace) -> int:
    from .. import fleet_client

    node_id = fleet_client.resolve_node_id()
    flushed = fleet_client.flush_spool(node_id)
    print(f"flushed {flushed} spooled event(s) for node {node_id}")
    return 0
