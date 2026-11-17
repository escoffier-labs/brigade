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

from . import fleet_hub_policy, fleet_hub_routing, fleet_node_projects, fleet_policy
from .fleet_hub import FleetHubError
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


@contextmanager
def _read_snapshot(conn: sqlite3.Connection) -> Iterator[None]:
    """One deferred read snapshot with writes refused, always rolled back and restored."""
    prior = conn.execute("PRAGMA query_only").fetchone()[0]
    conn.execute("PRAGMA query_only=ON")
    began = not conn.in_transaction
    try:
        if began:
            conn.execute("BEGIN")
        yield
    finally:
        try:
            if began and conn.in_transaction:
                conn.execute("ROLLBACK")
        finally:
            conn.execute("PRAGMA query_only=ON" if prior else "PRAGMA query_only=OFF")


def _parse(body: Any) -> tuple[str, str, float]:
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
    return repo, key, threshold


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


def _policy_repo_key(document: Mapping[str, Any], repo: str, key: str) -> str:
    """The policy repositories entry for this repo: exact spelling, then the key, then by normalization.

    One answer feeds both ``eligible_machines`` and ``resolve_policy``, so every spelling of a
    repository applies the same patches, privacy and machine list. With no entry, the normalized key.
    """
    repositories = document.get("repositories") or {}
    for candidate in (repo, key):
        if candidate in repositories:
            return candidate
    for name in sorted(repositories):
        if fleet_node_projects.normalize_repo_key(name) == key:
            return str(name)
    return key


def _usable_rows(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Bound, fresh inventory rows per machine, ordered by project id so the first one is a stable choice."""
    usable: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        machine = row.get("machine")
        if machine and row["bound"] and not row["stale"]:
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
    repo, key, threshold = _parse(body)
    caller = fleet_hub_routing._bounded(caller_node, "caller_node")
    assert caller is not None
    moment = now or fleet_hub_routing._now()
    with _read_snapshot(conn):
        return _place(conn, repo=repo, key=key, threshold=threshold, caller=caller, now=moment)


def _place(
    conn: sqlite3.Connection,
    *,
    repo: str,
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

    inventory = fleet_node_projects.projects_for_repo(conn, key, now)
    by_machine: dict[str, list[dict[str, Any]]] = {}
    for row in inventory:
        if row.get("machine"):
            by_machine.setdefault(row["machine"], []).append(row)
    usable = _usable_rows(inventory)
    if not usable:
        return denied("repo-not-placeable")
    policy_repo = _policy_repo_key(document, repo, key)
    repo_machines = list(((document.get("repositories") or {}).get(policy_repo) or {}).get("eligible_machines") or [])
    live: dict[str, int] | None
    try:
        live = count_live_by_node(conn, now_epoch=now.timestamp())
    except Exception:
        # Fail closed: unreadable sessions must never read as zero sessions.
        live = None

    loads: dict[str, dict[str, Any]] = {}

    def machine_reasons(name: str, machine: Mapping[str, Any], telemetry: Mapping[str, Any] | None) -> list[str]:
        reasons: list[str] = []
        rows = by_machine.get(name) or []
        if name not in usable:
            if any(r["bound"] for r in rows):
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
        sessions = None if live is None else int(live.get(str(machine.get("node_id")), 0))
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
        if telemetry is not None and cpu is None:
            reasons.append("machine-load-unknown")
        elif effective is not None and effective >= threshold:
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
    candidates = [
        {
            "machine": row["machine"],
            "eligible": row["eligible"],
            "effective_load": (loads.get(row["machine"]) or {}).get("effective_load"),
            # ``multiple-projects`` is informational: it never changes ``eligible``.
            "reasons": list(row["reasons"])
            + (["multiple-projects"] if len(usable.get(row["machine"], [])) > 1 else []),
        }
        for row in rows
    ]
    eligible = [row["machine"] for row in rows if row["eligible"]]
    if not eligible:
        return denied("no-eligible-candidate", candidates=candidates)
    if origin in eligible:
        chosen, reason = str(origin), "local-under-threshold"
    else:
        chosen = min(
            eligible,
            key=lambda name: (
                fleet_hub_routing._rank(document, name, WORKLOAD),
                loads[name]["effective_load"],
                name,
            ),
        )
        reason = "peer-ranked"
    choices = usable[chosen]
    row = choices[0]
    info = loads[chosen]
    inventory_expires = fleet_hub_routing._parse_stamp(row.get("expires_at"))
    expiries = [stamp for stamp in (info["telemetry_expires_at"], inventory_expires) if stamp is not None]
    recommended = {
        "machine": chosen,
        "node_id": row["node_id"],
        "environment_id": row["environment_id"],
        "project_id": row["project_id"],
        "project_choices": len(choices),
        "effective_load": info["effective_load"],
        "load_inputs": info["load_inputs"],
        "telemetry_observed_at": info["telemetry_observed_at"],
        "inventory_observed_at": row["observed_at"],
    }
    return denied(reason, recommended=recommended, candidates=candidates, expires=min(expiries))
