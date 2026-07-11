"""Build JSON-ready station manifest scaffold payloads."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping, Sequence

from . import registry, station_manifest, stations_cmd


SCHEMA = "brigade.station_scaffold.v1"
FILES = ("README.md", "station.json")
NEXT_COMMANDS = ("brigade stations verify .", "brigade add . --install")


def scaffold_payload(
    output: str | os.PathLike[str],
    *,
    station: str,
    name: str,
    summary: str,
    command: str,
    install: Sequence[str],
    surfaces: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a JSON-ready scaffold plan after station-manifest validation."""
    output_path = Path(output)
    unsafe = _unsafe_output(output_path)
    if unsafe:
        return _error_payload(output_path, detail=unsafe, status="refused")
    try:
        manifest = _manifest_payload(
            station=station,
            name=name,
            summary=summary,
            command=command,
            install=install,
            surfaces=surfaces,
        )
        _validate_manifest(manifest)
    except ValueError as exc:
        return _error_payload(output_path, detail=str(exc))

    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "ok": True,
        "status": "ready",
        "output": str(output_path),
        "files": list(FILES),
        "manifest": manifest,
        "would_write": True,
        "wrote": False,
        "written": [],
        "safety": _safety_payload(),
        "next_commands": list(NEXT_COMMANDS),
        "detail": "station scaffold is ready to write",
    }
    payload["safety"]["writes_outside_output"] = False
    return payload


def write_scaffold(
    output: str | os.PathLike[str],
    *,
    station: str,
    name: str,
    summary: str,
    command: str,
    install: Sequence[str],
    surfaces: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write station.json and README without running install, probe, or command surfaces."""
    output_path = Path(output)
    payload = scaffold_payload(
        output_path,
        station=station,
        name=name,
        summary=summary,
        command=command,
        install=install,
        surfaces=surfaces,
    )
    if not payload["ok"]:
        return payload
    existing = [relative for relative in FILES if os.path.lexists(output_path / relative)]
    if existing:
        payload.update(
            ok=False,
            status="refused",
            would_write=False,
            detail=f"refusing to overwrite file(s) that already exists: {', '.join(existing)}",
            existing=existing,
        )
        return payload

    output_path.mkdir(parents=True, exist_ok=True)
    trusted_root = output_path.resolve(strict=True)
    station_path = output_path / "station.json"
    readme_path = output_path / "README.md"
    _atomic_write(station_path, json.dumps(payload["manifest"], indent=2, sort_keys=True) + "\n", trusted_root)
    _atomic_write(readme_path, _readme(payload["manifest"]), trusted_root)
    written = [
        {"path": "README.md", "bytes": readme_path.stat().st_size},
        {"path": "station.json", "bytes": station_path.stat().st_size},
    ]
    payload.update(
        ok=True,
        status="written",
        would_write=False,
        wrote=True,
        written=written,
        detail="station scaffold written",
    )
    return payload


def _manifest_payload(
    *,
    station: str,
    name: str,
    summary: str,
    command: str,
    install: Sequence[str],
    surfaces: Sequence[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    resolved_station = registry.resolve(station)
    if resolved_station is None:
        raise ValueError(f"unknown station: {station}")
    install_argv = _argv("install", install)
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    tool_command = command.strip()
    surface_payloads = (
        [_default_surface(tool_command)]
        if surfaces is None
        else [_surface_payload(surface, index) for index, surface in enumerate(surfaces)]
    )
    return {
        "schema": station_manifest.SCHEMA,
        "name": _required_text("name", name),
        "station": resolved_station.name,
        "summary": _required_text("summary", summary),
        "tools": [
            {
                "name": _required_text("name", name),
                "command": tool_command,
                "summary": _required_text("summary", summary),
                "install": install_argv,
                "surfaces": surface_payloads,
            }
        ],
    }


def _validate_manifest(manifest: Mapping[str, Any]) -> None:
    with tempfile.TemporaryDirectory(prefix="brigade-station-scaffold-") as temp:
        path = Path(temp) / "station.json"
        path.write_text(json.dumps(manifest))
        loaded = station_manifest.load(str(path))
        for tool in loaded.tools:
            for surface in tool.surfaces:
                status, _, detail = stations_cmd._surface_preflight(tool, surface, manifest_dir=path.parent)
                if status is not None:
                    raise ValueError(f"surface {surface.kind} fails stations verify preflight: {detail}")


def _required_text(field: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _argv(field: str, value: Sequence[str]) -> list[str]:
    if isinstance(value, str) or not value or not all(isinstance(item, str) and item for item in value):
        raise ValueError(f"{field} must be a non-empty string array")
    return list(value)


def _default_surface(command: str) -> dict[str, Any]:
    return {
        "kind": "verify-exit",
        "command": [command, "--version"],
        "read_only": True,
        "timeout_seconds": 2,
    }


def _surface_payload(surface: Mapping[str, Any], index: int) -> dict[str, Any]:
    if not isinstance(surface, Mapping):
        raise ValueError(f"surface {index} must be an object")
    payload: dict[str, Any] = {}
    allowed = {"kind", "command", "read_only", "timeout_seconds", "max_chars", "probe", "probe_contains"}
    for key, value in surface.items():
        if key not in allowed:
            raise ValueError(f"surface {index} contains unsupported field: {key}")
        if key in {"command", "probe", "probe_contains"}:
            payload[key] = _argv(f"surface.{key}", value)
        else:
            payload[key] = value
    return payload


def _safety_payload() -> dict[str, bool | None]:
    return {
        "install_executed": False,
        "probe_executed": False,
        "writes_outside_output": None,
    }


def _error_payload(output: Path, *, detail: str, status: str = "error") -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "ok": False,
        "status": status,
        "output": str(output),
        "files": list(FILES),
        "manifest": None,
        "would_write": False,
        "wrote": False,
        "written": [],
        "safety": _safety_payload(),
        "next_commands": [],
        "detail": detail,
    }


def _readme(manifest: Mapping[str, Any]) -> str:
    tool = manifest["tools"][0]
    install = " ".join(tool["install"])
    return (
        f"# {manifest['name']}\n\n"
        f"{manifest['summary']}\n\n"
        "This scaffold declares a Brigade station manifest. Review the manifest, "
        "put the tool command on PATH, then verify before installing.\n\n"
        "```bash\n"
        "brigade stations verify .\n"
        "brigade add . --install\n"
        "```\n\n"
        f"Station: `{manifest['station']}`\n\n"
        f"Command: `{tool['command']}`\n\n"
        f"Install argv: `{install}`\n"
    )


def _unsafe_output(output: Path) -> str | None:
    absolute = output.absolute()
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if not os.path.lexists(current):
            break
        if current.is_symlink():
            return f"output path contains a symlink: {current}"
    if os.path.lexists(output) and not output.is_dir():
        return "output path already exists and is not a directory"
    if output.is_dir():
        for relative in FILES:
            candidate = output / relative
            if os.path.lexists(candidate) and candidate.is_symlink():
                return f"destination is a symlink: {relative}"
    return None


def _atomic_write(destination: Path, content: str, trusted_root: Path) -> None:
    parent = destination.parent.resolve(strict=True)
    if not parent.is_relative_to(trusted_root):
        raise ValueError(f"destination escapes trusted root: {destination}")
    if os.path.lexists(destination):
        raise ValueError(f"destination already exists: {destination.name}")
    fd, temp_name = tempfile.mkstemp(prefix=f".{destination.name}.", dir=parent, text=True)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w") as target:
            target.write(content)
        os.replace(temp_path, destination)
    finally:
        if os.path.lexists(temp_path):
            temp_path.unlink()
