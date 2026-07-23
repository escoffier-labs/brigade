"""A run whose synthesis succeeds but whose workers failed is 'incomplete', exit 3.

Harness adapted from tests/test_aboyeur.py: the orchestrator plans two stage-1
workers (coder, reviewer); workers named in ``worker_failures`` return
``ok=False`` while the rest return ``ok=True``; synthesis always succeeds.
"""

import json
import re
from types import SimpleNamespace

import pytest

from brigade import aboyeur
from brigade import agents
from brigade.roster import Agent, Roster


def _roster():
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan and synthesize"),
            "coder": Agent("coder", "ollama:llama3.3", "write code"),
            "reviewer": Agent("reviewer", "codex", "review code"),
        },
        max_workers=2,
    )


_WORKER_RE = re.compile(r"^You are Brigade worker (\w+)\.", re.MULTILINE)


@pytest.fixture
def aboyeur_harness(monkeypatch):
    """Drive ``aboyeur.run`` end-to-end with fake agents and selective worker failures."""

    def run_with(*, worker_failures, output_dir):
        failures = set(worker_failures)

        def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False, **kwargs):
            if prompt.startswith("You are Brigade worker "):
                match = _WORKER_RE.search(prompt)
                worker = match.group(1) if match is not None else ""
                if worker in failures:
                    return agents.AgentResult(text="partial output", ok=False, detail="boom")
                return agents.AgentResult(text="worker output", ok=True)
            if prompt.startswith("You are the Brigade orchestrator. Synthesize"):
                return agents.AgentResult(text="final answer", ok=True)
            # plan call: split the task across both workers
            return agents.AgentResult(
                text=json.dumps(
                    {
                        "assignments": [
                            {"worker": "coder", "task": "implement it"},
                            {"worker": "reviewer", "task": "review it"},
                        ]
                    }
                ),
                ok=True,
            )

        monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
        return aboyeur.run(
            "build feature",
            _roster(),
            cwd=output_dir.parent,
            output_dir=output_dir,
            route_enabled=False,
        )

    return SimpleNamespace(run_with=run_with)


def test_worker_failure_yields_incomplete(aboyeur_harness, tmp_path):
    # orchestrator plan -> 2 workers, worker 'reviewer' fails, synthesis returns ok
    rc = aboyeur_harness.run_with(worker_failures={"reviewer"}, output_dir=tmp_path / "run")
    assert rc == 3
    meta = json.loads((tmp_path / "run" / "run.json").read_text())
    assert meta["status"] == "incomplete"
    assert meta["failure_phase"] == "workers"
    assert meta["failure_kind"] == "worker-failure"


def test_all_workers_ok_still_ok(aboyeur_harness, tmp_path):
    rc = aboyeur_harness.run_with(worker_failures=set(), output_dir=tmp_path / "run")
    assert rc == 0
    meta = json.loads((tmp_path / "run" / "run.json").read_text())
    assert meta["status"] == "ok"
