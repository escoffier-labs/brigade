"""Compatibility facade for the split command family."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any

_MODULE_NAMES = (
    "config",
    "candidates",
    "approvals",
    "status_plan",
    "run_loop",
    "telemetry",
    "hardening",
    "closeout",
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


_module_config = importlib.import_module(f"{__name__}.config")
_module_candidates = importlib.import_module(f"{__name__}.candidates")
_module_approvals = importlib.import_module(f"{__name__}.approvals")
_module_status_plan = importlib.import_module(f"{__name__}.status_plan")
_module_run_loop = importlib.import_module(f"{__name__}.run_loop")
_module_telemetry = importlib.import_module(f"{__name__}.telemetry")
_module_hardening = importlib.import_module(f"{__name__}.hardening")
_module_closeout = importlib.import_module(f"{__name__}.closeout")

from .config import *
from .candidates import *
from .approvals import *
from .status_plan import *
from .run_loop import *
from .telemetry import *
from .hardening import *
from .closeout import *

_MODULES = (
    _module_config,
    _module_candidates,
    _module_approvals,
    _module_status_plan,
    _module_run_loop,
    _module_telemetry,
    _module_hardening,
    _module_closeout,
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
