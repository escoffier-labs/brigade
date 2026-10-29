"""Resolve a Codex session's own cwd from its bounded metadata header."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from .work_cmd.session_resume import _header, _metadata

MAX_ENTRIES = 4096
LOOKUP_SECONDS = 0.25


def codex_session_root(session_id: str) -> Path | None:
    """Read only this session's header, never conversation events.

    The brief's --target may name a checkout that the session only inspected.
    Rollout filenames include the native session ID. Bound directory traversal
    and refuse malformed headers instead of guessing ownership.
    """
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", session_id):
        return None
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    pending = [home / "sessions"]
    deadline = time.monotonic() + LOOKUP_SECONDS
    entries = 0
    try:
        while pending and time.monotonic() < deadline:
            pending.sort()
            directory = pending.pop()
            with os.scandir(directory) as children:
                for child in children:
                    entries += 1
                    if entries > MAX_ENTRIES or time.monotonic() >= deadline:
                        return None
                    if child.is_dir(follow_symlinks=False):
                        pending.append(Path(child.path))
                    elif child.name.endswith(f"-{session_id}.jsonl") and child.is_file(follow_symlinks=False):
                        header = _header(Path(child.path))
                        parsed = _metadata("codex", header) if header is not None else None
                        if parsed is not None and parsed[0] == session_id and Path(parsed[1]).is_absolute():
                            return Path(parsed[1]).resolve()
    except (OSError, ValueError):
        return None
    return None


def session_repository_root(cwd: Path, *, harness: str) -> Path:
    """Publish the session's own checkout, not an arbitrary wired ancestor.

    The nearest Git root at or below the wired workspace wins; a wired project
    inside a larger repository keeps its own boundary.
    """
    from .wiring import resolve_wired_target

    cwd = cwd.resolve()
    wired = resolve_wired_target(str(cwd), harness=harness)
    for candidate in (cwd, *cwd.parents):
        if (candidate / ".git").exists():
            if wired is None or candidate == wired or wired in candidate.parents:
                return candidate
            break
        if candidate == wired:
            break
    return wired or cwd
