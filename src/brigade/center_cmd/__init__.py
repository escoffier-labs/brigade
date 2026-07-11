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
_MODULE_ALIASES = {
    "schema": "schema_ops",
    "report_review": "report_review_ops",
}


class _ModuleAlias(ModuleType):
    def __init__(self, alias_name: str, target: ModuleType, call_name: str | None) -> None:
        super().__init__(alias_name, target.__doc__)
        object.__setattr__(self, "_target_module", target)
        object.__setattr__(self, "_call_name", call_name)
        object.__setattr__(self, "_call_target", getattr(target, call_name) if call_name else None)
        self.__dict__.update(vars(target))
        self.__dict__["__name__"] = alias_name
        self.__dict__["__package__"] = __name__

    def __getattr__(self, name: str) -> Any:
        return getattr(self.__dict__["_target_module"], name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("__") or name in {"_target_module", "_call_name", "_call_target"}:
            super().__setattr__(name, value)
            return
        setattr(self.__dict__["_target_module"], name, value)
        super().__setattr__(name, value)

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        call_name = self.__dict__.get("_call_name")
        if call_name is None:
            raise TypeError(f"module {self.__name__!r} is not callable")
        call_target = getattr(self.__dict__["_target_module"], call_name)
        if call_target is self:
            call_target = self.__dict__["_call_target"]
        return call_target(*args, **kwargs)


def _facade_module(name: str) -> ModuleType:
    target_name = _MODULE_ALIASES.get(name, name)
    target = importlib.import_module(f"{__name__}.{target_name}")
    if target_name == name:
        return target
    call_name = name if callable(getattr(target, name, None)) else None
    module = _ModuleAlias(f"{__name__}.{name}", target, call_name)
    sys.modules[f"{__name__}.{name}"] = module
    globals()[name] = module
    return module


def _restore_module_aliases() -> None:
    for name in _MODULE_ALIASES:
        globals()[name] = sys.modules[f"{__name__}.{name}"]


def __getattr__(name: str) -> Any:
    if name in _MODULE_NAMES:
        module = _facade_module(name)
        globals()[name] = module
        return module
    for module_name in _MODULE_NAMES:
        module = _facade_module(module_name)
        if hasattr(module, name):
            value = getattr(module, name)
            globals()[name] = value
            return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


_module_schema = _facade_module("schema")
_module_core = importlib.import_module(f"{__name__}.core")
_module_readiness = importlib.import_module(f"{__name__}.readiness")
_module_reports = importlib.import_module(f"{__name__}.reports")
_module_report_review = _facade_module("report_review")
_module_actions = importlib.import_module(f"{__name__}.actions")

from .schema_ops import *
from .core import *
from .readiness import *
from .reports import *
from .report_review_ops import *
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
            if name.startswith("__") or name in {"_target_module", "_call_name", "_call_target"}:
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
_restore_module_aliases()
sys.modules[__name__].__class__ = _CommandFamilyFacade
