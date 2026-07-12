"""Read-only enforcement classification and `brigade run` advisory (issue #87)."""

from __future__ import annotations

from brigade import agents
from brigade.cli import run as run_cli
from brigade.roster import Agent, Roster


def test_read_only_enforcement_classification():
    assert agents.read_only_enforcement("codex") == "hard"
    assert agents.read_only_enforcement("aider") == "hard"
    assert agents.read_only_enforcement("grok") == "hard"
    assert agents.read_only_enforcement("goose") == "soft"
    assert agents.read_only_enforcement("crush") == "soft"
    assert agents.read_only_enforcement("claude") == "none"
    assert agents.read_only_enforcement("opencode") == "none"
    assert agents.read_only_enforcement("ollama:llama3") == "none"
    assert agents.read_only_enforcement("totally-unknown") == "none"


def _roster() -> Roster:
    return Roster(
        orchestrator="lead",
        agents={
            "lead": Agent(name="lead", cli="claude", role="orchestrator"),
            "safe": Agent(name="safe", cli="codex", role="builder"),  # hard
            "soft": Agent(name="soft", cli="goose", role="builder"),  # soft
            "open": Agent(name="open", cli="opencode", role="builder"),  # none
        },
    )


def test_advisory_lists_non_hard_agents_including_orchestrator():
    lines = run_cli._read_only_advisory(_roster(), None)
    joined = "\n".join(lines)
    assert "soft (goose)" in joined
    assert "open (opencode)" in joined
    assert "safe (codex)" not in joined  # natively sandboxed, hard-enforced
    assert "lead (claude)" in joined  # the orchestrator runs too and claude does not enforce read-only


def test_writable_sandbox_override_downgrades_hard_agents():
    lines = run_cli._read_only_advisory(_roster(), "workspace-write")
    joined = "\n".join(lines)
    # the native sandbox is overridden, so even codex is now best-effort
    assert "safe (codex)" in joined
    assert "soft (goose)" in joined


def test_direct_worker_advisory_only_checks_selected_seat():
    lines = run_cli._read_only_advisory(_roster(), None, worker="safe")
    assert lines == []

    lines = run_cli._read_only_advisory(_roster(), None, worker="open")
    assert len(lines) == 1
    assert "open (opencode)" in lines[0]
