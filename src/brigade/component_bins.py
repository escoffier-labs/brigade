"""Resolve executables for the native components ``brigade setup`` manages.

Every consumer of a managed engine (code graph, evidence, sessionfind) goes
through :func:`resolve` so one resolution order applies everywhere:

1. an explicit env override (``BRIGADE_CODE_GRAPH_BIN``, ``BRIGADE_EVIDENCE_BIN``,
   ...; the legacy variable names are read too and conflicting values refused);
2. the managed install recorded in ``installed.json`` under the user data root;
3. ``PATH``, where each directory is searched for the canonical executable name
   before the legacy one (docs/phase-engine-rename.md, R4);
4. a legacy standalone location kept for machines mid-migration.

Legacy environment overrides print one stderr deprecation notice per spelling
per process. Canonical resolution stays silent.

Config generators use the same resolver so emitted configs (Cursor MCP
servers, station wiring) carry the managed absolute path instead of a bare
name that depends on the spawning environment's PATH.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path

from . import component_manifest, component_paths, component_state, engine_deprecation, localio

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


def _managed_record(
    name: str, state_path: Path
) -> tuple[component_state.InstalledState, component_state.InstalledComponentRecord] | None:
    state = component_state.load_installed_state(state_path)
    if state is None:
        return None
    key = component_manifest.wire_id(state.components, name)
    record = state.components.get(key) if key is not None else None
    return (state, record) if record is not None else None


def managed_path(name: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """Return the installed.json executable for ``name`` when present on disk."""
    try:
        state_path = _installed_state_path(env)
    except ValueError:
        return None
    entry = _managed_record(name, state_path)
    if entry is None:
        return None
    _, record = entry
    executable = Path(record.executable)
    return str(executable) if _is_executable(executable) else None


def _override_values(names: tuple[str, ...], environment: Mapping[str, str]) -> list[str]:
    return [
        environment[key]
        for alias in names
        if (key := ENV_OVERRIDES.get(alias)) is not None
        and key in environment
        and (len(names) > 1 or bool(environment[key]))
    ]


def has_explicit_override(name: str, *, env: Mapping[str, str] | None = None) -> bool:
    environment = os.environ if env is None else env
    return any(value.strip() for value in _override_values(component_manifest.family_names(name), environment))


def _installed_state_path(env: Mapping[str, str] | None) -> Path:
    return Path(component_paths.installed_state_path(component_paths.data_root(env=env)))


def _file_identity(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_dev, stat.st_ino


# Release installation manifests use the release tag and optional full Git SHA.
_RELEASE_REVISION = re.compile(r"v(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}(?:\+[0-9a-f]{40})?")


@lru_cache(maxsize=32)
def _verified_managed_release(
    name: str,
    binary: Path,
    binary_identity: tuple[int, ...],
    state_path: Path,
    state_identity: tuple[int, ...],
) -> str:
    entry = _managed_record(name, state_path)
    if entry is None:
        raise ValueError("no managed component record")
    state, record = entry
    if not _RELEASE_REVISION.fullmatch(state.manifest_revision):
        raise ValueError("not a release manifest revision")
    if binary != Path(record.executable) or binary_identity[0] != record.byte_size:
        raise ValueError("binary does not match the managed component record")
    if localio.file_sha256(binary) != record.sha256:
        raise ValueError("binary digest does not match the managed component record")
    if _file_identity(binary) != binary_identity or _file_identity(state_path) != state_identity:
        raise ValueError("managed component changed during verification")
    return state.manifest_revision


def managed_release(name: str, binary: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """Return the recorded release only for a matching managed executable."""
    if has_explicit_override(name, env=env):
        return None
    executable = Path(binary)
    if not executable.is_absolute():
        return None
    try:
        state_path = _installed_state_path(env)
        if not _is_executable(executable):
            return None
        return _verified_managed_release(
            name, executable, _file_identity(executable), state_path, _file_identity(state_path)
        )
    except (OSError, ValueError, RecursionError):
        return None


def resolve(name: str, *, env: Mapping[str, str] | None = None) -> str | None:
    """Resolve ``name`` to an executable path, or None when nowhere to be found."""
    environment = env if env is not None else os.environ
    search_path = environment.get("PATH")
    names = component_manifest.family_names(name)
    for alias in names[1:]:
        engine_deprecation.environment(ENV_OVERRIDES[alias], ENV_OVERRIDES[names[0]], environment)
    overrides = _override_values(names, environment)
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
    if len(names) == 1:
        found = shutil.which(name, path=search_path)
        if found:
            return found
    else:
        # ``family_names`` lists the canonical name first, so a directory that
        # holds both executables yields the canonical one whatever was requested.
        path_value = search_path if search_path is not None else os.environ.get("PATH", os.defpath)
        for directory in path_value.split(os.pathsep) if path_value else ():
            for alias in names:
                found = shutil.which(alias, path=directory or os.curdir)
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

    Unknown or already-absolute commands pass through unchanged. Invalid explicit
    overrides raise ValueError so callers cannot silently fall back to PATH.
    """
    if not argv:
        return list(argv)
    head = argv[0]
    if head in ENV_OVERRIDES and not Path(head).is_absolute():
        resolved = resolve(head)
        if resolved:
            return [resolved, *argv[1:]]
        if _override_values(component_manifest.family_names(head), os.environ):
            raise ValueError(f"invalid or conflicting explicit executable override for {head!r}")
    return list(argv)


def canonical_command(command: str | None) -> str | None:
    """Project bare engine commands to a resolved engine path, preserving anything unresolved.

    When Brigade cannot resolve the engine, the original command is kept: the
    server may find it through its own PATH, and a bare canonical name could
    point at nothing there.
    """
    if command not in component_manifest.ACCEPTED_COMPONENT_IDS:
        return command
    assert command is not None
    canonical = component_manifest.family_id(command)
    if canonical not in component_manifest.COMPONENT_FAMILIES.values():
        return command
    # resolve_argv keeps the explicit-override validation; an unresolved engine keeps the original command.
    resolved = resolve_argv([canonical])[0]
    return resolved if resolved != canonical else command
