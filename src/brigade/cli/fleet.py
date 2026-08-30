"""brigade fleet command group (issues #1123, #1125, #1141, #1150, #1223): hub serve, status, spool flush, claims, nodes, preference."""

from __future__ import annotations

import argparse
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

DEFAULT_PORT = 3774
DEFAULT_DB_REL_PATH = Path(".brigade") / "fleet-hub.db"


def _safe_table_cell(value: object) -> str:
    """Render a table cell so stored non-printables cannot drive the terminal."""
    text = str(value)
    if text.isprintable():
        return text
    return "".join(ch if ch.isprintable() else ascii(ch)[1:-1] for ch in text)


def _format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def _format_remaining(expires_at: object, *, now: datetime | None = None) -> str:
    """Bounded remaining TTL from a Hub epoch or ISO timestamp."""
    current = now or datetime.now(timezone.utc)
    epoch: float | None = None
    if isinstance(expires_at, bool):
        return "-"
    if isinstance(expires_at, (int, float)):
        epoch = float(expires_at)
    elif isinstance(expires_at, str) and expires_at:
        try:
            stamp = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        except ValueError:
            try:
                epoch = float(expires_at)
            except ValueError:
                return "-"
        else:
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            epoch = stamp.timestamp()
    if epoch is None:
        return "-"
    if not math.isfinite(epoch):
        return "-"
    remaining = int(epoch - current.timestamp())
    if remaining <= 0:
        return "expired"
    return _format_duration(remaining)


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
    return _format_duration(seconds)


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
        "--token-file", type=Path, default=None, help="Admin bearer token file (else BRIGADE_FLEET_TOKEN env)."
    )
    p_serve.add_argument(
        "--allow-admin-writes",
        action="store_true",
        help=(
            "Let the admin token POST events and claims under any node_id (the pre-node-token shared-token "
            "mode). Off by default: writes need a node token from `brigade fleet nodes add`."
        ),
    )
    p_serve.add_argument(
        "--deck-config",
        type=Path,
        default=None,
        help="Command Deck JSON config (else BRIGADE_FLEET_DECK_CONFIG).",
    )
    p_serve.add_argument(
        "--trust-tailscale-identity",
        action="store_true",
        help=(
            "Trust the Tailscale-User-Login header added by a Tailscale Serve reverse proxy for "
            "dashboard routes only. Only safe when the hub is bound to a loopback interface and the "
            "proxy strips spoofed identity headers."
        ),
    )
    p_serve.set_defaults(func=_dispatch_serve)

    p_nodes = fleet_sub.add_parser(
        "nodes",
        help="Manage per-node hub credentials (admin token): enroll, list, or revoke a node.",
    )
    nodes_sub = p_nodes.add_subparsers(dest="nodes_command", metavar="<nodes-command>")
    nodes_sub.required = True
    p_nodes_add = nodes_sub.add_parser(
        "add",
        help="Enroll a node on the hub and print its token once (the hub stores only a hash).",
    )
    p_nodes_add.add_argument("node_id", help="The node identity (`brigade node --machine` on that machine prints it).")
    p_nodes_add.add_argument("--label", default=None, help="Free-text label, e.g. the hostname.")
    p_nodes_add.add_argument("--json", action="store_true", help="Emit JSON (includes the token) instead of text.")
    p_nodes_add.set_defaults(func=_dispatch_nodes_add)
    p_nodes_list = nodes_sub.add_parser("list", help="List enrolled nodes (revoked ones flagged).")
    p_nodes_list.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    p_nodes_list.set_defaults(func=_dispatch_nodes_list)
    p_nodes_revoke = nodes_sub.add_parser(
        "revoke", help="Revoke a node's token; `nodes add` it again to issue a new one."
    )
    p_nodes_revoke.add_argument("node_id", help="The node identity to revoke.")
    p_nodes_revoke.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p_nodes_revoke.set_defaults(func=_dispatch_nodes_revoke)

    p_status = fleet_sub.add_parser("status", help="Show latest run state per node from the fleet hub.")
    p_status.add_argument("--all", action="store_true", help="Include terminal runs.")
    p_status.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    p_status.set_defaults(func=_dispatch_status)

    p_sessions = fleet_sub.add_parser("sessions", help="Show interactive sessions from the fleet hub.")
    p_sessions.add_argument("--all", action="store_true", help="Include ended and expired sessions.")
    p_sessions.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    p_sessions.set_defaults(func=_dispatch_sessions)

    p_flush = fleet_sub.add_parser("flush", help="Re-POST locally spooled events to the fleet hub.")
    p_flush.set_defaults(func=_dispatch_flush)

    p_cloud = fleet_sub.add_parser("cloud", help="Read the fleet hub's sanitized cloud lease snapshot.")
    p_cloud.add_argument("--all", action="store_true", help="Include released and expired cloud leases.")
    p_cloud.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    p_cloud.set_defaults(func=_dispatch_cloud)

    p_models = fleet_sub.add_parser("models", help="Read the fleet hub's sanitized model policy.")
    p_models.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    p_models.set_defaults(func=_dispatch_models)
    models_sub = p_models.add_subparsers(dest="models_command", metavar="<models-command>")
    p_models_set = models_sub.add_parser("set", help="Set one provider/model/seat policy (admin token).")
    p_models_set.add_argument("provider", help="Lowercase provider identifier.")
    p_models_set.add_argument("model", help="Lowercase model identifier.")
    p_models_set.add_argument("seat", help="Lowercase seat identifier.")
    enabled = p_models_set.add_mutually_exclusive_group(required=True)
    enabled.add_argument("--enable", action="store_true", help="Enable this model policy.")
    enabled.add_argument("--disable", action="store_true", help="Disable this model policy.")
    p_models_set.add_argument("--limit", type=int, default=None, help="Optional concurrent-use limit (0 through 64).")
    p_models_set.add_argument("--notes", default=None, help="Optional operator note (stored as safe policy metadata).")
    p_models_set.add_argument("--reasoning", default=None, help="Exact reasoning value for the seat.")
    p_models_set.add_argument("--brigade-cli", default=None, dest="brigade_cli", help="Exact Brigade CLI binding.")
    p_models_set.add_argument(
        "--t3-instance-id", default=None, dest="t3_instance_id", help="Exact T3 instance binding."
    )
    p_models_set.add_argument(
        "--t3-service-tier", default=None, dest="t3_service_tier", help="Optional T3 service tier."
    )
    p_models_set.add_argument(
        "--expect-revision", type=int, required=True, dest="expect_revision", help="Current hub roster revision."
    )
    p_models_set.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p_models_set.set_defaults(func=_dispatch_models_set)
    p_models_admit = models_sub.add_parser("admit", help="Admit one consumer/seat from the hub roster.")
    p_models_admit.add_argument("--consumer", required=True, help="brigade-run or t3-fleet.")
    p_models_admit.add_argument("--request-id", required=True, help="Caller-generated idempotency key.")
    p_models_admit.add_argument("--phase", required=True, help="controller, target, or brigade-run.")
    p_models_admit.add_argument("--seat", default=None, help="Optional explicit seat instead of the consumer default.")
    p_models_admit.add_argument("--expect-revision", type=int, default=None, help="Bind replay to this revision.")
    p_models_admit.add_argument("--expect-digest", default=None, help="Bind replay to this roster digest.")
    p_models_admit.add_argument("--no-lkg", action="store_true", help="Disable last-known-good fallback.")
    p_models_admit.add_argument("--json", action="store_true", help="Emit JSON.")
    p_models_admit.set_defaults(func=_dispatch_models_admit)
    p_models_doctor = models_sub.add_parser("doctor", help="Read-only hub/cache/roster health for one consumer.")
    p_models_doctor.add_argument("--consumer", required=True, help="brigade-run or t3-fleet.")
    p_models_doctor.add_argument("--json", action="store_true", help="Emit JSON.")
    p_models_doctor.set_defaults(func=_dispatch_models_doctor)
    p_models_reconcile = models_sub.add_parser(
        "reconcile", help="Report local roster and project-default drift without mutating Hub or T3."
    )
    p_models_reconcile.add_argument("--consumer", required=True, help="brigade-run or t3-fleet.")
    p_models_reconcile.add_argument("--json", action="store_true", help="Emit JSON.")
    p_models_reconcile.set_defaults(func=_dispatch_models_reconcile)
    p_models_retire = models_sub.add_parser("retire", help="Retire a provider/family on the hub roster.")
    p_models_retire.add_argument("provider", help="Lowercase provider identifier.")
    p_models_retire.add_argument("family", help="Family root such as gpt-5.4.")
    p_models_retire.add_argument("--permanent", action="store_true", help="Mark the retirement as permanent.")
    p_models_retire.add_argument("--expect-revision", type=int, required=True, help="Current hub roster revision.")
    p_models_retire.add_argument("--reason-code", default="operator-retired", help="Bounded retirement reason.")
    p_models_retire.add_argument("--json", action="store_true", help="Emit JSON.")
    p_models_retire.set_defaults(func=_dispatch_models_retire)
    p_models_default = models_sub.add_parser("default", help="Mutate a consumer default seat.")
    default_sub = p_models_default.add_subparsers(dest="models_default_command", metavar="<default-command>")
    p_models_default_set = default_sub.add_parser("set", help="Set the hub consumer default seat.")
    p_models_default_set.add_argument("consumer", help="brigade-run or t3-fleet.")
    p_models_default_set.add_argument("seat", help="Enabled roster seat.")
    p_models_default_set.add_argument("--expect-revision", type=int, required=True, help="Current hub roster revision.")
    p_models_default_set.add_argument("--json", action="store_true", help="Emit JSON.")
    p_models_default_set.set_defaults(func=_dispatch_models_default_set)

    p_claims = fleet_sub.add_parser("claims", help="List active repo claims held on the fleet hub, or release one.")
    p_claims.add_argument("--all", action="store_true", help="Include expired claims.")
    p_claims.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    p_claims.add_argument(
        "--acquire",
        metavar="TARGET",
        default=None,
        help=(
            "Acquire TARGET for an external harness session. Records opaque --harness/--session labels "
            "(optional --role/--job) and prints the fencing holder once."
        ),
    )
    p_claims.add_argument(
        "--heartbeat",
        metavar="TARGET",
        default=None,
        help="Renew the live claim on TARGET. Requires the fencing --holder from --acquire.",
    )
    p_claims.add_argument(
        "--release",
        metavar="TARGET",
        default=None,
        help=(
            "Release the hub claim on TARGET, a claim key as listed by this command (a claim left behind by a "
            "crashed run). Refused unless this node holds it and the run that took it is verifiably dead: the "
            "claim's recorded run directory resolves to a workspace on this machine whose run lock has no live "
            "owner (with --path, that workspace must be the one given). See --force. With --holder, release "
            "the exact session that acquired the row instead of the operator recovery path."
        ),
    )
    p_claims.add_argument("--harness", default=None, help="Opaque harness label for --acquire or --outcome.")
    p_claims.add_argument("--role", default=None, help="Opaque role label for --acquire or --outcome.")
    p_claims.add_argument("--job", default=None, help="Opaque job label for --acquire or --outcome.")
    p_claims.add_argument("--session", default=None, help="Opaque session label for --acquire or --outcome.")
    p_claims.add_argument(
        "--holder",
        default=None,
        help="Fencing token printed by --acquire. Required for --heartbeat and session --release.",
    )
    p_claims.add_argument("--ttl", type=int, default=None, help="Claim TTL in seconds for --acquire or --heartbeat.")
    p_claims.add_argument(
        "--outcome",
        default=None,
        choices=("external.completed", "external.failed", "external.canceled"),
        help="With --release --holder: release first, then publish this terminal event when the holder matches.",
    )
    p_claims.add_argument(
        "--path",
        action="store_true",
        help=(
            "With --release: TARGET is a workspace directory, not a key; its name is the claim key, its node "
            "identity is used, and the claim must have been taken by a run in that workspace. The directory "
            "must be the workspace itself, not a directory inside one."
        ),
    )
    p_claims.add_argument(
        "--node",
        metavar="NODE_ID",
        default=None,
        help="With --release: release as this owner node identity; one other than this machine's needs --force.",
    )
    p_claims.add_argument(
        "--force",
        action="store_true",
        help=(
            "With --release: release even when another node holds it, a run owner is alive on the lock, or "
            "liveness cannot be verified."
        ),
    )
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
        "--include-expired",
        action="store_true",
        help="Include expired claims and mark them expired (requires --claims).",
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

    p_preference = fleet_sub.add_parser(
        "preference",
        help="Get, set, or pull the fleet run preference overlay (#1223).",
    )
    preference_sub = p_preference.add_subparsers(dest="preference_command", metavar="<preference-command>")
    preference_sub.required = True
    p_pref_get = preference_sub.add_parser("get", help="Show the hub pin, or the local cache when the hub is down.")
    p_pref_get.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p_pref_get.set_defaults(func=_dispatch_preference_get)
    p_pref_set = preference_sub.add_parser("set", help="Replace the hub pin and refresh the local cache (admin token).")
    p_pref_set.add_argument("--impl", default=None, help="Default implementation seat name.")
    p_pref_set.add_argument("--review", default=None, help="Default review seat name.")
    p_pref_set.add_argument("--chef", default=None, help="Default chef/orchestrator seat name.")
    p_pref_set.add_argument("--notes", default=None, help="Optional operator note (no secrets or home paths).")
    p_pref_set.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p_pref_set.set_defaults(func=_dispatch_preference_set)
    p_pref_pull = preference_sub.add_parser("pull", help="Refresh the local cache from the hub. Never fails a run.")
    p_pref_pull.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    p_pref_pull.set_defaults(func=_dispatch_preference_pull)


def _dispatch_serve(args: argparse.Namespace, *, environ: Mapping[str, str] | None = None) -> int:
    from .. import fleet_command_deck, fleet_hub

    db_path = args.db if args.db is not None else (Path.home() / DEFAULT_DB_REL_PATH)
    return fleet_hub.run(
        host=args.host,
        port=args.port,
        db_path=db_path,
        token_file=args.token_file,
        allow_admin_writes=bool(args.allow_admin_writes),
        deck_config_path=fleet_command_deck.resolve_config_path(
            args.deck_config, os.environ if environ is None else environ
        ),
        trust_tailscale_identity=bool(args.trust_tailscale_identity),
    )


def _dispatch_nodes_add(args: argparse.Namespace) -> int:
    import json as _json

    from .. import fleet_client

    try:
        payload = fleet_client.add_node(args.node_id, label=args.label)
    except fleet_client.FleetClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    node_raw = payload.get("node")
    node: dict = node_raw if isinstance(node_raw, dict) else {}
    if args.json:
        print(_json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"enrolled node {node.get('node_id') or args.node_id} (label {node.get('label') or '-'})")
    print("node token (shown once; the hub keeps only its hash — store it as that machine's node_token_file):")
    print(str(payload.get("token") or ""))
    return 0


def _dispatch_nodes_list(args: argparse.Namespace) -> int:
    import json as _json

    from .. import fleet_client

    try:
        nodes = fleet_client.fetch_nodes()
    except fleet_client.FleetClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(_json.dumps({"nodes": nodes}, indent=2, sort_keys=True))
        return 0
    headers = ("node", "label", "created", "status")
    rows = []
    for node in nodes:
        revoked = node.get("revoked_at")
        rows.append(
            [
                _safe_table_cell(str(node.get("node_id") or "-")),
                _safe_table_cell(str(node.get("label") or "-")),
                _safe_table_cell(_format_age(node.get("created_at"))),
                _safe_table_cell(f"revoked {_format_age(revoked)} ago" if revoked else "active"),
            ]
        )
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)))
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)))
    if not rows:
        print("(no enrolled fleet nodes)")
    return 0


def _dispatch_nodes_revoke(args: argparse.Namespace) -> int:
    import json as _json

    from .. import fleet_client

    try:
        payload = fleet_client.revoke_node(args.node_id)
    except fleet_client.FleetClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(_json.dumps(payload, indent=2, sort_keys=True))
        return 0
    node_raw = payload.get("node")
    node: dict = node_raw if isinstance(node_raw, dict) else {}
    print(f"revoked node {node.get('node_id') or args.node_id} (token no longer accepted)")
    return 0


def _dispatch_export(args: argparse.Namespace) -> int:
    from .. import fleet_export

    return fleet_export.dispatch_export(args)


def _dispatch_sink(args: argparse.Namespace) -> int:
    from .. import fleet_export

    return fleet_export.dispatch_sink(args)


def _dispatch_sessions(args: argparse.Namespace) -> int:
    import json as _json

    from .. import fleet_client

    try:
        sessions = fleet_client.fetch_sessions(include_all=args.all)
    except fleet_client.FleetClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(_json.dumps(sessions, indent=2, sort_keys=True))
        return 0
    headers = ("NODE", "HARNESS", "SESSION", "REPO", "BRANCH", "DIRTY", "AGE", "EXPIRES")
    rows = []
    for session in sessions:
        dirty = session.get("dirty_paths")
        dirty_count = len(dirty) if isinstance(dirty, list) else 0
        dirty_cell = f"{dirty_count}+" if session.get("dirty_truncated") else str(dirty_count)
        rows.append(
            [
                _safe_table_cell(str(session.get("node_id") or "-")),
                _safe_table_cell(str(session.get("harness") or "-")),
                _safe_table_cell(str(session.get("session_id") or "-")),
                _safe_table_cell(str(session.get("repo_identity") or "-")),
                _safe_table_cell(str(session.get("branch") or "-")),
                _safe_table_cell(dirty_cell),
                _safe_table_cell(_format_age(session.get("heartbeat_at"))),
                _safe_table_cell(_format_remaining(session.get("expires_at"))),
            ]
        )
    widths = [
        max(len(header), *(len(row[index]) for row in rows)) if rows else len(header)
        for index, header in enumerate(headers)
    ]
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True)))
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)))
    if not rows:
        print("(no active fleet sessions)")
    return 0


def _dispatch_status(args: argparse.Namespace) -> int:
    import json as _json

    from .. import fleet_client, run_preference

    try:
        runs = fleet_client.fetch_status(include_all=args.all)
    except fleet_client.FleetClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    preference_source = "hub"
    try:
        preference_payload = _preference_payload(fleet_client.fetch_run_preference())
    except fleet_client.FleetClientError:
        preference_payload = _preference_payload(run_preference.load_cached())
        preference_source = "cached"
    if args.json:
        print(
            _json.dumps(
                {"preference": preference_payload, "runs": runs},
                indent=2,
                sort_keys=True,
            )
        )
        return 0
    headers = ("node", "repo", "run_id", "seat/harness", "state", "exit", "age")
    rows = []
    for run_row in runs:
        seat = "/".join(filter(None, [run_row.get("seat"), run_row.get("harness")])) or "-"
        exit_status = run_row.get("exit_status")
        rows.append(
            [
                _safe_table_cell(str(run_row.get("node_id") or "-"))[:12],
                _safe_table_cell(str(run_row.get("repo") or "-")),
                _safe_table_cell(str(run_row.get("run_id") or "-")),
                _safe_table_cell(seat),
                _safe_table_cell(str(run_row.get("state") or "-")),
                _safe_table_cell("-" if exit_status is None else str(exit_status)),
                _safe_table_cell(_format_age(run_row.get("ts"))),
            ]
        )
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)))
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)))
    if not rows:
        print("(no active fleet runs)")
    print()
    _print_preference(preference_payload, source=preference_source)
    return 0


def _preference_payload(preference: object) -> dict[str, str | None]:
    from .. import run_preference

    if isinstance(preference, run_preference.RunPreference):
        return {
            "impl": preference.impl,
            "review": preference.review,
            "chef": preference.chef,
            "notes": preference.notes,
        }
    if isinstance(preference, dict):
        return {
            "impl": preference.get("impl"),
            "review": preference.get("review"),
            "chef": preference.get("chef"),
            "notes": preference.get("notes"),
        }
    return {"impl": None, "review": None, "chef": None, "notes": None}


def _print_preference(payload: dict[str, str | None], *, source: str) -> None:
    print(f"run preference ({source})")
    for key in ("impl", "review", "chef", "notes"):
        print(f"  {key}: {_safe_table_cell(payload.get(key) or '-')}")


def _dispatch_preference_get(args: argparse.Namespace) -> int:
    import json as _json

    from .. import fleet_client, run_preference

    source = "hub"
    try:
        payload = _preference_payload(fleet_client.fetch_run_preference())
        parsed = run_preference.parse_preference({key: value for key, value in payload.items() if value})
        run_preference.write_cached(parsed)
    except (fleet_client.FleetClientError, run_preference.RunPreferenceError):
        payload = _preference_payload(run_preference.load_cached())
        source = "cached"
    if args.json:
        print(_json.dumps({"preference": payload, "source": source}, indent=2, sort_keys=True))
        return 0
    _print_preference(payload, source=source)
    return 0


def _dispatch_preference_set(args: argparse.Namespace) -> int:
    import json as _json

    from .. import fleet_client, run_preference

    raw = {key: getattr(args, key) for key in ("impl", "review", "chef", "notes") if getattr(args, key) is not None}
    if not raw:
        print("error: set at least one of --impl, --review, --chef, or --notes", file=sys.stderr)
        return 2
    try:
        parsed = run_preference.parse_preference(raw)
        stored = fleet_client.put_run_preference(parsed.payload())
        run_preference.write_cached(run_preference.parse_preference(stored))
    except run_preference.RunPreferenceError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except fleet_client.FleetClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    payload = _preference_payload(stored)
    if args.json:
        print(_json.dumps({"preference": payload, "source": "hub"}, indent=2, sort_keys=True))
        return 0
    _print_preference(payload, source="hub")
    return 0


def _dispatch_preference_pull(args: argparse.Namespace) -> int:
    import json as _json

    from .. import run_preference

    preference = run_preference.refresh_cache()
    payload = _preference_payload(preference)
    if args.json:
        print(_json.dumps({"preference": payload, "source": "cached"}, indent=2, sort_keys=True))
        return 0
    _print_preference(payload, source="cached")
    return 0


def _workspace_from_run_dir(raw_run_dir: str | None) -> tuple[Path | None, str]:
    """(workspace, reason) for a claim's recorded run directory: the workspace
    it belongs to when that directory is on this machine — ``run.json``'s
    ``lock_workspace`` / ``cwd``, else the ``<workspace>/.brigade/runs/<id>``
    layout — or ``None`` and why not."""
    import json as _json

    if not raw_run_dir:
        return None, "the claim records no run directory"
    run_dir = Path(raw_run_dir).expanduser()
    if not run_dir.is_dir():
        return None, f"its run directory {raw_run_dir} does not exist on this machine"
    try:
        meta = _json.loads((run_dir / "run.json").read_text())
    except (OSError, ValueError):
        meta = None
    if isinstance(meta, dict):
        for key in ("lock_workspace", "cwd"):
            value = meta.get(key)
            if isinstance(value, str) and value and Path(value).expanduser().is_dir():
                return Path(value).expanduser().resolve(), "ok"
    if run_dir.parent.name == "runs" and run_dir.parent.parent.name == ".brigade":
        return run_dir.parent.parent.parent.resolve(), "ok"
    return None, (
        f"its run directory {raw_run_dir} has no readable run.json naming its workspace and is not under a "
        "<workspace>/.brigade/runs layout"
    )


def _print_claim_failure(decision, *, what: str) -> bool:
    """Print a hub/config failure for ``what`` and return True when there was one."""
    if decision.reason == "no-hub":
        print("error: no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)", file=sys.stderr)
        return True
    if decision.reason == "no-identity":
        print(
            f"error: no usable fleet node identity ({decision.detail}); initialize this machine's "
            "identity with `brigade node --machine`, or pass --node",
            file=sys.stderr,
        )
        return True
    if decision.reason == "auth-failed":
        print(
            f"error: fleet hub rejected this node's credentials ({decision.detail}); enroll this machine "
            "with 'brigade fleet nodes add <node_id>' and set [fleet] node_token_file",
            file=sys.stderr,
        )
        return True
    if decision.reason == "hub-unavailable":
        detail = decision.detail or ""
        if "'holder'" in detail or "'scope'" in detail or "'action'" in detail:
            detail += " (this fleet hub predates token-less release; upgrade it)"
        print(f"error: fleet hub claim {what} failed: {detail}", file=sys.stderr)
        return True
    if decision.reason == "invalid":
        print(f"error: {decision.detail}", file=sys.stderr)
        return True
    return False


def _release_claim(raw_target: str, *, as_path: bool, node_override: str | None, force: bool, as_json: bool) -> int:
    """``brigade fleet claims --release`` (issue #1141): free a claim whose
    holder token died with its run. Without ``force``, both modes run the
    same proof: this node must own the claim, its recorded run directory
    must resolve to a workspace on this machine (with ``--path``, the one
    given), and that workspace's run lock must have no live owner; the
    release is then fenced to the inspected row."""
    import json as _json

    from .. import fleet_client, runguard

    workspace: Path | None = None
    if as_path:
        candidate = Path(raw_target).expanduser()
        if not candidate.is_dir():
            print(f"error: --path target is not a directory: {raw_target}", file=sys.stderr)
            return 1
        try:
            workspace = candidate.resolve()
        except OSError:
            workspace = candidate
        # Never an ancestor walk: the directory must be the workspace itself.
        enclosing = fleet_client.find_workspace_for_path(workspace)
        if enclosing is not None and enclosing != workspace:
            print(
                f"error: {workspace} is inside the workspace {enclosing}; pass that workspace path instead",
                file=sys.stderr,
            )
            return 1
        target = workspace.name
        local_node = fleet_client.resolve_node_id(workspace)
    else:
        target = raw_target
        local_node = fleet_client.resolve_node_id()
    if node_override is not None and not fleet_client._node_id_is_claimable(node_override):
        print(f"error: --node {node_override!r} is not a usable fleet node identity", file=sys.stderr)
        return 1
    if node_override is not None and node_override != local_node and not force:
        print(
            f"error: --node {node_override} is not this node's identity ({local_node}); releasing another "
            "node's claim requires --force",
            file=sys.stderr,
        )
        return 1
    node_id = node_override or local_node
    inspected_acquired_at: str | None = None
    if not force:
        probe = fleet_client.inspect_claim(target, node_id=node_id)
        if _print_claim_failure(probe, what="lookup"):
            return 1
        if probe.claim is None or probe.claim.get("owner_node") != node_id:
            # Nothing this node verifiably owns: an unfenced delete here
            # could hit a row a fresh run acquires between this probe and
            # the release. Refuse; only --force deletes unverified.
            if as_json:
                receipt = {
                    "target": target,
                    "released": False,
                    "forced": False,
                    "node_id": node_id,
                    "claim": None,
                    "owner": probe.claim,
                }
                print(_json.dumps(receipt, indent=2, sort_keys=True))
            if probe.claim is None:
                print(
                    f"error: no claim owned by this node ({node_id}) on {target!r}; "
                    "use --force to delete another owner's or an unverifiable row",
                    file=sys.stderr,
                )
            else:
                print(
                    f"error: claim on {target!r} is held by node {probe.claim.get('owner_node') or '-'}, not "
                    f"this node ({node_id}); pass --force to release it anyway",
                    file=sys.stderr,
                )
            return 1
        run_workspace, why = _workspace_from_run_dir(probe.lock_run_dir)
        if run_workspace is None:
            print(
                f"error: cannot verify that the run holding {target!r} is dead ({why}); "
                "pass --force to release it anyway",
                file=sys.stderr,
            )
            return 1
        if workspace is not None and run_workspace != workspace:
            print(
                f"error: the claim on {target!r} was taken by a run in {run_workspace}, not {workspace}; "
                f"release it from that workspace (--release {run_workspace} --path) or pass --force",
                file=sys.stderr,
            )
            return 1
        verdict = runguard.inspect_run_lock_reconcile(run_workspace)
        if verdict == "live":
            print(
                f"error: a run owner is still alive on the run lock of {run_workspace}; refusing to "
                f"release {target!r} (pass --force to release it anyway)",
                file=sys.stderr,
            )
            return 1
        if verdict == "invalid":
            print(
                f"error: the run lock of {run_workspace} is malformed, so liveness cannot be verified; "
                f"refusing to release {target!r} (pass --force to release it anyway)",
                file=sys.stderr,
            )
            return 1
        acquired_at = probe.claim.get("acquired_at")
        inspected_acquired_at = acquired_at if isinstance(acquired_at, str) else None
    decision = fleet_client.release_claim(target, node_id=node_id, force=force, acquired_at=inspected_acquired_at)
    if _print_claim_failure(decision, what="release"):
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
        if owner.get("owner_node") == node_id:
            print(
                f"error: the claim on {target!r} was re-acquired since it was inspected (now acquired "
                f"{owner.get('acquired_at') or '-'}); not released",
                file=sys.stderr,
            )
        else:
            print(
                f"error: claim on {target!r} is held by node {owner.get('owner_node') or '-'}, not this node "
                f"({node_id}); pass --force to release it anyway",
                file=sys.stderr,
            )
        return 1
    if not as_json:
        print(f"no active claim on {target!r}")
    return 0


def _session_labels(args: argparse.Namespace) -> dict[str, str | None]:
    return {
        "harness": args.harness,
        "role": args.role,
        "job": args.job,
        "session": args.session,
    }


def _acquire_session(
    target: str,
    *,
    harness: str | None,
    role: str | None,
    job: str | None,
    session: str | None,
    holder: str | None,
    ttl: int | None,
    as_json: bool,
) -> int:
    import json as _json

    from .. import fleet_client

    if not harness or not session:
        print("error: --acquire requires --harness and --session", file=sys.stderr)
        return 2
    try:
        fleet_client.validated_opaque_labels(harness=harness, role=role, job=job, session=session)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    decision = fleet_client.acquire_claim(
        target,
        holder=holder,
        harness=harness,
        role=role,
        job=job,
        session=session,
        ttl_seconds=ttl if ttl is not None else fleet_client.DEFAULT_CLAIM_TTL_SECONDS,
    )
    if _print_claim_failure(decision, what="acquire"):
        return 1
    if as_json:
        print(
            _json.dumps(
                {
                    "target": target,
                    "granted": decision.granted,
                    "holder": decision.holder,
                    "claim": decision.claim,
                    "owner": decision.owner,
                },
                indent=2,
                sort_keys=True,
            )
        )
    if decision.granted:
        if not as_json:
            print(f"acquired claim on {target!r} harness {harness} session {session}")
            print("holder token (shown once; the hub never lists it; pass it to --heartbeat / --release):")
            print(decision.holder or "")
        return 0
    if not as_json:
        owner = (decision.owner or {}).get("owner_node") or "-"
        print(f"error: claim on {target!r} is held by node {owner}", file=sys.stderr)
    return 1


def _heartbeat_session(target: str, *, holder: str | None, ttl: int | None, as_json: bool) -> int:
    import json as _json

    from .. import fleet_client

    if not holder:
        print("error: --heartbeat requires --holder", file=sys.stderr)
        return 2
    decision = fleet_client.renew_claim(
        target,
        holder=holder,
        ttl_seconds=ttl if ttl is not None else fleet_client.DEFAULT_CLAIM_TTL_SECONDS,
    )
    if _print_claim_failure(decision, what="renew"):
        return 1
    if as_json:
        print(
            _json.dumps(
                {"target": target, "renewed": decision.granted, "claim": decision.claim, "owner": decision.owner},
                indent=2,
                sort_keys=True,
            )
        )
    if decision.granted:
        if not as_json:
            print(f"renewed claim on {target!r}")
        return 0
    if not as_json:
        print(
            f"error: claim on {target!r} is held or the holder token does not match",
            file=sys.stderr,
        )
    return 1


def _release_session(
    target: str,
    *,
    holder: str,
    outcome: str | None,
    harness: str | None,
    role: str | None,
    job: str | None,
    session: str | None,
    as_json: bool,
) -> int:
    import json as _json

    from .. import fleet_client

    if outcome is not None and (not harness or not session):
        print("error: --outcome requires --harness and --session", file=sys.stderr)
        return 2
    try:
        labels = (
            fleet_client.validated_opaque_labels(harness=harness, role=role, job=job, session=session)
            if outcome is not None
            else {"harness": harness, "role": role, "job": job, "session": session}
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    harness, role, job, session = labels["harness"], labels["role"], labels["job"], labels["session"]
    decision = fleet_client.release_claim(target, holder=holder)
    if _print_claim_failure(decision, what="release"):
        return 1
    published = None
    if decision.granted and outcome is not None:
        if harness is None or session is None:
            print("error: --outcome requires --harness and --session", file=sys.stderr)
            return 2
        fleet_client.report_external_event(
            target=target, harness=harness, role=role, job=job, session=session, state=outcome
        )
        published = outcome
    if as_json:
        print(
            _json.dumps(
                {
                    "target": target,
                    "released": decision.granted,
                    "forced": False,
                    "claim": decision.claim,
                    "owner": decision.owner,
                    "outcome": published,
                },
                indent=2,
                sort_keys=True,
            )
        )
    if decision.granted:
        if not as_json:
            print(f"released claim on {target!r}")
        return 0
    if not as_json:
        print(
            f"error: claim on {target!r} is held or the holder token does not match",
            file=sys.stderr,
        )
    return 1


def _dispatch_claims(args: argparse.Namespace) -> int:
    import json as _json

    from .. import fleet_client

    actions = [name for name in ("acquire", "heartbeat", "release") if getattr(args, name) is not None]
    if len(actions) > 1:
        print("error: --acquire, --heartbeat, and --release are mutually exclusive", file=sys.stderr)
        return 2
    if args.release is None and (args.force or args.path or args.node is not None):
        print("error: --force, --path, and --node require --release <target>", file=sys.stderr)
        return 2
    if args.holder is not None and (args.force or args.path or args.node is not None):
        print("error: --holder cannot be combined with --force, --path, or --node", file=sys.stderr)
        return 2
    if args.outcome is not None and (args.release is None or args.holder is None):
        print("error: --outcome requires --release <target> and --holder", file=sys.stderr)
        return 2
    if args.acquire is not None:
        return _acquire_session(
            args.acquire,
            holder=args.holder,
            ttl=args.ttl,
            as_json=args.json,
            **_session_labels(args),
        )
    if args.heartbeat is not None:
        return _heartbeat_session(args.heartbeat, holder=args.holder, ttl=args.ttl, as_json=args.json)
    if args.release is not None and args.holder is not None:
        return _release_session(
            args.release,
            holder=args.holder,
            outcome=args.outcome,
            as_json=args.json,
            **_session_labels(args),
        )
    if args.release is not None:
        return _release_claim(
            args.release, as_path=args.path, node_override=args.node, force=args.force, as_json=args.json
        )
    try:
        claims = fleet_client.fetch_claims(include_all=args.all)
    except fleet_client.FleetClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(_json.dumps({"claims": claims}, indent=2, sort_keys=True))
        return 0
    headers = ("target", "node", "conductor", "harness", "session", "acquired", "expires")
    rows = []
    for claim in claims:
        expires = str(claim.get("expires_at") or "-")
        if claim.get("expired"):
            expires += " (expired)"
        rows.append(
            [
                _safe_table_cell(str(claim.get("target") or "-")),
                _safe_table_cell(str(claim.get("owner_node") or "-"))[:12],
                _safe_table_cell(str(claim.get("owner_conductor") or "-")),
                _safe_table_cell(str(claim.get("harness") or "-")),
                _safe_table_cell(str(claim.get("session") or "-")),
                _safe_table_cell(_format_age(claim.get("acquired_at"))),
                _safe_table_cell(expires),
            ]
        )
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    print("  ".join(h.ljust(w) for h, w in zip(headers, widths, strict=True)))
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)))
    if not rows:
        print("(no active fleet claims)")
    return 0


def _dispatch_cloud(args: argparse.Namespace) -> int:
    import json as _json

    from .. import fleet_client

    try:
        snapshot = fleet_client.fetch_cloud(include_all=args.all)
    except fleet_client.FleetClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(_json.dumps(snapshot, indent=2, sort_keys=True))
        return 0
    leases_raw = snapshot.get("leases")
    leases = leases_raw if isinstance(leases_raw, list) else []
    headers = ("lease", "provider", "node", "state", "expires")
    rows = [
        [
            str(lease.get("lease_id") or "-"),
            str(lease.get("provider") or "-"),
            str(lease.get("owner_node") or "-")[:12],
            str(lease.get("state") or "-"),
            str(lease.get("expires_at") or "-"),
        ]
        for lease in leases
        if isinstance(lease, dict)
    ]
    widths = [
        max(len(header), *(len(row[index]) for row in rows)) if rows else len(header)
        for index, header in enumerate(headers)
    ]
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True)))
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)))
    if not rows:
        print("(no cloud leases)")
    return 0


def _dispatch_models(args: argparse.Namespace) -> int:
    import json as _json

    from .. import fleet_client

    try:
        models = fleet_client.fetch_model_policy()
    except fleet_client.FleetClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(_json.dumps({"models": models}, indent=2, sort_keys=True))
        return 0
    headers = ("provider", "model", "enabled", "limit")
    rows = [
        [
            str(model.get("provider") or "-"),
            str(model.get("model") or "-"),
            "yes" if model.get("enabled") else "no",
            str(model.get("limit") if model.get("limit") is not None else "-"),
        ]
        for model in models
    ]
    widths = [
        max(len(header), *(len(row[index]) for row in rows)) if rows else len(header)
        for index, header in enumerate(headers)
    ]
    print("  ".join(header.ljust(width) for header, width in zip(headers, widths, strict=True)))
    for row in rows:
        print("  ".join(cell.ljust(width) for cell, width in zip(row, widths, strict=True)))
    if not rows:
        print("(no model policy entries)")
    return 0


def _dispatch_models_set(args: argparse.Namespace) -> int:
    import json as _json

    from .. import fleet_client

    try:
        kwargs: dict[str, object] = {
            "enabled": bool(args.enable),
            "limit": args.limit,
            "notes": args.notes,
        }
        if args.reasoning is not None:
            kwargs["reasoning"] = args.reasoning
        if args.brigade_cli is not None:
            kwargs["brigade_cli"] = args.brigade_cli
        if args.t3_instance_id is not None:
            kwargs["t3_instance_id"] = args.t3_instance_id
        if args.t3_service_tier is not None:
            kwargs["t3_service_tier"] = args.t3_service_tier
        kwargs["expected_revision"] = args.expect_revision
        policy = fleet_client.set_model_policy(args.provider, args.model, args.seat, **kwargs)
    except fleet_client.FleetClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(_json.dumps(policy, indent=2, sort_keys=True))
        return 0
    print(
        f"model policy {policy.get('provider')}/{policy.get('model')} ({policy.get('seat')}): "
        f"{'enabled' if policy.get('enabled') else 'disabled'}"
    )
    return 0


def _print_admission_json(payload: object, *, reason: str | None = None, include_error: bool = False) -> None:
    import json as _json

    body = dict(payload) if isinstance(payload, dict) else {}
    if reason and "reason" not in body:
        body["reason"] = reason
    if include_error:
        if "error" not in body:
            body["error"] = body.get("reason") or reason
        if "reason" not in body:
            body["reason"] = body.get("error")
    print(_json.dumps(body, indent=2, sort_keys=True))


def _dispatch_models_admit(args: argparse.Namespace) -> int:
    from .. import fleet_model_admission, fleet_model_roster

    if args.consumer not in fleet_model_roster.CONSUMERS or args.phase not in fleet_model_roster.ADMISSION_PHASES:
        if args.json:
            _print_admission_json({"reason": "unsupported-schema", "error": "unsupported-schema"})
        else:
            print("error: unsupported consumer or phase", file=sys.stderr)
        return 2
    decision = fleet_model_admission.admit_model(
        consumer=args.consumer,
        request_id=args.request_id,
        phase=args.phase,
        seat=args.seat,
        expect_revision=args.expect_revision,
        expect_digest=args.expect_digest,
        allow_lkg=False if args.phase == "target" else not args.no_lkg,
    )
    if args.json:
        if decision.ok:
            _print_admission_json(decision.payload)
        else:
            _print_admission_json(decision.payload, reason=decision.reason, include_error=True)
    elif not decision.ok:
        print(f"error: {decision.reason}", file=sys.stderr)
    return decision.exit_code


def _dispatch_models_doctor(args: argparse.Namespace) -> int:
    from .. import fleet_model_admission, fleet_model_roster

    if args.consumer not in fleet_model_roster.CONSUMERS:
        if args.json:
            _print_admission_json({"reason": "unsupported-schema"})
        return 2
    decision = fleet_model_admission.doctor_model_roster(consumer=args.consumer)
    if args.json:
        _print_admission_json(decision.payload)
    elif not decision.ok:
        print(f"error: {decision.reason}", file=sys.stderr)
    return decision.exit_code


def _dispatch_models_reconcile(args: argparse.Namespace) -> int:
    from .. import fleet_model_admission, fleet_model_roster

    if args.consumer not in fleet_model_roster.CONSUMERS:
        if args.json:
            _print_admission_json({"reason": "unsupported-schema"})
        return 2
    decision = fleet_model_admission.reconcile_model_roster(consumer=args.consumer)
    if args.json:
        _print_admission_json(decision.payload)
    elif not decision.ok:
        print(f"error: {decision.reason}", file=sys.stderr)
    return decision.exit_code


def _dispatch_models_retire(args: argparse.Namespace) -> int:
    from .. import fleet_model_admission

    decision = fleet_model_admission.retire_model(
        args.provider,
        args.family,
        permanent=bool(args.permanent),
        expected_revision=args.expect_revision,
        reason_code=args.reason_code,
    )
    if args.json:
        _print_admission_json(decision.payload)
    elif not decision.ok:
        print(f"error: {decision.reason}", file=sys.stderr)
    return decision.exit_code


def _dispatch_models_default_set(args: argparse.Namespace) -> int:
    from .. import fleet_model_admission

    decision = fleet_model_admission.set_consumer_default(
        args.consumer, args.seat, expected_revision=args.expect_revision
    )
    if args.json:
        _print_admission_json(decision.payload)
    elif not decision.ok:
        print(f"error: {decision.reason}", file=sys.stderr)
    return decision.exit_code


def _dispatch_flush(args: argparse.Namespace) -> int:
    from .. import fleet_client

    node_id = fleet_client.resolve_node_id()
    flushed = fleet_client.flush_spool(node_id)
    print(f"flushed {flushed} spooled event(s) for node {node_id}")
    return 0
