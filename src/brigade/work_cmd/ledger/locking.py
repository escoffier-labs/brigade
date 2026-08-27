"""Stage 1 compatibility seam for the task and import ledger."""
# ruff: noqa: F401

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence, cast
from uuid import uuid4

from .. import constants, edges as edges_mod, helpers, inbox_lock
from ..inbox_lock import verify_canonical_write_locks
from ... import component_paths, evidence_redaction, provenance, runguard, trust_gate
from ...untrusted import scan_handoff_injection_heuristics

from . import queries_handoffs, tasks_plans


def _task_ledger_lock_path(target: Path) -> Path:
    path = helpers._tasks_path(target)
    return path.with_name(f"{path.name}.lock")


#: Bounded window while an outside writer waits for the task-ledger lock.
#: Patient by design so concurrent trusted writers serialize behind a brief
#: holder instead of failing; bounded so a lost holder cannot block them
#: forever (mirrors ``inbox_lock.DEFAULT_LOCK_DEADLINE_SECONDS``).
TASK_LEDGER_LOCK_DEADLINE_SECONDS = inbox_lock.DEFAULT_LOCK_DEADLINE_SECONDS

#: Shorter bound inside a scanner run: a marked child holding the writer lock
#: must not pin the task-ledger lock behind another holder for long.
SCANNER_CHILD_TASK_LEDGER_LOCK_DEADLINE_SECONDS = 5.0

#: Poll interval while a bounded task-ledger acquisition waits for a busy lock.
_TASK_LEDGER_LOCK_RETRY_INTERVAL_SECONDS = 0.05


class TaskLedgerLockTimeout(TimeoutError):
    """Typed failure when the task-ledger lock stayed busy past its deadline."""


def _task_ledger_lock_deadline_seconds() -> float:
    """Return this process's bounded wait for the task-ledger lock.

    Marked scanner children stay short so a child already holding the writer
    lock cannot pin the task-ledger lock for a run window; outside writers get
    the patient bounded window so concurrent trusted writers serialize instead
    of failing immediately.
    """
    if inbox_lock.inside_scanner_run():
        return SCANNER_CHILD_TASK_LEDGER_LOCK_DEADLINE_SECONDS
    return TASK_LEDGER_LOCK_DEADLINE_SECONDS


@contextmanager
def _canonical_inbox_write(target: Path) -> Iterator[None]:
    """Lock set for one canonical inbox read-modify-write (run then writer).

    Inside a scanner run (``BRIGADE_SCANNER_RUN_ID``, a marker that carries no
    capability) a self-importing child takes only the writer lock, which it
    opens itself; outside writers take the run lock first and then the writer
    lock. Resolved through the ``inbox_lock`` module so tests can instrument
    either lock in one place.
    """
    if inbox_lock.inside_scanner_run():
        with inbox_lock.inbox_writer_lock(target):
            yield
    else:
        with inbox_lock.scanner_inbox_run_lock(target), inbox_lock.inbox_writer_lock(target):
            yield


def _acquire_task_ledger_lock(path: Path) -> runguard._LockOwnership:
    # Bounded retry so concurrent trusted writers serialize behind a brief
    # holder instead of failing (the lock order task -> run -> writer means no
    # holder ever waits on an inbox lock here). Inside a scanner run the bound
    # is short and fails closed with ``RunLockError``: a marked child already
    # holding the writer lock must never pin the task-ledger lock behind
    # another holder for a long window. Outside writers are patient but still
    # bounded, raising the typed timeout on expiry.
    requested_seconds = _task_ledger_lock_deadline_seconds()
    deadline = time.monotonic() + requested_seconds
    while True:
        try:
            return runguard._acquire_lock(path)
        except runguard.RunLockError as exc:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if inbox_lock.inside_scanner_run():
                    raise
                raise TaskLedgerLockTimeout(f"task ledger lock stayed busy for {requested_seconds:g}s: {path}") from exc
            time.sleep(min(_TASK_LEDGER_LOCK_RETRY_INTERVAL_SECONDS, remaining))


@contextmanager
def _task_ledger_lock(target: Path) -> Iterator[None]:
    path = _task_ledger_lock_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    ownership = _acquire_task_ledger_lock(path)
    try:
        yield
    finally:
        runguard._release_lock(path, ownership)


REASON_CORRUPT_LEDGER = "corrupt_ledger"
REASON_UNSUPPORTED_LEDGER_VERSION = "unsupported_ledger_version"
REASON_TRUST_POLICY = "trust_policy"
REASON_IMPORT_SNAPSHOT_CHANGED = "import_snapshot_changed"

# Byte budget enforced while reading one import-inbox snapshot, so growth
# after any earlier size check can never drive unbounded chunk accumulation.
_IMPORT_INBOX_SNAPSHOT_LIMIT_BYTES = 4 * 1024 * 1024


class ImportInboxSnapshotLimitExceeded(OSError):
    """Typed failure when an import inbox grows past its snapshot read cap."""


class TaskLedgerError(ValueError):
    """Fail-closed read/parse failure for ``.brigade/work/tasks.json``."""

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        details: dict[str, Any] | None = None,
        exit_code: int = 2,
    ):
        super().__init__(message)
        self.reason = reason
        self.details = details or {}
        self.exit_code = exit_code

    def as_dict(self) -> dict[str, Any]:
        payload = {"error": str(self), "reason": self.reason, "exit_code": self.exit_code}
        payload.update(self.details)
        return payload


def emit_task_ledger_error(exc: TaskLedgerError, *, json_output: bool = False) -> int:
    """Print a ``TaskLedgerError`` with the same contract as ``task_add`` (rc=2)."""
    if json_output:
        print(json.dumps(exc.as_dict(), indent=2, sort_keys=True))
    else:
        print(f"error: {exc}", file=sys.stderr)
    return int(exc.exit_code)


def guard_task_ledger_dispatch(args: Any, impl: Callable[[Any], int]) -> int:
    """Catch ``TaskLedgerError`` from a CLI dispatch implementation."""
    try:
        return impl(args)
    except TaskLedgerError as exc:
        return emit_task_ledger_error(exc, json_output=bool(getattr(args, "json", False)))


def _read_task_ledger(target: Path) -> dict[str, Any]:
    path = helpers._tasks_path(target)
    if not path.exists():
        return {"version": edges_mod.TASK_LEDGER_VERSION, "tasks": [], "edges": []}
    try:
        raw = path.read_text()
    except OSError as exc:
        raise TaskLedgerError(
            f"task ledger is unreadable: {path}",
            reason=REASON_CORRUPT_LEDGER,
            details={"path": str(path), "error": str(exc)},
        ) from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise TaskLedgerError(
            f"task ledger is corrupt (invalid JSON): {path}",
            reason=REASON_CORRUPT_LEDGER,
            details={"path": str(path), "error": str(exc)},
        ) from exc
    if not isinstance(payload, dict):
        raise TaskLedgerError(
            f"task ledger is corrupt (root must be an object): {path}",
            reason=REASON_CORRUPT_LEDGER,
            details={"path": str(path)},
        )
    version = payload.get("version", 1)
    if isinstance(version, bool) or not isinstance(version, int):
        raise TaskLedgerError(
            f"task ledger has invalid version {version!r}: {path}",
            reason=REASON_UNSUPPORTED_LEDGER_VERSION,
            details={
                "path": str(path),
                "version": version,
                "supported": edges_mod.TASK_LEDGER_VERSION,
            },
        )
    if version > edges_mod.TASK_LEDGER_VERSION:
        raise TaskLedgerError(
            f"task ledger version {version} is newer than supported {edges_mod.TASK_LEDGER_VERSION}: {path}",
            reason=REASON_UNSUPPORTED_LEDGER_VERSION,
            details={
                "path": str(path),
                "version": version,
                "supported": edges_mod.TASK_LEDGER_VERSION,
            },
        )
    if version < 1:
        raise TaskLedgerError(
            f"task ledger has invalid version {version}: {path}",
            reason=REASON_UNSUPPORTED_LEDGER_VERSION,
            details={
                "path": str(path),
                "version": version,
                "supported": edges_mod.TASK_LEDGER_VERSION,
            },
        )
    tasks = payload.get("tasks")
    if not isinstance(tasks, list):
        payload["tasks"] = []
    edges_mod.ensure_ledger_edges(payload)
    return payload


def _write_task_ledger(target: Path, payload: dict[str, Any]) -> None:
    edges_mod.ensure_ledger_edges(payload)
    helpers._write_json(helpers._tasks_path(target), payload)


def _claim_task(
    target: Path,
    task_id: str | None,
    *,
    actor: str,
    claim_id: str | None = None,
    claim_next: bool = False,
    if_actor: str | None = None,
    if_status: str | None = None,
) -> dict[str, Any]:
    """CAS claim under the task-ledger lock. Raises ``claim.ClaimError``."""
    from ..claiming import (
        ClaimError,
        REASON_ALREADY_CLAIMED,
        REASON_GUARD_MISMATCH,
        REASON_NO_READY,
        REASON_NOT_READY,
        apply_claim_to_task,
        generate_claim_id,
        ready_sort_key,
    )

    resolved_claim_id = claim_id or generate_claim_id()
    with _task_ledger_lock(target):
        ledger = _read_task_ledger(target)
        if claim_next:
            resolution = edges_mod.resolve_readiness(ledger)
            ordered = sorted(resolution.ready, key=ready_sort_key)
            task_map = {
                str(task.get("id")): task
                for task in ledger.get("tasks", [])
                if isinstance(task, dict) and task.get("id")
            }
            for item in ordered:
                task = task_map.get(str(item.get("id")))
                if task is None:
                    continue
                try:
                    queries_handoffs._evaluate_task_start_gates(target, ledger, task)
                except ClaimError as exc:
                    if exc.reason in {
                        REASON_ALREADY_CLAIMED,
                        REASON_NOT_READY,
                        REASON_GUARD_MISMATCH,
                        "unresolved_plan_decisions",
                        tasks_plans.REASON_MISSING_PLAN_RECEIPT,
                        tasks_plans.REASON_CORRUPT_PLAN_RECEIPT,
                        tasks_plans.REASON_PLAN_RECEIPT_MISMATCH,
                        tasks_plans.REASON_MALFORMED_PLAN_DECISIONS,
                    }:
                        continue
                    raise
                try:
                    result = apply_claim_to_task(
                        ledger,
                        task,
                        actor=actor,
                        claim_id=resolved_claim_id,
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
                _write_task_ledger(target, ledger)
                return result
            raise ClaimError(
                "no ready task available to claim",
                reason=REASON_NO_READY,
                details={"ready_count": len(ordered)},
                exit_code=1,
            )
        if not task_id:
            raise ClaimError(
                "task id is required unless --next is set",
                reason="unknown_task",
                details={},
                exit_code=2,
            )
        task = None
        matches: list[dict[str, Any]] = []
        for item in ledger["tasks"]:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if item_id == task_id:
                task = item
                break
            if isinstance(item_id, str) and item_id.startswith(task_id):
                matches.append(item)
        if task is None and len(matches) == 1:
            task = matches[0]
        if task is None:
            raise ClaimError(
                f"task not found: {task_id}",
                reason="unknown_task",
                details={"task_id": task_id},
                exit_code=1,
            )
        queries_handoffs._evaluate_task_start_gates(target, ledger, task)
        result = apply_claim_to_task(
            ledger,
            task,
            actor=actor,
            claim_id=resolved_claim_id,
            if_actor=if_actor,
            if_status=if_status,
            require_ready=True,
        )
        _write_task_ledger(target, ledger)
        return result


def _release_tasks(
    target: Path,
    *,
    task_ids: list[str] | None = None,
    actors: list[str] | None = None,
    claim_ids: list[str] | None = None,
    stale_after_hours: float | None = None,
    if_actor: str | None = None,
    if_status: str | None = None,
) -> dict[str, Any]:
    """Release matching claims under the task-ledger lock (fail-closed filters)."""
    from ..claiming import (
        ClaimError,
        REASON_MISSING_FILTER,
        apply_release_to_task,
        claim_record,
        normalize_filter_list,
        task_matches_release_filters,
    )

    # Normalize filters before taking the lock so empty values never match-all.
    normalized_task_ids = normalize_filter_list(task_ids, field="task") if task_ids is not None else None
    normalized_actors = normalize_filter_list(actors, field="actor") if actors is not None else None
    normalized_claim_ids = normalize_filter_list(claim_ids, field="claim_id") if claim_ids is not None else None
    if normalized_task_ids is None and normalized_actors is None and normalized_claim_ids is None:
        if stale_after_hours is None:
            raise ClaimError(
                "release requires a filter (--task, --actor, and/or --claim-id)",
                reason=REASON_MISSING_FILTER,
                details={},
                exit_code=2,
            )

    with _task_ledger_lock(target):
        ledger = _read_task_ledger(target)
        released: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for task in list(ledger.get("tasks", [])):
            if not isinstance(task, dict):
                continue
            if not task_matches_release_filters(
                task,
                actors=normalized_actors,
                claim_ids=normalized_claim_ids,
                task_ids=normalized_task_ids,
                stale_after_hours=stale_after_hours,
            ):
                continue
            existing = claim_record(task)
            try:
                result = apply_release_to_task(
                    ledger,
                    task,
                    # Compare the claim identity observed under this lock so a
                    # concurrent reassignment cannot be cleared by a stale match.
                    claim_id=existing["claim_id"] if existing is not None else None,
                    if_actor=if_actor,
                    if_status=if_status,
                )
                released.append(result)
            except ClaimError as exc:
                skipped.append(exc.as_dict())
        if released:
            _write_task_ledger(target, ledger)
        result = {"released": released, "released_count": len(released), "skipped": skipped}
        if not released and skipped:
            # Propagate precondition failures (guard mismatch / claim-id) when
            # every matched item was rejected and nothing was written.
            exit_codes = [int(item.get("exit_code") or 13) for item in skipped if isinstance(item, dict)]
            if exit_codes and all(code == 13 for code in exit_codes):
                raise ClaimError(
                    str(skipped[0].get("error") or "release guard mismatch"),
                    reason=str(skipped[0].get("reason") or "guard_mismatch"),
                    details={"skipped": skipped, **{k: v for k, v in skipped[0].items() if k != "skipped"}},
                    exit_code=13,
                )
        return result


def _reassign_task(
    target: Path,
    task_id: str,
    *,
    to_actor: str,
    claim_id: str | None = None,
    new_claim_id: str | None = None,
    if_actor: str | None = None,
    if_status: str | None = None,
) -> dict[str, Any]:
    from ..claiming import ClaimError, apply_reassign_to_task

    with _task_ledger_lock(target):
        ledger = _read_task_ledger(target)
        task = None
        matches: list[dict[str, Any]] = []
        for item in ledger["tasks"]:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            if item_id == task_id:
                task = item
                break
            if isinstance(item_id, str) and item_id.startswith(task_id):
                matches.append(item)
        if task is None and len(matches) == 1:
            task = matches[0]
        if task is None:
            raise ClaimError(
                f"task not found: {task_id}",
                reason="unknown_task",
                details={"task_id": task_id},
                exit_code=1,
            )
        result = apply_reassign_to_task(
            ledger,
            task,
            to_actor=to_actor,
            claim_id=claim_id,
            new_claim_id=new_claim_id,
            if_actor=if_actor,
            if_status=if_status,
        )
        _write_task_ledger(target, ledger)
        return result
