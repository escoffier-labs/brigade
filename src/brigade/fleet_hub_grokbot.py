"""Authoritative Grok Bot queue and lifecycle on the Fleet Hub.

When a hub is configured, this module is the sole queue authority. Local
Brigade storage keeps only immutable private snapshots. Hub events and
payloads are metadata-only: never task text, report bodies, PATs, listener
credentials, fleet tokens, or lease-holder secrets.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import fleet_command_deck, fleet_hub
from .fleet_hub import FleetHubConflict, FleetHubError, FleetHubForbidden
from .grokbot_job_validation import GrokbotJobError, validate_repository


GROKBOT_JOB_ID_RE = re.compile(r"^grokbot-[0-9a-f]{24}$")
TASK_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_DIGEST_RE = re.compile(r"^[0-9a-f]{40}$")
OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
GITHUB_PULL_URL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9][A-Za-z0-9_.-]{0,98}/[A-Za-z0-9][A-Za-z0-9_.-]{0,98}/pull/[1-9][0-9]*$"
)
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
ROLE_VALUES = frozenset({"implementation-worker", "repository-scout"})
ACTOR_KINDS = frozenset({"feed", "control", "operator", "implementation-worker", "repository-scout"})
ARTIFACT_KINDS = frozenset({"draft-pr", "branch", "report"})
WORK_STATES = frozenset({"queued", "claimed", "running"})
TERMINAL_STATES = frozenset({"completed", "failed", "expired", "canceled"})
HUB_STATES = WORK_STATES | TERMINAL_STATES
CREATE_ACTIONS = frozenset({"enqueue"})
OPERATOR_ACTIONS = frozenset({"list", "status", "whoami", "cancel", "expire", "report-metadata"})
WORKER_ACTIONS = frozenset({"list", "status", "whoami", "claim", "start", "renew", "complete", "fail", "ack-cancel"})
FEED_ACTIONS = frozenset({"enqueue", "list", "whoami"})
ADMIN_ACTIONS = frozenset({"enroll-actor"})
IDENTITY_ACTIONS = frozenset({"whoami"})
ACTIONS = CREATE_ACTIONS | OPERATOR_ACTIONS | WORKER_ACTIONS | ADMIN_ACTIONS | IDENTITY_ACTIONS
MUTATING_ACTIONS = frozenset(
    {"enqueue", "claim", "start", "renew", "complete", "fail", "cancel", "expire", "ack-cancel"}
)
LEASE_ACTIONS = frozenset({"start", "renew", "complete", "fail", "ack-cancel"})
EVENT_STATES = {
    "queued": "external.queued",
    "claimed": "external.claimed",
    "running": "external.running",
    "completed": "external.completed",
    "failed": "external.failed",
    "expired": "external.expired",
    "canceled": "external.canceled",
    "cancel-requested": "external.cancel-requested",
    "cancel-acknowledged": "external.cancel-acknowledged",
    "renewed": "external.heartbeat",
}
FORBIDDEN_REQUEST_KEYS = frozenset(
    {
        "instructions",
        "verification_commands",
        "ownership_paths",
        "base_ref",
        "report_text",
        "report",
        "spec",
        "token",
        "secret",
        "password",
        "credential",
        "authorization",
        "holder",
        "holder_token",
        "node_token",
        "fleet_token",
        "pat",
        "node_id",
        "owner_node",
        "queue",
        "queue_id",
        "queue_owner_node_id",
        "actor_kind",
        "bot_id",
        "ttl_policy",
    }
)
SAFE_JOB_FIELDS = (
    "job_id",
    "role",
    "repository",
    "label",
    "task_digest",
    "state",
    "item_revision",
    "sequence",
    "created_at",
    "updated_at",
    "queued_at",
    "timeout_seconds",
    "artifact_kind",
    "private_snapshot_id",
    "claimed_at",
    "lease_expires_at",
    "lease_generation",
    "cancel_requested_at",
    "artifact_ref",
    "artifact_digest",
    "artifact_size",
    "owner_node",
    "claimant_node",
    "claimant_worker",
    "queue_id",
    "harness",
)
JOB_FIELDS = (
    "job_id",
    "role",
    "repository",
    "label",
    "task_digest",
    "idempotency_key_hash",
    "state",
    "item_revision",
    "sequence",
    "created_at",
    "updated_at",
    "queued_at",
    "timeout_seconds",
    "artifact_kind",
    "private_snapshot_id",
    "claimed_at",
    "lease_token_digest",
    "lease_generation",
    "lease_expires_at",
    "cancel_requested_at",
    "artifact_ref",
    "artifact_digest",
    "artifact_size",
    "owner_node",
    "claimant_node",
    "claimant_worker",
    "queue_id",
)
_JOB_COLUMNS = ", ".join(JOB_FIELDS)
SWEEP_OPERATION_PREFIX = "expire:deadline"
DEFAULT_SWEEP_INTERVAL_SECONDS = 60.0
LEASE_SECONDS_MIN = 30
LEASE_SECONDS_MAX = 3600
DEFAULT_LEASE_SECONDS = 300
_CLOUD_HOLDER_DOMAIN = b"brigade.grokbot.cloud-holder"
_ACTOR_KIND_ACTIONS = {
    "feed": FEED_ACTIONS,
    "control": FEED_ACTIONS,
    "operator": OPERATOR_ACTIONS,
    "implementation-worker": WORKER_ACTIONS,
    "repository-scout": WORKER_ACTIONS,
}

GROKBOT_SCHEMA = """
CREATE TABLE IF NOT EXISTS grokbot_jobs (
    job_id TEXT NOT NULL PRIMARY KEY,
    role TEXT NOT NULL,
    repository TEXT NOT NULL,
    label TEXT NOT NULL,
    task_digest TEXT NOT NULL,
    idempotency_key_hash TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL,
    item_revision INTEGER NOT NULL,
    sequence INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    queued_at TEXT NOT NULL,
    timeout_seconds INTEGER NOT NULL,
    artifact_kind TEXT NOT NULL,
    private_snapshot_id TEXT,
    claimed_at TEXT,
    lease_token_digest TEXT,
    lease_generation INTEGER,
    lease_expires_at TEXT,
    cancel_requested_at TEXT,
    artifact_ref TEXT,
    artifact_digest TEXT,
    artifact_size INTEGER,
    owner_node TEXT,
    claimant_node TEXT,
    claimant_worker TEXT,
    queue_id TEXT
);
CREATE INDEX IF NOT EXISTS grokbot_jobs_state ON grokbot_jobs (state, updated_at);
CREATE INDEX IF NOT EXISTS grokbot_jobs_role ON grokbot_jobs (role, state);
CREATE INDEX IF NOT EXISTS grokbot_jobs_queue ON grokbot_jobs (queue_id, role, state);
CREATE TABLE IF NOT EXISTS grokbot_actor_policy (
    node_id TEXT NOT NULL PRIMARY KEY,
    queue_owner_node_id TEXT NOT NULL,
    queue_id TEXT NOT NULL,
    actor_kind TEXT NOT NULL,
    role TEXT,
    enabled INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS grokbot_operations (
    job_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    action TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    result_revision INTEGER NOT NULL,
    result_state TEXT NOT NULL,
    result_json TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    actor_node_id TEXT,
    queue_id TEXT,
    queue_owner_node_id TEXT,
    PRIMARY KEY (job_id, operation_id)
);
"""
_JOB_ADDITIVE_COLUMNS = {
    "lease_token_digest": "TEXT",
    "lease_generation": "INTEGER",
    "queue_id": "TEXT",
}
_OPERATION_ADDITIVE_COLUMNS = {
    "actor_node_id": "TEXT",
    "queue_id": "TEXT",
    "queue_owner_node_id": "TEXT",
}


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create additive Grok Bot authority tables without touching non-Grok rows.

    Uses ``execute`` per statement, not ``executescript``: the latter issues
    COMMIT first and would drop a caller-held ``BEGIN IMMEDIATE``.
    """
    for statement in (part.strip() for part in GROKBOT_SCHEMA.split(";")):
        if statement:
            conn.execute(statement)
    existing = {row[1] for row in conn.execute("PRAGMA table_info(grokbot_jobs)")}
    for column, decl in _JOB_ADDITIVE_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE grokbot_jobs ADD COLUMN {column} {decl}")
    existing_ops = {row[1] for row in conn.execute("PRAGMA table_info(grokbot_operations)")}
    for column, decl in _OPERATION_ADDITIVE_COLUMNS.items():
        if column not in existing_ops:
            conn.execute(f"ALTER TABLE grokbot_operations ADD COLUMN {column} {decl}")


def handle_grokbot(
    conn: sqlite3.Connection,
    raw: Any,
    *,
    caller_node: str | None = None,
    config: fleet_command_deck.DeckConfig | None = None,
) -> tuple[int, dict[str, Any]]:
    """Atomically enqueue, claim, or terminalize one Grok Bot job."""
    request = _validate_request(raw)
    config = config or fleet_command_deck.DeckConfig()
    action = request["action"]
    if action == "enroll-actor":
        if caller_node is not None:
            raise FleetHubForbidden("enroll-actor requires the hub admin token")
        return _enroll_actor(conn, request)
    policy = _require_actor_policy(conn, caller_node)
    _authorize(policy, request)
    request["node_id"] = policy["node_id"]
    request["queue_id"] = policy["queue_id"]
    request["queue_owner_node_id"] = policy["queue_owner_node_id"]
    _refuse_unscoped_jobs(conn)
    if action == "whoami":
        return 200, {"actor_kind": policy["actor_kind"], "role": policy["role"]}
    if action in {"list", "status"} and "role" not in request and policy.get("role"):
        request["role"] = policy["role"]
    if action == "list":
        return 200, {
            "jobs": list_jobs(
                conn,
                role=request.get("role"),
                queue_id=policy["queue_id"],
                include_all=bool(request.get("include_all")),
                config=config,
            )
        }
    if action in {"status", "report-metadata"}:
        _commit_stale_sweep(conn, config)
        job = _scoped_job(conn, request["job_id"], policy)
        payload = _job_payload(job)
        if action == "report-metadata":
            payload = {
                key: payload[key]
                for key in (
                    "job_id",
                    "role",
                    "state",
                    "artifact_kind",
                    "artifact_ref",
                    "artifact_digest",
                    "artifact_size",
                    "private_snapshot_id",
                )
                if key in payload
            }
        return 200, {"job": payload}
    _commit_stale_sweep(conn, config, skip_job_id=request.get("job_id") if action == "expire" else None)
    conn.execute("BEGIN IMMEDIATE")
    try:
        replayed, mismatch = _replay_operation(conn, request)
        if mismatch:
            conn.rollback()
            return 409, {_result_flag(action): False, "error": "operation-mismatch"}
        if replayed is not None:
            if action == "enqueue":
                current = _require_job(conn, request["job_id"])
                if current.get("queue_id") != policy["queue_id"]:
                    conn.rollback()
                    raise FleetHubForbidden("job is outside this actor's queue")
            else:
                current = _scoped_job(conn, request["job_id"], policy)
                if action in LEASE_ACTIONS or action == "claim":
                    if current.get("claimant_node") not in (None, policy["node_id"]):
                        conn.rollback()
                        return 409, {_result_flag(action): False, "error": "lease-conflict"}
            conn.commit()
            return 200, replayed
        if action != "enqueue":
            current = _scoped_job(conn, request["job_id"], policy)
            if action == "claim" and current["state"] == "expired":
                # The deadline sweep above already bumped the revision, so the
                # generic revision check below would hide why the claim failed.
                conn.rollback()
                return 409, {"claimed": False, "error": "job-expired"}
            if int(current["item_revision"]) != request["expected_item_revision"]:
                conn.rollback()
                return 409, {_result_flag(action): False, "error": "revision-conflict"}
        if action == "enqueue":
            status, payload = _enqueue(conn, request, policy)
        elif action == "claim":
            status, payload = _claim(conn, request, policy, config)
        elif action == "start":
            status, payload = _start(conn, request, policy)
        elif action == "renew":
            status, payload = _renew(conn, request, policy, config)
        elif action == "complete":
            status, payload = _complete(conn, request, policy, config)
        elif action == "fail":
            status, payload = _fail(conn, request, policy, config)
        elif action == "cancel":
            status, payload = _cancel(conn, request, policy, config)
        elif action == "expire":
            status, payload = _expire(conn, request, policy, config)
        else:
            status, payload = _ack_cancel(conn, request, policy, config)
        if status == 200 and payload.get(_result_flag(action)) is True:
            _store_operation(conn, request, payload)
            conn.commit()
        elif status == 200:
            conn.commit()
        else:
            conn.rollback()
        return status, payload
    except FleetHubConflict:
        conn.rollback()
        return 409, {_result_flag(action): False, "error": "revision-conflict"}
    except BaseException:
        conn.rollback()
        raise


def list_jobs(
    conn: sqlite3.Connection,
    *,
    role: str | None = None,
    queue_id: str | None = None,
    include_all: bool = False,
    config: fleet_command_deck.DeckConfig | None = None,
) -> list[dict[str, Any]]:
    """Safe metadata projections. Lease tokens and task bodies never leave SQLite."""
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        _expire_stale_jobs(conn, fleet_hub._now_epoch(), config or fleet_command_deck.DeckConfig())
        rows = conn.execute(f"SELECT {_JOB_COLUMNS} FROM grokbot_jobs ORDER BY queued_at, job_id").fetchall()
        jobs = [_job_payload(_job_dict(row)) for row in rows]
        if queue_id is not None:
            jobs = [job for job in jobs if job.get("queue_id") == queue_id]
        if role is not None:
            jobs = [job for job in jobs if job["role"] == role]
        if not include_all:
            jobs = [job for job in jobs if job["state"] not in TERMINAL_STATES]
        if opened:
            conn.commit()
        return jobs
    except BaseException:
        if opened:
            conn.rollback()
        raise


def sweep_expired_jobs(conn: sqlite3.Connection, config: fleet_command_deck.DeckConfig | None = None) -> list[str]:
    """Expire every job past its own deadline and return the ids that moved.

    Read and write paths sweep before they answer, but a queue nobody polls
    still has to expire: a job enqueued with ``timeout_seconds`` 7200 that no
    worker ever claims must not sit ``queued`` forever waiting for an operator
    to run ``expire`` by hand (#1353).

    Transaction-aware: if the caller already has an open transaction, the
    sweep piggybacks on it so it can be used from request handlers that are
    already inside a SQLite transaction.
    """
    opened = False
    if conn.in_transaction is False:
        conn.execute("BEGIN IMMEDIATE")
        opened = True
    try:
        expired = _expire_stale_jobs(conn, fleet_hub._now_epoch(), config or fleet_command_deck.DeckConfig())
        if opened:
            conn.commit()
    except BaseException:
        if opened:
            conn.rollback()
        raise
    return expired


def start_expiry_sweeper(
    db_path: Path,
    *,
    interval: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
    stop: threading.Event | None = None,
    config: fleet_command_deck.DeckConfig | None = None,
) -> threading.Thread:
    """Run :func:`sweep_expired_jobs` on a timer for the life of the hub.

    Each pass opens and closes its own connection, so the sweeper never holds
    a handle across a sleep and never shares one across threads. A locked or
    transiently unreadable database is left for the next tick rather than
    killing the thread.
    """
    stop_event = stop or threading.Event()
    resolved = Path(db_path)

    def _loop() -> None:
        while not stop_event.wait(interval):
            try:
                conn = fleet_hub.open_db(resolved)
            except (sqlite3.Error, FleetHubError):
                continue
            try:
                sweep_expired_jobs(conn, config)
            except (sqlite3.Error, FleetHubError):
                pass
            finally:
                conn.close()

    thread = threading.Thread(target=_loop, name="grokbot-expiry-sweeper", daemon=True)
    thread.start()
    return thread


def deck_projection(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    """Sanitized hub rows for Command Deck and cloud tracker. Terminal jobs are history."""
    rows = conn.execute(f"SELECT {_JOB_COLUMNS} FROM grokbot_jobs ORDER BY queued_at, job_id").fetchall()
    jobs = [_job_payload(_job_dict(row)) for row in rows]
    return {
        "active": [job for job in jobs if job["state"] in WORK_STATES],
        "history": [job for job in jobs if job["state"] in TERMINAL_STATES],
    }


def _enroll_actor(conn: sqlite3.Connection, request: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    conn.execute(
        "INSERT INTO grokbot_actor_policy "
        "(node_id, queue_owner_node_id, queue_id, actor_kind, role, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(node_id) DO UPDATE SET "
        "queue_owner_node_id=excluded.queue_owner_node_id, queue_id=excluded.queue_id, "
        "actor_kind=excluded.actor_kind, role=excluded.role, enabled=excluded.enabled",
        (
            request["enroll_node_id"],
            request["queue_owner_node_id"],
            request["queue_id"],
            request["actor_kind"],
            request.get("role"),
            1 if request["enabled"] else 0,
        ),
    )
    conn.commit()
    return 200, {"enrolled": True, "node_id": request["enroll_node_id"]}


def _require_actor_policy(conn: sqlite3.Connection, caller_node: str | None) -> dict[str, Any]:
    if caller_node is None:
        raise FleetHubForbidden("the admin token may not substitute for an enrolled Grok Bot listener")
    row = conn.execute(
        "SELECT node_id, queue_owner_node_id, queue_id, actor_kind, role, enabled "
        "FROM grokbot_actor_policy WHERE node_id = ?",
        (caller_node,),
    ).fetchone()
    if row is None or not row[5]:
        raise FleetHubForbidden("caller is not an enrolled Grok Bot actor")
    return {
        "node_id": row[0],
        "queue_owner_node_id": row[1],
        "queue_id": row[2],
        "actor_kind": row[3],
        "role": row[4],
        "enabled": bool(row[5]),
    }


def _authorize(policy: dict[str, Any], request: dict[str, Any]) -> None:
    allowed = _ACTOR_KIND_ACTIONS[policy["actor_kind"]]
    if request["action"] not in allowed:
        raise FleetHubForbidden(f"{policy['actor_kind']} may not {request['action']}")
    if policy["actor_kind"] in ROLE_VALUES:
        if request.get("role") not in (None, policy["role"]):
            raise FleetHubForbidden("worker role is fixed by the enrolled actor policy")
        request["role"] = policy["role"]


def _scoped_job(conn: sqlite3.Connection, job_id: str, policy: dict[str, Any]) -> dict[str, Any]:
    job = _require_job(conn, job_id)
    if job.get("queue_id") != policy["queue_id"]:
        raise FleetHubForbidden("job is outside this actor's queue")
    if policy["actor_kind"] in ROLE_VALUES and job["role"] != policy["role"]:
        raise FleetHubForbidden("job is outside this actor's role")
    return job


def _enqueue(conn: sqlite3.Connection, request: dict[str, Any], policy: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    existing = conn.execute(
        f"SELECT {_JOB_COLUMNS} FROM grokbot_jobs WHERE idempotency_key_hash = ?",
        (request["idempotency_key_hash"],),
    ).fetchone()
    if existing is not None:
        job = _job_dict(existing)
        if job.get("queue_id") != policy["queue_id"] or job.get("owner_node") != policy["queue_owner_node_id"]:
            raise FleetHubForbidden("job is outside this actor's queue")
        if job["task_digest"] != request["task_digest"] or job["job_id"] != request["job_id"]:
            return 409, {"enqueued": False, "error": "idempotency-conflict"}
        return 200, {"enqueued": True, "idempotent": True, "job": _job_payload(job)}
    collision = conn.execute("SELECT 1 FROM grokbot_jobs WHERE job_id = ?", (request["job_id"],)).fetchone()
    if collision is not None:
        return 409, {"enqueued": False, "error": "job-id-conflict"}
    now = _now_iso()
    conn.execute(
        "INSERT INTO grokbot_jobs ("
        "job_id, role, repository, label, task_digest, idempotency_key_hash, state, item_revision, "
        "sequence, created_at, updated_at, queued_at, timeout_seconds, artifact_kind, private_snapshot_id, "
        "owner_node, queue_id"
        ") VALUES (?, ?, ?, ?, ?, ?, 'queued', 1, 1, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            request["job_id"],
            request["role"],
            request["repository"],
            request["label"],
            request["task_digest"],
            request["idempotency_key_hash"],
            now,
            now,
            now,
            request["timeout_seconds"],
            request["artifact_kind"],
            request.get("private_snapshot_id") or request["job_id"],
            policy["queue_owner_node_id"],
            policy["queue_id"],
        ),
    )
    job = _require_job(conn, request["job_id"])
    _record_event(conn, job, "queued")
    return 200, {"enqueued": True, "idempotent": False, "job": _job_payload(job)}


def _claim(
    conn: sqlite3.Connection,
    request: dict[str, Any],
    policy: dict[str, Any],
    config: fleet_command_deck.DeckConfig,
) -> tuple[int, dict[str, Any]]:
    job = _scoped_job(conn, request["job_id"], policy)
    presented = _lease_digest(request["lease_id"])
    instant = datetime.now(timezone.utc)
    if job["state"] == "claimed" and _digests_match(job.get("lease_token_digest"), presented):
        if job.get("claimant_node") != policy["node_id"]:
            return 409, {"claimed": False, "error": "lease-conflict"}
        if _lease_live(job, instant):
            payload = _job_payload(job)
            payload["lease_generation"] = job["lease_generation"]
            return 200, {"claimed": True, "idempotent": True, "job": payload}
        return 409, {"claimed": False, "error": "lease-expired"}
    if job["state"] != "queued":
        return 409, {"claimed": False, "error": "invalid-state"}
    if instant >= _deadline(job):
        # A refusal rolls this transaction back, so the deadline sweep that ran
        # before it (and the periodic sweeper) own the transition to expired.
        return 409, {"claimed": False, "error": "job-expired"}
    generation = int(job["lease_generation"] or 0) + 1
    admitted, error = _admit_capacity(conn, job, request, policy, presented, config)
    if not admitted:
        return 409, {"claimed": False, "error": error}
    expires = min(instant + timedelta(seconds=request["lease_seconds"]), _deadline(job))
    updated = _mutate(
        conn,
        job,
        {
            "state": "claimed",
            "claimed_at": _now_iso(),
            "lease_token_digest": presented,
            "lease_generation": generation,
            "lease_expires_at": _format_ts(expires),
            "claimant_node": policy["node_id"],
            "claimant_worker": policy["actor_kind"],
        },
    )
    _record_event(conn, updated, "claimed")
    payload = _job_payload(updated)
    return 200, {"claimed": True, "idempotent": False, "job": payload, "lease_generation": generation}


def _start(conn: sqlite3.Connection, request: dict[str, Any], policy: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    job = _require_live_holder(conn, request, policy)
    if job is None:
        return 409, {"started": False, "error": "lease-conflict"}
    if job["state"] != "claimed" or job["cancel_requested_at"] is not None:
        return 409, {
            "started": False,
            "error": "invalid-state" if job["cancel_requested_at"] is None else "cancel-requested",
        }
    updated = _mutate(conn, job, {"state": "running"})
    _record_event(conn, updated, "running")
    return 200, {"started": True, "job": _job_payload(updated)}


def _renew(
    conn: sqlite3.Connection,
    request: dict[str, Any],
    policy: dict[str, Any],
    config: fleet_command_deck.DeckConfig,
) -> tuple[int, dict[str, Any]]:
    job = _require_live_holder(conn, request, policy)
    if job is None:
        return 409, {"renewed": False, "error": "lease-conflict"}
    instant = datetime.now(timezone.utc)
    expires = min(instant + timedelta(seconds=request["lease_seconds"]), _deadline(job))
    updated = _mutate(conn, job, {"lease_expires_at": _format_ts(expires)})
    if not _renew_capacity(conn, updated, request, config):
        return 409, {"renewed": False, "error": "lease-conflict"}
    _record_event(conn, updated, "renewed")
    return 200, {"renewed": True, "job": _job_payload(updated)}


def _complete(
    conn: sqlite3.Connection,
    request: dict[str, Any],
    policy: dict[str, Any],
    config: fleet_command_deck.DeckConfig,
) -> tuple[int, dict[str, Any]]:
    job = _require_live_holder(conn, request, policy)
    if job is None:
        return 409, {"completed": False, "error": "lease-conflict"}
    artifact = request["artifact"]
    allowed_states = {"claimed", "running"} if artifact.get("kind") == "report" else {"running"}
    if job["state"] not in allowed_states or job["cancel_requested_at"] is not None:
        return 409, {
            "completed": False,
            "error": "invalid-state" if job["cancel_requested_at"] is None else "cancel-requested",
        }
    if artifact["kind"] != job["artifact_kind"]:
        return 409, {"completed": False, "error": "artifact-mismatch"}
    updated = _mutate(
        conn,
        job,
        {
            "state": "completed",
            "artifact_ref": artifact.get("ref"),
            "artifact_digest": artifact.get("digest"),
            "artifact_size": artifact.get("size"),
            "private_snapshot_id": artifact.get("private_snapshot_id") or job["private_snapshot_id"],
        },
    )
    _release_capacity(conn, updated, config, state="completed", artifact_ref=artifact.get("ref"))
    _record_event(conn, updated, "completed")
    return 200, {"completed": True, "job": _job_payload(updated)}


def _fail(
    conn: sqlite3.Connection,
    request: dict[str, Any],
    policy: dict[str, Any],
    config: fleet_command_deck.DeckConfig,
) -> tuple[int, dict[str, Any]]:
    job = _require_live_holder(conn, request, policy)
    if job is None:
        return 409, {"failed": False, "error": "lease-conflict"}
    if job["state"] not in {"claimed", "running"}:
        return 409, {"failed": False, "error": "invalid-state"}
    updated = _mutate(conn, job, {"state": "failed"})
    _release_capacity(conn, updated, config, state="failed")
    _record_event(conn, updated, "failed")
    return 200, {"failed": True, "job": _job_payload(updated)}


def _cancel(
    conn: sqlite3.Connection,
    request: dict[str, Any],
    policy: dict[str, Any],
    config: fleet_command_deck.DeckConfig,
) -> tuple[int, dict[str, Any]]:
    del config
    job = _scoped_job(conn, request["job_id"], policy)
    if job["state"] in TERMINAL_STATES:
        return 200, {"canceled": True, "job": _job_payload(job)}
    if job["state"] == "queued":
        updated = _mutate(conn, job, {"state": "canceled"})
        _record_event(conn, updated, "canceled")
        return 200, {"canceled": True, "job": _job_payload(updated)}
    if job["cancel_requested_at"] is not None:
        return 200, {"canceled": True, "job": _job_payload(job)}
    updated = _mutate(conn, job, {"cancel_requested_at": _now_iso()})
    _record_event(conn, updated, "cancel-requested")
    return 200, {"canceled": True, "job": _job_payload(updated)}


def _expire(
    conn: sqlite3.Connection,
    request: dict[str, Any],
    policy: dict[str, Any],
    config: fleet_command_deck.DeckConfig,
) -> tuple[int, dict[str, Any]]:
    job = _scoped_job(conn, request["job_id"], policy)
    if job["state"] in TERMINAL_STATES:
        return 200, {"expired": True, "job": _job_payload(job)}
    instant = datetime.now(timezone.utc)
    deadline = _deadline(job)
    expires_at = deadline if job["state"] == "queued" else min(_parse_ts(job["lease_expires_at"]) or deadline, deadline)
    if instant < expires_at:
        return 200, {"expired": False, "job": _job_payload(job)}
    updated = _mark_expired(conn, job, config, record_operation=False)
    return 200, {"expired": True, "job": _job_payload(updated)}


def _ack_cancel(
    conn: sqlite3.Connection,
    request: dict[str, Any],
    policy: dict[str, Any],
    config: fleet_command_deck.DeckConfig,
) -> tuple[int, dict[str, Any]]:
    job = _require_live_holder(conn, request, policy)
    if job is None:
        return 409, {"acknowledged": False, "error": "lease-conflict"}
    if job["cancel_requested_at"] is None:
        return 409, {"acknowledged": False, "error": "cancellation-not-requested"}
    updated = _mutate(conn, job, {"state": "canceled"})
    _release_capacity(conn, updated, config, state="canceled")
    _record_event(conn, updated, "cancel-acknowledged")
    return 200, {"acknowledged": True, "job": _job_payload(updated)}


def _mark_expired(
    conn: sqlite3.Connection,
    job: dict[str, Any],
    config: fleet_command_deck.DeckConfig,
    *,
    record_operation: bool = True,
) -> dict[str, Any]:
    updated = _mutate(conn, job, {"state": "expired"})
    _release_capacity(conn, updated, config, state="expired")
    _record_event(conn, updated, "expired")
    if record_operation:
        _record_deadline_expire_operation(conn, job, updated)
    return updated


def _record_deadline_expire_operation(conn: sqlite3.Connection, job: dict[str, Any], updated: dict[str, Any]) -> None:
    """Leave the audit row an operator ``expire`` would have left.

    The operation id is namespaced so it can never collide with an operator or
    CLI ``expire:<job_id>:<revision>`` id and turn a later replay into a
    spurious ``operation-mismatch``. ``actor_node_id`` stays NULL: no actor
    asked for this, the deadline did.
    """
    operation_id = f"{SWEEP_OPERATION_PREFIX}:{job['job_id']}:{int(job['item_revision'])}"
    payload = {"expired": True, "job": _job_payload(updated)}
    digest_source = {
        "action": "expire",
        "job_id": job["job_id"],
        "operation_id": operation_id,
        "expected_item_revision": int(job["item_revision"]),
    }
    conn.execute(
        "INSERT OR IGNORE INTO grokbot_operations "
        "(job_id, operation_id, action, request_digest, result_revision, result_state, result_json, timestamp, "
        "actor_node_id, queue_id, queue_owner_node_id) "
        "VALUES (?, ?, 'expire', ?, ?, 'expired', ?, ?, NULL, ?, ?)",
        (
            job["job_id"],
            operation_id,
            hashlib.sha256(
                json.dumps(digest_source, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest(),
            updated["item_revision"],
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            _now_iso(),
            job.get("queue_id"),
            job.get("owner_node"),
        ),
    )


def _refuse_unscoped_jobs(conn: sqlite3.Connection) -> None:
    row = conn.execute("SELECT 1 FROM grokbot_jobs WHERE queue_id IS NULL LIMIT 1").fetchone()
    if row is not None:
        raise FleetHubError("grokbot jobs with missing queue scope must be reconciled by an admin")


def _commit_stale_sweep(
    conn: sqlite3.Connection,
    config: fleet_command_deck.DeckConfig,
    *,
    skip_job_id: str | None = None,
) -> None:
    conn.execute("BEGIN IMMEDIATE")
    try:
        _expire_stale_jobs(conn, fleet_hub._now_epoch(), config, skip_job_id=skip_job_id)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _expire_stale_jobs(
    conn: sqlite3.Connection,
    now_epoch: float,
    config: fleet_command_deck.DeckConfig,
    *,
    skip_job_id: str | None = None,
) -> list[str]:
    now = datetime.fromtimestamp(now_epoch, tz=timezone.utc)
    rows = conn.execute(
        f"SELECT {_JOB_COLUMNS} FROM grokbot_jobs WHERE state IN ('queued', 'claimed', 'running')"
    ).fetchall()
    expired: list[str] = []
    for row in rows:
        job = _job_dict(row)
        if skip_job_id is not None and job["job_id"] == skip_job_id:
            continue
        deadline = _deadline(job)
        if job["state"] == "queued":
            if now >= deadline:
                _mark_expired(conn, job, config)
                expired.append(job["job_id"])
            continue
        expires = min(_parse_ts(job["lease_expires_at"]) or deadline, deadline)
        if now >= expires:
            _mark_expired(conn, job, config)
            expired.append(job["job_id"])
    return expired


def _admit_capacity(
    conn: sqlite3.Connection,
    job: dict[str, Any],
    request: dict[str, Any],
    policy: dict[str, Any],
    lease_digest: str,
    config: fleet_command_deck.DeckConfig,
) -> tuple[bool, str]:
    now = fleet_hub._now_epoch()
    fleet_hub._expire_cloud_leases(conn, now)
    lease_id = job["job_id"]
    existing = fleet_hub._fetch_cloud_lease(conn, lease_id)
    holder = _cloud_holder_digest(job["job_id"], lease_digest)
    node = policy["node_id"]
    if existing is not None:
        if existing[14] == holder and existing[5] == node and existing[13] is None and existing[11] > now:
            return True, "ok"
        return False, "cloud-lease-held"
    policy_row = fleet_hub._cloud_policy(conn, config)
    provider = "grok-bot"
    provider_policy = policy_row["providers"].get(provider, fleet_hub._provider_defaults(config, provider))
    if not provider_policy["enabled"] or provider_policy.get("circuit_state") == "open":
        return False, provider_policy.get("reason") or "provider disabled by policy"
    hosted_count, provider_counts = fleet_hub._active_cloud_counts(conn, policy_row)
    if provider_counts.get(provider, 0) >= provider_policy["limit"]:
        return False, "provider cloud capacity is exhausted"
    if provider_policy["hosted"] and hosted_count >= policy_row["global_limit"]:
        return False, "global hosted cloud capacity is exhausted"
    now_iso = fleet_hub._epoch_to_iso(now)
    conn.execute(
        "INSERT INTO cloud_leases (lease_id, provider, provider_task_id, repo, label, prompt_hash, "
        "owner_node, owner_conductor, holder_token, state, admitted_at, renewed_at, ttl_seconds, "
        "expires_at, artifact_ref, released_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'bound', ?, ?, ?, ?, NULL, NULL)",
        (
            lease_id,
            provider,
            lease_id,
            job["repository"],
            job["label"],
            job["task_digest"],
            node,
            policy["actor_kind"],
            holder,
            now_iso,
            now_iso,
            request["lease_seconds"],
            now + request["lease_seconds"],
        ),
    )
    return True, "ok"


def _renew_capacity(
    conn: sqlite3.Connection,
    job: dict[str, Any],
    request: dict[str, Any],
    config: fleet_command_deck.DeckConfig,
) -> bool:
    del config
    now = fleet_hub._now_epoch()
    now_iso = fleet_hub._epoch_to_iso(now)
    holder = _cloud_holder_digest(job["job_id"], job["lease_token_digest"] or "")
    cursor = conn.execute(
        "UPDATE cloud_leases SET renewed_at=?, ttl_seconds=?, expires_at=? "
        "WHERE lease_id=? AND holder_token=? AND released_at IS NULL AND expires_at > ?",
        (now_iso, request["lease_seconds"], now + request["lease_seconds"], job["job_id"], holder, now),
    )
    return cursor.rowcount == 1


def _release_capacity(
    conn: sqlite3.Connection,
    job: dict[str, Any],
    config: fleet_command_deck.DeckConfig,
    *,
    state: str,
    artifact_ref: str | None = None,
) -> None:
    del config
    digest = job.get("lease_token_digest")
    if not digest:
        return
    holder = _cloud_holder_digest(job["job_id"], digest)
    conn.execute(
        "UPDATE cloud_leases SET state=?, released_at=?, artifact_ref=COALESCE(?, artifact_ref) "
        "WHERE lease_id=? AND holder_token=? AND released_at IS NULL",
        (state, _now_iso(), artifact_ref, job["job_id"], holder),
    )


def _mutate(conn: sqlite3.Connection, job: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    updates = dict(fields)
    updates["updated_at"] = _now_iso()
    updates["item_revision"] = int(job["item_revision"]) + 1
    updates["sequence"] = int(job["sequence"]) + 1
    assignments = ", ".join(f"{column}=?" for column in updates)
    cursor = conn.execute(
        f"UPDATE grokbot_jobs SET {assignments} WHERE job_id=? AND item_revision=?",
        (*updates.values(), job["job_id"], job["item_revision"]),
    )
    if cursor.rowcount != 1:
        raise FleetHubConflict("grokbot revision-conflict")
    return _require_job(conn, job["job_id"])


def _record_event(conn: sqlite3.Connection, job: dict[str, Any], kind: str) -> None:
    payload = _job_payload(job)
    revision = payload.get("item_revision")
    if type(revision) is not int or revision != payload.get("sequence"):
        raise FleetHubError("grokbot item_revision must equal sequence")
    event_sequence = revision
    event_state = EVENT_STATES[kind]
    if conn.execute(
        "SELECT 1 FROM events WHERE run_id=? AND harness='grokbot' AND sequence=?",
        (payload["job_id"], event_sequence),
    ).fetchone():
        return
    digest_source = {
        "job_id": payload["job_id"],
        "role": payload["role"],
        "harness": "grokbot",
        "item_revision": event_sequence,
        "sequence": event_sequence,
        "task_digest": payload["task_digest"],
        "node_id": payload.get("claimant_node") or payload.get("owner_node"),
        "worker": payload.get("claimant_worker"),
        "artifact_kind": payload.get("artifact_kind"),
        "artifact_ref": payload.get("artifact_ref"),
        "artifact_digest": payload.get("artifact_digest"),
        "artifact_size": payload.get("artifact_size"),
        "private_snapshot_id": payload.get("private_snapshot_id"),
        "state": event_state,
    }
    digest = hashlib.sha256(
        json.dumps(digest_source, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO events "
        "(node_id, run_id, sequence, digest, repo, seat, harness, state, ts, exit_status, "
        "capability_fingerprint, received_at) VALUES (?, ?, ?, ?, ?, ?, 'grokbot', ?, ?, NULL, NULL, ?)",
        (
            digest_source["node_id"] or "grokbot",
            payload["job_id"],
            event_sequence,
            digest,
            payload["repository"],
            payload["role"],
            event_state,
            payload["updated_at"],
            payload["updated_at"],
        ),
    )


def _replay_operation(conn: sqlite3.Connection, request: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    if request["action"] not in MUTATING_ACTIONS:
        return None, False
    row = conn.execute(
        "SELECT request_digest, result_json, actor_node_id, queue_id, queue_owner_node_id "
        "FROM grokbot_operations WHERE job_id=? AND operation_id=?",
        (request["job_id"], request["operation_id"]),
    ).fetchone()
    if row is None:
        return None, False
    digest = _request_digest(request)
    if not hmac.compare_digest(row[0], digest):
        return None, True
    if row[2] not in (None, request["node_id"]):
        return None, True
    if row[3] not in (None, request["queue_id"]):
        return None, True
    if row[4] not in (None, request["queue_owner_node_id"]):
        return None, True
    payload = json.loads(row[1])
    if request["action"] == "enqueue" and payload.get("enqueued") is True:
        payload["idempotent"] = True
    return payload, False


def _store_operation(conn: sqlite3.Connection, request: dict[str, Any], payload: dict[str, Any]) -> None:
    if request["action"] not in MUTATING_ACTIONS:
        return
    job = payload.get("job")
    if not isinstance(job, dict):
        return
    conn.execute(
        "INSERT OR IGNORE INTO grokbot_operations "
        "(job_id, operation_id, action, request_digest, result_revision, result_state, result_json, timestamp, "
        "actor_node_id, queue_id, queue_owner_node_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            request["job_id"],
            request["operation_id"],
            request["action"],
            _request_digest(request),
            job.get("item_revision"),
            job.get("state"),
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            _now_iso(),
            request["node_id"],
            request["queue_id"],
            request["queue_owner_node_id"],
        ),
    )


def _request_digest(request: dict[str, Any]) -> str:
    safe = {key: value for key, value in request.items() if key != "lease_id"}
    if "lease_id" in request:
        safe["lease_token_digest"] = _lease_digest(request["lease_id"])
    return hashlib.sha256(json.dumps(safe, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _require_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any]:
    row = conn.execute(f"SELECT {_JOB_COLUMNS} FROM grokbot_jobs WHERE job_id = ?", (job_id,)).fetchone()
    if row is None:
        raise FleetHubError("job-not-found")
    return _job_dict(row)


def _require_live_holder(
    conn: sqlite3.Connection, request: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any] | None:
    job = _scoped_job(conn, request["job_id"], policy)
    if job["state"] in TERMINAL_STATES or job["state"] not in {"claimed", "running"}:
        return None
    presented = _lease_digest(request["lease_id"])
    if not _digests_match(job.get("lease_token_digest"), presented):
        return None
    if job.get("claimant_node") != policy["node_id"]:
        return None
    if int(job["lease_generation"] or 0) != request["lease_generation"]:
        return None
    if not _lease_live(job, datetime.now(timezone.utc)):
        return None
    return job


def _lease_live(job: dict[str, Any], instant: datetime) -> bool:
    expires = _parse_ts(job.get("lease_expires_at"))
    if expires is None:
        return False
    return instant < min(expires, _deadline(job))


def _deadline(job: dict[str, Any]) -> datetime:
    start = _parse_ts(job.get("queued_at") or job.get("created_at")) or datetime.now(timezone.utc)
    return start + timedelta(seconds=int(job["timeout_seconds"]))


def _job_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return dict(zip(JOB_FIELDS, row, strict=True))


def _job_payload(job: dict[str, Any]) -> dict[str, Any]:
    payload = {key: job.get(key) for key in SAFE_JOB_FIELDS if key != "harness"}
    payload["harness"] = "grokbot"
    return {key: value for key, value in payload.items() if value is not None}


def _validate_request(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise FleetHubError("grokbot request must be a JSON object")
    action = raw.get("action")
    if action not in ACTIONS:
        raise FleetHubError("grokbot field 'action' is invalid")
    forbidden = FORBIDDEN_REQUEST_KEYS
    if action == "enroll-actor":
        forbidden = FORBIDDEN_REQUEST_KEYS - {"queue_id", "queue_owner_node_id", "actor_kind"}
    unknown_sensitive = forbidden.intersection(raw)
    if unknown_sensitive:
        raise FleetHubError("grokbot request must not include private task, identity, or credential fields")
    request: dict[str, Any] = {"action": action}
    if action == "enroll-actor":
        return _validate_enroll(raw, request)
    if "role" in raw:
        if action not in {"enqueue", "list"}:
            raise FleetHubError("grokbot field 'role' is not accepted for this action")
        role = raw.get("role")
        if role not in ROLE_VALUES:
            raise FleetHubError("grokbot field 'role' is invalid")
        request["role"] = role
    if action == "list":
        if raw.get("include_all") not in (None, True, False):
            raise FleetHubError("grokbot field 'include_all' must be a boolean")
        request["include_all"] = bool(raw.get("include_all"))
        return request
    if action == "whoami":
        return request
    if action != "enqueue":
        job_id = raw.get("job_id")
        if not isinstance(job_id, str) or not GROKBOT_JOB_ID_RE.fullmatch(job_id):
            raise FleetHubError("grokbot field 'job_id' is invalid")
        request["job_id"] = job_id
    if action in {"status", "report-metadata"}:
        return request
    request["operation_id"] = _opaque(raw.get("operation_id"), "operation_id")
    if action != "enqueue":
        revision = raw.get("expected_item_revision")
        if type(revision) is not int or revision < 1:
            raise FleetHubError("grokbot field 'expected_item_revision' must be a positive integer")
        request["expected_item_revision"] = revision
    if action in {"claim", *LEASE_ACTIONS}:
        request["lease_id"] = _opaque(raw.get("lease_id"), "lease_id")
    if action in LEASE_ACTIONS:
        generation = raw.get("lease_generation")
        if type(generation) is not int or generation < 1:
            raise FleetHubError("grokbot field 'lease_generation' must be a positive integer")
        request["lease_generation"] = generation
    if action in {"claim", "renew"}:
        lease_seconds = raw.get("lease_seconds", DEFAULT_LEASE_SECONDS)
        if type(lease_seconds) is not int or not LEASE_SECONDS_MIN <= lease_seconds <= LEASE_SECONDS_MAX:
            raise FleetHubError("grokbot field 'lease_seconds' is invalid")
        request["lease_seconds"] = lease_seconds
    if action == "enqueue":
        return _validate_enqueue(raw, request)
    if action == "complete":
        request["artifact"] = _validate_artifact_metadata(raw.get("artifact"))
    return request


def _validate_enroll(raw: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    request["enroll_node_id"] = fleet_hub._validate_node_id(raw.get("enroll_node_id"))
    request["queue_owner_node_id"] = fleet_hub._validate_node_id(raw.get("queue_owner_node_id"))
    request["queue_id"] = _opaque(raw.get("queue_id"), "queue_id")
    kind = raw.get("actor_kind")
    if kind not in ACTOR_KINDS:
        raise FleetHubError("grokbot field 'actor_kind' is invalid")
    request["actor_kind"] = kind
    role = raw.get("role")
    if kind in ROLE_VALUES:
        if role != kind:
            raise FleetHubError("worker actor role must match actor_kind")
        request["role"] = role
    elif role is not None:
        raise FleetHubError("non-worker actors do not carry a job role")
    if raw.get("enabled") not in (None, True, False):
        raise FleetHubError("grokbot field 'enabled' must be a boolean")
    request["enabled"] = raw.get("enabled", True) is not False
    return request


def _validate_enqueue(raw: dict[str, Any], request: dict[str, Any]) -> dict[str, Any]:
    job_id = raw.get("job_id")
    if not isinstance(job_id, str) or not GROKBOT_JOB_ID_RE.fullmatch(job_id):
        raise FleetHubError("grokbot field 'job_id' is invalid")
    role = raw.get("role")
    if role not in ROLE_VALUES:
        raise FleetHubError("grokbot field 'role' is invalid")
    repository = _bounded_text(raw.get("repository"), "repository", 200)
    try:
        validate_repository(repository)
    except GrokbotJobError as exc:
        raise FleetHubError("grokbot field 'repository' is invalid") from exc
    label = _bounded_text(raw.get("label"), "label", 160)
    digest = raw.get("task_digest")
    if not isinstance(digest, str) or not TASK_DIGEST_RE.fullmatch(digest):
        raise FleetHubError("grokbot field 'task_digest' is invalid")
    key_hash = raw.get("idempotency_key_hash")
    if not isinstance(key_hash, str) or not TASK_DIGEST_RE.fullmatch(key_hash):
        raise FleetHubError("grokbot field 'idempotency_key_hash' is invalid")
    timeout = raw.get("timeout_seconds")
    if type(timeout) is not int or not 60 <= timeout <= 14400:
        raise FleetHubError("grokbot field 'timeout_seconds' is invalid")
    kind = raw.get("artifact_kind")
    if kind not in ARTIFACT_KINDS:
        raise FleetHubError("grokbot field 'artifact_kind' is invalid")
    snapshot = raw.get("private_snapshot_id")
    if snapshot is not None:
        snapshot = _bounded_text(snapshot, "private_snapshot_id", 128)
    request.update(
        {
            "job_id": job_id,
            "role": role,
            "repository": repository,
            "label": label,
            "task_digest": digest,
            "idempotency_key_hash": key_hash,
            "timeout_seconds": timeout,
            "artifact_kind": kind,
            "private_snapshot_id": snapshot,
        }
    )
    return request


def _validate_artifact_metadata(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise FleetHubError("grokbot field 'artifact' must be an object")
    kind = raw.get("kind")
    if kind not in ARTIFACT_KINDS:
        raise FleetHubError("grokbot artifact kind is invalid")
    artifact: dict[str, Any] = {"kind": kind}
    if kind == "draft-pr":
        ref = raw.get("ref")
        if not isinstance(ref, str) or not GITHUB_PULL_URL_RE.fullmatch(ref):
            raise FleetHubError("grokbot draft-pr artifact ref is invalid")
        artifact["ref"] = ref
    elif kind == "branch":
        ref = raw.get("ref")
        digest = raw.get("digest")
        if not isinstance(ref, str) or not BRANCH_RE.fullmatch(ref) or ".." in ref or ref.endswith((".", "/")):
            raise FleetHubError("grokbot branch artifact ref is invalid")
        if not isinstance(digest, str) or not COMMIT_DIGEST_RE.fullmatch(digest):
            raise FleetHubError("grokbot branch artifact digest is invalid")
        artifact["ref"] = ref
        artifact["digest"] = digest
    else:
        if raw.get("ref") is not None:
            raise FleetHubError("grokbot report artifact must not include a local path ref")
        snapshot = _bounded_text(raw.get("private_snapshot_id"), "private_snapshot_id", 128)
        digest = raw.get("digest")
        size = raw.get("size")
        if not isinstance(digest, str) or not TASK_DIGEST_RE.fullmatch(digest):
            raise FleetHubError("grokbot report artifact digest is invalid")
        if type(size) is not int or size < 0 or size > 65_536:
            raise FleetHubError("grokbot artifact size is invalid")
        artifact["private_snapshot_id"] = snapshot
        artifact["digest"] = digest
        artifact["size"] = size
        return artifact
    snapshot_id = raw.get("private_snapshot_id")
    if snapshot_id is not None:
        artifact["private_snapshot_id"] = _bounded_text(snapshot_id, "private_snapshot_id", 128)
    size = raw.get("size")
    if size is not None:
        if type(size) is not int or size < 0 or size > 65_536:
            raise FleetHubError("grokbot artifact size is invalid")
        artifact["size"] = size
    return artifact


def _opaque(value: Any, field: str) -> str:
    if not isinstance(value, str) or not OPAQUE_ID_RE.fullmatch(value):
        raise FleetHubError(f"grokbot field {field!r} is invalid")
    fleet_hub._reject_controls(value, field, kind="grokbot")
    return value


def _bounded_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise FleetHubError(f"grokbot field {field!r} is invalid")
    fleet_hub._reject_controls(value, field, kind="grokbot")
    return value.strip()


def _lease_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cloud_holder_digest(job_id: str, lease_digest: str) -> str:
    return hmac.new(_CLOUD_HOLDER_DOMAIN, f"{job_id}:{lease_digest}".encode("utf-8"), hashlib.sha256).hexdigest()


def _digests_match(stored: object, presented: str) -> bool:
    if not isinstance(stored, str) or not stored:
        return False
    return hmac.compare_digest(stored, presented)


def _result_flag(action: str) -> str:
    return {
        "enqueue": "enqueued",
        "claim": "claimed",
        "start": "started",
        "renew": "renewed",
        "complete": "completed",
        "fail": "failed",
        "cancel": "canceled",
        "expire": "expired",
        "ack-cancel": "acknowledged",
    }[action]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _format_ts(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
