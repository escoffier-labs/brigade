"""brigade release version-sync: reconcile in-tree version stamps against one source."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from . import toml_compat

MANIFEST_REL = Path(".brigade") / "version-sync.toml"


@dataclass(frozen=True)
class Source:
    file: str
    key: str | None
    regex: str | None


@dataclass(frozen=True)
class Location:
    path: str | None
    glob: str | None
    pattern: str
    guard: str | None
    required: bool


@dataclass(frozen=True)
class Manifest:
    source: Source
    locations: tuple[Location, ...]


def _as_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _require_one_group(pattern: str, field: str) -> None:
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise ValueError(f"{field} is not a valid regex: {exc}") from exc
    if compiled.groups != 1:
        raise ValueError(f"{field} must have exactly one capture group, found {compiled.groups}")


def _parse_location(item: object, index: int) -> Location:
    where = f"location[{index}]"
    if not isinstance(item, dict):
        raise ValueError(f"{where} must be a table")
    path = item.get("path")
    glob = item.get("glob")
    if (path is None) == (glob is None):
        raise ValueError(f"{where} must set exactly one of `path` or `glob`")
    pattern = _as_str(item.get("pattern"), f"{where}.pattern")
    _require_one_group(pattern, f"{where}.pattern")
    guard = item.get("guard")
    if guard is not None:
        guard = _as_str(guard, f"{where}.guard")
    if path is not None and guard is not None:
        raise ValueError(f"{where}.guard is only valid with `glob`")
    required = item.get("required", True)
    if not isinstance(required, bool):
        raise ValueError(f"{where}.required must be a boolean")
    return Location(
        path=_as_str(path, f"{where}.path") if path is not None else None,
        glob=_as_str(glob, f"{where}.glob") if glob is not None else None,
        pattern=pattern,
        guard=guard,
        required=required,
    )


def load_manifest(target: Path) -> Manifest:
    manifest_path = target / MANIFEST_REL
    if not manifest_path.is_file():
        raise FileNotFoundError(manifest_path)
    data = toml_compat.loads(manifest_path.read_text())
    src = data.get("source")
    if not isinstance(src, dict):
        raise ValueError("[source] table is required")
    file = _as_str(src.get("file"), "source.file")
    key = src.get("key")
    regex = src.get("regex")
    if (key is None) == (regex is None):
        raise ValueError("source must set exactly one of `key` or `regex`")
    if key is not None:
        key = _as_str(key, "source.key")
    if regex is not None:
        regex = _as_str(regex, "source.regex")
        _require_one_group(regex, "source.regex")
    raw = data.get("location")
    if not isinstance(raw, list) or not raw:
        raise ValueError("at least one [[location]] is required")
    locations = tuple(_parse_location(item, i) for i, item in enumerate(raw))
    return Manifest(source=Source(file, key, regex), locations=locations)
