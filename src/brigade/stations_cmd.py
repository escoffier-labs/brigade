"""Inspect Brigade's built-in station catalog and discover external station.json files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import managed, profiles, registry, station_manifest
from .install import DEFAULT_WIRED_SKILLS


def _json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _selection_for(station_name: str, profile: profiles.StationProfile) -> str:
    if station_name in profile.selected_stations:
        return "selected"
    if station_name in profile.optional_stations:
        return "optional"
    return "not selected"


def _surface_payload(surface: managed.MachineSurface) -> dict[str, Any]:
    return {
        "kind": surface.kind,
        "command": list(surface.command),
        "read_only": surface.read_only,
        "timeout_seconds": surface.timeout_seconds,
        "max_chars": surface.max_chars,
    }


def list_stations(*, profile_name: str = "repo", json_output: bool = False) -> int:
    profile = profiles.resolve(profile_name)
    if profile is None:
        print(f"unknown profile: {profile_name}")
        return 2

    rows: list[dict[str, Any]] = []
    for station in registry.all_stations():
        tools = []
        for tool in managed.for_station(station.name):
            tools.append(
                {
                    "name": tool.name,
                    "command": tool.command,
                    "summary": tool.summary,
                    "install_args": list(tool.install_args),
                    "surfaces": [_surface_payload(surface) for surface in tool.surfaces],
                }
            )
        rows.append(
            {
                "station": station.name,
                "selection": _selection_for(station.name, profile),
                "summary": station.summary,
                "aliases": list(station.aliases),
                "tools": tools,
                "built_in_skills": list(DEFAULT_WIRED_SKILLS) if station.name == "skills" else [],
            }
        )

    payload = {"profile": profile.name, "stations": rows}
    if json_output:
        _json_print(payload)
        return 0

    print(f"brigade stations: profile={profile.name}")
    width = max((len(row["station"]) for row in rows), default=8)
    for row in rows:
        tool_labels = [tool["name"] for tool in row["tools"]]
        tool_labels.extend(row["built_in_skills"])
        tool_names = ", ".join(tool_labels) or "built-in"
        print(f"  {row['station'].ljust(width)}  [{row['selection']}]  {tool_names}  - {row['summary']}")
        for tool in row["tools"]:
            surfaces = tool.get("surfaces") or []
            if not surfaces:
                continue
            labels = ", ".join(surface["kind"] for surface in surfaces)
            print(f"  {'':{width}}    surfaces: {tool['name']}: {labels}")
    return 0


def _default_discover_roots() -> list[Path]:
    home = Path.home()
    roots = [
        Path.cwd(),
        home / "repos",
        home / "src",
        home / "code",
    ]
    # De-dupe while preserving order; keep only existing dirs.
    seen: set[Path] = set()
    out: list[Path] = []
    for root in roots:
        try:
            resolved = root.expanduser().resolve()
        except OSError:
            continue
        if resolved in seen or not resolved.is_dir():
            continue
        seen.add(resolved)
        out.append(resolved)
    return out


def discover_payload(
    *,
    roots: list[Path] | None = None,
    max_depth: int = 3,
) -> dict[str, Any]:
    search_roots = roots or _default_discover_roots()
    found: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    skip_dir_names = {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "dist",
        "build",
        ".tox",
        "target",
    }

    for root in search_roots:
        root = root.expanduser().resolve()
        if not root.is_dir():
            errors.append({"path": str(root), "error": "not a directory"})
            continue
        # Always check root/station.json
        candidates = [root / "station.json"]
        if max_depth >= 1:
            try:
                for child in sorted(root.iterdir()):
                    if not child.is_dir() or child.name in skip_dir_names or child.name.startswith("."):
                        continue
                    candidates.append(child / "station.json")
                    if max_depth >= 2:
                        try:
                            for grand in sorted(child.iterdir()):
                                if not grand.is_dir() or grand.name in skip_dir_names or grand.name.startswith("."):
                                    continue
                                candidates.append(grand / "station.json")
                        except OSError:
                            continue
            except OSError as exc:
                errors.append({"path": str(root), "error": str(exc)})
                continue

        seen_paths: set[Path] = set()
        for path in candidates:
            if path in seen_paths or not path.is_file():
                continue
            seen_paths.add(path)
            try:
                manifest = station_manifest.load(str(path))
            except ValueError as exc:
                errors.append({"path": str(path), "error": str(exc)})
                continue
            tools = []
            for tool in manifest.tools:
                tools.append(
                    {
                        "name": tool.name,
                        "kind": tool.kind,
                        "command": tool.command,
                        "summary": tool.summary,
                        "install": list(tool.install),
                        "surfaces": [
                            {
                                "kind": surface.kind,
                                "command": list(surface.command),
                                "read_only": surface.read_only,
                                "timeout_seconds": surface.timeout_seconds,
                                "max_chars": surface.max_chars,
                                "probe": list(surface.probe),
                                "probe_contains": list(surface.probe_contains),
                                "placeholders": list(surface.placeholders),
                            }
                            for surface in tool.surfaces
                        ],
                    }
                )
            found.append(
                {
                    "path": str(manifest.path),
                    "name": manifest.name,
                    "station": manifest.station,
                    "summary": manifest.summary,
                    "lifecycle": manifest.lifecycle,
                    "owner": manifest.owner,
                    "tools": tools,
                    "add_command": f"brigade add {manifest.path.parent}",
                }
            )

    lifecycle_counts = {lifecycle: 0 for lifecycle in station_manifest.LIFECYCLES}
    for manifest in found:
        lifecycle_counts[manifest["lifecycle"]] += 1

    return {
        "roots": [str(r) for r in search_roots],
        "max_depth": max_depth,
        "count": len(found),
        "active_count": lifecycle_counts["active"],
        "non_active_count": len(found) - lifecycle_counts["active"],
        "lifecycle_counts": lifecycle_counts,
        "manifests": found,
        "errors": errors,
        "docs": {
            "schema": station_manifest.SCHEMA,
            "add": "brigade add <path-to-dir-or-station.json> [--install]",
            "list": "brigade stations list",
        },
    }


def discover(
    *,
    roots: list[Path] | None = None,
    max_depth: int = 3,
    json_output: bool = False,
) -> int:
    payload = discover_payload(roots=roots, max_depth=max_depth)
    if json_output:
        _json_print(payload)
        return 0
    print(
        f"brigade stations discover: {payload['count']} station.json file(s) "
        f"({payload['active_count']} active, {payload['non_active_count']} non-active)"
    )
    for row in payload["manifests"]:
        tool_names = ", ".join(tool["name"] for tool in row["tools"]) or "(none)"
        print(f"  {row['name']}  station={row['station']}  lifecycle={row['lifecycle']}  tools={tool_names}")
        print(f"    path: {row['path']}")
        print(f"    next: {row['add_command']}")
    if payload["errors"]:
        print(f"errors: {len(payload['errors'])}")
        for err in payload["errors"][:10]:
            print(f"  {err['path']}: {err['error']}")
    if not payload["manifests"]:
        print("next: place a station.json (schema brigade.station.v1) in a sidecar repo, then re-run discover")
    return 0
