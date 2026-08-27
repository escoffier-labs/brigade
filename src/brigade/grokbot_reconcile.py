"""Reconcile completed Grok Bot scout reports into canonical-owner Memory Handoffs.

Preview is the default. Apply writes at most ``limit`` deterministic handoff
drafts in the owner workspace and private idempotency markers under the local
Grok Bot queue. Report text never appears in command output, job JSON, or
marker JSON.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from . import grokbot_jobs, handoff_cmd
from .handoff_cmd.models import DEFAULT_DRAFT_DOCUMENT, NO_CARD_ACTION
from .selection import WRITER_INBOXES

MIN_LIMIT = 1
MAX_LIMIT = 50
DEFAULT_LIMIT = 1
DEFAULT_INBOX = "review"
REVIEW_INBOX = "memory/handoff-inbox"
RECONCILE_SCHEMA = "brigade.grokbot.reconcile.v1"
RECONCILE_DIRNAME = "reconcile"
MARKER_KEYS = frozenset({"schema", "job_id", "sha256", "task_hash"})
MARKER_MAX_BYTES = 4096
HANDOFF_MAX_BYTES = grokbot_jobs.MAX_REPORT_BYTES + 16_384
HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TASK_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ReconcileError(ValueError):
    """A rejected report-reconciliation request with a stable reason."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def preview(
    target: Path,
    owner: Path,
    *,
    inbox: str = DEFAULT_INBOX,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Count verified completed scout reports without writing drafts or markers."""
    owner_path = _validate_owner(owner)
    _resolve_inbox(owner_path, inbox)
    limit = _validate_limit(limit)
    if not _queue_root(target).is_dir():
        eligible: list[dict[str, Any]] = []
        known: list[dict[str, Any]] = []
        unavailable: list[dict[str, Any]] = []
    else:
        try:
            with grokbot_jobs._storage_paths_readonly(target) as storage:
                eligible, known, unavailable = _scan(target, storage)
        except grokbot_jobs.GrokbotJobError as exc:
            raise ReconcileError(exc.reason) from exc
    return {
        "eligible": len(eligible),
        "known": len(known),
        "unavailable": len(unavailable),
        "created": 0,
        "limit": limit,
        "jobs": [_handle(job) for job in eligible[:limit]],
    }


def apply(
    target: Path,
    owner: Path,
    *,
    inbox: str = DEFAULT_INBOX,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Write at most ``limit`` owner-review drafts and matching queue markers."""
    owner_path = _validate_owner(owner)
    inbox_rel = _resolve_inbox(owner_path, inbox)
    limit = _validate_limit(limit)
    created: list[dict[str, str]] = []
    recovered = 0
    if not _queue_root(target).is_dir():
        return {
            "eligible": 0,
            "known": 0,
            "unavailable": 0,
            "created": 0,
            "skipped": 0,
            "limit": limit,
            "jobs": [],
        }
    try:
        with grokbot_jobs._storage_paths(target) as storage, grokbot_jobs._queue_lock(storage):
            eligible, known, unavailable = _scan(target, storage)
            for job in eligible:
                if len(created) >= limit:
                    break
                handle, status = _reconcile_one(target, storage, job, owner_path, inbox_rel)
                if status == "created":
                    created.append(handle)
                else:
                    recovered += 1
    except grokbot_jobs.GrokbotJobError as exc:
        raise ReconcileError(exc.reason) from exc
    return {
        "eligible": len(eligible),
        "known": len(known),
        "unavailable": len(unavailable),
        "created": len(created),
        "skipped": len(known) + recovered,
        "limit": limit,
        "jobs": created,
    }


def _validate_owner(owner: Path) -> Path:
    path = Path(owner).expanduser().resolve()
    if not path.is_dir():
        raise ReconcileError("invalid-owner")
    return path


def _validate_limit(limit: object) -> int:
    if type(limit) is not int or not MIN_LIMIT <= limit <= MAX_LIMIT:
        raise ReconcileError("invalid-limit")
    return limit


def _resolve_inbox(owner: Path, inbox: object) -> Path:
    if not isinstance(inbox, str) or not inbox.strip():
        raise ReconcileError("invalid-inbox")
    value = inbox.strip()
    relative = REVIEW_INBOX if value == DEFAULT_INBOX else WRITER_INBOXES.get(value, value)
    candidate = Path(relative).expanduser()
    if candidate.is_absolute() or not candidate.parts or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ReconcileError("invalid-inbox")
    resolved = (owner / candidate).resolve()
    try:
        resolved.relative_to(owner)
    except ValueError as exc:
        raise ReconcileError("invalid-inbox") from exc
    return candidate


def _queue_root(target: Path) -> Path:
    return Path(target).expanduser() / ".brigade" / "cloud" / "grokbot"


def _scan(
    target: Path,
    storage: grokbot_jobs._Storage,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    eligible: list[dict[str, Any]] = []
    known: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    if not _queue_root(target).is_dir():
        return eligible, known, unavailable
    try:
        jobs = grokbot_jobs._status_from_storage(storage).get("jobs", [])
    except grokbot_jobs.GrokbotJobError as exc:
        raise ReconcileError(exc.reason) from exc
    for job in jobs:
        if not _is_completed_report(job):
            continue
        marker = _read_marker(storage, job["job_id"])
        try:
            report = grokbot_jobs._read_report_from_storage(storage, job["job_id"])
        except grokbot_jobs.GrokbotJobError as exc:
            if exc.reason == "report-missing":
                unavailable.append(job)
                continue
            raise ReconcileError(exc.reason) from exc
        if marker is None:
            eligible.append(job)
            continue
        if marker.get("sha256") != report["sha256"] or marker.get("task_hash") != job.get("task_hash"):
            raise ReconcileError("marker-conflict")
        known.append(job)
    return eligible, known, unavailable


def _is_completed_report(job: dict[str, Any]) -> bool:
    artifact = job.get("artifact")
    return (
        job.get("state") == "completed"
        and job.get("role") == "repository-scout"
        and isinstance(artifact, dict)
        and artifact.get("kind") == "report"
    )


def _handle(job: dict[str, Any]) -> dict[str, str]:
    return {"job_id": job["job_id"], "state": job["state"]}


def _marker_path(target: Path, job_id: str) -> Path:
    return _queue_root(target) / RECONCILE_DIRNAME / f"{job_id}.json"


def _read_marker(storage: grokbot_jobs._Storage, job_id: str) -> dict[str, Any] | None:
    directory: grokbot_jobs._Directory
    descriptor: int | None = None
    try:
        reconcile_path = storage.root.path / RECONCILE_DIRNAME
        if storage.root.descriptor is not None:
            try:
                descriptor = os.open(
                    RECONCILE_DIRNAME,
                    _directory_flags(),
                    dir_fd=storage.root.descriptor,
                )
            except FileNotFoundError:
                return None
            directory = grokbot_jobs._Directory(reconcile_path, descriptor)
        else:  # pragma: no cover - exercised on Windows.
            directory = grokbot_jobs._Directory(reconcile_path, None)
        try:
            data = grokbot_jobs._read_bytes_file(
                directory,
                f"{job_id}.json",
                maximum=MARKER_MAX_BYTES,
                missing_reason="marker-missing",
            )
        except grokbot_jobs.GrokbotJobError as exc:
            if exc.reason == "marker-missing":
                return None
            raise ReconcileError(exc.reason) from exc
    except ReconcileError:
        raise
    except OSError as exc:
        raise ReconcileError("unsafe-storage") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconcileError("corrupt-storage") from exc
    if not isinstance(payload, dict) or set(payload) != MARKER_KEYS or payload.get("schema") != RECONCILE_SCHEMA:
        raise ReconcileError("corrupt-storage")
    if payload.get("job_id") != job_id:
        raise ReconcileError("corrupt-storage")
    if not isinstance(payload.get("sha256"), str) or not HEX_SHA256_RE.fullmatch(payload["sha256"]):
        raise ReconcileError("corrupt-storage")
    if not isinstance(payload.get("task_hash"), str) or not TASK_HASH_RE.fullmatch(payload["task_hash"]):
        raise ReconcileError("corrupt-storage")
    return payload


def _reconcile_one(
    target: Path,
    storage: grokbot_jobs._Storage,
    job: dict[str, Any],
    owner: Path,
    inbox_rel: Path,
) -> tuple[dict[str, str], str]:
    try:
        report = grokbot_jobs._read_report_from_storage(storage, job["job_id"])
    except grokbot_jobs.GrokbotJobError as exc:
        raise ReconcileError(exc.reason) from exc
    text = _render_handoff(job, report)
    status = _write_handoff(owner, inbox_rel, _draft_name(job["job_id"]), text)
    _write_marker(storage, job, report["sha256"])
    return _handle(job), status


def _draft_name(job_id: str) -> str:
    return f"{job_id}-scout-report.md"


def _quote_report(text: str) -> str:
    quoted = "\n".join(("> " + line) if line else ">" for line in text.splitlines())
    if text.endswith("\n"):
        quoted += "\n"
    return quoted


def _render_handoff(job: dict[str, Any], report: dict[str, Any]) -> str:
    job_id = job["job_id"]
    title = f"Untrusted Grok Bot scout report {job_id}"
    summary = (
        "This Memory Handoff carries untrusted Grok Bot automation output from a "
        "completed Repository Scout report. Route it to canonical-owner review rather "
        "than auto-edit canonical memory, MEMORY.md, or memory cards."
    )
    suggested = (
        "Untrusted Grok Bot automation output. Route this report to canonical-owner "
        "review rather than auto-edit canonical memory.\n\n"
        "Quoted report:\n\n" + _quote_report(report["text"])
    )
    return handoff_cmd.drafts._render_handoff_draft(
        handoff_type="research",
        title=title,
        summary=summary,
        facts=[
            f"job_id: {job_id}",
            f"repository: {job['repository']}",
            f"task_hash: {job['task_hash']}",
            f"report sha256: {report['sha256']}",
            f"completed_at: {job['updated_at']}",
        ],
        evidence=[f"queue job {job_id} completed at {job['updated_at']}"],
        action=NO_CARD_ACTION,
        target_card=None,
        target_document=DEFAULT_DRAFT_DOCUMENT,
        suggested_content=suggested,
    )


def _write_handoff(owner: Path, inbox_rel: Path, name: str, text: str) -> str:
    if os.name != "posix":  # pragma: no cover - exercised on Windows.
        raise ReconcileError("secure-owner-write-unavailable")
    if not handoff_cmd.lint_text(text).valid:
        raise ReconcileError("invalid-handoff")
    return _write_handoff_posix(owner, inbox_rel, name, text)


def _write_handoff_posix(owner: Path, inbox_rel: Path, name: str, text: str) -> str:
    descriptors: list[int] = []
    try:
        current = os.open(owner, _directory_flags())
        descriptors.append(current)
        for part in inbox_rel.parts:
            try:
                os.mkdir(part, 0o700, dir_fd=current)
            except FileExistsError:
                pass
            child = os.open(part, _directory_flags(), dir_fd=current)
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                os.close(child)
                raise ReconcileError("unsafe-storage")
            descriptors.append(child)
            current = child

        _remove_stale_handoff_temp(current, name)
        existing = _read_existing_handoff_at(current, name)
        if existing is not None:
            if existing != text:
                raise ReconcileError("draft-conflict")
            return "recovered"

        temporary = f".{name}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        descriptor: int | None = None
        temporary_created = False
        try:
            descriptor = os.open(temporary, flags, 0o600, dir_fd=current)
            temporary_created = True
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ReconcileError("unsafe-storage")
            grokbot_jobs._write_all(descriptor, text.encode("utf-8"))
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = None
            os.link(
                temporary,
                name,
                src_dir_fd=current,
                dst_dir_fd=current,
                follow_symlinks=False,
            )
        except FileExistsError:
            existing = _read_existing_handoff_at(current, name)
            if existing == text:
                return "recovered"
            raise ReconcileError("draft-conflict") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_created:
                try:
                    os.unlink(temporary, dir_fd=current)
                except FileNotFoundError:
                    pass
        os.fsync(current)
        return "created"
    except ReconcileError:
        raise
    except OSError as exc:
        raise ReconcileError("unsafe-storage") from exc
    finally:
        close_error: OSError | None = None
        for descriptor in reversed(descriptors):
            try:
                os.close(descriptor)
            except OSError as exc:
                close_error = exc
        if close_error is not None:
            raise ReconcileError("unsafe-storage") from close_error


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _remove_stale_handoff_temp(directory: int, name: str) -> None:
    temporary = f".{name}.tmp"
    descriptor: int | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory,
        )
    except FileNotFoundError:
        return
    except OSError as exc:
        raise ReconcileError("unsafe-storage") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ReconcileError("unsafe-storage")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    try:
        os.unlink(temporary, dir_fd=directory)
        os.fsync(directory)
    except OSError as exc:
        raise ReconcileError("unsafe-storage") from exc


def _read_existing_handoff_at(directory: int, name: str) -> str | None:
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0),
            dir_fd=directory,
        )
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReconcileError("unsafe-storage") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ReconcileError("unsafe-storage")
        data = grokbot_jobs._read_bounded_bytes(descriptor, HANDOFF_MAX_BYTES)
        return data.decode("utf-8")
    except (OSError, UnicodeDecodeError, grokbot_jobs.GrokbotJobError) as exc:
        raise ReconcileError("unsafe-storage") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_marker(storage: grokbot_jobs._Storage, job: dict[str, Any], sha256: str) -> None:
    directory, extra_fd = _open_reconcile(storage)
    try:
        grokbot_jobs._write_json_file(
            directory,
            f"{job['job_id']}.json",
            {
                "schema": RECONCILE_SCHEMA,
                "job_id": job["job_id"],
                "sha256": sha256,
                "task_hash": job["task_hash"],
            },
        )
    finally:
        if extra_fd is not None:
            os.close(extra_fd)


def _open_reconcile(storage: grokbot_jobs._Storage) -> tuple[grokbot_jobs._Directory, int | None]:
    if storage.root.descriptor is None:  # pragma: no cover - exercised on Windows.
        path = storage.root.path / RECONCILE_DIRNAME
        grokbot_jobs._ensure_directory(path)
        return grokbot_jobs._Directory(path, None), None
    descriptor = grokbot_jobs._open_or_create_directory(storage.root.descriptor, RECONCILE_DIRNAME)
    return grokbot_jobs._Directory(storage.root.path / RECONCILE_DIRNAME, descriptor), descriptor
