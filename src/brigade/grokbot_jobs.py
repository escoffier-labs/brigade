"""Retained local job projections and validation for Task 5."""

from __future__ import annotations


import hashlib


import json


import os


import stat


from contextlib import contextmanager


from dataclasses import dataclass


from datetime import timedelta


from pathlib import Path


from typing import Any, Iterator


from .grokbot_job_clock import parse_timestamp as _parse_timestamp


from . import dirfd as dirfd_mod


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
    validate_memory_refs,
    validate_opaque_id as _validate_opaque_id,
    validate_ownership_paths as _validate_ownership_paths,
    validate_repository as _validate_repository,
    validate_worker_label,
)


from .grokbot_jobs_claim_target import valid_claim_target as _valid_claim_target


from .grokbot_jobs_hub import _hub_jobs, _hub_tracker_artifact, _hub_unavailable_row


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


OPTIONAL_ENVELOPE_KEYS = frozenset({"queue_ttl_seconds", "memory_refs"})


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


JOB_SCHEMA = "brigade.grokbot.job.v1"


AUTHORITY_SCHEMA = "brigade.grokbot.authority.v1"


AUTHORITY_MARKER_NAME = "authority.json"


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


def _directory_flags() -> int:
    try:
        return dirfd_mod.directory_flags()
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc


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


def _read_json_file(directory: _Directory, name: str, *, missing_ok: bool = False) -> dict[str, Any] | None:
    if directory.descriptor is None:  # pragma: no cover - exercised on Windows.
        return _read_json_file_windows(directory.path / name, missing_ok=missing_ok)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), dir_fd=directory.descriptor
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


def _list_names(directory: _Directory, *, prefix: str, suffix: str) -> list[str]:
    try:
        if directory.descriptor is None:  # pragma: no cover - exercised on Windows.
            names = [path.name for path in directory.path.glob(f"{prefix}*{suffix}")]
        else:
            # Storage may predate lock acquisition; enumerate through a fresh
            # open description while staying anchored to the verified directory.
            scan_fd = os.open(".", _directory_flags(), dir_fd=directory.descriptor)
            try:
                names = os.listdir(scan_fd)
            finally:
                os.close(scan_fd)
    except OSError as exc:
        raise GrokbotJobError("unsafe-storage") from exc
    return sorted(name for name in names if name.startswith(prefix) and name.endswith(suffix))


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
        "worker_label",
        "claim_target",
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
        if "worker_label" in record:
            try:
                validate_worker_label(record["worker_label"])
            except GrokbotJobError:
                raise GrokbotJobError("corrupt-storage") from None
    elif "worker_label" in record:
        raise GrokbotJobError("corrupt-storage")
    if "claim_target" in record and not (present_live_fields and _valid_claim_target(record["claim_target"])):
        raise GrokbotJobError("corrupt-storage")
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
    if "memory_refs" in normalized:
        normalized["memory_refs"] = validate_memory_refs(normalized["memory_refs"])
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
    # Card references follow the cards, so a changed card never changes a job's identity.
    spec = {key: value for key, value in spec.items() if key != "memory_refs"}
    encoded = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


DEFAULT_QUEUE_TTL_SECONDS = 86400
