"""Compatibility coverage for the split aboyeur facade."""

from __future__ import annotations

import importlib
from pathlib import Path
from types import ModuleType

import pytest

from brigade import aboyeur
from brigade.roster import Agent, Roster


ABOYEUR_NAMES_FIXTURE = Path(__file__).with_name("fixtures") / "aboyeur_facade_names.txt"

# Names tests/test_aboyeur.py already monkeypatches, plus other owned names the
# aboyeur suite patches, so the forwarding check covers at least 15 seams.
PATCHED_FACADE_NAMES = (
    "_graphtrail_bin",
    "_run_orchestrator",
    "_supports_directory_fsync",
    "_resolve_authority_state",
    "_write_json",
    "plan",
    "dispatch",
    "write_run_handoff",
    "record_run_start",
    "run",
    "make_run_dir",
    "code_graph_brief",
    "record_run_termination",
    "_authoritative_prior_decision",
    "parse_plan",
    "build_plan_prompt",
)


def _main_aboyeur_names() -> set[str]:
    # Top-level names from origin/main src/brigade/aboyeur.py, frozen once.
    return set(ABOYEUR_NAMES_FIXTURE.read_text().splitlines())


def _owner_module(name: str) -> ModuleType:
    owner = aboyeur._OWNERS[name]
    return importlib.import_module(f"{aboyeur.__name__}.{owner}")


def test_facade_matches_every_top_level_name_from_main_aboyeur() -> None:
    expected = _main_aboyeur_names()
    assert set(aboyeur._OWNERS) == expected
    for name in expected:
        owner = _owner_module(name)
        assert getattr(aboyeur, name) is getattr(owner, name)


def test_facade_monkeypatches_forward_to_owning_module(monkeypatch: pytest.MonkeyPatch) -> None:
    assert len(PATCHED_FACADE_NAMES) >= 15
    for name in PATCHED_FACADE_NAMES:
        sentinel = object()
        monkeypatch.setattr(aboyeur, name, sentinel)
        assert getattr(_owner_module(name), name) is sentinel


@pytest.mark.parametrize("name", ("os", "proc"))
def test_facade_monkeypatches_broadcast_shared_imports(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    sentinel = object()
    monkeypatch.setattr(aboyeur, name, sentinel)
    for module_name in aboyeur._SHARED_IMPORTS[name]:
        module = importlib.import_module(f"{aboyeur.__name__}.{module_name}")
        assert getattr(module, name) is sentinel


def test_cross_module_calls_read_live_owner_attributes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(aboyeur, "_read_only_rules", lambda: "LIVE-PATCH-MARKER")
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan and synthesize"),
            "coder": Agent("coder", "ollama:llama3.3", "write code"),
        },
        max_workers=2,
    )
    prompt = _owner_module("build_plan_prompt").build_plan_prompt("split the work", roster, read_only=True)
    assert "LIVE-PATCH-MARKER" in prompt


def test_facade_shared_import_assignment_reads_back(monkeypatch: pytest.MonkeyPatch) -> None:
    # The facade must not shadow a shared import with its own binding: after
    # assignment, the facade and every seam that binds the name agree.
    for name in ("sys", "Any", "os"):
        sentinel = object()
        monkeypatch.setattr(aboyeur, name, sentinel)
        assert getattr(aboyeur, name) is sentinel
        for module_name in aboyeur._SHARED_IMPORTS[name]:
            assert getattr(importlib.import_module(f"{aboyeur.__name__}.{module_name}"), name) is sentinel
    monkeypatch.undo()
    import sys as real_sys

    assert aboyeur.sys is real_sys
