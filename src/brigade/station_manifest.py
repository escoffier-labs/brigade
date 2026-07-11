"""Load external station manifests used by `brigade add <path>`."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import __version__ as _BRIGADE_VERSION


SCHEMA = "brigade.station.v1"
LIFECYCLES = ("active", "embedded", "deprecated", "historical")
TOOL_KINDS = ("executable", "skill-roster")
ALLOWED_PLACEHOLDERS = frozenset({"task", "query"})
_PLACEHOLDER_RE = re.compile(r"<([^<>]+)>")
_STRICT_SEMVER_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_LEADING_SEMVER_RE = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")


@dataclass(frozen=True)
class RequiresBrigade:
    min_version: str | None = None
    max_version_exclusive: str | None = None


@dataclass(frozen=True)
class Compatibility:
    status: str
    compatible: bool
    current_version: str
    detail: str = ""


@dataclass(frozen=True)
class ManifestSurface:
    kind: str
    command: tuple[str, ...]
    read_only: bool = True
    timeout_seconds: float | None = None
    max_chars: int | None = None
    probe: tuple[str, ...] = ()
    probe_contains: tuple[str, ...] = ()
    placeholders: tuple[str, ...] = ()


@dataclass(frozen=True)
class ManifestTool:
    name: str
    command: str
    summary: str
    install: tuple[str, ...] = ()
    surfaces: tuple[ManifestSurface, ...] = ()
    kind: str = "executable"
    produces: tuple[str, ...] = ()
    consumes: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True)
class StationManifest:
    path: Path
    name: str
    station: str
    summary: str
    tools: tuple[ManifestTool, ...]
    lifecycle: str = "active"
    owner: str = ""
    contract_version: int = 1
    requires_brigade: RequiresBrigade = field(default_factory=RequiresBrigade)
    compatibility: Compatibility = field(
        default_factory=lambda: Compatibility(
            status="compatible",
            compatible=True,
            current_version=_BRIGADE_VERSION,
        )
    )


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
        source = path.read_text()
    except OSError as exc:
        raise ValueError("station manifest could not be read") from exc
    try:
        raw = json.loads(source)
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
    contract_version = _contract_version(raw.get("contract_version", 1))
    requires_brigade = _requires_brigade(raw.get("requires_brigade", {}))
    compatibility = _compatibility(requires_brigade)
    lifecycle = _optional_str(raw, "lifecycle", "active")
    if lifecycle not in LIFECYCLES:
        allowed = ", ".join(LIFECYCLES)
        raise ValueError(f"station manifest field 'lifecycle' must be one of: {allowed}")
    owner = _optional_str(raw, "owner")
    if lifecycle != "active" and not owner:
        raise ValueError("non-active station manifest field 'owner' must be a non-empty string")
    tools_raw = raw.get("tools")
    if not isinstance(tools_raw, list):
        raise ValueError("station manifest field 'tools' must be an array")
    if lifecycle == "active" and not tools_raw:
        raise ValueError("active station manifest requires at least one tool")
    tools = tuple(_parse_tool(item, index) for index, item in enumerate(tools_raw))
    tool_names: set[str] = set()
    for tool in tools:
        if tool.name in tool_names:
            raise ValueError(f"station manifest contains duplicate tool name: {tool.name}")
        tool_names.add(tool.name)
    return StationManifest(
        path=path,
        name=name,
        station=station,
        summary=summary,
        tools=tools,
        lifecycle=lifecycle,
        owner=owner,
        contract_version=contract_version,
        requires_brigade=requires_brigade,
        compatibility=compatibility,
    )


def _parse_semver(value: str, field: str) -> tuple[int, int, int]:
    match = _STRICT_SEMVER_RE.fullmatch(value)
    if not match:
        raise ValueError(f"station manifest field {field!r} must be strict numeric semver MAJOR.MINOR.PATCH")
    major, minor, patch = match.groups()
    return (int(major), int(minor), int(patch))


def _current_semver() -> tuple[int, int, int]:
    match = _LEADING_SEMVER_RE.match(_BRIGADE_VERSION)
    if match is None:
        raise ValueError("Brigade version must begin with numeric MAJOR.MINOR.PATCH")
    major, minor, patch = match.groups()
    return (int(major), int(minor), int(patch))


def _contract_version(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("station manifest field 'contract_version' must be a positive integer")
    return value


def _requires_brigade(value: object) -> RequiresBrigade:
    if value is None:
        return RequiresBrigade()
    if not isinstance(value, dict):
        raise ValueError("station manifest field 'requires_brigade' must be an object")
    allowed = {"min_version", "max_version_exclusive"}
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ValueError("station manifest field 'requires_brigade' contains unsupported keys: " + ", ".join(unknown))
    min_version = value.get("min_version")
    max_version = value.get("max_version_exclusive")
    if min_version is not None:
        if not isinstance(min_version, str):
            raise ValueError("station manifest field 'requires_brigade.min_version' must be a string")
        _parse_semver(min_version, "requires_brigade.min_version")
    if max_version is not None:
        if not isinstance(max_version, str):
            raise ValueError("station manifest field 'requires_brigade.max_version_exclusive' must be a string")
        _parse_semver(max_version, "requires_brigade.max_version_exclusive")
    if min_version is not None and max_version is not None:
        if _parse_semver(min_version, "requires_brigade.min_version") >= _parse_semver(
            max_version, "requires_brigade.max_version_exclusive"
        ):
            raise ValueError(
                "station manifest field 'requires_brigade.min_version' must be less than "
                "'requires_brigade.max_version_exclusive'"
            )
    return RequiresBrigade(min_version=min_version, max_version_exclusive=max_version)


def _compatibility(requirement: RequiresBrigade) -> Compatibility:
    current = _current_semver()
    current_label = _BRIGADE_VERSION
    if requirement.min_version is not None and current < _parse_semver(
        requirement.min_version, "requires_brigade.min_version"
    ):
        return Compatibility(
            status="incompatible",
            compatible=False,
            current_version=current_label,
            detail=f"requires Brigade >= {requirement.min_version}; current is {current_label}",
        )
    if requirement.max_version_exclusive is not None and current >= _parse_semver(
        requirement.max_version_exclusive,
        "requires_brigade.max_version_exclusive",
    ):
        return Compatibility(
            status="incompatible",
            compatible=False,
            current_version=current_label,
            detail=f"requires Brigade < {requirement.max_version_exclusive}; current is {current_label}",
        )
    return Compatibility(status="compatible", compatible=True, current_version=current_label)


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


def _parse_placeholders(command: tuple[str, ...], field: str, *, allow_templates: bool) -> tuple[str, ...]:
    placeholders: list[str] = []
    for argument in command:
        for match in _PLACEHOLDER_RE.finditer(argument):
            placeholder = match.group(1)
            if not allow_templates:
                raise ValueError(f"station manifest field {field!r} may not contain placeholders")
            if placeholder not in ALLOWED_PLACEHOLDERS:
                allowed = ", ".join(f"<{item}>" for item in sorted(ALLOWED_PLACEHOLDERS))
                raise ValueError(
                    f"station manifest field {field!r} contains unsupported placeholder "
                    f"<{placeholder}>; allowed: {allowed}"
                )
            if placeholder not in placeholders:
                placeholders.append(placeholder)
    return tuple(placeholders)


def _parse_surface(raw: object, index: int) -> ManifestSurface:
    if not isinstance(raw, dict):
        raise ValueError(f"station manifest surface {index} must be an object")
    kind = _required_str(raw, "kind")
    command = _string_tuple(raw.get("command"), "surface.command")
    placeholders = _parse_placeholders(command, "surface.command", allow_templates=True)
    probe = _string_tuple(raw.get("probe"), "surface.probe")
    _parse_placeholders(probe, "surface.probe", allow_templates=False)
    probe_contains = _string_tuple(raw.get("probe_contains"), "surface.probe_contains")
    read_only = raw.get("read_only", True)
    if not isinstance(read_only, bool):
        raise ValueError("station manifest field 'surface.read_only' must be a boolean")
    timeout = raw.get("timeout_seconds")
    if timeout is not None:
        if not isinstance(timeout, int | float) or isinstance(timeout, bool):
            raise ValueError("station manifest field 'surface.timeout_seconds' must be a number")
        try:
            timeout_number = float(timeout)
        except OverflowError as exc:
            raise ValueError(
                "station manifest field 'surface.timeout_seconds' must be within finite numeric range"
            ) from exc
        if not math.isfinite(timeout_number):
            raise ValueError("station manifest field 'surface.timeout_seconds' must be finite")
    max_chars = raw.get("max_chars")
    if max_chars is not None and (not isinstance(max_chars, int) or isinstance(max_chars, bool)):
        raise ValueError("station manifest field 'surface.max_chars' must be an integer")
    return ManifestSurface(
        kind=kind,
        command=command,
        read_only=read_only,
        timeout_seconds=float(timeout) if timeout is not None else None,
        max_chars=max_chars,
        probe=probe,
        probe_contains=probe_contains,
        placeholders=placeholders,
    )


def _parse_tool(raw: object, index: int) -> ManifestTool:
    if not isinstance(raw, dict):
        raise ValueError(f"station manifest tool {index} must be an object")
    name = _required_str(raw, "name")
    kind = _optional_str(raw, "kind", "executable")
    if kind not in TOOL_KINDS:
        allowed = ", ".join(TOOL_KINDS)
        raise ValueError(f"station manifest field 'tool.kind' must be one of: {allowed}")
    command = _optional_str(raw, "command")
    if kind == "executable" and not command:
        raise ValueError("station manifest field 'tool.command' must be a non-empty string for executable tools")
    if kind == "skill-roster" and command:
        raise ValueError("station manifest field 'tool.command' must be omitted for skill-roster tools")
    summary = _optional_str(raw, "summary")
    install = _string_tuple(raw.get("install"), "tool.install")
    produces = _string_tuple(raw.get("produces"), "tool.produces")
    consumes = _string_tuple(raw.get("consumes"), "tool.consumes")
    dependencies = _string_tuple(raw.get("dependencies"), "tool.dependencies")
    surfaces_raw = raw.get("surfaces", [])
    if not isinstance(surfaces_raw, list):
        raise ValueError("station manifest field 'tool.surfaces' must be an array")
    surfaces = tuple(_parse_surface(item, surface_index) for surface_index, item in enumerate(surfaces_raw))
    return ManifestTool(
        name=name,
        command=command,
        summary=summary,
        install=install,
        surfaces=surfaces,
        kind=kind,
        produces=produces,
        consumes=consumes,
        dependencies=dependencies,
    )
