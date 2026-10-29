"""Bounded, read-only reported context from one Worklore database snapshot.

Text validation is not semantic secret or instruction classification. No liveness,
conflict, evidence, item-version or ownership-generation binding is inferred.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Mapping

from . import fleet_dot, worklore_ownership as ownership, worklore_store as store
from .worklore_validate import BLOCKER_MAX, KINDS, TIMESTAMP_MAX, TITLE_MAX, TRANSITIONS, safe_text

_LABELS = {
    "schema": "brigade.prepared_handoff.v1",
    "consistency": "single-db-snapshot",
    "visibility": "fleet-trust",
    "authority": "read-only-context",
    "person_attribution": "unrecorded",
    "historical_human_attribution": "unrecorded",
}
_ITEM_FIELDS = {"work_id", "version", "title", "kind", "status", "acceptance", "effective_acceptance", "blocker"}
_OWNERSHIP_FIELDS = {
    "state",
    "revision",
    "generation",
    "owner_node",
    "offered_to",
    "last_seq",
    "updated_at",
    "liveness",
}
_CHECKPOINT_FIELDS = {"repo_identity", "write_scope", "source_revision", "next_action", "evidence_refs"}
_CHECKPOINT_LABELS = {
    "conflict_check_freshness": "historical-checkpoint-admission",
    "evidence_verification": "reported-unverified",
    "checkpoint_current_item_version_binding": "unavailable",
}
_REPORT_STORAGE_FIELDS = {"reporter_node", "evidence_verification", "provider_lifecycle"}
_REPORT_LABELS = {"evidence_verification": "reported-unverified", "provider_lifecycle": "unobserved"}
_RUN_LABELS = {"coverage": "recorded-execution-events", "ownership_generation_binding": "unavailable"}
_RUN_LIMIT = 50


def _choice(raw: object, allowed: set[str] | frozenset[str]) -> str:
    if not isinstance(raw, str) or raw not in allowed:
        raise ownership._invalid("invalid prepared handoff value")
    return raw


def _labels(raw: Mapping[str, Any], expected: Mapping[str, str]) -> None:
    for field, value in expected.items():
        _choice(raw[field], {value})


def _timestamp(raw: object) -> str:
    text = safe_text(raw, "timestamp", max_len=TIMESTAMP_MAX)
    if datetime.fromisoformat(text.replace("Z", "+00:00")).tzinfo is None:
        raise ownership._invalid("prepared handoff timestamp requires timezone")
    return text


def _acceptance(raw: object) -> list[str]:
    return [
        safe_text(value, "acceptance", max_len=store.ACCEPTANCE_ITEM_MAX)
        for value in ownership._array(raw, store.ACCEPTANCE_MAX_ITEMS)
    ]


def _report_data(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ownership._invalid("invalid prepared handoff report")
    # Only the storage-added labels are removed. The public Dot validator must
    # see every other field so unknown keys cannot be silently discarded.
    normalized = fleet_dot.validate_ownership_report(
        {key: value for key, value in raw.items() if key not in _REPORT_LABELS}
    )
    normalized["observed_at"] = _timestamp(raw.get("observed_at"))
    return normalized


def validate_prepared_handoff(raw: object, work_id: str) -> dict[str, Any]:
    """Strict wire allowlist shared by the query owner and CLI consumer."""
    view = ownership._object(
        raw, set(_LABELS) | {"observed_at", "item", "ownership", "checkpoint", "last_report", "run_refs"}
    )
    _labels(view, _LABELS)
    view["observed_at"] = _timestamp(view["observed_at"])
    item = ownership._object(view["item"], _ITEM_FIELDS)
    identifier = safe_text(item["work_id"], "work_id", max_len=128)
    if identifier != work_id:
        raise ownership._invalid("prepared handoff item does not match")
    view["item"] = {
        "work_id": identifier,
        "version": ownership._integer(item["version"], minimum=1),
        "title": safe_text(item["title"], "title", max_len=TITLE_MAX),
        "kind": _choice(item["kind"], KINDS),
        "status": _choice(item["status"], frozenset(TRANSITIONS).union(*TRANSITIONS.values())),
        "acceptance": _acceptance(item["acceptance"]),
        "effective_acceptance": _acceptance(item["effective_acceptance"]),
        "blocker": None
        if item["blocker"] is None
        else safe_text(item["blocker"], "blocker", max_len=BLOCKER_MAX, min_len=0),
    }
    current = ownership._object(view["ownership"], _OWNERSHIP_FIELDS)
    view["ownership"] = {
        "state": _choice(current["state"], {"unowned", "offered", "owned", "handoff-pending"}),
        "revision": ownership._integer(current["revision"]),
        "generation": ownership._integer(current["generation"]),
        "owner_node": None if current["owner_node"] is None else ownership._node(current["owner_node"]),
        "offered_to": None if current["offered_to"] is None else ownership._node(current["offered_to"]),
        "last_seq": None if current["last_seq"] is None else ownership._integer(current["last_seq"]),
        "updated_at": None if current["updated_at"] is None else _timestamp(current["updated_at"]),
        "liveness": _choice(current["liveness"], {"unknown"}),
    }
    if view["checkpoint"] is not None:
        checkpoint = ownership._object(
            view["checkpoint"], _CHECKPOINT_FIELDS | set(_CHECKPOINT_LABELS) | {"conflict_check"}
        )
        _labels(checkpoint, _CHECKPOINT_LABELS)
        parsed = ownership._parse(
            {key: checkpoint[key] for key in _CHECKPOINT_FIELDS}
            | {"action": "checkpoint", "generation": current["generation"]}
        )
        view["checkpoint"] = {
            **{key: parsed[key] for key in _CHECKPOINT_FIELDS},
            **_CHECKPOINT_LABELS,
            "conflict_check": _choice(checkpoint["conflict_check"], {"not-performed", "path-admission"}),
        }
    if view["last_report"] is not None:
        report = ownership._object(view["last_report"], {"data", "reporter_node", "freshness"})
        if not isinstance(report["data"], Mapping):
            raise ownership._invalid("invalid prepared handoff report")
        _labels(report["data"], _REPORT_LABELS)
        view["last_report"] = {
            "data": _report_data(report["data"]),
            "reporter_node": ownership._node(report["reporter_node"]),
            "freshness": _choice(report["freshness"], {"unassessed"}),
        }
    refs = ownership._object(view["run_refs"], {"items", "truncated"} | set(_RUN_LABELS))
    _labels(refs, _RUN_LABELS)
    if type(refs["truncated"]) is not bool:
        raise ownership._invalid("invalid prepared handoff truncation")
    items = []
    seen = set()
    for raw_ref in ownership._array(refs["items"], _RUN_LIMIT):
        ref = ownership._object(raw_ref, {"node_id", "run_id"})
        node = ownership._node(ref["node_id"])
        run = safe_text(ref["run_id"], "run_id", max_len=store.ATTEMPT_RUN_ID_MAX)
        if (node, run) in seen:
            raise ownership._invalid("duplicate prepared handoff run identity")
        seen.add((node, run))
        items.append({"node_id": node, "run_id": run})
    view["run_refs"] = {"items": items, "truncated": refs["truncated"], **_RUN_LABELS}
    return view


def _current_ownership(conn: sqlite3.Connection, work_id: str) -> dict[str, Any]:
    # The existing multi-type ORDER BY can choose a work-id history scan. Seek
    # only the newest row of each ownership type, then choose among at most nine.
    latest = None
    for event_type in ownership.EVENT_TYPES.values():
        row = conn.execute(
            "SELECT seq, detail_json FROM work_events WHERE work_id=? AND event_type=? ORDER BY seq DESC LIMIT 1",
            (work_id, event_type),
        ).fetchone()
        if row is not None and (latest is None or row[0] > latest[0]):
            latest = row
    if latest is None:
        return ownership._public(ownership._initial())
    detail = json.loads(latest[1])
    snapshot = detail.get("ownership") if isinstance(detail, Mapping) else None
    required = _OWNERSHIP_FIELDS | _CHECKPOINT_FIELDS | {"last_report", "conflict_check"}
    if not isinstance(snapshot, Mapping) or not required.issubset(snapshot):
        raise ownership._invalid("invalid stored ownership snapshot")
    return ownership._public(snapshot)


def get_prepared_handoff(conn: sqlite3.Connection, work_id: str) -> dict[str, Any]:
    """Own one deferred read transaction, refusing an active caller untouched."""
    if conn.in_transaction:
        raise sqlite3.ProgrammingError("prepared handoff requires an idle connection")
    conn.execute("BEGIN")
    try:
        item = store.get_item(conn, work_id)
        # BEGIN is deferred. Only the first SELECT establishes the snapshot.
        observed_at = store._utc_now()
        current = _current_ownership(conn, work_id)
        checkpoint = None
        if current["repo_identity"] is not None:
            checkpoint = {
                **{key: current[key] for key in _CHECKPOINT_FIELDS},
                "conflict_check": current["conflict_check"],
                **_CHECKPOINT_LABELS,
            }
        report = None
        if current["last_report"] is not None:
            saved = current["last_report"]
            if not isinstance(saved, Mapping):
                raise ownership._invalid("invalid stored ownership report")
            report = {
                "data": _report_data({key: value for key, value in saved.items() if key not in _REPORT_STORAGE_FIELDS}),
                "reporter_node": ownership._node(saved.get("reporter_node")),
                "freshness": "unassessed",
            }
        rows = conn.execute(
            "SELECT node_id, run_id, seq FROM work_events WHERE work_id=? "
            "AND event_type='execution-linked' ORDER BY seq DESC LIMIT 51",
            (work_id,),
        ).fetchall()
        refs = []
        seen = set()
        for node, run, _seq in rows[:_RUN_LIMIT]:
            node = ownership._node(node)
            run = safe_text(run, "run_id", max_len=store.ATTEMPT_RUN_ID_MAX)
            if (node, run) not in seen:
                seen.add((node, run))
                refs.append({"node_id": node, "run_id": run})
        view = validate_prepared_handoff(
            {
                **_LABELS,
                "observed_at": observed_at,
                "item": {key: item[key] for key in _ITEM_FIELDS},
                "ownership": {key: current[key] for key in _OWNERSHIP_FIELDS},
                "checkpoint": checkpoint,
                "last_report": report,
                "run_refs": {"items": refs, "truncated": len(rows) > _RUN_LIMIT, **_RUN_LABELS},
            },
            work_id,
        )
        conn.commit()
        return view
    except BaseException:
        conn.rollback()
        raise
