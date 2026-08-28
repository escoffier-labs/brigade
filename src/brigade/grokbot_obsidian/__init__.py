"""First-party Grok Bot Obsidian Operator Phase 1 connector pack."""

from __future__ import annotations

from .contracts import (
    DEFAULT_BIND,
    ERROR_MESSAGES,
    PACK_ID,
    PUBLIC_ROUTE,
    TOOLS,
    ObsidianError,
    parse_action_status_input,
    parse_capabilities_input,
    parse_execute_input,
    parse_propose_input,
    parse_read_input,
    parse_search_input,
)

__all__ = [
    "DEFAULT_BIND",
    "ERROR_MESSAGES",
    "PACK_ID",
    "PUBLIC_ROUTE",
    "TOOLS",
    "ObsidianError",
    "parse_action_status_input",
    "parse_capabilities_input",
    "parse_execute_input",
    "parse_propose_input",
    "parse_read_input",
    "parse_search_input",
]


def doctor(target, *, timeout=None):
    from .lifecycle import doctor as _doctor

    return _doctor(target, timeout=timeout)


def canary(target, *, timeout=None):
    from .lifecycle import canary as _canary

    return _canary(target, timeout=timeout)


def render_unit(target, *, python=None):
    from .lifecycle import render_unit as _render_unit

    return _render_unit(target, python=python)


def write_unit(target, out_dir, *, force=False, python=None):
    from .lifecycle import write_unit as _write_unit

    return _write_unit(target, out_dir, force=force, python=python)


def validate_disjoint_state_paths(runtime_path, action_state_path, approval_dir, staging_dir, excalidraw_bin):
    from .lifecycle import validate_disjoint_state_paths as _validate

    return _validate(runtime_path, action_state_path, approval_dir, staging_dir, excalidraw_bin)


def required_upstream_url(value):
    from .runtime_config import required_upstream_url as _required

    return _required(value)
