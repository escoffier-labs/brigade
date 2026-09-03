"""Private, handle-first queue storage for Grok Bot jobs.

This module deliberately has no provider or transport integration. It validates
and persists private job envelopes, then exposes only safe job projections.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .grokbot_job_clock import format_timestamp as _format_timestamp
from .grokbot_job_clock import parse_timestamp as _parse_timestamp
from .grokbot_job_clock import timestamp as _timestamp
from .grokbot_job_validation import (
    JOB_ID_RE,
    LOWER_HEX_64_RE as LOWER_HEX_64_RE,
    OPAQUE_ID_RE as OPAQUE_ID_RE,
    TASK_HASH_RE,
    GrokbotJobError,
    bounded_string as _bounded_string,
    validate_artifact as _validate_artifact,
    validate_base_ref as _validate_base_ref,
    validate_commands as _validate_commands,
    validate_completion_artifact as _validate_completion_artifact,
    validate_idempotency_key as _validate_idempotency_key,
    validate_job_id as _validate_job_id,
    validate_lease_seconds as _validate_lease_seconds,
    validate_opaque_id as _validate_opaque_id,
    validate_ownership_paths as _validate_ownership_paths,
    validate_repository as _validate_repository,
)

try:  # pragma: no cover - Windows does not provide fcntl.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows.
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - unavailable on POSIX.
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX.
    msvcrt = None  # type: ignore[assignment]


JOB_STATES = frozenset({"queued", "claimed", "running", "completed", "failed", "expired", "canceled"})
ROLES = frozenset({"implementation-worker", "repository-scout"})
REQUIRED_ENVELOPE_KEYS = frozenset(
    {
        "label",
        "role",
        "repository",
        "base_ref",
        "ownership_paths",
        "instructions",
        "verification_commands",
        "artifact",
        "timeout_seconds",
    }
)
OPTIONAL_ENVELOPE_KEYS = frozenset({"queue_ttl_seconds"})
# ``timeout_seconds`` is the execution budget, counted from ``claimed_at``.
# ``queue_ttl_seconds`` bounds the wait before a claim, so a job that queued
# behind other work is not charged for that wait (#1403).
DEFAULT_QUEUE_TTL_SECONDS = 86400
QUEUE_TTL_SECONDS_MIN = 60
QUEUE_TTL_SECONDS_MAX = 604800
SENSITIVE_KEY_TOKENS = (
    "token",
    "secret",
    "password",
    "credential",
    "authorization",
    "header",
    "cookie",
    "environment",
    "env",
)
ROOT_MODE = 0o700
FILE_MODE = 0o600
MAX_REPORT_BYTES = 12000
JOB_SCHEMA = "brigade.grokbot.job.v1"
IDEMPOTENCY_SCHEMA = "brigade.grokbot.idempotency.v1"
SNAPSHOT_SCHEMA = "brigade.grokbot.snapshot.v1"
AUTHORITY_SCHEMA = "brigade.grokbot.authority.v1"
AUTHORITY_MARKER_NAME = "authority.json"
ACTIVE_LIFECYCLE_STATES = frozenset({"queued", "claimed", "running"})
# Enqueue refusals that mean the hub already recorded this key or operation, so
# the derived job id is the hub's and its private snapshot must survive.
HUB_DUPLICATE_REFUSALS = frozenset({"idempotency-conflict", "operation-mismatch"})


@dataclass(frozen=True)
class _Directory:
    path: Path
    descriptor: int | None


@dataclass(frozen=True)
class _Storage:
    root: _Directory
    jobs: _Directory
    idempotency: _Directory
    artifacts: _Directory
    snapshots: _Directory | None = None


def hub_authority(target: Path | None = None) -> bool:
    """True when Fleet Hub is the sole queue authority.

    A local authority marker is irreversible: once written, a hub outage or
    missing URL cannot fall back to local lifecycle.
    """
    if target is not None and authority_marker_present(target):
        return True
    try:
        from . import fleet_client_grokbot

        return fleet_client_grokbot.hub_configured()
    except Exception:
        return True


def authority_marker_path(target: Path) -> Path:
    return Path(target).expanduser() / ".brigade" / "cloud" / "grokbot" / AUTHORITY_MARKER_NAME


def authority_marker_present(target: Path) -> bool:
    path = authority_marker_path(target)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return True
    return isinstance(payload, dict) and payload.get("schema") == AUTHORITY_SCHEMA


def admit_hub_authority(target: Path) -> dict[str, Any]:
    """Write the irreversible hub-authority marker after a safe admission check.

    Active local lifecycle rows are refused. The marker is not written on
    conflict. Remaining operator migration of local history is an explicit
    leftover, not automatic requeue.
    """
    if authority_marker_present(target):
        return {"admitted": True, "idempotent": True, "legacy_blocked": False}
    blocked = _active_legacy_job_ids(target)
    if blocked:
        raise GrokbotJobError("legacy-active-needs-reconcile")
    _write_authority_marker(target)
    return {"admitted": True, "idempotent": False, "legacy_blocked": False}


def _write_authority_marker(target: Path) -> None:
    payload = {"schema": AUTHORITY_SCHEMA, "admitted_at": _now_iso(None)}
    with _storage_paths(target) as storage, _queue_lock(storage):
        _write_json_file(storage.root, AUTHORITY_MARKER_NAME, payload)


def _active_legacy_job_ids(target: Path) -> list[str]:
    root = Path(target).expanduser() / ".brigade" / "cloud" / "grokbot" / "jobs"
    if not root.exists():
        return []
    blocked: list[str] = []
    with _storage_paths(target) as storage:
        for name in _list_names(storage.jobs, prefix="grokbot-", suffix=".json"):
            record = _read_json_file(storage.jobs, name)
            if record is None:
                continue
            try:
                validated = _validate_record(record)
            except GrokbotJobError:
                blocked.append(name)
                continue
            if validated["state"] in ACTIVE_LIFECYCLE_STATES:
                blocked.append(validated["job_id"])
    return blocked


def enqueue(
    target: Path,
    spec: dict[str, Any],
    idempotency_key: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Queue a validated private envelope and return an opaque handle."""
    if hub_authority(target):
        return _enqueue_via_hub(target, spec, idempotency_key, now)
    envelope = _validate_spec(spec)
    key = _validate_idempotency_key(idempotency_key)
    with _storage_paths(target) as storage, _queue_lock(storage):
        return _enqueue_locked(storage, envelope, key, now)


def enqueue_repository_scout(
    target: Path,
    spec: dict[str, Any],
    idempotency_key: str,
    *,
    daily_limit: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Atomically admit one Repository Scout under its active and daily limits."""
    return _enqueue_under_limits(
        target,
        spec,
        idempotency_key,
        role="repository-scout",
        active_reason="active-scout",
        daily_limit=daily_limit,
        now=now,
    )


def enqueue_implementation_worker(
    target: Path,
    spec: dict[str, Any],
    idempotency_key: str,
    *,
    daily_limit: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Atomically admit one Implementation Worker under its active and daily limits."""
    return _enqueue_under_limits(
        target,
        spec,
        idempotency_key,
        role="implementation-worker",
        active_reason="active-implementation-worker",
        daily_limit=daily_limit,
        now=now,
    )


def _enqueue_under_limits(
    target: Path,
    spec: dict[str, Any],
    idempotency_key: str,
    *,
    role: str,
    active_reason: str,
    daily_limit: int,
    now: datetime | None,
) -> dict[str, Any]:
    """Admit one job for a role after rechecking its bounds under queue authority."""
    envelope = _validate_spec(spec)
    if envelope["role"] != role:
        raise GrokbotJobError("invalid-role")
    key = _validate_idempotency_key(idempotency_key)
    if type(daily_limit) is not int or daily_limit < 1:
        raise GrokbotJobError("invalid-daily-limit")
    _, instant = _timestamp(now)
    if hub_authority(target):
        return _enqueue_under_limits_via_hub(target, envelope, key, role, active_reason, daily_limit, instant)
    with _storage_paths(target) as storage, _queue_lock(storage):
        role_records = _role_records_locked(storage, role)
        created_today = sum(_parse_timestamp(record["created_at"]).date() == instant.date() for record in role_records)
        if any(_is_active_role_record(record) for record in role_records):
            return {"reason": active_reason, "created_today": created_today, "handle": None}
        if created_today >= daily_limit:
            return {"reason": "daily-limit-reached", "created_today": created_today, "handle": None}
        handle = _enqueue_locked(storage, envelope, key, instant)
        if handle["idempotent"]:
            return {"reason": "all-known", "created_today": created_today, "handle": handle}
        return {"reason": "created", "created_today": created_today, "handle": handle}


def _enqueue_locked(storage: _Storage, envelope: dict[str, Any], key: str, now: datetime | None) -> dict[str, Any]:
    """Persist one validated queue envelope while the caller holds the queue lock."""
    task_hash = _task_hash(envelope)
    key_hash = _idempotency_key_hash(key)
    idempotency_name = f"{key_hash.removeprefix('sha256:')}.json"
    existing = _read_json_file(storage.idempotency, idempotency_name, missing_ok=True)
    if existing is not None:
        idempotency = _validate_idempotency_record(existing)
        if idempotency["task_hash"] != task_hash:
            raise GrokbotJobError("idempotency-conflict")
        job_id = idempotency["job_id"]
        record = _load_record(storage.jobs, job_id)
        if record["task_hash"] != task_hash:
            raise GrokbotJobError("corrupt-storage")
        return _handle(record, idempotent=True)

    recovered = _find_record_by_idempotency_hash(storage.jobs, key_hash)
    if recovered is not None:
        if recovered["task_hash"] != task_hash:
            raise GrokbotJobError("idempotency-conflict")
        _write_json_file(
            storage.idempotency,
            idempotency_name,
            {"schema": IDEMPOTENCY_SCHEMA, "job_id": recovered["job_id"], "task_hash": task_hash},
        )
        return _handle(recovered, idempotent=True)

    timestamp = _now_iso(now)
    job_id = f"grokbot-{secrets.token_hex(12)}"
    record = {
        "schema": JOB_SCHEMA,
        "job_id": job_id,
        "task_hash": task_hash,
        "idempotency_key_hash": key_hash,
        "state": "queued",
        "created_at": timestamp,
        "updated_at": timestamp,
        "queued_at": timestamp,
        "timeout_seconds": envelope["timeout_seconds"],
        "item_revision": 1,
        "spec": envelope,
    }
    _write_json_file(storage.jobs, f"{job_id}.json", record)
    _write_json_file(
        storage.idempotency,
        idempotency_name,
        {"schema": IDEMPOTENCY_SCHEMA, "job_id": job_id, "task_hash": task_hash},
    )
    return _handle(record, idempotent=False)


def _role_records_locked(storage: _Storage, role: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for name in _list_names(storage.jobs, prefix="grokbot-", suffix=".json"):
        payload = _read_json_file(storage.jobs, name)
        if payload is None:
            raise GrokbotJobError("corrupt-storage")
        record = _validate_record(payload)
        if record["spec"]["role"] == role:
            records.append(record)
    return records


def _is_active_role_record(record: dict[str, Any]) -> bool:
    return record["state"] in {"queued", "claimed", "running"} or (
        "cancel_requested_at" in record and record["state"] not in {"completed", "failed", "expired", "canceled"}
    )


def get_job(target: Path, job_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Return the safe projection for one job, never its private envelope.

    A read is also a deadline check (#1353): an elapsed job is terminalized
    here, not reported as still queued or running.
    """
    if hub_authority(target):
        return _hub_job(job_id)
    job_id = _validate_job_id(job_id)
    with _storage_paths(target) as storage, _queue_lock(storage):
        return _projection(_expire_if_elapsed(storage, _load_record(storage.jobs, job_id), now))


def status(target: Path, job_id: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """Return one job projection or all safe projections in deterministic order."""
    if job_id is not None:
        return get_job(target, job_id, now)
    if hub_authority(target):
        return {"jobs": _hub_jobs(include_all=True)}
    with _storage_paths(target) as storage, _queue_lock(storage):
        jobs: list[dict[str, Any]] = []
        for name in _list_names(storage.jobs, prefix="grokbot-", suffix=".json"):
            record = _read_json_file(storage.jobs, name)
            if record is None:
                raise GrokbotJobError("corrupt-storage")
            jobs.append(_projection(_expire_if_elapsed(storage, _validate_record(record), now)))
        return {"jobs": jobs}


def _status_from_storage(storage: _Storage) -> dict[str, Any]:
    """Return safe job projections through one already-anchored queue storage."""
    jobs: list[dict[str, Any]] = []
    for name in _list_names(storage.jobs, prefix="grokbot-", suffix=".json"):
        record = _read_json_file(storage.jobs, name)
        if record is None:
            raise GrokbotJobError("corrupt-storage")
        jobs.append(_projection(_validate_record(record)))
    return {"jobs": jobs}


def tracker_rows(target: Path) -> list[dict[str, Any]]:
    """Return the bounded queue fields that the cloud tracker may project.

    This deliberately differs from the queue CLI projection. The tracker needs
    completion artifact references to reconcile draft PRs, but it never needs
    the private envelope, lease holder, or verification commands.
    """
    if hub_authority(target):
        try:
            hub_jobs = _hub_jobs(include_all=True)
        except GrokbotJobError as exc:
            if exc.reason == "hub-unavailable":
                return [_hub_unavailable_row()]
            raise
        rows: list[dict[str, Any]] = []
        for job in hub_jobs:
            kind = job.get("artifact_kind", "report")
            row: dict[str, Any] = {
                "job_id": job["job_id"],
                "label": job.get("label"),
                "task_hash": job["task_hash"],
                "state": job["state"],
                "created_at": job["created_at"],
                "updated_at": job["updated_at"],
                "queued_at": job["queued_at"],
                "artifact": {"kind": kind},
            }
            if "claimed_at" in job:
                row["claimed_at"] = job["claimed_at"]
            completed = _hub_tracker_artifact(job, kind)
            if completed:
                row["result_artifact"] = completed
            rows.append(row)
        return rows
    root = Path(target).expanduser() / ".brigade" / "cloud" / "grokbot"
    if not root.exists():
        return []
    with _storage_paths(target) as storage:
        local_rows: list[dict[str, Any]] = []
        for name in _list_names(storage.jobs, prefix="grokbot-", suffix=".json"):
            record = _read_json_file(storage.jobs, name)
            if record is None:
                raise GrokbotJobError("corrupt-storage")
            valid = _validate_record(record)
            spec = valid["spec"]
            assert isinstance(spec, dict)
            local_row: dict[str, Any] = {
                "job_id": valid["job_id"],
                "label": spec["label"],
                "task_hash": valid["task_hash"],
                "state": valid["state"],
                "created_at": valid["created_at"],
                "updated_at": valid["updated_at"],
                "queued_at": valid["queued_at"],
                "artifact": spec["artifact"],
            }
            for key in ("claimed_at", "result_artifact"):
                if key in valid:
                    local_row[key] = valid[key]
            local_rows.append(local_row)
        return local_rows


def claim(
    target: Path,
    job_id: str,
    bot_id: str,
    lease_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Claim a queued job with one opaque bot lease."""
    return _claim_job(target, job_id, bot_id, lease_id, lease_seconds, now, include_context=False)


def claim_execution_context(
    target: Path,
    job_id: str,
    bot_id: str,
    lease_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Claim a queued job and return its validated worker context."""
    return _claim_job(target, job_id, bot_id, lease_id, lease_seconds, now, include_context=True)


def _claim_job(
    target: Path,
    job_id: str,
    bot_id: str,
    lease_id: str,
    lease_seconds: int,
    now: datetime | None,
    *,
    include_context: bool,
) -> dict[str, Any]:
    job_id = _validate_job_id(job_id)
    bot_id = _validate_opaque_id(bot_id, "invalid-bot-id")
    lease_id = _validate_opaque_id(lease_id, "invalid-lease-id")
    lease_seconds = _validate_lease_seconds(lease_seconds)
    if hub_authority(target):
        return _claim_via_hub(target, job_id, bot_id, lease_id, lease_seconds, include_context=include_context)
    timestamp, instant = _timestamp(now)
    with _storage_paths(target) as storage, _queue_lock(storage):
        record = _load_record(storage.jobs, job_id)
        if record["state"] == "claimed":
            if record["bot_id"] == bot_id and record["lease_id"] == lease_id:
                _require_live_lease(record, instant)
                return _claim_result(record, include_context=include_context)
            raise GrokbotJobError("lease-conflict")
        if record["state"] != "queued":
            _reject_nonqueued_claim(record)
        if instant >= _queue_deadline(record):
            _expire_if_elapsed(storage, record, instant)
            raise GrokbotJobError("job-expired")
        # The execution budget starts at the claim, so a job that waited in the
        # queue still gets its whole ``timeout_seconds`` to run (#1403).
        deadline = instant + timedelta(seconds=record["timeout_seconds"])
        record.update(
            {
                "state": "claimed",
                "claimed_at": timestamp,
                "lease_expires_at": _format_timestamp(min(instant + timedelta(seconds=lease_seconds), deadline)),
                "bot_id": bot_id,
                "lease_id": lease_id,
            }
        )
        _commit_mutation(storage.jobs, record, timestamp)
        return _claim_result(record, include_context=include_context)


def renew(
    target: Path,
    job_id: str,
    bot_id: str,
    lease_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Renew the current unexpired lease without changing state."""
    job_id = _validate_job_id(job_id)
    bot_id = _validate_opaque_id(bot_id, "invalid-bot-id")
    lease_id = _validate_opaque_id(lease_id, "invalid-lease-id")
    lease_seconds = _validate_lease_seconds(lease_seconds)
    if hub_authority(target):
        return _renew_via_hub(job_id, bot_id, lease_id, lease_seconds)
    timestamp, instant = _timestamp(now)
    with _storage_paths(target) as storage, _queue_lock(storage):
        record = _load_record(storage.jobs, job_id)
        _require_live_lease_or_expire(storage, record, bot_id, lease_id, now, instant)
        record["lease_expires_at"] = _format_timestamp(
            min(instant + timedelta(seconds=lease_seconds), _deadline(record))
        )
        _commit_mutation(storage.jobs, record, timestamp)
        return _projection(record)


def transition(
    target: Path,
    job_id: str,
    bot_id: str,
    lease_id: str,
    state: str,
    *,
    artifact: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Make one valid lease-holder lifecycle transition."""
    job_id = _validate_job_id(job_id)
    bot_id = _validate_opaque_id(bot_id, "invalid-bot-id")
    lease_id = _validate_opaque_id(lease_id, "invalid-lease-id")
    if state not in {"running", "completed", "failed"}:
        raise GrokbotJobError("invalid-transition")
    if hub_authority(target):
        return _transition_via_hub(job_id, bot_id, lease_id, state, artifact=artifact)
    timestamp, instant = _timestamp(now)
    with _storage_paths(target) as storage, _queue_lock(storage):
        record = _load_record(storage.jobs, job_id)
        _require_live_lease_or_expire(storage, record, bot_id, lease_id, now, instant)
        current = record["state"]
        if state == "running":
            if current != "claimed" or artifact is not None:
                raise GrokbotJobError("invalid-transition")
            if "cancel_requested_at" in record:
                raise GrokbotJobError("cancel-requested")
        elif state == "failed":
            if current not in {"claimed", "running"} or artifact is not None:
                raise GrokbotJobError("invalid-transition")
        else:
            record["result_artifact"] = _validated_completion(record, artifact)
            _unlink_artifact_file(storage.artifacts, f"{record['job_id']}.md")
        record["state"] = state
        _discard_orphan_report_snapshot(storage, record)
        _commit_mutation(storage.jobs, record, timestamp)
        return _projection(record)


def complete_report(
    target: Path,
    job_id: str,
    bot_id: str,
    lease_id: str,
    artifact: dict[str, Any],
    report_text: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Store verified Repository Scout report bytes, then complete the job."""
    job_id = _validate_job_id(job_id)
    bot_id = _validate_opaque_id(bot_id, "invalid-bot-id")
    lease_id = _validate_opaque_id(lease_id, "invalid-lease-id")
    report_bytes = _report_bytes(report_text)
    if hub_authority(target):
        return _complete_report_via_hub(target, job_id, bot_id, lease_id, artifact, report_bytes)
    timestamp, instant = _timestamp(now)
    with _storage_paths(target) as storage, _queue_lock(storage):
        record = _load_record(storage.jobs, job_id)
        _require_live_lease_or_expire(storage, record, bot_id, lease_id, now, instant)
        _require_report_job(record)
        validated = _validated_completion(record, artifact)
        if hashlib.sha256(report_bytes).hexdigest() != validated["sha256"]:
            raise GrokbotJobError("digest-mismatch")
        _write_bytes_file(storage.artifacts, f"{job_id}.md", report_bytes)
        record["result_artifact"] = validated
        record["state"] = "completed"
        _commit_mutation(storage.jobs, record, timestamp)
        return _projection(record)


def read_report(target: Path, job_id: str) -> dict[str, Any]:
    """Return verified snapshot bytes for one completed Repository Scout report."""
    job_id = _validate_job_id(job_id)
    if hub_authority(target):
        job = _hub_job(job_id)
        kind = job.get("artifact_kind") or (job.get("artifact") or {}).get("kind")
        if job["state"] != "completed" or kind != "report":
            raise GrokbotJobError("invalid-state")
        expected = job.get("artifact_digest")
        if not isinstance(expected, str) or not expected:
            raise GrokbotJobError("missing-digest")
        with _storage_paths(target) as storage:
            data = _read_bytes_file(
                storage.artifacts, f"{job_id}.md", maximum=MAX_REPORT_BYTES, missing_reason="report-missing"
            )
        return _verified_report(job_id, data, expected)
    with _storage_paths(target) as storage:
        return _read_report_from_storage(storage, job_id)


def _read_report_from_storage(storage: _Storage, job_id: str) -> dict[str, Any]:
    """Return one verified report through an already-anchored queue storage."""
    job_id = _validate_job_id(job_id)
    record = _load_record(storage.jobs, job_id)
    if record["state"] != "completed":
        raise GrokbotJobError("invalid-state")
    _require_report_job(record)
    data = _read_bytes_file(
        storage.artifacts, f"{job_id}.md", maximum=MAX_REPORT_BYTES, missing_reason="report-missing"
    )
    return _verified_report(job_id, data, record["result_artifact"]["sha256"])


def cancel(target: Path, job_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Cancel a queued job or request cooperative cancellation of a live lease."""
    job_id = _validate_job_id(job_id)
    if hub_authority(target):
        current = _hub_job(job_id)
        return _hub_projection(
            _require_hub(
                "cancel",
                job_id,
                expected_item_revision=current["item_revision"],
                operation_id=f"cancel:{job_id}:{current['item_revision']}",
            )
        )
    timestamp, _ = _timestamp(now)
    with _storage_paths(target) as storage, _queue_lock(storage):
        record = _load_record(storage.jobs, job_id)
        if record["state"] in {"completed", "failed", "expired", "canceled"}:
            _discard_orphan_report_snapshot(storage, record)
            return _projection(record)
        if record["state"] == "queued":
            record["state"] = "canceled"
            _discard_orphan_report_snapshot(storage, record)
            _commit_mutation(storage.jobs, record, timestamp)
            return _projection(record)
        if "cancel_requested_at" not in record:
            record["cancel_requested_at"] = timestamp
            _commit_mutation(storage.jobs, record, timestamp)
        return _projection(record)


def acknowledge_cancel(
    target: Path,
    job_id: str,
    bot_id: str,
    lease_id: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Terminalize a previously requested cooperative cancellation."""
    job_id = _validate_job_id(job_id)
    bot_id = _validate_opaque_id(bot_id, "invalid-bot-id")
    lease_id = _validate_opaque_id(lease_id, "invalid-lease-id")
    if hub_authority(target):
        return _hub_lease_op("ack_cancel", job_id, bot_id, lease_id)
    timestamp, instant = _timestamp(now)
    with _storage_paths(target) as storage, _queue_lock(storage):
        record = _load_record(storage.jobs, job_id)
        _require_live_lease_or_expire(storage, record, bot_id, lease_id, now, instant)
        if "cancel_requested_at" not in record:
            raise GrokbotJobError("cancellation-not-requested")
        record["state"] = "canceled"
        _discard_orphan_report_snapshot(storage, record)
        _commit_mutation(storage.jobs, record, timestamp)
        return _projection(record)


@contextmanager
def _storage_paths(target: Path) -> Iterator[_Storage]:
    """Open queue directories once so POSIX operations stay below verified fds."""
    descriptors: list[int] = []
    try:
        if os.name != "posix":  # pragma: no cover - exercised on Windows.
            base = Path(target).expanduser().absolute()
            root = base / ".brigade" / "cloud" / "grokbot"
            _ensure_directory(root)
            jobs = root / "jobs"
            idempotency = root / "idempotency"
            artifacts = root / "artifacts"
            snapshots = root / "snapshots"
            _ensure_directory(jobs)
            _ensure_directory(idempotency)
            _ensure_directory(artifacts)
            _ensure_directory(snapshots)
            _assert_regular_or_missing(root / "queue.lock")
            yield _Storage(
                _Directory(root, None),
                _Directory(jobs, None),
                _Directory(idempotency, None),
                _Directory(artifacts, None),
                _Directory(snapshots, None),
            )
            return

        base_fd = _open_directory_path(Path(target).expanduser().absolute())
        descriptors.append(base_fd)
        brigade_fd = _open_or_create_directory(base_fd, ".brigade")
        descriptors.append(brigade_fd)
        cloud_fd = _open_or_create_directory(brigade_fd, "cloud")
        descriptors.append(cloud_fd)
        root_fd = _open_or_create_directory(cloud_fd, "grokbot")
        descriptors.append(root_fd)
        jobs_fd = _open_or_create_directory(root_fd, "jobs")
        descriptors.append(jobs_fd)
        idempotency_fd = _open_or_create_directory(root_fd, "idempotency")
        descriptors.append(idempotency_fd)
        artifacts_fd = _open_or_create_directory(root_fd, "artifacts")
        descriptors.append(artifacts_fd)
        snapshots_fd = _open_or_create_directory(root_fd, "snapshots")
        descriptors.append(snapshots_fd)
        yield _Storage(
            _Directory(Path(target) / ".brigade" / "cloud" / "grokbot", root_fd),
            _Directory(Path(target) / ".brigade" / "cloud" / "grokbot" / "jobs", jobs_fd),
            _Directory(Path(target) / ".brigade" / "cloud" / "grokbot" / "idempotency", idempotency_fd),
            _Directory(Path(target) / ".brigade" / "cloud" / "grokbot" / "artifacts", artifacts_fd),
            _Directory(Path(target) / ".brigade" / "cloud" / "grokbot" / "snapshots", snapshots_fd),
        )
    except GrokbotJobError:
        raise
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc
    finally:
        close_error: OSError | None = None
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError as exc:
                close_error = exc
        if close_error is not None:
            raise GrokbotJobError("unsafe-storage") from close_error


@contextmanager
def _storage_paths_readonly(target: Path) -> Iterator[_Storage]:
    """Open an existing queue without creating directories or changing modes."""
    descriptors: list[int] = []
    try:
        if os.name != "posix":  # pragma: no cover - exercised on Windows.
            root = Path(target).expanduser().absolute() / ".brigade" / "cloud" / "grokbot"
            jobs = root / "jobs"
            idempotency = root / "idempotency"
            artifacts = root / "artifacts"
            for path in (root, jobs, idempotency, artifacts):
                current = path.lstat()
                if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
                    raise GrokbotJobError("unsafe-storage")
            yield _Storage(
                _Directory(root, None),
                _Directory(jobs, None),
                _Directory(idempotency, None),
                _Directory(artifacts, None),
            )
            return

        base_fd = _open_directory_path(Path(target).expanduser().absolute())
        descriptors.append(base_fd)
        brigade_fd = _open_existing_directory(base_fd, ".brigade")
        descriptors.append(brigade_fd)
        cloud_fd = _open_existing_directory(brigade_fd, "cloud")
        descriptors.append(cloud_fd)
        root_fd = _open_existing_directory(cloud_fd, "grokbot")
        descriptors.append(root_fd)
        jobs_fd = _open_existing_directory(root_fd, "jobs")
        descriptors.append(jobs_fd)
        idempotency_fd = _open_existing_directory(root_fd, "idempotency")
        descriptors.append(idempotency_fd)
        artifacts_fd = _open_existing_directory(root_fd, "artifacts")
        descriptors.append(artifacts_fd)
        root = Path(target) / ".brigade" / "cloud" / "grokbot"
        yield _Storage(
            _Directory(root, root_fd),
            _Directory(root / "jobs", jobs_fd),
            _Directory(root / "idempotency", idempotency_fd),
            _Directory(root / "artifacts", artifacts_fd),
        )
    except GrokbotJobError:
        raise
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc
    finally:
        close_error: OSError | None = None
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError as exc:
                close_error = exc
        if close_error is not None:
            raise GrokbotJobError("unsafe-storage") from close_error


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_directory_path(path: Path) -> int:
    descriptor = os.open(os.fspath(path), _directory_flags())
    _assert_directory_descriptor(descriptor)
    return descriptor


def _open_or_create_directory(parent: int, name: str) -> int:
    try:
        os.mkdir(name, ROOT_MODE, dir_fd=parent)
    except FileExistsError:
        pass
    descriptor = os.open(name, _directory_flags(), dir_fd=parent)
    try:
        _assert_directory_descriptor(descriptor)
        os.fchmod(descriptor, ROOT_MODE)
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def _open_existing_directory(parent: int, name: str) -> int:
    descriptor = os.open(name, _directory_flags(), dir_fd=parent)
    try:
        _assert_directory_descriptor(descriptor)
    except OSError:
        os.close(descriptor)
        raise
    return descriptor


def _assert_directory_descriptor(descriptor: int) -> None:
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        raise GrokbotJobError("unsafe-storage")


def _ensure_directory(path: Path) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(parents=True, mode=ROOT_MODE)
        except FileExistsError:
            pass
        try:
            current = path.lstat()
        except FileNotFoundError as exc:
            raise GrokbotJobError("unsafe-storage") from exc
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISDIR(current.st_mode):
        raise GrokbotJobError("unsafe-storage")
    os.chmod(path, ROOT_MODE)


def _assert_regular_or_missing(path: Path) -> None:
    try:
        current = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(current.st_mode) or not stat.S_ISREG(current.st_mode):
        raise GrokbotJobError("unsafe-storage")


def _assert_regular_file(path: Path) -> None:
    _assert_regular_or_missing(path)
    if not path.exists():
        raise GrokbotJobError("job-not-found")


@contextmanager
def _queue_lock(storage: _Storage) -> Iterator[None]:
    if storage.root.descriptor is None:  # pragma: no cover - exercised on Windows.
        with _queue_lock_windows(storage.root.path, storage.root.path / "queue.lock"):
            yield
        return
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open("queue.lock", flags, FILE_MODE, dir_fd=storage.root.descriptor)
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc
    locked = False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise GrokbotJobError("unsafe-storage")
        _chmod_file_descriptor(descriptor, None)
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows only.
            os.lseek(descriptor, 0, os.SEEK_SET)
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
                os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - unsupported Python platform.
            raise GrokbotJobError("queue-lock-unavailable")
        locked = True
        yield
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc
    finally:
        if locked:
            try:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                elif msvcrt is not None:  # pragma: no cover - Windows only.
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
            finally:
                os.close(descriptor)
        else:
            os.close(descriptor)


@contextmanager
def _queue_lock_windows(root: Path, lock_path: Path) -> Iterator[None]:  # pragma: no cover - Windows only.
    _ensure_directory(root)
    _assert_regular_or_missing(lock_path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(lock_path, flags, FILE_MODE)
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc
    locked = False
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise GrokbotJobError("unsafe-storage")
        _chmod_file_descriptor(descriptor, lock_path)
        if msvcrt is None:
            raise GrokbotJobError("queue-lock-unavailable")
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
            os.fsync(descriptor)
            os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_LOCK, 1)  # type: ignore[attr-defined]
        locked = True
        yield
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc
    finally:
        try:
            if locked:
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]
        except OSError as exc:
            raise GrokbotJobError("unsafe-storage") from exc
        finally:
            os.close(descriptor)


def _write_json_file(directory: _Directory, name: str, payload: dict[str, Any]) -> None:
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    _write_bytes_file(directory, name, data)


def _write_bytes_file(directory: _Directory, name: str, data: bytes) -> None:
    if directory.descriptor is None:  # pragma: no cover - exercised on Windows.
        _write_bytes_file_windows(directory.path / name, data)
        return
    temporary = f".{name}.{secrets.token_hex(12)}.tmp"
    descriptor: int | None = None
    temporary_created = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            FILE_MODE,
            dir_fd=directory.descriptor,
        )
        temporary_created = True
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise GrokbotJobError("unsafe-storage")
        _chmod_file_descriptor(descriptor, None)
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, name, src_dir_fd=directory.descriptor, dst_dir_fd=directory.descriptor)
        temporary_created = False
        os.fsync(directory.descriptor)
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc
    finally:
        cleanup_error: OSError | None = None
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                cleanup_error = exc
        if temporary_created:
            try:
                os.unlink(temporary, dir_fd=directory.descriptor)
            except FileNotFoundError:
                pass
            except OSError as exc:
                cleanup_error = exc
        if cleanup_error is not None:
            raise GrokbotJobError("unsafe-storage") from cleanup_error


def _write_bytes_file_windows(path: Path, data: bytes) -> None:  # pragma: no cover - Windows only.
    _ensure_directory(path.parent)
    _assert_regular_or_missing(path)
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        temporary = path.parent / f".{path.name}.{secrets.token_hex(12)}.tmp"
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
        _chmod_file_descriptor(descriptor, temporary)
        _write_all(descriptor, data)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
        temporary = None
        os.chmod(path, FILE_MODE)
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                raise GrokbotJobError("unsafe-storage") from exc


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError("short write")
        offset += written


def _read_json_file(directory: _Directory, name: str, *, missing_ok: bool = False) -> dict[str, Any] | None:
    if directory.descriptor is None:  # pragma: no cover - exercised on Windows.
        return _read_json_file_windows(directory.path / name, missing_ok=missing_ok)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=directory.descriptor
        )
    except FileNotFoundError:
        if missing_ok:
            return None
        raise GrokbotJobError("job-not-found") from None
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise GrokbotJobError("unsafe-storage")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GrokbotJobError("corrupt-storage") from exc
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                raise GrokbotJobError("unsafe-storage") from exc
    if not isinstance(payload, dict):
        raise GrokbotJobError("corrupt-storage")
    return payload


def _read_json_file_windows(
    path: Path, *, missing_ok: bool
) -> dict[str, Any] | None:  # pragma: no cover - Windows only.
    _assert_regular_or_missing(path)
    if not path.exists():
        if missing_ok:
            return None
        raise GrokbotJobError("job-not-found")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GrokbotJobError("corrupt-storage") from exc
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc
    if not isinstance(payload, dict):
        raise GrokbotJobError("corrupt-storage")
    return payload


def _read_bytes_file(directory: _Directory, name: str, *, maximum: int, missing_reason: str) -> bytes:
    if directory.descriptor is None:  # pragma: no cover - exercised on Windows.
        return _read_bytes_file_windows(directory.path / name, maximum=maximum, missing_reason=missing_reason)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), dir_fd=directory.descriptor
        )
    except FileNotFoundError:
        raise GrokbotJobError(missing_reason) from None
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise GrokbotJobError("unsafe-storage")
        return _read_bounded_bytes(descriptor, maximum)
    except GrokbotJobError:
        raise
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                raise GrokbotJobError("unsafe-storage") from exc


def _read_bytes_file_windows(
    path: Path, *, maximum: int, missing_reason: str
) -> bytes:  # pragma: no cover - Windows only.
    _assert_regular_or_missing(path)
    if not path.exists():
        raise GrokbotJobError(missing_reason)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise GrokbotJobError("unsafe-storage")
        return _read_bounded_bytes(descriptor, maximum)
    except GrokbotJobError:
        raise
    except FileNotFoundError:
        raise GrokbotJobError(missing_reason) from None
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _read_bounded_bytes(descriptor: int, maximum: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    limit = maximum + 1
    while total < limit:
        chunk = os.read(descriptor, limit - total)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    if total > maximum:
        raise GrokbotJobError("report-too-large")
    return b"".join(chunks)


def _unlink_artifact_file(directory: _Directory, name: str) -> None:
    if directory.descriptor is None:  # pragma: no cover - exercised on Windows.
        path = directory.path / name
        _assert_regular_or_missing(path)
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise GrokbotJobError("unsafe-storage") from exc
        return
    try:
        os.unlink(name, dir_fd=directory.descriptor)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc


def _discard_orphan_report_snapshot(storage: _Storage, record: dict[str, Any]) -> None:
    if record["state"] not in {"failed", "expired", "canceled"}:
        return
    _unlink_artifact_file(storage.artifacts, f"{record['job_id']}.md")


def _chmod_file_descriptor(descriptor: int, path: Path | None) -> None:
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, FILE_MODE)
        return
    if path is None:
        raise GrokbotJobError("unsafe-storage")
    _assert_regular_file(path)
    os.chmod(path, FILE_MODE)


def _load_record(jobs_dir: _Directory, job_id: str) -> dict[str, Any]:
    record = _read_json_file(jobs_dir, f"{job_id}.json")
    if record is None:  # Kept for the benefit of static type checkers.
        raise GrokbotJobError("job-not-found")
    return _validate_record(record)


def _list_names(directory: _Directory, *, prefix: str, suffix: str) -> list[str]:
    try:
        if directory.descriptor is None:  # pragma: no cover - exercised on Windows.
            names = [path.name for path in directory.path.glob(f"{prefix}*{suffix}")]
        else:
            names = os.listdir(directory.descriptor)
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc
    return sorted(name for name in names if name.startswith(prefix) and name.endswith(suffix))


def _find_record_by_idempotency_hash(jobs_dir: _Directory, key_hash: str) -> dict[str, Any] | None:
    match: dict[str, Any] | None = None
    for name in _list_names(jobs_dir, prefix="grokbot-", suffix=".json"):
        record = _read_json_file(jobs_dir, name)
        if record is None:
            raise GrokbotJobError("corrupt-storage")
        validated = _validate_record(record)
        if validated["idempotency_key_hash"] == key_hash:
            if match is not None:
                raise GrokbotJobError("corrupt-storage")
            match = validated
    return match


def _validate_record(record: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "job_id",
        "task_hash",
        "idempotency_key_hash",
        "state",
        "created_at",
        "updated_at",
        "queued_at",
        "timeout_seconds",
        "item_revision",
        "spec",
    }
    optional = {
        "claimed_at",
        "lease_expires_at",
        "bot_id",
        "lease_id",
        "cancel_requested_at",
        "result_artifact",
    }
    if set(record) - required - optional or not required <= set(record) or record.get("schema") != JOB_SCHEMA:
        raise GrokbotJobError("corrupt-storage")
    job_id = record.get("job_id")
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise GrokbotJobError("corrupt-storage")
    if record.get("state") not in JOB_STATES:
        raise GrokbotJobError("corrupt-storage")
    if not isinstance(record.get("spec"), dict):
        raise GrokbotJobError("corrupt-storage")
    spec = _validate_spec(record["spec"])
    if record["spec"] != spec or record.get("timeout_seconds") != spec["timeout_seconds"]:
        raise GrokbotJobError("corrupt-storage")
    task_hash = record.get("task_hash")
    if not isinstance(task_hash, str) or not TASK_HASH_RE.fullmatch(task_hash) or task_hash != _task_hash(spec):
        raise GrokbotJobError("corrupt-storage")
    idempotency_key_hash = record.get("idempotency_key_hash")
    if not isinstance(idempotency_key_hash, str) or not TASK_HASH_RE.fullmatch(idempotency_key_hash):
        raise GrokbotJobError("corrupt-storage")
    if type(record.get("item_revision")) is not int or record["item_revision"] < 1:
        raise GrokbotJobError("corrupt-storage")
    created_at = _parse_timestamp(record.get("created_at"))
    queued_at = _parse_timestamp(record.get("queued_at"))
    updated_at = _parse_timestamp(record.get("updated_at"))
    if not created_at <= queued_at <= updated_at:
        raise GrokbotJobError("corrupt-storage")
    live_fields = {"claimed_at", "lease_expires_at", "bot_id", "lease_id"}
    present_live_fields = live_fields & set(record)
    if present_live_fields and present_live_fields != live_fields:
        raise GrokbotJobError("corrupt-storage")
    if present_live_fields:
        claimed_at = _parse_timestamp(record["claimed_at"])
        lease_expires_at = _parse_timestamp(record["lease_expires_at"])
        deadline = claimed_at + timedelta(seconds=spec["timeout_seconds"])
        if not created_at <= claimed_at <= updated_at or not claimed_at <= lease_expires_at <= deadline:
            raise GrokbotJobError("corrupt-storage")
        _validate_opaque_id(record["bot_id"], "corrupt-storage")
        _validate_opaque_id(record["lease_id"], "corrupt-storage")
    if "cancel_requested_at" in record:
        if (
            not present_live_fields
            or not _parse_timestamp(record["claimed_at"])
            <= _parse_timestamp(record["cancel_requested_at"])
            <= updated_at
        ):
            raise GrokbotJobError("corrupt-storage")
    state = record["state"]
    if state == "queued" and (present_live_fields or "cancel_requested_at" in record or "result_artifact" in record):
        raise GrokbotJobError("corrupt-storage")
    if state in {"claimed", "running", "failed", "completed"} and not present_live_fields:
        raise GrokbotJobError("corrupt-storage")
    if state == "completed":
        if "result_artifact" not in record or "cancel_requested_at" in record:
            raise GrokbotJobError("corrupt-storage")
        _validate_completion_artifact(record["result_artifact"], spec)
    elif "result_artifact" in record:
        raise GrokbotJobError("corrupt-storage")
    if state == "canceled" and "cancel_requested_at" in record and not present_live_fields:
        raise GrokbotJobError("corrupt-storage")
    return record


def _validate_idempotency_record(record: object) -> dict[str, Any]:
    if not isinstance(record, dict) or set(record) != {"schema", "job_id", "task_hash"}:
        raise GrokbotJobError("corrupt-storage")
    if record.get("schema") != IDEMPOTENCY_SCHEMA:
        raise GrokbotJobError("corrupt-storage")
    job_id = record.get("job_id")
    task_hash = record.get("task_hash")
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise GrokbotJobError("corrupt-storage")
    if not isinstance(task_hash, str) or not TASK_HASH_RE.fullmatch(task_hash):
        raise GrokbotJobError("corrupt-storage")
    return record


def _commit_mutation(jobs_dir: _Directory, record: dict[str, Any], timestamp: str) -> None:
    if _parse_timestamp(timestamp) < _parse_timestamp(record["updated_at"]):
        raise GrokbotJobError("clock-regression")
    record["updated_at"] = timestamp
    record["item_revision"] += 1
    _write_json_file(jobs_dir, f"{record['job_id']}.json", record)


def _reject_nonqueued_claim(record: dict[str, Any]) -> None:
    if record["state"] == "expired":
        raise GrokbotJobError("job-expired")
    if record["state"] in {"completed", "failed", "canceled"}:
        raise GrokbotJobError("terminal-state")
    raise GrokbotJobError("invalid-state")


def _report_bytes(report_text: object) -> bytes:
    if not isinstance(report_text, str) or not report_text:
        raise GrokbotJobError("invalid-report")
    try:
        data = report_text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise GrokbotJobError("invalid-report") from exc
    if len(data) > MAX_REPORT_BYTES:
        raise GrokbotJobError("report-too-large")
    return data


def _require_report_job(record: dict[str, Any]) -> None:
    spec = record["spec"]
    assert isinstance(spec, dict)
    if spec["role"] != "repository-scout":
        raise GrokbotJobError("invalid-role")
    artifact = spec["artifact"]
    if not isinstance(artifact, dict) or artifact.get("kind") != "report":
        raise GrokbotJobError("invalid-artifact")


def _validated_completion(record: dict[str, Any], artifact: object) -> dict[str, Any]:
    if record["state"] != "running":
        raise GrokbotJobError("invalid-transition")
    if "cancel_requested_at" in record:
        raise GrokbotJobError("cancel-requested")
    return _validate_completion_artifact(artifact, record["spec"])


def _validate_spec(spec: object) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise GrokbotJobError("invalid-spec")
    _reject_sensitive_keys(spec)
    keys = set(spec)
    unknown = keys - REQUIRED_ENVELOPE_KEYS - OPTIONAL_ENVELOPE_KEYS
    if unknown:
        raise GrokbotJobError("unknown-key")
    if not REQUIRED_ENVELOPE_KEYS <= keys or keys - REQUIRED_ENVELOPE_KEYS - OPTIONAL_ENVELOPE_KEYS:
        raise GrokbotJobError("missing-key")

    normalized = json.loads(json.dumps(spec, sort_keys=True, separators=(",", ":")))
    _bounded_string(normalized["label"], reason="invalid-label", maximum=160)
    role = normalized["role"]
    if role not in ROLES:
        raise GrokbotJobError("invalid-role")
    _validate_repository(normalized["repository"])
    _validate_base_ref(normalized["base_ref"])
    _validate_ownership_paths(normalized["ownership_paths"])
    _bounded_string(normalized["instructions"], reason="invalid-instructions", maximum=16000)
    _validate_commands(normalized["verification_commands"])
    _validate_artifact(normalized["artifact"], role=role)
    timeout = normalized["timeout_seconds"]
    if type(timeout) is not int or not 60 <= timeout <= 14400:
        raise GrokbotJobError("invalid-timeout")
    if "queue_ttl_seconds" in normalized:
        queue_ttl = normalized["queue_ttl_seconds"]
        if type(queue_ttl) is not int or not QUEUE_TTL_SECONDS_MIN <= queue_ttl <= QUEUE_TTL_SECONDS_MAX:
            raise GrokbotJobError("invalid-queue-ttl")
    return normalized


def _reject_sensitive_keys(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise GrokbotJobError("invalid-spec")
            lowered = key.casefold()
            if any(token in lowered for token in SENSITIVE_KEY_TOKENS):
                raise GrokbotJobError("forbidden-key")
            _reject_sensitive_keys(child)
    elif isinstance(value, list):
        for child in value:
            _reject_sensitive_keys(child)


def _task_hash(spec: dict[str, Any]) -> str:
    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _idempotency_key_hash(key: str) -> str:
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def _now_iso(now: datetime | None) -> str:
    return _timestamp(now)[0]


def _verified_report(job_id: str, data: bytes, expected: object) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise GrokbotJobError("invalid-report") from exc
    if not text:
        raise GrokbotJobError("invalid-report")
    digest = hashlib.sha256(data).hexdigest()
    if expected is not None and digest != expected:
        raise GrokbotJobError("digest-mismatch")
    return {"job_id": job_id, "text": text, "bytes": len(data), "sha256": digest}


def _store_task_snapshot(target: Path, job_id: str, envelope: dict[str, Any], key_hash: str) -> None:
    payload = {
        "schema": SNAPSHOT_SCHEMA,
        "job_id": job_id,
        "task_hash": _task_hash(envelope),
        "idempotency_key_hash": key_hash,
        "spec": envelope,
    }
    with _storage_paths(target) as storage, _queue_lock(storage):
        snapshots = storage.snapshots
        if snapshots is None:
            raise GrokbotJobError("unsafe-storage")
        existing = _read_json_file(snapshots, f"{job_id}.json", missing_ok=True)
        if existing is None:
            _write_json_file(snapshots, f"{job_id}.json", payload)
            return
        if (
            existing.get("schema") == SNAPSHOT_SCHEMA
            and existing.get("job_id") == job_id
            and existing.get("task_hash") == payload["task_hash"]
            and existing.get("idempotency_key_hash") == key_hash
            and existing.get("spec") == envelope
        ):
            return
        raise GrokbotJobError("idempotency-conflict")


def _hub_holds_job(job_id: str) -> bool | None:
    """Report whether the hub still holds ``job_id``; ``None`` when unknown.

    The proof is one granted listing under the caller's own identity. Every
    actor that may enqueue holds ``list`` but not ``status``, and the hub scopes
    a listing to that actor's own queue, so the probe passes no role filter and
    a granted listing that omits the derived job id proves absence. Any refused
    or failed listing, including ``auth-failed`` and ``hub-unavailable``, leaves
    the answer unknown and so keeps the snapshot.

    The client offers no per-call deadline, so this probe adds one hub round
    trip bounded by ``GROKBOT_TIMEOUT_SECONDS`` to a failed enqueue. Only
    ``Exception`` is caught: a ``KeyboardInterrupt`` raised during the probe
    propagates instead of being answered as unknown.
    """
    from . import fleet_client_grokbot

    try:
        decision = fleet_client_grokbot.list_jobs(include_all=True)
    except Exception:
        return None
    if not decision.granted or decision.jobs is None:
        return None
    return any(job.get("job_id") == job_id for job in decision.jobs)


def _discard_unheld_task_snapshot(target: Path, job_id: str, key_hash: str) -> None:
    """Discard a snapshot only once the hub proves it holds no such job.

    An enqueue that fails in doubt, and a loser racing a granted enqueue under
    the same key, both reach this path while the hub already holds the job. A
    snapshot deleted there would strand the live job: ``claim`` with an
    execution context and the hub report completion both need it, and the
    active-job guard blocks the re-apply that would rewrite it. Proof is a
    granted queue listing that does not carry the derived job id; anything less
    keeps the snapshot.
    """
    if _hub_holds_job(job_id) is not False:
        return
    _discard_task_snapshot(target, job_id, key_hash)


def _discard_task_snapshot(target: Path, job_id: str, key_hash: str) -> None:
    """Drop the private snapshot of an enqueue the hub never granted.

    The snapshot is written before the hub decides, so a refused or failed
    enqueue would otherwise leave a file that later reads as a queued job. Only
    a snapshot carrying this exact key hash is removed, and a storage failure
    here never replaces the refusal reason the caller is about to raise.
    """
    try:
        with _storage_paths(target) as storage, _queue_lock(storage):
            snapshots = storage.snapshots
            if snapshots is None:
                return
            existing = _read_json_file(snapshots, f"{job_id}.json", missing_ok=True)
            if existing is None or existing.get("idempotency_key_hash") != key_hash:
                return
            _unlink_artifact_file(snapshots, f"{job_id}.json")
    except (GrokbotJobError, OSError):
        return


def _load_task_snapshot(target: Path, job_id: str) -> dict[str, Any]:
    with _storage_paths(target) as storage:
        snapshots = storage.snapshots
        if snapshots is None:
            raise GrokbotJobError("unsafe-storage")
        payload = _read_json_file(snapshots, f"{job_id}.json", missing_ok=True)
    if payload is None or payload.get("schema") != SNAPSHOT_SCHEMA:
        raise GrokbotJobError("snapshot-missing")
    spec = payload.get("spec")
    if not isinstance(spec, dict):
        raise GrokbotJobError("corrupt-storage")
    return _validate_spec(spec)


def _enqueue_via_hub(target: Path, spec: dict[str, Any], idempotency_key: str, now: datetime | None) -> dict[str, Any]:
    del now
    from . import fleet_client_grokbot

    envelope = _validate_spec(spec)
    key = _validate_idempotency_key(idempotency_key)
    task_hash = _task_hash(envelope)
    key_hash = _idempotency_key_hash(key)
    if not authority_marker_present(target) and _active_legacy_job_ids(target):
        raise GrokbotJobError("legacy-active-needs-reconcile")
    job_id = f"grokbot-{key_hash.removeprefix('sha256:')[:24]}"
    _store_task_snapshot(target, job_id, envelope, key_hash)
    try:
        decision = fleet_client_grokbot.enqueue(
            job_id=job_id,
            role=envelope["role"],
            repository=envelope["repository"],
            label=envelope["label"],
            task_digest=task_hash.removeprefix("sha256:"),
            idempotency_key_hash=key_hash.removeprefix("sha256:"),
            timeout_seconds=envelope["timeout_seconds"],
            queue_ttl_seconds=envelope.get("queue_ttl_seconds", DEFAULT_QUEUE_TTL_SECONDS),
            artifact_kind=envelope["artifact"]["kind"],
            private_snapshot_id=job_id,
            operation_id=f"enqueue:{job_id}",
        )
    except BaseException:
        _discard_unheld_task_snapshot(target, job_id, key_hash)
        raise
    if not decision.granted or decision.job is None:
        if decision.reason not in HUB_DUPLICATE_REFUSALS:
            _discard_unheld_task_snapshot(target, job_id, key_hash)
        raise GrokbotJobError(decision.reason)
    if not authority_marker_present(target):
        _write_authority_marker(target)
    return {"job_id": decision.job["job_id"], "state": decision.job["state"], "idempotent": decision.idempotent}


def _enqueue_under_limits_via_hub(
    target: Path,
    envelope: dict[str, Any],
    key: str,
    role: str,
    active_reason: str,
    daily_limit: int,
    instant: datetime,
) -> dict[str, Any]:
    jobs = _hub_jobs(role=role, include_all=True)
    created_today = sum(_parse_timestamp(job["created_at"]).date() == instant.date() for job in jobs)
    if any(
        job["state"] in WORK_STATES or "cancel_requested_at" in job
        for job in jobs
        if job["state"] not in TERMINAL_STATES
    ):
        return {"reason": active_reason, "created_today": created_today, "handle": None}
    if created_today >= daily_limit:
        return {"reason": "daily-limit-reached", "created_today": created_today, "handle": None}
    handle = _enqueue_via_hub(target, envelope, key, instant)
    if handle["idempotent"]:
        return {"reason": "all-known", "created_today": created_today, "handle": handle}
    return {"reason": "created", "created_today": created_today, "handle": handle}


WORK_STATES = frozenset({"queued", "claimed", "running"})
TERMINAL_STATES = frozenset({"completed", "failed", "expired", "canceled"})


def _hub_unavailable_row() -> dict[str, Any]:
    return {
        "job_id": "grokbot-hub-unavailable",
        "label": "Grok Bot hub unavailable",
        "task_hash": None,
        "state": "unavailable",
        "created_at": None,
        "updated_at": None,
        "queued_at": None,
        "artifact": {"kind": "report"},
        "degraded": True,
        "classification": "needs-investigation",
        "source": "grokbot-hub",
    }


def _hub_tracker_artifact(job: dict[str, Any], kind: object) -> dict[str, Any]:
    artifact: dict[str, Any] = {"kind": kind}
    if kind == "draft-pr" and job.get("artifact_ref"):
        artifact["url"] = job["artifact_ref"]
    elif kind == "branch":
        if job.get("artifact_ref"):
            artifact["branch"] = job["artifact_ref"]
        if job.get("artifact_digest"):
            artifact["commit"] = job["artifact_digest"]
    elif kind == "report":
        if job.get("artifact_digest"):
            artifact["sha256"] = job["artifact_digest"]
        if job.get("artifact_size") is not None:
            artifact["size"] = job["artifact_size"]
        if job.get("private_snapshot_id"):
            artifact["private_snapshot_id"] = job["private_snapshot_id"]
    else:
        return {}
    return artifact if len(artifact) > 1 else {}


def _hub_job(job_id: str) -> dict[str, Any]:
    return _hub_projection(_require_hub("status", job_id=job_id))


def _hub_jobs(*, role: str | None = None, include_all: bool = False) -> list[dict[str, Any]]:
    from . import fleet_client_grokbot

    decision = fleet_client_grokbot.list_jobs(role=role, include_all=include_all)
    if not decision.granted or decision.jobs is None:
        raise GrokbotJobError(decision.reason, action="list")
    return [_hub_projection_job(job) for job in decision.jobs]


def _claim_via_hub(
    target: Path, job_id: str, bot_id: str, lease_id: str, lease_seconds: int, *, include_context: bool
) -> dict[str, Any]:
    current = _hub_job(job_id)
    decision = _require_hub(
        "claim",
        job_id,
        lease_id=lease_id,
        lease_seconds=lease_seconds,
        expected_item_revision=current["item_revision"],
        operation_id=f"claim:{job_id}:{current['item_revision']}",
    )
    result = _hub_projection(decision)
    if include_context:
        result["execution_context"] = _execution_context({"spec": _load_task_snapshot(target, job_id), **result})
    return result


def _renew_via_hub(job_id: str, bot_id: str, lease_id: str, lease_seconds: int) -> dict[str, Any]:
    current = _hub_job(job_id)
    return _hub_projection(
        _require_hub(
            "renew",
            job_id,
            lease_id=lease_id,
            lease_seconds=lease_seconds,
            expected_item_revision=current["item_revision"],
            lease_generation=current.get("lease_generation"),
            operation_id=f"renew:{job_id}:{current['item_revision']}",
        )
    )


def _transition_via_hub(
    job_id: str, bot_id: str, lease_id: str, state: str, *, artifact: dict[str, Any] | None
) -> dict[str, Any]:
    current = _hub_job(job_id)
    if state == "running":
        return _hub_lease_op(
            "start",
            job_id,
            bot_id,
            lease_id,
            expected_item_revision=current["item_revision"],
            lease_generation=current.get("lease_generation"),
        )
    if state == "failed":
        return _hub_lease_op(
            "fail",
            job_id,
            bot_id,
            lease_id,
            expected_item_revision=current["item_revision"],
            lease_generation=current.get("lease_generation"),
        )
    if artifact is None:
        raise GrokbotJobError("invalid-artifact")
    metadata = _safe_artifact_metadata(artifact, current)
    return _hub_projection(
        _require_hub(
            "complete",
            job_id,
            lease_id=lease_id,
            expected_item_revision=current["item_revision"],
            lease_generation=current.get("lease_generation"),
            operation_id=f"complete:{job_id}:{current['item_revision']}",
            artifact=metadata,
        )
    )


def _complete_report_via_hub(
    target: Path, job_id: str, bot_id: str, lease_id: str, artifact: dict[str, Any], report_bytes: bytes
) -> dict[str, Any]:
    snapshot = _load_task_snapshot(target, job_id)
    validated = _validate_completion_artifact(artifact, snapshot)
    if hashlib.sha256(report_bytes).hexdigest() != validated["sha256"]:
        raise GrokbotJobError("digest-mismatch")
    artifact_name = f"{job_id}.md"
    preexisting: bytes | None = None
    created_orphan = False
    with _storage_paths(target) as storage, _queue_lock(storage):
        try:
            preexisting = _read_bytes_file(
                storage.artifacts, artifact_name, maximum=MAX_REPORT_BYTES, missing_reason="report-missing"
            )
        except GrokbotJobError as exc:
            if exc.reason != "report-missing":
                raise
            preexisting = None
        _write_bytes_file(storage.artifacts, artifact_name, report_bytes)
        created_orphan = preexisting is None
    try:
        current = _hub_job(job_id)
        metadata = _safe_artifact_metadata(validated, current)
        metadata["size"] = len(report_bytes)
        metadata["private_snapshot_id"] = job_id
        return _hub_projection(
            _require_hub(
                "complete",
                job_id,
                lease_id=lease_id,
                expected_item_revision=current["item_revision"],
                lease_generation=current.get("lease_generation"),
                operation_id=f"complete:{job_id}:{current['item_revision']}",
                artifact=metadata,
            )
        )
    except GrokbotJobError:
        with _storage_paths(target) as storage, _queue_lock(storage):
            if created_orphan:
                _unlink_artifact_file(storage.artifacts, artifact_name)
            elif preexisting is not None:
                _write_bytes_file(storage.artifacts, artifact_name, preexisting)
        raise


def _hub_lease_op(action: str, job_id: str, bot_id: str, lease_id: str, **fields: Any) -> dict[str, Any]:
    current = fields.pop("expected_item_revision", None)
    if current is None:
        current = _hub_job(job_id)["item_revision"]
    return _hub_projection(
        _require_hub(
            action,
            job_id,
            lease_id=lease_id,
            expected_item_revision=current,
            lease_generation=fields.pop("lease_generation", None) or _hub_job(job_id).get("lease_generation"),
            operation_id=fields.pop("operation_id", None) or f"{action}:{job_id}:{current}",
            **fields,
        )
    )


def _require_hub(action: str, job_id: str | None = None, **fields: Any) -> Any:
    from . import fleet_client_grokbot

    operation = getattr(fleet_client_grokbot, action)
    decision = operation(job_id, **fields) if job_id is not None else operation(**fields)
    if not decision.granted:
        raise GrokbotJobError(decision.reason, action=action)
    return decision


def _hub_projection(decision: Any) -> dict[str, Any]:
    if decision.job is None:
        raise GrokbotJobError(decision.reason)
    return _hub_projection_job(decision.job)


def _hub_projection_job(job: dict[str, Any]) -> dict[str, Any]:
    digest = job.get("task_digest")
    projection = {
        "job_id": job["job_id"],
        "label": job.get("label"),
        "role": job.get("role"),
        "repository": job.get("repository"),
        "task_hash": f"sha256:{digest}"
        if isinstance(digest, str) and not str(digest).startswith("sha256:")
        else digest,
        "state": job["state"],
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "queued_at": job.get("queued_at"),
        "timeout_seconds": job.get("timeout_seconds"),
        "queue_ttl_seconds": job.get("queue_ttl_seconds", DEFAULT_QUEUE_TTL_SECONDS),
        "item_revision": job.get("item_revision"),
        "sequence": job.get("sequence"),
        "artifact": {"kind": job.get("artifact_kind")},
        "artifact_kind": job.get("artifact_kind"),
        "harness": "grokbot",
    }
    for key in (
        "claimed_at",
        "lease_expires_at",
        "cancel_requested_at",
        "private_snapshot_id",
        "artifact_ref",
        "artifact_digest",
        "artifact_size",
        "claimant_node",
        "claimant_worker",
        "lease_generation",
        "queue_id",
    ):
        if key in job and job[key] is not None:
            projection[key] = job[key]
    return projection


def _safe_artifact_metadata(artifact: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    kind = artifact.get("kind") or job.get("artifact_kind")
    metadata: dict[str, Any] = {
        "kind": kind,
        "private_snapshot_id": job.get("private_snapshot_id") or job.get("job_id"),
    }
    if kind == "draft-pr":
        metadata["ref"] = artifact.get("url")
    elif kind == "branch":
        metadata["ref"] = artifact.get("branch")
        metadata["digest"] = artifact.get("commit")
    elif kind == "report":
        metadata["digest"] = artifact.get("sha256")
        size = artifact.get("size")
        if type(size) is int:
            metadata["size"] = size
    return metadata


def load_task_snapshot(target: Path, job_id: str) -> dict[str, Any]:
    """Return the immutable private envelope for a hub-authoritative job."""
    return _load_task_snapshot(target, _validate_job_id(job_id))


# Keep the established public import surface in this module. The job state,
# deadline, and lease-expiry helpers live separately from storage primitives.
from .grokbot_jobs_expiry import (  # noqa: E402, F401
    _claim_result,
    _deadline,
    _execution_deadline,
    _queue_deadline,
    _queue_ttl_seconds,
    _execution_context,
    _expire_if_elapsed,
    _handle,
    _projection,
    _require_current_lease,
    _require_live_lease,
    _require_live_lease_or_expire,
    expire,
)
