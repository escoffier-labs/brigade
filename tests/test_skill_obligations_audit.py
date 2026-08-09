"""Tests for advisory skill obligations vs receipts audit (#499)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import cli, skill_obligations, skills_cmd


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _write_skill(target: Path, skill_id: str, *, obligations: list[dict] | None) -> None:
    skill_dir = target / ".brigade" / "skills" / "registry" / skill_id
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill_id}\ndescription: Test skill for obligations audit.\n---\n\n# {skill_id}\n"
    )
    metadata: dict = {
        "id": skill_id,
        "title": skill_id,
        "version": "0.1.0",
        "description": "test skill",
        "tests": [f"brigade skills lint {skill_id}"],
    }
    if obligations is not None:
        metadata["obligations"] = obligations
    _write_json(skill_dir / "skill.json", metadata)


def _write_run(target: Path, run_id: str, skill_ids: list[str]) -> Path:
    run_dir = target / ".brigade" / "runs" / run_id
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "run.json",
        {
            "run_id": run_id,
            "status": "completed",
            "started_at": "2026-08-01T00:00:00Z",
            "completed_at": "2026-08-01T01:00:00Z",
        },
    )
    assignments = []
    if skill_ids:
        assignments.append(
            {
                "stage": 1,
                "worker": "coder",
                "task": "do work",
                "covers": ["implement"],
                "selected_skill_ids": skill_ids,
            }
        )
    else:
        assignments.append({"stage": 1, "worker": "coder", "task": "do work", "covers": ["implement"]})
    _write_json(
        run_dir / "plan.json",
        {"schema": "brigade.run_plan.v1", "schema_version": 1, "assignments": assignments},
    )
    return run_dir


def _write_verify_receipt(
    target: Path,
    run_id: str,
    *,
    status: str = "completed",
    obligation_id: str | None = None,
    command_status: str = "completed",
    exit_code: int = 0,
) -> None:
    command = {
        "command": "pytest -q",
        "argv": ["pytest", "-q"],
        "status": command_status,
        "exit_code": exit_code,
        "check_id": "verify.pytest",
        "check_role": "effectiveness",
    }
    if obligation_id is not None:
        command["obligation_id"] = obligation_id
    _write_json(
        target / ".brigade" / "work" / "verify-runs" / run_id / "receipt.json",
        {
            "run_id": run_id,
            "status": status,
            "started_at": "2026-08-01T00:10:00Z",
            "completed_at": "2026-08-01T00:11:00Z",
            "commands": [command],
        },
    )


def _write_review_receipt(target: Path, run_id: str, *, status: str = "completed", exit_code: int = 0) -> None:
    _write_json(
        target / ".brigade" / "reviews" / "runs" / run_id / "receipt.json",
        {
            "run_id": run_id,
            "reviewer_id": "local",
            "status": status,
            "exit_code": exit_code,
            "started_at": "2026-08-01T00:20:00Z",
            "completed_at": "2026-08-01T00:21:00Z",
        },
    )


def _write_handoff_receipt(target: Path, run_id: str, *, processed: list[str] | None = None) -> None:
    _write_json(
        target / ".brigade" / "handoffs" / "ingest-runs" / f"{run_id}.json",
        {
            "run_id": run_id,
            "status": "ingested",
            "started_at": "2026-08-01T00:30:00Z",
            "completed_at": "2026-08-01T00:31:00Z",
            "processed_handoff_paths": processed if processed is not None else ["memory-handoffs/demo.md"],
            "skipped_handoff_paths": [],
            "failed_handoff_paths": [],
            "safe_summary": "ingested demo handoff",
        },
    )


def test_parse_obligations_accepts_valid_and_rejects_bad_shapes():
    obligations, errors = skill_obligations.parse_obligations(
        {
            "obligations": [
                {"id": "fresh-verify", "kind": "check", "required": True},
                {"id": "code-review", "kind": "review"},
                {"id": "memory-handoff", "kind": "handoff", "required": False, "description": "optional note"},
            ]
        }
    )
    assert errors == []
    assert [item.id for item in obligations] == ["fresh-verify", "code-review", "memory-handoff"]
    assert obligations[1].required is True
    assert obligations[2].required is False

    _, bad = skill_obligations.parse_obligations({"obligations": [{"id": "x", "kind": "deploy"}]})
    assert bad and "kind must be one of" in bad[0]

    _, not_list = skill_obligations.parse_obligations({"obligations": {"id": "x"}})
    assert not_list == ["metadata obligations must be a list"]


def test_declared_skill_ids_from_plan_are_ordered_unique():
    plan = {
        "assignments": [
            {"selected_skill_ids": ["brigade-work", "check"]},
            {"selected_skill_ids": ["check", "taste"]},
            {"task": "no skills"},
        ]
    }
    assert skill_obligations.declared_skill_ids_from_plan(plan) == ["brigade-work", "check", "taste"]
    assert skill_obligations.declared_skill_ids_from_plan(None) == []


def test_audit_reports_missing_required_evidence_as_advisory(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    _write_skill(
        tmp_path,
        "demo-skill",
        obligations=[
            {"id": "fresh-verify", "kind": "check", "required": True},
            {"id": "code-review", "kind": "review", "required": True},
            {"id": "memory-handoff", "kind": "handoff", "required": True},
        ],
    )
    run_dir = _write_run(tmp_path, "20260801-audit-demo", ["demo-skill"])

    rc = cli.main(["skills", "audit", str(run_dir), "--target", str(tmp_path), "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["schema"] == skill_obligations.AUDIT_SCHEMA
    assert payload["advisory"] is True
    assert payload["blocking"] is False
    assert payload["result"] == skill_obligations.RESULT_WARN
    assert payload["finding_count"] == 3
    kinds = {finding["obligation_kind"] for finding in payload["findings"]}
    assert kinds == {"check", "review", "handoff"}
    assert all(finding["severity"] == "medium" for finding in payload["findings"])


def test_audit_satisfies_check_review_handoff_and_obligation_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    _write_skill(
        tmp_path,
        "demo-skill",
        obligations=[
            {"id": "fresh-verify", "kind": "check", "required": True},
            {"id": "code-review", "kind": "review", "required": True},
            {"id": "memory-handoff", "kind": "handoff", "required": True},
        ],
    )
    run_dir = _write_run(tmp_path, "20260801-audit-ok", ["demo-skill"])
    _write_verify_receipt(tmp_path, "verify-1", obligation_id="fresh-verify")
    _write_review_receipt(tmp_path, "review-1")
    _write_handoff_receipt(tmp_path, "handoff-1")

    assert cli.main(["skills", "audit", str(run_dir), "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == skill_obligations.RESULT_OK
    assert payload["finding_count"] == 0
    assert payload["satisfied_count"] == 3
    assert payload["evidence_counts"] == {"check": 1, "review": 1, "handoff": 1}


def test_stamped_obligation_id_failure_does_not_fall_back_to_unrelated_verify(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    _write_skill(
        tmp_path,
        "demo-skill",
        obligations=[{"id": "fresh-verify", "kind": "check", "required": True}],
    )
    _write_run(tmp_path, "20260801-audit-stamp", ["demo-skill"])
    _write_verify_receipt(
        tmp_path,
        "verify-fail",
        obligation_id="fresh-verify",
        status="failed",
        command_status="failed",
        exit_code=1,
    )
    _write_verify_receipt(tmp_path, "verify-other", obligation_id=None)

    assert cli.main(["skills", "audit", "20260801-audit-stamp", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["finding_count"] == 1
    assert payload["findings"][0]["obligation_id"] == "fresh-verify"


def test_kind_level_verify_satisfies_when_no_obligation_id_stamped(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    _write_skill(
        tmp_path,
        "demo-skill",
        obligations=[{"id": "fresh-verify", "kind": "check", "required": True}],
    )
    run_dir = _write_run(tmp_path, "20260801-audit-kind", ["demo-skill"])
    _write_verify_receipt(tmp_path, "verify-plain")

    assert skill_obligations.audit(target=tmp_path, run=run_dir, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["finding_count"] == 0
    assert payload["satisfied_count"] == 1


def test_optional_obligation_missing_is_skipped_not_finding(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    _write_skill(
        tmp_path,
        "demo-skill",
        obligations=[{"id": "code-review", "kind": "review", "required": False}],
    )
    run_dir = _write_run(tmp_path, "20260801-audit-optional", ["demo-skill"])

    assert skill_obligations.audit(target=tmp_path, run=run_dir, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == skill_obligations.RESULT_OK
    assert payload["finding_count"] == 0
    assert len(payload["skipped_optional"]) == 1


def test_no_declared_skills_is_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    run_dir = _write_run(tmp_path, "20260801-audit-empty", [])
    assert skill_obligations.audit(target=tmp_path, run=run_dir, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == skill_obligations.RESULT_NO_DECLARED_SKILLS
    assert payload["finding_count"] == 0


def test_skills_without_obligations_is_ok(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    _write_skill(tmp_path, "plain-skill", obligations=None)
    run_dir = _write_run(tmp_path, "20260801-audit-plain", ["plain-skill"])
    assert skill_obligations.audit(target=tmp_path, run=run_dir, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == skill_obligations.RESULT_NO_OBLIGATIONS
    assert payload["finding_count"] == 0


def test_audit_does_not_write_workspace(tmp_path: Path):
    _write_skill(
        tmp_path,
        "demo-skill",
        obligations=[{"id": "fresh-verify", "kind": "check", "required": True}],
    )
    run_dir = _write_run(tmp_path, "20260801-audit-ro", ["demo-skill"])
    before = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert skill_obligations.audit(target=tmp_path, run=run_dir, json_output=False) == 0
    after = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert before == after


def test_missing_run_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    rc = cli.main(["skills", "audit", "missing-run", "--target", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == 2
    assert "not found" in err


def test_skills_lint_rejects_malformed_obligations(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    _write_skill(
        tmp_path,
        "bad-skill",
        obligations=[{"id": "x", "kind": "not-a-kind"}],
    )
    assert skills_cmd.lint(target=tmp_path, skill="bad-skill", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is False
    assert any("kind must be one of" in error for error in payload["errors"])


def test_bundled_brigade_work_declares_check_and_handoff_obligations():
    from brigade.templates import template_root

    metadata = json.loads((template_root() / "skills" / "brigade-work" / "skill.json").read_text())
    obligations, errors = skill_obligations.parse_obligations(metadata)
    assert errors == []
    assert {(item.id, item.kind) for item in obligations} == {
        ("verify-through-brigade", "check"),
        ("session-handoff", "handoff"),
    }
