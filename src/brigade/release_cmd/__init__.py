"""Compatibility facade for the split command family."""
# ruff: noqa: E402,F401,F403,F811,F821

from __future__ import annotations

import importlib
import sys
from types import ModuleType
from typing import Any

_MODULE_NAMES = (
    "paths",
    "install_smoke",
    "ci",
    "evidence",
    "candidate",
    "candidate_audit",
    "schema",
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


_module_paths = importlib.import_module(f"{__name__}.paths")
_module_install_smoke = importlib.import_module(f"{__name__}.install_smoke")
_module_ci = importlib.import_module(f"{__name__}.ci")
_module_evidence = importlib.import_module(f"{__name__}.evidence")
_module_candidate = importlib.import_module(f"{__name__}.candidate")
_module_candidate_audit = importlib.import_module(f"{__name__}.candidate_audit")
_module_schema = importlib.import_module(f"{__name__}.schema")
_module_commands = importlib.import_module(f"{__name__}.commands")

from .paths import *
from .install_smoke import *
from .ci import *
from .evidence import *
from .candidate import *
from .candidate_audit import *
from .schema import *
from .commands import *

_MODULES = (
    _module_paths,
    _module_install_smoke,
    _module_ci,
    _module_evidence,
    _module_candidate,
    _module_candidate_audit,
    _module_schema,
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
