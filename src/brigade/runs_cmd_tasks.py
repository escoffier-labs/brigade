"""Per-task status, results, waiting and the explicit result actions.

The durable facts live in the run journal and are read through
``brigade.run_tasks.read_tasks``. Status, results and waiting add no state: they
never append, cancel or reconcile anything, and a wait timeout records nothing.
The explicit actions are the exceptions, and each one lives in its own module:
``integrate`` cuts a new local ref, ``dispose`` removes a worktree after a
retained-copy proof, ``collect`` recovers an interrupted collection, and
``cancel`` records a cancellation intent through the per-run command inbox.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import stat
import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import runguard, run_journal, run_lifecycle, run_task_journal
from . import run_tasks as tasks

RUN_TASKS_SCHEMA = "brigade.runs_tasks.v1"
RUN_TASKS_NEXT_SCHEMA = "brigade.runs_tasks_next.v1"
RUN_TASKS_ACK_SCHEMA = "brigade.runs_tasks_ack.v1"
POLL_INTERVAL_SECONDS = 0.5
# A refusal that persists this many polls is reported instead of waited out.
MAX_CONSECUTIVE_READ_ERRORS = 20
MAX_KNOWN_TASK_IDS = 10
_SIDECAR_CHUNK_BYTES = 1024 * 1024
_SIDECAR_MAX_BYTES = 64 * 1024 * 1024

_COLUMNS = (
    ("task", "task_id", 24),
    ("status", "status", 22),
    ("attempt", "attempt_id", 12),
    ("terminal", "terminal", 8),
    ("ack", "acknowledged", 5),
    ("cancel", "cancel", 21),
    ("containment", "containment", 11),
    ("result", "result_id", 12),
    ("artifact", "artifact_kind", 11),
    ("digest", "artifact_digest", 12),
)


@dataclass(frozen=True)
class TaskRow:
    """The public projection of one durable task result."""

    task_id: str
    status: str
    attempt_id: str | None
    terminal: bool
    acknowledged: bool
    containment: str
    result_id: str | None
    artifact_kind: str | None
    artifact_digest: str | None
    cancel: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        """The closed row schema. JSON emission sorts keys for stable output."""
        return {
            "task_id": self.task_id,
            "status": self.status,
            "attempt_id": self.attempt_id,
            "terminal": self.terminal,
            "acknowledged": self.acknowledged,
            "containment": self.containment,
            "result_id": self.result_id,
            "artifact_kind": self.artifact_kind,
            "artifact_digest": self.artifact_digest,
            "cancel": self.cancel,
        }


def _pending_cancel_reasons(run_dir: Path) -> dict[str, str]:
    """Cancel reasons whose request is still only in the inbox, bounded."""
    from . import run_task_inbox

    try:
        pending = run_task_inbox.pending(run_dir)
    except run_task_inbox.TaskInboxError:
        return {}
    reasons: dict[str, str] = {}
    for request in pending.requests:
        if request.kind == run_task_inbox.KIND_CANCEL:
            assert request.reason is not None
            reasons.setdefault(request.task_id, request.reason)
    return reasons


def _cancel_state(result: tasks.TaskResult, pending: dict[str, str]) -> dict[str, Any]:
    """Journaled intent wins; an inbox-only request is reported as queued."""
    if result.cancel_requested:
        return {"requested": True, "queued": False, "reason": result.cancel_reason, "too_late": result.cancel_too_late}
    reason = pending.get(result.handle.spec.task_id)
    if reason is not None:
        return {"requested": False, "queued": True, "reason": reason}
    return {"requested": False, "queued": False, "reason": None}


def _row(
    result: tasks.TaskResult, pending: dict[str, str] | None = None, acknowledged: set[tuple[str, str]] | None = None
) -> TaskRow:
    return TaskRow(
        task_id=result.handle.spec.task_id,
        status=result.status,
        attempt_id=result.handle.attempt_id,
        terminal=result.status in run_task_journal.TERMINAL,
        acknowledged=result.acknowledged
        or (result.handle.spec.task_id, result.result_id or "") in (acknowledged or set()),
        containment=result.containment,
        result_id=result.result_id,
        artifact_kind=result.artifact_kind,
        artifact_digest=result.artifact_digest,
        cancel=_cancel_state(result, pending or {}),
    )


def _bounded_ids(task_ids: Iterable[str]) -> list[str]:
    """Sorted, deduplicated, and capped so a diagnostic stays bounded."""
    return sorted(set(task_ids))[:MAX_KNOWN_TASK_IDS]


def _fail(code: str, detail: str) -> int:
    print(f"error: {code}: {detail}", file=sys.stderr)
    return 2


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _journal_path(run_dir: Path) -> Path:
    return run_lifecycle._journal_path(run_dir)


def _journal_present(run_dir: Path) -> bool:
    return run_journal.bound_is_file(_journal_path(run_dir))


def _pending_acks(run_dir: Path) -> set[tuple[str, str]]:
    from . import run_task_inbox

    # Fail closed: an unreadable inbox could hide a queued acknowledgement, so
    # the caller refuses instead of re-offering a result already consumed.
    requests = run_task_inbox.pending(run_dir).requests
    return {
        (request.task_id, request.result_id)
        for request in requests
        if request.kind == run_task_inbox.KIND_ACK and request.result_id is not None
    }


def _terminal_order(run_dir: Path) -> dict[str, int]:
    """Return the first terminal observation position, preserving journal order."""
    try:
        with run_task_journal.transaction(run_dir) as (_run_dir, events):
            order: dict[str, int] = {}
            for position, event in enumerate(events):
                if event.event_type == "task.observed" and event.payload.get("status") in run_task_journal.TERMINAL:
                    order.setdefault(event.payload["task_id"], position)
            return order
    except run_task_journal.TaskJournalError:
        return {}


def _resolve(run: str | Path, *, cwd: Path, runs_dir: Path | None) -> Path | None:
    from . import runs_cmd

    run_dir, error = runs_cmd._resolve_run_dir(run, cwd=cwd, runs_dir=runs_dir)
    if error is not None:
        print(error, file=sys.stderr)
        return None
    return run_dir


def _read(run_dir: Path) -> tuple[tasks.TaskResult, ...] | None:
    if not _journal_present(run_dir):
        _fail("no-task-journal", f"no task journal in run {run_dir.name}")
        return None
    try:
        return tasks.read_tasks(run_dir)
    except tasks.TaskJournalError as exc:
        _fail("task-journal-unreadable", str(exc))
        return None


def _unknown(task_id: str, known: Iterable[str]) -> int:
    detail = f"{task_id} is not an accepted task"
    ids = _bounded_ids(known)
    if ids:
        detail = f"{detail}; known task ids: {', '.join(ids)}"
    return _fail("unknown-task", detail)


def _sidecar(run_dir: Path, name: str) -> tuple[str | None, str | None]:
    """A receipt sidecar's reference and digest, when that regular file exists."""
    path = run_dir / name
    digest = hashlib.sha256()
    try:
        # Never follow a symlink out of the run directory, and only hash a
        # regular file of bounded size.
        fd = run_journal._open_nofollow(path, os.O_RDONLY)
    except (OSError, run_journal.RunJournalError):
        return None, None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _SIDECAR_MAX_BYTES:
            return None, None
        with os.fdopen(fd, "rb") as handle:
            fd = -1
            for chunk in iter(lambda: handle.read(_SIDECAR_CHUNK_BYTES), b""):
                digest.update(chunk)
    except OSError:
        return None, None
    finally:
        if fd >= 0:
            os.close(fd)
    return name, digest.hexdigest()


def _result_sidecar(run_dir: Path, task_id: str) -> tuple[str | None, str | None]:
    """The batch result receipt reference and digest, when that file exists."""
    return _sidecar(run_dir, f"batch-result-{hashlib.sha256(task_id.encode()).hexdigest()}.json")


def _artifact_sidecar(run_dir: Path, task_id: str) -> tuple[str | None, str | None]:
    """The batch task artifact reference and digest, when that file exists."""
    return _sidecar(run_dir, f"batch-artifact-{hashlib.sha256(task_id.encode()).hexdigest()}.json")


# A batch request marker is read through run_batch.read_request_marker, which
# owns the no-follow, bounded read.
def _selection(run_dir: Path, task_id: str) -> dict[str, Any] | None:
    """The batch task's selector, read only from a bounded request marker.

    Seat-only batches show ``{"kind": "seat", "capability": null}``. The marker
    is read no-follow and bounded, so a symlinked or oversized one reports no
    selection instead of being followed. A run without a readable batch marker
    has no selection to report.
    """
    from .run_batch import BatchError, read_request_marker

    try:
        payload = read_request_marker(run_dir) or {}
    except BatchError:
        return None
    for row in payload.get("tasks") or ():
        if isinstance(row, dict) and row.get("task_id") == task_id:
            capability = row.get("capability")
            kind = "capability" if isinstance(capability, str) else "seat"
            return {"kind": kind, "capability": capability if isinstance(capability, str) else None}
    return None


def _selection_text(selection: dict[str, Any] | None) -> str:
    if selection is None:
        return "-"
    capability = selection.get("capability")
    return f"capability {capability}" if selection.get("kind") == "capability" else "seat"


def _cancel_text(cancel: dict[str, Any]) -> str:
    if cancel.get("requested"):
        return f"requested ({cancel['reason']})"
    if cancel.get("queued"):
        return f"queued ({cancel['reason']})"
    return "-"


def _cell(row: TaskRow, field: str, width: int) -> str:
    value = getattr(row, field)
    if isinstance(value, dict):
        return _cancel_text(value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    if value is None or value == "":
        return "-"
    return str(value)[:width]


def _print_table(rows: Sequence[TaskRow]) -> None:
    if not rows:
        print("no tasks")
        return
    print("  ".join(label.ljust(width) for label, _field, width in _COLUMNS))
    for row in rows:
        print("  ".join(_cell(row, field, width).ljust(width) for _label, field, width in _COLUMNS))


def _print_detail(
    row: TaskRow,
    result_ref: str | None,
    result_sha256: str | None,
    artifact_ref: str | None,
    artifact_sha256: str | None,
    selection: dict[str, Any] | None = None,
) -> None:
    for field in (
        "task_id",
        "status",
        "attempt_id",
        "terminal",
        "acknowledged",
        "cancel",
        "containment",
        "result_id",
        "artifact_kind",
        "artifact_digest",
    ):
        cell = _cell(row, field, 1024)
        print(f"{field}: {cell}")
    print(f"selection: {_selection_text(selection)}")
    print(f"result_ref: {result_ref if result_ref is not None else '-'}")
    print(f"result_sha256: {result_sha256 if result_sha256 is not None else '-'}")
    print(f"artifact_ref: {artifact_ref if artifact_ref is not None else '-'}")
    print(f"artifact_sha256: {artifact_sha256 if artifact_sha256 is not None else '-'}")


def _wait_payload(
    run_id: str,
    rows: Sequence[TaskRow],
    *,
    timed_out: bool,
    timeout: float | None,
    waiting_for: Sequence[str],
) -> dict[str, Any]:
    return {
        "schema": RUN_TASKS_SCHEMA,
        "run_id": run_id,
        "timed_out": timed_out,
        "timeout_seconds": timeout,
        "waiting_for": list(waiting_for),
        "tasks": [row.to_dict() for row in rows],
    }


def list_tasks(
    run: str | Path,
    *,
    cwd: Path,
    runs_dir: Path | None = None,
    json_output: bool = False,
) -> int:
    run_dir = _resolve(run, cwd=cwd, runs_dir=runs_dir)
    if run_dir is None:
        return 2
    results = _read(run_dir)
    if results is None:
        return 2
    pending = _pending_cancel_reasons(run_dir)
    acks = _pending_acks(run_dir)
    rows = [_row(result, pending, acks) for result in results]
    if json_output:
        _emit(
            {
                "schema": RUN_TASKS_SCHEMA,
                "run_id": run_dir.name,
                "tasks": [row.to_dict() for row in rows],
            }
        )
    else:
        _print_table(rows)
    return 0


def show_task(
    run: str | Path,
    task_id: str,
    *,
    cwd: Path,
    runs_dir: Path | None = None,
    json_output: bool = False,
) -> int:
    run_dir = _resolve(run, cwd=cwd, runs_dir=runs_dir)
    if run_dir is None:
        return 2
    results = _read(run_dir)
    if results is None:
        return 2
    by_id = {result.handle.spec.task_id: result for result in results}
    if task_id not in by_id:
        return _unknown(task_id, by_id)
    row = _row(by_id[task_id], _pending_cancel_reasons(run_dir), _pending_acks(run_dir))
    result_ref, result_sha256 = _result_sidecar(run_dir, task_id)
    artifact_ref, artifact_sha256 = _artifact_sidecar(run_dir, task_id)
    selection = _selection(run_dir, task_id)
    if json_output:
        _emit(
            {
                "schema": RUN_TASKS_SCHEMA,
                "run_id": run_dir.name,
                "task": {
                    **row.to_dict(),
                    "selection": selection,
                    "result_ref": result_ref,
                    "result_sha256": result_sha256,
                    "artifact_ref": artifact_ref,
                    "artifact_sha256": artifact_sha256,
                },
            }
        )
    else:
        _print_detail(row, result_ref, result_sha256, artifact_ref, artifact_sha256, selection)
    return 0


def wait_tasks(
    run: str | Path,
    task_ids: Sequence[str] = (),
    *,
    cwd: Path,
    runs_dir: Path | None = None,
    timeout: float | None = None,
    json_output: bool = False,
    interval: float = POLL_INTERVAL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Wait for named tasks (default all) to be terminal. Never mutates anything."""
    if not math.isfinite(interval) or interval <= 0:
        return _fail("invalid-interval", "poll interval must be greater than zero")
    if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
        return _fail("invalid-timeout", "timeout must not be negative")
    run_dir = _resolve(run, cwd=cwd, runs_dir=runs_dir)
    if run_dir is None:
        return 2
    if not _journal_present(run_dir):
        return _fail("no-task-journal", f"no task journal in run {run_dir.name}")

    selected = list(dict.fromkeys(task_ids))
    deadline = None if timeout is None else monotonic() + timeout
    latest: list[TaskRow] | None = None
    last_error: tasks.TaskJournalError | None = None
    consecutive_errors = 0
    while True:
        try:
            results = tasks.read_tasks(run_dir)
        except tasks.TaskJournalError as exc:
            # A live dispatcher can transiently refuse between a task's
            # checkpoint and its fact. Keep waiting; a persistent refusal is
            # reported instead of being mistaken for a timeout.
            last_error = exc
            consecutive_errors += 1
            if not _journal_present(run_dir):
                return _fail("no-task-journal", f"no task journal in run {run_dir.name}")
            if consecutive_errors >= MAX_CONSECUTIVE_READ_ERRORS:
                return _fail("task-journal-unreadable", str(exc))
        else:
            last_error = None
            consecutive_errors = 0
            by_id = {result.handle.spec.task_id: result for result in results}
            watched = selected or list(by_id)
            if not watched:
                # Nothing accepted yet is not "all terminal".
                return _fail("no-tasks", "the run has no accepted tasks to wait for")
            unknown = [task_id for task_id in watched if task_id not in by_id]
            if unknown:
                return _unknown(unknown[0], by_id)
            pending = _pending_cancel_reasons(run_dir)
            latest = [_row(by_id[task_id], pending) for task_id in watched]
            if all(row.terminal for row in latest):
                if json_output:
                    _emit(_wait_payload(run_dir.name, latest, timed_out=False, timeout=timeout, waiting_for=[]))
                else:
                    _print_table(latest)
                return 0
        now = monotonic()
        if deadline is not None and now >= deadline:
            if latest is None:
                return _fail(
                    "task-journal-unreadable",
                    str(last_error) if last_error is not None else "task journal did not become readable",
                )
            waiting_for = sorted(row.task_id for row in latest if not row.terminal)
            if json_output:
                _emit(
                    _wait_payload(
                        run_dir.name,
                        latest,
                        timed_out=True,
                        timeout=timeout,
                        waiting_for=waiting_for,
                    )
                )
            else:
                _fail("wait-timeout", f"non-terminal tasks: {', '.join(waiting_for)}")
            return 3
        delay = interval if deadline is None else min(interval, max(0.0, deadline - now))
        sleep(delay)


def next_task(
    run: str | Path,
    *,
    cwd: Path,
    runs_dir: Path | None = None,
    timeout: float | None = None,
    json_output: bool = False,
    interval: float = POLL_INTERVAL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    """Pull the oldest unacknowledged terminal result without mutating the run."""
    from . import run_task_inbox

    # Multiple parents can be added here by giving each consumer its own cursor.
    if timeout is not None and (not math.isfinite(timeout) or timeout < 0):
        return _fail("invalid-timeout", "timeout must not be negative")
    run_dir = _resolve(run, cwd=cwd, runs_dir=runs_dir)
    if run_dir is None:
        return 2
    if not _journal_present(run_dir):
        return _fail("no-task-journal", f"no task journal in run {run_dir.name}")
    deadline = None if timeout is None else monotonic() + timeout
    while True:
        try:
            results = tasks.read_tasks(run_dir)
        except tasks.TaskJournalError as exc:
            if timeout is None or deadline is None or monotonic() >= deadline:
                return _fail("task-journal-unreadable", str(exc))
            sleep(min(interval, max(0.0, deadline - monotonic())))
            continue
        try:
            acks = _pending_acks(run_dir)
        except run_task_inbox.TaskInboxError as exc:
            return _fail(exc.code, exc.detail)
        order = _terminal_order(run_dir)
        terminal = [r for r in results if r.status in run_task_journal.TERMINAL and r.result_id is not None]
        offered = [r for r in terminal if not r.acknowledged and (r.handle.spec.task_id, r.result_id or "") not in acks]
        offered.sort(key=lambda r: order.get(r.handle.spec.task_id, 2**63))
        task = _row(offered[0], acknowledged=acks) if offered else None
        done = len(terminal) == len(results) and not offered
        remaining = sum(
            1
            for result in terminal
            if not result.acknowledged and (result.handle.spec.task_id, result.result_id or "") not in acks
        )
        if task is not None or done:
            payload = {
                "schema": RUN_TASKS_NEXT_SCHEMA,
                "run_id": run_dir.name,
                "task": None if task is None else task.to_dict(),
                "remaining": remaining,
                "done": done,
            }
            if json_output:
                _emit(payload)
            elif task is None:
                print(f"run: {run_dir.name}\ndone: yes\nremaining: 0")
            else:
                _print_detail(task, *_result_sidecar(run_dir, task.task_id), *_artifact_sidecar(run_dir, task.task_id))
            return 0
        now = monotonic()
        if deadline is not None and now >= deadline:
            payload = {
                "schema": RUN_TASKS_NEXT_SCHEMA,
                "run_id": run_dir.name,
                "task": None,
                "remaining": remaining,
                "done": False,
                "timed_out": True,
            }
            if json_output:
                _emit(payload)
            else:
                _fail("next-timeout", "no unacknowledged terminal result became available")
            return 3
        delay = interval if deadline is None else min(interval, max(0.0, deadline - now))
        sleep(delay)


def ack_task(
    run: str | Path,
    task_id: str,
    result_id: str,
    *,
    cwd: Path,
    runs_dir: Path | None = None,
    json_output: bool = False,
) -> int:
    """Submit an acknowledgement through the inbox, then journal only as lock owner."""
    from . import run_task_inbox

    run_dir = _resolve(run, cwd=cwd, runs_dir=runs_dir)
    if run_dir is None:
        return 2
    try:
        results = tasks.read_tasks(run_dir)
    except tasks.TaskJournalError as exc:
        return _fail("journal-unreadable", str(exc))
    state = next((item for item in results if item.handle.spec.task_id == task_id), None)
    outcome = "unknown-task"
    if state is not None:
        try:
            # Resolve everything that can refuse before the request is written,
            # so a refusal never leaves a durable acknowledgement behind it.
            workspace = _workspace_for_run(run_dir)
            pending_acks = _pending_acks(run_dir)
        except run_task_inbox.TaskInboxError as exc:
            return _fail(exc.code, exc.detail)
        if state.status not in run_task_journal.TERMINAL or state.result_id is None:
            outcome = "no-result"
        elif state.result_id != result_id:
            outcome = "result-mismatch"
        elif state.acknowledged:
            outcome = "already-acknowledged"
        else:
            # A request already queued is retried below, so a later call can
            # still journal an acknowledgement a busy batch never drained.
            if (task_id, result_id) not in pending_acks:
                try:
                    run_task_inbox.submit(run_dir, run_task_inbox.KIND_ACK, task_id, result_id=result_id)
                except run_task_inbox.TaskInboxError as exc:
                    return _fail(exc.code, exc.detail)
            outcome = "queued"
            held = contextlib.ExitStack()
            try:
                held.enter_context(runguard.run_lock(workspace, run_dir=run_dir, stale_action="claim"))
            except (runguard.RunLockError, OSError):
                held.close()
            else:
                with held:
                    try:
                        run_task_inbox.drain(run_dir, workspace)
                        journaled = next(
                            (r for r in tasks.read_tasks(run_dir) if r.handle.spec.task_id == task_id), None
                        )
                    except (run_task_inbox.TaskInboxError, tasks.TaskJournalError) as exc:
                        # The request stays queued and `next` treats it as
                        # consumed; the refusal says it is not journaled yet.
                        return _fail(getattr(exc, "code", "journal-unreadable"), str(exc))
                if journaled is not None and journaled.acknowledged:
                    outcome = "acknowledged"
    payload = {
        "schema": RUN_TASKS_ACK_SCHEMA,
        "run_id": run_dir.name,
        "task_id": task_id,
        "result_id": result_id,
        "outcome": outcome,
    }
    if json_output:
        _emit(payload)
    else:
        print(f"task {task_id}: {outcome}")
    return 0 if outcome in {"acknowledged", "queued", "already-acknowledged"} else 2


def _workspace_for_run(run_dir: Path) -> Path:
    from . import run_task_inbox

    return run_task_inbox._workspace(run_dir, None)


def integrate_tasks(
    run: str | Path,
    task_ids: Sequence[str],
    *,
    cwd: Path,
    runs_dir: Path | None = None,
    check: bool = False,
    json_output: bool = False,
) -> int:
    """Integrate selected task candidates, or report one closed refusal."""
    from . import run_batch_integrate

    run_dir = _resolve(run, cwd=cwd, runs_dir=runs_dir)
    if run_dir is None:
        return 2
    try:
        payload = run_batch_integrate.integrate(run_dir, task_ids, check=check)
    except run_batch_integrate.IntegrationError as exc:
        payload = run_batch_integrate.refusal_payload(exc, run_id=run_dir.name, check=check)
        if json_output:
            _emit(payload)
        else:
            print(run_batch_integrate.render(payload), file=sys.stderr)
        return 2
    if json_output:
        _emit(payload)
    else:
        print(run_batch_integrate.render(payload))
    return 0


def dispose_tasks(
    run: str | Path,
    task_ids: Sequence[str],
    *,
    cwd: Path,
    runs_dir: Path | None = None,
    confirm: bool = False,
    discard: Sequence[str] = (),
    delete_branch: bool = False,
    json_output: bool = False,
) -> int:
    """Remove selected task worktrees, or report what a confirmed run would do."""
    from . import run_batch_dispose

    run_dir = _resolve(run, cwd=cwd, runs_dir=runs_dir)
    if run_dir is None:
        return 2
    try:
        payload = run_batch_dispose.dispose(
            run_dir, task_ids, discard=discard, delete_branch=delete_branch, confirm=confirm
        )
    except run_batch_dispose.DisposeError as exc:
        payload = run_batch_dispose.refusal_payload(exc, run_id=run_dir.name, confirm=confirm)
        if json_output:
            _emit(payload)
        else:
            print(run_batch_dispose.render(payload), file=sys.stderr)
        return 2
    if json_output:
        _emit(payload)
    else:
        print(run_batch_dispose.render(payload))
    refused = [row for row in payload["tasks"] if row["status"] == run_batch_dispose.STATUS_REFUSED]
    if refused and not json_output:
        for row in refused:
            print(
                f"task {row['task_id']}: {row['refusal']['code']} ({row['refusal']['detail']})",
                file=sys.stderr,
            )
    return 2 if refused else 0


def cancel_tasks(
    run: str | Path,
    task_ids: Sequence[str],
    *,
    cwd: Path,
    runs_dir: Path | None = None,
    reason: str = "operator",
    json_output: bool = False,
) -> int:
    """Record cancellation intent for selected tasks, or report why not."""
    from . import run_task_inbox

    run_dir = _resolve(run, cwd=cwd, runs_dir=runs_dir)
    if run_dir is None:
        return 2
    try:
        payload = run_task_inbox.cancel(run_dir, task_ids, reason=reason)
    except run_task_inbox.TaskInboxError as exc:
        payload = run_task_inbox.refusal_payload(exc, run_id=run_dir.name, reason=reason)
        if json_output:
            _emit(payload)
        else:
            print(run_task_inbox.render(payload), file=sys.stderr)
        return 2
    if json_output:
        _emit(payload)
    else:
        print(run_task_inbox.render(payload))
    refused = [row for row in payload["tasks"] if row["outcome"] in run_task_inbox.REFUSAL_OUTCOMES]
    if refused and not json_output:
        for row in refused:
            print(f"task {row['task_id']}: {row['outcome']}", file=sys.stderr)
    return 2 if refused else 0


def collect_task(
    run: str | Path,
    task_id: str,
    *,
    cwd: Path,
    runs_dir: Path | None = None,
    json_output: bool = False,
) -> int:
    """Recover one interrupted artifact collection, or report one closed refusal."""
    from . import run_batch_dispose

    run_dir = _resolve(run, cwd=cwd, runs_dir=runs_dir)
    if run_dir is None:
        return 2
    try:
        payload = run_batch_dispose.collect(run_dir, task_id)
    except run_batch_dispose.DisposeError as exc:
        if json_output:
            _emit(
                {
                    "schema": run_batch_dispose.COLLECT_SCHEMA,
                    "run_id": run_dir.name,
                    "task_id": task_id,
                    "refusal": {"code": exc.code, "detail": exc.detail},
                }
            )
        else:
            print(f"error: {exc.code}: {exc.detail}", file=sys.stderr)
        return 2
    if json_output:
        _emit(payload)
    else:
        print(run_batch_dispose.render(payload))
    return 0


def dispatch(args, *, cwd: Path, runs_dir: Path | None) -> int:
    if args.tasks_command == "list":
        return list_tasks(args.run, cwd=cwd, runs_dir=runs_dir, json_output=args.json)
    if args.tasks_command == "show":
        return show_task(args.run, args.task_id, cwd=cwd, runs_dir=runs_dir, json_output=args.json)
    if args.tasks_command == "wait":
        return wait_tasks(
            args.run,
            args.task_ids,
            cwd=cwd,
            runs_dir=runs_dir,
            timeout=args.timeout,
            json_output=args.json,
        )
    if args.tasks_command == "next":
        return next_task(args.run, cwd=cwd, runs_dir=runs_dir, timeout=args.timeout, json_output=args.json)
    if args.tasks_command == "ack":
        return ack_task(args.run, args.task_id, args.result_id, cwd=cwd, runs_dir=runs_dir, json_output=args.json)
    if args.tasks_command == "integrate":
        return integrate_tasks(
            args.run,
            args.task_ids,
            cwd=cwd,
            runs_dir=runs_dir,
            check=args.check,
            json_output=args.json,
        )
    if args.tasks_command == "dispose":
        return dispose_tasks(
            args.run,
            args.task_ids,
            cwd=cwd,
            runs_dir=runs_dir,
            confirm=args.confirm,
            discard=args.discard,
            delete_branch=args.delete_branch,
            json_output=args.json,
        )
    if args.tasks_command == "collect":
        return collect_task(args.run, args.task_id, cwd=cwd, runs_dir=runs_dir, json_output=args.json)
    if args.tasks_command == "cancel":
        return cancel_tasks(
            args.run,
            args.task_ids,
            cwd=cwd,
            runs_dir=runs_dir,
            reason=args.reason,
            json_output=args.json,
        )
    print(f"error: unknown runs tasks command: {args.tasks_command}", file=sys.stderr)
    return 2
