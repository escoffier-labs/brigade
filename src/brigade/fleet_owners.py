"""Hub ownership map: policy charters joined to what each owner holds right now.

Read-only. ``load_view`` reads the current policy revision's ``owners`` section
and four live sources the hub already keeps (repo claims, Grok Bot jobs,
interactive sessions, policy delegations). ``attribute`` is a pure matcher over
plain dataclasses, so it is tested without a database. Nothing here enforces a
charter: an item no charter matches lands in ``unassigned``, and an item two
charters match is listed under both and reported in ``overlaps``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from . import fleet_hub_claims, fleet_hub_grokbot, fleet_hub_policy, fleet_hub_sessions, fleet_policy_owners
from .fleet_hub import FleetHubError

SCHEMA = "brigade.fleet_owners.v1"
SOURCE_LIMIT = 500
JSON_HOLDINGS_LIMIT = 100
# Response-wide bound for GET /owners. Charters alone can approach the 256 KiB
# policy document cap and overlapping charters repeat the same holdings, so the
# payload is filled against a byte budget kept under the client's 512 KiB
# MAX_POLICY_RESPONSE_BYTES. The *_total counts stay exact either way.
JSON_RESPONSE_BUDGET_BYTES = 480 * 1024


@dataclass(frozen=True)
class Holding:
    """One live item, normalized. Selector fields are not part of the public shape."""

    kind: str
    ref: str
    repo: str
    node_label: str
    detail: str
    since: str
    nodes: tuple[str, ...] = ()
    harness: str = ""
    conductor: str = ""
    session_harness: str = ""
    adapter: str = ""

    def public(self) -> dict[str, str]:
        return {
            "kind": self.kind,
            "ref": self.ref,
            "repo": self.repo,
            "node_label": self.node_label,
            "detail": self.detail,
            "since": self.since,
        }


@dataclass(frozen=True)
class Overlap:
    item: Holding
    owners: tuple[str, ...]


@dataclass(frozen=True)
class OwnersView:
    policy_version: int | None
    policy_error: str | None
    charters: Mapping[str, Mapping[str, Any]]
    by_owner: Mapping[str, Sequence[Holding]]
    unassigned: Sequence[Holding]
    overlaps: Sequence[Overlap]
    enrolled_principals: frozenset[str]
    unknown_sources: tuple[str, ...]
    truncated_sources: tuple[str, ...] = ()


def _matches(match: Mapping[str, Any], item: Holding) -> bool:
    if match.get("grokbot_jobs") and item.kind == "grokbot-job":
        return True
    if set(match.get("nodes") or ()).intersection(item.nodes):
        return True
    for key, value in (
        ("harnesses", item.harness),
        ("conductors", item.conductor),
        ("session_harnesses", item.session_harness),
        ("adapters", item.adapter),
    ):
        if value and value in (match.get(key) or ()):
            return True
    return False


def attribute(
    charters: Mapping[str, Mapping[str, Any]], items: Sequence[Holding]
) -> tuple[dict[str, list[Holding]], list[Holding], list[Overlap]]:
    """Split *items* into per-owner lists, an unassigned list, and overlaps.

    An item matches an owner when any one selector in that owner's ``match``
    record matches (OR). A charter with no selectors matches nothing. An item
    two charters match is listed under both and once in the overlaps.
    """
    by_owner: dict[str, list[Holding]] = {name: [] for name in charters}
    unassigned: list[Holding] = []
    overlaps: list[Overlap] = []
    for item in items:
        owners = [name for name in sorted(charters) if _matches(charters[name].get("match") or {}, item)]
        if not owners:
            unassigned.append(item)
            continue
        for name in owners:
            by_owner[name].append(item)
        if len(owners) > 1:
            overlaps.append(Overlap(item=item, owners=tuple(owners)))
    return by_owner, unassigned, overlaps


def charter_only(match: Mapping[str, Any]) -> bool:
    """True when a charter carries no selector of any kind (principals included)."""
    return not any(match.get(key) for key in fleet_policy_owners.MATCH_KEYS)


def _label(labels: Mapping[str, str], node_id: str | None) -> str:
    if not node_id:
        return ""
    return labels.get(node_id) or node_id[:8]


def _node_labels(conn: sqlite3.Connection) -> dict[str, str]:
    """Cosmetic only: a failed read falls back to short node ids."""
    try:
        rows = conn.execute("SELECT node_id, label FROM nodes WHERE label IS NOT NULL AND label != ''").fetchall()
    except sqlite3.Error:
        return {}
    return {str(node_id): str(label) for node_id, label in rows}


def _claims(conn: sqlite3.Connection, labels: Mapping[str, str], now_epoch: float) -> list[Holding]:
    return [
        Holding(
            kind="claim",
            ref=claim["target"],
            repo=claim["target"],
            node_label=_label(labels, claim["owner_node"]),
            detail=" ".join(part for part in (claim.get("role"), claim.get("harness")) if part) or "claim",
            since=claim["acquired_at"],
            nodes=(claim["owner_node"],),
            harness=claim.get("harness") or "",
            conductor=claim.get("owner_conductor") or "",
        )
        for claim in fleet_hub_claims.list_active_claims(conn, now_epoch=now_epoch, limit=SOURCE_LIMIT + 1)
    ]


def _grokbot_jobs(conn: sqlite3.Connection, labels: Mapping[str, str]) -> list[Holding]:
    states = sorted(fleet_hub_grokbot.WORK_STATES)
    rows = conn.execute(
        "SELECT job_id, repository, role, state, claimant_node, queued_at, claimed_at FROM grokbot_jobs "
        f"WHERE state IN ({', '.join('?' for _ in states)}) ORDER BY updated_at DESC, job_id LIMIT ?",
        (*states, SOURCE_LIMIT + 1),
    ).fetchall()
    return [
        Holding(
            kind="grokbot-job",
            ref=job_id,
            repo=repository,
            node_label=_label(labels, claimant),
            detail=f"{role} {state}",
            since=claimed_at or queued_at,
            nodes=(claimant,) if claimant else (),
        )
        for job_id, repository, role, state, claimant, queued_at, claimed_at in rows
    ]


def _sessions(conn: sqlite3.Connection, labels: Mapping[str, str], now_epoch: float) -> list[Holding]:
    return [
        Holding(
            kind="session",
            ref=str(session["session_id"]),
            repo=str(session.get("repo_identity") or session.get("repo_label") or ""),
            node_label=_label(labels, str(session["node_id"])),
            detail=" ".join(part for part in (str(session["harness"]), str(session.get("branch") or "")) if part),
            since=str(session["started_at"]),
            nodes=(str(session["node_id"]),),
            session_harness=str(session["harness"]),
        )
        for session in fleet_hub_sessions.list_sessions(conn, now_epoch=now_epoch)
    ]


def _delegations(conn: sqlite3.Connection, labels: Mapping[str, str], now_epoch: float) -> list[Holding]:
    """Delegations whose reservation still occupies its machine.

    Liveness mirrors ``fleet_hub_routing._reservation_occupies``: an unreleased
    reservation is live once accepted, whatever its expiry, and otherwise until
    its ``expires_at``. Renewals extend the reservation, not the delegation's
    own copy of the expiry, so the reservation row is read and filtered in SQL
    before the limit applies. ``julianday`` parses the stored ISO text.
    """
    now = datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat()
    rows = conn.execute(
        "SELECT d.delegation_id, d.origin_node, d.target_node, d.adapter, d.repo_identity, d.seat, d.workload, "
        "d.created_at FROM fleet_policy_delegations d "
        "JOIN fleet_reservations r ON r.reservation_id = d.reservation_id "
        "WHERE r.state != 'released' AND (r.accepted = 1 OR julianday(r.expires_at) > julianday(?)) "
        "ORDER BY d.created_at DESC, d.delegation_id LIMIT ?",
        (now, SOURCE_LIMIT + 1),
    ).fetchall()
    return [
        Holding(
            kind="delegation",
            ref=delegation_id,
            repo=repo,
            node_label=_label(labels, target),
            detail=f"{seat} {workload}".strip(),
            since=created_at,
            nodes=(origin, target),
            adapter=adapter,
        )
        for delegation_id, origin, target, adapter, repo, seat, workload, created_at in rows
    ]


def _enrolled_principals(conn: sqlite3.Connection) -> frozenset[str]:
    rows = conn.execute("SELECT name FROM fleet_message_principals WHERE revoked_at IS NULL").fetchall()
    return frozenset(str(name) for (name,) in rows)


def load_view(conn: sqlite3.Connection, *, now_epoch: float, include_grokbot: bool = True) -> OwnersView:
    """Read charters and live holdings. A failing source degrades, it never raises.

    ``include_grokbot=False`` skips the Grok Bot job source entirely, for callers
    that must not expose it. A source that exceeds ``SOURCE_LIMIT`` is listed in
    ``truncated_sources``.
    """
    policy_version: int | None = None
    policy_error: str | None = None
    charters: Mapping[str, Mapping[str, Any]] = {}
    try:
        current = fleet_hub_policy.current_policy(conn)
        policy_version = int(current["revision"])
        charters = current["document"].get("owners") or {}
    except (FleetHubError, sqlite3.Error, ValueError) as exc:
        policy_error = f"policy read failed: {type(exc).__name__}"
    labels = _node_labels(conn)
    unknown: list[str] = []
    truncated: list[str] = []
    items: list[Holding] = []
    loaders: tuple[tuple[str, Callable[[], list[Holding]]], ...] = (
        ("claims", lambda: _claims(conn, labels, now_epoch)),
        *((("grokbot-jobs", lambda: _grokbot_jobs(conn, labels)),) if include_grokbot else ()),
        ("sessions", lambda: _sessions(conn, labels, now_epoch)),
        ("delegations", lambda: _delegations(conn, labels, now_epoch)),
    )
    for name, loader in loaders:
        try:
            loaded = loader()
        except (FleetHubError, sqlite3.Error, ValueError):
            unknown.append(name)
            continue
        # list_sessions stops at its own LIST_ACTIVE_LIMIT, so a full page there
        # counts as possibly truncated; the other loaders fetch one extra row.
        full_session_page = name == "sessions" and len(loaded) >= fleet_hub_sessions.LIST_ACTIVE_LIMIT
        if len(loaded) > SOURCE_LIMIT or full_session_page:
            truncated.append(name)
        items.extend(loaded[:SOURCE_LIMIT])
    enrolled: frozenset[str] = frozenset()
    try:
        enrolled = _enrolled_principals(conn)
    except sqlite3.Error:
        unknown.append("principals")
    by_owner, unassigned, overlaps = attribute(charters, items)
    return OwnersView(
        policy_version=policy_version,
        policy_error=policy_error,
        charters=charters,
        by_owner=by_owner,
        unassigned=unassigned,
        overlaps=overlaps,
        enrolled_principals=enrolled,
        unknown_sources=tuple(unknown),
        truncated_sources=tuple(truncated),
    )


def _size(value: Any) -> int:
    return len(json.dumps(value, separators=(", ", ": ")).encode()) + 2


def view_payload(view: OwnersView) -> dict[str, Any]:
    """The ``brigade.fleet_owners.v1`` JSON body served by ``GET /owners``.

    Owner metadata fills first in name order, with omitted names reported.
    Holdings lists are capped at ``JSON_HOLDINGS_LIMIT``: unassigned items fill
    first, then overlaps, then owner holdings, all within the shared byte budget.
    """
    owners: list[dict[str, Any]] = []
    for name in sorted(view.charters):
        charter = view.charters[name]
        match = charter.get("match") or {}
        owners.append(
            {
                "name": name,
                "label": charter.get("label"),
                "summary": charter.get("summary"),
                "owns": list(charter.get("owns") or []),
                "never": list(charter.get("never") or []),
                "intake": list(charter.get("intake") or []),
                "contact": charter.get("contact"),
                "match": {key: value for key, value in match.items() if key != "principals"},
                "charter_only": charter_only(match),
                "principals": [
                    {"name": principal, "enrolled": principal in view.enrolled_principals}
                    for principal in match.get("principals") or []
                ],
                "holdings": [],
                "holdings_total": len(view.by_owner.get(name, ())),
            }
        )
    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "policy_version": view.policy_version,
        "policy_error": view.policy_error,
        "owners": [],
        "owners_total": len(owners),
        # Reserve room for every omitted name before filling metadata. Removing
        # included names later only makes this conservative byte budget smaller.
        "truncated_owners": [owner["name"] for owner in owners],
        "unassigned": [],
        "unassigned_total": len(view.unassigned),
        "overlaps": [],
        "overlaps_total": len(view.overlaps),
        "unknown_sources": list(view.unknown_sources),
        "truncated_sources": list(view.truncated_sources),
    }
    remaining = JSON_RESPONSE_BUDGET_BYTES - _size(payload)

    def fill(target: list[dict[str, Any]], rows: Sequence[dict[str, Any]], *, limit: int = JSON_HOLDINGS_LIMIT) -> None:
        nonlocal remaining
        for row in rows[:limit]:
            cost = _size(row)
            if cost > remaining:
                return
            target.append(row)
            remaining -= cost

    fill(payload["owners"], owners, limit=len(owners))
    payload["truncated_owners"] = payload["truncated_owners"][len(payload["owners"]) :]
    fill(payload["unassigned"], [item.public() for item in view.unassigned[:JSON_HOLDINGS_LIMIT]])
    fill(
        payload["overlaps"],
        [{**overlap.item.public(), "owners": list(overlap.owners)} for overlap in view.overlaps[:JSON_HOLDINGS_LIMIT]],
    )
    for owner in payload["owners"]:
        fill(owner["holdings"], [item.public() for item in view.by_owner.get(owner["name"], ())[:JSON_HOLDINGS_LIMIT]])
    return payload
