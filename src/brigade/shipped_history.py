"""Digests of every released text of the files `brigade init` and `brigade care install` place.

An installed copy that is byte-for-byte (SKILL.md: newline-folded; runbook: pins
excluded) a text Brigade shipped in an earlier release has no local edits, so an
upgrade may replace it with the current bundled text. Any other differing copy
is the operator's. This module only answers "is this digest a recorded shipped
text of that file?"; it never touches an installed file.

The record is the checked-in ``templates/shipped-file-history.json``, generated
by ``scripts/shipped_file_history.py --write`` from every ``v*`` release tag and
the working tree. It is read as a plain file: no symlink is followed, the size is
capped, and any problem (missing, linked, oversized, malformed) yields no
digests, so the caller keeps the installed file. Nothing is fetched or executed.

Keys are ``skill:<skill id>`` for a bundled SKILL.md and ``runbook:<installed
relative path>`` for a shipped runbook. A runbook is digested without its
top-level ``pins`` block (and only that block), because pins are the one part
`brigade` rewrites on every install.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = 1
MANIFEST_NAME = "shipped-file-history.json"
MAX_MANIFEST_BYTES = 1 << 20
MAX_INSTALLED_BYTES = 1 << 20
TAG_PATTERN = re.compile(r"^v\d+\.\d+\.\d+$")
DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_READ_FLAGS = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_BINARY", 0) | getattr(os, "O_CLOEXEC", 0)
_READ_FLAGS |= getattr(os, "O_NONBLOCK", 0)


@dataclass(frozen=True)
class Entry:
    """One recorded text: its digest and the first release tag that shipped it (None while unreleased)."""

    sha256: str
    tag: str | None


def skill_key(skill_id: str) -> str:
    return f"skill:{skill_id}"


def runbook_key(rel: str) -> str:
    return f"runbook:{rel}"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def skill_digest(data: bytes) -> str:
    """Digest of SKILL.md bytes with CRLF folded to LF (the form skills are compared in)."""
    return _sha256(data.replace(b"\r\n", b"\n"))


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def runbook_payload_digest(payload: Mapping[str, Any]) -> str:
    """Digest of a runbook body without its top-level ``pins`` key."""
    body = {key: value for key, value in payload.items() if key != "pins"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _sha256(text.encode("utf-8"))


def runbook_bytes_digest(raw: bytes) -> str | None:
    """Digest of an installed runbook's bytes, or None when they are not a plain JSON object."""
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
        return runbook_payload_digest(payload) if isinstance(payload, dict) else None
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None


def manifest_path() -> Path:
    return Path(__file__).resolve().parent / "templates" / MANIFEST_NAME


def read_regular_file(path: Path, limit: int) -> bytes:
    """Read a regular file without following a link; raise OSError for a link, special file or oversize."""
    fd = os.open(path, _READ_FLAGS)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"not a regular file: {path}")
        # Windows has no O_NOFOLLOW: require the opened file to be the path itself, not a link's target.
        named = os.lstat(path)
        if stat.S_ISLNK(named.st_mode) or (info.st_dev, info.st_ino) != (named.st_dev, named.st_ino):
            raise OSError(f"path is a link, not the opened file: {path}")
        if info.st_size > limit:
            raise OSError(f"file exceeds {limit} bytes: {path}")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(fd, 1 << 16):
            total += len(chunk)
            if total > limit:
                raise OSError(f"file exceeds {limit} bytes: {path}")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def parse_manifest(raw: bytes) -> dict[str, tuple[Entry, ...]]:
    """Validate manifest bytes and return key -> entries; ValueError names the first problem."""
    try:
        payload = json.loads(raw.decode("utf-8"), object_pairs_hook=_reject_duplicate_keys)
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or set(payload) != {"schema", "files"}:
        raise ValueError("expected an object with exactly 'schema' and 'files'")
    if payload["schema"] != SCHEMA or isinstance(payload["schema"], bool):
        raise ValueError(f"unsupported schema {payload['schema']!r}")
    files = payload["files"]
    if not isinstance(files, dict):
        raise ValueError("'files' must be an object")
    parsed: dict[str, tuple[Entry, ...]] = {}
    for key, rows in files.items():
        if not key.startswith(("skill:", "runbook:")) or not isinstance(rows, list) or not rows:
            raise ValueError(f"bad entry list for {key!r}")
        entries: list[Entry] = []
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict) or set(row) != {"sha256", "tag"}:
                raise ValueError(f"bad entry for {key!r}")
            digest, tag = row["sha256"], row["tag"]
            if not isinstance(digest, str) or not DIGEST_PATTERN.match(digest) or digest in seen:
                raise ValueError(f"bad or repeated digest for {key!r}")
            if tag is not None and not (isinstance(tag, str) and TAG_PATTERN.match(tag)):
                raise ValueError(f"bad tag for {key!r}: {tag!r}")
            seen.add(digest)
            entries.append(Entry(digest, tag))
        parsed[key] = tuple(entries)
    return parsed


def load(path: Path | None = None) -> dict[str, tuple[Entry, ...]]:
    """Read and validate the manifest. Raises OSError or ValueError; callers on the install path use :func:`digests`."""
    return parse_manifest(read_regular_file(path or manifest_path(), MAX_MANIFEST_BYTES))


def digests(key: str, path: Path | None = None) -> frozenset[str]:
    """Recorded digests for ``key``. Empty on any problem, which means "keep the installed file"."""
    try:
        return frozenset(entry.sha256 for entry in load(path).get(key, ()))
    except (OSError, ValueError):
        return frozenset()
