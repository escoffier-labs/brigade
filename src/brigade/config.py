"""Read/write .brigade/config.json - the per-target source of truth."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .selection import Selection


WORKSPACE_DIRNAME = ".brigade"
LEGACY_WORKSPACE_DIRNAMES = (".solo-mise",)
CONFIG_REL_PATH = f"{WORKSPACE_DIRNAME}/config.json"
SUPPORTED_VERSIONS = (1,)
DEFAULT_GRAPHTRAIL_DELTA_TIMEOUT_SECONDS = 10.0


@dataclass
class Config:
    version: int
    selection: Selection
    graphtrail_delta_timeout_seconds: float = DEFAULT_GRAPHTRAIL_DELTA_TIMEOUT_SECONDS


def validate_graphtrail_delta_timeout(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("graphtrail_delta_timeout_seconds must be a positive number")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("graphtrail_delta_timeout_seconds must be a positive number")
    return timeout


def resolve_graphtrail_delta_timeout(target: Path, cli_override: float | None = None) -> float:
    if cli_override is not None:
        try:
            return validate_graphtrail_delta_timeout(cli_override)
        except ValueError:
            raise ValueError("--graphtrail-timeout must be a positive number") from None
    cfg = load_config(target)
    if cfg is not None:
        return cfg.graphtrail_delta_timeout_seconds
    return DEFAULT_GRAPHTRAIL_DELTA_TIMEOUT_SECONDS


def config_path(target: Path) -> Path:
    return target / CONFIG_REL_PATH


def write_config(target: Path, cfg: Config) -> None:
    cfg.selection.validate()
    graphtrail_timeout = validate_graphtrail_delta_timeout(cfg.graphtrail_delta_timeout_seconds)
    path = config_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": cfg.version,
        "depth": cfg.selection.depth,
        "harnesses": list(cfg.selection.harnesses),
        "owner": cfg.selection.owner,
        "includes": list(cfg.selection.includes),
    }
    if graphtrail_timeout != DEFAULT_GRAPHTRAIL_DELTA_TIMEOUT_SECONDS:
        payload["graphtrail_delta_timeout_seconds"] = graphtrail_timeout
    path.write_text(json.dumps(payload, indent=2) + "\n")


def load_config(target: Path) -> Optional[Config]:
    path = config_path(target)
    if not path.is_file():
        for legacy in LEGACY_WORKSPACE_DIRNAMES:
            legacy_path = target / legacy / "config.json"
            if legacy_path.is_file():
                path = legacy_path
                break
        else:
            return None
    data = json.loads(path.read_text())
    version = data.get("version")
    if version not in SUPPORTED_VERSIONS:
        raise ValueError(f"unsupported config version: {version!r} (supported: {SUPPORTED_VERSIONS})")
    sel = Selection(
        depth=data.get("depth", ""),
        harnesses=list(data.get("harnesses", [])),
        owner=data.get("owner", "this-repo"),
        includes=list(data.get("includes", [])),
    )
    sel.validate()
    timeout_raw = data.get("graphtrail_delta_timeout_seconds", DEFAULT_GRAPHTRAIL_DELTA_TIMEOUT_SECONDS)
    timeout = validate_graphtrail_delta_timeout(timeout_raw)
    return Config(version=version, selection=sel, graphtrail_delta_timeout_seconds=timeout)
