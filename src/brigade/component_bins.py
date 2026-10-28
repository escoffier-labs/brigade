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

from . import component_manifest, component_paths, component_state

# Engine name -> env var override honored before any other resolution step.
ENV_OVERRIDES = {
    "agent-notify": "AGENT_NOTIFY_BIN",
    "graphtrail": "GRAPHTRAIL_BIN",
    "graphtrail-mcp": "GRAPHTRAIL_MCP_BIN",
    "miseledger": "MISELEDGER_BIN",
    "sessionfind": "SESSIONFIND_BIN",
}

ENV_OVERRIDES.update(
    {
        "brigade-code-graph": "BRIGADE_CODE_GRAPH_BIN",
        "brigade-code-graph-mcp": "BRIGADE_CODE_GRAPH_MCP_BIN",
        "brigade-evidence": "BRIGADE_EVIDENCE_BIN",
    }
)

# Pre-consolidation install locations still honored as a last resort.
_LEGACY_RELATIVE = {
    "agent-notify": (Path("go") / "bin" / "agent-notify",),
    "graphtrail": (Path(".cargo") / "bin" / "graphtrail",),
    "graphtrail-mcp": (Path(".cargo") / "bin" / "graphtrail-mcp",),
    "miseledger": (Path(".local") / "bin" / "miseledger",),
    "sessionfind": (Path(".local") / "bin" / "sessionfind",),
}


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _expand_home(value: str, environment: Mapping[str, str]) -> Path:
    """Expand a leading ``~`` against the supplied environment's HOME."""
    home = environment.get("HOME")
    if home:
        if value == "~":
            return Path(home)
        if value.startswith("~/"):
            return Path(home) / value[2:]
    return Path(value).expanduser()


def managed_path(name: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """Return the installed.json executable for ``name`` when present on disk."""
    try:
        data_root = component_paths.data_root(env=env)
    except ValueError:
        return None
    state = component_state.load_installed_state(Path(component_paths.installed_state_path(data_root)))
    if state is None:
        return None
    key = component_manifest.wire_id(state.components, name)
    record = state.components.get(key) if key is not None else None
    if record is None:
        return None
    executable = Path(record.executable)
    return str(executable) if _is_executable(executable) else None


def resolve(name: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """Resolve ``name`` to an executable path, or None when nowhere to be found."""
    environment = env if env is not None else os.environ
    search_path = environment.get("PATH")
    names = component_manifest.family_names(name)
    overrides = [
        environment[key] for alias in names if (key := ENV_OVERRIDES.get(alias)) is not None and key in environment
    ]
    if overrides:
        if len(set(overrides)) != 1 or not overrides[0].strip():
            return None
        override = overrides[0]
        candidate = _expand_home(override, environment)
        if _is_executable(candidate):
            return str(candidate)
        return shutil.which(override, path=search_path)
    managed = managed_path(name, env=env)
    if managed:
        return managed
    for alias in names:
        found = shutil.which(alias, path=search_path)
        if found:
            return found
    home = environment.get("HOME")
    home_path = Path(home) if home else Path.home()
    for relative in (path for alias in names for path in _LEGACY_RELATIVE.get(alias, ())):
        legacy = home_path / relative
        if os.name == "nt" and legacy.suffix.lower() != ".exe":
            legacy = legacy.with_suffix(".exe")
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
        if any(ENV_OVERRIDES[alias] in os.environ for alias in component_manifest.family_names(head)):
            raise ValueError(f"invalid or conflicting explicit executable override for {head!r}")
    return list(argv)
