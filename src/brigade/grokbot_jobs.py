"""Private, handle-first queue storage for Grok Bot jobs.

This module deliberately has no provider or transport integration. It validates
and persists private job envelopes, then exposes only safe job projections.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

try:  # pragma: no cover - Windows does not provide fcntl.
    import fcntl
except ImportError:  # pragma: no cover - exercised on Windows.
    fcntl = None  # type: ignore[assignment]

try:  # pragma: no cover - unavailable on POSIX.
    import msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX.
    msvcrt = None  # type: ignore[assignment]


JOB_ID_RE = re.compile(r"^grokbot-[0-9a-f]{24}$")
REPOSITORY_PART_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,98}$")
IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
TASK_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
GITHUB_PULL_URL_RE = re.compile(
    r"^https://github\.com/[A-Za-z0-9][A-Za-z0-9_.-]{0,98}/[A-Za-z0-9][A-Za-z0-9_.-]{0,98}/pull/[1-9][0-9]*$"
)
LOWER_HEX_40_RE = re.compile(r"^[0-9a-f]{40}$")
LOWER_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")

JOB_STATES = frozenset({"queued", "claimed", "running", "completed", "failed", "expired", "canceled"})
ROLES = frozenset({"implementation-worker", "repository-scout"})
ARTIFACT_KINDS = frozenset({"draft-pr", "branch", "report"})
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
JOB_SCHEMA = "brigade.grokbot.job.v1"
IDEMPOTENCY_SCHEMA = "brigade.grokbot.idempotency.v1"


class GrokbotJobError(ValueError):
    """A rejected Grok Bot job request with a stable machine-readable reason."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def enqueue(
    target: Path,
    spec: dict[str, Any],
    idempotency_key: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Queue a validated private envelope and return an opaque handle."""
    envelope = _validate_spec(spec)
    key = _validate_idempotency_key(idempotency_key)
    task_hash = _task_hash(envelope)
    root, jobs_dir, idempotency_dir, lock_path = _storage_paths(target)

    with _queue_lock(root, lock_path):
        idempotency_path = idempotency_dir / f"{hashlib.sha256(key.encode('utf-8')).hexdigest()}.json"
        existing = _read_json_file(idempotency_path, missing_ok=True)
        if existing is not None:
            idempotency = _validate_idempotency_record(existing)
            if idempotency["task_hash"] != task_hash:
                raise GrokbotJobError("idempotency-conflict")
            job_id = idempotency["job_id"]
            record = _load_record(jobs_dir, job_id)
            if record["task_hash"] != task_hash:
                raise GrokbotJobError("corrupt-storage")
            return _handle(record, idempotent=True)

        timestamp = _now_iso(now)
        job_id = f"grokbot-{secrets.token_hex(12)}"
        record = {
            "schema": JOB_SCHEMA,
            "job_id": job_id,
            "task_hash": task_hash,
            "state": "queued",
            "created_at": timestamp,
            "updated_at": timestamp,
            "queued_at": timestamp,
            "timeout_seconds": envelope["timeout_seconds"],
            "item_revision": 1,
            "spec": envelope,
        }
        _write_json_file(jobs_dir / f"{job_id}.json", record)
        _write_json_file(
            idempotency_path,
            {"schema": IDEMPOTENCY_SCHEMA, "job_id": job_id, "task_hash": task_hash},
        )
        return _handle(record, idempotent=False)


def get_job(target: Path, job_id: str) -> dict[str, Any]:
    """Return the safe projection for one job, never its private envelope."""
    _, jobs_dir, _, _ = _storage_paths(target)
    return _projection(_load_record(jobs_dir, _validate_job_id(job_id)))


def status(target: Path, job_id: str | None = None) -> dict[str, Any]:
    """Return one job projection or all safe projections in deterministic order."""
    if job_id is not None:
        return get_job(target, job_id)
    _, jobs_dir, _, _ = _storage_paths(target)
    jobs: list[dict[str, Any]] = []
    for path in sorted(jobs_dir.glob("grokbot-*.json")):
        _assert_regular_file(path)
        record = _read_json_file(path)
        if record is None:
            raise GrokbotJobError("corrupt-storage")
        jobs.append(_projection(_validate_record(record)))
    return {"jobs": jobs}


def claim(
    target: Path,
    job_id: str,
    bot_id: str,
    lease_id: str,
    lease_seconds: int,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Claim a queued job with one opaque bot lease."""
    job_id = _validate_job_id(job_id)
    bot_id = _validate_opaque_id(bot_id, "invalid-bot-id")
    lease_id = _validate_opaque_id(lease_id, "invalid-lease-id")
    lease_seconds = _validate_lease_seconds(lease_seconds)
    root, jobs_dir, _, lock_path = _storage_paths(target)
    timestamp, instant = _timestamp(now)
    with _queue_lock(root, lock_path):
        record = _load_record(jobs_dir, job_id)
        if record["state"] == "claimed":
            if record["bot_id"] == bot_id and record["lease_id"] == lease_id:
                _require_live_lease(record, instant)
                return _projection(record)
            raise GrokbotJobError("lease-conflict")
        if record["state"] != "queued":
            _reject_nonqueued_claim(record)
        deadline = _deadline(record)
        if instant >= deadline:
            raise GrokbotJobError("job-expired")
        record.update(
            {
                "state": "claimed",
                "claimed_at": timestamp,
                "lease_expires_at": _format_timestamp(min(instant + timedelta(seconds=lease_seconds), deadline)),
                "bot_id": bot_id,
                "lease_id": lease_id,
            }
        )
        _commit_mutation(jobs_dir, record, timestamp)
        return _projection(record)


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
    root, jobs_dir, _, lock_path = _storage_paths(target)
    timestamp, instant = _timestamp(now)
    with _queue_lock(root, lock_path):
        record = _load_record(jobs_dir, job_id)
        _require_current_lease(record, bot_id, lease_id, instant)
        record["lease_expires_at"] = _format_timestamp(
            min(instant + timedelta(seconds=lease_seconds), _deadline(record))
        )
        _commit_mutation(jobs_dir, record, timestamp)
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
    root, jobs_dir, _, lock_path = _storage_paths(target)
    timestamp, instant = _timestamp(now)
    with _queue_lock(root, lock_path):
        record = _load_record(jobs_dir, job_id)
        _require_current_lease(record, bot_id, lease_id, instant)
        current = record["state"]
        if state == "running":
            if current != "claimed" or artifact is not None:
                raise GrokbotJobError("invalid-transition")
        elif state == "failed":
            if current not in {"claimed", "running"} or artifact is not None:
                raise GrokbotJobError("invalid-transition")
        else:
            if current != "running":
                raise GrokbotJobError("invalid-transition")
            if "cancel_requested_at" in record:
                raise GrokbotJobError("cancel-requested")
            record["result_artifact"] = _validate_completion_artifact(artifact, record["spec"])
        record["state"] = state
        _commit_mutation(jobs_dir, record, timestamp)
        return _projection(record)


def cancel(target: Path, job_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Cancel a queued job or request cooperative cancellation of a live lease."""
    job_id = _validate_job_id(job_id)
    root, jobs_dir, _, lock_path = _storage_paths(target)
    timestamp, _ = _timestamp(now)
    with _queue_lock(root, lock_path):
        record = _load_record(jobs_dir, job_id)
        if record["state"] in {"completed", "failed", "expired", "canceled"}:
            return _projection(record)
        if record["state"] == "queued":
            record["state"] = "canceled"
            _commit_mutation(jobs_dir, record, timestamp)
            return _projection(record)
        if "cancel_requested_at" not in record:
            record["cancel_requested_at"] = timestamp
            _commit_mutation(jobs_dir, record, timestamp)
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
    root, jobs_dir, _, lock_path = _storage_paths(target)
    timestamp, instant = _timestamp(now)
    with _queue_lock(root, lock_path):
        record = _load_record(jobs_dir, job_id)
        _require_current_lease(record, bot_id, lease_id, instant)
        if "cancel_requested_at" not in record:
            raise GrokbotJobError("cancellation-not-requested")
        record["state"] = "canceled"
        _commit_mutation(jobs_dir, record, timestamp)
        return _projection(record)


def expire(target: Path, job_id: str, now: datetime | None = None) -> dict[str, Any]:
    """Terminalize jobs whose deadline or current lease has elapsed, never requeue."""
    job_id = _validate_job_id(job_id)
    root, jobs_dir, _, lock_path = _storage_paths(target)
    timestamp, instant = _timestamp(now)
    with _queue_lock(root, lock_path):
        record = _load_record(jobs_dir, job_id)
        if record["state"] in {"completed", "failed", "expired", "canceled"}:
            return _projection(record)
        deadline = _deadline(record)
        expires_at = deadline if record["state"] == "queued" else min(_parse_timestamp(record["lease_expires_at"]), deadline)
        if instant < expires_at:
            return _projection(record)
        record["state"] = "expired"
        _commit_mutation(jobs_dir, record, timestamp)
        return _projection(record)


def _storage_paths(target: Path) -> tuple[Path, Path, Path, Path]:
    base = Path(target).expanduser().absolute()
    root = base / ".brigade" / "cloud" / "grokbot"
    _ensure_directory(root)
    jobs_dir = root / "jobs"
    idempotency_dir = root / "idempotency"
    _ensure_directory(jobs_dir)
    _ensure_directory(idempotency_dir)
    lock_path = root / "queue.lock"
    _assert_regular_or_missing(lock_path)
    return root, jobs_dir, idempotency_dir, lock_path


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
def _queue_lock(root: Path, lock_path: Path) -> Iterator[None]:
    _ensure_directory(root)
    _assert_regular_or_missing(lock_path)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(lock_path, flags, FILE_MODE)
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc
    locked = False
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise GrokbotJobError("unsafe-storage")
        _chmod_file_descriptor(descriptor, lock_path)
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


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    _ensure_directory(path.parent)
    _assert_regular_or_missing(path)
    data = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    temporary = Path(temporary_name)
    try:
        _chmod_file_descriptor(descriptor, temporary)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _assert_regular_or_missing(path)
        os.replace(temporary, path)
        os.chmod(path, FILE_MODE)
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc
    finally:
        temporary.unlink(missing_ok=True)


def _read_json_file(path: Path, *, missing_ok: bool = False) -> dict[str, Any] | None:
    _assert_regular_or_missing(path)
    if not path.exists():
        if missing_ok:
            return None
        raise GrokbotJobError("job-not-found")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GrokbotJobError("corrupt-storage") from exc
    if not isinstance(payload, dict):
        raise GrokbotJobError("corrupt-storage")
    return payload


def _chmod_file_descriptor(descriptor: int, path: Path) -> None:
    if hasattr(os, "fchmod"):
        os.fchmod(descriptor, FILE_MODE)
        return
    _assert_regular_file(path)
    os.chmod(path, FILE_MODE)


def _load_record(jobs_dir: Path, job_id: str) -> dict[str, Any]:
    record = _read_json_file(jobs_dir / f"{job_id}.json")
    if record is None:  # Kept for the benefit of static type checkers.
        raise GrokbotJobError("job-not-found")
    return _validate_record(record)


def _validate_record(record: dict[str, Any]) -> dict[str, Any]:
    required = {
        "schema",
        "job_id",
        "task_hash",
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
    if type(record.get("item_revision")) is not int or record["item_revision"] < 1:
        raise GrokbotJobError("corrupt-storage")
    created_at = _parse_timestamp(record.get("created_at"))
    queued_at = _parse_timestamp(record.get("queued_at"))
    updated_at = _parse_timestamp(record.get("updated_at"))
    if not created_at <= queued_at <= updated_at:
        raise GrokbotJobError("corrupt-storage")
    deadline = created_at + timedelta(seconds=spec["timeout_seconds"])
    live_fields = {"claimed_at", "lease_expires_at", "bot_id", "lease_id"}
    present_live_fields = live_fields & set(record)
    if present_live_fields and present_live_fields != live_fields:
        raise GrokbotJobError("corrupt-storage")
    if present_live_fields:
        claimed_at = _parse_timestamp(record["claimed_at"])
        lease_expires_at = _parse_timestamp(record["lease_expires_at"])
        if not created_at <= claimed_at <= updated_at or not claimed_at <= lease_expires_at <= deadline:
            raise GrokbotJobError("corrupt-storage")
        _validate_opaque_id(record["bot_id"], "corrupt-storage")
        _validate_opaque_id(record["lease_id"], "corrupt-storage")
    if "cancel_requested_at" in record:
        if not present_live_fields or not _parse_timestamp(record["claimed_at"]) <= _parse_timestamp(
            record["cancel_requested_at"]
        ) <= updated_at:
            raise GrokbotJobError("corrupt-storage")
    state = record["state"]
    if state == "queued" and (present_live_fields or "cancel_requested_at" in record or "result_artifact" in record):
        raise GrokbotJobError("corrupt-storage")
    if state in {"claimed", "running", "failed", "completed"} and not present_live_fields:
        raise GrokbotJobError("corrupt-storage")
    if state == "completed":
        if "result_artifact" not in record:
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


def _commit_mutation(jobs_dir: Path, record: dict[str, Any], timestamp: str) -> None:
    record["updated_at"] = timestamp
    record["item_revision"] += 1
    _write_json_file(jobs_dir / f"{record['job_id']}.json", record)


def _reject_nonqueued_claim(record: dict[str, Any]) -> None:
    if record["state"] in {"completed", "failed", "expired", "canceled"}:
        raise GrokbotJobError("terminal-state")
    raise GrokbotJobError("invalid-state")


def _require_current_lease(record: dict[str, Any], bot_id: str, lease_id: str, instant: datetime) -> None:
    if record["state"] in {"completed", "failed", "expired", "canceled"}:
        raise GrokbotJobError("terminal-state")
    if record["state"] not in {"claimed", "running"}:
        raise GrokbotJobError("invalid-state")
    if record["bot_id"] != bot_id or record["lease_id"] != lease_id:
        raise GrokbotJobError("lease-conflict")
    _require_live_lease(record, instant)


def _require_live_lease(record: dict[str, Any], instant: datetime) -> None:
    if instant >= min(_parse_timestamp(record["lease_expires_at"]), _deadline(record)):
        raise GrokbotJobError("lease-expired")


def _deadline(record: dict[str, Any]) -> datetime:
    return _parse_timestamp(record["created_at"]) + timedelta(seconds=record["timeout_seconds"])


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


def _validate_spec(spec: object) -> dict[str, Any]:
    if not isinstance(spec, dict):
        raise GrokbotJobError("invalid-spec")
    _reject_sensitive_keys(spec)
    keys = set(spec)
    unknown = keys - REQUIRED_ENVELOPE_KEYS
    if unknown:
        raise GrokbotJobError("unknown-key")
    if keys != REQUIRED_ENVELOPE_KEYS:
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


def _bounded_string(value: object, *, reason: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise GrokbotJobError(reason)
    return value


def _validate_repository(value: object) -> None:
    repository = _bounded_string(value, reason="invalid-repository", maximum=200)
    parts = repository.split("/")
    if len(parts) != 2 or not all(REPOSITORY_PART_RE.fullmatch(part) for part in parts):
        raise GrokbotJobError("invalid-repository")


def _validate_base_ref(value: object) -> None:
    ref = _bounded_string(value, reason="invalid-base-ref", maximum=128)
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref)
        or "//" in ref
        or ".." in ref
        or "@{" in ref
        or ref.endswith((".", "/"))
        or any(part.endswith(".lock") for part in ref.split("/"))
    ):
        raise GrokbotJobError("invalid-base-ref")


def _validate_ownership_paths(value: object) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 64:
        raise GrokbotJobError("invalid-ownership-paths")
    paths: set[str] = set()
    for item in value:
        path = _bounded_string(item, reason="invalid-ownership-paths", maximum=512)
        if (
            path in paths
            or path.startswith("/")
            or "\\" in path
            or any(part in {"", ".", ".."} for part in path.split("/"))
        ):
            raise GrokbotJobError("invalid-ownership-paths")
        paths.add(path)


def _validate_commands(value: object) -> None:
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise GrokbotJobError("invalid-verification-commands")
    for command in value:
        _bounded_string(command, reason="invalid-verification-commands", maximum=1000)


def _validate_artifact(value: object, *, role: object) -> None:
    if not isinstance(value, dict):
        raise GrokbotJobError("invalid-artifact")
    if set(value) != {"kind"}:
        raise GrokbotJobError("invalid-artifact")
    kind = value.get("kind")
    if kind not in ARTIFACT_KINDS:
        raise GrokbotJobError("invalid-artifact")
    if role == "implementation-worker" and kind not in {"draft-pr", "branch"}:
        raise GrokbotJobError("invalid-artifact")
    if role == "repository-scout" and kind != "report":
        raise GrokbotJobError("invalid-artifact")


def _validate_idempotency_key(value: object) -> str:
    if not isinstance(value, str) or not IDEMPOTENCY_KEY_RE.fullmatch(value):
        raise GrokbotJobError("invalid-idempotency-key")
    return value


def _validate_opaque_id(value: object, reason: str) -> str:
    if not isinstance(value, str) or not OPAQUE_ID_RE.fullmatch(value):
        raise GrokbotJobError(reason)
    return value


def _validate_lease_seconds(value: object) -> int:
    if type(value) is not int or not 30 <= value <= 3600:
        raise GrokbotJobError("invalid-lease-seconds")
    return value


def _validate_job_id(value: object) -> str:
    if not isinstance(value, str) or not JOB_ID_RE.fullmatch(value):
        raise GrokbotJobError("invalid-job-id")
    return value


def _task_hash(spec: dict[str, Any]) -> str:
    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _now_iso(now: datetime | None) -> str:
    return _timestamp(now)[0]


def _timestamp(now: datetime | None) -> tuple[str, datetime]:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    return _format_timestamp(value), value


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise GrokbotJobError("corrupt-storage")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise GrokbotJobError("corrupt-storage") from exc
    if parsed.tzinfo is None or _format_timestamp(parsed) != value:
        raise GrokbotJobError("corrupt-storage")
    return parsed.astimezone(timezone.utc)


def _validate_completion_artifact(artifact: object, spec: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(artifact, dict):
        raise GrokbotJobError("invalid-artifact")
    expected = spec.get("artifact")
    if not isinstance(expected, dict):  # Covered by record validation; retained for type safety.
        raise GrokbotJobError("corrupt-storage")
    kind = expected["kind"]
    if artifact.get("kind") != kind:
        raise GrokbotJobError("artifact-mismatch")
    if kind == "draft-pr":
        if set(artifact) != {"kind", "url", "branch"}:
            raise GrokbotJobError("invalid-artifact")
        url = artifact.get("url")
        if not isinstance(url, str) or not GITHUB_PULL_URL_RE.fullmatch(url):
            raise GrokbotJobError("invalid-artifact")
        _validate_repo_relative_branch(artifact.get("branch"))
    elif kind == "branch":
        if set(artifact) != {"kind", "branch", "commit"}:
            raise GrokbotJobError("invalid-artifact")
        _validate_repo_relative_branch(artifact.get("branch"))
        commit = artifact.get("commit")
        if not isinstance(commit, str) or not LOWER_HEX_40_RE.fullmatch(commit):
            raise GrokbotJobError("invalid-artifact")
    elif kind == "report":
        if set(artifact) != {"kind", "path", "sha256"}:
            raise GrokbotJobError("invalid-artifact")
        _validate_repo_relative_path(artifact.get("path"))
        digest = artifact.get("sha256")
        if not isinstance(digest, str) or not LOWER_HEX_64_RE.fullmatch(digest):
            raise GrokbotJobError("invalid-artifact")
    else:  # _validate_spec guarantees this cannot happen for a valid record.
        raise GrokbotJobError("corrupt-storage")
    return artifact


def _validate_repo_relative_branch(value: object) -> str:
    branch = _bounded_string(value, reason="invalid-artifact", maximum=255)
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch)
        or "//" in branch
        or branch.startswith("-")
        or ".." in branch
        or "@{" in branch
        or branch.endswith((".", "/", ".lock"))
        or any(part in {"", ".", ".."} or part.endswith(".lock") for part in branch.split("/"))
    ):
        raise GrokbotJobError("invalid-artifact")
    return branch


def _validate_repo_relative_path(value: object) -> str:
    path = _bounded_string(value, reason="invalid-artifact", maximum=512)
    if (
        path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or any(ord(character) < 32 for character in path)
    ):
        raise GrokbotJobError("invalid-artifact")
    return path
