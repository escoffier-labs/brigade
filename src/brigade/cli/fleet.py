"""brigade fleet command group (issues #1123, #1125, #1141): hub serve, status, spool flush, claims."""

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

    p_claims = fleet_sub.add_parser("claims", help="List active repo claims held on the fleet hub, or release one.")
    p_claims.add_argument("--all", action="store_true", help="Include expired claims.")
    p_claims.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    p_claims.add_argument(
        "--release",
        metavar="TARGET",
        default=None,
        help=(
            "Release the hub claim on TARGET (a claim left behind by a crashed run). "
            "Refused unless this node holds it; see --force."
        ),
    )
    p_claims.add_argument(
        "--force",
        action="store_true",
        help="With --release: release the claim even when another node holds it.",
    )
    p_claims.set_defaults(func=_dispatch_claims)


def _dispatch_serve(args: argparse.Namespace) -> int:
    from .. import fleet_hub

    db_path = args.db if args.db is not None else (Path.home() / DEFAULT_DB_REL_PATH)
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


def _release_claim(target: str, *, force: bool, as_json: bool) -> int:
    """``brigade fleet claims --release`` (issue #1141): free a claim whose
    holder token died with its run. Own-node claims only, unless ``force``."""
    import json as _json

    from .. import fleet_client

    node_id = fleet_client.resolve_node_id()
    decision = fleet_client.release_claim(target, node_id=node_id, force=force)
    if decision.reason == "no-hub":
        print("error: no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)", file=sys.stderr)
        return 1
    if decision.reason == "no-identity":
        print(
            f"error: no usable fleet node identity ({decision.detail}); run from a Brigade workspace "
            "with .brigade/node.toml (see `brigade node`)",
            file=sys.stderr,
        )
        return 1
    if decision.reason == "hub-unavailable":
        print(f"error: fleet hub claim release failed: {decision.detail}", file=sys.stderr)
        return 1
    if as_json:
        payload = {
            "target": target,
            "released": decision.granted,
            "forced": force,
            "node_id": node_id,
            "claim": decision.claim,
            "owner": decision.owner,
        }
        print(_json.dumps(payload, indent=2, sort_keys=True))
    if decision.granted:
        claim = decision.claim or {}
        if not as_json:
            print(
                f"released claim on {target!r} held by node {claim.get('owner_node') or '-'} "
                f"(conductor {claim.get('owner_conductor') or '-'})"
            )
        return 0
    if decision.reason == "held":
        owner = decision.owner or {}
        print(
            f"error: claim on {target!r} is held by node {owner.get('owner_node') or '-'}, not this node "
            f"({node_id}); pass --force to release it anyway",
            file=sys.stderr,
        )
        return 1
    if not as_json:
        print(f"no active claim on {target!r}")
    return 0


def _dispatch_claims(args: argparse.Namespace) -> int:
    import json as _json

    from .. import fleet_client

    if args.force and args.release is None:
        print("error: --force requires --release <target>", file=sys.stderr)
        return 2
    if args.release is not None:
        return _release_claim(args.release, force=args.force, as_json=args.json)
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
