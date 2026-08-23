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
from datetime import datetime, timezone
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
            if existing.get("task_hash") != task_hash:
                raise GrokbotJobError("idempotency-conflict")
            job_id = existing.get("job_id")
            if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
                raise GrokbotJobError("corrupt-storage")
            record = _load_record(jobs_dir, job_id)
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
            "revision": 1,
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
    if record.get("schema") != JOB_SCHEMA:
        raise GrokbotJobError("corrupt-storage")
    job_id = record.get("job_id")
    if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id):
        raise GrokbotJobError("corrupt-storage")
    if record.get("state") not in JOB_STATES:
        raise GrokbotJobError("corrupt-storage")
    if not isinstance(record.get("spec"), dict):
        raise GrokbotJobError("corrupt-storage")
    _validate_spec(record["spec"])
    return record


def _handle(record: dict[str, Any], *, idempotent: bool) -> dict[str, Any]:
    return {"job_id": record["job_id"], "state": record["state"], "idempotent": idempotent}


def _projection(record: dict[str, Any]) -> dict[str, Any]:
    spec = record["spec"]
    assert isinstance(spec, dict)
    return {
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
        "revision": record["revision"],
        "artifact": spec["artifact"],
    }


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


def _validate_job_id(value: object) -> str:
    if not isinstance(value, str) or not JOB_ID_RE.fullmatch(value):
        raise GrokbotJobError("invalid-job-id")
    return value


def _task_hash(spec: dict[str, Any]) -> str:
    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _now_iso(now: datetime | None) -> str:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
