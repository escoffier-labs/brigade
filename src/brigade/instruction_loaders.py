"""Read-only caps that harness loaders put on always-loaded instruction files.

OpenClaw's caps come from its own configuration file, per agent, and Codex's
from `project_doc_max_bytes`. Nothing here writes, starts a subprocess, or reads
more than 1 MiB of configuration. A character cap is used as the same number of
bytes, which is never looser than the harness.
"""

from __future__ import annotations

import json
import math
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import harness_profiles as hp
from .codex_user_config import read_user_base_cap

CONFIG_MAX_BYTES = 1024 * 1024

LABEL_AGENT_MAX = "loader:openclaw:agent-max-chars"
LABEL_DEFAULTS_MAX = "loader:openclaw:defaults-max-chars"
LABEL_DEFAULT_MAX = "loader:openclaw:default-max-chars"
LABEL_AGENT_TOTAL = "loader:openclaw:agent-total-max-chars"
LABEL_DEFAULTS_TOTAL = "loader:openclaw:defaults-total-max-chars"
LABEL_DEFAULT_TOTAL = "loader:openclaw:default-total-max-chars"
LABEL_USER_MD = "loader:openclaw:user-md-fixed-cap"
LABEL_CODEX = "loader:codex:project-doc-max-bytes"

UNKNOWN_AGENT = "openclaw:unknown-agent"
CODEX_LOADER = "codex:project"


@dataclass(frozen=True)
class LoaderCap:
    loader: str  # "openclaw:main", "openclaw:unknown-agent", "codex:project"
    label: str  # "loader:openclaw:agent-max-chars" and so on
    cap: int | None  # None when unreadable
    reason: str | None  # set when cap is None


@dataclass(frozen=True)
class OpenClawLoaders:
    per_file: tuple[LoaderCap, ...]  # one per matched agent, sorted by loader
    totals: tuple[LoaderCap, ...]  # one per matched agent, sorted by loader
    reason: str | None  # whole-config reason, or None


def _expand(value: str, home: Path) -> Path:
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    return Path(value)


def openclaw_state_dir(home: Path, environ: Mapping[str, str]) -> Path:
    custom = environ.get(hp.OPENCLAW_STATE_DIR_ENV, "")
    return _expand(custom, home) if custom else home / ".openclaw"


def openclaw_config_path(home: Path, environ: Mapping[str, str]) -> Path:
    custom = environ.get(hp.OPENCLAW_CONFIG_ENV, "")
    if custom:
        return _expand(custom, home)
    return openclaw_state_dir(home, environ) / hp.OPENCLAW_CONFIG_NAME


def openclaw_install_state_path(home: Path) -> Path:
    # The openclaw user profile keeps its install state here, never under
    # OPENCLAW_STATE_DIR.
    return home / ".openclaw" / "brigade" / "install-state.json"


def _reject_constant(name: str) -> Any:
    raise ValueError(f"not strict JSON: {name}")


def _read_config(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None, "config-missing"
    except OSError:
        return None, "config-unreadable"
    if not stat.S_ISREG(info.st_mode):
        return None, "config-unreadable"
    if info.st_size > CONFIG_MAX_BYTES:
        return None, "config-too-large"
    try:
        with path.open("rb") as handle:
            raw = handle.read(CONFIG_MAX_BYTES + 1)
    except OSError:
        return None, "config-unreadable"
    if len(raw) > CONFIG_MAX_BYTES:
        return None, "config-too-large"
    try:
        data = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        return None, "config-not-strict-json"
    if not isinstance(data, dict):
        return None, "config-key-invalid"
    return data, None


def _cap_value(value: object) -> int | None:
    """A cap counts when it is a finite number of at least 1, floored."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    if isinstance(value, float) and math.isfinite(value):
        floored = math.floor(value)
        return floored if floored >= 1 else None
    return None


def _agents(config: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, Any], str | None]:
    """Agent entries by id, the defaults table, and a whole-config reason."""
    section = config.get("agents", {})
    if not isinstance(section, dict):
        return {}, {}, "config-key-invalid"
    defaults = section.get("defaults", {})
    entries = section.get("entries", {})
    listed = section.get("list", [])
    if not isinstance(defaults, dict) or not isinstance(entries, dict) or not isinstance(listed, list):
        return {}, {}, "config-key-invalid"
    agents: dict[str, dict[str, Any]] = {}
    for agent_id, value in entries.items():
        if isinstance(value, dict):
            agents[str(agent_id)] = dict(value)
    for item in listed:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            agents[item["id"]] = {**agents.get(item["id"], {}), **item}
    if not agents:
        agents["main"] = {}
    return agents, defaults, None


def _workspace_candidates(
    agent_id: str,
    agent: dict[str, Any],
    defaults: dict[str, Any],
    *,
    home: Path,
    state: Path,
) -> list[Path]:
    """Real paths an agent's workspace may resolve to. A bad value matches nothing."""

    def absolute(value: object) -> Path | None:
        if not isinstance(value, str) or not value:
            return None
        path = _expand(value, home)
        return path if path.is_absolute() else None

    if "workspace" in agent:
        own = absolute(agent["workspace"])
        return [own] if own is not None else []
    default = absolute(defaults.get("workspace")) if "workspace" in defaults else None
    if "workspace" in defaults:
        return [default, default / agent_id] if default is not None else []
    return [state / f"workspace-{agent_id}", state / "workspace"]


def _resolved(path: Path) -> Path:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def _level_cap(
    agent: dict[str, Any],
    defaults: dict[str, Any],
    key: str,
    labels: tuple[str, str, str],
    fallback: int,
    loader: str,
) -> LoaderCap:
    agent_label, defaults_label, default_label = labels
    for table, label in ((agent, agent_label), (defaults, defaults_label)):
        if key in table:
            value = _cap_value(table[key])
            if value is None:
                return LoaderCap(loader, label, None, "config-key-invalid")
            return LoaderCap(loader, label, value, None)
    return LoaderCap(loader, default_label, fallback, None)


def _install_state_matches(target: Path, home: Path) -> bool:
    try:
        raw = openclaw_install_state_path(home).read_bytes()[: CONFIG_MAX_BYTES + 1]
        if len(raw) > CONFIG_MAX_BYTES:
            return False
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return False
    workspace = data.get("workspace") if isinstance(data, dict) else None
    if not isinstance(workspace, str) or not workspace:
        return False
    path = _expand(workspace, home)
    return path.is_absolute() and _resolved(path) == _resolved(target)


def openclaw_loaders(target: Path, *, home: Path, environ: Mapping[str, str]) -> OpenClawLoaders:
    config, reason = _read_config(openclaw_config_path(home, environ))
    if config is not None:
        agents, defaults, reason = _agents(config)
        if reason is None:
            return _matched(target, agents, defaults, home=home, state=openclaw_state_dir(home, environ))
    # Whole-config failure: the install-state file still says whether OpenClaw
    # loads this workspace, so say so without inventing a cap.
    if reason is not None and _install_state_matches(target, home):
        unknown = LoaderCap(UNKNOWN_AGENT, LABEL_AGENT_MAX, None, reason)
        unknown_total = LoaderCap(UNKNOWN_AGENT, LABEL_AGENT_TOTAL, None, reason)
        return OpenClawLoaders((unknown,), (unknown_total,), reason)
    return OpenClawLoaders((), (), reason)


def _matched(
    target: Path,
    agents: dict[str, dict[str, Any]],
    defaults: dict[str, Any],
    *,
    home: Path,
    state: Path,
) -> OpenClawLoaders:
    real_target = _resolved(target)
    per_file: list[LoaderCap] = []
    totals: list[LoaderCap] = []
    for agent_id in sorted(agents):
        agent = agents[agent_id]
        candidates = _workspace_candidates(agent_id, agent, defaults, home=home, state=state)
        if real_target not in {_resolved(path) for path in candidates}:
            continue
        loader = f"openclaw:{agent_id}"
        per_file.append(
            _level_cap(
                agent,
                defaults,
                hp.OPENCLAW_PER_FILE_KEY,
                (LABEL_AGENT_MAX, LABEL_DEFAULTS_MAX, LABEL_DEFAULT_MAX),
                hp.OPENCLAW_BOOTSTRAP_MAX_CHARS,
                loader,
            )
        )
        totals.append(
            _level_cap(
                agent,
                defaults,
                hp.OPENCLAW_TOTAL_KEY,
                (LABEL_AGENT_TOTAL, LABEL_DEFAULTS_TOTAL, LABEL_DEFAULT_TOTAL),
                hp.OPENCLAW_BOOTSTRAP_TOTAL_MAX_CHARS,
                loader,
            )
        )
    return OpenClawLoaders(tuple(per_file), tuple(totals), None)


def user_md_caps(loaders: OpenClawLoaders) -> tuple[LoaderCap, ...]:
    """The fixed `USER.md` cap, once per OpenClaw loader present."""
    return tuple(
        LoaderCap(cap.loader, LABEL_USER_MD, hp.OPENCLAW_USER_BOOTSTRAP_MAX_CHARS, None) for cap in loaders.per_file
    )


def codex_project_cap(*, home: Path, environ: Mapping[str, str]) -> LoaderCap:
    base = read_user_base_cap(home=home, environ=environ)
    if base.value is None:
        return LoaderCap(CODEX_LOADER, LABEL_CODEX, None, base.reason)
    if base.value == 0:
        # A cap of 0 would make every limit 0.
        return LoaderCap(CODEX_LOADER, LABEL_CODEX, None, "config-key-invalid")
    return LoaderCap(CODEX_LOADER, LABEL_CODEX, base.value, None)
