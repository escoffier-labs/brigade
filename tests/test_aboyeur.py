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


def _timeout_roster():
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan and synthesize", timeout_seconds=45.0),
            "coder": Agent("coder", "ollama:llama3.3", "write code"),
        },
        max_workers=1,
        timeout_seconds=12.0,
    )


def _model_roster():
    return Roster(
        orchestrator="architect",
        agents={
            "architect": Agent("architect", "claude", "plan and synthesize", model="claude-fable-5"),
            "builder": Agent("builder", "codex", "write code", model="gpt-5.5-codex"),
        },
        max_workers=1,
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


def test_parse_plan_accepts_staged_json_and_defaults_missing_stage():
    plan = aboyeur.parse_plan(
        json.dumps(
            {
                "assignments": [
                    {"stage": 2, "worker": "reviewer", "task": "review it"},
                    {"worker": "coder", "task": "implement it"},
                ]
            }
        ),
        _roster(),
    )
    assert plan == [
        aboyeur.Assignment(worker="coder", task="implement it", stage=1),
        aboyeur.Assignment(worker="reviewer", task="review it", stage=2),
    ]


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


def test_parse_plan_rejects_invalid_stage():
    for stage in (0, -1, "2", True):
        try:
            aboyeur.parse_plan(
                json.dumps({"assignments": [{"stage": stage, "worker": "coder", "task": "implement it"}]}),
                _roster(),
            )
        except ValueError as exc:
            assert "assignment.stage" in str(exc)
        else:
            raise AssertionError(f"expected ValueError for stage {stage!r}")


def test_parse_plan_deduplicates_by_stage_and_limits_each_stage():
    plan = aboyeur.parse_plan(
        json.dumps(
            {
                "assignments": [
                    {"stage": 1, "worker": "coder", "task": "implement it"},
                    {"stage": 1, "worker": "coder", "task": "implement it"},
                    {"stage": 2, "worker": "coder", "task": "implement it"},
                    {"stage": 2, "worker": "reviewer", "task": "review it"},
                ]
            }
        ),
        _roster(),
    )
    assert plan == [
        aboyeur.Assignment(worker="coder", task="implement it", stage=1),
        aboyeur.Assignment(worker="coder", task="implement it", stage=2),
        aboyeur.Assignment(worker="reviewer", task="review it", stage=2),
    ]

    try:
        aboyeur.parse_plan(
            json.dumps(
                {
                    "assignments": [
                        {"stage": 1, "worker": "coder", "task": "implement it"},
                        {"stage": 1, "worker": "reviewer", "task": "review it"},
                        {"stage": 1, "worker": "coder", "task": "test it"},
                    ]
                }
            ),
            _roster(),
        )
    except ValueError as exc:
        assert "stage 1" in str(exc)
        assert "limit is 2" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_build_plan_prompt_describes_stage_contract():
    prompt = aboyeur.build_plan_prompt("build feature", _roster())
    assert '"stage":1' in prompt
    assert "stage 1" in prompt
    assert "same stage run in parallel" in prompt
    assert "later stages receive earlier-stage worker results" in prompt


def test_worker_prompt_without_prior_context_keeps_original_contract():
    assignment = aboyeur.Assignment(worker="coder", task="implement it")
    prompt = aboyeur._worker_prompt(_roster().agents["coder"], assignment)
    assert "Sub-task:\nimplement it" in prompt
    assert "Return a concise, complete result for the orchestrator to synthesize." in prompt
    assert "Earlier-stage context" not in prompt


def test_assignment_payload_serializes_stage():
    payload = aboyeur._assignment_payload([aboyeur.Assignment(worker="coder", task="implement it", stage=2)])
    assert payload == [{"stage": 2, "worker": "coder", "task": "implement it"}]


def test_run_dry_run_stops_after_plan(monkeypatch, capsys):
    calls = []

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False):
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

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False):
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


def test_run_dispatches_stages_in_order_with_earlier_context(monkeypatch):
    calls = []

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False):
        calls.append((cli_ref, prompt))
        if len(calls) == 1:
            return agents.AgentResult(
                text=json.dumps(
                    {
                        "assignments": [
                            {"stage": 2, "worker": "reviewer", "task": "review it"},
                            {"stage": 1, "worker": "coder", "task": "implement it"},
                        ]
                    }
                ),
                ok=True,
            )
        if cli_ref == "ollama:llama3.3":
            return agents.AgentResult(text="implementation output", ok=True)
        if "Sub-task:\nreview it" in prompt:
            return agents.AgentResult(text="review output", ok=True)
        return agents.AgentResult(text="final answer", ok=True)

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    assert aboyeur.run("build feature", _roster()) == 0

    worker_calls = [call for call in calls if "You are Brigade worker" in call[1]]
    assert [call[0] for call in worker_calls] == ["ollama:llama3.3", "codex"]
    assert "Earlier-stage context" not in worker_calls[0][1]
    assert "Earlier-stage context" in worker_calls[1][1]
    assert "implementation output" in worker_calls[1][1]
    assert calls[-1][0] == "codex"
    assert "implementation output" in calls[-1][1]
    assert "review output" in calls[-1][1]


def test_run_uses_roster_timeouts(monkeypatch):
    calls = []

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False):
        calls.append((cli_ref, timeout))
        if len(calls) == 1:
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"worker": "coder", "task": "implement it"}]}),
                ok=True,
            )
        if cli_ref == "ollama:llama3.3":
            return agents.AgentResult(text="worker output", ok=True)
        return agents.AgentResult(text="final answer", ok=True)

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    assert aboyeur.run("build feature", _timeout_roster()) == 0
    assert calls == [("codex", 45.0), ("ollama:llama3.3", 12.0), ("codex", 45.0)]


def test_run_passes_agent_models(monkeypatch):
    calls = []

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False, model=None):
        calls.append((cli_ref, model))
        if len(calls) == 1:
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"worker": "builder", "task": "implement it"}]}),
                ok=True,
            )
        if cli_ref == "codex":
            return agents.AgentResult(text="worker output", ok=True)
        return agents.AgentResult(text="final answer", ok=True)

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    assert aboyeur.run("build feature", _model_roster()) == 0
    assert calls == [
        ("claude", "claude-fable-5"),
        ("codex", "gpt-5.5-codex"),
        ("claude", "claude-fable-5"),
    ]


def test_roster_payload_includes_model():
    payload = aboyeur._roster_payload(_model_roster())
    assert payload["agents"]["architect"]["model"] == "claude-fable-5"
    assert payload["agents"]["builder"]["model"] == "gpt-5.5-codex"


def test_read_only_mode_is_in_all_prompts_and_artifacts(monkeypatch, tmp_path):
    calls = []

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False):
        calls.append((cli_ref, prompt, read_only))
        if len(calls) == 1:
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"worker": "coder", "task": "inspect it"}]}),
                ok=True,
            )
        if cli_ref == "ollama:llama3.3":
            return agents.AgentResult(text="worker output", ok=True)
        return agents.AgentResult(text="final answer", ok=True)

    output_dir = tmp_path / "run"
    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    assert aboyeur.run("inspect feature", _roster(), output_dir=output_dir, read_only=True) == 0
    assert all("READ-ONLY MODE" in prompt for _, prompt, _ in calls)
    assert all("Do not modify files" in prompt for _, prompt, _ in calls)
    assert all(read_only for _, _, read_only in calls)
    assert json.loads((output_dir / "run.json").read_text())["read_only"] is True


def test_prompt_read_only_can_disable_native_sandbox(monkeypatch):
    calls = []

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False):
        calls.append((cli_ref, prompt, read_only))
        if len(calls) == 1:
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"worker": "coder", "task": "inspect it"}]}),
                ok=True,
            )
        if cli_ref == "ollama:llama3.3":
            return agents.AgentResult(text="worker output", ok=True)
        return agents.AgentResult(text="final answer", ok=True)

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    assert aboyeur.run("inspect feature", _roster(), read_only=True, sandbox_read_only=False) == 0
    assert all("READ-ONLY MODE" in prompt for _, prompt, _ in calls)
    assert all(read_only is False for _, _, read_only in calls)


def test_prompt_read_only_can_set_explicit_sandbox(monkeypatch):
    calls = []

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False, sandbox=None):
        calls.append((cli_ref, prompt, read_only, sandbox))
        if len(calls) == 1:
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"worker": "coder", "task": "inspect it"}]}),
                ok=True,
            )
        if cli_ref == "ollama:llama3.3":
            return agents.AgentResult(text="worker output", ok=True)
        return agents.AgentResult(text="final answer", ok=True)

    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    assert aboyeur.run("inspect feature", _roster(), read_only=True, sandbox="danger-full-access") == 0
    assert all("READ-ONLY MODE" in prompt for _, prompt, _, _ in calls)
    assert all(sandbox == "danger-full-access" for _, _, _, sandbox in calls)


def test_show_plan_prints_assignments(monkeypatch, capsys):
    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False):
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
    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False):
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

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False):
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

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False):
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
    assert (output_dir / "plan-attempts.json").is_file()
    assert (output_dir / "roster.json").is_file()
    assert (output_dir / "worker-results.json").is_file()
    assert (output_dir / "synthesis.json").is_file()
    assert (output_dir / "final.txt").read_text() == "final answer\n"
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "ok"
    assert run_meta["cwd"] == str(run_cwd)
    assert run_meta["artifacts"] == str(output_dir)
    assert run_meta["started_at"].endswith("Z")
    assert run_meta["finished_at"].endswith("Z")
    assert run_meta["duration_seconds"] >= 0
    roster_meta = json.loads((output_dir / "roster.json").read_text())
    assert roster_meta["orchestrator"] == "chef"
    assert roster_meta["max_workers"] == 2
    assert roster_meta["allow_models"] == []
    assert roster_meta["agents"]["coder"]["cli"] == "ollama:llama3.3"
    plan_attempts = json.loads((output_dir / "plan-attempts.json").read_text())["attempts"]
    assert plan_attempts[0]["stage"] == "initial"
    assert plan_attempts[0]["parsed"] is True
    assert "implement it" in plan_attempts[0]["text"]
    synthesis = json.loads((output_dir / "synthesis.json").read_text())
    assert synthesis["orchestrator"] == "chef"
    assert synthesis["result"]["ok"] is True
    assert synthesis["result"]["text"] == "final answer"
    assert {call[2] for call in calls} == {run_cwd}


def test_dry_run_writes_plan_artifact(monkeypatch, tmp_path):
    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False):
        return agents.AgentResult(
            text=json.dumps({"assignments": [{"worker": "coder", "task": "implement it"}]}),
            ok=True,
        )

    output_dir = tmp_path / "run"
    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)

    assert aboyeur.run("build feature", _roster(), dry_run=True, output_dir=output_dir) == 0
    assert json.loads((output_dir / "plan.json").read_text())["assignments"][0]["worker"] == "coder"
    assert json.loads((output_dir / "plan-attempts.json").read_text())["attempts"][0]["parsed"] is True
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "dry-run"
    assert run_meta["artifacts"] == str(output_dir)
    assert run_meta["started_at"].endswith("Z")
    assert run_meta["finished_at"].endswith("Z")
    assert run_meta["duration_seconds"] >= 0
    assert json.loads((output_dir / "roster.json").read_text())["agents"]["chef"]["cli"] == "codex"
    assert not (output_dir / "worker-results.json").exists()
    assert not (output_dir / "synthesis.json").exists()


def test_invalid_plan_writes_attempt_artifact(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False):
        calls.append((cli_ref, prompt))
        return agents.AgentResult(text="not json", ok=True)

    output_dir = tmp_path / "run"
    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)

    assert aboyeur.run("build feature", _roster(), output_dir=output_dir) == 2
    assert "invalid plan" in capsys.readouterr().err
    assert len(calls) == 2
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "failed"
    assert run_meta["artifacts"] == str(output_dir)
    assert run_meta["started_at"].endswith("Z")
    assert run_meta["finished_at"].endswith("Z")
    assert run_meta["duration_seconds"] >= 0
    attempts = json.loads((output_dir / "plan-attempts.json").read_text())["attempts"]
    assert [attempt["stage"] for attempt in attempts] == ["initial", "correction"]
    assert [attempt["parsed"] for attempt in attempts] == [False, False]
    assert all(attempt["text"] == "not json" for attempt in attempts)
    assert all("plan is not valid JSON" in attempt["parse_error"] for attempt in attempts)
    assert not (output_dir / "plan.json").exists()


def test_synthesis_failure_writes_artifact(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False):
        calls.append((cli_ref, prompt))
        if len(calls) == 1:
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"worker": "coder", "task": "implement it"}]}),
                ok=True,
            )
        if cli_ref == "ollama:llama3.3":
            return agents.AgentResult(text="worker output", ok=True)
        return agents.AgentResult(text="partial synthesis", ok=False, detail="synthesis failed")

    output_dir = tmp_path / "run"
    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)

    assert aboyeur.run("build feature", _roster(), output_dir=output_dir) == 2
    assert "synthesis failed" in capsys.readouterr().err
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "failed"
    assert run_meta["artifacts"] == str(output_dir)
    assert run_meta["started_at"].endswith("Z")
    assert run_meta["finished_at"].endswith("Z")
    assert run_meta["duration_seconds"] >= 0
    synthesis = json.loads((output_dir / "synthesis.json").read_text())
    assert synthesis["orchestrator"] == "chef"
    assert synthesis["result"] == {
        "ok": False,
        "detail": "synthesis failed",
        "text": "partial synthesis",
    }
    assert not (output_dir / "final.txt").exists()


def test_run_writes_handoff(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False):
        calls.append((cli_ref, prompt))
        if len(calls) == 1:
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"worker": "coder", "task": "implement it"}]}),
                ok=True,
            )
        if cli_ref == "ollama:llama3.3":
            return agents.AgentResult(text="worker output", ok=True)
        return agents.AgentResult(text="final answer\n## model heading", ok=True)

    inbox = tmp_path / ".claude" / "memory-handoffs"
    output_dir = tmp_path / "run"
    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)

    assert (
        aboyeur.run(
            "build feature\n## task heading",
            _roster(),
            output_dir=output_dir,
            handoff_inbox=inbox,
            read_only=True,
        )
        == 0
    )
    handoffs = list(inbox.glob("*-brigade-run-build-feature-task-heading.md"))
    assert len(handoffs) == 1
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "ok"
    assert run_meta["handoff"] == str(handoffs[0])
    assert run_meta["artifacts"] == str(output_dir)
    body = handoffs[0].read_text()
    assert "## Recommended memory action\n\nno-card" in body
    assert "## Target document\n\n.learnings/LEARNINGS.md" in body
    assert "- mode: read-only" in body
    assert "final answer" in body
    assert "\n## task heading" not in body
    assert "\n## model heading" not in body
    assert "\n### model heading" in body
    assert "handoff:" in capsys.readouterr().err


def test_handoff_failure_preserves_final_artifacts(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False):
        calls.append((cli_ref, prompt))
        if len(calls) == 1:
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"worker": "coder", "task": "implement it"}]}),
                ok=True,
            )
        if cli_ref == "ollama:llama3.3":
            return agents.AgentResult(text="worker output", ok=True)
        return agents.AgentResult(text="final answer", ok=True)

    def fail_handoff(*args, **kwargs):
        raise OSError("cannot write handoff")

    output_dir = tmp_path / "run"
    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)
    monkeypatch.setattr(aboyeur, "write_run_handoff", fail_handoff)

    assert aboyeur.run("build feature", _roster(), output_dir=output_dir, handoff_inbox=tmp_path / "handoffs") == 2
    captured = capsys.readouterr()
    assert captured.out.strip() == "final answer"
    assert "handoff failed: cannot write handoff" in captured.err
    assert (output_dir / "final.txt").read_text() == "final answer\n"
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "handoff-failed"
    assert run_meta["error"] == "handoff failed: cannot write handoff"
    assert run_meta["artifacts"] == str(output_dir)
    assert run_meta["started_at"].endswith("Z")
    assert run_meta["finished_at"].endswith("Z")
    assert run_meta["duration_seconds"] >= 0


def test_disallowed_worker_is_recorded_not_run(monkeypatch):
    calls = []

    def fake_run_agent(cli_ref, prompt, timeout=600.0, cwd=None, read_only=False):
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
