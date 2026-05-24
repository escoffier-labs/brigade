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

_BUILTIN: Tuple[Station, ...] = (CORE, MEMORY, GUARD, TOKENS)


def all_stations() -> Tuple[Station, ...]:
    return _BUILTIN


def resolve(name_or_alias: str) -> Optional[Station]:
    for station in _BUILTIN:
        if station.matches(name_or_alias):
            return station
    return None
