"""Built-in station profiles.

Profiles are catalog metadata only. They describe the stations Brigade treats
as selected by default for a workspace shape; they do not install external
tools, write sidecar config, start services, or run station doctors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class StationProfile:
    name: str
    summary: str
    selected_stations: Tuple[str, ...]
    optional_stations: Tuple[str, ...] = ()
    aliases: Tuple[str, ...] = ()

    def matches(self, name_or_alias: str) -> bool:
        return name_or_alias == self.name or name_or_alias in self.aliases


REPO = StationProfile(
    name="repo",
    summary="Default repo workspace profile with priority stations and Scout skills selected.",
    aliases=("default",),
    selected_stations=("core", "skills", "memory", "guard", "security", "tokens", "evidence", "search"),
    optional_stations=("mcp", "pantry", "notifications"),
)

WORKSPACE = StationProfile(
    name="workspace",
    summary="Operator workspace profile with the repo priority stack and Scout skills selected.",
    selected_stations=("core", "skills", "memory", "guard", "security", "tokens", "evidence", "search", "mcp"),
    optional_stations=("pantry", "notifications"),
)

FLEET_OPERATOR = StationProfile(
    name="fleet-operator",
    summary="Read-only fleet visibility profile; fleet-kit style automation remains optional.",
    selected_stations=("core", "skills", "memory", "guard", "security", "evidence", "search", "mcp"),
    optional_stations=("tokens", "pantry", "notifications"),
)

_BUILTIN: Tuple[StationProfile, ...] = (REPO, WORKSPACE, FLEET_OPERATOR)


def all_profiles() -> Tuple[StationProfile, ...]:
    return _BUILTIN


def resolve(name_or_alias: str) -> Optional[StationProfile]:
    for profile in _BUILTIN:
        if profile.matches(name_or_alias):
            return profile
    return None
