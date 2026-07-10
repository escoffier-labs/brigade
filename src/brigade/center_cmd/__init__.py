"""Compatibility facade for the split command family."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any

_MODULE_NAMES = (
    "schema",
    "core",
    "readiness",
    "reports",
    "report_review",
    "actions",
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


_module_schema = importlib.import_module(f"{__name__}.schema")
_module_core = importlib.import_module(f"{__name__}.core")
_module_readiness = importlib.import_module(f"{__name__}.readiness")
_module_reports = importlib.import_module(f"{__name__}.reports")
_module_report_review = importlib.import_module(f"{__name__}.report_review")
_module_actions = importlib.import_module(f"{__name__}.actions")

from .schema import *
from .core import *
from .readiness import *
from .reports import *
from .report_review import *
from .actions import *

_MODULES = (
    _module_schema,
    _module_core,
    _module_readiness,
    _module_reports,
    _module_report_review,
    _module_actions,
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
