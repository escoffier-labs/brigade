"""First-party t3-dispatch Grok Bot connector pack."""

from __future__ import annotations

from .contracts import PACK_ID, TOOLS, T3DispatchError
from .lifecycle import (
    build_listener_from_target,
    canary,
    doctor,
    render_unit,
    run_listener,
    write_unit,
)
from .runtime_config import validate_runtime_reference

__all__ = [
    "PACK_ID",
    "TOOLS",
    "T3DispatchError",
    "build_listener_from_target",
    "canary",
    "doctor",
    "render_unit",
    "run_listener",
    "validate_runtime_reference",
    "write_unit",
]
