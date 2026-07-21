"""Resolve executables for the native components ``brigade setup`` manages.

Every consumer of a managed engine (GraphTrail, MiseLedger, sessionfind) goes
through :func:`resolve` so one resolution order applies everywhere:

1. an explicit env override (``GRAPHTRAIL_BIN``, ``MISELEDGER_BIN``, ...);
2. the managed install recorded in ``installed.json`` under the user data root;
3. ``PATH``;
4. a legacy standalone location kept for machines mid-migration.

Config generators use the same resolver so emitted configs (Cursor MCP
servers, station wiring) carry the managed absolute path instead of a bare
name that depends on the spawning environment's PATH.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from pathlib import Path

from . import component_paths, component_state

# Engine name -> env var override honored before any other resolution step.
ENV_OVERRIDES = {
    "graphtrail": "GRAPHTRAIL_BIN",
    "graphtrail-mcp": "GRAPHTRAIL_MCP_BIN",
    "miseledger": "MISELEDGER_BIN",
    "sessionfind": "SESSIONFIND_BIN",
}

# Pre-consolidation install locations still honored as a last resort.
_LEGACY_RELATIVE = {
    "graphtrail": (Path(".cargo") / "bin" / "graphtrail",),
    "graphtrail-mcp": (Path(".cargo") / "bin" / "graphtrail-mcp",),
    "miseledger": (Path(".local") / "bin" / "miseledger",),
    "sessionfind": (Path(".local") / "bin" / "sessionfind",),
}


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def managed_path(name: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """Return the installed.json executable for ``name`` when present on disk."""
    try:
        data_root = component_paths.data_root(env=env)
    except ValueError:
        return None
    state = component_state.load_installed_state(Path(component_paths.installed_state_path(data_root)))
    if state is None:
        return None
    record = state.components.get(name)
    if record is None:
        return None
    executable = Path(record.executable)
    return str(executable) if _is_executable(executable) else None


def resolve(name: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """Resolve ``name`` to an executable path, or None when nowhere to be found."""
    environment = env if env is not None else os.environ
    override = environment.get(ENV_OVERRIDES.get(name, ""))
    if override:
        candidate = Path(override).expanduser()
        if _is_executable(candidate):
            return str(candidate)
        found = shutil.which(override)
        if found:
            return found
        return None
    managed = managed_path(name, env=env)
    if managed:
        return managed
    found = shutil.which(name)
    if found:
        return found
    for relative in _LEGACY_RELATIVE.get(name, ()):
        legacy = Path.home() / relative
        if _is_executable(legacy):
            return str(legacy)
    return None


def resolve_argv(argv: tuple[str, ...] | list[str]) -> list[str]:
    """Return argv with a managed engine name in position 0 resolved to a path.

    Unknown or already-absolute commands pass through unchanged, so this is safe
    to apply to any surface command before execution.
    """
    if not argv:
        return list(argv)
    head = argv[0]
    if head in ENV_OVERRIDES and not Path(head).is_absolute():
        resolved = resolve(head)
        if resolved:
            return [resolved, *argv[1:]]
    return list(argv)
