"""Shared local helpers for JSON receipts, UTC timestamps, hashes, and slugs.

These helpers were extracted from near-identical private copies that lived in
most command modules. Modules with intentionally different behavior (error
reporting reads, unsorted writes, custom slug charsets) keep their own copies.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write payload as indented, key-sorted JSON, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


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
        )
    except OSError:
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
