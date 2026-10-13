"""Bounded sanitized review history in a caller-owned SQLite transaction."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from contextlib import closing
from typing import Any

from brigade.claude_review_evidence import _head, _object_id, _timestamp
from brigade.claude_review_import import (
    GetTransport,
    Limits,
    _OWNER,
    _REPO,
    _SLUG,
    _utc_time,
    collect_managed_review,
)

_TABLE = "brigade_managed_review_history"
_SUBJECT = "repository=? AND pr_number=? AND app_id=? AND app_slug=?"
_MAX_SNAPSHOT_BYTES = 256 * 1024
_MAX_SUBJECT = 200
_MAX_GLOBAL = 20000
_KINDS = ("checks", "annotations", "reviews", "comments")


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def _sort_time(value: str) -> str:
    return _utc_time(value).isoformat(timespec="microseconds")


def _subject(owner: str, repo: str, pr_number: int, app_id: int, app_slug: str, head: str) -> tuple[str, str]:
    current = _head(head)
    if (
        not isinstance(owner, str)
        or not _OWNER.fullmatch(owner)
        or not isinstance(repo, str)
        or not _REPO.fullmatch(repo)
        or repo in {".", ".."}
        or _object_id(pr_number) is None
        or _object_id(app_id) is None
        or not isinstance(app_slug, str)
        or not _SLUG.fullmatch(app_slug)
        or app_slug == "github-actions"
        or current is None
    ):
        raise ValueError("canonical repository, positive PR, full head and trusted app are required")
    return f"{owner.casefold()}/{repo.casefold()}", current


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Add only namespaced storage, without committing or changing user_version."""
    conn.execute(
        f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
        observation_id TEXT PRIMARY KEY, repository TEXT NOT NULL,
        pr_number INTEGER NOT NULL, app_id INTEGER NOT NULL, app_slug TEXT NOT NULL,
        observed_at TEXT NOT NULL, observed_sort TEXT NOT NULL,
        current_head TEXT NOT NULL, payload TEXT NOT NULL)"""
    )
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS {_TABLE}_subject ON {_TABLE} "
        "(repository, pr_number, app_id, app_slug, observed_sort DESC, observation_id)"
    )


def ingest_managed_review(
    conn: sqlite3.Connection,
    transport: GetTransport,
    *,
    owner: str,
    repo: str,
    pr_number: int,
    current_head: str,
    expected_app_id: int,
    expected_app_slug: str,
    observed_at: str,
    limits: Limits | None = None,
) -> dict[str, Any]:
    """Collect through the sanitizer, then atomically append or recognize replay.

    A previously idle connection is left in a transaction for caller commit or
    rollback. No quota rejection prunes history or rolls back unrelated work.
    """
    repository, current = _subject(owner, repo, pr_number, expected_app_id, expected_app_slug, current_head)
    observed = _timestamp(observed_at)
    if observed is None:
        raise ValueError("a timezone-aware observation time is required")
    if limits is not None and not isinstance(limits, Limits):
        raise ValueError("collection limits must be a Limits instance")
    snapshot = collect_managed_review(
        transport,
        owner=owner.casefold(),
        repo=repo.casefold(),
        pr_number=pr_number,
        current_head=current,
        expected_app_id=expected_app_id,
        expected_app_slug=expected_app_slug,
        observed_at=observed,
        limits=limits,
    )
    payload = _canonical(snapshot)
    encoded = payload.encode("utf-8")
    if len(encoded) > _MAX_SNAPSHOT_BYTES:
        raise ValueError("sanitized snapshot exceeds 256 KiB")
    identity = hashlib.sha256(encoded).hexdigest()
    if not conn.in_transaction:
        conn.execute("BEGIN")
    ensure_schema(conn)
    conn.execute("SAVEPOINT brigade_review_ingest")
    try:
        # Acquire SQLite's writer lock before the replay/quota probes.
        conn.execute(f"UPDATE {_TABLE} SET observation_id=observation_id WHERE 0")
        duplicate = conn.execute(f"SELECT 1 FROM {_TABLE} WHERE observation_id=?", (identity,)).fetchone() is not None
        if not duplicate:
            subject = (repository, pr_number, expected_app_id, expected_app_slug)
            rows = conn.execute(f"SELECT 1 FROM {_TABLE} WHERE {_SUBJECT} LIMIT ?", (*subject, _MAX_SUBJECT)).fetchall()
            if len(rows) >= _MAX_SUBJECT:
                raise ValueError("managed review subject snapshot quota reached")
            rows = conn.execute(f"SELECT observation_id FROM {_TABLE} LIMIT ?", (_MAX_GLOBAL,)).fetchall()
            if len(rows) >= _MAX_GLOBAL:
                raise ValueError("managed review global snapshot quota reached")
            conn.execute(
                f"INSERT INTO {_TABLE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (identity, *subject, observed, _sort_time(observed), current, payload),
            )
    except BaseException:
        if conn.in_transaction:
            conn.execute("ROLLBACK TO brigade_review_ingest")
            conn.execute("RELEASE brigade_review_ingest")
        raise
    conn.execute("RELEASE brigade_review_ingest")
    return {"observation_id": identity, "duplicate": duplicate}


def _contradicted_checks(history: list[dict[str, Any]]) -> set[int]:
    """Check IDs claimed by more than one known head; a check run's head is immutable."""
    heads: dict[int, set[str]] = defaultdict(set)
    for snapshot in history:
        for record in snapshot["checks"]:
            if record["reviewed_head"] is not None:
                heads[record["check_id"]].add(record["reviewed_head"])
    return {check_id for check_id, claimed in heads.items() if len(claimed) > 1}


def _coverage(record: dict[str, Any], current: str, contradicted: bool = False) -> None:
    reviewed = record["reviewed_head"]
    record["current_head"] = current
    record["coverage"] = (
        "unknown" if reviewed is None or contradicted else "current" if reviewed == current else "stale"
    )
    if record["coverage"] != "current" and "current_findings_known" in record:
        record["current_findings_known"] = False
        record["current_zero_findings"] = False


def _project(
    history: list[dict[str, Any]], current: str, truncated: bool, contradicted: set[int]
) -> tuple[dict[str, Any], bool]:
    exact = [s for s in history if s["current_head"] == current]
    latest = [s for s in exact if s["observed_at"] == exact[0]["observed_at"]] if exact else []
    complete = bool(latest) and not truncated and len({_canonical(s) for s in latest}) == 1
    complete = complete and all(
        any(source["resource"] == "check-runs" and source["complete"] for source in s["sources"]) for s in latest
    )
    enumerated = {c["identity_key"] for s in latest for c in s["checks"]}
    projection: dict[str, Any] = {kind: [] for kind in _KINDS}
    for kind in _KINDS:
        objects: dict[str, tuple[str, dict[str, Any], bool, str]] = {}
        locations: dict[str, set[str]] = defaultdict(set)
        heads: dict[str, set[str | None]] = defaultdict(set)
        for snapshot in history:
            for raw in snapshot[kind]:
                key = raw["identity_key"]
                heads[key].add(raw["reviewed_head"])
                if kind == "annotations":
                    locations[key].add(_canonical(raw["location"]))
                stamp = snapshot["observed_at"]
                if key not in objects:
                    objects[key] = (stamp, dict(raw), False, snapshot["current_head"])
                elif _sort_time(stamp) == _sort_time(objects[key][0]) and raw != objects[key][1]:
                    previous = objects[key]
                    objects[key] = (previous[0], previous[1], True, previous[3])
        for _, record, conflict, observation_head in objects.values():
            parent_contradicted = kind in ("checks", "annotations") and record["check_id"] in contradicted
            conflict = conflict or (kind == "checks" and parent_contradicted)
            if parent_contradicted or (conflict and len(heads[record["identity_key"]]) > 1):
                record["reviewed_head"] = None
            _coverage(record, current)
            if kind == "annotations" and len(locations[record["identity_key"]]) > 1:
                record["location_disposition"] = "ordinal-location-revised"
                if record["ambiguity"] == "no-universal-immutable-id":
                    record["ambiguity"] = "ordinal-location-revised"
            record["history_disposition"] = "conflicting-observations" if conflict else "latest-observed"
            if kind == "checks" and (
                not complete or conflict or observation_head != current or record["identity_key"] not in enumerated
            ):
                record["current_findings_known"] = False
                record["current_zero_findings"] = False
            projection[kind].append(record)
    _order_checks(history, projection["checks"], enumerated, truncated)
    return projection, complete


def _order_checks(
    history: list[dict[str, Any]], checks: list[dict[str, Any]], enumerated: set[str], truncated: bool
) -> None:
    heads: dict[int, set[str]] = defaultdict(set)
    listed: dict[int, set[str]] = defaultdict(set)
    starts: dict[int, set[str]] = defaultdict(set)
    missing_starts: set[int] = set()
    for snapshot in history:
        for record in snapshot["checks"]:
            if record["reviewed_head"] is not None:
                heads[record["check_id"]].add(record["reviewed_head"])
            else:
                listed[record["check_id"]].add(snapshot["current_head"])
            if record["started_at"] is not None:
                starts[record["check_id"]].add(_sort_time(record["started_at"]))
            else:
                missing_starts.add(record["check_id"])
    uncertain = set()
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in checks:
        identity = record["check_id"]
        if len(starts[identity]) > 1:
            record["history_disposition"] = "conflicting-observations"
        if (
            truncated
            or record["reviewed_head"] is None
            or record["started_at"] is None
            or record["history_disposition"] == "conflicting-observations"
            or (heads[identity] and listed[identity] - heads[identity])
        ):
            uncertain.add(identity)
            record["supersession_disposition"] = "unknown"
            record["superseded_by"] = None
            record["current_findings_known"] = False
            record["current_zero_findings"] = False
        # Listing context adds uncertain rivals, never proven reviewed-head claims.
        for head in heads[identity] | (listed[identity] if identity in uncertain else set()):
            groups[head].append(record)
    if truncated:
        return
    for peers in groups.values():
        rivals = [r for r in peers if r["check_id"] in uncertain]
        rounds: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for record in peers:
            if record["check_id"] not in uncertain:
                rounds[_sort_time(record["started_at"])].append(record)
        times = sorted(rounds)
        next_round = {time: rounds[times[n + 1]] for n, time in enumerate(times[:-1])}
        latest_round = rounds[times[-1]] if times else []
        absent_round = any(
            r["identity_key"] not in enumerated for r in latest_round if r["reviewed_head"] == peers[0]["current_head"]
        )
        for round_start, records in rounds.items():
            # A digest-selected start cannot prove a peer latest or name its next run.
            blocked = any(
                r["check_id"] in missing_starts
                or not starts[r["check_id"]]
                or max(starts[r["check_id"]]) >= round_start
                for r in rivals
            )
            newer = [] if blocked else next_round.get(round_start, [])
            known = not blocked and len(records) == 1
            for record in records:
                record["supersession_disposition"] = (
                    "superseded" if newer else "latest-observed" if known else "unknown"
                )
                record["superseded_by"] = newer[0]["check_id"] if len(newer) == 1 else None
                if newer or not known or absent_round:
                    record["current_findings_known"] = False
                    record["current_zero_findings"] = False


def read_managed_review_history(
    conn: sqlite3.Connection,
    *,
    owner: str,
    repo: str,
    pr_number: int,
    expected_app_id: int,
    expected_app_slug: str,
    current_head: str,
    limit: int = 20,
) -> dict[str, Any]:
    """Read at most limit+1 snapshots and project objects for the caller's head.

    Truncation and uncertainty are independent from execution and severity. The
    projection is not an approval, aggregate finding count or merge gate.
    """
    repository, current = _subject(owner, repo, pr_number, expected_app_id, expected_app_slug, current_head)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("history limit must be an integer from 1 through 100")
    with closing(conn.cursor()) as cursor:
        cursor.row_factory = None
        exists = cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (_TABLE,)
        ).fetchone()
        rows = (
            cursor.execute(
                f"SELECT observation_id, payload FROM {_TABLE} WHERE {_SUBJECT} "
                "ORDER BY observed_sort DESC, observation_id LIMIT ?",
                (repository, pr_number, expected_app_id, expected_app_slug, limit + 1),
            ).fetchall()
            if exists is not None
            else []
        )
    truncated = len(rows) > limit
    history = [
        dict(
            json.loads(
                bytes(payload).decode("utf-8") if isinstance(payload, (bytes, bytearray, memoryview)) else payload
            ),
            observation_id=bytes(identity).decode("utf-8")
            if isinstance(identity, (bytes, bytearray, memoryview))
            else identity,
        )
        for identity, payload in rows[:limit]
    ]
    contradicted = _contradicted_checks(history)
    projection, complete = _project(history, current, truncated, contradicted)
    projected_checks = {c["identity_key"]: c for c in projection["checks"]}
    for snapshot in history:
        for kind in _KINDS:
            for record in snapshot[kind]:
                _coverage(record, current, kind in ("checks", "annotations") and record["check_id"] in contradicted)
                if kind == "checks":
                    latest = projected_checks[record["identity_key"]]
                    is_latest = snapshot["current_head"] == current and record["observed_at"] == latest["observed_at"]
                    for flag in ("current_findings_known", "current_zero_findings"):
                        record[flag] = record[flag] and is_latest and latest[flag]
                    record["superseded_by"] = latest["superseded_by"]
                    record["supersession_disposition"] = latest["supersession_disposition"]
    return {
        "provider": "github",
        "repository": repository,
        "pr_number": pr_number,
        "app_id": expected_app_id,
        "app_slug": expected_app_slug,
        "current_head": current,
        "history": history,
        "history_truncated": truncated,
        "latest_check_enumeration_complete": complete,
        **projection,
        "finding_totals": None,
        "remaining_acceptance": [
            "verified-review-and-inline-app-identity",
            "graphql-thread-disposition",
            "hub-worklore-json-fleethub-integration",
        ],
    }
