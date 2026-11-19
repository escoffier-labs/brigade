"""Unambiguous ``git status`` snapshot of a target, and the dirty-subject check that reads it back (#2589).

``git status --short`` quotes some paths, joins a rename as ``old -> new`` (so a filename that holds
`` -> `` splits wrong) and prints paths relative to the working directory, where a wholly untracked
target reads ``?? ./``. This module reads ``git status --porcelain=v1 -z`` instead. The entries are NUL
separated, paths are never quoted and are relative to the repository root, and a rename or copy names
both of its paths. The snapshot records the target's own prefix inside the repository, so the subject
(relative to the target) is compared as a repository path.

A recorded snapshot is a mapping with ``dirty_files`` (one ``XY path`` line per path, both paths of a
rename), ``status_format`` and ``prefix``. Anything that is not this format, whether an older
``--short`` list or a malformed record, reads as dirty for every subject: a format the check cannot
parse must never read clean.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

STATUS_FORMAT = "z1"


def _run(target: Path, args: list[str], timeout: float) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(target), *args],
            capture_output=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if result.returncode != 0:
        return None
    out = result.stdout
    return out.encode("utf-8", errors="replace") if isinstance(out, str) else out


def parse_status_z(raw: bytes) -> list[str] | None:
    """``XY path`` lines from ``git status --porcelain=v1 -z`` output, ``None`` when it is malformed.

    A rename or copy entry is followed by its source path, which becomes a second line with the same
    status, so a subject renamed away reads as dirty too.
    """
    text = raw.decode("utf-8", errors="replace")
    fields = text.split("\0")
    if fields and fields[-1] == "":
        fields.pop()
    lines: list[str] = []
    index = 0
    while index < len(fields):
        entry = fields[index]
        index += 1
        if len(entry) < 4 or entry[2] != " ":
            return None
        status, path = entry[:2], entry[3:]
        lines.append(f"{status} {path}")
        if "R" in status or "C" in status:
            if index >= len(fields) or not fields[index]:
                return None
            lines.append(f"{status} {fields[index]}")
            index += 1
    return lines


def snapshot(target: Path, *, untracked_all: bool, timeout: float) -> dict[str, Any] | None:
    """The target's status as a recorded snapshot, or ``None`` on any git failure or timeout."""
    status_args = [
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all" if untracked_all else "--untracked-files=normal",
    ]
    raw = _run(target, status_args, timeout)
    prefix_raw = _run(target, ["rev-parse", "--show-prefix"], timeout)
    if raw is None or prefix_raw is None:
        return None
    lines = parse_status_z(raw)
    if lines is None:
        return None
    prefix = prefix_raw.decode("utf-8", errors="replace").strip()
    return {"dirty_files": lines, "status_format": STATUS_FORMAT, "prefix": prefix}


def valid_snapshot(git: object) -> bool:
    """Whether ``git`` is a recorded snapshot in the current format."""
    if not isinstance(git, Mapping) or git.get("status_format") != STATUS_FORMAT:
        return False
    if not isinstance(git.get("prefix"), str):
        return False
    lines = git.get("dirty_files")
    return isinstance(lines, list) and all(
        isinstance(line, str) and len(line) >= 4 and line[2] == " " for line in lines
    )


def fingerprint(git: object) -> str | None:
    """Digest of a valid snapshot, its prefix included. ``None`` for any other value."""
    if not valid_snapshot(git):
        return None
    assert isinstance(git, Mapping)
    body = json.dumps({"prefix": git["prefix"], "dirty_files": sorted(git["dirty_files"])}, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _repo_path(path: str) -> str:
    return posixpath.normpath(path) if path else "."


def subject_dirty(subject_path: str, git: object) -> bool:
    """Whether the recorded snapshot marks the subject dirty, or cannot be read as clean.

    A directory entry (``?? newdir/``) marks every subject under it dirty. A root entry, the whole
    repository or target untracked, marks every subject dirty. An older or malformed snapshot marks
    every subject dirty.
    """
    if not valid_snapshot(git):
        return True
    assert isinstance(git, Mapping)
    prefix = git["prefix"]
    subjects = {
        _repo_path(f"{prefix}{variant.lstrip('/')}") for variant in (subject_path, subject_path.replace("\\", "/"))
    }
    for line in git["dirty_files"]:
        path = line[3:]
        candidate = _repo_path(path)
        if candidate in {".", ""}:
            return True
        if path.endswith("/"):
            if any(subject == candidate or subject.startswith(f"{candidate}/") for subject in subjects):
                return True
        elif candidate in subjects:
            return True
    return False
