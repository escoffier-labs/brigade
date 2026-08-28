"""Operator-only redaction for authoritative lifecycle journals.

Normal lifecycle writes remain append-only. This module is the exceptional
incident procedure for removing exposed payload bytes from a closed run:

1. verify the authoritative journal, projection, and absent run lock;
2. durably quarantine the original journal under a deterministic transaction;
3. replace affected payloads and idempotency keys, then re-chain the journal;
4. atomically replace the active journal and compatibility projection;
5. verify both again before writing a bounded redaction record.

The quarantine is retained by default. Deleting it requires a separate,
explicit cleanup call which repeats chain and projection verification first.
Transaction state makes retries safe across crashes before or after journal
replacement. Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from brigade import localio, run_checkpoint, run_events, run_journal, run_projector, run_shadow, runguard

REDACTION_SCHEMA = "brigade.run_redaction.v1"
REDACTION_SCHEMA_VERSION = 1
MAX_RUN_JSON_BYTES = 1024 * 1024
REDACTED_VALUE = "[REDACTED]"
REASON_CODES = frozenset(
    {
        "credential-exposure",
        "personal-data-exposure",
        "policy-removal",
        "other-sensitive-data",
    }
)
TERMINAL_STATUSES = frozenset({"ok", "dry-run", "failed", "timeout", "incomplete", "canceled", "orphaned"})

_FILE_MODE = 0o600
_DIR_MODE = 0o700
_OPERATION_RE = re.compile(r"^redact-[0-9a-f]{16}$")
_QUARANTINE_TEMP_RE = re.compile(r"^\.original\.jsonl\.[0-9a-f]{32}\.tmp$")
_STATE_TEMP_RE = re.compile(r"^\.state\.json\.[0-9a-f]{32}\.tmp$")
_RECORD_TEMP_RE = re.compile(r"^\.record\.json\.[0-9a-f]{32}\.tmp$")
_PROCESS_LOCK = threading.Lock()
_PROJECTION_DIGEST_FIELD = "journal_last_event_digest"
_PLATFORM_NAME = os.name
_UNSUPPORTED_PLATFORM = "unsupported platform for safe redaction transactions"
_REQUIRED_DIR_FD_OPERATIONS = (os.open, os.mkdir, os.rename, os.unlink, os.link)
_LINK_OPERATION = os.link
_LISTDIR_OPERATION = os.listdir


class RedactionError(RuntimeError):
    """A bounded redaction failure which never carries journal payload values."""

    def __init__(self, diagnostic: str) -> None:
        bounded = _bound(diagnostic)
        super().__init__(bounded)
        self.diagnostic = bounded


@dataclass(frozen=True)
class RedactionReport:
    """Paths and state for one redaction transaction."""

    operation_id: str
    sequence_start: int
    sequence_end: int
    quarantine_path: Path
    record_path: Path
    cleaned: bool = False


def _require_secure_transaction_platform() -> None:
    if (
        _PLATFORM_NAME != "posix"
        or any(operation not in os.supports_dir_fd for operation in _REQUIRED_DIR_FD_OPERATIONS)
        or _LISTDIR_OPERATION not in os.supports_fd
        or _LINK_OPERATION not in os.supports_follow_symlinks
        or run_journal._O_NOFOLLOW == 0
        or run_journal._O_DIRECTORY == 0
        or not run_journal._HAS_FCHMOD
        or not callable(getattr(os, "fsync", None))
    ):
        raise RedactionError(_UNSUPPORTED_PLATFORM)


def _probe_secure_transaction_directory(run_dir: Path) -> None:
    path = run_dir / "events"
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise RedactionError("events path validation failed") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RedactionError("events path is not a safe directory")
    if getattr(info, "st_file_attributes", 0) & 0x400:
        raise RedactionError("events path is a reparse point")
    try:
        if path.resolve(strict=True) != path.absolute():
            raise RedactionError("events path traverses a link")
    except (OSError, RuntimeError) as exc:
        raise RedactionError("events path validation failed") from exc
    try:
        fd = run_journal._open_nofollow(
            path,
            os.O_RDONLY | run_journal._O_DIRECTORY,
        )
    except (OSError, run_journal.RunJournalError) as exc:
        raise RedactionError(_UNSUPPORTED_PLATFORM) from exc

    primary: BaseException | None = None
    try:
        try:
            opened = os.fstat(fd)
            if not stat.S_ISDIR(opened.st_mode) or opened.st_dev != info.st_dev or opened.st_ino != info.st_ino:
                raise RedactionError("events directory identity changed")
            os.fsync(fd)
        except (OSError, run_journal.RunJournalError) as exc:
            raise RedactionError(_UNSUPPORTED_PLATFORM) from exc
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            os.close(fd)
        except BaseException as exc:
            if primary is None:
                raise RedactionError(_UNSUPPORTED_PLATFORM) from exc


def _bound(message: str) -> str:
    if len(message) <= run_events.MAX_DIAGNOSTIC_LEN:
        return message
    return message[: run_events.MAX_DIAGNOSTIC_LEN - 1] + "…"


def _operation_id(run_id: str, sequence_start: int, sequence_end: int, reason: str) -> str:
    request = {
        "run_id": run_id,
        "sequence_start": sequence_start,
        "sequence_end": sequence_end,
        "reason_code": reason,
    }
    encoded = json.dumps(request, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return f"redact-{hashlib.sha256(encoded).hexdigest()[:16]}"


def _operation_paths(run_dir: Path, operation_id: str) -> tuple[Path, Path, Path]:
    operation_dir = run_dir / "events" / "redactions" / operation_id
    return operation_dir, operation_dir / "original.jsonl", operation_dir / "record.json"


def _validate_request(
    sequence_start: object,
    sequence_end: object,
    reason: object,
    *,
    operator_confirmed: bool,
) -> tuple[int, int, str]:
    if operator_confirmed is not True:
        raise RedactionError("operator confirmation is required")
    if (
        isinstance(sequence_start, bool)
        or not isinstance(sequence_start, int)
        or isinstance(sequence_end, bool)
        or not isinstance(sequence_end, int)
        or sequence_start < 1
        or sequence_end < sequence_start
    ):
        raise RedactionError("invalid sequence range")
    if not isinstance(reason, str) or reason not in REASON_CODES:
        raise RedactionError(f"reason code must be one of: {', '.join(sorted(REASON_CODES))}")
    return sequence_start, sequence_end, reason


def _resolve_run_dir(run_dir: Path) -> Path:
    path = Path(run_dir).expanduser()
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise RedactionError("run directory must not be a symlink")
        resolved = path.resolve(strict=True)
    except RedactionError:
        raise
    except (OSError, RuntimeError) as exc:
        raise RedactionError("run directory is not resolvable") from exc
    if not resolved.is_dir():
        raise RedactionError("run directory is not a directory")
    return resolved


def _read_bounded_regular(path: Path, *, limit: int, category: str) -> bytes:
    try:
        fd = run_journal._open_nofollow(path, os.O_RDONLY)
    except (OSError, run_journal.RunJournalError) as exc:
        raise RedactionError(f"{category} read failed") from exc
    try:
        try:
            info = os.fstat(fd)
        except OSError as exc:
            raise RedactionError(f"{category} stat failed") from exc
        if not stat.S_ISREG(info.st_mode):
            raise RedactionError(f"{category} is not a regular file")
        if info.st_nlink != 1:
            raise RedactionError(f"{category} link count is not one")
        if stat.S_IMODE(info.st_mode) != _FILE_MODE:
            try:
                run_journal._chmod_fd_or_path(fd, path, _FILE_MODE)
            except (OSError, run_journal.RunJournalError) as exc:
                raise RedactionError(f"{category} mode normalization failed") from exc
        if info.st_size < 0 or info.st_size > limit:
            raise RedactionError(f"{category} exceeds size bound")
        data = bytearray()
        while len(data) < info.st_size:
            try:
                chunk = os.read(fd, min(run_journal._READ_CHUNK, info.st_size - len(data)))
            except OSError as exc:
                raise RedactionError(f"{category} read failed") from exc
            if not chunk:
                break
            data.extend(chunk)
        if len(data) != info.st_size:
            raise RedactionError(f"{category} changed during read")
        try:
            extra = os.read(fd, 1)
        except OSError as exc:
            raise RedactionError(f"{category} read failed") from exc
        if extra:
            raise RedactionError(f"{category} changed during read")
        return bytes(data)
    finally:
        os.close(fd)


def _parse_json_object(raw: bytes, *, category: str) -> dict[str, Any]:
    def no_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicate_keys)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise RedactionError(f"{category} is malformed") from exc
    if not isinstance(payload, dict):
        raise RedactionError(f"{category} is not an object")
    return payload


def _load_json_object(path: Path, *, limit: int, category: str) -> dict[str, Any]:
    return _parse_json_object(
        _read_bounded_regular(path, limit=limit, category=category),
        category=category,
    )


def _snapshot_gate(run_dir: Path) -> tuple[dict[str, Any], Path]:
    snapshot = _load_json_object(
        run_dir / "run.json",
        limit=MAX_RUN_JSON_BYTES,
        category="run projection",
    )
    if snapshot.get("run_journal_authority_requested") is not True:
        raise RedactionError("run is not an authoritative journal run")
    if snapshot.get("status") not in TERMINAL_STATUSES:
        raise RedactionError("redaction requires a closed terminal run")
    try:
        workspace = runguard.resolve_run_lock_workspace(snapshot, run_dir)
    except (OSError, RuntimeError, runguard.RunGuardError) as exc:
        raise RedactionError("run lock workspace is ambiguous") from exc
    if workspace is None:
        raise RedactionError("run lock workspace is ambiguous")
    return snapshot, workspace


def _run_snapshot_and_lock_gate(run_dir: Path) -> tuple[dict[str, Any], Path]:
    snapshot, workspace = _snapshot_gate(run_dir)
    try:
        lock_path = runguard.lock_path(workspace)
    except (OSError, RuntimeError, runguard.RunGuardError) as exc:
        raise RedactionError("run lock workspace is ambiguous") from exc
    state = runguard.run_lock_state(workspace, run_dir)
    if state == "absent" and os.path.lexists(lock_path):
        raise RedactionError("run lock state is ambiguous")
    if state != "absent":
        raise RedactionError(f"run lock state is {state}; redaction requires an absent lock")
    return snapshot, workspace


@contextmanager
def _exclusive_redaction_lock(run_dir: Path):
    _, workspace = _run_snapshot_and_lock_gate(run_dir)
    try:
        with runguard.run_lock(workspace, run_dir=run_dir):
            if not runguard.is_active_run_owner(workspace, run_dir):
                raise RedactionError("redaction could not prove exclusive run lock ownership")
            snapshot, locked_workspace = _snapshot_gate(run_dir)
            if locked_workspace != workspace:
                raise RedactionError("run lock workspace changed during redaction")
            yield snapshot, workspace
    except RedactionError:
        raise
    except runguard.RunGuardError as exc:
        raise RedactionError("redaction could not acquire exclusive run lock") from exc


def _assert_active_owner(workspace: Path, run_dir: Path) -> None:
    if not runguard.is_active_run_owner(workspace, run_dir):
        raise RedactionError("redaction lost exclusive run lock ownership")


def _verified_events(journal_path: Path, *, category: str) -> list[run_journal.RunEvent]:
    try:
        report = run_journal.read_journal_bounded(journal_path)
    except (OSError, run_journal.RunJournalError) as exc:
        raise RedactionError(f"{category} verification failed") from exc
    if report.partial_tail is not None or report.chain_errors or not report.events:
        raise RedactionError(f"{category} verification failed")
    return report.events


def _projection(snapshot: Mapping[str, Any], events: Sequence[run_journal.RunEvent]) -> run_projector.RunProjection:
    try:
        return run_projector.project_run_snapshot(snapshot, events, journal_present=True)
    except run_projector.ProjectionError as exc:
        raise RedactionError("projection verification failed") from exc


def _projection_semantics(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in snapshot.items() if key != _PROJECTION_DIGEST_FIELD}


def _verify_current_projection(
    snapshot: Mapping[str, Any],
    events: Sequence[run_journal.RunEvent],
) -> run_projector.RunProjection:
    projected = _projection(snapshot, events)
    if dict(snapshot) != projected.snapshot:
        raise RedactionError("run projection is stale or inconsistent")
    return projected


def _validate_checkpoint_artifacts(
    run_dir: Path,
    snapshot: Mapping[str, Any],
    events: Sequence[run_journal.RunEvent],
) -> None:
    checkpoint_events = [event for event in events if event.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE]
    latest = run_checkpoint.latest_checkpoint_event(list(events))
    if latest is None or not checkpoint_events:
        raise RedactionError("checkpoint verification failed")
    latest_bytes: bytes | None = None
    try:
        for event in checkpoint_events:
            checkpoint_bytes = run_checkpoint.validate_checkpoint(run_dir, event)
            if event.sequence == latest.sequence:
                latest_bytes = checkpoint_bytes
        if latest_bytes is None:
            raise RedactionError("checkpoint verification failed")
        checkpoint_obj = run_checkpoint._parse_checkpoint_object(latest_bytes)
        run_checkpoint._verify_coverage(list(events), latest, checkpoint_obj)
    except RedactionError:
        raise
    except run_checkpoint.CheckpointError as exc:
        raise RedactionError("checkpoint verification failed") from exc
    if (
        latest.payload.get("body_kind") != "base-stripped"
        or checkpoint_obj.get("run_journal_authority_requested") is not True
    ):
        raise RedactionError("checkpoint authority verification failed")
    projected = _projection(checkpoint_obj, events)
    if dict(snapshot) != projected.snapshot:
        raise RedactionError("checkpoint authority projection mismatch")


def _redacted_payload(event: run_journal.RunEvent) -> dict[str, Any]:
    # Checkpoint payload values are a closed structural reference verified by
    # run_checkpoint. Replacing them would make recovery unverifiable. They
    # cannot contain arbitrary lifecycle detail under the checkpoint schema.
    if event.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE:
        try:
            run_checkpoint._validate_payload(event.payload)
        except run_checkpoint.CheckpointError as exc:
            raise RedactionError("checkpoint payload validation failed") from exc
        return dict(event.payload)

    redacted: dict[str, Any] = {}
    for key, value in event.payload.items():
        # Status is a projector input with a closed allowlist. Retaining the
        # allowed status token preserves the observable run state.
        if key == "status":
            redacted[key] = value
        elif value is None:
            redacted[key] = None
        elif isinstance(value, str):
            redacted[key] = REDACTED_VALUE
        elif isinstance(value, int) and not isinstance(value, bool):
            redacted[key] = 0
        else:
            raise RedactionError("affected payload contains an unsupported value")
    return redacted


def _redaction_idempotency_key(operation_id: str, sequence: int) -> str:
    return f"redaction:{operation_id}:{sequence}"


def _rewrite_events(
    events: Sequence[run_journal.RunEvent],
    *,
    sequence_start: int,
    sequence_end: int,
    operation_id: str,
) -> tuple[list[run_journal.RunEvent], bytes]:
    rewritten: list[run_journal.RunEvent] = []
    previous_digest: str | None = None
    total = bytearray()
    idempotency_keys: set[str] = set()
    for event in events:
        affected = sequence_start <= event.sequence <= sequence_end
        preserved_anchor = event.event_type == "run.redaction.recorded"
        payload = _redacted_payload(event) if affected and not preserved_anchor else dict(event.payload)
        idempotency_key = (
            _redaction_idempotency_key(operation_id, event.sequence)
            if affected and not preserved_anchor
            else event.idempotency_key
        )
        if idempotency_key in idempotency_keys:
            raise RedactionError("rewritten journal idempotency key collision")
        idempotency_keys.add(idempotency_key)
        try:
            envelope = run_events.build_event(
                run_id=event.run_id,
                sequence=event.sequence,
                event_type=event.event_type,
                payload=payload,
                idempotency_key=idempotency_key,
                recorded_at=event.recorded_at,
                previous_digest=previous_digest,
            )
            line = run_events.canonical_bytes(envelope) + b"\n"
        except (run_events.CanonicalizationError, ValueError) as exc:
            raise RedactionError("rewritten journal canonicalization failed") from exc
        if len(line) > run_events.MAX_LINE_BYTES:
            raise RedactionError("rewritten journal line exceeds size bound")
        total.extend(line)
        if len(total) > run_checkpoint.MAX_JOURNAL_BYTES:
            raise RedactionError("rewritten journal exceeds size bound")
        try:
            rewritten_event = run_journal.RunEvent(
                schema=envelope["schema"],
                schema_version=envelope["schema_version"],
                event_id=envelope["event_id"],
                run_id=envelope["run_id"],
                sequence=envelope["sequence"],
                event_type=envelope["event_type"],
                recorded_at=envelope["recorded_at"],
                idempotency_key=envelope["idempotency_key"],
                request_digest=envelope["request_digest"],
                previous_digest=envelope["previous_digest"],
                event_digest=envelope["event_digest"],
                payload=dict(envelope["payload"]),
            )
        except (KeyError, TypeError) as exc:
            raise RedactionError("rewritten journal envelope construction failed") from exc
        rewritten.append(rewritten_event)
        previous_digest = rewritten_event.event_digest
    return rewritten, bytes(total)


def _write_all(fd: int, data: bytes, *, category: str) -> None:
    position = 0
    while position < len(data):
        try:
            written = os.write(fd, data[position:])
        except OSError as exc:
            raise RedactionError(f"{category} write failed") from exc
        if written <= 0:
            raise RedactionError(f"{category} write failed")
        position += written


def _fsync_file(fd: int, *, category: str) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        raise RedactionError(f"{category} durability failed") from exc


def _open_directory_handle(path: Path, *, category: str) -> int:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise RedactionError(f"{category} path validation failed") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RedactionError(f"{category} path is not a safe directory")
    if getattr(info, "st_file_attributes", 0) & 0x400:
        raise RedactionError(f"{category} path is a reparse point")
    try:
        if path.resolve(strict=True) != path.absolute():
            raise RedactionError(f"{category} path traverses a link")
    except (OSError, RuntimeError) as exc:
        raise RedactionError(f"{category} path validation failed") from exc
    flags = os.O_RDONLY | run_journal._O_DIRECTORY
    try:
        fd = run_journal._open_nofollow(path, flags)
    except (OSError, run_journal.RunJournalError) as exc:
        raise RedactionError(f"{category} path validation failed") from exc
    try:
        opened = os.fstat(fd)
        if not stat.S_ISDIR(opened.st_mode) or opened.st_dev != info.st_dev or opened.st_ino != info.st_ino:
            raise RedactionError(f"{category} directory identity changed")
        if stat.S_IMODE(opened.st_mode) != _DIR_MODE:
            try:
                run_journal._chmod_fd_or_path(fd, path, _DIR_MODE)
            except (OSError, run_journal.RunJournalError) as exc:
                raise RedactionError(f"{category} mode normalization failed") from exc
        return fd
    except BaseException:
        try:
            os.close(fd)
        except BaseException:
            pass
        raise


def _assert_directory_identity(path: Path, fd: int, *, category: str) -> None:
    try:
        current = os.lstat(path)
        opened = os.fstat(fd)
    except OSError as exc:
        raise RedactionError(f"{category} directory identity check failed") from exc
    if (
        stat.S_ISLNK(current.st_mode)
        or not stat.S_ISDIR(current.st_mode)
        or current.st_dev != opened.st_dev
        or current.st_ino != opened.st_ino
    ):
        raise RedactionError(f"{category} directory identity changed")


def _fsync_directory_handle(path: Path, fd: int, *, category: str) -> None:
    _assert_directory_identity(path, fd, category=category)
    try:
        os.fsync(fd)
    except OSError as exc:
        raise RedactionError(f"{category} directory durability failed") from exc


def _mkdir_private_durable(path: Path, *, category: str) -> None:
    parent = path.parent
    parent_fd = _open_directory_handle(parent, category=f"{category} parent")
    created = False
    try:
        try:
            os.mkdir(path.name, mode=_DIR_MODE, dir_fd=parent_fd)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise RedactionError(f"{category} creation failed") from exc
        _assert_directory_identity(parent, parent_fd, category=f"{category} parent")
        child_fd = _open_directory_handle(path, category=category)
        try:
            if created:
                _fsync_directory_handle(parent, parent_fd, category=f"{category} parent")
        finally:
            os.close(child_fd)
    finally:
        os.close(parent_fd)


def _prepare_operation_dirs(run_dir: Path, operation_id: str) -> Path:
    events_dir = run_dir / "events"
    events_fd = _open_directory_handle(events_dir, category="events")
    os.close(events_fd)
    redactions_dir = events_dir / "redactions"
    _mkdir_private_durable(redactions_dir, category="redactions")
    operation_dir = redactions_dir / operation_id
    _mkdir_private_durable(operation_dir, category="redaction operation")
    return operation_dir


def _existing_operation_dir(run_dir: Path, operation_id: str) -> bool:
    redactions_dir = run_dir / "events" / "redactions"
    if not os.path.lexists(redactions_dir):
        return False
    redactions_fd = _open_directory_handle(redactions_dir, category="redactions")
    os.close(redactions_fd)
    operation_dir = redactions_dir / operation_id
    if not os.path.lexists(operation_dir):
        return False
    operation_fd = _open_directory_handle(operation_dir, category="redaction operation")
    os.close(operation_fd)
    return True


def _open_relative(
    directory: Path,
    directory_fd: int,
    name: str,
    flags: int,
    mode: int = 0o666,
    *,
    category: str = "redaction operation",
) -> int:
    _assert_directory_identity(directory, directory_fd, category=category)
    open_flags = flags | run_journal._O_NOFOLLOW
    try:
        return os.open(name, open_flags, mode, dir_fd=directory_fd)
    except OSError as exc:
        if exc.errno in {getattr(os, "ELOOP", 40), getattr(os, "ENOTDIR", 20)}:
            raise RedactionError(f"{category} path refused a link") from exc
        raise


def _publish_no_replace(
    directory: Path,
    directory_fd: int,
    temporary_name: str,
    final_name: str,
) -> None:
    _assert_directory_identity(directory, directory_fd, category="redaction operation")
    os.link(
        temporary_name,
        final_name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
        follow_symlinks=False,
    )


def _unlink_relative(directory: Path, directory_fd: int, name: str) -> bool:
    _assert_directory_identity(directory, directory_fd, category="redaction operation")
    try:
        os.unlink(name, dir_fd=directory_fd)
    except FileNotFoundError:
        return False
    return True


def _remove_operation_temps(
    operation_dir: Path,
    operation_fd: int,
    *,
    patterns: Sequence[re.Pattern[str]],
    category: str,
) -> None:
    _assert_directory_identity(
        operation_dir,
        operation_fd,
        category="redaction operation",
    )
    try:
        names = os.listdir(operation_fd)
    except OSError as exc:
        raise RedactionError(f"{category} listing failed") from exc
    removed = False
    for name in names:
        if any(pattern.fullmatch(name) for pattern in patterns):
            try:
                removed = _unlink_relative(operation_dir, operation_fd, name) or removed
            except OSError as exc:
                raise RedactionError(f"{category} removal failed") from exc
    if removed:
        _fsync_directory_handle(operation_dir, operation_fd, category=category)


def _replace_relative(
    directory: Path,
    directory_fd: int,
    temporary_name: str,
    final_name: str,
    *,
    category: str,
) -> None:
    _assert_directory_identity(directory, directory_fd, category=category)
    os.rename(
        temporary_name,
        final_name,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )


def _verify_open_regular(
    fd: int,
    path: Path,
    *,
    category: str,
    expected: bytes | None = None,
    limit: int = run_checkpoint.MAX_JOURNAL_BYTES,
) -> bytes:
    try:
        info = os.fstat(fd)
    except OSError as exc:
        raise RedactionError(f"{category} stat failed") from exc
    if not stat.S_ISREG(info.st_mode):
        raise RedactionError(f"{category} is not a regular file")
    if info.st_nlink != 1:
        raise RedactionError(f"{category} link count is not one")
    if stat.S_IMODE(info.st_mode) != _FILE_MODE:
        try:
            run_journal._chmod_fd_or_path(fd, path, _FILE_MODE)
        except (OSError, run_journal.RunJournalError) as exc:
            raise RedactionError(f"{category} mode normalization failed") from exc
    size_limit = len(expected) if expected is not None else limit
    if info.st_size > size_limit or (expected is not None and info.st_size != len(expected)):
        raise RedactionError(f"{category} conflicts with transaction data")
    data = bytearray()
    while len(data) < info.st_size:
        try:
            chunk = os.read(fd, min(run_journal._READ_CHUNK, info.st_size - len(data)))
        except OSError as exc:
            raise RedactionError(f"{category} read failed") from exc
        if not chunk:
            break
        data.extend(chunk)
    try:
        extra = os.read(fd, 1)
    except OSError as exc:
        raise RedactionError(f"{category} read failed") from exc
    if len(data) != info.st_size or extra or (expected is not None and bytes(data) != expected):
        raise RedactionError(f"{category} conflicts with transaction data")
    return bytes(data)


def _read_relative_bounded_regular_from_handle(
    directory: Path,
    directory_fd: int,
    name: str,
    *,
    limit: int,
    category: str,
) -> bytes:
    fd = _open_relative(
        directory,
        directory_fd,
        name,
        os.O_RDONLY,
        category=f"{category} parent",
    )
    try:
        return _verify_open_regular(
            fd,
            directory / name,
            category=category,
            limit=limit,
        )
    finally:
        os.close(fd)


def _read_relative_bounded_regular(
    directory: Path,
    name: str,
    *,
    limit: int,
    category: str,
) -> bytes:
    directory_fd = _open_directory_handle(directory, category=f"{category} parent")
    try:
        try:
            return _read_relative_bounded_regular_from_handle(
                directory,
                directory_fd,
                name,
                limit=limit,
                category=category,
            )
        except OSError as exc:
            raise RedactionError(f"{category} read failed") from exc
    finally:
        os.close(directory_fd)


def _load_json_object_relative(
    directory: Path,
    name: str,
    *,
    limit: int,
    category: str,
) -> dict[str, Any]:
    return _parse_json_object(
        _read_relative_bounded_regular(
            directory,
            name,
            limit=limit,
            category=category,
        ),
        category=category,
    )


def _publish_quarantine(operation_dir: Path, quarantine_path: Path, data: bytes) -> None:
    directory_fd = _open_directory_handle(operation_dir, category="redaction operation")
    temporary_name = f".original.jsonl.{uuid4().hex}.tmp"
    temporary_fd: int | None = None
    primary: RedactionError | None = None
    try:
        _remove_operation_temps(
            operation_dir,
            directory_fd,
            patterns=(_QUARANTINE_TEMP_RE,),
            category="redaction quarantine cleanup",
        )
        try:
            existing_fd = _open_relative(
                operation_dir,
                directory_fd,
                quarantine_path.name,
                os.O_RDONLY,
            )
        except FileNotFoundError:
            existing_fd = None
        except OSError as exc:
            raise RedactionError("redaction quarantine open failed") from exc
        if existing_fd is not None:
            try:
                _verify_open_regular(
                    existing_fd,
                    quarantine_path,
                    category="redaction quarantine",
                    expected=data,
                )
                _fsync_file(existing_fd, category="redaction quarantine")
            finally:
                os.close(existing_fd)
            _fsync_directory_handle(
                operation_dir,
                directory_fd,
                category="redaction quarantine",
            )
            return

        try:
            temporary_fd = _open_relative(
                operation_dir,
                directory_fd,
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                _FILE_MODE,
            )
            run_journal._chmod_fd_or_path(
                temporary_fd,
                operation_dir / temporary_name,
                _FILE_MODE,
            )
            _write_all(temporary_fd, data, category="redaction quarantine")
            _fsync_file(temporary_fd, category="redaction quarantine")
            os.close(temporary_fd)
            temporary_fd = None
            _publish_no_replace(
                operation_dir,
                directory_fd,
                temporary_name,
                quarantine_path.name,
            )
            _fsync_directory_handle(
                operation_dir,
                directory_fd,
                category="redaction quarantine",
            )
        except RedactionError:
            raise
        except OSError as exc:
            raise RedactionError("redaction quarantine durability failed") from exc
    except RedactionError as exc:
        primary = exc
        raise
    finally:
        if temporary_fd is not None:
            try:
                os.close(temporary_fd)
            except OSError:
                pass
        try:
            unlinked = _unlink_relative(operation_dir, directory_fd, temporary_name)
            if unlinked:
                _fsync_directory_handle(
                    operation_dir,
                    directory_fd,
                    category="redaction quarantine cleanup",
                )
        except (OSError, RedactionError):
            if primary is None:
                raise RedactionError("redaction quarantine cleanup failed") from None
        finally:
            os.close(directory_fd)


def _atomic_write(
    path: Path,
    data: bytes,
    *,
    mode: int,
    category: str,
    parent_fd: int | None = None,
) -> None:
    parent = path.parent
    owns_parent_fd = parent_fd is None
    if parent_fd is None:
        parent_fd = _open_directory_handle(parent, category=f"{category} parent")
    temporary_name = f".{path.name}.{uuid4().hex}.tmp"
    temporary_fd: int | None = None
    primary: RedactionError | None = None
    try:
        try:
            existing_fd = _open_relative(
                parent,
                parent_fd,
                path.name,
                os.O_RDONLY,
                category=f"{category} parent",
            )
        except FileNotFoundError:
            existing_fd = None
        except OSError as exc:
            raise RedactionError(f"{category} path validation failed") from exc
        if existing_fd is not None:
            try:
                _verify_open_regular(existing_fd, path, category=category)
            finally:
                os.close(existing_fd)
        try:
            temporary_fd = _open_relative(
                parent,
                parent_fd,
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                mode,
                category=f"{category} parent",
            )
            run_journal._chmod_fd_or_path(
                temporary_fd,
                parent / temporary_name,
                mode,
            )
            _write_all(temporary_fd, data, category=category)
            _fsync_file(temporary_fd, category=category)
            os.close(temporary_fd)
            temporary_fd = None
            _replace_relative(
                parent,
                parent_fd,
                temporary_name,
                path.name,
                category=f"{category} parent",
            )
            _fsync_directory_handle(
                parent,
                parent_fd,
                category=category,
            )
        except RedactionError:
            raise
        except (OSError, run_journal.RunJournalError) as exc:
            raise RedactionError(f"{category} replace failed") from exc
    except RedactionError as exc:
        primary = exc
        raise
    finally:
        if temporary_fd is not None:
            try:
                os.close(temporary_fd)
            except OSError:
                pass
        try:
            unlinked = _unlink_relative(parent, parent_fd, temporary_name)
            if unlinked:
                _fsync_directory_handle(
                    parent,
                    parent_fd,
                    category=f"{category} cleanup",
                )
        except (OSError, RedactionError):
            if primary is None:
                raise RedactionError(f"{category} cleanup failed") from None
        finally:
            if owns_parent_fd:
                os.close(parent_fd)


def _state_bytes(
    *,
    operation_id: str,
    run_id: str,
    sequence_start: int,
    sequence_end: int,
    reason: str,
    original_sha256: str | None,
    rewritten_sha256: str | None,
    phase: str,
    parent_operation_id: str | None,
    rewritten_digest_retired_by: str | None = None,
) -> bytes:
    if (rewritten_sha256 is None) == (rewritten_digest_retired_by is None):
        raise RedactionError("redaction transaction digest is invalid")
    payload = {
        "schema": REDACTION_SCHEMA,
        "schema_version": REDACTION_SCHEMA_VERSION,
        "operation_id": operation_id,
        "run_id": run_id,
        "sequence_start": sequence_start,
        "sequence_end": sequence_end,
        "reason_code": reason,
        "parent_operation_id": parent_operation_id,
        "phase": phase,
    }
    if rewritten_sha256 is not None:
        payload["rewritten_sha256"] = rewritten_sha256
    if rewritten_digest_retired_by is not None:
        payload["rewritten_digest_retired_by"] = rewritten_digest_retired_by
    if original_sha256 is not None:
        payload["original_sha256"] = original_sha256
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _load_and_validate_state(
    state_path: Path,
    *,
    operation_id: str,
    run_id: str,
    sequence_start: int | None = None,
    sequence_end: int | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    state = _load_json_object_relative(
        state_path.parent,
        state_path.name,
        limit=16 * 1024,
        category="redaction transaction",
    )
    if (
        state.get("schema") != REDACTION_SCHEMA
        or state.get("schema_version") != REDACTION_SCHEMA_VERSION
        or state.get("operation_id") != operation_id
        or state.get("run_id") != run_id
    ):
        raise RedactionError("redaction transaction metadata mismatch")
    if sequence_start is not None and state.get("sequence_start") != sequence_start:
        raise RedactionError("redaction transaction sequence mismatch")
    if sequence_end is not None and state.get("sequence_end") != sequence_end:
        raise RedactionError("redaction transaction sequence mismatch")
    if reason is not None and state.get("reason_code") != reason:
        raise RedactionError("redaction transaction reason mismatch")
    if state.get("phase") not in {"quarantined", "replaced", "verified", "cleanup-authorized", "cleaned"}:
        raise RedactionError("redaction transaction phase is invalid")
    rewritten_digest = state.get("rewritten_sha256")
    rewritten_digest_retired_by = state.get("rewritten_digest_retired_by")
    valid_rewritten_digest = isinstance(rewritten_digest, str) and bool(re.fullmatch(r"[0-9a-f]{64}", rewritten_digest))
    valid_retirement = (
        isinstance(rewritten_digest_retired_by, str)
        and bool(_OPERATION_RE.fullmatch(rewritten_digest_retired_by))
        and rewritten_digest_retired_by != operation_id
    )
    if valid_rewritten_digest == valid_retirement:
        raise RedactionError("redaction transaction digest is invalid")
    original_digest = state.get("original_sha256")
    if state.get("phase") == "cleaned":
        if original_digest is not None:
            raise RedactionError("cleaned redaction transaction retains original digest")
    elif not isinstance(original_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", original_digest):
        raise RedactionError("redaction transaction digest is invalid")
    parent_operation_id = state.get("parent_operation_id")
    if parent_operation_id is not None and (
        not isinstance(parent_operation_id, str) or not _OPERATION_RE.fullmatch(parent_operation_id)
    ):
        raise RedactionError("redaction transaction parent is invalid")
    return state


def _write_state(
    state_path: Path,
    *,
    operation_id: str,
    run_id: str,
    sequence_start: int,
    sequence_end: int,
    reason: str,
    original_sha256: str | None,
    rewritten_sha256: str | None,
    phase: str,
    parent_operation_id: str | None = None,
    rewritten_digest_retired_by: str | None = None,
) -> None:
    _atomic_write(
        state_path,
        _state_bytes(
            operation_id=operation_id,
            run_id=run_id,
            sequence_start=sequence_start,
            sequence_end=sequence_end,
            reason=reason,
            original_sha256=original_sha256,
            rewritten_sha256=rewritten_sha256,
            phase=phase,
            parent_operation_id=parent_operation_id,
            rewritten_digest_retired_by=rewritten_digest_retired_by,
        ),
        mode=_FILE_MODE,
        category="redaction transaction",
    )


def _record_bytes(
    *,
    operation_id: str,
    run_id: str,
    sequence_start: int,
    sequence_end: int,
    reason: str,
    rewritten_sha256: str | None,
    quarantine_retained: bool,
    parent_operation_id: str | None,
    parent_record_sha256: str | None = None,
    rewritten_digest_retired_by: str | None = None,
) -> bytes:
    if (rewritten_sha256 is None) == (rewritten_digest_retired_by is None):
        raise RedactionError("redaction record digest is invalid")
    if (parent_operation_id is None) != (parent_record_sha256 is None):
        raise RedactionError("redaction record parent reference is invalid")
    if parent_record_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", parent_record_sha256):
        raise RedactionError("redaction record parent reference is invalid")
    payload = {
        "schema": REDACTION_SCHEMA,
        "schema_version": REDACTION_SCHEMA_VERSION,
        "operation_id": operation_id,
        "run_id": run_id,
        "sequence_start": sequence_start,
        "sequence_end": sequence_end,
        "reason_code": reason,
        "parent_operation_id": parent_operation_id,
        "chain_verified": True,
        "projection_verified": True,
        "quarantine_retained": quarantine_retained,
    }
    if parent_record_sha256 is not None:
        payload["parent_record_sha256"] = parent_record_sha256
    if rewritten_sha256 is not None:
        payload["rewritten_journal_sha256"] = rewritten_sha256
    if rewritten_digest_retired_by is not None:
        payload["rewritten_digest_retired_by"] = rewritten_digest_retired_by
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_redaction_record(
    record_path: Path,
    *,
    operation_id: str,
    run_id: str,
    sequence_start: int,
    sequence_end: int,
    reason: str,
    rewritten_sha256: str | None,
    quarantine_retained: bool,
    parent_operation_id: str | None = None,
    parent_record_sha256: str | None = None,
    rewritten_digest_retired_by: str | None = None,
) -> None:
    _atomic_write(
        record_path,
        _record_bytes(
            operation_id=operation_id,
            run_id=run_id,
            sequence_start=sequence_start,
            sequence_end=sequence_end,
            reason=reason,
            rewritten_sha256=rewritten_sha256,
            quarantine_retained=quarantine_retained,
            parent_operation_id=parent_operation_id,
            parent_record_sha256=parent_record_sha256,
            rewritten_digest_retired_by=rewritten_digest_retired_by,
        ),
        mode=_FILE_MODE,
        category="redaction record",
    )


def _redaction_record_sha256(record_path: Path) -> str:
    return _digest(
        _read_bounded_regular(
            record_path,
            limit=16 * 1024,
            category="redaction record",
        )
    )


def _validate_redaction_record(
    record_path: Path,
    *,
    operation_id: str,
    run_id: str,
    sequence_start: int,
    sequence_end: int,
    reason: str,
    rewritten_sha256: str | None,
    parent_operation_id: str | None = None,
    rewritten_digest_retired_by: str | None = None,
) -> dict[str, Any]:
    record = _load_json_object_relative(
        record_path.parent,
        record_path.name,
        limit=16 * 1024,
        category="redaction record",
    )
    if (
        record.get("schema") != REDACTION_SCHEMA
        or record.get("schema_version") != REDACTION_SCHEMA_VERSION
        or record.get("operation_id") != operation_id
        or record.get("run_id") != run_id
        or record.get("sequence_start") != sequence_start
        or record.get("sequence_end") != sequence_end
        or record.get("reason_code") != reason
        or record.get("parent_operation_id") != parent_operation_id
        or record.get("chain_verified") is not True
        or record.get("projection_verified") is not True
        or not isinstance(record.get("quarantine_retained"), bool)
    ):
        raise RedactionError("redaction record verification failed")
    parent_record_sha256 = record.get("parent_record_sha256")
    if (parent_operation_id is None) != (parent_record_sha256 is None) or (
        parent_record_sha256 is not None
        and (not isinstance(parent_record_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", parent_record_sha256))
    ):
        raise RedactionError("redaction record verification failed")
    if rewritten_sha256 is not None:
        if (
            not re.fullmatch(r"[0-9a-f]{64}", rewritten_sha256)
            or record.get("rewritten_journal_sha256") != rewritten_sha256
            or record.get("rewritten_digest_retired_by") is not None
        ):
            raise RedactionError("redaction record verification failed")
    elif (
        rewritten_digest_retired_by is None
        or not _OPERATION_RE.fullmatch(rewritten_digest_retired_by)
        or rewritten_digest_retired_by == operation_id
        or record.get("rewritten_journal_sha256") is not None
        or record.get("rewritten_digest_retired_by") != rewritten_digest_retired_by
    ):
        raise RedactionError("redaction record verification failed")
    return record


def _parse_lineage_record(raw_record: bytes, *, operation_id: str, run_id: str) -> dict[str, Any]:
    record = _parse_json_object(raw_record, category="redaction record")
    rewritten = record.get("rewritten_journal_sha256")
    rewritten_digest_retired_by = record.get("rewritten_digest_retired_by")
    valid_rewritten = isinstance(rewritten, str) and bool(re.fullmatch(r"[0-9a-f]{64}", rewritten))
    valid_retirement = (
        isinstance(rewritten_digest_retired_by, str)
        and bool(_OPERATION_RE.fullmatch(rewritten_digest_retired_by))
        and rewritten_digest_retired_by != operation_id
    )
    parent = record.get("parent_operation_id")
    parent_record_sha256 = record.get("parent_record_sha256")
    if (
        record.get("schema") != REDACTION_SCHEMA
        or record.get("schema_version") != REDACTION_SCHEMA_VERSION
        or record.get("operation_id") != operation_id
        or record.get("run_id") != run_id
        or record.get("reason_code") not in REASON_CODES
        or isinstance(record.get("sequence_start"), bool)
        or not isinstance(record.get("sequence_start"), int)
        or isinstance(record.get("sequence_end"), bool)
        or not isinstance(record.get("sequence_end"), int)
        or valid_rewritten == valid_retirement
        or (parent is not None and (not isinstance(parent, str) or not _OPERATION_RE.fullmatch(parent)))
        or (parent is None) != (parent_record_sha256 is None)
        or (
            parent_record_sha256 is not None
            and (not isinstance(parent_record_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", parent_record_sha256))
        )
        or record.get("chain_verified") is not True
        or record.get("projection_verified") is not True
        or not isinstance(record.get("quarantine_retained"), bool)
    ):
        raise RedactionError("redaction lineage record verification failed")
    return record


def _parent_record_reference(
    events: Sequence[run_journal.RunEvent],
    parent_operation_id: str | None,
) -> str | None:
    if parent_operation_id is None:
        return None
    parent_anchors = [
        event
        for event in events
        if event.event_type == "run.redaction.recorded" and event.payload.get("operation_id") == parent_operation_id
    ]
    if len(parent_anchors) != 1:
        raise RedactionError("redaction lineage is incomplete")
    payload = parent_anchors[0].payload
    reference = payload.get("record_sha256")
    if (
        set(payload)
        != {
            "operation_id",
            "affected_first_sequence",
            "affected_last_sequence",
            "reason_class",
            "record_sha256",
        }
        or not isinstance(reference, str)
        or not re.fullmatch(r"[0-9a-f]{64}", reference)
    ):
        raise RedactionError("redaction lineage parent reference is invalid")
    return reference


def _validate_chained_anchors(
    events: Sequence[run_journal.RunEvent],
    records: Mapping[str, Mapping[str, Any]],
    record_digests: Mapping[str, str],
    states: Mapping[str, Mapping[str, Any]] | None = None,
    *,
    resumable_operation_id: str | None = None,
) -> None:
    seen: set[str] = set()
    for event in events:
        if event.event_type != "run.redaction.recorded":
            continue
        payload = event.payload
        if set(payload) != {
            "operation_id",
            "affected_first_sequence",
            "affected_last_sequence",
            "reason_class",
            "record_sha256",
        }:
            raise RedactionError("redaction chained anchor verification failed")
        operation_id = payload.get("operation_id")
        if not isinstance(operation_id, str) or operation_id in seen:
            raise RedactionError("redaction chained anchor verification failed")
        record = records.get(operation_id)
        if record is None:
            raise RedactionError("redaction chained anchor verification failed")
        seen.add(operation_id)
        expected_digest = record_digests.get(operation_id)
        if expected_digest is None:
            raise RedactionError("redaction chained anchor verification failed")
        if (
            payload.get("affected_first_sequence") != record.get("sequence_start")
            or payload.get("affected_last_sequence") != record.get("sequence_end")
            or payload.get("reason_class") != record.get("reason_code")
        ):
            raise RedactionError("redaction chained anchor verification failed")
        if payload.get("record_sha256") != expected_digest:
            current = states.get(resumable_operation_id) if states is not None and resumable_operation_id else None
            allowed_current = (
                operation_id == resumable_operation_id
                and current is not None
                and current.get("phase")
                in {
                    "cleanup-authorized",
                    "cleaned",
                }
            )
            allowed_parent = (
                current is not None
                and current.get("phase") in {"cleanup-authorized", "cleaned"}
                and record.get("rewritten_digest_retired_by") == resumable_operation_id
                and current.get("parent_operation_id") == operation_id
            )
            resumable_record = records.get(resumable_operation_id) if resumable_operation_id else None
            allowed_retiring_child = (
                current is not None
                and current.get("phase") in {"cleanup-authorized", "cleaned"}
                and resumable_record is not None
                and resumable_record.get("rewritten_digest_retired_by") == operation_id
            )
            if not (allowed_current or allowed_parent or allowed_retiring_child):
                raise RedactionError("redaction chained anchor verification failed")
    missing = set(records) - seen
    if missing:
        resumable_state = states.get(resumable_operation_id) if states is not None and resumable_operation_id else None
        if missing != {resumable_operation_id} or resumable_state is None or resumable_state.get("phase") != "verified":
            raise RedactionError("redaction chained anchor verification failed")


def _redaction_anchor_payload(record: Mapping[str, Any], *, record_sha256: str) -> dict[str, Any]:
    return {
        "operation_id": record["operation_id"],
        "affected_first_sequence": record["sequence_start"],
        "affected_last_sequence": record["sequence_end"],
        "reason_class": record["reason_code"],
        "record_sha256": record_sha256,
    }


def _append_redaction_anchor(
    journal_path: Path,
    events: Sequence[run_journal.RunEvent],
    record: Mapping[str, Any],
    *,
    record_sha256: str,
) -> run_journal.RunEvent:
    return run_journal.append_event(
        journal_path,
        run_id=events[-1].run_id,
        event_type="run.redaction.recorded",
        payload=_redaction_anchor_payload(record, record_sha256=record_sha256),
        idempotency_key=f"redaction-recorded:{record['operation_id']}",
        expected_previous_sequence=events[-1].sequence,
    )


def _refresh_chained_anchors(
    run_dir: Path,
    *,
    workspace: Path,
    resumable_operation_id: str | None,
) -> str:
    """Re-chain anchor payload hashes after a record lifecycle update."""
    journal_path = run_dir / "events" / "lifecycle.jsonl"
    with run_journal.journal_mutation(journal_path):
        events = _verified_events(journal_path, category="journal")
        records, states, record_digests = _load_operation_inventory(
            run_dir / "events" / "redactions", run_dir.name, resumable_operation_id=resumable_operation_id
        )
        _validate_chained_anchors(
            events,
            records,
            record_digests,
            states,
            resumable_operation_id=resumable_operation_id,
        )
        rewritten: list[run_journal.RunEvent] = []
        previous_digest: str | None = None
        for event in events:
            payload = (
                _redaction_anchor_payload(
                    records[event.payload["operation_id"]],
                    record_sha256=record_digests[event.payload["operation_id"]],
                )
                if event.event_type == "run.redaction.recorded"
                else event.payload
            )
            envelope = run_events.build_event(
                run_id=event.run_id,
                sequence=event.sequence,
                event_type=event.event_type,
                payload=payload,
                idempotency_key=event.idempotency_key,
                recorded_at=event.recorded_at,
                previous_digest=previous_digest,
            )
            rewritten_event = run_journal.RunEvent(
                schema=envelope["schema"],
                schema_version=envelope["schema_version"],
                event_id=envelope["event_id"],
                run_id=envelope["run_id"],
                sequence=envelope["sequence"],
                event_type=envelope["event_type"],
                recorded_at=envelope["recorded_at"],
                idempotency_key=envelope["idempotency_key"],
                request_digest=envelope["request_digest"],
                previous_digest=envelope["previous_digest"],
                event_digest=envelope["event_digest"],
                payload=dict(envelope["payload"]),
            )
            rewritten.append(rewritten_event)
            previous_digest = rewritten_event.event_digest
        data = _canonical_event_bytes(rewritten)
        current = _read_bounded_regular(
            journal_path,
            limit=run_checkpoint.MAX_JOURNAL_BYTES,
            category="journal",
        )
        # Ownership is asserted even on the no-op path so a stale lock cannot
        # claim a successful refresh without re-checking active ownership.
        _assert_active_owner(workspace, run_dir)
        if current == data:
            # Anchor payloads already match the record digests; skip journal and
            # projection rewrites so a second verify/cleanup pass is a no-op.
            return _digest(data)
        _replace_journal(journal_path, data)
        snapshot = _load_json_object(run_dir / "run.json", limit=MAX_RUN_JSON_BYTES, category="run projection")
        _replace_projection(run_dir, _projection(snapshot, rewritten))
        return _digest(data)


def _preretirement_parent_record_digest(
    record: Mapping[str, Any],
    *,
    operation_id: str,
    run_id: str,
    rewritten_sha256: str,
) -> str:
    """Reconstruct the parent record digest from before retirement was recorded.

    During the mid-flight window after the parent record gains
    ``rewritten_digest_retired_by`` but before the child is realigned, the
    child's ``parent_record_sha256`` still names this prior digest. The prior
    record is fully determined by validated parent fields plus the still-present
    state-side rewritten digest.
    """
    return _digest(
        _record_bytes(
            operation_id=operation_id,
            run_id=run_id,
            sequence_start=record["sequence_start"],
            sequence_end=record["sequence_end"],
            reason=record["reason_code"],
            rewritten_sha256=rewritten_sha256,
            quarantine_retained=record["quarantine_retained"],
            parent_operation_id=record.get("parent_operation_id"),
            parent_record_sha256=record.get("parent_record_sha256"),
            rewritten_digest_retired_by=None,
        )
    )


def _accepted_parent_record_digests_for_retirement(
    *,
    parent_operation_id: str,
    run_id: str,
    parent_record: Mapping[str, Any],
    parent_state: Mapping[str, Any] | None,
    current_digest: str | None,
) -> set[str]:
    """Return digests a retiring child may name for this parent.

    Always includes the current parent record digest when present. During the
    validated mid-flight split (record already carries
    ``rewritten_digest_retired_by``, state still holds ``rewritten_sha256``),
    also accepts the exact pre-retirement digest reconstructed from those
    fields. Outside that window only the current digest is accepted.
    """
    accepted: set[str] = set()
    if isinstance(current_digest, str) and re.fullmatch(r"[0-9a-f]{64}", current_digest):
        accepted.add(current_digest)
    if parent_state is None:
        return accepted
    record_retired_by = parent_record.get("rewritten_digest_retired_by")
    state_digest = parent_state.get("rewritten_sha256")
    state_retired_by = parent_state.get("rewritten_digest_retired_by")
    if (
        isinstance(record_retired_by, str)
        and state_retired_by is None
        and isinstance(state_digest, str)
        and re.fullmatch(r"[0-9a-f]{64}", state_digest)
    ):
        accepted.add(
            _preretirement_parent_record_digest(
                parent_record,
                operation_id=parent_operation_id,
                run_id=run_id,
                rewritten_sha256=state_digest,
            )
        )
    return accepted


def _validate_lineage_graph(
    records: Mapping[str, Mapping[str, Any]],
    record_digests: Mapping[str, str] | None = None,
    *,
    states: Mapping[str, Mapping[str, Any]] | None = None,
    run_id: str | None = None,
) -> None:
    if not records:
        return
    children: dict[str, list[str]] = {}
    for operation_id, record in records.items():
        parent = record.get("parent_operation_id")
        if isinstance(parent, str):
            if parent not in records:
                raise RedactionError("redaction lineage is incomplete")
            children.setdefault(parent, []).append(operation_id)
        retired_by = record.get("rewritten_digest_retired_by")
        if isinstance(retired_by, str):
            child = records.get(retired_by)
            if child is None or child.get("parent_operation_id") != operation_id:
                raise RedactionError("redaction lineage retirement mismatch")

    if any(len(descendants) != 1 for descendants in children.values()):
        raise RedactionError("redaction lineage contains a fork")
    for parent, descendants in children.items():
        retired_by = records[parent].get("rewritten_digest_retired_by")
        if retired_by is not None and retired_by != descendants[0]:
            raise RedactionError("redaction lineage retirement mismatch")
        # Digest cross-check applies to split/full retirement edges: the child
        # must name the parent's current record digest after retirement rewrite,
        # or the exact pre-retirement digest during the mid-flight window only.
        if record_digests is not None and retired_by is not None:
            child = records[descendants[0]]
            accepted = _accepted_parent_record_digests_for_retirement(
                parent_operation_id=parent,
                run_id=run_id or "",
                parent_record=records[parent],
                parent_state=None if states is None else states.get(parent),
                current_digest=record_digests.get(parent),
            )
            if child.get("parent_record_sha256") not in accepted:
                raise RedactionError("redaction lineage digest mismatch")

    tips = set(records) - set(children)
    if len(tips) != 1:
        raise RedactionError("redaction lineage contains multiple tips")
    for operation_id in records:
        visited: set[str] = set()
        current: str | None = operation_id
        while current is not None:
            if current in visited:
                raise RedactionError("redaction lineage contains a cycle")
            visited.add(current)
            parent = records[current].get("parent_operation_id")
            current = parent if isinstance(parent, str) else None


def _load_operation_inventory(
    redactions_dir: Path,
    run_id: str,
    *,
    resumable_operation_id: str | None,
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]], dict[str, str]]:
    if not os.path.lexists(redactions_dir):
        return {}, {}, {}
    root_fd = _open_directory_handle(redactions_dir, category="redactions")
    records: dict[str, dict[str, Any]] = {}
    states: dict[str, dict[str, Any]] = {}
    record_digests: dict[str, str] = {}
    incomplete: list[str] = []
    try:
        try:
            names = sorted(os.listdir(root_fd))
        except OSError as exc:
            raise RedactionError("redaction transaction listing failed") from exc
        for operation_id in names:
            if not _OPERATION_RE.fullmatch(operation_id):
                continue
            operation_dir = redactions_dir / operation_id
            operation_fd = _open_directory_handle(operation_dir, category="redaction operation")
            try:
                try:
                    _read_relative_bounded_regular_from_handle(
                        operation_dir,
                        operation_fd,
                        "state.json",
                        limit=16 * 1024,
                        category="redaction transaction",
                    )
                except FileNotFoundError:
                    try:
                        _read_relative_bounded_regular_from_handle(
                            operation_dir,
                            operation_fd,
                            "record.json",
                            limit=16 * 1024,
                            category="redaction record",
                        )
                    except FileNotFoundError:
                        if operation_id != resumable_operation_id:
                            raise RedactionError(
                                "incomplete redaction transaction exists; retry its original request"
                            ) from None
                        incomplete.append(operation_id)
                        continue
                    except OSError as exc:
                        raise RedactionError("redaction record read failed") from exc
                    raise RedactionError("redaction transaction state/record mismatch") from None
                except OSError as exc:
                    raise RedactionError("redaction transaction read failed") from exc
            finally:
                os.close(operation_fd)

            state = _load_and_validate_state(
                operation_dir / "state.json",
                operation_id=operation_id,
                run_id=run_id,
            )
            states[operation_id] = state
            operation_fd = _open_directory_handle(operation_dir, category="redaction operation")
            try:
                try:
                    raw_record = _read_relative_bounded_regular_from_handle(
                        operation_dir,
                        operation_fd,
                        "record.json",
                        limit=16 * 1024,
                        category="redaction record",
                    )
                except FileNotFoundError:
                    if operation_id != resumable_operation_id or state.get("phase") not in {
                        "quarantined",
                        "replaced",
                    }:
                        raise RedactionError(
                            "incomplete redaction transaction exists; retry its original request"
                        ) from None
                    incomplete.append(operation_id)
                    continue
                except OSError as exc:
                    raise RedactionError("redaction record read failed") from exc
            finally:
                os.close(operation_fd)

            record = _parse_lineage_record(raw_record, operation_id=operation_id, run_id=run_id)
            if state.get("phase") in {"quarantined", "replaced", "cleanup-authorized"}:
                if operation_id != resumable_operation_id:
                    raise RedactionError("incomplete redaction transaction exists; retry its original request")
                incomplete.append(operation_id)
            records[operation_id] = record
            record_digests[operation_id] = _digest(raw_record)
    finally:
        os.close(root_fd)

    if len(set(incomplete)) > 1:
        raise RedactionError("multiple incomplete redaction transactions exist")
    for operation_id, record in records.items():
        state = states[operation_id]
        if (
            state.get("sequence_start") != record.get("sequence_start")
            or state.get("sequence_end") != record.get("sequence_end")
            or state.get("reason_code") != record.get("reason_code")
            or state.get("parent_operation_id") != record.get("parent_operation_id")
        ):
            raise RedactionError("redaction transaction state/record mismatch")
        state_digest = state.get("rewritten_sha256")
        record_digest = record.get("rewritten_journal_sha256")
        state_retired_by = state.get("rewritten_digest_retired_by")
        record_retired_by = record.get("rewritten_digest_retired_by")
        if state_digest != record_digest or state_retired_by != record_retired_by:
            if record_retired_by is not None and state_digest is not None:
                split_retired_by = record_retired_by
                split_digest = state_digest
            elif state_retired_by is not None and record_digest is not None:
                split_retired_by = state_retired_by
                split_digest = record_digest
            else:
                raise RedactionError("redaction transaction state/record mismatch")
            child_state = states.get(split_retired_by)
            child_record = records.get(split_retired_by)
            accepted_parent_digests = _accepted_parent_record_digests_for_retirement(
                parent_operation_id=operation_id,
                run_id=run_id,
                parent_record=record,
                parent_state=state,
                current_digest=record_digests.get(operation_id),
            )
            if (
                split_retired_by != resumable_operation_id
                or child_state is None
                or child_record is None
                or child_state.get("phase") != "cleanup-authorized"
                or child_state.get("parent_operation_id") != operation_id
                or child_record.get("parent_operation_id") != operation_id
                or child_record.get("parent_record_sha256") not in accepted_parent_digests
                or not isinstance(split_digest, str)
                or not re.fullmatch(r"[0-9a-f]{64}", split_digest)
            ):
                raise RedactionError("redaction transaction state/record mismatch")
        phase = state.get("phase")
        quarantine_retained = record.get("quarantine_retained")
        if (phase == "verified" and quarantine_retained is not True) or (
            phase == "cleaned" and quarantine_retained is not False
        ):
            raise RedactionError("redaction transaction state/record mismatch")
        parent_operation_id = record.get("parent_operation_id")
        parent_reference = record.get("parent_record_sha256")
        if parent_operation_id is None:
            if parent_reference is not None:
                raise RedactionError("redaction lineage parent reference is invalid")
            continue
        if parent_operation_id not in states or not isinstance(parent_reference, str):
            raise RedactionError("redaction lineage parent reference is invalid")
        quarantine = redactions_dir / operation_id / "original.jsonl"
        if os.path.lexists(quarantine):
            original_sha256 = state.get("original_sha256")
            if not isinstance(original_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", original_sha256):
                raise RedactionError("redaction lineage child quarantine is invalid")
            _verify_quarantine(quarantine, original_sha256)
            if _parent_record_reference(
                _verified_events(quarantine, category="redaction quarantine"), parent_operation_id
            ) != (parent_reference):
                raise RedactionError("redaction lineage parent reference is invalid")
        elif phase not in {"cleanup-authorized", "cleaned"}:
            raise RedactionError("redaction lineage child quarantine is missing")
    _validate_lineage_graph(records, record_digests, states=states, run_id=run_id)
    return records, states, record_digests


def _realign_child_parent_record_digest(
    redactions_dir: Path,
    *,
    run_id: str,
    parent_operation_id: str,
    child_operation_id: str,
    records: Mapping[str, Mapping[str, Any]],
) -> None:
    """Point a child record at the parent's current record digest."""
    child_record = records.get(child_operation_id)
    if child_record is None or child_record.get("parent_operation_id") != parent_operation_id:
        raise RedactionError("redaction lineage retirement mismatch")
    parent_record_path = redactions_dir / parent_operation_id / "record.json"
    child_record_path = redactions_dir / child_operation_id / "record.json"
    parent_record_digest = _redaction_record_sha256(parent_record_path)
    if child_record.get("parent_record_sha256") == parent_record_digest:
        return
    _write_redaction_record(
        child_record_path,
        operation_id=child_operation_id,
        run_id=run_id,
        sequence_start=child_record["sequence_start"],
        sequence_end=child_record["sequence_end"],
        reason=child_record["reason_code"],
        rewritten_sha256=child_record.get("rewritten_journal_sha256"),
        quarantine_retained=child_record["quarantine_retained"],
        parent_operation_id=parent_operation_id,
        parent_record_sha256=parent_record_digest,
        rewritten_digest_retired_by=child_record.get("rewritten_digest_retired_by"),
    )
    child_operation_dir = child_record_path.parent
    child_fd = _open_directory_handle(child_operation_dir, category="redaction operation")
    try:
        _remove_operation_temps(
            child_operation_dir,
            child_fd,
            patterns=(_RECORD_TEMP_RE,),
            category="redaction lineage cleanup",
        )
    finally:
        os.close(child_fd)


def _retire_rewritten_digest_aliases(
    redactions_dir: Path,
    *,
    run_id: str,
    parent_operation_id: str | None,
    retired_by_operation_id: str,
    records: Mapping[str, Mapping[str, Any]],
) -> None:
    if parent_operation_id is None:
        return
    record = records.get(parent_operation_id)
    if record is None:
        raise RedactionError("redaction lineage is incomplete")
    operation_dir = redactions_dir / parent_operation_id
    state_path = operation_dir / "state.json"
    record_path = operation_dir / "record.json"
    state = _load_and_validate_state(
        state_path,
        operation_id=parent_operation_id,
        run_id=run_id,
        sequence_start=record["sequence_start"],
        sequence_end=record["sequence_end"],
        reason=record["reason_code"],
    )
    if state.get("parent_operation_id") != record.get("parent_operation_id"):
        raise RedactionError("redaction lineage metadata mismatch")
    if record.get("rewritten_digest_retired_by") not in {None, retired_by_operation_id} or state.get(
        "rewritten_digest_retired_by"
    ) not in {None, retired_by_operation_id}:
        raise RedactionError("redaction lineage digest mismatch")
    if record.get("rewritten_digest_retired_by") != retired_by_operation_id:
        _write_redaction_record(
            record_path,
            operation_id=parent_operation_id,
            run_id=run_id,
            sequence_start=record["sequence_start"],
            sequence_end=record["sequence_end"],
            reason=record["reason_code"],
            rewritten_sha256=None,
            quarantine_retained=record["quarantine_retained"],
            parent_operation_id=record.get("parent_operation_id"),
            parent_record_sha256=record.get("parent_record_sha256"),
            rewritten_digest_retired_by=retired_by_operation_id,
        )
    # Align the retiring child's parent_record_sha256 before the parent state
    # write so a state-side crash still leaves a digest-consistent split.
    _realign_child_parent_record_digest(
        redactions_dir,
        run_id=run_id,
        parent_operation_id=parent_operation_id,
        child_operation_id=retired_by_operation_id,
        records=records,
    )
    if state.get("rewritten_digest_retired_by") != retired_by_operation_id:
        _write_state(
            state_path,
            operation_id=parent_operation_id,
            run_id=run_id,
            sequence_start=record["sequence_start"],
            sequence_end=record["sequence_end"],
            reason=record["reason_code"],
            original_sha256=state.get("original_sha256"),
            rewritten_sha256=None,
            phase=state["phase"],
            parent_operation_id=state.get("parent_operation_id"),
            rewritten_digest_retired_by=retired_by_operation_id,
        )
    operation_fd = _open_directory_handle(operation_dir, category="redaction operation")
    try:
        _remove_operation_temps(
            operation_dir,
            operation_fd,
            patterns=(_STATE_TEMP_RE, _RECORD_TEMP_RE),
            category="redaction lineage cleanup",
        )
    finally:
        os.close(operation_fd)
    _validate_redaction_record(
        record_path,
        operation_id=parent_operation_id,
        run_id=run_id,
        sequence_start=record["sequence_start"],
        sequence_end=record["sequence_end"],
        reason=record["reason_code"],
        rewritten_sha256=None,
        parent_operation_id=record.get("parent_operation_id"),
        rewritten_digest_retired_by=retired_by_operation_id,
    )


def _lineage_parent_for_digest(
    records: Mapping[str, Mapping[str, Any]],
    journal_digest: str,
) -> str | None:
    matches = [
        operation_id
        for operation_id, record in records.items()
        if record.get("rewritten_journal_sha256") == journal_digest
    ]
    if len(matches) > 1:
        raise RedactionError("redaction lineage is ambiguous")
    return matches[0] if matches else None


def _lineage_contains(
    records: Mapping[str, Mapping[str, Any]],
    *,
    ancestor_operation_id: str,
    active_journal_digest: str,
    active_events: Sequence[run_journal.RunEvent] | None = None,
) -> bool:
    anchored_operations = (
        [event.payload["operation_id"] for event in active_events if event.event_type == "run.redaction.recorded"]
        if active_events is not None
        else []
    )
    current = (
        anchored_operations[-1] if anchored_operations else _lineage_parent_for_digest(records, active_journal_digest)
    )
    visited: set[str] = set()
    while current is not None:
        if current == ancestor_operation_id:
            return True
        if current in visited:
            raise RedactionError("redaction lineage contains a cycle")
        visited.add(current)
        record = records.get(current)
        if record is None:
            raise RedactionError("redaction lineage is incomplete")
        parent = record.get("parent_operation_id")
        current = parent if isinstance(parent, str) else None
    return False


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_event_bytes(events: Sequence[run_journal.RunEvent]) -> bytes:
    try:
        return b"".join(run_events.canonical_bytes(event.to_dict()) + b"\n" for event in events)
    except run_events.CanonicalizationError as exc:
        raise RedactionError("journal canonicalization verification failed") from exc


def _verify_quarantine(quarantine_path: Path, expected_digest: str) -> None:
    original = _read_relative_bounded_regular(
        quarantine_path.parent,
        quarantine_path.name,
        limit=run_checkpoint.MAX_JOURNAL_BYTES,
        category="redaction quarantine",
    )
    if _digest(original) != expected_digest:
        raise RedactionError("redaction quarantine verification failed")


def _assert_affected_values_removed(
    original: Sequence[run_journal.RunEvent],
    rewritten: Sequence[run_journal.RunEvent],
    start: int,
    end: int,
) -> None:
    for prior, current in zip(original, rewritten, strict=True):
        if not start <= prior.sequence <= end or prior.event_type in {
            run_checkpoint.CHECKPOINT_EVENT_TYPE,
            "run.redaction.recorded",
        }:
            continue
        for key, value in prior.payload.items():
            if key == "status" or value is None:
                continue
            if value == REDACTED_VALUE or value == 0:
                continue
            if current.payload.get(key) == value:
                raise RedactionError("rewritten journal still contains affected private values")


def _replace_journal(journal_path: Path, rewritten: bytes) -> None:
    try:
        info = os.lstat(journal_path)
    except OSError as exc:
        raise RedactionError("journal replace preflight failed") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
        raise RedactionError("journal replace preflight failed")
    _atomic_write(
        journal_path,
        rewritten,
        mode=_FILE_MODE,
        category="journal",
    )


def _replace_projection(run_dir: Path, projection: run_projector.RunProjection) -> None:
    path = run_dir / "run.json"
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise RedactionError("projection replace preflight failed") from exc
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
        raise RedactionError("projection replace preflight failed")
    _atomic_write(
        path,
        projection.to_bytes(),
        mode=_FILE_MODE,
        category="projection",
    )


def _remove_shadow_history_siblings(events_dir: Path, events_fd: int) -> None:
    _assert_directory_identity(events_dir, events_fd, category="events")
    try:
        names = os.listdir(events_fd)
    except OSError as exc:
        raise RedactionError("shadow projection history listing failed") from exc
    artifact_name = run_shadow.SHADOW_ARTIFACT_NAME
    removed = False
    for name in names:
        if not (name.startswith(f"{artifact_name}.corrupt-") or name.startswith(".stale-projector-")):
            continue
        try:
            removed = _unlink_relative(events_dir, events_fd, name) or removed
        except OSError as exc:
            raise RedactionError("shadow projection history removal failed") from exc
    if removed:
        _fsync_directory_handle(events_dir, events_fd, category="shadow projection history")


def _refresh_shadow_artifact(
    run_dir: Path,
    snapshot: Mapping[str, Any],
    events: Sequence[run_journal.RunEvent],
) -> dict[str, Any]:
    events_dir = run_dir / "events"
    events_fd = _open_directory_handle(events_dir, category="events")
    artifact_path = run_shadow.shadow_artifact_path(run_dir)
    try:
        _remove_shadow_history_siblings(events_dir, events_fd)
        # Redaction changes every digest downstream of the affected range.
        # Rebase shadow evidence to the verified rewritten journal instead of
        # retaining comparison hashes that can serve as candidate-value oracles.
        artifact = run_shadow._empty_artifact(run_dir.name)
        tail = events[-1]
        projected = _projection(snapshot, events)
        projected_digest = _digest(projected.to_bytes())
        artifact["comparisons"] = 1
        artifact["matches"] = 1
        artifact["last_compared_sequence"] = tail.sequence
        artifact["last_compared_event_digest"] = tail.event_digest
        artifact["last_shadow_digest"] = projected_digest
        artifact["last_projected_digest"] = projected_digest
        artifact["last_differing_fields"] = []
        artifact["last_outcome"] = run_shadow.OUTCOME_MATCH
        artifact["last_error_category"] = None
        recorded_at = localio.utc_now_iso()
        artifact["last_recorded_at"] = recorded_at
        artifact["recent_records"] = [
            {
                "outcome": run_shadow.OUTCOME_MATCH,
                "category": None,
                "sequence": tail.sequence,
                "event_digest": tail.event_digest,
                "shadow_digest": projected_digest,
                "projected_digest": projected_digest,
                "differing_fields": [],
                "recorded_at": recorded_at,
            }
        ]
        artifact_bytes = (json.dumps(artifact, indent=2, sort_keys=True) + "\n").encode("utf-8")
        _atomic_write(
            artifact_path,
            artifact_bytes,
            mode=_FILE_MODE,
            category="shadow projection",
            parent_fd=events_fd,
        )
        return _parse_json_object(artifact_bytes, category="shadow projection")
    finally:
        os.close(events_fd)


def _post_replace_verify(
    run_dir: Path,
    *,
    expected_digest: str | None,
    resumable_operation_id: str | None = None,
) -> tuple[dict[str, Any], list[run_journal.RunEvent], str]:
    journal_path = run_dir / "events" / "lifecycle.jsonl"
    active = _read_bounded_regular(
        journal_path,
        limit=run_checkpoint.MAX_JOURNAL_BYTES,
        category="journal",
    )
    active_digest = _digest(active)
    if expected_digest is not None and active_digest != expected_digest:
        raise RedactionError("post-rewrite verification failed")
    events = _verified_events(journal_path, category="post-rewrite journal")
    if any(event.event_type == "run.redaction.recorded" for event in events):
        records, states, record_digests = _load_operation_inventory(
            run_dir / "events" / "redactions",
            run_dir.name,
            resumable_operation_id=resumable_operation_id,
        )
        _validate_chained_anchors(
            events,
            records,
            record_digests,
            states,
            resumable_operation_id=resumable_operation_id,
        )
    snapshot = _load_json_object(
        run_dir / "run.json",
        limit=MAX_RUN_JSON_BYTES,
        category="run projection",
    )
    _verify_current_projection(snapshot, events)
    _validate_checkpoint_artifacts(run_dir, snapshot, events)
    shadow = _refresh_shadow_artifact(run_dir, snapshot, events)
    tail = events[-1]
    if (
        shadow.get("schema") != run_shadow.SHADOW_SCHEMA
        or shadow.get("schema_version") != run_shadow.SHADOW_SCHEMA_VERSION
        or shadow.get("projector_version") != run_projector.PROJECTOR_VERSION
        or shadow.get("run_id") != run_dir.name
        or shadow.get("last_outcome") != run_shadow.OUTCOME_MATCH
        or shadow.get("last_compared_sequence") != tail.sequence
        or shadow.get("last_compared_event_digest") != tail.event_digest
        or shadow.get("last_shadow_digest") != shadow.get("last_projected_digest")
        or shadow.get("last_differing_fields") != []
    ):
        raise RedactionError("shadow projection verification failed")
    return snapshot, events, active_digest


def _resume_projection_after_rewrite(
    run_dir: Path,
    events: Sequence[run_journal.RunEvent],
) -> None:
    snapshot = _load_json_object(
        run_dir / "run.json",
        limit=MAX_RUN_JSON_BYTES,
        category="run projection",
    )
    projected = _projection(snapshot, events)
    if snapshot == projected.snapshot:
        return
    last_sequence = snapshot.get("journal_last_sequence")
    projected_last_sequence = projected.snapshot.get("journal_last_sequence")
    if last_sequence == projected_last_sequence:
        if _projection_semantics(snapshot) != _projection_semantics(projected.snapshot):
            raise RedactionError("post-rewrite projection semantics changed")
    else:
        snapshot_without_sequence = {
            key: value
            for key, value in snapshot.items()
            if key not in {_PROJECTION_DIGEST_FIELD, "journal_last_sequence"}
        }
        projected_without_sequence = {
            key: value
            for key, value in projected.snapshot.items()
            if key not in {_PROJECTION_DIGEST_FIELD, "journal_last_sequence"}
        }
        if snapshot_without_sequence != projected_without_sequence:
            raise RedactionError("post-rewrite projection semantics changed")
        if (
            isinstance(last_sequence, bool)
            or not isinstance(last_sequence, int)
            or isinstance(projected_last_sequence, bool)
            or not isinstance(projected_last_sequence, int)
            or last_sequence < 0
            or last_sequence > projected_last_sequence
            or any(event.sequence > last_sequence and event.event_type != "run.redaction.recorded" for event in events)
        ):
            raise RedactionError("post-rewrite projection sequence lag is invalid")
    _replace_projection(run_dir, projected)


def redact_journal(
    run_dir: Path,
    *,
    sequence_start: int,
    sequence_end: int,
    reason: str,
    operator_confirmed: bool = False,
) -> RedactionReport:
    """Redact an inclusive sequence range and retain the source quarantine.

    The operation is deterministic for ``run_id + range + reason``. Repeating
    the same call resumes an interrupted transaction or returns the already
    verified result without creating another sensitive copy.
    """
    start, end, bounded_reason = _validate_request(
        sequence_start,
        sequence_end,
        reason,
        operator_confirmed=operator_confirmed,
    )
    _require_secure_transaction_platform()
    resolved_run_dir = _resolve_run_dir(run_dir)
    _probe_secure_transaction_directory(resolved_run_dir)

    with _PROCESS_LOCK:
        with (
            _exclusive_redaction_lock(resolved_run_dir) as (snapshot, workspace),
            run_journal.journal_mutation(resolved_run_dir / "events" / "lifecycle.jsonl"),
        ):
            journal_path = resolved_run_dir / "events" / "lifecycle.jsonl"
            events = _verified_events(journal_path, category="journal")
            if end > len(events):
                raise RedactionError("invalid sequence range")
            if any(event.run_id != resolved_run_dir.name for event in events):
                raise RedactionError("journal run identity mismatch")

            operation_id = _operation_id(resolved_run_dir.name, start, end, bounded_reason)
            operation_dir, quarantine_path, record_path = _operation_paths(
                resolved_run_dir,
                operation_id,
            )
            state_path = operation_dir / "state.json"
            redactions_dir = operation_dir.parent
            original_bytes = _read_bounded_regular(
                journal_path,
                limit=run_checkpoint.MAX_JOURNAL_BYTES,
                category="journal",
            )
            if original_bytes != _canonical_event_bytes(events):
                raise RedactionError("journal changed during redaction preflight")
            active_digest = _digest(original_bytes)
            lineage_records, inventory_states, record_digests = _load_operation_inventory(
                redactions_dir,
                resolved_run_dir.name,
                resumable_operation_id=operation_id,
            )
            _validate_chained_anchors(
                events,
                lineage_records,
                record_digests,
                inventory_states,
                resumable_operation_id=operation_id,
            )
            prior_state = inventory_states.get(operation_id)

            if prior_state is not None and any(
                event.event_type == "run.redaction.recorded" and event.payload.get("operation_id") == operation_id
                for event in events
            ):
                _resume_projection_after_rewrite(resolved_run_dir, events)
                snapshot = _load_json_object(
                    resolved_run_dir / "run.json",
                    limit=MAX_RUN_JSON_BYTES,
                    category="run projection",
                )

            if prior_state is not None:
                if (
                    prior_state.get("sequence_start") != start
                    or prior_state.get("sequence_end") != end
                    or prior_state.get("reason_code") != bounded_reason
                ):
                    raise RedactionError("redaction transaction metadata mismatch")
                prior_rewritten_sha256 = prior_state.get("rewritten_sha256")
                if isinstance(prior_rewritten_sha256, str) and active_digest == prior_rewritten_sha256:
                    _resume_projection_after_rewrite(resolved_run_dir, events)
                    snapshot = _load_json_object(
                        resolved_run_dir / "run.json",
                        limit=MAX_RUN_JSON_BYTES,
                        category="run projection",
                    )

            before_projection = _verify_current_projection(snapshot, events)
            _validate_checkpoint_artifacts(resolved_run_dir, snapshot, events)

            if prior_state is not None:
                parent_operation_id = prior_state.get("parent_operation_id")
                rewritten_sha256 = prior_state.get("rewritten_sha256")
                rewritten_digest_retired_by = prior_state.get("rewritten_digest_retired_by")
                is_current_or_descendant = (
                    isinstance(rewritten_sha256, str) and active_digest == rewritten_sha256
                ) or _lineage_contains(
                    lineage_records,
                    ancestor_operation_id=operation_id,
                    active_journal_digest=active_digest,
                    active_events=events,
                )
                if is_current_or_descendant:
                    if prior_state["phase"] == "cleanup-authorized":
                        raise RedactionError("redaction cleanup is incomplete; retry explicit cleanup")
                    cleaned = prior_state["phase"] == "cleaned"
                    if cleaned:
                        if os.path.lexists(quarantine_path):
                            raise RedactionError("cleanup state conflicts with retained quarantine")
                    else:
                        original_sha256 = prior_state.get("original_sha256")
                        if not isinstance(original_sha256, str):
                            raise RedactionError("redaction transaction digest is invalid")
                        _verify_quarantine(quarantine_path, original_sha256)
                    anchor_present = any(
                        event.event_type == "run.redaction.recorded"
                        and event.payload.get("operation_id") == operation_id
                        for event in events
                    )
                    if anchor_present:
                        _resume_projection_after_rewrite(resolved_run_dir, events)
                    if not os.path.lexists(record_path):
                        if not isinstance(rewritten_sha256, str) or active_digest != rewritten_sha256 or cleaned:
                            raise RedactionError("redaction lineage record is missing")
                        try:
                            _write_redaction_record(
                                record_path,
                                operation_id=operation_id,
                                run_id=resolved_run_dir.name,
                                sequence_start=start,
                                sequence_end=end,
                                reason=bounded_reason,
                                rewritten_sha256=rewritten_sha256,
                                quarantine_retained=True,
                                parent_operation_id=parent_operation_id,
                                parent_record_sha256=_parent_record_reference(events, parent_operation_id),
                            )
                        except (OSError, RedactionError) as exc:
                            raise RedactionError("redaction record write failed") from exc
                        _write_state(
                            state_path,
                            operation_id=operation_id,
                            run_id=resolved_run_dir.name,
                            sequence_start=start,
                            sequence_end=end,
                            reason=bounded_reason,
                            original_sha256=prior_state.get("original_sha256"),
                            rewritten_sha256=rewritten_sha256,
                            phase="verified",
                            parent_operation_id=parent_operation_id,
                        )
                    _validate_redaction_record(
                        record_path,
                        operation_id=operation_id,
                        run_id=resolved_run_dir.name,
                        sequence_start=start,
                        sequence_end=end,
                        reason=bounded_reason,
                        rewritten_sha256=rewritten_sha256,
                        parent_operation_id=parent_operation_id,
                        rewritten_digest_retired_by=rewritten_digest_retired_by,
                    )
                    if not anchor_present:
                        record = _load_json_object(record_path, limit=16 * 1024, category="redaction record")
                        _assert_active_owner(workspace, resolved_run_dir)
                        anchor = _append_redaction_anchor(
                            journal_path,
                            events,
                            record,
                            record_sha256=_redaction_record_sha256(record_path),
                        )
                        _replace_projection(
                            resolved_run_dir,
                            _projection(
                                _load_json_object(
                                    resolved_run_dir / "run.json",
                                    limit=MAX_RUN_JSON_BYTES,
                                    category="run projection",
                                ),
                                [*events, anchor],
                            ),
                        )
                    _post_replace_verify(resolved_run_dir, expected_digest=None)
                    return RedactionReport(
                        operation_id,
                        start,
                        end,
                        quarantine_path,
                        record_path,
                        cleaned=cleaned,
                    )
                if active_digest != prior_state.get("original_sha256") or prior_state["phase"] != "quarantined":
                    raise RedactionError("redaction transaction does not match the active journal")

            active_anchor_ids = [
                event.payload["operation_id"] for event in events if event.event_type == "run.redaction.recorded"
            ]
            parent_operation_id = (
                prior_state.get("parent_operation_id")
                if prior_state is not None
                else _lineage_parent_for_digest(lineage_records, active_digest)
                or (active_anchor_ids[-1] if active_anchor_ids else None)
            )
            if not any(
                start <= event.sequence <= end
                and event.event_type not in {run_checkpoint.CHECKPOINT_EVENT_TYPE, "run.redaction.recorded"}
                for event in events
            ):
                raise RedactionError("redaction range contains no redactable payloads")
            parent_record_sha256 = _parent_record_reference(events, parent_operation_id)
            rewritten_events, rewritten_bytes = _rewrite_events(
                events,
                sequence_start=start,
                sequence_end=end,
                operation_id=operation_id,
            )
            _assert_affected_values_removed(events, rewritten_events, start, end)
            after_projection = _projection(snapshot, rewritten_events)
            if _projection_semantics(before_projection.snapshot) != _projection_semantics(after_projection.snapshot):
                raise RedactionError("redaction changes the observable run projection")
            original_sha256 = active_digest
            rewritten_sha256 = _digest(rewritten_bytes)
            if prior_state is not None and (
                prior_state.get("original_sha256") != original_sha256
                or prior_state.get("rewritten_sha256") != rewritten_sha256
                or prior_state.get("rewritten_digest_retired_by") is not None
            ):
                raise RedactionError("redaction transaction digest mismatch")

            _prepare_operation_dirs(resolved_run_dir, operation_id)
            try:
                _publish_quarantine(operation_dir, quarantine_path, original_bytes)
                _write_state(
                    state_path,
                    operation_id=operation_id,
                    run_id=resolved_run_dir.name,
                    sequence_start=start,
                    sequence_end=end,
                    reason=bounded_reason,
                    original_sha256=original_sha256,
                    rewritten_sha256=rewritten_sha256,
                    phase="quarantined",
                    parent_operation_id=parent_operation_id,
                )
            except RedactionError:
                raise
            except (OSError, run_journal.RunJournalError) as exc:
                raise RedactionError("redaction quarantine durability failed") from exc

            _assert_active_owner(workspace, resolved_run_dir)
            _replace_journal(journal_path, rewritten_bytes)
            _write_state(
                state_path,
                operation_id=operation_id,
                run_id=resolved_run_dir.name,
                sequence_start=start,
                sequence_end=end,
                reason=bounded_reason,
                original_sha256=original_sha256,
                rewritten_sha256=rewritten_sha256,
                phase="replaced",
                parent_operation_id=parent_operation_id,
            )
            _replace_projection(resolved_run_dir, after_projection)
            _post_replace_verify(
                resolved_run_dir,
                expected_digest=rewritten_sha256,
                resumable_operation_id=operation_id,
            )
            try:
                _write_redaction_record(
                    record_path,
                    operation_id=operation_id,
                    run_id=resolved_run_dir.name,
                    sequence_start=start,
                    sequence_end=end,
                    reason=bounded_reason,
                    rewritten_sha256=rewritten_sha256,
                    quarantine_retained=True,
                    parent_operation_id=parent_operation_id,
                    parent_record_sha256=parent_record_sha256,
                )
            except (OSError, RedactionError) as exc:
                raise RedactionError("redaction record write failed") from exc
            _write_state(
                state_path,
                operation_id=operation_id,
                run_id=resolved_run_dir.name,
                sequence_start=start,
                sequence_end=end,
                reason=bounded_reason,
                original_sha256=original_sha256,
                rewritten_sha256=rewritten_sha256,
                phase="verified",
                parent_operation_id=parent_operation_id,
            )
            _validate_redaction_record(
                record_path,
                operation_id=operation_id,
                run_id=resolved_run_dir.name,
                sequence_start=start,
                sequence_end=end,
                reason=bounded_reason,
                rewritten_sha256=rewritten_sha256,
                parent_operation_id=parent_operation_id,
            )
            record = _load_json_object(record_path, limit=16 * 1024, category="redaction record")
            _assert_active_owner(workspace, resolved_run_dir)
            anchor = _append_redaction_anchor(
                journal_path,
                rewritten_events,
                record,
                record_sha256=_redaction_record_sha256(record_path),
            )
            _replace_projection(resolved_run_dir, _projection(after_projection.snapshot, [*rewritten_events, anchor]))
            _post_replace_verify(resolved_run_dir, expected_digest=None)
            return RedactionReport(operation_id, start, end, quarantine_path, record_path)


def cleanup_redaction_quarantine(
    run_dir: Path,
    *,
    operation_id: str,
    operator_confirmed: bool = False,
) -> RedactionReport:
    """Remove one verified quarantine after an explicit incident-procedure step."""
    if operator_confirmed is not True:
        raise RedactionError("operator confirmation is required")
    if not isinstance(operation_id, str) or not _OPERATION_RE.fullmatch(operation_id):
        raise RedactionError("invalid redaction operation id")
    _require_secure_transaction_platform()
    resolved_run_dir = _resolve_run_dir(run_dir)
    _probe_secure_transaction_directory(resolved_run_dir)

    with _PROCESS_LOCK:
        with _exclusive_redaction_lock(resolved_run_dir) as (snapshot, workspace):
            operation_dir, quarantine_path, record_path = _operation_paths(
                resolved_run_dir,
                operation_id,
            )
            if not _existing_operation_dir(resolved_run_dir, operation_id):
                raise RedactionError("redaction transaction does not exist")
            state_path = operation_dir / "state.json"
            lineage_records, inventory_states, _ = _load_operation_inventory(
                operation_dir.parent,
                resolved_run_dir.name,
                resumable_operation_id=operation_id,
            )
            state = inventory_states.get(operation_id)
            if state is None:
                raise RedactionError("redaction transaction metadata mismatch")
            record = lineage_records.get(operation_id)
            if record is None:
                raise RedactionError("redaction transaction metadata mismatch")
            start = state.get("sequence_start")
            end = state.get("sequence_end")
            reason = state.get("reason_code")
            parent_operation_id = state.get("parent_operation_id")
            parent_record_sha256 = record.get("parent_record_sha256")
            rewritten_sha256 = state.get("rewritten_sha256")
            rewritten_digest_retired_by = state.get("rewritten_digest_retired_by")
            if (
                isinstance(start, bool)
                or not isinstance(start, int)
                or isinstance(end, bool)
                or not isinstance(end, int)
                or not isinstance(reason, str)
                or reason not in REASON_CODES
            ):
                raise RedactionError("redaction transaction metadata mismatch")

            journal_path = resolved_run_dir / "events" / "lifecycle.jsonl"
            events = _verified_events(journal_path, category="journal")
            _resume_projection_after_rewrite(resolved_run_dir, events)
            snapshot = _load_json_object(
                resolved_run_dir / "run.json",
                limit=MAX_RUN_JSON_BYTES,
                category="run projection",
            )
            _verify_current_projection(snapshot, events)
            _validate_checkpoint_artifacts(resolved_run_dir, snapshot, events)
            try:
                _, _, active_digest = _post_replace_verify(
                    resolved_run_dir,
                    expected_digest=None,
                    resumable_operation_id=operation_id,
                )
            except RedactionError as exc:
                raise RedactionError("cleanup verification failed") from exc
            if not _lineage_contains(
                lineage_records,
                ancestor_operation_id=operation_id,
                active_journal_digest=active_digest,
                active_events=events,
            ):
                raise RedactionError("cleanup verification failed")
            try:
                _validate_redaction_record(
                    record_path,
                    operation_id=operation_id,
                    run_id=resolved_run_dir.name,
                    sequence_start=start,
                    sequence_end=end,
                    reason=reason,
                    rewritten_sha256=rewritten_sha256,
                    parent_operation_id=parent_operation_id,
                    rewritten_digest_retired_by=rewritten_digest_retired_by,
                )
            except RedactionError as exc:
                raise RedactionError("cleanup verification failed") from exc

            if state["phase"] == "cleaned":
                if os.path.lexists(quarantine_path):
                    raise RedactionError("cleanup state conflicts with retained quarantine")
                refreshed_digest = _refresh_chained_anchors(
                    resolved_run_dir,
                    workspace=workspace,
                    resumable_operation_id=operation_id,
                )
                _post_replace_verify(resolved_run_dir, expected_digest=refreshed_digest)
                return RedactionReport(
                    operation_id,
                    start,
                    end,
                    quarantine_path,
                    record_path,
                    cleaned=True,
                )

            original_sha256 = state.get("original_sha256")
            if not isinstance(original_sha256, str):
                raise RedactionError("cleanup verification failed")
            if os.path.lexists(quarantine_path):
                try:
                    _verify_quarantine(quarantine_path, original_sha256)
                except RedactionError as exc:
                    raise RedactionError("cleanup verification failed") from exc
            elif state["phase"] != "cleanup-authorized":
                raise RedactionError("cleanup verification failed")

            # Authorization is durable before deletion. A crash in this
            # window leaves enough state to revalidate and complete cleanup.
            _write_redaction_record(
                record_path,
                operation_id=operation_id,
                run_id=resolved_run_dir.name,
                sequence_start=start,
                sequence_end=end,
                reason=reason,
                rewritten_sha256=rewritten_sha256,
                quarantine_retained=True,
                parent_operation_id=parent_operation_id,
                parent_record_sha256=parent_record_sha256,
                rewritten_digest_retired_by=rewritten_digest_retired_by,
            )
            _write_state(
                state_path,
                operation_id=operation_id,
                run_id=resolved_run_dir.name,
                sequence_start=start,
                sequence_end=end,
                reason=reason,
                original_sha256=original_sha256,
                rewritten_sha256=rewritten_sha256,
                phase="cleanup-authorized",
                parent_operation_id=parent_operation_id,
                rewritten_digest_retired_by=rewritten_digest_retired_by,
            )

            operation_fd = _open_directory_handle(
                operation_dir,
                category="redaction operation",
            )
            try:
                _assert_active_owner(workspace, resolved_run_dir)
                try:
                    removed_quarantine = _unlink_relative(
                        operation_dir,
                        operation_fd,
                        quarantine_path.name,
                    )
                    if removed_quarantine:
                        _fsync_directory_handle(
                            operation_dir,
                            operation_fd,
                            category="quarantine cleanup",
                        )
                    _remove_operation_temps(
                        operation_dir,
                        operation_fd,
                        patterns=(_QUARANTINE_TEMP_RE, _STATE_TEMP_RE),
                        category="redaction cleanup",
                    )
                except (OSError, RedactionError) as exc:
                    raise RedactionError("quarantine cleanup failed") from exc
            finally:
                os.close(operation_fd)

            _retire_rewritten_digest_aliases(
                operation_dir.parent,
                run_id=resolved_run_dir.name,
                parent_operation_id=parent_operation_id,
                retired_by_operation_id=operation_id,
                records=lineage_records,
            )
            if parent_operation_id is not None:
                # Retirement rewrites the parent record and realigns this child's
                # parent_record_sha256; refresh before the cleaned record write.
                parent_record_sha256 = _redaction_record_sha256(
                    operation_dir.parent / parent_operation_id / "record.json"
                )
            _write_redaction_record(
                record_path,
                operation_id=operation_id,
                run_id=resolved_run_dir.name,
                sequence_start=start,
                sequence_end=end,
                reason=reason,
                rewritten_sha256=rewritten_sha256,
                quarantine_retained=False,
                parent_operation_id=parent_operation_id,
                parent_record_sha256=parent_record_sha256,
                rewritten_digest_retired_by=rewritten_digest_retired_by,
            )
            if isinstance(rewritten_digest_retired_by, str):
                # Cleaning a retired parent rewrites its record digest; keep the
                # retiring child aligned for the lineage digest cross-check.
                lineage_records = {
                    **lineage_records,
                    operation_id: {
                        **record,
                        "quarantine_retained": False,
                        "rewritten_journal_sha256": rewritten_sha256,
                        "rewritten_digest_retired_by": rewritten_digest_retired_by,
                    },
                }
                _realign_child_parent_record_digest(
                    operation_dir.parent,
                    run_id=resolved_run_dir.name,
                    parent_operation_id=operation_id,
                    child_operation_id=rewritten_digest_retired_by,
                    records=lineage_records,
                )
            _write_state(
                state_path,
                operation_id=operation_id,
                run_id=resolved_run_dir.name,
                sequence_start=start,
                sequence_end=end,
                reason=reason,
                original_sha256=None,
                rewritten_sha256=rewritten_sha256,
                phase="cleaned",
                parent_operation_id=parent_operation_id,
                rewritten_digest_retired_by=rewritten_digest_retired_by,
            )
            refreshed_digest = _refresh_chained_anchors(
                resolved_run_dir,
                workspace=workspace,
                resumable_operation_id=operation_id,
            )
            _post_replace_verify(resolved_run_dir, expected_digest=refreshed_digest)
            return RedactionReport(
                operation_id,
                start,
                end,
                quarantine_path,
                record_path,
                cleaned=True,
            )


__all__ = [
    "REDACTED_VALUE",
    "REASON_CODES",
    "RedactionError",
    "RedactionReport",
    "cleanup_redaction_quarantine",
    "redact_journal",
]
