"""The Dot connector pack and the hosted-agent profiles that share its code."""

from .contracts import DotError, PACK_ID, TOOLS

# Profiles load before lifecycle: lifecycle imports grokbot_packs, which reads them from this package.
from .profile import DOT, DUET, PROFILES, ConnectorProfile, profile_for
from .runtime_config import validate_runtime_reference
from .lifecycle import (
    build_listener_from_target,
    canary,
    doctor,
    render_unit,
    run_listener,
    write_unit,
    write_watch_units,
)

DUET_PACK_ID = DUET.pack_id
DUET_TOOLS = DUET.tools

__all__ = [
    "PACK_ID",
    "TOOLS",
    "DUET_PACK_ID",
    "DUET_TOOLS",
    "DOT",
    "DUET",
    "PROFILES",
    "ConnectorProfile",
    "profile_for",
    "DotError",
    "doctor",
    "canary",
    "render_unit",
    "write_unit",
    "write_watch_units",
    "validate_runtime_reference",
    "build_listener_from_target",
    "run_listener",
]
