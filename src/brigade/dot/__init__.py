"""The Dot connector pack and the hosted-agent profiles that share its code."""

from .contracts import DotError, PACK_ID, TOOLS

# Profiles load before lifecycle: lifecycle imports grokbot_packs, which reads them from this package.
from .profile import DOT, DUET, GROKBOT_INBOX, GROKBOT_MEMORY, PROFILES, ConnectorProfile, profile_for
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
GROKBOT_MEMORY_PACK_ID = GROKBOT_MEMORY.pack_id
GROKBOT_MEMORY_TOOLS = GROKBOT_MEMORY.tools
GROKBOT_INBOX_PACK_ID = GROKBOT_INBOX.pack_id
GROKBOT_INBOX_TOOLS = GROKBOT_INBOX.tools

__all__ = [
    "PACK_ID",
    "TOOLS",
    "DUET_PACK_ID",
    "DUET_TOOLS",
    "GROKBOT_MEMORY_PACK_ID",
    "GROKBOT_MEMORY_TOOLS",
    "GROKBOT_INBOX_PACK_ID",
    "GROKBOT_INBOX_TOOLS",
    "DOT",
    "DUET",
    "GROKBOT_MEMORY",
    "GROKBOT_INBOX",
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
