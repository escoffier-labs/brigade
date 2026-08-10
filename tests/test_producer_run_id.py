"""producer_run_id transport + receipt producer coverage (#499)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from brigade import acpx_adapter, agents, handoff_cmd, receipt_schema, run_transport, work_cmd
from brigade.roster import Agent, Roster
from brigade.run_transport import Assignment
from brigade.work_cmd import reviews as reviews_mod


def _roster(env=None):
    chef = Agent(name="chef", cli="codex", role="plan")
    worker = Agent(name="k3", cli="claude", role="worker", model="kimi-k3", env=env)
    return Roster(orchestrator="chef", agents={"chef": chef, "k3": worker})


def _dispatch(roster, monkeypatch, captured, *, run_id=None):
    def fake_run(argv, **kw):
        captured["env"] = kw.get("env")
        return agents.proc.Result(0, "worker answer", "")

    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", fake_run)
    return run_transport.dispatch(
        [Assignment(worker="k3", task="do the thing")],
        roster,
        build_prompt=lambda agent, assignment, **kw: assignment.task,
        run_appserver_worker=lambda *a, **kw: agents.AgentResult(text="", ok=False, detail="unused"),
        event_writer=lambda events_dir, worker, verbose=False: None,
        read_only=True,
        run_id=run_id,
    )


def test_worker_env_receives_brigade_run_id_without_seat_env(monkeypatch):
    captured = {}
    results = _dispatch(_roster(None), monkeypatch, captured, run_id="orch-run-1")
    assert results[0].ok
    assert captured["env"][receipt_schema.BRIGADE_RUN_ID_ENV] == "orch-run-1"
    assert results[0].env_overrides == ()


def test_worker_env_merges_brigade_run_id_with_seat_env(monkeypatch):
    monkeypatch.setenv("LANE_KEY", "sk-lane-value")
    captured = {}
    roster = _roster(
        {
            "ANTHROPIC_BASE_URL": "https://api.example.com/anthropic",
            "ANTHROPIC_AUTH_TOKEN_REF": "LANE_KEY",
        }
    )
    results = _dispatch(roster, monkeypatch, captured, run_id="orch-run-2")
    assert results[0].ok
    env = captured["env"]
    assert env[receipt_schema.BRIGADE_RUN_ID_ENV] == "orch-run-2"
    assert env["ANTHROPIC_BASE_URL"] == "https://api.example.com/anthropic"
    assert env["ANTHROPIC_AUTH_TOKEN"] == "sk-lane-value"
    assert "sk-lane-value" not in str(results[0])


def test_worker_without_run_id_keeps_legacy_env_none(monkeypatch):
    captured = {}
    results = _dispatch(_roster(None), monkeypatch, captured, run_id=None)
    assert results[0].ok
    assert captured["env"] is None


def test_acpx_worker_receives_brigade_run_id(monkeypatch, tmp_path):
    captured = {}
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="plan"),
            "composer": Agent(
                name="composer",
                cli="cursor",
                role="worker",
                transport="acpx",
                transport_version="0.12.0",
                model="composer-2.5",
            ),
        },
    )

    def fake_run_cursor(prompt, **kwargs):
        captured["env"] = kwargs.get("env")
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(acpx_adapter, "run_cursor", fake_run_cursor)
    result = run_transport.dispatch(
        [Assignment(worker="composer", task="do the thing")],
        roster,
        build_prompt=lambda agent, assignment, **kw: assignment.task,
        run_appserver_worker=lambda *a, **kw: agents.AgentResult(text="", ok=False, detail="unused"),
        event_writer=lambda events_dir, worker, verbose=False: None,
        cwd=tmp_path,
        read_only=True,
        run_id="orch-acpx-1",
    )[0]
    assert result.ok
    assert captured["env"][receipt_schema.BRIGADE_RUN_ID_ENV] == "orch-acpx-1"


def _init_git(tmp_path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "dev@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("ok\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=tmp_path, check=True)


def test_verify_producer_stamps_and_omits_producer_run_id(tmp_path, monkeypatch, capsys):
    _init_git(tmp_path)
    monkeypatch.delenv(receipt_schema.BRIGADE_RUN_ID_ENV, raising=False)
    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=[f"{sys.executable} -c \"print('ok')\""],
            timeout=30,
            json_output=True,
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert "producer_run_id" not in receipt

    monkeypatch.setenv(receipt_schema.BRIGADE_RUN_ID_ENV, "orch-verify-1")
    assert (
        work_cmd.verify_run(
            target=tmp_path,
            commands=[f"{sys.executable} -c \"print('ok')\""],
            timeout=30,
            json_output=True,
        )
        == 0
    )
    stamped = json.loads(capsys.readouterr().out)
    assert stamped["producer_run_id"] == "orch-verify-1"
    serialized = json.dumps(stamped)
    assert "OPENAI_API_KEY" not in serialized
    assert "sk-" not in serialized


def test_review_producer_stamps_and_omits_producer_run_id(tmp_path, monkeypatch):
    monkeypatch.delenv(receipt_schema.BRIGADE_RUN_ID_ENV, raising=False)
    reviewer = {
        "id": "local",
        "name": "local",
        "command": f"{sys.executable} -c \"print('review ok')\"",
        "timeout": 30,
        "cwd": str(tmp_path),
    }
    receipt = reviews_mod._review_run_one(tmp_path, reviewer)
    assert "producer_run_id" not in receipt

    monkeypatch.setenv(receipt_schema.BRIGADE_RUN_ID_ENV, "orch-review-1")
    stamped = reviews_mod._review_run_one(tmp_path, reviewer)
    assert stamped["producer_run_id"] == "orch-review-1"


_NO_CARD_HANDOFF = """# Memory Handoff

## Type
learning

## Title
Producer run id handoff

## Summary
Handoff producer stamps optional producer_run_id.

## Recommended memory action
no-card

## Target document
.learnings/LEARNINGS.md

## Suggested document content
### Producer run id handoff

Durable fact for producer_run_id coverage.
"""


def test_handoff_producer_stamps_and_omits_producer_run_id(tmp_path, monkeypatch, capsys):
    inbox = tmp_path / ".hermes" / "memory-handoffs"
    inbox.mkdir(parents=True)
    (inbox / "demo.md").write_text(_NO_CARD_HANDOFF)
    monkeypatch.delenv(receipt_schema.BRIGADE_RUN_ID_ENV, raising=False)
    assert (
        handoff_cmd.receipt_record(
            target=tmp_path,
            draft_ids=["demo"],
            owner="test",
            run_id="handoff-no-orch",
            json_output=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    receipt = json.loads(Path(payload["receipt_path"]).read_text())
    assert "producer_run_id" not in receipt

    monkeypatch.setenv(receipt_schema.BRIGADE_RUN_ID_ENV, "orch-handoff-1")
    assert (
        handoff_cmd.receipt_record(
            target=tmp_path,
            draft_ids=["demo"],
            owner="test",
            run_id="handoff-with-orch",
            json_output=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    stamped = json.loads(Path(payload["receipt_path"]).read_text())
    assert stamped["producer_run_id"] == "orch-handoff-1"


def test_skills_audit_text_output_labels_unattributed_without_secrets(tmp_path, capsys, monkeypatch):
    from brigade import skill_obligations

    def write_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    skill_dir = tmp_path / ".brigade" / "skills" / "registry" / "demo-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\ndescription: x\n---\n\n# demo\n")
    write_json(
        skill_dir / "skill.json",
        {
            "id": "demo-skill",
            "title": "demo",
            "version": "0.1.0",
            "description": "demo",
            "tests": ["brigade skills lint demo-skill"],
            "obligations": [{"id": "fresh-verify", "kind": "check", "required": True}],
        },
    )
    run_dir = tmp_path / ".brigade" / "runs" / "20260801-audit-text"
    run_dir.mkdir(parents=True)
    write_json(
        run_dir / "run.json",
        {"run_id": run_dir.name, "status": "completed", "started_at": "2026-08-01T00:00:00Z"},
    )
    write_json(
        run_dir / "plan.json",
        {
            "schema": "brigade.run_plan.v1",
            "schema_version": 1,
            "assignments": [
                {
                    "stage": 1,
                    "worker": "coder",
                    "task": "do work",
                    "covers": ["implement"],
                    "selected_skill_ids": ["demo-skill"],
                }
            ],
        },
    )
    write_json(
        tmp_path / ".brigade" / "work" / "verify-runs" / "verify-legacy" / "receipt.json",
        {
            "run_id": "verify-legacy",
            "status": "completed",
            "started_at": "2026-08-01T00:10:00Z",
            "commands": [
                {
                    "command": "pytest -q",
                    "status": "completed",
                    "exit_code": 0,
                    "obligation_id": "fresh-verify",
                }
            ],
        },
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-should-not-leak")
    assert skill_obligations.audit(target=tmp_path, run=run_dir, json_output=False) == 0
    out = capsys.readouterr().out
    assert "unattributed" in out
    assert "producer_run_id" in out
    assert "sk-secret-should-not-leak" not in out
    assert "OPENAI_API_KEY" not in out
