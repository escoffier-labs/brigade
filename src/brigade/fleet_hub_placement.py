"""Read-only advisory placement: which machine should a new child agent for a repository run on.

This module reuses the routing candidate evaluator (``fleet_hub_routing._collect_candidates``)
and adds three machine-level checks on top of it: the repository must be a bound, fresh T3
project on the machine, the machine's effective load must be under a threshold, and the
policy's per-repository ``eligible_machines`` (when present) must allow it. There is no second
chooser.

The answer is advice, never admission. It writes nothing: the query runs on one deferred read
snapshot with ``PRAGMA query_only=ON``, never calls a routing or quota ``ensure_schema``, never
stores a decision, mints an id, or creates a reservation or intent. A caller that wants a slot
still goes through ``route``, which re-checks every gate.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator, Mapping

from . import fleet_hub_policy, fleet_hub_routing, fleet_node_projects, fleet_policy, fleet_t3_health
from .fleet_hub import FleetHubError
from .fleet_hub_claims import ClaimFenceError
from .fleet_hub_sessions import count_live_by_node

PLACEMENT_SCHEMA = "brigade.fleet_placement.v1"
DEFAULT_THRESHOLD = 0.70
ADVICE_TTL_SECONDS = 120
TRUST = "node-reported telemetry and inventory; advisory only, never admission"
# Placement is about a repository, not a consumer, so it resolves seats from policy defaults and the
# repository's patches and evaluates the ``general`` workload. ``route`` still gates enrollment.
WORKLOAD = "general"
_REQUEST_FIELDS = frozenset({"action", "repo", "threshold"})
_REQUIRED_TABLES = ("fleet_machine_telemetry", "fleet_reservations")
# Per-machine reasons that reveal what another machine's inventory holds.
_INVENTORY_REASONS = frozenset({"inventory-unbound", "inventory-stale", "inventory-invalid", "repo-not-placeable"})


@contextmanager
def _read_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """One deferred read snapshot with writes refused, always rolled back and restored.

    The caller guarantees no transaction is open, so the rollback only ever ends our own snapshot."""
    prior = conn.execute("PRAGMA query_only").fetchone()[0]
    conn.execute("PRAGMA query_only=ON")
    try:
        conn.execute("BEGIN")
        yield
    finally:
        try:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
        finally:
            conn.execute("PRAGMA query_only=ON" if prior else "PRAGMA query_only=OFF")


def _parse(body: Any) -> tuple[str, float]:
    if not isinstance(body, Mapping):
        raise FleetHubError("fleet placement request must be a JSON object")
    unknown = sorted(set(body).difference(_REQUEST_FIELDS))
    if unknown:
        raise FleetHubError(f"unknown place field(s): {', '.join(unknown)}")
    repo = fleet_hub_routing._bounded(body.get("repo"), "repo")
    assert repo is not None
    key = fleet_node_projects.normalize_repo_key(repo)
    if key is None:
        raise FleetHubError("place field 'repo' must be a canonical repository key such as host.tld/owner/name")
    threshold = fleet_hub_routing._number(body.get("threshold"), "threshold")
    if threshold is None:
        threshold = DEFAULT_THRESHOLD
    if not 0.0 <= threshold <= 1.0:
        raise FleetHubError("place field 'threshold' must be a number in 0..1")
    return key, threshold


def _telemetry_expiry(telemetry: Mapping[str, Any] | None, routing: Mapping[str, Any]) -> datetime | None:
    """When this telemetry row stops counting, by the same TTL rule the evaluator applies."""
    if telemetry is None:
        return None
    observed = fleet_hub_routing._parse_stamp(telemetry.get("observed_at"))
    if observed is None:
        return None
    ttl = int(routing.get("telemetry_ttl_seconds") or fleet_policy.DEFAULT_TELEMETRY_TTL_SECONDS)
    report_ttl = int(telemetry.get("ttl_seconds") or ttl)
    return observed + timedelta(seconds=min(ttl, report_ttl))


def _policy_repo_keys(document: Mapping[str, Any], key: str) -> list[str]:
    """Every policy repositories entry that normalizes to ``key``. The caller's spelling never matters."""
    repositories = document.get("repositories") or {}
    return sorted(str(name) for name in repositories if fleet_policy.normalize_repository_key(str(name)) == key)


def _usable_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Bound, fresh, well-formed inventory rows per machine, ordered by project id so the first is a stable choice."""
    usable: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        machine = row.get("machine")
        # Rows stored under older, looser rules must not leak an id the shared predicates now refuse.
        valid = (
            fleet_node_projects.clean_project_id(row["project_id"]) is not None
            and fleet_node_projects.clean_id(row["environment_id"], fleet_node_projects.MAX_ENVIRONMENT_ID_CHARS)
            is not None
        )
        if machine and valid and row["bound"] and not row["stale"]:
            usable.setdefault(machine, []).append(row)
    for choices in usable.values():
        choices.sort(key=lambda item: str(item["project_id"]))
    return usable


def _envelope(
    *,
    current: Mapping[str, Any],
    key: str,
    threshold: float,
    origin: str | None,
    now: datetime,
    reason: str,
    recommended: dict[str, Any] | None = None,
    candidates: list[dict[str, Any]] | None = None,
    expires: datetime | None = None,
) -> dict[str, Any]:
    ceiling = now + timedelta(seconds=ADVICE_TTL_SECONDS)
    return {
        "schema": PLACEMENT_SCHEMA,
        "status": "recommended" if recommended is not None else "none",
        "advisory": True,
        "trust": TRUST,
        "reason": reason,
        "repo_key": key,
        "threshold": threshold,
        "origin_machine": origin,
        "recommended": recommended,
        "candidates": candidates or [],
        "policy_version": current["revision"],
        "policy_digest": current["digest"],
        "decided_at": now.isoformat(),
        "expires_at": min(expires, ceiling).isoformat() if expires is not None else ceiling.isoformat(),
    }


def _fold(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per machine: eligible if any seat pair is, else the pair with the fewest reasons."""
    folded: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        held = folded.get(pair["machine"])
        if held is None or (not held["eligible"] and (pair["eligible"] or len(pair["reasons"]) < len(held["reasons"]))):
            folded[pair["machine"]] = pair
    return list(folded.values())


def place(
    conn: sqlite3.Connection,
    body: Any,
    caller_node: str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Recommend one machine for a new child agent on ``repo``. Strictly read-only."""
    key, threshold = _parse(body)
    caller = fleet_hub_routing._bounded(caller_node, "caller_node")
    assert caller is not None
    moment = now or fleet_hub_routing._now()
    if conn.in_transaction:
        # The snapshot is rolled back at the end, so it must not be the caller's transaction.
        raise FleetHubError("placement-unavailable: open-transaction")
    with _read_snapshot(conn):
        try:
            return _place(conn, key=key, threshold=threshold, caller=caller, now=moment)
        except (ClaimFenceError, sqlite3.Error, ValueError) as exc:
            # A named, data-free error instead of a generic failure. The class name is all that leaves.
            raise FleetHubError(f"placement-unavailable: {type(exc).__name__}") from exc


def _place(
    conn: sqlite3.Connection,
    *,
    key: str,
    threshold: float,
    caller: str,
    now: datetime,
) -> dict[str, Any]:
    current = fleet_hub_policy.current_policy(conn)
    document = current["document"]
    routing = document.get("routing") or fleet_policy.empty_routing()
    origin = fleet_hub_routing._machine_for_node(document, caller)

    def denied(reason: str, **extra: Any) -> dict[str, Any]:
        return _envelope(current=current, key=key, threshold=threshold, origin=origin, now=now, reason=reason, **extra)

    present = {
        row[0]
        for row in conn.execute(
            f"SELECT name FROM sqlite_master WHERE type='table' AND name IN ({','.join('?' * len(_REQUIRED_TABLES))})",
            _REQUIRED_TABLES,
        )
    }
    if present != set(_REQUIRED_TABLES):
        return denied("routing-not-initialized")
    if not routing.get("enabled"):
        return denied("routing-disabled")
    requirements = (routing.get("workload_requirements") or {}).get(WORKLOAD)
    if requirements is None:
        return denied("unknown-workload")

    matches = _policy_repo_keys(document, key)
    if len(matches) > 1:
        # Possible only for a document saved before the parser refused colliding keys: fail closed.
        return denied("repo-policy-ambiguous")
    policy_repo = matches[0] if matches else key
    inventory = fleet_node_projects.projects_for_repo(conn, key, now)
    by_machine: dict[str, list[dict[str, Any]]] = {}
    for row in inventory:
        if row.get("machine"):
            by_machine.setdefault(row["machine"], []).append(row)
    usable = _usable_rows(inventory)
    if not usable:
        return denied("repo-not-placeable")
    repo_machines = list(((document.get("repositories") or {}).get(policy_repo) or {}).get("eligible_machines") or [])
    live: dict[str, int] | None
    try:
        live = count_live_by_node(conn, now_epoch=now.timestamp())
    except Exception:
        # Fail closed: unreadable sessions must never read as zero sessions.
        live = None
    # Nodes whose T3 observer stopped reporting. Their expired presence rows would read as zero
    # sessions, so their count is unknown. None means the health read itself failed: all unknown.
    stale = fleet_t3_health.stale_nodes(conn, now)

    loads: dict[str, dict[str, Any]] = {}

    def machine_reasons(name: str, machine: Mapping[str, Any], telemetry: Mapping[str, Any] | None) -> list[str]:
        reasons: list[str] = []
        rows = by_machine.get(name) or []
        if name not in usable:
            if any(r["bound"] and not r["stale"] for r in rows):
                reasons.append("inventory-invalid")
            elif any(r["bound"] for r in rows):
                reasons.append("inventory-stale")
            elif rows:
                reasons.append("inventory-unbound")
            else:
                reasons.append("repo-not-placeable")
        if repo_machines and name not in repo_machines:
            reasons.append("repo-machine-not-eligible")
        expiry = _telemetry_expiry(telemetry, routing)
        cpu = None if telemetry is None else telemetry.get("load")
        capacity = int(machine.get("concurrency") or 0)
        node = str(machine.get("node_id"))
        sessions = None if live is None or stale is None or node in stale else int(live.get(node, 0))
        if sessions is None:
            reasons.append("sessions-unknown")
        # Hub-counted sessions are a floor under the node-reported CPU load. Node memory joins this
        # max in a follow-up once nodes report it.
        session_ratio = None if sessions is None else (sessions / capacity if capacity > 0 else 1.0)
        fresh = expiry is not None and now < expiry
        effective = max(float(cpu), session_ratio) if fresh and cpu is not None and session_ratio is not None else None
        loads[name] = {
            "effective_load": None if effective is None else round(effective, 4),
            "load_inputs": {"cpu": cpu, "sessions": sessions, "concurrency": capacity},
            "telemetry_observed_at": None if telemetry is None else telemetry.get("observed_at"),
            "telemetry_expires_at": expiry,
        }
        if effective is None:
            # Fail closed on our own evidence: a machine with no usable load is never placeable, even if
            # the routing evaluator's reasons change. ``sessions-unknown`` already names the session case.
            if "sessions-unknown" not in reasons:
                reasons.append("machine-load-unknown")
        elif effective >= threshold:
            reasons.append("machine-load-shed")
        return reasons

    pairs, _eligible, _selected, denial = fleet_hub_routing._collect_candidates(
        conn,
        document=document,
        routing=routing,
        workload=WORKLOAD,
        requirements=requirements,
        resolution=fleet_policy.resolve_policy(document, None, policy_repo),
        now=now,
        quota_read_only=True,
        extra_reasons=machine_reasons,
    )
    if denial == "unknown-role":
        return denied("unknown-role")
    rows = _fold(pairs)
    eligible = [row["machine"] for row in rows if row["eligible"]]
    # Only machines with a usable load can be ranked or kept local.
    placeable = [name for name in eligible if loads[name]["effective_load"] is not None]

    def candidate_rows(chosen: str | None) -> list[dict[str, Any]]:
        result = []
        for row in rows:
            name = row["machine"]
            reasons = list(row["reasons"])
            if name != origin:
                # Inventory detail about another machine is opaque: the caller learns only that it is not placeable.
                reasons = list(dict.fromkeys("not-placeable" if r in _INVENTORY_REASONS else r for r in reasons))
            if len(usable.get(name, [])) > 1 and name in (origin, chosen):
                reasons.append("multiple-projects")  # informational: never changes ``eligible``
            result.append(
                {
                    "machine": name,
                    "eligible": row["eligible"],
                    "effective_load": (loads.get(name) or {}).get("effective_load"),
                    "reasons": reasons,
                }
            )
        return result

    if not placeable:
        return denied("no-eligible-candidate", candidates=candidate_rows(None))
    if origin in placeable:
        chosen, reason = str(origin), "local-under-threshold"
    else:
        chosen = min(
            placeable,
            key=lambda name: (
                fleet_hub_routing._rank(document, name, WORKLOAD),
                loads[name]["effective_load"],
                name,
            ),
        )
        reason = "peer-ranked"
    candidates = candidate_rows(chosen)
    choices = usable[chosen]
    row = choices[0]
    info = loads[chosen]
    inventory_expires = fleet_hub_routing._parse_stamp(row.get("expires_at"))
    expiries = [stamp for stamp in (info["telemetry_expires_at"], inventory_expires) if stamp is not None]
    recommended = {
        "machine": chosen,
        "environment_id": row["environment_id"],
        "project_id": row["project_id"],
        "project_choices": len(choices),
        "effective_load": info["effective_load"],
        "load_inputs": info["load_inputs"],
        "telemetry_observed_at": info["telemetry_observed_at"],
        "inventory_observed_at": row["observed_at"],
    }
    return denied(reason, recommended=recommended, candidates=candidates, expires=min(expiries))
