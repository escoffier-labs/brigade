"""Compatibility coverage for the split skills_cmd package facade."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest

from brigade import skills_cmd


SKILLS_CMD_NAMES_FIXTURE = Path(__file__).with_name("fixtures") / "skills_cmd_facade_names.txt"

# Names tests/test_skills_cmd.py already patches on brigade.skills_cmd, plus
# other skills_cmd attributes that file calls so the forwarding suite stays
# above the required 15-name floor. template_root is a shared import and is
# checked in the broadcast tests instead of here.
PATCHED_FACADE_NAMES = (
    "RENDERER_SCHEMA_VERSION",
    "_HAS_DESCRIPTOR_ANCHOR",
    "_collect_source_tree",
    "_lint_payload",
    "_write_collected_tree_into_anchor",
    "install",
    "import_skill",
    "search",
    "sync",
    "lint",
    "doctor",
    "serve_mcp",
    "_compatibility_payload",
    "_fleet_status_payload",
    "_text_fingerprint",
)

SEAM_NAMED_PUBLIC_FUNCTIONS = ("install",)


def _main_skills_cmd_names() -> set[str]:
    # Top-level names from src/brigade/skills_cmd.py on origin/main, frozen here
    # so the test never reads origin/main at test time.
    return set(SKILLS_CMD_NAMES_FIXTURE.read_text().splitlines())


def _owner_module(name: str) -> ModuleType:
    owner = skills_cmd._OWNERS[name]
    return importlib.import_module(f"{skills_cmd.__name__}.{owner}")


def test_facade_matches_every_top_level_name_from_main_skills_cmd() -> None:
    expected = _main_skills_cmd_names()
    assert set(skills_cmd._OWNERS) == expected
    for name in expected:
        owner = _owner_module(name)
        assert getattr(skills_cmd, name) is getattr(owner, name)


def test_facade_monkeypatches_forward_to_owning_module(monkeypatch: pytest.MonkeyPatch) -> None:
    assert len(PATCHED_FACADE_NAMES) >= 15
    for name in PATCHED_FACADE_NAMES:
        sentinel = object()
        monkeypatch.setattr(skills_cmd, name, sentinel)
        assert getattr(_owner_module(name), name) is sentinel


@pytest.mark.parametrize("name", ("os", "template_root", "json"))
def test_facade_monkeypatches_broadcast_shared_imports(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    sentinel = object()
    monkeypatch.setattr(skills_cmd, name, sentinel)
    for module_name in skills_cmd._SHARED_IMPORTS[name]:
        module = importlib.import_module(f"{skills_cmd.__name__}.{module_name}")
        assert getattr(module, name) is sentinel


def test_facade_shared_import_assignment_reads_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # The facade must not shadow a shared import with its own binding: after
    # assignment, the facade and every seam that binds the name agree.
    for name in ("sys", "Any", "os"):
        sentinel = object()
        monkeypatch.setattr(skills_cmd, name, sentinel)
        assert getattr(skills_cmd, name) is sentinel
        for module_name in skills_cmd._SHARED_IMPORTS[name]:
            assert getattr(importlib.import_module(f"{skills_cmd.__name__}.{module_name}"), name) is sentinel
    monkeypatch.undo()
    import sys as real_sys

    assert skills_cmd.sys is real_sys


def test_cross_module_calls_read_live_owner_attributes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(skills_cmd, "_state_root_selector_kind", lambda target, requested: "registry")
    assert _owner_module("_selects_registry_skill")._selects_registry_skill(tmp_path, "not-a-prefix") is True


def test_seam_named_public_functions_are_callables_not_modules() -> None:
    for name in SEAM_NAMED_PUBLIC_FUNCTIONS:
        exported = getattr(skills_cmd, name)
        assert callable(exported)
        assert not isinstance(exported, ModuleType)
        assert exported.__module__ == f"{skills_cmd.__name__}.{name}"
