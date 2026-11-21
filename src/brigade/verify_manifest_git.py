"""Read workspace verify manifests from their trusted Git base."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import runguard


@dataclass(frozen=True)
class BaseManifestPayload:
    payload: dict[str, Any]
    commit: str
    payload_sha256: str
    worktree_modified: bool


def _base_commit(target: Path) -> str | None:
    upstream = runguard._git(target, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    reference = upstream.stdout.strip() if upstream.code == 0 else ""
    if not reference:
        fallback = runguard._git(target, "rev-parse", "--verify", "origin/main^{commit}")
        reference = "origin/main" if fallback.code == 0 else "HEAD"
    base = runguard._git(target, "merge-base", "HEAD", reference)
    return base.stdout.strip() if base.code == 0 and base.stdout.strip() else None


def load_base_manifest(target: Path, path: Path) -> BaseManifestPayload | None:
    """Return the manifest stored at the default-branch merge base."""
    try:
        relative = path.resolve().relative_to(target.resolve()).as_posix()
    except ValueError:
        return None
    prefix_result = runguard._git(target, "rev-parse", "--show-prefix")
    if prefix_result.code != 0:
        return None
    repository_path = f"{prefix_result.stdout.strip()}{relative}"
    commit = _base_commit(target)
    if commit is None:
        return None
    shown = runguard._git(target, "show", f"{commit}:{repository_path}")
    if shown.code != 0:
        return None
    raw = shown.stdout.encode("utf-8")
    try:
        payload = json.loads(shown.stdout)
    except (json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    try:
        worktree_modified = path.read_bytes() != raw
    except OSError:
        worktree_modified = True
    return BaseManifestPayload(
        payload=payload,
        commit=commit,
        payload_sha256=f"sha256:{hashlib.sha256(raw).hexdigest()}",
        worktree_modified=worktree_modified,
    )
