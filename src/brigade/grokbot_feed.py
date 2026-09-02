"""Local-only approved feed for the Grok Bot queue.

The feed never invents work. It reads an explicit private manifest, validates
every entry against the existing job-envelope rules, and optionally enqueues a
bounded number of unknown jobs through the existing queue primitives.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import grokbot_jobs

FEED_SCHEMA = "brigade.grokbot.feed.v1"
REQUIRED_MANIFEST_KEYS = frozenset({"schema", "approved", "label", "entries"})
REQUIRED_ENTRY_KEYS = frozenset({"idempotency_key", "spec"})
MIN_LIMIT = 1
MAX_LIMIT = 10
DEFAULT_LIMIT = 1
LABEL_MAXIMUM = 160
WAKE_SCHEMA = "brigade.grokbot.wake.v1"
WAKE_CONFIG_NAME = "wake.json"
WAKE_LOG_NAME = "wake-notify.jsonl"
WAKE_REQUIRED_KEYS = frozenset({"schema", "webhook_url", "sender_key_file"})
WAKE_BODY_KEYS = ("job_id", "role", "label", "repository")
WAKE_TIMEOUT_SECONDS = 8
WAKE_URL_MAXIMUM = 2048
WAKE_KEY_MAXIMUM = 4096
WAKE_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


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
            notify_enqueue(
                target,
                {
                    "job_id": handle["job_id"],
                    "role": spec["role"],
                    "label": spec["label"],
                    "repository": spec["repository"],
                },
            )
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the sender key cannot follow a Location header."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def wake_config_path(target: Path) -> Path:
    return Path(target).expanduser() / ".brigade" / "cloud" / "grokbot" / WAKE_CONFIG_NAME


def wake_notify_log_path(target: Path) -> Path:
    return Path(target).expanduser() / ".brigade" / "cloud" / "grokbot" / WAKE_LOG_NAME


def notify_enqueue(target: Path, job: dict[str, Any]) -> None:
    """Best-effort wake POST after a newly created enqueue. Never raises."""
    try:
        config = _load_wake_config(target)
        if config is None:
            return
        key = _read_wake_sender_key(Path(config["sender_key_file"]))
        status = _post_wake(config["webhook_url"], key, _wake_body(job))
        _append_wake_status(target, status)
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception:
        return


def _wake_body(job: dict[str, Any]) -> dict[str, str]:
    body = {key: job[key] for key in WAKE_BODY_KEYS}
    if any(not isinstance(value, str) or not value or "\x00" in value for value in body.values()):
        raise FeedError("invalid-wake-body")
    return body


def _load_wake_config(target: Path) -> dict[str, str] | None:
    path = wake_config_path(target)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        return None
    if stat.S_IMODE(info.st_mode) != 0o600:
        return None
    owner_uid = getattr(os, "getuid", None)
    if owner_uid is not None and info.st_uid != owner_uid():
        return None
    try:
        payload = json.loads(_read_owner_regular_file(path, maximum=4096))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, FeedError):
        return None
    if not isinstance(payload, dict) or set(payload) != WAKE_REQUIRED_KEYS:
        return None
    if payload.get("schema") != WAKE_SCHEMA:
        return None
    url = payload.get("webhook_url")
    key_file = payload.get("sender_key_file")
    if not isinstance(url, str) or not _valid_wake_url(url):
        return None
    if not isinstance(key_file, str) or not key_file:
        return None
    expanded = Path(key_file).expanduser()
    if not expanded.is_absolute():
        return None
    return {"schema": WAKE_SCHEMA, "webhook_url": url, "sender_key_file": str(expanded)}


def _valid_wake_url(value: object) -> bool:
    if not isinstance(value, str) or not value or len(value) > WAKE_URL_MAXIMUM or "\x00" in value:
        return False
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or parsed.username is not None or parsed.password is not None:
        return False
    host = (parsed.hostname or "").casefold()
    if not host:
        return False
    if parsed.scheme == "http" and host not in WAKE_LOOPBACK_HOSTS:
        return False
    return True


def _read_wake_sender_key(path: Path) -> str:
    data = _read_owner_regular_file(path, maximum=WAKE_KEY_MAXIMUM, require_mode=0o600)
    text = data.decode("utf-8").rstrip("\r\n")
    if not text or len(text) > WAKE_KEY_MAXIMUM or "\x00" in text or not text.isascii():
        raise FeedError("unsafe-wake-key")
    return text


def _read_owner_regular_file(path: Path, *, maximum: int, require_mode: int | None = 0o600) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    else:
        info = Path(path).lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise FeedError("unsafe-wake-config")
    descriptor: int | None = None
    try:
        descriptor = os.open(os.fspath(path), flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise FeedError("unsafe-wake-config")
        owner_uid = getattr(os, "getuid", None)
        if owner_uid is not None and info.st_uid != owner_uid():
            raise FeedError("unsafe-wake-config")
        if require_mode is not None and stat.S_IMODE(info.st_mode) != require_mode:
            raise FeedError("unsafe-wake-config")
        if info.st_size > maximum:
            raise FeedError("unsafe-wake-config")
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > maximum:
            raise FeedError("unsafe-wake-config")
        return data
    except FeedError:
        raise
    except OSError as exc:
        raise FeedError("unsafe-wake-config") from exc
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError as exc:
                raise FeedError("unsafe-wake-config") from exc


def _post_wake(url: str, key: str, body: dict[str, str]) -> int:
    data = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {key}")
    request.add_header("X-Automation-Key", key)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=WAKE_TIMEOUT_SECONDS) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except TimeoutError:
        return 0
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError) or isinstance(reason, socket.timeout):
            return 0
        message = str(reason).casefold() if reason is not None else ""
        if "timed out" in message or "timeout" in message:
            return 0
        return 0
    except (socket.timeout, OSError, ValueError):
        return 0


def _append_wake_status(target: Path, status: int) -> None:
    path = wake_notify_log_path(target)
    line = (json.dumps({"status": status}, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    descriptor: int | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(os.fspath(path), flags, grokbot_jobs.FILE_MODE)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            return
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, grokbot_jobs.FILE_MODE)
        os.write(descriptor, line)
        os.fsync(descriptor)
    except OSError:
        return
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                return
