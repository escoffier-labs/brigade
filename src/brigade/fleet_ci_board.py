"""Offline Hub CI projection, conservative exact-SHA rollups and privacy.

The managed Claude review normalizer is deliberately not used for generic CI.
Only its general identity/time validators and the Action evidence scalar
validators are shared. This board never infers review or merge readiness.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any

from .claude_actions_evidence import _repository, _text, _STATUSES, _CONCLUSIONS
from .claude_review_evidence import _head, _object_id, _timestamp

MAX_EVENTS = 1000
MAX_REPOSITORIES = 200
FRESH_SECONDS = 3600
VIEW_RUN_LIMIT = 20
CHECK_LIMIT = 1000
RUN_LIMIT = 100
RETENTION_DAYS = 30
EVENT_IDS = {
    "workflow_run": "run_id",
    "check_run": "check_run_id",
    "check_suite": "check_suite_id",
    "pull_request": "pr_number",
    "status": "status_id",
}
EVENTS = frozenset(EVENT_IDS)


def stamp(value: str | None = None) -> str:
    result = _timestamp(value or datetime.now(timezone.utc).isoformat())
    if result is None:
        raise ValueError("invalid CI observation time")
    return result


def _order_time(value: str) -> str:
    # Fixed precision keeps SQLite/Python ordering chronological across whole
    # seconds and fractional provider timestamps. Display stamps stay intact.
    return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat(timespec="microseconds")


def repo_name(value: object) -> str:
    result = _repository(value)
    if result is None:
        raise ValueError("invalid CI repository")
    return result


def refresh_key(repo: str) -> str:
    return hashlib.sha256(repo.encode()).hexdigest()


def normalize_event(raw: Any, *, now: str, source: str = "github-relay") -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("event") not in EVENTS:
        raise ValueError("invalid CI event")
    repo = repo_name(raw.get("repo"))
    kind = raw["event"]
    identity = EVENT_IDS[kind]
    object_id = _object_id(raw.get(identity, raw.get("id")))
    attempt = _object_id(raw.get("run_attempt", 1))
    head = _head(raw.get("head_sha"))
    updated = _timestamp(raw.get("updated_at") or raw.get("completed_at") or raw.get("created_at"))
    if object_id is None or attempt is None or updated is None or head is None:
        raise ValueError("CI event requires provider identity, SHA and timestamp")
    status = raw.get("status")
    conclusion = raw.get("conclusion")
    if kind == "status":
        status = "completed" if status in {"success", "failure", "error"} else "pending"
        conclusion = (
            "success"
            if raw.get("status") == "success"
            else "failure"
            if raw.get("status") in {"error", "failure"}
            else None
        )
    return dict(
        repo=repo,
        event=kind,
        object_id=object_id,
        attempt=attempt,
        head_sha=head,
        head_branch=_text(raw.get("head_branch"), 255),
        status=status if status in _STATUSES else "unknown",
        conclusion=conclusion
        if status == "completed" and conclusion in _CONCLUSIONS | {"startup_failure"}
        else "unknown",
        name=_text(raw.get("name") or raw.get("context"), 255),
        app_id=_object_id(raw.get("app_id")),
        pr_number=_object_id(raw.get("pr_number")),
        action=_text(raw.get("action"), 40),
        created_at=_timestamp(raw.get("created_at")) or updated,
        creation_known=_timestamp(raw.get("created_at")) is not None,
        updated_at=updated,
        observed_at=stamp(now),
        source=source,
    )


def validate_snapshot(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("invalid CI repository observation")
    repo = repo_name(raw.get("repo"))
    observed = stamp(raw.get("observed_at"))
    if raw.get("visibility") not in {"public", "private", "unknown"}:
        raise ValueError("explicit CI visibility required")
    pulls = raw.get("pulls", [])
    required = raw.get("required_checks", [])
    heads = raw.get("checks_complete", [])
    if not isinstance(pulls, list) or len(pulls) > 1000 or not isinstance(required, list) or len(required) > 500:
        raise ValueError("bounded CI observation lists required")
    clean_pulls = []
    for pr in pulls:
        if not isinstance(pr, dict) or _object_id(pr.get("number")) is None or _head(pr.get("head_sha")) is None:
            raise ValueError("invalid CI PR head")
        clean_pulls.append(
            {"number": pr["number"], "head_sha": _head(pr["head_sha"]), "updated_at": _timestamp(pr.get("updated_at"))}
        )
    clean_required = []
    for check in required:
        if not isinstance(check, dict) or _text(check.get("context"), 255) is None:
            raise ValueError("invalid required CI check")
        app_id = check.get("app_id")
        if app_id is not None and _object_id(app_id) is None:
            raise ValueError("invalid required CI app")
        clean_required.append({"context": check["context"], "app_id": app_id})
    if not isinstance(heads, list) or len(heads) > 1001 or any(_head(h) is None for h in heads):
        raise ValueError("invalid complete CI heads")
    check_ids = raw.get("check_ids", {})
    if not isinstance(check_ids, dict) or len(check_ids) > 1001:
        raise ValueError("invalid authoritative CI check identities")
    for head, ids in check_ids.items():
        if (
            _head(head) is None
            or not isinstance(ids, list)
            or len(ids) > MAX_EVENTS
            or any(
                not isinstance(i, str) or not re.fullmatch(r"(?:check_run|status):[0-9]{1,19}:[0-9]{1,19}", i)
                for i in ids
            )
        ):
            raise ValueError("invalid authoritative CI check identities")
    for key in ("required_complete", "pulls_complete", "complete"):
        if type(raw.get(key)) is not bool:
            raise ValueError("explicit CI completeness required")
    return dict(
        repo=repo,
        visibility=raw["visibility"],
        observed_at=observed,
        default_branch=_text(raw.get("default_branch"), 255),
        latest_run_id=_object_id(raw.get("latest_run_id")),
        latest_run_attempt=_object_id(raw.get("latest_run_attempt")),
        head_sha=_head(raw.get("head_sha")),
        required_checks=clean_required,
        required_observed_at=_timestamp(raw.get("required_observed_at")),
        required_complete=raw["required_complete"],
        pulls_complete=raw["pulls_complete"],
        complete=raw["complete"],
        checks_complete=heads,
        check_ids=check_ids,
        pulls=clean_pulls,
        reason=_text(raw.get("reason"), 100),
        source="github-reconcile",
        refresh_id=_timestamp(raw.get("refresh_id")),
        repair_started_at=_timestamp(raw.get("repair_started_at")),
    )


def store(conn: sqlite3.Connection, raw: Any, *, now: str | None = None) -> dict[str, int]:
    now = stamp(now)
    if not isinstance(raw, dict) or set(raw) - {"events", "repositories", "source", "invalidations", "relay"}:
        raise ValueError("invalid CI batch fields")
    events, repositories = raw.get("events", []), raw.get("repositories", [])
    if (
        not isinstance(events, list)
        or len(events) > MAX_EVENTS
        or not isinstance(repositories, list)
        or len(repositories) > MAX_REPOSITORIES
        or raw.get("source", "github-relay") not in {"github-relay", "github-reconcile"}
    ):
        raise ValueError("bounded CI batch required")
    invalidations = raw.get("invalidations", [])
    if not isinstance(invalidations, list) or len(invalidations) > MAX_EVENTS:
        raise ValueError("bounded CI invalidations required")
    clean_invalidations = []
    for item in invalidations:
        if (
            not isinstance(item, dict)
            or set(item) - {"repo", "head_sha", "reason", "relay_cursor"}
            or not {"repo", "head_sha", "reason"} <= set(item)
        ):
            raise ValueError("invalid CI invalidation")
        repo = repo_name(item["repo"])
        head = _head(item["head_sha"])
        if item["head_sha"] is not None and head is None:
            raise ValueError("invalid CI invalidation SHA")
        if item["reason"] not in {
            "relay-incomplete-fields",
            "relay-check-context-unknown",
            "relay-state-unknown",
            "relay-PR-state-unknown",
            "relay-event-unknown",
        }:
            raise ValueError("invalid CI invalidation reason")
        clean_invalidations.append(
            dict(repo=repo, head_sha=head, reason=item["reason"], relay_cursor=item.get("relay_cursor"))
        )
    source = raw.get("source", "github-relay")
    events = [dict(normalize_event(e, now=now, source=source), relay_cursor=e.get("relay_cursor")) for e in events]
    counts = {"events": len(events), "repositories": len(repositories)}
    if "invalidations" in raw:
        counts["invalidations"] = len(clean_invalidations)
    relay = raw.get("relay")
    if relay is not None and (
        source != "github-relay" or not isinstance(relay, str) or not re.fullmatch(r"[a-f0-9]{64}", relay)
    ):
        raise ValueError("invalid CI relay key")
    if relay is not None and any(
        type(r["relay_cursor"]) is not int or not 1 <= r["relay_cursor"] <= 999_999_999_999_999
        for r in [*events, *clean_invalidations]
    ):
        raise ValueError("invalid CI relay cursor")
    repositories = [validate_snapshot(r) for r in repositories]
    for row in repositories:
        row["received_at"] = now
    rank_sql = "CASE json_extract({payload}, '$.status') WHEN 'completed' THEN 3 WHEN 'in_progress' THEN 2 WHEN 'unknown' THEN 0 ELSE 1 END"
    with conn:
        if relay is not None:
            events, clean_invalidations = _accept_relay(conn, relay, events, clean_invalidations)
        for row in repositories:
            conn.execute("INSERT OR IGNORE INTO ci_repo_state(repo) VALUES (?)", (row["repo"],))
            saved = conn.execute("SELECT snapshot FROM ci_repo_state WHERE repo=?", (row["repo"],)).fetchone()[0]
            saved_marker = json.loads(saved).get("repair_started_at") if saved else None
            # Snapshot order follows collection start in the Hub clock domain,
            # not POST arrival. Equal starts retain the first accepted snapshot.
            # Provider objects and batch acknowledgements remain independent.
            if saved_marker and (
                not row["repair_started_at"] or _order_time(row["repair_started_at"]) <= _order_time(saved_marker)
            ):
                continue
            if row["repair_started_at"]:
                conn.execute(
                    "DELETE FROM ci_invalidations WHERE repo=? AND received_at<?",
                    (row["repo"], _order_time(row["repair_started_at"])),
                )
            for head, reason in conn.execute("SELECT head, reason FROM ci_invalidations WHERE repo=?", (row["repo"],)):
                _unknown_scope(row, None if head == "*" else head, reason)
            conn.execute(
                "UPDATE ci_repo_state SET visibility=?, snapshot=?, observed_at=?, last_reconciled=?, "
                "refresh_requested=CASE WHEN refresh_requested=? THEN NULL ELSE refresh_requested END "
                "WHERE repo=?",
                (
                    row["visibility"],
                    json.dumps(row),
                    _order_time(row["observed_at"]),
                    row["observed_at"],
                    row["refresh_id"],
                    row["repo"],
                ),
            )
        for row in events:
            conn.execute("INSERT OR IGNORE INTO ci_repo_state(repo) VALUES (?)", (row["repo"],))
            if source == "github-relay":
                conn.execute(
                    "UPDATE ci_repo_state SET last_event_received=max(coalesce(last_event_received, ?), ?) WHERE repo=?",
                    (now, now, row["repo"]),
                )
            if row["event"] == "pull_request":
                _pr_event(conn, row)
                continue
            table = "ci_runs" if row["event"] == "workflow_run" else "ci_check_runs"
            keys = ["repo"] + ([] if table == "ci_runs" else ["kind"]) + ["object_id", "attempt"]
            values = [row["repo"]] + ([] if table == "ci_runs" else [row["event"]]) + [row["object_id"], row["attempt"]]
            columns = ", ".join(keys)
            placeholders = ", ".join("?" for _ in keys)
            # Lifecycle-only relay observations cannot erase identity established
            # by a full provider read of the same object and attempt.
            previous = conn.execute(
                f"SELECT payload FROM {table} WHERE " + " AND ".join(f"{key}=?" for key in keys), values
            ).fetchone()
            if previous:
                prior = json.loads(previous[0])
                for key in ("name", "app_id"):
                    if row[key] is None:
                        row[key] = prior.get(key)
            # Equal-time terminal evidence may refine an unknown conclusion,
            # while known conclusions and the completed head remain fixed.
            conn.execute(
                f"INSERT INTO {table} ({columns}, updated_at, payload) VALUES ({placeholders}, ?, ?) "
                f"ON CONFLICT({columns}) DO UPDATE SET updated_at=excluded.updated_at, payload=excluded.payload "
                f"WHERE excluded.updated_at>{table}.updated_at OR "
                f"(excluded.updated_at={table}.updated_at AND "
                f"(({rank_sql.format(payload='excluded.payload')}) >= ({rank_sql.format(payload=table + '.payload')})) AND "
                f"(json_extract({table}.payload, '$.status') != 'completed' OR "
                f"(json_extract({table}.payload, '$.status')=json_extract(excluded.payload, '$.status') AND "
                f"(json_extract({table}.payload, '$.conclusion')=json_extract(excluded.payload, '$.conclusion') OR "
                f"json_extract({table}.payload, '$.conclusion')='unknown') AND "
                f"json_extract({table}.payload, '$.head_sha')=json_extract(excluded.payload, '$.head_sha'))))",
                (*values, _order_time(row["updated_at"]), json.dumps(row)),
            )
        for invalidation in clean_invalidations:
            _invalidate(conn, invalidation, now=now)
        cutoff = _order_time(
            (datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(days=RETENTION_DAYS)).isoformat()
        )
        touched = {r["repo"] for r in [*events, *repositories, *clean_invalidations]}
        historical = {
            r[0]
            for r in conn.execute(
                "SELECT repo FROM ci_runs WHERE updated_at<? UNION SELECT repo FROM ci_check_runs WHERE updated_at<?",
                (cutoff, cutoff),
            )
        }
        for repo in touched | historical:
            _retain(conn, repo, cutoff=cutoff)
    return counts


def _accept_relay(
    conn: sqlite3.Connection, relay: str, events: list[dict[str, Any]], invalidations: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Deduplicate transport receipts, independently of provider identities.

    Read each repository watermark once so an event and its invalidation at the
    same cursor are both accepted. A write first serializes overlapping posts.
    Legacy batches without a relay marker remain conservatively replayable.
    """
    previous = {}
    for repo in {r["repo"] for r in [*events, *invalidations]}:
        conn.execute("INSERT OR IGNORE INTO ci_repo_state(repo) VALUES (?)", (repo,))
        row = conn.execute("SELECT cursor FROM ci_relay_watermarks WHERE repo=? AND relay=?", (repo, relay)).fetchone()
        previous[repo] = row[0] if row else 0
    accepted = [r for r in [*events, *invalidations] if r["relay_cursor"] > previous[r["repo"]]]
    for row in accepted:
        conn.execute(
            "INSERT INTO ci_relay_watermarks VALUES (?, ?, ?) ON CONFLICT(repo, relay) DO UPDATE SET cursor=max(cursor, excluded.cursor)",
            (row["repo"], relay, row["relay_cursor"]),
        )
    return (
        [r for r in events if r["relay_cursor"] > previous[r["repo"]]],
        [r for r in invalidations if r["relay_cursor"] > previous[r["repo"]]],
    )


def _retain(conn: sqlite3.Connection, repo: str, *, cutoff: str) -> None:
    row = conn.execute("SELECT snapshot FROM ci_repo_state WHERE repo=?", (repo,)).fetchone()
    snapshot = json.loads(row[0]) if row and row[0] else {}
    heads = {h for h in [snapshot.get("head_sha"), *(p["head_sha"] for p in snapshot.get("pulls", []))] if h}
    placeholders = ",".join("?" for _ in heads) or "NULL"
    count, latest = conn.execute(
        "SELECT count(*), max(received_at) FROM ci_invalidations WHERE repo=?", (repo,)
    ).fetchone()
    collapsed = count > CHECK_LIMIT
    if collapsed:
        # Preserve the newest repair barrier when a collector pulls without
        # reconciling. Coalescing becomes unknown scope rather than losing it.
        conn.execute("DELETE FROM ci_invalidations WHERE repo=?", (repo,))
        conn.execute("INSERT INTO ci_invalidations VALUES (?, '*', ?, 'relay-incomplete-fields')", (repo, latest))
        _unknown_scope(snapshot, None, "relay-incomplete-fields")
    trimmed_heads: set[str] = set()
    for table, limit in (("ci_runs", RUN_LIMIT), ("ci_check_runs", CHECK_LIMIT)):
        conn.execute(
            f"DELETE FROM {table} WHERE repo=? AND updated_at<? AND json_extract(payload, '$.head_sha') NOT IN ({placeholders})",
            (repo, cutoff, *heads),
        )
        # An empty head set also permits all old rows to expire.
        if not heads:
            conn.execute(f"DELETE FROM {table} WHERE repo=? AND updated_at<?", (repo, cutoff))
        # Keep current-head evidence before unrelated history, even if that
        # history has newer provider timestamps. Workflow history never changes
        # the completeness of independently collected check scopes.
        referenced = f"json_extract(payload, '$.head_sha') IN ({placeholders})"
        kind = "kind" if table == "ci_check_runs" else "'workflow_run'"
        priority = (
            f"CASE WHEN {referenced} THEN CASE WHEN {kind} IN ('check_run', 'status') THEN 2 ELSE 1 END ELSE 0 END"
        )
        dropped = conn.execute(
            f"SELECT rowid, json_extract(payload, '$.head_sha'), {kind} FROM {table} WHERE repo=? "
            f"ORDER BY ({priority}) DESC, "
            "updated_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (repo, *heads, limit),
        ).fetchall()
        if table == "ci_check_runs":
            trimmed_heads.update(head for _, head, kind in dropped if head in heads and kind in {"check_run", "status"})
        conn.executemany(f"DELETE FROM {table} WHERE rowid=?", ((rowid,) for rowid, _, _ in dropped))
    if trimmed_heads and snapshot:
        snapshot.update(
            checks_complete=[h for h in snapshot.get("checks_complete", []) if h not in trimmed_heads],
            complete=False,
            reason="retention-bound",
        )
    if (trimmed_heads or collapsed) and snapshot:
        conn.execute("UPDATE ci_repo_state SET snapshot=? WHERE repo=?", (json.dumps(snapshot), repo))


def _unknown_scope(snapshot: dict[str, Any], head: str | None, reason: str) -> None:
    snapshot["checks_complete"] = [h for h in snapshot.get("checks_complete", []) if head is not None and h != head]
    snapshot["complete"] = False
    snapshot["reason"] = reason
    if head is None or reason == "relay-PR-state-unknown":
        snapshot["pulls_complete"] = False


def _invalidate(conn: sqlite3.Connection, item: dict[str, Any], *, now: str) -> None:
    conn.execute("INSERT OR IGNORE INTO ci_repo_state(repo) VALUES (?)", (item["repo"],))
    raw = conn.execute("SELECT snapshot FROM ci_repo_state WHERE repo=?", (item["repo"],)).fetchone()[0]
    snapshot = json.loads(raw) if raw else {"observed_at": now, "pulls": []}
    _unknown_scope(snapshot, item["head_sha"], item["reason"])
    conn.execute(
        "INSERT INTO ci_invalidations VALUES (?, ?, ?, ?) ON CONFLICT(repo, head) DO UPDATE SET received_at=max(received_at, excluded.received_at), reason=excluded.reason",
        (item["repo"], item["head_sha"] or "*", _order_time(now), item["reason"]),
    )
    conn.execute(
        "UPDATE ci_repo_state SET snapshot=?, last_event_received=? WHERE repo=?",
        (json.dumps(snapshot), now, item["repo"]),
    )


def _pr_event(conn: sqlite3.Connection, row: dict[str, Any]) -> None:
    # A PR delivery updates just that PR, never the default branch. Incomplete
    # inventories stay incomplete until reconcile establishes the full set.
    prior = conn.execute(
        "SELECT updated_at FROM ci_check_runs WHERE repo=? AND kind='pull_request' AND object_id=? AND attempt=1",
        (row["repo"], row["object_id"]),
    ).fetchone()
    if prior and _order_time(row["updated_at"]) <= prior[0]:
        return
    conn.execute(
        "INSERT INTO ci_check_runs VALUES (?, 'pull_request', ?, 1, ?, ?) "
        "ON CONFLICT(repo, kind, object_id, attempt) DO UPDATE SET updated_at=excluded.updated_at, payload=excluded.payload",
        (row["repo"], row["object_id"], _order_time(row["updated_at"]), json.dumps(row)),
    )
    result = conn.execute("SELECT snapshot FROM ci_repo_state WHERE repo=?", (row["repo"],)).fetchone()
    if not result or not result[0]:
        return
    snapshot = json.loads(result[0])
    current = next((p for p in snapshot["pulls"] if p["number"] == row["object_id"]), None)
    if current is None and row["action"] == "closed":
        return
    provider_updated = current.get("updated_at") if current else None
    if provider_updated:
        if _order_time(row["updated_at"]) <= _order_time(provider_updated):
            return
    elif snapshot.get("repair_started_at"):
        # A complete repair can establish absence without a provider close
        # timestamp. A delayed reopen cannot prove membership is newer. Missing
        # provider time for an existing head is equally ambiguous.
        snapshot.update(pulls_complete=False, complete=False, reason="PR-event-order-unknown")
        conn.execute("UPDATE ci_repo_state SET snapshot=? WHERE repo=?", (json.dumps(snapshot), row["repo"]))
        return
    else:
        # Legacy snapshots have no provider ordering fence. Compare only Hub
        # receipts and explicitly retain unknown inventory scope.
        boundary = snapshot.get("received_at")
        if not boundary or _order_time(row["observed_at"]) < _order_time(boundary):
            return
        snapshot.update(pulls_complete=False, complete=False, reason="PR-event-order-unknown")
    pulls = [p for p in snapshot["pulls"] if p["number"] != row["object_id"]]
    if row["action"] != "closed":
        pulls.append(
            {
                "number": row["object_id"],
                "head_sha": row["head_sha"],
                "source": row["source"],
                "observed_at": row["observed_at"],
                "updated_at": row["updated_at"],
            }
        )
    snapshot["pulls"] = pulls
    conn.execute("UPDATE ci_repo_state SET snapshot=? WHERE repo=?", (json.dumps(snapshot), row["repo"]))


def fact(*, source: str, observed_at: str | None, now: str, **value: Any) -> dict[str, Any]:
    age = (
        None
        if observed_at is None
        else (
            datetime.fromisoformat(now.replace("Z", "+00:00"))
            - datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        ).total_seconds()
    )
    freshness = "unknown" if age is None or age < -300 else "fresh" if age <= FRESH_SECONDS else "stale"
    return dict(value, source=source, observed_at=observed_at, freshness=freshness)


def rollup(snapshot: dict[str, Any], checks: list[dict[str, Any]], head: str | None, *, now: str) -> dict[str, Any]:
    required = snapshot.get("required_checks", [])
    required_time = snapshot.get("required_observed_at")
    required_age = (
        None
        if not required_time
        else (
            datetime.fromisoformat(now.replace("Z", "+00:00"))
            - datetime.fromisoformat(required_time.replace("Z", "+00:00"))
        ).total_seconds()
    )
    state = "unknown"
    coverage = (
        head is not None
        and head in snapshot.get("checks_complete", [])
        and snapshot.get("required_complete")
        and required_age is not None
        and 0 <= required_age < 86400
    )
    if coverage and required:
        outcomes = []
        for requirement in required:
            candidates = [
                c
                for c in checks
                if c["event"] in {"check_run", "status"}
                and c["head_sha"] == head
                and c["name"] == requirement["context"]
                and (
                    head not in snapshot.get("check_ids", {})
                    or f"{c['event']}:{c['object_id']}:{c['attempt']}" in snapshot["check_ids"][head]
                    or _order_time(c["observed_at"]) > _order_time(snapshot.get("received_at", snapshot["observed_at"]))
                )
                and (requirement["app_id"] is None or c["app_id"] == requirement["app_id"])
            ]
            # The latest attempt wins for a context, even when an earlier attempt succeeded.
            candidate = max(
                candidates,
                key=lambda c: (
                    _order_time(c["created_at"]),
                    c["attempt"],
                    _order_time(c["updated_at"]),
                    c["object_id"],
                ),
                default=None,
            )
            outcomes.append(
                "unknown"
                if candidate is None
                or candidate["status"] == "unknown"
                or (candidate["status"] == "completed" and candidate["conclusion"] == "unknown")
                else "pending"
                if candidate["status"] != "completed"
                else "passing"
                if candidate["conclusion"] == "success"
                else "not-passing"
            )
        state = (
            "unknown"
            if "unknown" in outcomes
            else "not-passing"
            if "not-passing" in outcomes
            else "pending"
            if "pending" in outcomes
            else "passing"
        )
    elif coverage:
        state = "none-required"
    return fact(
        source="github-reconcile",
        observed_at=snapshot.get("observed_at"),
        now=now,
        state=state,
        head_sha=head,
        required=required,
    )


def _pr_rollup(snapshot: dict[str, Any], checks: list[dict[str, Any]], head: str, *, now: str) -> dict[str, Any]:
    # PR rollups include all observed checks, including optional failures. They
    # do not project the default branch's required set onto another base branch.
    latest: dict[tuple[str, str | None, int | None], dict[str, Any]] = {}
    for check in checks:
        if check["head_sha"] != head or check["event"] not in {"check_run", "status"}:
            continue
        ids = snapshot.get("check_ids", {}).get(head)
        if (
            ids is not None
            and f"{check['event']}:{check['object_id']}:{check['attempt']}" not in ids
            and _order_time(check["observed_at"]) <= _order_time(snapshot.get("received_at", snapshot["observed_at"]))
        ):
            continue
        key = (check["event"], check["name"], check["app_id"])
        prior = latest.get(key)

        def order(c: dict[str, Any]) -> tuple[str, int, str, int]:
            return (_order_time(c["created_at"]), c["attempt"], _order_time(c["updated_at"]), c["object_id"])

        if prior is None or order(check) > order(prior):
            latest[key] = check
    state = "unknown"
    if head in snapshot.get("checks_complete", []) and latest:
        if any(
            c["status"] == "unknown" or (c["status"] == "completed" and c["conclusion"] == "unknown")
            for c in latest.values()
        ):
            state = "unknown"
        elif any(c["status"] == "completed" and c["conclusion"] != "success" for c in latest.values()):
            state = "not-passing"
        elif any(c["status"] != "completed" for c in latest.values()):
            state = "pending"
        else:
            state = "passing"
    sources = sorted({c["source"] for c in latest.values()} | {"github-reconcile"})
    return fact(
        source=" + ".join(sources), observed_at=snapshot.get("observed_at"), now=now, state=state, head_sha=head
    )


def _latest_run(snapshot: dict[str, Any], runs: list[dict[str, Any]]) -> dict[str, Any] | None:
    # REST's latest-run identity is authority. A late completion is not a new
    # workflow creation. Minimal relay envelopes may omit creation timestamps.
    anchored = [r for r in runs if r["object_id"] == snapshot.get("latest_run_id")]
    anchor = max(anchored, key=lambda r: r["attempt"], default=None)
    if anchor is not None:
        newer = [
            r
            for r in runs
            if r["head_sha"] == snapshot.get("head_sha")
            and r.get("head_branch") in (None, snapshot.get("default_branch"))
            and r.get("creation_known")
            and anchor.get("creation_known")
            and _order_time(r["created_at"]) > _order_time(anchor["created_at"])
            and snapshot.get("received_at")
            and _order_time(r["observed_at"]) > _order_time(snapshot["received_at"])
        ]
        return max(newer, key=lambda r: (_order_time(r["created_at"]), r["attempt"]), default=anchor)
    runs = [
        r
        for r in runs
        if r["head_sha"] == snapshot.get("head_sha")
        or (snapshot.get("default_branch") and r.get("head_branch") == snapshot["default_branch"])
    ]
    if not runs:
        return None
    if len({r["object_id"] for r in runs}) == 1:
        return max(runs, key=lambda r: r["attempt"])
    if all(r.get("creation_known") for r in runs):
        return max(runs, key=lambda r: (_order_time(r["created_at"]), r["attempt"]))
    return None


def load_view(conn: sqlite3.Connection, *, operator: bool = False, now: str | None = None) -> dict[str, Any]:
    now = stamp(now)
    rows = []
    for repo, visibility, raw, observed, last_event, last_reconciled, refresh in conn.execute(
        "SELECT repo, visibility, snapshot, observed_at, last_event_received, last_reconciled, refresh_requested FROM ci_repo_state "
        "WHERE (? OR visibility='public') ORDER BY repo LIMIT ?",
        (operator, MAX_REPOSITORIES),
    ):
        if not operator and visibility != "public":
            continue
        snapshot = json.loads(raw) if raw else {}
        runs = [
            json.loads(r[0])
            for r in conn.execute(
                "SELECT payload FROM ci_runs WHERE repo=? ORDER BY updated_at DESC, rowid DESC LIMIT ?",
                (repo, VIEW_RUN_LIMIT),
            )
        ]
        anchor = snapshot.get("latest_run_id")
        if anchor and not any(r["object_id"] == anchor for r in runs):
            result = conn.execute(
                "SELECT payload FROM ci_runs WHERE repo=? AND object_id=? ORDER BY attempt DESC LIMIT 1", (repo, anchor)
            ).fetchone()
            if result:
                runs = [*runs[: VIEW_RUN_LIMIT - 1], json.loads(result[0])]
        heads = list(
            dict.fromkeys(
                h
                for h in [
                    head
                    for head in [snapshot.get("head_sha"), *(p["head_sha"] for p in snapshot.get("pulls", [])[:20])]
                ]
                if h
            )
        )
        placeholders = ",".join("?" for _ in heads) or "NULL"
        checks = [
            json.loads(r[0])
            for r in conn.execute(
                f"SELECT payload FROM ci_check_runs WHERE repo=? AND json_extract(payload, '$.head_sha') IN ({placeholders}) ORDER BY updated_at DESC LIMIT ?",
                (repo, *heads, CHECK_LIMIT + 1),
            )
        ]
        if len(checks) > CHECK_LIMIT:
            snapshot = dict(snapshot, checks_complete=[], complete=False, reason="view-bound")
            checks = checks[:CHECK_LIMIT]
        head = snapshot.get("head_sha")
        for run in runs:
            run["head_relation"] = (
                "unknown"
                if head is None
                else "current-head"
                if run["head_sha"] == head
                else "stale-head"
                if run.get("head_branch") == snapshot.get("default_branch") and snapshot.get("default_branch")
                else "unknown-branch"
            )
            run.update(fact(source=run["source"], observed_at=run["observed_at"], now=now))
        latest = _latest_run(snapshot, runs)
        pulls = []
        for pr in snapshot.get("pulls", []):
            pulls.append(
                dict(
                    pr,
                    rollup=_pr_rollup(snapshot, checks, pr["head_sha"], now=now),
                    **fact(
                        source=pr.get("source", "github-reconcile"),
                        observed_at=pr.get("observed_at", observed),
                        now=now,
                    ),
                )
            )
        rows.append(
            dict(
                repo=repo,
                visibility=visibility,
                head=fact(
                    source="github-reconcile",
                    observed_at=observed,
                    now=now,
                    sha=head,
                    branch=snapshot.get("default_branch"),
                ),
                required=rollup(snapshot, checks, head, now=now),
                latest_run=latest,
                runs=runs,
                pulls=pulls,
                pulls_complete=snapshot.get("pulls_complete", False),
                complete=snapshot.get("complete", False),
                reason=snapshot.get("reason"),
                last_event_received=last_event,
                last_reconciled=last_reconciled,
                refresh_requested=refresh,
            )
        )
    return {"repositories": rows, "as_of": now}


def request_refresh(conn: sqlite3.Connection, repo: str, *, now: str | None = None) -> None:
    repo, now = repo_name(repo), stamp(now)
    with conn:
        conn.execute("INSERT OR IGNORE INTO ci_repo_state(repo) VALUES (?)", (repo,))
        conn.execute("UPDATE ci_repo_state SET refresh_requested=? WHERE repo=?", (now, repo))


def work_plan(conn: sqlite3.Connection, *, operator: bool) -> dict[str, Any]:
    from . import fleet_hub_policy

    policy = fleet_hub_policy.current_policy(conn)["document"]["repositories"]
    states = {r[0]: r[1] for r in conn.execute("SELECT repo, visibility FROM ci_repo_state")}
    rows = {}
    for identity in policy:
        name = identity.removeprefix("github.com/")
        if _repository(name):
            name = repo_name(name)
            visibility = states.get(name, "unknown")
            if operator or visibility == "public":
                rows[name] = {"repo": name, "visibility": visibility}
    requests = {
        refresh_key(r[0]): r[1]
        for r in conn.execute("SELECT repo, refresh_requested FROM ci_repo_state WHERE refresh_requested IS NOT NULL")
        if operator or r[0] in rows
    }
    return {"repositories": [r for _, r in sorted(rows.items())], "refresh_requests": requests, "as_of": stamp()}
