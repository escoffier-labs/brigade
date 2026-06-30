"""Inspect built-in station profiles."""

from __future__ import annotations

import json
from typing import Any

from . import profiles, registry


def _json_print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _profile_payload(profile: profiles.StationProfile) -> dict[str, Any]:
    known = {station.name for station in registry.all_stations()}
    selected = list(profile.selected_stations)
    optional = list(profile.optional_stations)
    missing = sorted((set(selected) | set(optional)) - known)
    return {
        "name": profile.name,
        "aliases": list(profile.aliases),
        "summary": profile.summary,
        "selected_stations": selected,
        "optional_stations": optional,
        "missing_stations": missing,
    }


def list_profiles(*, json_output: bool = False) -> int:
    rows = [_profile_payload(profile) for profile in profiles.all_profiles()]
    if json_output:
        _json_print({"profiles": rows})
        return 0

    width = max((len(row["name"]) for row in rows), default=7)
    print("brigade profiles:")
    for row in rows:
        print(
            f"  {row['name'].ljust(width)}  selected={len(row['selected_stations'])} "
            f"optional={len(row['optional_stations'])}  - {row['summary']}"
        )
    return 0


def show_profile(name: str, *, json_output: bool = False) -> int:
    profile = profiles.resolve(name)
    if profile is None:
        print(f"unknown profile: {name}")
        return 2

    payload = _profile_payload(profile)
    if json_output:
        _json_print(payload)
        return 0

    print(f"profile: {payload['name']}")
    if payload["aliases"]:
        print(f"aliases: {', '.join(payload['aliases'])}")
    print(payload["summary"])
    print("selected stations:")
    for station in payload["selected_stations"]:
        print(f"  - {station}")
    print("optional stations:")
    for station in payload["optional_stations"]:
        print(f"  - {station}")
    if payload["missing_stations"]:
        print(f"missing stations: {', '.join(payload['missing_stations'])}")
        return 1
    return 0
