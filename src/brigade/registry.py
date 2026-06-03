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
TOKENS = Station(
    name="tokens",
    summary="output compaction",
    aliases=(),
    doctor=_doctor.tokens_station_checks,
    tools=("tokenjuice",),
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

_BUILTIN: Tuple[Station, ...] = (CORE, MEMORY, GUARD, TOKENS, SECURITY, PANTRY, NOTIFICATIONS)


def all_stations() -> Tuple[Station, ...]:
    return _BUILTIN


def resolve(name_or_alias: str) -> Optional[Station]:
    for station in _BUILTIN:
        if station.matches(name_or_alias):
            return station
    return None
