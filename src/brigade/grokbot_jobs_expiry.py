"""Job state, deadline, and lease-expiry helpers for private Grok Bot queue jobs.

These helpers project stored records to safe public views, decide when a job has
run past its own deadline or lapsed lease, terminalize the record, and answer
the public ``expire`` sweep. They are split from ``grokbot_jobs`` so the storage
module stays under the size ratchet.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .grokbot_job_clock import parse_timestamp as _parse_timestamp
from .grokbot_job_clock import timestamp as _timestamp


def _handle(record: dict[str, Any], *, idempotent: bool) -> dict[str, Any]:
    return {"job_id": record["job_id"], "state": record["state"], "idempotent": idempotent}


def _projection(record: dict[str, Any]) -> dict[str, Any]:
    spec = record["spec"]
    assert isinstance(spec, dict)
    projection = {
        "job_id": record["job_id"],
        "label": spec["label"],
        "role": spec["role"],
        "repository": spec["repository"],
        "task_hash": record["task_hash"],
        "state": record["state"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "queued_at": record["queued_at"],
        "timeout_seconds": record["timeout_seconds"],
        "item_revision": record["item_revision"],
        "artifact": spec["artifact"],
    }
    for key in ("claimed_at", "lease_expires_at", "cancel_requested_at"):
        if key in record:
            projection[key] = record[key]
    return projection


def _claim_result(record: dict[str, Any], *, include_context: bool) -> dict[str, Any]:
    result = _projection(record)
    if include_context:
        result["execution_context"] = _execution_context(record)
    return result


def _execution_context(record: dict[str, Any]) -> dict[str, Any]:
    spec = record["spec"]
    assert isinstance(spec, dict)
    return {
        "label": spec["label"],
        "role": spec["role"],
        "repository": spec["repository"],
        "base_ref": spec["base_ref"],
        "ownership_paths": list(spec["ownership_paths"]),
        "instructions": spec["instructions"],
        "verification_commands": list(spec["verification_commands"]),
        "artifact": dict(spec["artifact"]),
        "timeout_seconds": record["timeout_seconds"],
    }


def _expire_if_elapsed(
    storage: Any, record: dict[str, Any], now: datetime | None, *, lease_counts: bool = False
) -> dict[str, Any]:
    """Terminalize one loaded record whose expiry instant passed.

    Reads and claims key on the job's own deadline only (#1353). A lapsed
    lease is a lease matter, not the end of the job: the holder hears
    ``lease-expired`` and the row stays claimed so it can be picked up again
    (#1383). Only an explicit :func:`expire` counts a lapsed lease, which is
    the contract that call has always had.
    """
    from .grokbot_jobs import TERMINAL_STATES, _commit_mutation, _discard_orphan_report_snapshot

    if record["state"] in TERMINAL_STATES:
        return record
    stamp, instant = _timestamp(now)
    expires_at = _deadline(record)
    if lease_counts and record["state"] != "queued":
        expires_at = min(_parse_timestamp(record["lease_expires_at"]), expires_at)
    if instant < expires_at:
        return record
    record["state"] = "expired"
    _discard_orphan_report_snapshot(storage, record)
    _commit_mutation(storage.jobs, record, stamp)
    return record


def _require_live_lease_or_expire(
    storage: Any, record: dict[str, Any], bot_id: str, lease_id: str, now: datetime | None, instant: datetime
) -> None:
    """Guard one lease-holder mutation, expiring a job whose deadline passed.

    Both refusals answer ``lease-expired``: that is the reason the Bot acts on,
    and it is true either way, since a granted lease is always clamped to the
    job's own deadline. When the deadline is what lapsed the row is
    terminalized here too, so the queue does not need an operator sweep to
    agree with the answer it just gave (#1353 composed with #1383).
    """
    from .grokbot_jobs import GrokbotJobError

    try:
        _require_current_lease(record, bot_id, lease_id, instant)
    except GrokbotJobError as exc:
        if exc.reason == "lease-expired":
            _expire_if_elapsed(storage, record, now)
        raise


def _require_current_lease(record: dict[str, Any], bot_id: str, lease_id: str, instant: datetime) -> None:
    from .grokbot_jobs import GrokbotJobError

    if record["state"] in {"completed", "failed", "expired", "canceled"}:
        raise GrokbotJobError("terminal-state")
    if record["state"] not in {"claimed", "running"}:
        raise GrokbotJobError("invalid-state")
    if record["bot_id"] != bot_id or record["lease_id"] != lease_id:
        raise GrokbotJobError("lease-conflict")
    _require_live_lease(record, instant)


def _require_live_lease(record: dict[str, Any], instant: datetime) -> None:
    if instant >= min(_parse_timestamp(record["lease_expires_at"]), _deadline(record)):
        from .grokbot_jobs import GrokbotJobError

        raise GrokbotJobError("lease-expired")


def _deadline(record: dict[str, Any]) -> datetime:
    return _parse_timestamp(record["created_at"]) + timedelta(seconds=record["timeout_seconds"])


def expire(target: Path, job_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Terminalize jobs whose deadline or current lease has elapsed, never requeue."""
    from .grokbot_jobs import (
        TERMINAL_STATES,
        _discard_orphan_report_snapshot,
        _hub_job,
        _hub_projection,
        _load_record,
        _queue_lock,
        _require_hub,
        _storage_paths,
        _validate_job_id,
        hub_authority,
    )

    job_id = _validate_job_id(job_id)
    if hub_authority(target):
        current = _hub_job(job_id)
        return _hub_projection(
            _require_hub(
                "expire",
                job_id,
                expected_item_revision=current["item_revision"],
                operation_id=f"expire:{job_id}:{current['item_revision']}",
            )
        )
    with _storage_paths(target) as storage, _queue_lock(storage):
        record = _load_record(storage.jobs, job_id)
        if record["state"] in TERMINAL_STATES:
            _discard_orphan_report_snapshot(storage, record)
        return _projection(_expire_if_elapsed(storage, record, now, lease_counts=True))
