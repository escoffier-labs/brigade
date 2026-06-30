"""The built-in station registry."""

from __future__ import annotations

from typing import Optional, Tuple

from . import doctor as _doctor
from .station import Station

CORE = Station(
    name="core",
    summary="workspace bootstrap and harness adapters",
    aliases=("mise",),
    doctor=_doctor.core_station_checks,
)
MEMORY = Station(
    name="memory",
    summary="handoff inbox, ingest, and memory-care",
    aliases=("garde",),
    doctor=_doctor.memory_station_checks,
    tools=("memory-doctor", "bootstrap-doctor"),
)
GUARD = Station(
    name="guard",
    summary="publish safety and content scrub",
    aliases=("pass",),
    doctor=_doctor.guard_station_checks,
    tools=("content-guard",),
)
SKILLS = Station(
    name="skills",
    summary="portable skillet-style agent skills, Scout workflows, and runbooks",
    aliases=("skillet",),
    doctor=_doctor.skills_station_checks,
)
TOKENS = Station(
    name="tokens",
    summary="Token Glace output compaction",
    aliases=(),
    doctor=_doctor.tokens_station_checks,
    tools=("token-glace",),
)
SEARCH = Station(
    name="search",
    summary="local semantic code search",
    aliases=("code-search",),
    doctor=_doctor.search_station_checks,
    tools=("code-search-api", "code-search-mcp"),
)
SECURITY = Station(
    name="security",
    summary="agent workspace security scanning",
    aliases=("sec",),
    doctor=_doctor.security_station_checks,
)
PANTRY = Station(
    name="pantry",
    summary="agent session auth sync",
    aliases=("larder",),
    doctor=_doctor.pantry_station_checks,
    tools=("agentpantry",),
)
NOTIFICATIONS = Station(
    name="notifications",
    summary="operator notification wiring",
    aliases=("notify",),
    doctor=_doctor.notifications_station_checks,
    tools=("agent-notify",),
)

EVIDENCE = Station(
    name="evidence",
    summary="local-first evidence ledger and source exporters",
    aliases=("ledger",),
    doctor=_doctor.evidence_station_checks,
    tools=("miseledger", "stationtrail", "sourceharvest"),
)

MCP = Station(
    name="mcp",
    summary="canonical MCP server config sync",
    aliases=("brigadier",),
    doctor=_doctor.mcp_station_checks,
)

_BUILTIN: Tuple[Station, ...] = (
    CORE,
    SKILLS,
    MEMORY,
    GUARD,
    TOKENS,
    SEARCH,
    SECURITY,
    PANTRY,
    NOTIFICATIONS,
    EVIDENCE,
    MCP,
)


def all_stations() -> Tuple[Station, ...]:
    return _BUILTIN


def resolve(name_or_alias: str) -> Optional[Station]:
    for station in _BUILTIN:
        if station.matches(name_or_alias):
            return station
    return None
