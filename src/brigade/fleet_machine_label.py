"""Display words for machine status. Presentation only; counts keep using ``station_status``."""

from __future__ import annotations

from .fleet_command_deck import MachineState, StationView
from .fleet_deck_overview import POLICY_LIMITS, station_status


def status_label(station: StationView, machine: MachineState | None, status: str | None = None) -> str:
    """The word an admin reads for a machine. ``limited`` becomes its specific reason.

    ``station_status`` keeps returning ``limited`` for counts and styling. The display text is the
    policy limit when one is reported, else the machine state (busy, draining, unavailable).
    """
    status = station_status(station, machine) if status is None else status
    if status == "limited" and machine is not None:
        return POLICY_LIMITS.get(machine.unused_reason, machine.state)
    return status
