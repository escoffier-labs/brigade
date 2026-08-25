"""Local-only approved feed for the Grok Bot queue.

The feed never invents work. It reads an explicit private manifest, validates
every entry against the existing job-envelope rules, and optionally enqueues a
bounded number of unknown jobs through the existing queue primitives.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from . import grokbot_jobs

FEED_SCHEMA = "brigade.grokbot.feed.v1"
REQUIRED_MANIFEST_KEYS = frozenset({"schema", "approved", "label", "entries"})
REQUIRED_ENTRY_KEYS = frozenset({"idempotency_key", "spec"})
MIN_LIMIT = 1
MAX_LIMIT = 10
DEFAULT_LIMIT = 1
LABEL_MAXIMUM = 160


class FeedError(ValueError):
    """A rejected feed request with a stable machine-readable reason."""

    def __init__(self, reason: str, index: int | None = None):
        self.reason = reason
        self.index = index
        super().__init__(reason)


def preflight(target: Path, manifest: Path, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Validate a feed manifest without creating or mutating queue state."""
    limit = _validate_limit(limit)
    payload = load_manifest(manifest)
    return _preview(target, payload, limit)


def apply(target: Path, manifest: Path, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
    """Enqueue unknown feed entries after a complete preflight succeeds."""
    limit = _validate_limit(limit)
    payload = load_manifest(manifest)
    preview = _preview(target, payload, limit)
    jobs: list[dict[str, Any]] = []
    created = 0
    skipped = 0
    for index, entry in enumerate(payload["entries"]):
        if created >= preview["limit"]:
            break
        try:
            spec = grokbot_jobs._validate_spec(entry["spec"])
            key = grokbot_jobs._validate_idempotency_key(entry["idempotency_key"])
            handle = grokbot_jobs.enqueue(target, spec, key)
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            raise FeedError("queue-error", index=index) from exc
        jobs.append(
            {
                "job_id": handle["job_id"],
                "state": handle["state"],
                "idempotent": handle["idempotent"],
            }
        )
        if handle["idempotent"]:
            skipped += 1
        else:
            created += 1
    return {
        "valid": preview["valid"],
        "known": preview["known"],
        "limit": preview["limit"],
        "created": created,
        "skipped": skipped,
        "jobs": jobs,
    }


def load_manifest(path: Path) -> dict[str, Any]:
    """Load a private feed file after checking ownership and permissions."""
    try:
        payload = json.loads(_read_manifest_snapshot(path))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeedError("malformed-manifest") from exc
    if not isinstance(payload, dict):
        raise FeedError("malformed-manifest")
    if set(payload) != REQUIRED_MANIFEST_KEYS:
        if payload.get("schema") != FEED_SCHEMA:
            raise FeedError("invalid-schema")
        if payload.get("approved") is not True:
            raise FeedError("not-approved")
        raise FeedError("malformed-manifest")
    if payload["schema"] != FEED_SCHEMA:
        raise FeedError("invalid-schema")
    if payload["approved"] is not True:
        raise FeedError("not-approved")
    _bounded_label(payload["label"])
    entries = payload["entries"]
    if not isinstance(entries, list):
        raise FeedError("malformed-manifest")
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or set(entry) != REQUIRED_ENTRY_KEYS:
            raise FeedError("invalid-entry", index=index)
        key = entry["idempotency_key"]
        if not isinstance(key, str):
            raise FeedError("invalid-idempotency-key", index=index)
        if key in seen:
            raise FeedError("duplicate-key", index=index)
        seen.add(key)
    return payload


def _preview(target: Path, payload: dict[str, Any], limit: int) -> dict[str, Any]:
    known = 0
    for index, entry in enumerate(payload["entries"]):
        try:
            key = grokbot_jobs._validate_idempotency_key(entry["idempotency_key"])
            spec = grokbot_jobs._validate_spec(entry["spec"])
        except grokbot_jobs.GrokbotJobError as exc:
            raise FeedError(exc.reason, index=index) from exc
        existing = _existing_idempotency(target, key)
        if existing is None:
            continue
        if existing["task_hash"] != grokbot_jobs._task_hash(spec):
            raise FeedError("idempotency-conflict", index=index)
        known += 1
    return {"valid": len(payload["entries"]), "known": known, "limit": limit}


def _validate_limit(limit: object) -> int:
    if type(limit) is not int or not MIN_LIMIT <= limit <= MAX_LIMIT:
        raise FeedError("invalid-limit")
    return limit


def _bounded_label(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > LABEL_MAXIMUM or "\x00" in value:
        raise FeedError("invalid-label")
    return value


def _read_manifest_snapshot(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    else:
        try:
            info = Path(path).lstat()
        except OSError as exc:
            raise FeedError("unsafe-manifest") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise FeedError("unsafe-manifest")
    descriptor: int | None = None
    try:
        descriptor = os.open(os.fspath(path), flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise FeedError("unsafe-manifest")
        owner_uid = getattr(os, "getuid", None)
        if owner_uid is not None and info.st_uid != owner_uid():
            raise FeedError("unsafe-manifest")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise FeedError("unsafe-manifest")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    except FeedError:
        raise
    except OSError as exc:
        raise FeedError("unsafe-manifest") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                raise FeedError("unsafe-manifest") from exc


def _existing_idempotency(target: Path, key: str) -> dict[str, Any] | None:
    name = f"{grokbot_jobs._idempotency_key_hash(key).removeprefix('sha256:')}.json"
    descriptor: int | None = None
    try:
        if os.name != "posix":
            directory = grokbot_jobs._Directory(
                Path(target).expanduser() / ".brigade" / "cloud" / "grokbot" / "idempotency",
                None,
            )
        else:
            descriptor = grokbot_jobs._open_directory_path(Path(target).expanduser().absolute())
            for component in (".brigade", "cloud", "grokbot", "idempotency"):
                try:
                    child = grokbot_jobs.os.open(component, grokbot_jobs._directory_flags(), dir_fd=descriptor)
                except FileNotFoundError:
                    return None
                previous = descriptor
                descriptor = child
                grokbot_jobs.os.close(previous)
                grokbot_jobs._assert_directory_descriptor(descriptor)
            directory = grokbot_jobs._Directory(
                Path(target) / ".brigade" / "cloud" / "grokbot" / "idempotency",
                descriptor,
            )
        payload = grokbot_jobs._read_json_file(directory, name, missing_ok=True)
        if payload is None:
            return None
        return grokbot_jobs._validate_idempotency_record(payload)
    except grokbot_jobs.GrokbotJobError as exc:
        raise FeedError(exc.reason) from exc
    except OSError as exc:
        raise FeedError("unsafe-storage") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                raise FeedError("unsafe-storage") from exc
