"""Tests for CandidateSetGate (#504)."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import aboyeur
from brigade import agents
from brigade import candidate_set
from brigade.roster import Agent, Roster
from brigade.run_transport import Assignment
from tests.run_test_helpers import run_aboyeur_guarded


def _roster() -> Roster:
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan and synthesize"),
            "coder": Agent("coder", "codex", "write code"),
        },
        max_workers=1,
    )


def _write_tools_toml(target: Path, body: str) -> None:
    brigade = target / ".brigade"
    brigade.mkdir(parents=True, exist_ok=True)
    (brigade / "tools.toml").write_text(body)


def _stub_run_phases(monkeypatch, assignments: list[Assignment] | None = None) -> None:
    planned = assignments or [Assignment(worker="coder", task="implement")]
    monkeypatch.setattr(aboyeur, "plan", lambda *args, **kwargs: list(planned))
    monkeypatch.setattr(
        aboyeur,
        "dispatch",
        lambda *args, **kwargs: [
            aboyeur.WorkerResult(worker=a.worker, task=a.task, text="done", ok=True)
            for a in (args[0] if args else planned)
        ],
    )
    monkeypatch.setattr(
        aboyeur,
        "_run_orchestrator",
        lambda *args, **kwargs: agents.AgentResult(text="final answer", ok=True),
    )


def test_score_tool_requires_domain_capability_and_risk_ceiling():
    tool = {
        "id": "reader",
        "enabled": True,
        "domain": "code",
        "capability": ["read-files", "search"],
        "risk_class": "read",
    }
    req = candidate_set.ToolRequirements(domain="code", capabilities=("read-files",), max_risk_class="local-write")
    scored = candidate_set.score_tool(tool, req)
    assert scored.admitted is True
    assert scored.score > 0

    mismatch = candidate_set.score_tool(
        {**tool, "domain": "ops"},
        req,
    )
    assert mismatch.admitted is False
    assert "domain-mismatch:ops" in mismatch.reasons

    too_risky = candidate_set.score_tool(
        {**tool, "risk_class": "privileged"},
        req,
    )
    assert too_risky.admitted is False
    assert "risk-too-high:privileged" in too_risky.reasons

    missing_cap = candidate_set.score_tool(
        {**tool, "capability": ["search"]},
        req,
    )
    assert missing_cap.admitted is False
    assert "missing-capability:read-files" in missing_cap.reasons


def test_filter_candidate_set_orders_by_score_then_id():
    tools = [
        {
            "id": "b-tool",
            "enabled": True,
            "domain": "code",
            "capability": ["read-files"],
            "risk_class": "read",
        },
        {
            "id": "a-tool",
            "enabled": True,
            "domain": "code",
            "capability": ["read-files"],
            "risk_class": "read",
        },
        {
            "id": "writer",
            "enabled": True,
            "domain": "code",
            "capability": ["write-files"],
            "risk_class": "local-write",
        },
    ]
    req = candidate_set.ToolRequirements(domain="code", capabilities=("read-files",), max_risk_class="read")
    admissible, rejected = candidate_set.filter_candidate_set(tools, req)
    assert [item.tool_id for item in admissible] == ["a-tool", "b-tool"]
    assert [item.tool_id for item in rejected] == ["writer"]


def test_evaluate_assignments_enforces_only_when_requirements_declared(tmp_path):
    _write_tools_toml(
        tmp_path,
        """
[[tool]]
id = "reader"
name = "Reader"
family = "script"
enabled = true
command = "echo"
domain = "code"
capability = ["read-files"]
risk_class = "read"
""",
    )
    open_assignment = Assignment(worker="coder", task="look around")
    gated = Assignment(
        worker="coder",
        task="read sources",
        domain="ops",
        capabilities=("deploy",),
        max_risk_class="read",
    )
    decision = candidate_set.evaluate_assignments(
        tmp_path,
        [open_assignment, gated],
        run_id="run-test",
    )
    assert decision.steps[0].enforcement is False
    assert decision.steps[0].empty is False
    assert decision.steps[1].enforcement is True
    assert decision.steps[1].empty_required is True
    assert decision.has_empty_required_steps is True


def test_write_candidate_set_receipt_preserves_field_order(tmp_path):
    from datetime import datetime, timezone

    output_dir = tmp_path / "run"
    output_dir.mkdir()
    decision = candidate_set.CandidateSetDecision(
        run_id="run-1",
        decided_at=datetime.now(timezone.utc),
        tool_count=0,
        steps=[],
    )
    path = candidate_set.write_candidate_set_receipt(output_dir, decision)
    text = path.read_text()
    keys = ["schema", "schema_version", "gate_version", "run_id", "decided_at", "tool_count", "steps"]
    positions = [text.index(f'"{key}"') for key in keys]
    assert positions == sorted(positions)
    payload = json.loads(text)
    assert payload["schema"] == candidate_set.CANDIDATE_SET_SCHEMA
    assert payload["schema_version"] == candidate_set.CANDIDATE_SET_SCHEMA_VERSION


def test_parse_plan_accepts_tool_gate_fields():
    plan = aboyeur.parse_plan(
        json.dumps(
            {
                "assignments": [
                    {
                        "worker": "coder",
                        "task": "inspect",
                        "domain": "code",
                        "capabilities": ["read-files", "read-files", "search"],
                        "max_risk_class": "read",
                    }
                ]
            }
        ),
        _roster(),
    )
    assert plan == [
        Assignment(
            worker="coder",
            task="inspect",
            domain="code",
            capabilities=("read-files", "search"),
            max_risk_class="read",
        )
    ]


def test_parse_plan_rejects_invalid_max_risk_class():
    try:
        aboyeur.parse_plan(
            '{"assignments":[{"worker":"coder","task":"x","max_risk_class":"nuclear"}]}',
            _roster(),
        )
    except ValueError as exc:
        assert "max_risk_class" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_assignment_payload_includes_gate_fields():
    payload = aboyeur._assignment_payload(
        [
            Assignment(
                worker="coder",
                task="inspect",
                domain="code",
                capabilities=("read-files",),
                max_risk_class="read",
                admissible_tool_ids=("reader",),
            )
        ]
    )
    assert payload == [
        {
            "stage": 1,
            "worker": "coder",
            "task": "inspect",
            "domain": "code",
            "capabilities": ["read-files"],
            "max_risk_class": "read",
            "admissible_tool_ids": ["reader"],
        }
    ]


def test_worker_prompt_lists_admissible_tools():
    prompt = aboyeur._worker_prompt(
        Agent("coder", "codex", "write code"),
        Assignment(
            worker="coder",
            task="inspect",
            admissible_tool_ids=("reader", "searcher"),
        ),
    )
    assert "Admissible portable tools" in prompt
    assert "reader, searcher" in prompt


def test_run_writes_candidate_set_receipt(monkeypatch, tmp_path):
    _stub_run_phases(monkeypatch)
    _write_tools_toml(
        tmp_path,
        """
[[tool]]
id = "reader"
name = "Reader"
family = "script"
enabled = true
command = "echo"
domain = "code"
capability = ["read-files"]
risk_class = "read"
""",
    )
    output_dir = tmp_path / "run-cs"
    assert (
        run_aboyeur_guarded(
            "build feature",
            _roster(),
            cwd=tmp_path,
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )
    receipt = json.loads((output_dir / "candidate-set.json").read_text())
    assert receipt["schema"] == candidate_set.CANDIDATE_SET_SCHEMA
    assert receipt["run_id"] == "run-cs"
    assert receipt["tool_count"] == 1
    assert receipt["steps"][0]["admissible"][0]["tool_id"] == "reader"
    plan = json.loads((output_dir / "plan.json").read_text())
    assert plan["assignments"][0]["admissible_tool_ids"] == ["reader"]


def test_run_fails_closed_when_required_candidate_set_empty(monkeypatch, tmp_path):
    gated = Assignment(
        worker="coder",
        task="deploy",
        domain="ops",
        capabilities=("deploy",),
        max_risk_class="read",
    )
    _stub_run_phases(monkeypatch, assignments=[gated])
    # Replan still returns the same empty-gated assignment.
    monkeypatch.setattr(
        aboyeur,
        "_run_orchestrator",
        lambda *args, **kwargs: agents.AgentResult(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "worker": "coder",
                            "task": "deploy",
                            "domain": "ops",
                            "capabilities": ["deploy"],
                            "max_risk_class": "read",
                        }
                    ]
                }
            ),
            ok=True,
        ),
    )
    _write_tools_toml(
        tmp_path,
        """
[[tool]]
id = "reader"
name = "Reader"
family = "script"
enabled = true
command = "echo"
domain = "code"
capability = ["read-files"]
risk_class = "read"
""",
    )
    output_dir = tmp_path / "run-empty"
    dispatched = {"called": False}

    def _dispatch(*args, **kwargs):
        dispatched["called"] = True
        return []

    monkeypatch.setattr(aboyeur, "dispatch", _dispatch)
    rc = run_aboyeur_guarded(
        "deploy",
        _roster(),
        cwd=tmp_path,
        output_dir=output_dir,
        code_graph_enabled=False,
        route_enabled=False,
    )
    assert rc == 2
    assert dispatched["called"] is False
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "failed"
    assert run_meta["failure"]["kind"] == candidate_set.FAILURE_KIND
    assert run_meta["failure"]["phase"] == candidate_set.FAILURE_PHASE
    assert run_meta["failure_phase"] == candidate_set.FAILURE_PHASE
    receipt = json.loads((output_dir / "candidate-set.json").read_text())
    assert receipt["empty_required_steps"]
    attempts = json.loads((output_dir / "plan-attempts.json").read_text())
    stages = [entry.get("stage") for entry in attempts["attempts"]]
    assert "candidate-set-gate" in stages


def test_run_replans_when_candidate_set_becomes_admissible(monkeypatch, tmp_path):
    gated = Assignment(
        worker="coder",
        task="read sources",
        domain="ops",
        capabilities=("deploy",),
        max_risk_class="read",
    )
    _stub_run_phases(monkeypatch, assignments=[gated])
    monkeypatch.setattr(
        aboyeur,
        "_run_orchestrator",
        lambda *args, **kwargs: agents.AgentResult(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "worker": "coder",
                            "task": "read sources",
                            "domain": "code",
                            "capabilities": ["read-files"],
                            "max_risk_class": "read",
                        }
                    ]
                }
            ),
            ok=True,
        ),
    )
    _write_tools_toml(
        tmp_path,
        """
[[tool]]
id = "reader"
name = "Reader"
family = "script"
enabled = true
command = "echo"
domain = "code"
capability = ["read-files"]
risk_class = "read"
""",
    )
    output_dir = tmp_path / "run-replan"
    assert (
        run_aboyeur_guarded(
            "read sources",
            _roster(),
            cwd=tmp_path,
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )
    receipt = json.loads((output_dir / "candidate-set.json").read_text())
    assert receipt["steps"][0]["admissible"][0]["tool_id"] == "reader"
    assert "empty_required_steps" not in receipt
    plan = json.loads((output_dir / "plan.json").read_text())
    assert plan["assignments"][0]["domain"] == "code"
    assert plan["assignments"][0]["admissible_tool_ids"] == ["reader"]


def test_score_tool_rejects_malformed_enabled_in_raw_catalog():
    tool = {
        "id": "reader",
        "raw": {
            "id": "reader",
            "name": "Reader",
            "family": "script",
            "enabled": "yes",
            "command": "echo",
            "domain": "code",
            "capability": ["read-files"],
            "risk_class": "read",
        },
        "domain": "code",
        "capability": ["read-files"],
        "risk_class": "read",
    }
    req = candidate_set.ToolRequirements(
        domain="code",
        capabilities=("read-files",),
        max_risk_class="read",
    )
    scored = candidate_set.score_tool(tool, req)
    assert scored.admitted is False
    assert scored.reasons == ("invalid-enabled",)


def test_score_tool_omitted_enabled_remains_admissible():
    tool = {
        "id": "reader",
        "raw": {
            "id": "reader",
            "name": "Reader",
            "family": "script",
            "command": "echo",
            "domain": "code",
            "capability": ["read-files"],
            "risk_class": "read",
        },
        "enabled": True,
        "domain": "code",
        "capability": ["read-files"],
        "risk_class": "read",
    }
    req = candidate_set.ToolRequirements(
        domain="code",
        capabilities=("read-files",),
        max_risk_class="read",
    )
    scored = candidate_set.score_tool(tool, req)
    assert scored.admitted is True


def test_score_tool_enabled_false_still_rejects():
    tool = {
        "id": "reader",
        "raw": {
            "id": "reader",
            "name": "Reader",
            "family": "script",
            "enabled": False,
            "command": "echo",
            "domain": "code",
            "capability": ["read-files"],
            "risk_class": "read",
        },
        "enabled": False,
        "domain": "code",
        "capability": ["read-files"],
        "risk_class": "read",
    }
    req = candidate_set.ToolRequirements(
        domain="code",
        capabilities=("read-files",),
        max_risk_class="read",
    )
    scored = candidate_set.score_tool(tool, req)
    assert scored.admitted is False
    assert scored.reasons == ("disabled",)


def test_malformed_enabled_cannot_satisfy_active_requirement(tmp_path):
    _write_tools_toml(
        tmp_path,
        """
[[tool]]
id = "reader"
name = "Reader"
family = "script"
enabled = "yes"
command = "echo"
domain = "code"
capability = ["read-files"]
risk_class = "read"
""",
    )
    gated = Assignment(
        worker="coder",
        task="read sources",
        domain="code",
        capabilities=("read-files",),
        max_risk_class="read",
    )
    decision = candidate_set.evaluate_assignments(
        tmp_path,
        [gated],
        run_id="run-malformed-enabled",
    )
    assert decision.catalog_errors
    assert decision.steps[0].empty_required is True
    rejected = decision.steps[0].rejected
    assert any(score.tool_id == "reader" and "invalid-enabled" in score.reasons for score in rejected)


def test_candidate_set_corrective_replan_preserves_no_file_write_rule(monkeypatch, tmp_path):
    gated = Assignment(
        worker="coder",
        task="read sources",
        domain="ops",
        capabilities=("deploy",),
        max_risk_class="read",
    )
    _stub_run_phases(monkeypatch, assignments=[gated])
    prompts: list[str] = []

    def fake_run_orchestrator(roster, prompt, **kwargs):
        prompts.append(prompt)
        return agents.AgentResult(
            text=json.dumps(
                {
                    "assignments": [
                        {
                            "worker": "coder",
                            "task": "read sources",
                            "domain": "code",
                            "capabilities": ["read-files"],
                            "max_risk_class": "read",
                        }
                    ]
                }
            ),
            ok=True,
        )

    monkeypatch.setattr(aboyeur, "_run_orchestrator", fake_run_orchestrator)
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan and synthesize"),
            "coder": Agent("coder", "codex", "write code"),
        },
        max_workers=1,
    )
    _write_tools_toml(
        tmp_path,
        """
[[tool]]
id = "reader"
name = "Reader"
family = "script"
enabled = true
command = "echo"
domain = "code"
capability = ["read-files"]
risk_class = "read"
""",
    )
    output_dir = tmp_path / "run-replan-readonly"
    assert (
        run_aboyeur_guarded(
            "read sources",
            roster,
            cwd=tmp_path,
            output_dir=output_dir,
            read_only=True,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )
    assert len(prompts) >= 1
    corrective_prompts = [prompt for prompt in prompts if "Correction needed:" in prompt]
    assert len(corrective_prompts) == 1
    assert aboyeur.NO_PLAN_FILE_RULE in corrective_prompts[0]


def test_load_config_parses_domain_capability_risk_class(tmp_path):
    from brigade.tools_cmd.config import _load_config

    _write_tools_toml(
        tmp_path,
        """
[[tool]]
id = "reader"
name = "Reader"
family = "script"
enabled = true
command = "echo"
domain = "code"
capability = ["read-files", "search"]
risk_class = "read"
""",
    )
    tools, errors = _load_config(tmp_path)
    assert errors == []
    assert tools[0]["domain"] == "code"
    assert tools[0]["capability"] == ["read-files", "search"]
    assert tools[0]["risk_class"] == "read"
