"""Shared discovery for Brigade-wired project roots.

A directory is a project work root only when `.brigade/config.json` exists.
`~/.brigade` (the user-level aboyeur roster fallback for `brigade run`) has no
config.json and must never be treated as a project target by harness hooks.
"""

from __future__ import annotations

import json
from pathlib import Path

from .config import load_config


def resolve_wired_target(cwd: object, *, harness: str | None = "claude") -> Path | None:
    """Walk from ``cwd`` upward looking for a Brigade-wired project.

    A candidate qualifies only when ``.brigade/config.json`` is present and
    loads. When ``harness`` is set, that harness must also appear in the
    project's selected harnesses. When a config is found but the harness is
    absent (or the config is unreadable), the walk stops — nested non-matching
    projects do not fall through to a parent.
    """
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    try:
        current = Path(cwd).expanduser().resolve()
    except OSError:
        return None
    if not current.is_dir():
        current = current.parent
    required = harness.strip().lower() if isinstance(harness, str) and harness.strip() else None
    for candidate in (current, *current.parents):
        if not (candidate / ".brigade" / "config.json").is_file():
            continue
        try:
            config = load_config(candidate)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if config is None:
            return None
        if required is None:
            return candidate
        selected = {name.lower() for name in config.selection.harnesses}
        if required in selected:
            return candidate
        return None
    return None
