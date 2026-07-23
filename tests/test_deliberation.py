import json

import pytest

from brigade import aboyeur
from brigade import agents
from brigade import cli
from brigade import deliberation
from brigade import runs_cmd
from brigade.roster import Agent, Roster
from brigade.run_transport import Assignment, WorkerResult


def _roster(*, max_workers: int = 3):
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan and synthesize"),
            "coder": Agent("coder", "ollama:llama3.3", "write code"),
            "reviewer": Agent("reviewer", "codex", "review code"),
            "analyst": Agent("analyst", "codex", "analyze tradeoffs"),
        },
        max_workers=max_workers,
    )


def _scope(kind: str, reference: str, *, status: str = "valid", grounded: bool = True) -> deliberation.EvidenceScope:
    return deliberation.EvidenceScope(
        kind=kind,
        reference=reference,
        query=f"{kind} {reference}",
        text=f"scope:{kind}:{reference}",
        grounded=grounded,
        status=status,
    )


def test_default_run_does_not_write_deliberation_artifact(monkeypatch, tmp_path):
    output_dir = tmp_path / "run"

    def fake_run_agent(cli_ref, prompt, **kwargs):
        if len(fake_run_agent.calls) == 0:
            fake_run_agent.calls.append(1)
            return agents.AgentResult(
                text=json.dumps({"assignments": [{"stage": 1, "worker": "coder", "task": "implement"}]}),
                ok=True,
            )
        if cli_ref == "ollama:llama3.3":
            return agents.AgentResult(text="worker output", ok=True)
        return agents.AgentResult(text="final answer", ok=True)

    fake_run_agent.calls = []
    monkeypatch.setattr(aboyeur.agents, "run_agent", fake_run_agent)

    rc = aboyeur.run(
        "ship feature",
        _roster(),
        output_dir=output_dir,
        code_graph_enabled=False,
        route_enabled=False,
        deliberation=False,
    )
    assert rc == 0
    assert not (output_dir / "deliberation.json").exists()
    plan = json.loads((output_dir / "plan.json").read_text())
    assert plan.get("mode") is None


def test_cli_passes_deliberation_flag_to_aboyeur(tmp_path, monkeypatch):
    config_dir = tmp_path / ".brigade"
    config_dir.mkdir()
    (config_dir / "roster.toml").write_text(
        """
orchestrator = "chef"

[agents.chef]
cli = "codex"
role = "plan"

[agents.coder]
cli = "codex"
role = "code"

[agents.reviewer]
cli = "codex"
role = "review"
"""
    )
    seen = {}

    def fake_run(task, loaded_roster, **kwargs):
        seen["deliberation"] = kwargs.get("deliberation")
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(aboyeur, "run", fake_run)
    assert cli.main(["run", "decide migration", "--deliberate"]) == 0
    assert seen["deliberation"] is True


def test_mark_duplicate_scopes_flags_later_entries():
    scopes = [
        _scope("graphtrail-callers", "module.fn"),
        _scope("graphtrail-callers", "module.fn"),
        _scope("graphtrail-callees", "module.fn"),
    ]
    marked = deliberation.mark_duplicate_scopes(scopes)
    assert marked[0].status == "valid"
    assert marked[1].status == "duplicate"
    assert marked[2].status == "valid"


def test_build_plan_records_invalid_role_label_lens(monkeypatch, tmp_path):
    scopes = [
        _scope("graphtrail-context", "task-a"),
        _scope("graphtrail-callers", "module.fn"),
    ]
    monkeypatch.setattr(deliberation, "derive_evidence_scopes", lambda cwd, task, count=3: scopes)
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan"),
            "coder": Agent("coder", "codex", "code"),
            "reviewer": Agent("reviewer", "codex", "review"),
            "analyst": Agent("analyst", "codex", "analyze"),
            "architect": Agent("architect", "codex", "architecture"),
        },
        max_workers=4,
    )
    plan = deliberation.build_plan(roster, "choose datastore", cwd=tmp_path, perspective_count=3)
    assert len(plan.invalid_lenses) == 1
    assert plan.invalid_lenses[0].scope.kind == "role-label"
    perspective_workers = [lens.worker for lens in plan.lenses if lens.role == "perspective"]
    assert len(perspective_workers) == 2
    assert plan.lenses[-1].role == "challenger"
    assert plan.lenses[-1].stage == deliberation.CHALLENGER_STAGE


def test_build_plan_requires_two_grounded_scopes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        deliberation,
        "derive_evidence_scopes",
        lambda cwd, task, count=3: [_scope("role-label", "only-role", grounded=False, status="invalid")],
    )
    with pytest.raises(ValueError, match="GraphTrail"):
        deliberation.build_plan(_roster(), "choose auth model", cwd=tmp_path)


def test_challenger_prompt_receives_all_perspectives():
    plan = deliberation.DeliberationPlan(
        decision="pick queue",
        assignments=(
            Assignment(worker="coder", task="perspective", stage=1),
            Assignment(worker="reviewer", task="challenge", stage=2),
        ),
        lenses=(
            deliberation.DeliberationLens(
                worker="coder",
                stage=1,
                role="perspective",
                scope=_scope("graphtrail-context", "queue"),
                task="perspective",
            ),
            deliberation.DeliberationLens(
                worker="reviewer",
                stage=2,
                role="challenger",
                scope=_scope("deliberation-challenger", "consensus-attack"),
                task="challenge",
            ),
        ),
        invalid_lenses=(),
    )
    prior = [
        WorkerResult(worker="coder", task="perspective", text='{"position":"use redis"}', ok=True),
        WorkerResult(worker="analyst", task="perspective", text='{"position":"use nats"}', ok=True),
    ]
    prompt = deliberation.build_worker_prompt(
        Agent("reviewer", "codex", "review"),
        Assignment(worker="reviewer", task="challenge", stage=2),
        plan=plan,
        prior_results=prior,
        read_only=True,
        read_only_policy="READ ONLY",
    )
    assert "Independent perspectives" in prompt
    assert "use redis" in prompt
    assert "use nats" in prompt


def test_deliberation_run_orders_challenger_last_and_writes_artifact(monkeypatch, tmp_path):
    output_dir = tmp_path / "run"
    scopes = [
        _scope("graphtrail-context", "migration"),
        _scope("graphtrail-callers", "migrate.run"),
    ]
    monkeypatch.setattr(deliberation, "derive_evidence_scopes", lambda cwd, task, count=3: scopes)
    dispatch_calls = []

    def fake_dispatch(assignments, roster, **kwargs):
        dispatch_calls.append([(assignment.stage, assignment.worker) for assignment in assignments])
        results = []
        for assignment in assignments:
            if assignment.stage == 1:
                body = json.dumps(
                    {
                        "position": f"{assignment.worker} favors option {assignment.worker}",
                        "assumptions": [f"{assignment.worker}-assumption"],
                        "evidence_references": [f"{assignment.worker}-evidence"],
                        "agreements": ["shared-agreement"],
                        "conflicts": [f"{assignment.worker}-conflict"],
                    }
                )
            else:
                body = json.dumps(
                    {
                        "attacks": ["majority ignores rollback"],
                        "minority_report": "keep the old path for one release",
                        "recommendation": "dual-write then cut over",
                        "confidence": "medium",
                        "unresolved_conflicts": ["rollback window"],
                        "agreements": ["shared-agreement"],
                    }
                )
            results.append(WorkerResult(worker=assignment.worker, task=assignment.task, text=body, ok=True))
        return results

    def fake_orchestrator(roster, prompt, **kwargs):
        assert "Deliberation artifact" in prompt
        return agents.AgentResult(text="final recommendation", ok=True)

    monkeypatch.setattr(aboyeur, "dispatch", fake_dispatch)
    monkeypatch.setattr(aboyeur, "_run_orchestrator", fake_orchestrator)

    rc = aboyeur.run(
        "choose migration strategy",
        _roster(),
        output_dir=output_dir,
        cwd=tmp_path,
        code_graph_enabled=False,
        route_enabled=False,
        deliberation=True,
    )
    assert rc == 0
    assert dispatch_calls
    stages = dispatch_calls[0]
    assert stages[:2] == [(1, "coder"), (1, "reviewer")]
    assert stages[-1][0] == deliberation.CHALLENGER_STAGE

    artifact = json.loads((output_dir / "deliberation.json").read_text())
    deliberation.validate_schema(artifact)
    assert artifact["schema"] == deliberation.SCHEMA
    assert artifact["minority_report"] == "keep the old path for one release"
    assert artifact["recommendation"] == "dual-write then cut over"
    assert artifact["confidence"] == "medium"
    assert len(artifact["perspectives"]) == 2
    kinds = {item["evidence_scope"]["kind"] for item in artifact["perspectives"]}
    assert kinds == {"graphtrail-context", "graphtrail-callers"}
    assert artifact["challenger"]["worker"] == "analyst"


def test_validate_schema_requires_two_grounded_graphtrail_perspectives():
    base = {
        "schema": deliberation.SCHEMA,
        "decision": "pick queue",
        "challenger": {
            "worker": "reviewer",
            "stage": 2,
            "attacks": [],
            "minority_report": "minority",
            "recommendation": "ship",
            "confidence": "medium",
            "unresolved_conflicts": [],
            "agreements": [],
            "raw_output": "",
        },
        "agreements": [],
        "unresolved_conflicts": [],
        "assumptions": [],
        "evidence_references": [],
        "minority_report": "minority",
        "recommendation": "ship",
        "confidence": "medium",
        "invalid_lenses": [],
    }
    perspective = {
        "worker": "coder",
        "stage": 1,
        "evidence_scope": {
            "kind": "graphtrail-context",
            "reference": "queue",
            "query": "queue",
            "grounded": True,
            "status": "valid",
        },
        "position": "redis",
        "assumptions": [],
        "evidence_references": [],
        "agreements": [],
        "conflicts": [],
        "raw_output": "",
    }
    with pytest.raises(ValueError, match="2 to 3"):
        deliberation.validate_schema({**base, "perspectives": [perspective]})


def test_deliberation_run_resume_unavailable(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"deliberation": True, "status": "failed"}))
    (run_dir / "plan.json").write_text(json.dumps({"mode": "deliberation"}))
    assert runs_cmd._resume_available(run_dir) is False


def test_deliberation_dispatch_passes_prior_results_to_challenger(monkeypatch, tmp_path):
    scopes = [
        _scope("graphtrail-context", "migration"),
        _scope("graphtrail-callers", "migrate.run"),
    ]
    monkeypatch.setattr(deliberation, "derive_evidence_scopes", lambda cwd, task, count=3: scopes)
    plan = deliberation.build_plan(_roster(), "choose migration strategy", cwd=tmp_path)
    prompts: list[tuple[int, str, str]] = []

    def fake_run_agent(cli_ref, prompt, **kwargs):
        if "Independent perspectives" in prompt:
            prompts.append((2, cli_ref, prompt))
        return agents.AgentResult(text='{"position":"ok"}', ok=True)

    monkeypatch.setattr(agents, "run_agent", fake_run_agent)

    aboyeur.dispatch(
        list(plan.assignments),
        _roster(),
        build_prompt=deliberation.make_prompt_builder(plan),
        cwd=tmp_path,
    )
    assert prompts
    assert "Independent perspectives" in prompts[0][2]
    assert "ok" in prompts[0][2]


def test_runs_show_and_watch_surface_deliberation(tmp_path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run.v1",
                "status": "ok",
                "task": "choose auth",
                "started_at": "2026-07-22T00:00:00Z",
                "finished_at": "2026-07-22T00:01:00Z",
                "duration_seconds": 60,
            }
        )
    )
    (run_dir / "deliberation.json").write_text(
        json.dumps(
            {
                "schema": deliberation.SCHEMA,
                "decision": "choose auth",
                "perspectives": [
                    {
                        "worker": "coder",
                        "stage": 1,
                        "evidence_scope": {
                            "kind": "graphtrail-context",
                            "reference": "auth",
                            "query": "auth",
                            "grounded": True,
                            "status": "valid",
                        },
                        "position": "use oauth",
                        "assumptions": [],
                        "evidence_references": [],
                        "agreements": [],
                        "conflicts": [],
                        "raw_output": "",
                    },
                    {
                        "worker": "reviewer",
                        "stage": 1,
                        "evidence_scope": {
                            "kind": "graphtrail-callers",
                            "reference": "auth.login",
                            "query": "callers auth.login",
                            "grounded": True,
                            "status": "valid",
                        },
                        "position": "use sessions",
                        "assumptions": [],
                        "evidence_references": [],
                        "agreements": [],
                        "conflicts": [],
                        "raw_output": "",
                    },
                ],
                "challenger": {
                    "worker": "reviewer",
                    "stage": 2,
                    "attacks": [],
                    "minority_report": "session cookies still required",
                    "recommendation": "oauth with cookie fallback",
                    "confidence": "high",
                    "unresolved_conflicts": [],
                    "agreements": [],
                    "raw_output": "",
                },
                "agreements": [],
                "unresolved_conflicts": [],
                "assumptions": [],
                "evidence_references": [],
                "minority_report": "session cookies still required",
                "recommendation": "oauth with cookie fallback",
                "confidence": "high",
                "invalid_lenses": [],
            }
        )
    )

    assert runs_cmd.show(run_dir) == 0
    out = capsys.readouterr().out
    assert "deliberation:" in out
    assert "minority_report: session cookies still required" in out
    assert "oauth with cookie fallback" in out

    capsys.readouterr()
    meta, code = runs_cmd._poll_watch_artifacts(run_dir, {}, {}, json_output=False)
    assert code is None
    out = capsys.readouterr().out
    assert "deliberation:" in out
    assert meta is not None
