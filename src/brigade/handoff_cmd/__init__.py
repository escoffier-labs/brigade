"""Compatibility facade for the split command family."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any

_MODULE_NAMES = (
    "models",
    "inspect",
    "linting",
    "migrate",
    "drafts",
    "receipts",
    "issues",
    "sources",
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
_module_inspect = importlib.import_module(f"{__name__}.inspect")
_module_linting = importlib.import_module(f"{__name__}.linting")
_module_migrate = importlib.import_module(f"{__name__}.migrate")
_module_drafts = importlib.import_module(f"{__name__}.drafts")
_module_receipts = importlib.import_module(f"{__name__}.receipts")
_module_issues = importlib.import_module(f"{__name__}.issues")
_module_sources = importlib.import_module(f"{__name__}.sources")

from .models import *
from .inspect import *
from .linting import *
from .migrate import *
from .drafts import *
from .receipts import *
from .issues import *
from .sources import *

_MODULES = (
    _module_models,
    _module_inspect,
    _module_linting,
    _module_migrate,
    _module_drafts,
    _module_receipts,
    _module_issues,
    _module_sources,
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
