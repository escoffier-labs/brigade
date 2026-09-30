"""Project-scoped Claude Code work-loop hooks.

Public names are loaded lazily so ``brigade.claude_hooks.entry`` (the hot
per-tool-call path) does not import the installer.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_LAZY = {
    "hook_run": ".runtime",
    "hooks_install": ".install_cmd",
    "hooks_status": ".install_cmd",
    "hooks_uninstall": ".install_cmd",
    "hooks_update": ".install_cmd",
}

__all__ = ["hook_run", "hooks_install", "hooks_status", "hooks_uninstall", "hooks_update"]


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value
