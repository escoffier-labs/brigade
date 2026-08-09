"""Atomic work-task claims with compare-and-set guards (#738).

Claim is a ``pending`` → ``in_progress`` transition with an assignee, performed
under the single-file task-ledger lock. Idempotency is keyed by opaque
``claim_id`` (not actor name): the same claim id retries as a success no-op;
a different claim id — even from the same actor — is rejected at claim time.

Fail-closed readiness: a claim against a task that is not in the ready set
(open blockers, wrong status, parent-not-startable) is rejected, never
silently granted. Guard mismatches and lost races use exit code 13.

Bulk release/reassign filters treat empty or whitespace-only values as
match-nothing (never match-all). Conflict messages name the holder and stop;
they deliberately do not suggest a release or steal command.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from . import constants, edges as edges_mod, helpers

EXIT_GUARD_MISMATCH = constants.EXIT_GUARD_MISMATCH

REASON_ALREADY_CLAIMED = "already_claimed"
REASON_NOT_READY = "not_ready"
REASON_GUARD_MISMATCH = "guard_mismatch"
REASON_EMPTY_FILTER = "empty_filter"
REASON_CLAIM_ID_MISMATCH = "claim_id_mismatch"
REASON_NOT_CLAIMED = "not_claimed"
REASON_NO_READY = "no_ready"
REASON_UNKNOWN_TASK = "unknown_task"
REASON_MISSING_ACTOR = "missing_actor"
REASON_MISSING_FILTER = "missing_filter"

CLAIM_RECORD_KEYS = ("claim_id", "actor", "claimed_at", "item_revision")


class ClaimError(ValueError):
    """Precondition failure for a claim, release, or reassign mutation."""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        details: dict[str, Any] | None = None,
        exit_code: int = EXIT_GUARD_MISMATCH,
    ):
        super().__init__(message)
        self.reason = reason
        self.details = details or {}
        self.exit_code = exit_code

    def as_dict(self) -> dict[str, Any]:
        payload = {"error": str(self), "reason": self.reason, "exit_code": self.exit_code}
        payload.update(self.details)
        return payload


def generate_claim_id() -> str:
    return f"claim-{uuid4().hex}"


def _string_or_none(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _priority_rank(value: object) -> int:
    text = value.strip().casefold() if isinstance(value, str) else ""
    return constants.PRIORITY_RANK.get(text, constants.PRIORITY_RANK["normal"])


def ready_sort_key(item: dict[str, Any]) -> tuple[int, str, str]:
    """Highest priority first, then oldest created_at, then id."""
    return (
        _priority_rank(item.get("priority")),
        str(item.get("created_at") or ""),
        str(item.get("id") or ""),
    )


def normalize_filter_value(value: object, *, field: str) -> str | None:
    """Return a stripped filter value, or raise on empty/whitespace-only input.

    Callers that pass a filter flag must supply a non-empty value; empty means
    match-nothing (fail closed), never match-all.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ClaimError(
            f"empty filter value for {field}: match-nothing (fail closed)",
            reason=REASON_EMPTY_FILTER,
            details={"field": field, "value": value},
            exit_code=2,
        )
    stripped = value.strip()
    if not stripped:
        raise ClaimError(
            f"empty filter value for {field}: match-nothing (fail closed)",
            reason=REASON_EMPTY_FILTER,
            details={"field": field, "value": value},
            exit_code=2,
        )
    return stripped


def normalize_filter_list(values: list[str] | None, *, field: str) -> list[str] | None:
    """Normalize a repeated filter list; any empty entry fails closed."""
    if values is None:
        return None
    if not values:
        raise ClaimError(
            f"empty filter value for {field}: match-nothing (fail closed)",
            reason=REASON_EMPTY_FILTER,
            details={"field": field, "value": values},
            exit_code=2,
        )
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = normalize_filter_value(value, field=field)
        assert item is not None
        if item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


def claim_record(task: dict[str, Any]) -> dict[str, Any] | None:
    """Return the normalized claim record on a task, if present."""
    raw = task.get("claim")
    if isinstance(raw, dict):
        claim_id = _string_or_none(raw.get("claim_id"))
        actor = _string_or_none(raw.get("actor")) or _string_or_none(task.get("assignee"))
        claimed_at = raw.get("claimed_at") if isinstance(raw.get("claimed_at"), str) else None
        revision = raw.get("item_revision")
        if claim_id and actor:
            record = {
                "claim_id": claim_id,
                "actor": actor,
                "claimed_at": claimed_at or "",
                "item_revision": int(revision) if isinstance(revision, int) else 0,
            }
            return record
    # Legacy/flat fields: treat assignee + claim_id as a claim when in_progress.
    claim_id = _string_or_none(task.get("claim_id"))
    actor = _string_or_none(task.get("assignee"))
    if claim_id and actor and str(task.get("status") or "") == "in_progress":
        claimed_at = task.get("claimed_at") if isinstance(task.get("claimed_at"), str) else ""
        revision = task.get("item_revision")
        return {
            "claim_id": claim_id,
            "actor": actor,
            "claimed_at": claimed_at,
            "item_revision": int(revision) if isinstance(revision, int) else 0,
        }
    return None


def holder_payload(task: dict[str, Any]) -> dict[str, Any]:
    record = claim_record(task)
    payload: dict[str, Any] = {
        "task_id": task.get("id"),
        "status": task.get("status", "pending"),
        "assignee": _string_or_none(task.get("assignee")),
    }
    if record is not None:
        payload["holder"] = record["actor"]
        payload["claim_id"] = record["claim_id"]
        payload["claimed_at"] = record["claimed_at"]
        payload["item_revision"] = record["item_revision"]
    return payload


def conflict_message(task: dict[str, Any]) -> str:
    """Name the holder without suggesting a release/steal command."""
    record = claim_record(task)
    task_id = task.get("id")
    if record is None:
        return f"task {task_id} is not claimable"
    return f"task {task_id} is held by {record['actor']}"


def _bump_item_revision(task: dict[str, Any]) -> int:
    current = task.get("item_revision")
    next_revision = int(current) + 1 if isinstance(current, int) else 1
    task["item_revision"] = next_revision
    return next_revision


def _set_claim(task: dict[str, Any], *, actor: str, claim_id: str, claimed_at: str) -> dict[str, Any]:
    revision = _bump_item_revision(task)
    record = {
        "claim_id": claim_id,
        "actor": actor,
        "claimed_at": claimed_at,
        "item_revision": revision,
    }
    task["status"] = "in_progress"
    task["assignee"] = actor
    task["claim"] = record
    task["updated_at"] = claimed_at
    # Drop any flat legacy mirrors so the nested record is authoritative.
    task.pop("claim_id", None)
    task.pop("claimed_at", None)
    return record


def _clear_claim(task: dict[str, Any], *, now: str) -> None:
    _bump_item_revision(task)
    task["status"] = "pending"
    task.pop("assignee", None)
    task.pop("claim", None)
    task.pop("claim_id", None)
    task.pop("claimed_at", None)
    task["updated_at"] = now


def _check_guards(
    task: dict[str, Any],
    *,
    if_actor: str | None,
    if_status: str | None,
) -> None:
    if if_actor is not None:
        current_actor = _string_or_none(task.get("assignee"))
        record = claim_record(task)
        if record is not None:
            current_actor = record["actor"]
        if current_actor != if_actor:
            raise ClaimError(
                f"guard mismatch: expected actor {if_actor!r}, found {current_actor!r}",
                reason=REASON_GUARD_MISMATCH,
                details={
                    "guard": "if_actor",
                    "expected": if_actor,
                    "actual": current_actor,
                    **holder_payload(task),
                },
            )
    if if_status is not None:
        current_status = str(task.get("status") or "pending")
        if current_status != if_status:
            raise ClaimError(
                f"guard mismatch: expected status {if_status!r}, found {current_status!r}",
                reason=REASON_GUARD_MISMATCH,
                details={
                    "guard": "if_status",
                    "expected": if_status,
                    "actual": current_status,
                    **holder_payload(task),
                },
            )


def readiness_block_for_task(ledger: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    """Return the blocked entry for ``task_id`` when it is not ready, else None."""
    resolution = edges_mod.resolve_readiness(ledger)
    if any(item.get("id") == task_id for item in resolution.ready):
        return None
    for item in resolution.blocked:
        if item.get("id") == task_id:
            return item
    task_map = {
        str(task.get("id")): task for task in ledger.get("tasks", []) if isinstance(task, dict) and task.get("id")
    }
    task = task_map.get(task_id)
    status = str(task.get("status") or "pending") if task else "missing"
    return {
        "id": task_id,
        "title": str(task.get("text") or "") if task else "",
        "status": status,
        "reason": "not_ready",
        "blockers": [],
    }


def apply_claim_to_task(
    ledger: dict[str, Any],
    task: dict[str, Any],
    *,
    actor: str,
    claim_id: str,
    if_actor: str | None = None,
    if_status: str | None = None,
    require_ready: bool = True,
) -> dict[str, Any]:
    """Mutate ``task`` inside ``ledger`` to claim it. Raises ``ClaimError`` on reject."""
    actor = normalize_filter_value(actor, field="actor") or ""
    claim_id = normalize_filter_value(claim_id, field="claim_id") or ""
    if_actor = normalize_filter_value(if_actor, field="if_actor") if if_actor is not None else None
    if_status = normalize_filter_value(if_status, field="if_status") if if_status is not None else None

    _check_guards(task, if_actor=if_actor, if_status=if_status)

    existing = claim_record(task)
    if existing is not None:
        if existing["claim_id"] == claim_id:
            return {
                "task_id": task.get("id"),
                "status": task.get("status"),
                "assignee": existing["actor"],
                "claim": existing,
                "created": False,
                "idempotent": True,
            }
        raise ClaimError(
            conflict_message(task),
            reason=REASON_ALREADY_CLAIMED,
            details=holder_payload(task),
        )

    status = str(task.get("status") or "pending")
    if status != "pending":
        raise ClaimError(
            f"task {task.get('id')} is not ready for claim (status={status})",
            reason=REASON_NOT_READY,
            details={"task_id": task.get("id"), "status": status, "block": {"reason": "wrong_status"}},
        )

    if require_ready:
        block = readiness_block_for_task(ledger, str(task.get("id")))
        if block is not None:
            raise ClaimError(
                f"task {task.get('id')} is not ready for claim (reason={block.get('reason')})",
                reason=REASON_NOT_READY,
                details={"task_id": task.get("id"), "status": status, "block": block},
            )

    now = helpers._now().isoformat()
    record = _set_claim(task, actor=actor, claim_id=claim_id, claimed_at=now)
    return {
        "task_id": task.get("id"),
        "status": task.get("status"),
        "assignee": actor,
        "claim": record,
        "created": True,
        "idempotent": False,
    }


def apply_claim_next(
    ledger: dict[str, Any],
    *,
    actor: str,
    claim_id: str,
    if_actor: str | None = None,
    if_status: str | None = None,
) -> dict[str, Any]:
    """Claim the highest-priority ready task under one ledger snapshot."""
    resolution = edges_mod.resolve_readiness(ledger)
    ordered = sorted(resolution.ready, key=ready_sort_key)
    task_map = {
        str(task.get("id")): task for task in ledger.get("tasks", []) if isinstance(task, dict) and task.get("id")
    }
    for item in ordered:
        task = task_map.get(str(item.get("id")))
        if task is None:
            continue
        # Already claimed in this snapshot should not appear in ready, but walk
        # past anything that became unclaimable without failing the whole --next.
        existing = claim_record(task)
        if existing is not None:
            continue
        try:
            result = apply_claim_to_task(
                ledger,
                task,
                actor=actor,
                claim_id=claim_id,
                if_actor=if_actor,
                if_status=if_status,
                require_ready=True,
            )
        except ClaimError as exc:
            if exc.reason in {REASON_ALREADY_CLAIMED, REASON_NOT_READY, REASON_GUARD_MISMATCH}:
                continue
            raise
        result["selected_from"] = "ready"
        result["ready_count"] = len(ordered)
        return result
    raise ClaimError(
        "no ready task available to claim",
        reason=REASON_NO_READY,
        details={"ready_count": len(ordered)},
        exit_code=1,
    )


def apply_release_to_task(
    ledger: dict[str, Any],
    task: dict[str, Any],
    *,
    claim_id: str | None = None,
    actor: str | None = None,
    if_actor: str | None = None,
    if_status: str | None = None,
) -> dict[str, Any]:
    """Release one claimed task. Compares claim_id when provided so a late
    reaper cannot clear a newer claim for the same actor.
    """
    if_actor = normalize_filter_value(if_actor, field="if_actor") if if_actor is not None else None
    if_status = normalize_filter_value(if_status, field="if_status") if if_status is not None else None
    claim_id = normalize_filter_value(claim_id, field="claim_id") if claim_id is not None else None
    actor = normalize_filter_value(actor, field="actor") if actor is not None else None

    _check_guards(task, if_actor=if_actor, if_status=if_status)
    existing = claim_record(task)
    if existing is None:
        raise ClaimError(
            f"task {task.get('id')} is not claimed",
            reason=REASON_NOT_CLAIMED,
            details={"task_id": task.get("id"), "status": task.get("status", "pending")},
            exit_code=1,
        )
    if claim_id is not None and existing["claim_id"] != claim_id:
        raise ClaimError(
            f"claim id mismatch for task {task.get('id')}",
            reason=REASON_CLAIM_ID_MISMATCH,
            details={
                "expected_claim_id": claim_id,
                "actual_claim_id": existing["claim_id"],
                **holder_payload(task),
            },
        )
    if actor is not None and existing["actor"] != actor:
        raise ClaimError(
            conflict_message(task),
            reason=REASON_GUARD_MISMATCH,
            details={
                "guard": "actor",
                "expected": actor,
                "actual": existing["actor"],
                **holder_payload(task),
            },
        )
    now = helpers._now().isoformat()
    released = dict(existing)
    _clear_claim(task, now=now)
    return {
        "task_id": task.get("id"),
        "status": task.get("status"),
        "released": released,
    }


def apply_reassign_to_task(
    ledger: dict[str, Any],
    task: dict[str, Any],
    *,
    to_actor: str,
    claim_id: str | None = None,
    new_claim_id: str | None = None,
    if_actor: str | None = None,
    if_status: str | None = None,
) -> dict[str, Any]:
    """Reassign a live claim to a new actor, keeping status in_progress."""
    to_actor = normalize_filter_value(to_actor, field="to_actor") or ""
    if_actor = normalize_filter_value(if_actor, field="if_actor") if if_actor is not None else None
    if_status = normalize_filter_value(if_status, field="if_status") if if_status is not None else None
    claim_id = normalize_filter_value(claim_id, field="claim_id") if claim_id is not None else None
    resolved_claim_id = (
        normalize_filter_value(new_claim_id, field="new_claim_id") if new_claim_id is not None else generate_claim_id()
    )

    _check_guards(task, if_actor=if_actor, if_status=if_status)
    existing = claim_record(task)
    if existing is None:
        raise ClaimError(
            f"task {task.get('id')} is not claimed",
            reason=REASON_NOT_CLAIMED,
            details={"task_id": task.get("id"), "status": task.get("status", "pending")},
            exit_code=1,
        )
    if claim_id is not None and existing["claim_id"] != claim_id:
        raise ClaimError(
            f"claim id mismatch for task {task.get('id')}",
            reason=REASON_CLAIM_ID_MISMATCH,
            details={
                "expected_claim_id": claim_id,
                "actual_claim_id": existing["claim_id"],
                **holder_payload(task),
            },
        )
    now = helpers._now().isoformat()
    assert isinstance(resolved_claim_id, str)
    record = _set_claim(task, actor=to_actor, claim_id=resolved_claim_id, claimed_at=now)
    return {
        "task_id": task.get("id"),
        "status": task.get("status"),
        "assignee": to_actor,
        "previous": existing,
        "claim": record,
    }


def claim_age_hours(task: dict[str, Any], *, now: Any | None = None) -> float | None:
    record = claim_record(task)
    if record is None or not record.get("claimed_at"):
        return None
    claimed_dt = helpers._parse_iso_datetime(record["claimed_at"])
    if claimed_dt is None:
        return None
    current = now or helpers._now()
    return (current - claimed_dt).total_seconds() / 3600.0


def list_claimed_tasks(ledger: dict[str, Any]) -> list[dict[str, Any]]:
    claimed: list[dict[str, Any]] = []
    for task in ledger.get("tasks", []):
        if not isinstance(task, dict):
            continue
        record = claim_record(task)
        if record is None:
            continue
        claimed.append(task)
    claimed.sort(key=lambda item: str(item.get("id") or ""))
    return claimed


def list_stale_claims(
    ledger: dict[str, Any],
    *,
    stale_after_hours: float,
    now: Any | None = None,
) -> list[dict[str, Any]]:
    stale: list[dict[str, Any]] = []
    for task in list_claimed_tasks(ledger):
        age = claim_age_hours(task, now=now)
        if age is None:
            continue
        if age >= stale_after_hours:
            record = claim_record(task)
            assert record is not None
            stale.append(
                {
                    "task_id": task.get("id"),
                    "text": str(task.get("text") or ""),
                    "assignee": record["actor"],
                    "claim_id": record["claim_id"],
                    "claimed_at": record["claimed_at"],
                    "age_hours": round(age, 2),
                }
            )
    return stale


def task_matches_release_filters(
    task: dict[str, Any],
    *,
    actors: list[str] | None = None,
    claim_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    stale_after_hours: float | None = None,
    now: Any | None = None,
) -> bool:
    record = claim_record(task)
    if record is None:
        return False
    if task_ids is not None and str(task.get("id")) not in task_ids:
        return False
    if actors is not None and record["actor"] not in actors:
        return False
    if claim_ids is not None and record["claim_id"] not in claim_ids:
        return False
    if stale_after_hours is not None:
        age = claim_age_hours(task, now=now)
        if age is None or age < stale_after_hours:
            return False
    return True
