import json

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


def _restricted_roster():
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan and synthesize"),
            "coder": Agent("coder", "ollama:llama3.3", "write code"),
        },
        max_workers=1,
        allow_models=("codex",),
    )


def test_parse_plan_accepts_plain_json():
    plan = aboyeur.parse_plan(
        '{"assignments":[{"worker":"coder","task":"implement it"}]}',
        _roster(),
    )
    assert plan == [aboyeur.Assignment(worker="coder", task="implement it")]


def test_parse_plan_accepts_fenced_json():
    plan = aboyeur.parse_plan(
        'Here is the plan:\n```json\n{"assignments":[{"worker":"reviewer","task":"check it"}]}\n```\nDone.',
        _roster(),
    )
    assert plan == [aboyeur.Assignment(worker="reviewer", task="check it")]


def test_parse_plan_accepts_json_surrounded_by_prose():
    plan = aboyeur.parse_plan(
        'Here is the plan: {"assignments":[{"worker":"coder","task":"implement it"}]} Thanks.',
        _roster(),
    )
    assert plan == [aboyeur.Assignment(worker="coder", task="implement it")]


def test_parse_plan_rejects_orchestrator_assignment():
    try:
        aboyeur.parse_plan('{"assignments":[{"worker":"chef","task":"do it"}]}', _roster())
    except ValueError as exc:
        assert "orchestrator" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_run_dry_run_stops_after_plan(monkeypatch, capsys):
    calls = []

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None):
        calls.append((cli_ref, prompt))
        return agents.AgentResult(
            text=json.dumps({"assignments": [{"worker": "coder", "task": "implement it"}]}),
            ok=True,
        )

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    rc = aboyeur.run("build feature", _roster(), dry_run=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "implement it" in out
    assert len(calls) == 1


def test_run_dispatches_and_synthesizes(monkeypatch, capsys):
    calls = []

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None):
        calls.append((cli_ref, prompt))
        if len(calls) == 1:
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"worker": "coder", "task": "implement it"}]}),
                ok=True,
            )
        if cli_ref == "ollama:llama3.3":
            return agents.AgentResult(text="worker output", ok=True)
        return agents.AgentResult(text="final answer", ok=True)

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    rc = aboyeur.run("build feature", _roster())
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "final answer"
    assert [call[0] for call in calls] == ["codex", "ollama:llama3.3", "codex"]


def test_show_plan_prints_assignments(monkeypatch, capsys):
    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None):
        if "assignments" in prompt:
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"worker": "coder", "task": "implement it"}]}),
                ok=True,
            )
        if cli_ref == "ollama:llama3.3":
            return agents.AgentResult(text="worker output", ok=True)
        return agents.AgentResult(text="final answer", ok=True)

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    rc = aboyeur.run("build feature", _roster(), show_plan=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "plan:" in out
    assert "-> coder: implement it" in out
    assert out.strip().endswith("final answer")


def test_verbose_prints_worker_status(monkeypatch, capsys):
    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None):
        if "assignments" in prompt:
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"worker": "coder", "task": "implement it"}]}),
                ok=True,
            )
        if cli_ref == "ollama:llama3.3":
            return agents.AgentResult(text="worker output", ok=True)
        return agents.AgentResult(text="final answer", ok=True)

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    rc = aboyeur.run("build feature", _roster(), verbose=True)
    out = capsys.readouterr().out
    assert rc == 0
    assert "workers:" in out
    assert "[ok] coder" in out
    assert "synthesis:" in out


def test_worker_failure_is_sent_to_synthesis(monkeypatch):
    calls = []

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None):
        calls.append((cli_ref, prompt))
        if len(calls) == 1:
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"worker": "coder", "task": "implement it"}]}),
                ok=True,
            )
        if cli_ref == "ollama:llama3.3":
            return agents.AgentResult(text="", ok=False, detail="not installed")
        return agents.AgentResult(text="final answer", ok=True)

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    assert aboyeur.run("build feature", _roster()) == 0
    assert "not installed" in calls[-1][1]


def test_run_writes_artifacts(monkeypatch, tmp_path):
    calls = []

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None):
        calls.append((cli_ref, prompt, cwd))
        if len(calls) == 1:
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"worker": "coder", "task": "implement it"}]}),
                ok=True,
            )
        if cli_ref == "ollama:llama3.3":
            return agents.AgentResult(text="worker output", ok=True)
        return agents.AgentResult(text="final answer", ok=True)

    run_cwd = tmp_path / "work"
    run_cwd.mkdir()
    output_dir = tmp_path / "run"
    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)

    assert aboyeur.run("build feature", _roster(), cwd=run_cwd, output_dir=output_dir) == 0
    assert (output_dir / "plan.json").is_file()
    assert (output_dir / "worker-results.json").is_file()
    assert (output_dir / "final.txt").read_text() == "final answer\n"
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "ok"
    assert run_meta["cwd"] == str(run_cwd)
    assert {call[2] for call in calls} == {run_cwd}


def test_dry_run_writes_plan_artifact(monkeypatch, tmp_path):
    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None):
        return agents.AgentResult(
            text=json.dumps({"assignments": [{"worker": "coder", "task": "implement it"}]}),
            ok=True,
        )

    output_dir = tmp_path / "run"
    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)

    assert aboyeur.run("build feature", _roster(), dry_run=True, output_dir=output_dir) == 0
    assert json.loads((output_dir / "plan.json").read_text())["assignments"][0]["worker"] == "coder"
    assert json.loads((output_dir / "run.json").read_text())["status"] == "dry-run"
    assert not (output_dir / "worker-results.json").exists()


def test_disallowed_worker_is_recorded_not_run(monkeypatch):
    calls = []

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None):
        calls.append((cli_ref, prompt))
        if len(calls) == 1:
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"worker": "coder", "task": "implement it"}]}),
                ok=True,
            )
        return agents.AgentResult(text="final answer", ok=True)

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    assert aboyeur.run("build feature", _restricted_roster()) == 0
    assert [call[0] for call in calls] == ["codex", "codex"]
    assert "not allowed by limits.allow_models" in calls[-1][1]
