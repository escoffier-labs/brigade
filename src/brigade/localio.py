"""Shared local helpers for JSON receipts, UTC timestamps, hashes, and slugs.

These helpers were extracted from near-identical private copies that lived in
most command modules. Modules with intentionally different behavior (error
reporting reads, unsorted writes, custom slug charsets) keep their own copies.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TREE_FINGERPRINT_EVIDENCE_PATHS = (
    ".brigade/work/verify-runs",
    "memory/outcome/records.jsonl",
    "memory/outcome/.records.lock",
    ".brigade/work/miseledger-export-cursor.json",
    ":(glob).brigade/work/miseledger-export-*.jsonl",
)


def utc_now() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string with a +00:00 offset."""
    return datetime.now(timezone.utc).isoformat()


def utc_now_iso_z() -> str:
    """Return the current UTC time as an ISO-8601 string with a Z suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json_dict(path: Path) -> dict[str, Any] | None:
    """Read a JSON object from path; return None when missing, invalid, or not a dict."""
    try:
        payload = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def tree_fingerprint(target: Path) -> str | None:
    """Return the Git tree for the live workspace, excluding generated evidence."""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            index_file = Path(tmpdir) / "index"
            env = {**os.environ, "GIT_INDEX_FILE": str(index_file)}

            def git(*args: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run(
                    ["git", "-C", str(target), *args],
                    check=False,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    timeout=30,
                    env=env,
                )

            if git("read-tree", "HEAD").returncode != 0 or git("add", "-A").returncode != 0:
                return None
            if git("reset", "-q", "HEAD", "--", *TREE_FINGERPRINT_EVIDENCE_PATHS).returncode != 0:
                return None
            value = git("write-tree")
            return value.stdout.strip() if value.returncode == 0 and value.stdout.strip() else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _active_bound_target(path: Path) -> tuple[Any, tuple[str, ...], str] | None:
    from . import run_dirfd

    return run_dirfd.active_binding_for(path)


def write_text_atomic(path: Path, data: str) -> None:
    """Write data to path atomically, creating parents.

    The write goes to a temp file in the same directory and is swapped in with
    os.replace, so a reader (or a crashed writer) never observes a half-written
    file: it sees either the old file or the complete new one. On failure before
    replacement the temp file is removed and the existing file is left untouched.
    A directory-fsync failure occurs after replacement and means durable
    publication is unconfirmed, although the new bytes are already present.

    When a ``run_dirfd`` binding is active and ``path`` is a lexical descendant
    of that bound run directory, the write is authorized only through held
    no-follow dirfds. Without a binding the pathname writer is unchanged.
    """
    bound = _active_bound_target(path)
    if bound is not None:
        run_dir, components, name = bound
        run_dir.write_text_atomic(components, name, data)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
    _fsync_parent_directory(path.parent)


def _fsync_parent_directory(path: Path) -> None:
    """Durably publish a replacement on platforms that support directory fsync."""
    if not _supports_directory_fsync():
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path.resolve(), flags)
    primary: BaseException | None = None
    try:
        if not stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("atomic-write parent is not a directory")
        os.fsync(fd)
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            os.close(fd)
        except BaseException:
            if primary is None:
                raise


def _supports_directory_fsync() -> bool:
    return os.name == "posix"


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Write bytes to path atomically, creating parents.

    When a ``run_dirfd`` binding is active and ``path`` is a lexical descendant
    of that bound run directory, the write is authorized only through held
    no-follow dirfds. Without a binding the pathname writer is unchanged.
    """
    bound = _active_bound_target(path)
    if bound is not None:
        run_dir, components, name = bound
        run_dir.write_bytes_atomic(components, name, data)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write payload as indented, key-sorted JSON, atomically, creating parents."""
    write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def write_text_exclusive(path: Path, data: str) -> None:
    """Publish complete data atomically without replacing an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def write_json_exclusive(path: Path, payload: dict[str, Any]) -> None:
    """Create path with a JSON payload without replacing an existing file."""
    write_text_exclusive(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_jsonl_dicts(path: Path) -> list[dict[str, Any]]:
    """Read JSONL records from path, keeping only lines that parse to JSON objects."""
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return records
    for line in lines:
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def canonical_json_digest(payload: Any, *, exclude_keys: set[str] | None = None) -> str:
    """Return a full sha256 digest of payload's canonical sorted-key JSON.

    exclude_keys applies to the TOP LEVEL only: it exists so an artifact can
    carry its own digest field without self-reference. Nested keys with the
    same name are content and must stay inside the hash, or edits to them
    would be undetectable.
    """
    normalized = payload
    if exclude_keys and isinstance(payload, dict):
        normalized = {key: item for key, item in payload.items() if key not in exclude_keys}
    rendered = json.dumps(normalized, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    """Return the sha256 digest for path's bytes, streamed in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_hash(value: object) -> str:
    """Return a 16-char sha256 fingerprint of value's canonical JSON rendering."""
    rendered = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()[:16]


def slugify(value: str, *, fallback: str) -> str:
    """Lowercase value, collapse runs outside [a-z0-9._-] to hyphens, or fallback."""
    slug = re.sub(r"[^a-z0-9._-]+", "-", value.strip().lower()).strip("-")
    return slug or fallback


def check_git_ignored(repo: Path, path: Path) -> str:
    """Report whether path is git-ignored inside repo: yes/no/outside-target/unknown."""
    try:
        relative = path.expanduser().resolve().relative_to(repo)
    except ValueError:
        return "outside-target"
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "check-ignore", "-q", str(relative)],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if result.returncode == 0:
        return "yes"
    if result.returncode == 1:
        return "no"
    return "unknown"


def parse_iso_datetime(value: object) -> datetime | None:
    """Parse an ISO-8601 string (Z accepted) into an aware UTC datetime, or None."""
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
