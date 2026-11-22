"""Durable per-run command inbox for task cancellation and acknowledgement.

A task fact may only be journaled by the active run owner; that check is what
keeps a second process from rewriting another run's history. A cancel request
can still arrive from anywhere: an operator in another terminal, a parent run,
or a budget decision. So a request is first written as a bounded file under
``<run_dir>/task-inbox/``, and only the owner journals it, either from the live
batch during dispatch or from ``brigade runs tasks cancel`` when it can
take the run lock itself.

The inbox holds no paths and no free text. One request is a closed
``brigade.task_inbox_request.v1`` document with a kind, a task id, a closed
reason token and a UTC timestamp. Request files are created exclusively and are
never overwritten, so the first request wins and a replay is a no-op. They are
deliberately never deleted: the file is the replay source, and the journaled
task fact makes draining idempotent.

"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import re
import stat
import threading
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from uuid import uuid4

from . import dirfd, localio, run_events, run_journal, runguard
from . import run_task_journal as journal
from . import run_tasks

INBOX_SCHEMA = "brigade.task_inbox_request.v1"
CANCEL_SCHEMA = "brigade.runs_tasks_cancel.v1"
INBOX_DIRNAME = "task-inbox"
KIND_CANCEL = "cancel"
KIND_ACK = "ack"
KINDS = frozenset({KIND_CANCEL, KIND_ACK})
# Bounds are small on purpose: a run's inbox is a handful of operator requests,
# never a queue. Exceeding either bound refuses instead of scanning forever.
MAX_REQUEST_BYTES = 4096
MAX_REQUESTS = 1024
# Scan headroom for in-flight temporary names, which are not requests.
_SCAN_SLACK = 64
DEFAULT_REASON = "operator"

# Closed outcomes. ``recorded`` means the journal now holds the intent,
# ``queued`` means only the inbox file exists because a live batch owns the
# journal, and the other two are refusals that write nothing.
OUTCOME_RECORDED = "recorded"
OUTCOME_QUEUED = "queued"
OUTCOME_ALREADY_TERMINAL = "already-terminal"
OUTCOME_UNKNOWN_TASK = "unknown-task"
# A request that could not be written, or was not attempted after one failed.
OUTCOME_NOT_RECORDED = "not-recorded"
REFUSAL_OUTCOMES = frozenset({OUTCOME_ALREADY_TERMINAL, OUTCOME_UNKNOWN_TASK, OUTCOME_NOT_RECORDED})
LIVE_TERMINATION_UNAVAILABLE = "live-termination-unavailable"
_WATCHER_MARKER = "batch-cancel-watcher.json"

ERROR_CODES = frozenset(
    {
        "unknown-run",
        "unknown-task",
        "duplicate-task",
        "no-task-journal",
        "journal-unreadable",
        "already-terminal",
        "invalid-kind",
        "invalid-reason",
        "ack-unsupported",
        "invalid-result",
        "owner-required",
        "inbox-refused",
        "inbox-unavailable",
        "inbox-overflow",
    }
)

_NAME = re.compile(r"(?P<kind>cancel|ack)-(?P<key>[0-9a-f]{64})(?:-(?P<result>[A-Za-z0-9._:%-]+))?\.json", re.ASCII)
_CANCEL_FIELDS = frozenset({"schema", "kind", "task_id", "reason", "requested_at"})
_ACK_FIELDS = frozenset({"schema", "kind", "task_id", "result_id", "reason", "requested_at"})


class TaskInboxError(RuntimeError):
    """One bounded inbox or journal refusal."""

    def __init__(self, code: str, detail: str):
        if code not in ERROR_CODES:
            raise ValueError(f"unknown task inbox error code {code!r}")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True)
class CancelRequest:
    """One validated inbox request, bounded and free of paths or free text."""

    kind: str
    task_id: str
    reason: str | None
    requested_at: str
    result_id: str | None = None


@dataclass(frozen=True)
class Pending:
    """Every readable request plus the names that were skipped as malformed."""

    requests: tuple[CancelRequest, ...]
    malformed: tuple[str, ...]


def request_name(kind: str, task_id: str, result_id: str | None = None) -> str:
    """The deterministic file name for one request: kind, identity hash, result."""
    if kind not in KINDS:
        raise TaskInboxError("invalid-kind", "inbox kind must be cancel or ack")
    if not journal.task_id(task_id):
        raise TaskInboxError("unknown-task", "a task id is not a valid task identity")
    if kind == KIND_ACK and (result_id is None or not journal.digest(result_id)):
        raise TaskInboxError("invalid-result", "acknowledgement requires a valid result id")
    key = hashlib.sha256(task_id.encode("utf-8")).hexdigest()
    suffix = "" if result_id is None else f"-{result_id}"
    return f"{kind}-{key}{suffix}.json"


def _inbox_path(run_dir: Path) -> Path:
    return run_dir / INBOX_DIRNAME


def _open_inbox(run_dir: Path, *, create: bool) -> int | None:
    """Open the inbox directory without following a symlink, or ``None`` if absent."""
    path = _inbox_path(run_dir)
    try:
        descriptor = dirfd.open_directory_chain(path, create=create, writable=True)
    except FileNotFoundError:
        if not create:
            return None
        raise TaskInboxError("inbox-unavailable", "the task inbox could not be created") from None
    except OSError as exc:
        # ELOOP/ENOTDIR are the no-follow rejection of a symlinked component.
        code = "inbox-refused" if exc.errno in {errno.ELOOP, errno.ENOTDIR} else "inbox-unavailable"
        raise TaskInboxError(code, "the task inbox is not a contained directory") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISDIR(info.st_mode):
            raise TaskInboxError("inbox-refused", "the task inbox is not a directory")
        # A 0700 inbox is what submit creates. Anything looser is a shared
        # directory this run did not make, so refuse instead of writing into it.
        if dirfd.posix_available() and stat.S_IMODE(info.st_mode) & 0o077:
            raise TaskInboxError("inbox-refused", "the task inbox has unsafe permissions")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _names(descriptor: int) -> list[str]:
    """Directory entries from the held descriptor, bounded before any is read.

    Temporary and hidden names count toward the scan bound but are not
    requests. A scan that reaches the bound refuses instead of reading an
    unbounded directory.
    """
    limit = MAX_REQUESTS + _SCAN_SLACK
    try:
        names = dirfd.child_names(descriptor, limit + 1)
    except OSError as exc:
        raise TaskInboxError("inbox-unavailable", "the task inbox could not be read") from exc
    if len(names) > limit:
        raise TaskInboxError("inbox-overflow", "the task inbox exceeds the request bound")
    return sorted(name for name in names if not name.startswith("."))


def _fsync_directory(descriptor: int) -> None:
    try:
        os.fsync(descriptor)
    except OSError:
        # Directory fsync is unavailable on some platforms; the file fsync and
        # the exclusive create still bound the request.
        pass


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise OSError("short inbox write")
        view = view[written:]


def _publish(descriptor: int, name: str, data: bytes) -> bool:
    """Publish a complete request atomically; never overwrite an existing one."""
    temporary = f".tmp-{os.getpid()}-{uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    if dirfd.posix_available():
        flags = dirfd.file_flags(flags)
    created = dirfd.open_child_file(descriptor, temporary, flags, 0o600)
    try:
        _write_all(created, data)
        os.fsync(created)
    except BaseException:
        os.close(created)
        _unlink(descriptor, temporary)
        raise
    os.close(created)
    try:
        try:
            dirfd.link_child(descriptor, temporary, name)
        except FileExistsError:
            return False
        except OSError as exc:
            if exc.errno == errno.EEXIST:
                return False
            if dirfd.posix_available():
                raise
            # Hard links are unavailable on some Windows filesystems. Publish
            # the complete, synced temporary file with a no-replace rename, so
            # a crash never leaves a partial request under the final name.
            try:
                dirfd.replace_children(descriptor, temporary, name, replace=False)
            except FileExistsError:
                return False
            except OSError as rename_exc:
                if rename_exc.errno == errno.EEXIST:
                    return False
                raise
            _fsync_directory(descriptor)
            return True
        else:
            return True
    finally:
        _unlink(descriptor, temporary)


def _unlink(descriptor: int, name: str) -> None:
    try:
        dirfd.unlink_child(descriptor, name)
    except OSError:
        pass


def _document(kind: str, task_id: str, reason: str, requested_at: str) -> dict[str, Any]:
    return {
        "schema": INBOX_SCHEMA,
        "kind": kind,
        "task_id": task_id,
        "reason": reason,
        "requested_at": requested_at,
    }


def _ack_document(task_id: str, result_id: str, requested_at: str) -> dict[str, Any]:
    return {
        "schema": INBOX_SCHEMA,
        "kind": KIND_ACK,
        "task_id": task_id,
        "result_id": result_id,
        "reason": None,
        "requested_at": requested_at,
    }


def submit(
    run_dir: Path,
    kind: str,
    task_id: str,
    *,
    reason: str | None = None,
    result_id: str | None = None,
) -> str:
    """Write one durable request, or report that an identical one exists.

    ``created`` and ``already-requested`` are both success. A different reason
    for the same task keeps the request that arrived first.
    """
    name = request_name(kind, task_id, result_id)
    if kind == KIND_ACK:
        selected = None
    else:
        selected = DEFAULT_REASON if reason is None else reason
        if selected not in journal.CANCEL_REASONS:
            raise TaskInboxError("invalid-reason", "cancel reason must be operator, parent or budget")
    try:
        run_dir = run_journal.normalize_run_dir(Path(run_dir))
    except (OSError, ValueError, run_journal.RunJournalError) as exc:
        raise TaskInboxError("unknown-run", "run directory is not a run") from exc
    if not run_dir.is_dir():
        raise TaskInboxError("unknown-run", "run directory is not a run")
    requested_at = run_events.format_recorded_at(datetime.now(timezone.utc))
    if kind == KIND_ACK:
        assert result_id is not None
        payload = _ack_document(task_id, result_id, requested_at)
    else:
        assert selected is not None
        payload = _document(kind, task_id, selected, requested_at)
    data = run_events.canonical_bytes(payload)
    descriptor = _open_inbox(run_dir, create=True)
    assert descriptor is not None
    try:
        created = _publish(descriptor, name, data)
        _fsync_directory(descriptor)
    finally:
        os.close(descriptor)
    return "created" if created else "already-requested"


def _request(document: Any, *, kind: str, key: str, name_result_id: str | None) -> CancelRequest | None:
    """Validate one closed request document, or ``None`` when it is malformed."""
    fields = _ACK_FIELDS if kind == KIND_ACK else _CANCEL_FIELDS
    if not isinstance(document, Mapping) or set(document) != fields:
        return None
    if document["schema"] != INBOX_SCHEMA or document["kind"] != kind:
        return None
    task_id = document["task_id"]
    if not journal.task_id(task_id):
        return None
    if hashlib.sha256(task_id.encode("utf-8")).hexdigest() != key:
        return None
    reason = document["reason"]
    result_id = document.get("result_id")
    if kind == KIND_ACK:
        if reason is not None or not journal.digest(result_id) or name_result_id != result_id:
            return None
    elif reason not in journal.CANCEL_REASONS or name_result_id is not None:
        return None
    requested_at = document["requested_at"]
    if not run_events.is_valid_recorded_at(requested_at):
        return None
    return CancelRequest(kind, task_id, reason, requested_at, result_id)


def _read(descriptor: int, name: str, path: Path) -> Any:
    """Read one bounded request no-follow; a symlink or oversized file refuses."""
    try:
        info = dirfd.stat_child(descriptor, name)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TaskInboxError("inbox-unavailable", "an inbox request could not be inspected") from exc
    if not stat.S_ISREG(info.st_mode):
        raise TaskInboxError("inbox-refused", "an inbox request is not a regular file")
    if info.st_size > MAX_REQUEST_BYTES:
        raise TaskInboxError("inbox-overflow", "an inbox request exceeds the size bound")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if dirfd.posix_available():
        flags = dirfd.file_flags(flags | os.O_NONBLOCK)
    try:
        opened = dirfd.open_child_file(descriptor, name, flags, 0)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            raise TaskInboxError("inbox-refused", "an inbox request is a symlink") from exc
        raise TaskInboxError("inbox-unavailable", "an inbox request could not be read") from exc
    try:
        with os.fdopen(opened, "rb") as handle:
            # The entry may have been swapped between the name check and the
            # open, so the opened descriptor itself must be a bounded file.
            opened_info = os.fstat(handle.fileno())
            if not stat.S_ISREG(opened_info.st_mode):
                raise TaskInboxError("inbox-refused", "an inbox request is not a regular file")
            if opened_info.st_size > MAX_REQUEST_BYTES:
                raise TaskInboxError("inbox-overflow", "an inbox request exceeds the size bound")
            data = handle.read(MAX_REQUEST_BYTES + 1)
    except OSError as exc:
        raise TaskInboxError("inbox-unavailable", "an inbox request could not be read") from exc
    if len(data) > MAX_REQUEST_BYTES:
        raise TaskInboxError("inbox-overflow", "an inbox request exceeds the size bound")
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None


def pending(run_dir: Path) -> Pending:
    """Read every bounded request; malformed ones are skipped and reported."""
    try:
        run_dir = run_journal.normalize_run_dir(Path(run_dir))
    except (OSError, ValueError, run_journal.RunJournalError) as exc:
        raise TaskInboxError("unknown-run", "run directory is not a run") from exc
    path = _inbox_path(run_dir)
    descriptor = _open_inbox(run_dir, create=False)
    if descriptor is None:
        return Pending((), ())
    try:
        names = _names(descriptor)
        if len(names) > MAX_REQUESTS:
            raise TaskInboxError("inbox-overflow", "the task inbox exceeds the request bound")
        requests: list[CancelRequest] = []
        malformed: list[str] = []
        for name in names:
            matched = _NAME.fullmatch(name)
            if matched is None:
                # A stray non-request file is not a request; ignore it.
                continue
            document = _read(descriptor, name, path)
            if document is None:
                malformed.append(name)
                continue
            request = _request(
                document,
                kind=matched.group("kind"),
                key=matched.group("key"),
                name_result_id=matched.group("result"),
            )
            if request is None:
                malformed.append(name)
                continue
            requests.append(request)
        return Pending(tuple(requests), tuple(malformed))
    finally:
        os.close(descriptor)


def drain(run_dir: Path, workspace: Path) -> dict[str, str]:
    """Journal pending cancellation and acknowledgement requests under the owner.

    Returns one closed outcome per named task. Requests are never deleted: the
    journaled fact is what makes a later drain idempotent.
    """
    try:
        run_dir = run_journal.normalize_run_dir(Path(run_dir))
    except (OSError, ValueError, run_journal.RunJournalError) as exc:
        raise TaskInboxError("unknown-run", "run directory is not a run") from exc
    if not runguard.is_active_run_owner(workspace, run_dir):
        raise TaskInboxError("owner-required", "only the active run owner journals a cancel intent")
    requests = pending(run_dir).requests
    outcomes: dict[str, str] = {}
    if not requests:
        return outcomes
    try:
        with journal.transaction(run_dir, workspace=workspace, writing=True) as (run_dir, events):
            states = run_tasks._fold(run_dir.name, events)
            for request in requests:
                current = states.get(request.task_id)
                if current is None:
                    outcomes.setdefault(request.task_id, "unknown")
                    continue
                if request.kind == KIND_ACK:
                    if current.result_id != request.result_id or current.acknowledged:
                        outcomes.setdefault(
                            request.task_id, "already-acknowledged" if current.acknowledged else "stale"
                        )
                        continue
                    journal.append(
                        run_dir,
                        workspace=workspace,
                        event_type="task.acknowledged",
                        payload={"task_id": request.task_id, "result_id": request.result_id},
                    )
                    states[request.task_id] = replace(current, acknowledged=True)
                    outcomes[request.task_id] = "acknowledged"
                    continue
                if current.cancel_requested:
                    outcomes.setdefault(request.task_id, "already-requested")
                    continue
                event = journal.append(
                    run_dir,
                    workspace=workspace,
                    event_type="task.cancel-requested",
                    payload={"task_id": request.task_id, "reason": request.reason},
                )
                states[request.task_id] = replace(
                    current,
                    cancel_requested=True,
                    cancel_reason=request.reason,
                    version=event.event_digest,
                )
                outcomes[request.task_id] = "recorded"
    except journal.TaskJournalError as exc:
        raise TaskInboxError("journal-unreadable", "the task journal refused an inbox request") from exc
    return outcomes


def _workspace(run_dir: Path, workspace: Path | None) -> Path:
    if workspace is not None:
        return Path(workspace).expanduser().resolve()
    meta = localio.read_json_dict(run_dir / "run.json") or {}
    resolved = runguard.resolve_run_lock_workspace(meta, run_dir)
    if resolved is None:
        raise TaskInboxError("unknown-run", "caller workspace could not be resolved")
    return resolved


def _ordered(task_ids: Sequence[str]) -> tuple[str, ...]:
    if not task_ids:
        raise TaskInboxError("unknown-task", "name at least one task to cancel")
    seen: set[str] = set()
    for task_id in task_ids:
        if not journal.task_id(task_id):
            raise TaskInboxError("unknown-task", "a task id is not a valid task identity")
        if task_id in seen:
            raise TaskInboxError("duplicate-task", f"task {task_id} is named more than once")
        seen.add(task_id)
    return tuple(task_ids)


@contextlib.contextmanager
def live_watcher(run_dir: Path, workspace: Path, *, routed: bool = False) -> Iterator[None]:
    """Advertise live cancellation only for this workspace lock's owner."""
    # A batch also holds nested worktree locks. Name the caller's lock
    # directly, since the context's innermost lock may belong to a worktree.
    lock = runguard.lock_path(workspace)
    owner = runguard.read_lock_owner(lock)
    if owner is None or owner.get("pid") != os.getpid() or not runguard._owner_matches_run(owner, run_dir):
        yield
        return
    marker = lock / _WATCHER_MARKER
    # With a remote router, only tasks recorded as local are stoppable.
    localio.write_json(marker, {"owner_token": owner["owner_token"], "routed": routed, "local_tasks": []})
    try:
        yield
    finally:
        marker.unlink(missing_ok=True)


_MARKER_LOCK = threading.Lock()


def mark_local_task(run_dir: Path, workspace: Path, task_id: str) -> None:
    """Record that a routed batch kept this task on this machine, so it is stoppable."""
    lock = runguard.lock_path(workspace)
    owner = runguard.read_lock_owner(lock)
    if owner is None or owner.get("pid") != os.getpid() or not runguard._owner_matches_run(owner, run_dir):
        return
    marker = lock / _WATCHER_MARKER
    with _MARKER_LOCK:
        try:
            current = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(current, dict) or current.get("owner_token") != owner["owner_token"]:
            return
        local = sorted(set(current.get("local_tasks") or []) | {task_id})
        localio.write_json(
            marker, {"owner_token": owner["owner_token"], "routed": bool(current.get("routed")), "local_tasks": local}
        )


def retire_live_watcher(run_dir: Path, workspace: Path) -> None:
    """Stop advertising live cancellation once this process's watcher has ended."""
    lock = runguard.lock_path(workspace)
    owner = runguard.read_lock_owner(lock)
    if owner is None or owner.get("pid") != os.getpid() or not runguard._owner_matches_run(owner, run_dir):
        return
    try:
        (lock / _WATCHER_MARKER).unlink(missing_ok=True)
    except OSError:
        pass


def _live_watcher_stoppable(run_dir: Path, workspace: Path) -> Callable[[str], bool] | None:
    """Which tasks a live watcher can stop, or None when no live watcher exists."""
    lock = runguard.lock_path(workspace)
    owner = runguard.read_lock_owner(lock)
    if not runguard._owner_matches_run(owner, run_dir) or owner is None:
        return None
    pid, token = owner.get("pid"), owner.get("owner_token")
    if type(pid) is not int or pid <= 0 or not isinstance(token, str) or not runguard._pid_is_active(pid):
        return None
    try:
        descriptor = dirfd.open_directory_nofollow(lock, writable=False)
        try:
            marker = _read(descriptor, _WATCHER_MARKER, lock)
        finally:
            os.close(descriptor)
    except (OSError, TaskInboxError):
        return None
    if not isinstance(marker, dict) or set(marker) != {"owner_token", "routed", "local_tasks"}:
        return None
    routed, local = marker["routed"], marker["local_tasks"]
    if marker["owner_token"] != token or not isinstance(routed, bool) or not isinstance(local, list):
        return None
    if not all(isinstance(item, str) for item in local):
        return None
    if not routed:
        return lambda task_id: True
    known_local = frozenset(local)
    # A routed task that is not yet known to run here may go remote, where
    # no exact cancel exists, so it is never promised a live stop.
    return lambda task_id: task_id in known_local


def _has_live_watcher(run_dir: Path, workspace: Path) -> bool:
    return _live_watcher_stoppable(run_dir, workspace) is not None


def cancel(
    run_dir: Path,
    task_ids: Sequence[str],
    *,
    reason: str = DEFAULT_REASON,
    workspace: Path | None = None,
) -> dict[str, Any]:
    """Record cancellation intent for selected tasks, or report why not."""
    from . import run_lifecycle

    try:
        run_dir = run_journal.normalize_run_dir(Path(run_dir))
    except (OSError, ValueError, run_journal.RunJournalError) as exc:
        raise TaskInboxError("unknown-run", "run directory is not a run") from exc
    if reason not in journal.CANCEL_REASONS:
        raise TaskInboxError("invalid-reason", "cancel reason must be operator, parent or budget")
    ordered = _ordered(task_ids)
    workspace = _workspace(run_dir, workspace)
    if not run_journal.bound_is_file(run_lifecycle._journal_path(run_dir)):
        raise TaskInboxError("no-task-journal", f"no task journal in run {run_dir.name}")
    try:
        states = {result.handle.spec.task_id: result for result in run_tasks.read_tasks(run_dir)}
    except (run_tasks.TaskJournalError, run_journal.RunJournalError) as exc:
        raise TaskInboxError("journal-unreadable", f"task journal in run {run_dir.name} is not readable") from exc

    rows: dict[str, dict[str, Any]] = {}
    submitted: list[run_tasks.TaskResult] = []
    for task_id in ordered:
        state = states.get(task_id)
        row = {"task_id": task_id, "outcome": OUTCOME_UNKNOWN_TASK, "reason": reason, "live_termination": None}
        if state is None:
            row["outcome"] = OUTCOME_UNKNOWN_TASK
        elif state.status in journal.TERMINAL:
            row["outcome"] = OUTCOME_ALREADY_TERMINAL
        else:
            submitted.append(state)
            row["outcome"] = OUTCOME_QUEUED
        rows[task_id] = row

    # The request is durable before any attempt to take the run lock, so a
    # live batch can read it during dispatch even when this process never
    # becomes the owner.
    # A failure part way through still reports the requests already written:
    # they are durable and will be honored, so they must not vanish from the
    # answer behind a bare refusal.
    failure: TaskInboxError | None = None
    committed: list[run_tasks.TaskResult] = []
    stoppable = _live_watcher_stoppable(run_dir, workspace) if submitted else None
    for state in submitted:
        if failure is None:
            try:
                submit(run_dir, KIND_CANCEL, state.handle.spec.task_id, reason=reason)
            except TaskInboxError as exc:
                failure = exc
            else:
                committed.append(state)
                continue
        rows[state.handle.spec.task_id]["outcome"] = OUTCOME_NOT_RECORDED
    submitted = committed

    drained: dict[str, str] | None = None
    if submitted:
        try:
            # Non-blocking by default: a live batch keeps its lock, and this
            # command reports queued instead of waiting for it.
            held = contextlib.ExitStack()
            held.enter_context(runguard.run_lock(workspace, run_dir=run_dir, stale_action="claim"))
        except (runguard.RunLockError, OSError):
            # Only a lock this process could not take means "queued". A drain
            # failure under the lock is a refusal, never a queued success.
            held = None
        if held is not None:
            with held:
                drained = drain(run_dir, workspace)
                # A batch may have claimed a task and exited between the read
                # above and this lock, so judge live termination on the state
                # the drain saw, never the stale pre-lock read.
                try:
                    states = {result.handle.spec.task_id: result for result in run_tasks.read_tasks(run_dir)}
                except (run_tasks.TaskJournalError, run_journal.RunJournalError) as exc:
                    raise TaskInboxError(
                        "journal-unreadable", f"task journal in run {run_dir.name} is not readable"
                    ) from exc

    if drained is None and submitted and stoppable is None:
        stoppable = _live_watcher_stoppable(run_dir, workspace)
    for state in submitted:
        row = rows[state.handle.spec.task_id]
        if drained is None:
            # A live batch may claim the task after the state above was read,
            # so a request this process could not journal is reported as
            # possibly running, never as safely stopped before launch.
            row["outcome"] = OUTCOME_QUEUED
            live = stoppable is not None and stoppable(state.handle.spec.task_id)
            row["live_termination"] = "requested" if live else LIVE_TERMINATION_UNAVAILABLE
            continue
        outcome = drained.get(state.handle.spec.task_id)
        if outcome in {"recorded", "already-requested"}:
            row["outcome"] = OUTCOME_RECORDED
            current = states.get(state.handle.spec.task_id, state)
            if current.launch_claim is not None:
                row["live_termination"] = LIVE_TERMINATION_UNAVAILABLE
        else:
            row["outcome"] = OUTCOME_UNKNOWN_TASK
    return {
        "schema": CANCEL_SCHEMA,
        "run_id": run_dir.name,
        "reason": reason,
        "tasks": [rows[task_id] for task_id in ordered],
        "refusal": None if failure is None else {"code": failure.code, "detail": failure.detail},
    }


def refusal_payload(
    error: TaskInboxError, *, run_id: str | None = None, reason: str = DEFAULT_REASON
) -> dict[str, Any]:
    """The closed refusal projection for ``--json``."""
    return {
        "schema": CANCEL_SCHEMA,
        "run_id": run_id,
        "reason": reason,
        "tasks": [],
        "refusal": {"code": error.code, "detail": error.detail},
    }


def render(payload: dict[str, Any]) -> str:
    """Human output: what was recorded, or what could not be."""
    refusal = payload.get("refusal")
    if refusal is not None and not payload["tasks"]:
        return f"error: {refusal['code']}: {refusal['detail']}"
    lines = [f"run: {payload['run_id']}"]
    if refusal is not None:
        lines.append(f"error: {refusal['code']}: {refusal['detail']}")
    live = []
    for row in payload["tasks"]:
        detail = f"task {row['task_id']}: {row['outcome']}"
        if row["outcome"] in {OUTCOME_RECORDED, OUTCOME_QUEUED}:
            detail += f" ({row['reason']})"
        if row.get("live_termination") == "requested":
            detail += " (live cancellation requested)"
        lines.append(detail)
        if row.get("live_termination") == LIVE_TERMINATION_UNAVAILABLE:
            live.append(row["task_id"])
    for task_id in live:
        lines.append(f"task {task_id}: {LIVE_TERMINATION_UNAVAILABLE} (no live batch watcher)")
    return "\n".join(lines)
