"""Compatibility facade for the split command family."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any

_MODULE_NAMES = (
    "lifecycle",
    "brief",
    "tasks",
    "run_status",
)


def __getattr__(name: str) -> Any:
    if name in _MODULE_NAMES:
        module = importlib.import_module(f"{__name__}.{name}")
        globals()[name] = module
        return module
    for module_name in _MODULE_NAMES:
        module = importlib.import_module(f"{__name__}.{module_name}")
        if hasattr(module, name):
            value = getattr(module, name)
            globals()[name] = value
            return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_module_lifecycle = importlib.import_module(f"{__name__}.lifecycle")
_module_brief = importlib.import_module(f"{__name__}.brief")
_module_tasks = importlib.import_module(f"{__name__}.tasks")
_module_run_status = importlib.import_module(f"{__name__}.run_status")

from .lifecycle import *
from .brief import *
from .tasks import *
from .run_status import *

_MODULES = (
    _module_lifecycle,
    _module_brief,
    _module_tasks,
    _module_run_status,
)


def _sync_module_globals() -> None:
    exported: dict[str, Any] = {}
    for module in _MODULES:
        for name, value in vars(module).items():
            if name.startswith("__"):
                continue
            exported.setdefault(name, value)
    facade = sys.modules[__name__]
    for name, value in exported.items():
        setattr(facade, name, value)
    for module in _MODULES:
        for name, value in exported.items():
            if name not in module.__dict__:
                setattr(module, name, value)


class _CommandFamilyFacade(ModuleType):
    def __setattr__(self, name: str, value: Any) -> None:
        super().__setattr__(name, value)
        for module in _MODULES:
            if hasattr(module, name):
                setattr(module, name, value)


_sync_module_globals()
sys.modules[__name__].__class__ = _CommandFamilyFacade
