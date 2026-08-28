"""First-party Grok Bot Backup Steward connector pack."""

from __future__ import annotations

from .contracts import (
    DEFAULT_BIND,
    ERROR_MESSAGES,
    PACK_ID,
    TOOLS,
    BackupError,
    parse_execute_input,
    parse_operation_input,
    parse_overview_input,
    parse_propose_input,
    parse_target_input,
)

__all__ = [
    "DEFAULT_BIND",
    "ERROR_MESSAGES",
    "PACK_ID",
    "TOOLS",
    "BackupError",
    "parse_execute_input",
    "parse_operation_input",
    "parse_overview_input",
    "parse_propose_input",
    "parse_target_input",
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


def validate_disjoint_state_paths(runtime_path, ledger_path, action_state_path, approval_dir):
    from .lifecycle import validate_disjoint_state_paths as _validate

    return _validate(runtime_path, ledger_path, action_state_path, approval_dir)
