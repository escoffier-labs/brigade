"""Read/write .brigade/config.json - the per-target source of truth."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .selection import Selection


WORKSPACE_DIRNAME = ".brigade"
LEGACY_WORKSPACE_DIRNAMES = (".solo-mise",)
CONFIG_REL_PATH = f"{WORKSPACE_DIRNAME}/config.json"
SUPPORTED_VERSIONS = (1,)


@dataclass
class Config:
    version: int
    selection: Selection


def config_path(target: Path) -> Path:
    return target / CONFIG_REL_PATH


def write_config(target: Path, cfg: Config) -> None:
    cfg.selection.validate()
    path = config_path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": cfg.version,
        "depth": cfg.selection.depth,
        "harnesses": list(cfg.selection.harnesses),
        "owner": cfg.selection.owner,
        "includes": list(cfg.selection.includes),
    }
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
    return Config(version=version, selection=sel)
