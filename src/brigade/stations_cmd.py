"""Inspect Brigade's built-in station catalog."""

from __future__ import annotations

import json
from typing import Any

from . import managed, profiles, registry
from .install import DEFAULT_WIRED_SKILLS


def _json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _selection_for(station_name: str, profile: profiles.StationProfile) -> str:
    if station_name in profile.selected_stations:
        return "selected"
    if station_name in profile.optional_stations:
        return "optional"
    return "not selected"


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
    return 0
