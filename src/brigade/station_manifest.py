"""Load external station manifests used by `brigade add <path>`."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCHEMA = "brigade.station.v1"


@dataclass(frozen=True)
class ManifestSurface:
    kind: str
    command: tuple[str, ...]
    read_only: bool = True
    timeout_seconds: float | None = None
    max_chars: int | None = None


@dataclass(frozen=True)
class ManifestTool:
    name: str
    command: str
    summary: str
    install: tuple[str, ...] = ()
    surfaces: tuple[ManifestSurface, ...] = ()


@dataclass(frozen=True)
class StationManifest:
    path: Path
    name: str
    station: str
    summary: str
    tools: tuple[ManifestTool, ...]


def manifest_path(ref: str, *, cwd: Path | None = None) -> Path | None:
    base = cwd or Path.cwd()
    path = Path(ref).expanduser()
    if not path.is_absolute():
        path = base / path
    if path.is_dir():
        candidate = path / "station.json"
    else:
        candidate = path
    return candidate if candidate.is_file() and candidate.name == "station.json" else None


def load(ref: str, *, cwd: Path | None = None) -> StationManifest:
    path = manifest_path(ref, cwd=cwd)
    if path is None:
        raise ValueError(f"station manifest not found: {ref}")
    try:
        raw = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"station manifest is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError("station manifest must be a JSON object")
    schema = raw.get("schema")
    if schema != SCHEMA:
        raise ValueError(f"station manifest schema must be {SCHEMA!r}")
    name = _required_str(raw, "name")
    station = _required_str(raw, "station")
    summary = _required_str(raw, "summary")
    tools_raw = raw.get("tools")
    if not isinstance(tools_raw, list) or not tools_raw:
        raise ValueError("station manifest requires at least one tool")
    tools = tuple(_parse_tool(item, index) for index, item in enumerate(tools_raw))
    return StationManifest(path=path, name=name, station=station, summary=summary, tools=tools)


def _required_str(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"station manifest field {key!r} must be a non-empty string")
    return value.strip()


def _optional_str(data: dict[str, Any], key: str, default: str = "") -> str:
    value = data.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"station manifest field {key!r} must be a string")
    return value.strip()


def _string_tuple(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"station manifest field {field!r} must be a non-empty string array")
    return tuple(value)


def _parse_surface(raw: object, index: int) -> ManifestSurface:
    if not isinstance(raw, dict):
        raise ValueError(f"station manifest surface {index} must be an object")
    kind = _required_str(raw, "kind")
    command = _string_tuple(raw.get("command"), "surface.command")
    read_only = raw.get("read_only", True)
    if not isinstance(read_only, bool):
        raise ValueError("station manifest field 'surface.read_only' must be a boolean")
    timeout = raw.get("timeout_seconds")
    if timeout is not None and (not isinstance(timeout, int | float) or isinstance(timeout, bool)):
        raise ValueError("station manifest field 'surface.timeout_seconds' must be a number")
    max_chars = raw.get("max_chars")
    if max_chars is not None and (not isinstance(max_chars, int) or isinstance(max_chars, bool)):
        raise ValueError("station manifest field 'surface.max_chars' must be an integer")
    return ManifestSurface(
        kind=kind,
        command=command,
        read_only=read_only,
        timeout_seconds=float(timeout) if timeout is not None else None,
        max_chars=max_chars,
    )


def _parse_tool(raw: object, index: int) -> ManifestTool:
    if not isinstance(raw, dict):
        raise ValueError(f"station manifest tool {index} must be an object")
    name = _required_str(raw, "name")
    command = _required_str(raw, "command")
    summary = _optional_str(raw, "summary")
    install = _string_tuple(raw.get("install"), "tool.install")
    surfaces_raw = raw.get("surfaces", [])
    if not isinstance(surfaces_raw, list):
        raise ValueError("station manifest field 'tool.surfaces' must be an array")
    surfaces = tuple(_parse_surface(item, surface_index) for surface_index, item in enumerate(surfaces_raw))
    return ManifestTool(name=name, command=command, summary=summary, install=install, surfaces=surfaces)
