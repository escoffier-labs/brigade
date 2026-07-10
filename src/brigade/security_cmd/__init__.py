"""Compatibility facade for the split command family."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any

_MODULE_NAMES = (
    "models",
    "config",
    "reports",
    "enrichment",
    "suppression",
    "template_audit",
    "scan_engine",
    "commands",
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


_module_models = importlib.import_module(f"{__name__}.models")
_module_config = importlib.import_module(f"{__name__}.config")
_module_reports = importlib.import_module(f"{__name__}.reports")
_module_enrichment = importlib.import_module(f"{__name__}.enrichment")
_module_suppression = importlib.import_module(f"{__name__}.suppression")
_module_template_audit = importlib.import_module(f"{__name__}.template_audit")
_module_scan_engine = importlib.import_module(f"{__name__}.scan_engine")
_module_commands = importlib.import_module(f"{__name__}.commands")

from .models import *
from .config import *
from .reports import *
from .enrichment import *
from .suppression import *
from .template_audit import *
from .scan_engine import *
from .commands import *

_MODULES = (
    _module_models,
    _module_config,
    _module_reports,
    _module_enrichment,
    _module_suppression,
    _module_template_audit,
    _module_scan_engine,
    _module_commands,
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
