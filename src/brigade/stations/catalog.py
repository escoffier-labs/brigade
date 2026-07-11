"""Normalized read-only station catalog."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from .. import managed, registry, station_manifest
from ..install import DEFAULT_WIRED_SKILLS

CATALOG_SCHEMA = "brigade.stations.catalog.v1"


def compatibility_payload(compatibility: station_manifest.Compatibility) -> dict[str, Any]:
    return {
        "status": compatibility.status,
        "compatible": compatibility.compatible,
        "current_version": compatibility.current_version,
        "detail": compatibility.detail,
    }


def requires_payload(requirement: station_manifest.RequiresBrigade) -> dict[str, str | None]:
    return {
        "min_version": requirement.min_version,
        "max_version_exclusive": requirement.max_version_exclusive,
    }


def managed_surface_payload(surface: managed.MachineSurface) -> dict[str, Any]:
    return {
        "kind": surface.kind,
        "command": list(surface.command),
        "read_only": surface.read_only,
        "timeout_seconds": surface.timeout_seconds,
        "max_chars": surface.max_chars,
        "probe": list(surface.probe),
        "probe_contains": list(surface.probe_contains),
    }


def manifest_surface_payload(surface: station_manifest.ManifestSurface) -> dict[str, Any]:
    return {
        "kind": surface.kind,
        "command": list(surface.command),
        "read_only": surface.read_only,
        "timeout_seconds": surface.timeout_seconds,
        "max_chars": surface.max_chars,
        "probe": list(surface.probe),
        "probe_contains": list(surface.probe_contains),
        "placeholders": list(surface.placeholders),
    }


def manifest_tool_payload(tool: station_manifest.ManifestTool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "kind": tool.kind,
        "command": tool.command,
        "summary": tool.summary,
        "install": list(tool.install),
        "surfaces": [manifest_surface_payload(surface) for surface in tool.surfaces],
        "produces": list(tool.produces),
        "consumes": list(tool.consumes),
        "dependencies": list(tool.dependencies),
    }


def _managed_tool_payload(tool: managed.ManagedTool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "kind": "executable",
        "command": tool.command,
        "summary": tool.summary,
        "install": list(tool.install_args),
        "surfaces": [managed_surface_payload(surface) for surface in tool.surfaces],
        "produces": [surface.kind for surface in tool.surfaces],
        "consumes": [],
        "dependencies": [],
    }


def _station_payload(station_name: str) -> dict[str, Any]:
    station = registry.resolve(station_name)
    if station is None:
        return {
            "name": station_name,
            "summary": "",
            "aliases": [],
        }
    return {
        "name": station.name,
        "summary": station.summary,
        "aliases": list(station.aliases),
    }


def _managed_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for tool in managed.all_tools():
        row_id = f"managed:{tool.station}:{tool.name}"
        rows.append(
            {
                "id": row_id,
                "source": "managed",
                "station": _station_payload(tool.station),
                "lifecycle": "active",
                "owner": "brigade",
                "tool": _managed_tool_payload(tool),
                "tools": [_managed_tool_payload(tool)],
                "surfaces": [managed_surface_payload(surface) for surface in tool.surfaces],
                "managed": True,
                "compatible": True,
                "compatibility": {
                    "status": "compatible",
                    "compatible": True,
                    "current_version": "",
                    "detail": "",
                },
                "requires_brigade": {"min_version": None, "max_version_exclusive": None},
                "manifest": None,
            }
        )
    for station in registry.all_stations():
        if any(tool.station == station.name for tool in managed.all_tools()):
            continue
        tools = list(DEFAULT_WIRED_SKILLS) if station.name == "skills" else []
        rows.append(
            {
                "id": f"managed:{station.name}:station",
                "source": "managed",
                "station": _station_payload(station.name),
                "lifecycle": "active",
                "owner": "brigade",
                "tool": {
                    "name": station.name,
                    "kind": "builtin",
                    "command": "",
                    "summary": station.summary,
                    "install": [],
                    "surfaces": [],
                    "produces": tools,
                    "consumes": [],
                    "dependencies": [],
                },
                "tools": [],
                "surfaces": [],
                "managed": True,
                "compatible": True,
                "compatibility": {
                    "status": "compatible",
                    "compatible": True,
                    "current_version": "",
                    "detail": "",
                },
                "requires_brigade": {"min_version": None, "max_version_exclusive": None},
                "manifest": None,
            }
        )
    rows.sort(key=lambda row: (row["source"], row["station"]["name"], row["tool"]["name"], row["id"]))
    return rows


def _external_rows(manifests: Iterable[station_manifest.StationManifest]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for manifest in manifests:
        for tool in manifest.tools:
            tool_payload = manifest_tool_payload(tool)
            rows.append(
                {
                    "id": f"external:{manifest.name}:{tool.name}",
                    "source": "external",
                    "station": {
                        "name": manifest.station,
                        "summary": manifest.summary,
                        "aliases": [],
                    },
                    "lifecycle": manifest.lifecycle,
                    "owner": manifest.owner,
                    "tool": tool_payload,
                    "tools": [tool_payload],
                    "surfaces": tool_payload["surfaces"],
                    "managed": False,
                    "compatible": manifest.compatibility.compatible,
                    "compatibility": compatibility_payload(manifest.compatibility),
                    "requires_brigade": requires_payload(manifest.requires_brigade),
                    "contract_version": manifest.contract_version,
                    "manifest": {
                        "path": str(manifest.path),
                        "name": manifest.name,
                        "schema": station_manifest.SCHEMA,
                    },
                }
            )
        if not manifest.tools:
            rows.append(
                {
                    "id": f"external:{manifest.name}:station",
                    "source": "external",
                    "station": {
                        "name": manifest.station,
                        "summary": manifest.summary,
                        "aliases": [],
                    },
                    "lifecycle": manifest.lifecycle,
                    "owner": manifest.owner,
                    "tool": {
                        "name": manifest.name,
                        "kind": "manifest",
                        "command": "",
                        "summary": manifest.summary,
                        "install": [],
                        "surfaces": [],
                        "produces": [],
                        "consumes": [],
                        "dependencies": [],
                    },
                    "tools": [],
                    "surfaces": [],
                    "managed": False,
                    "compatible": manifest.compatibility.compatible,
                    "compatibility": compatibility_payload(manifest.compatibility),
                    "requires_brigade": requires_payload(manifest.requires_brigade),
                    "contract_version": manifest.contract_version,
                    "manifest": {
                        "path": str(manifest.path),
                        "name": manifest.name,
                        "schema": station_manifest.SCHEMA,
                    },
                }
            )
    rows.sort(key=lambda row: (row["source"], row["station"]["name"], row["tool"]["name"], row["id"]))
    return rows


def _load_external(paths: Iterable[Path | str]) -> tuple[list[station_manifest.StationManifest], list[dict[str, str]]]:
    manifests: list[station_manifest.StationManifest] = []
    errors: list[dict[str, str]] = []
    for path in paths:
        try:
            manifests.append(station_manifest.load(str(path)))
        except ValueError as exc:
            errors.append({"path": str(path), "error": str(exc)})
    return manifests, errors


def catalog_payload(*, external_manifests: Iterable[Path | str] = ()) -> dict[str, Any]:
    manifests, errors = _load_external(external_manifests)
    rows = [*_managed_rows(), *_external_rows(manifests)]
    rows.sort(key=lambda row: (row["source"], row["station"]["name"], row["tool"]["name"], row["id"]))
    source_counts = {
        "managed": sum(1 for row in rows if row["source"] == "managed"),
        "external": sum(1 for row in rows if row["source"] == "external"),
    }
    compatibility_counts = {
        "compatible": sum(1 for row in rows if row["compatible"]),
        "incompatible": sum(1 for row in rows if not row["compatible"]),
    }
    return {
        "schema": CATALOG_SCHEMA,
        "row_count": len(rows),
        "source_counts": source_counts,
        "compatibility_counts": compatibility_counts,
        "rows": rows,
        "errors": errors,
    }
