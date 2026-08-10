"""Tests for advisory skill obligations vs receipts audit (#499)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from brigade import cli, receipt_schema, skill_obligations, skills_cmd


def _external_public_label(path: Path) -> str:
    """Mirror the collision-resistant external label contract for assertions."""
    resolved = path.expanduser().resolve()
    name = resolved.name or "path"
    digest = hashlib.sha256(resolved.as_posix().encode("utf-8")).hexdigest()[:12]
    return f"external:{name}-{digest}"


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
    producer_run_id: str | None = None,
    started_at: str = "2026-08-01T00:10:00Z",
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
    payload: dict = {
        "run_id": run_id,
        "status": status,
        "started_at": started_at,
        "completed_at": "2026-08-01T00:11:00Z",
        "commands": [command],
    }
    if producer_run_id is not None:
        payload["producer_run_id"] = producer_run_id
    _write_json(target / ".brigade" / "work" / "verify-runs" / run_id / "receipt.json", payload)


def _write_review_receipt(
    target: Path,
    run_id: str,
    *,
    status: str = "completed",
    exit_code: int = 0,
    producer_run_id: str | None = None,
    started_at: str = "2026-08-01T00:20:00Z",
) -> None:
    payload: dict = {
        "run_id": run_id,
        "reviewer_id": "local",
        "status": status,
        "exit_code": exit_code,
        "started_at": started_at,
        "completed_at": "2026-08-01T00:21:00Z",
    }
    if producer_run_id is not None:
        payload["producer_run_id"] = producer_run_id
    _write_json(target / ".brigade" / "reviews" / "runs" / run_id / "receipt.json", payload)


def _write_handoff_receipt(
    target: Path,
    run_id: str,
    *,
    processed: list[str] | None = None,
    producer_run_id: str | None = None,
    started_at: str = "2026-08-01T00:30:00Z",
) -> None:
    payload: dict = {
        "run_id": run_id,
        "status": "ingested",
        "started_at": started_at,
        "completed_at": "2026-08-01T00:31:00Z",
        "processed_handoff_paths": processed if processed is not None else ["memory-handoffs/demo.md"],
        "skipped_handoff_paths": [],
        "failed_handoff_paths": [],
        "safe_summary": "ingested demo handoff",
    }
    if producer_run_id is not None:
        payload["producer_run_id"] = producer_run_id
    _write_json(target / ".brigade" / "handoffs" / "ingest-runs" / f"{run_id}.json", payload)


def test_parse_obligations_accepts_valid_and_rejects_bad_shapes():
    obligations, errors = skill_obligations.parse_obligations(
        {
            "obligations": [
                {"id": "fresh-verify", "kind": "check", "required": True},
                {"id": "code-review", "kind": "review", "required": False, "description": "peer review"},
            ]
        }
    )
    assert errors == []
    assert [(item.id, item.kind, item.required) for item in obligations] == [
        ("fresh-verify", "check", True),
        ("code-review", "review", False),
    ]
    _, bad = skill_obligations.parse_obligations({"obligations": [{"id": "x", "kind": "deploy"}]})
    assert bad
    _, not_list = skill_obligations.parse_obligations({"obligations": {"id": "x"}})
    assert not_list


def test_declared_skill_ids_from_plan_are_ordered_unique():
    plan = {
        "assignments": [
            {"selected_skill_ids": ["brigade-work", "check"]},
            {"selected_skill_ids": ["check", "taste"]},
            {"selected_skill_ids": "not-a-list"},
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
    assert payload["matching"] == "producer_run_id"
    assert payload["result"] == skill_obligations.RESULT_WARN
    assert payload["finding_count"] == 3
    kinds = {finding["obligation_kind"] for finding in payload["findings"]}
    assert kinds == {"check", "review", "handoff"}
    assert all(finding["severity"] == "medium" for finding in payload["findings"])


def test_audit_satisfies_check_review_handoff_and_obligation_id(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    audited = "20260801-audit-ok"
    _write_skill(
        tmp_path,
        "demo-skill",
        obligations=[
            {"id": "fresh-verify", "kind": "check", "required": True},
            {"id": "code-review", "kind": "review", "required": True},
            {"id": "memory-handoff", "kind": "handoff", "required": True},
        ],
    )
    run_dir = _write_run(tmp_path, audited, ["demo-skill"])
    _write_verify_receipt(tmp_path, "verify-1", obligation_id="fresh-verify", producer_run_id=audited)
    _write_review_receipt(tmp_path, "review-1", producer_run_id=audited)
    _write_handoff_receipt(tmp_path, "handoff-1", producer_run_id=audited)

    assert cli.main(["skills", "audit", str(run_dir), "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == skill_obligations.RESULT_OK
    assert payload["finding_count"] == 0
    assert payload["satisfied_count"] == 3
    assert payload["evidence_counts"] == {"check": 1, "review": 1, "handoff": 1}
    assert payload["unattributed_receipt_count"] == 0


def test_exact_producer_run_id_satisfies_wrong_run_and_timestamp_near_do_not(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    audited = "20260801-audit-exact"
    _write_skill(
        tmp_path,
        "demo-skill",
        obligations=[
            {"id": "fresh-verify", "kind": "check", "required": True},
            {"id": "code-review", "kind": "review", "required": True},
            {"id": "memory-handoff", "kind": "handoff", "required": True},
        ],
    )
    run_dir = _write_run(tmp_path, audited, ["demo-skill"])
    _write_json(
        run_dir / "run.json",
        {
            "run_id": audited,
            "status": "completed",
            "started_at": "2026-08-01T02:00:00Z",
            "completed_at": "2026-08-01T03:00:00Z",
        },
    )
    # Wrong-run receipts stamped near the audited window must not satisfy.
    _write_verify_receipt(
        tmp_path,
        "verify-wrong",
        obligation_id="fresh-verify",
        producer_run_id="other-orchestrator-run",
        started_at="2026-08-01T02:10:00Z",
    )
    _write_review_receipt(
        tmp_path,
        "review-wrong",
        producer_run_id="other-orchestrator-run",
        started_at="2026-08-01T02:20:00Z",
    )
    _write_handoff_receipt(
        tmp_path,
        "handoff-wrong",
        producer_run_id="other-orchestrator-run",
        started_at="2026-08-01T02:30:00Z",
    )

    assert skill_obligations.audit(target=tmp_path, run=run_dir, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == skill_obligations.RESULT_WARN
    assert payload["finding_count"] == 3
    assert payload["evidence_counts"] == {"check": 0, "review": 0, "handoff": 0}
    assert payload["unattributed_receipt_count"] == 0

    _write_verify_receipt(tmp_path, "verify-ok", obligation_id="fresh-verify", producer_run_id=audited)
    _write_review_receipt(tmp_path, "review-ok", producer_run_id=audited)
    _write_handoff_receipt(tmp_path, "handoff-ok", producer_run_id=audited)
    assert skill_obligations.audit(target=tmp_path, run=run_dir, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == skill_obligations.RESULT_OK
    assert payload["satisfied_count"] == 3


def test_legacy_unstamped_receipts_are_unattributed_and_do_not_satisfy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    audited = "20260801-audit-legacy"
    _write_skill(
        tmp_path,
        "demo-skill",
        obligations=[
            {"id": "fresh-verify", "kind": "check", "required": True},
            {"id": "code-review", "kind": "review", "required": True},
            {"id": "memory-handoff", "kind": "handoff", "required": True},
        ],
    )
    run_dir = _write_run(tmp_path, audited, ["demo-skill"])
    _write_verify_receipt(tmp_path, "verify-legacy", obligation_id="fresh-verify")
    _write_review_receipt(tmp_path, "review-legacy")
    _write_handoff_receipt(tmp_path, "handoff-legacy")

    assert skill_obligations.audit(target=tmp_path, run=run_dir, json_output=True) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["result"] == skill_obligations.RESULT_WARN
    assert payload["finding_count"] == 3
    assert payload["evidence_counts"] == {"check": 0, "review": 0, "handoff": 0}
    assert payload["unattributed_receipt_count"] == 3
    kinds = {item["kind"] for item in payload["unattributed_receipts"]}
    assert kinds == {"check", "review", "handoff"}
    assert all(item["attribution"] == "unattributed" for item in payload["unattributed_receipts"])
    assert "sk-" not in out
    assert "Bearer " not in out
    assert "OPENAI_API_KEY" not in out


def test_stamped_obligation_id_failure_does_not_fall_back_to_unrelated_verify(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    audited = "20260801-audit-stamp"
    _write_skill(
        tmp_path,
        "demo-skill",
        obligations=[{"id": "fresh-verify", "kind": "check", "required": True}],
    )
    _write_run(tmp_path, audited, ["demo-skill"])
    _write_verify_receipt(
        tmp_path,
        "verify-fail",
        obligation_id="fresh-verify",
        status="failed",
        command_status="failed",
        exit_code=1,
        producer_run_id=audited,
    )
    _write_verify_receipt(tmp_path, "verify-other", obligation_id=None, producer_run_id=audited)

    assert cli.main(["skills", "audit", audited, "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["finding_count"] == 1
    assert payload["findings"][0]["obligation_id"] == "fresh-verify"


def test_kind_level_verify_satisfies_when_no_obligation_id_stamped(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    audited = "20260801-audit-kind"
    _write_skill(
        tmp_path,
        "demo-skill",
        obligations=[{"id": "fresh-verify", "kind": "check", "required": True}],
    )
    run_dir = _write_run(tmp_path, audited, ["demo-skill"])
    _write_verify_receipt(tmp_path, "verify-plain", producer_run_id=audited)

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


def _assert_no_private_absolute_paths(text: str, *private_roots: Path) -> None:
    for root in private_roots:
        resolved = str(root.resolve())
        assert resolved not in text
        assert resolved.replace("\\", "/") not in text


def test_audit_json_and_text_never_emit_private_absolute_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external_skill = tmp_path / "outside" / "path-skill"
    external_skill.mkdir(parents=True)
    (external_skill / "SKILL.md").write_text(
        "---\nname: path-skill\ndescription: External path skill.\n---\n\n# path-skill\n"
    )
    _write_json(
        external_skill / "skill.json",
        {
            "id": "path-skill",
            "title": "path-skill",
            "version": "0.1.0",
            "description": "external path skill",
            "tests": ["brigade skills lint path-skill"],
            "obligations": [{"id": "fresh-verify", "kind": "check", "required": True}],
        },
    )
    audited = "20260801-audit-paths"
    run_dir = _write_run(workspace, audited, [str(external_skill.resolve())])
    _write_verify_receipt(workspace, "verify-legacy", obligation_id="fresh-verify")
    _write_review_receipt(workspace, "review-legacy")
    _write_handoff_receipt(workspace, "handoff-legacy")
    _write_verify_receipt(
        workspace,
        "verify-ok",
        obligation_id="fresh-verify",
        producer_run_id=audited,
    )

    assert skill_obligations.audit(target=workspace, run=run_dir, json_output=True) == 0
    json_out = capsys.readouterr().out
    payload = json.loads(json_out)
    _assert_no_private_absolute_paths(json_out, tmp_path, workspace, external_skill)

    expected_external = _external_public_label(external_skill)
    assert payload["target"] == skill_obligations.PUBLIC_TARGET
    assert payload["run_dir"] == f".brigade/runs/{audited}"
    assert payload["declared_skill_ids"] == [expected_external]
    assert payload["skills"][0]["source"]["kind"] == "path"
    assert payload["skills"][0]["source"]["identity"] == f"path:{expected_external}"
    assert payload["unattributed_receipt_count"] >= 1
    for item in payload["unattributed_receipts"]:
        path = item.get("path")
        if path is not None:
            assert not Path(str(path)).is_absolute()
            assert str(path).startswith((".brigade/", "external:"))
    satisfied_paths = [
        item["evidence"].get("path") for item in payload["satisfied"] if isinstance(item.get("evidence"), dict)
    ]
    assert satisfied_paths
    assert all(
        path is None or (not Path(str(path)).is_absolute() and str(path).startswith((".brigade/", "external:")))
        for path in satisfied_paths
    )

    assert skill_obligations.audit(target=workspace, run=run_dir, json_output=False) == 0
    text_out = capsys.readouterr().out
    _assert_no_private_absolute_paths(text_out, tmp_path, workspace, external_skill)
    assert f"target: {skill_obligations.PUBLIC_TARGET}" in text_out
    assert f"run: .brigade/runs/{audited}" in text_out
    assert "unattributed" in text_out


def test_audit_registry_source_keeps_stable_non_path_identity(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    _write_skill(
        tmp_path,
        "demo-skill",
        obligations=[{"id": "fresh-verify", "kind": "check", "required": True}],
    )
    run_dir = _write_run(tmp_path, "20260801-audit-registry-src", ["demo-skill"])
    assert skill_obligations.audit(target=tmp_path, run=run_dir, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    source = payload["skills"][0]["source"]
    assert source["kind"] == "registry"
    assert source["identity"] == "registry://skills/demo-skill"
    _assert_no_private_absolute_paths(json.dumps(payload), tmp_path)


def test_missing_path_skill_load_error_redacts_absolute_path_in_json_and_text(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    missing = (tmp_path / "outside" / "missing-skill").resolve()
    assert not missing.exists()
    run_dir = _write_run(workspace, "20260801-audit-missing-path", [str(missing)])
    expected = _external_public_label(missing)

    metadata, source, load_error = skill_obligations._load_skill_metadata(workspace, str(missing))
    assert metadata == {}
    assert source is None
    assert load_error is not None
    assert str(missing) not in load_error
    assert load_error == f"skill not found: {expected}"

    assert skill_obligations.audit(target=workspace, run=run_dir, json_output=True) == 0
    json_out = capsys.readouterr().out
    payload = json.loads(json_out)
    _assert_no_private_absolute_paths(json_out, tmp_path, workspace, missing)
    assert payload["declared_skill_ids"] == [expected]
    assert payload["skills"][0]["loaded"] is False
    assert payload["skills"][0]["load_error"] == f"skill not found: {expected}"
    assert payload["load_warnings"] == [{"skill_id": expected, "detail": f"skill not found: {expected}"}]

    assert skill_obligations.audit(target=workspace, run=run_dir, json_output=False) == 0
    text_out = capsys.readouterr().out
    _assert_no_private_absolute_paths(text_out, tmp_path, workspace, missing)
    assert str(missing) not in text_out


def test_external_same_basename_paths_get_distinct_stable_public_labels(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    skill_a = tmp_path / "a" / "foo"
    skill_b = tmp_path / "b" / "foo"
    for skill_dir, skill_id in ((skill_a, "foo-a"), (skill_b, "foo-b")):
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: {skill_id}\ndescription: Same basename external skill.\n---\n\n# {skill_id}\n"
        )
        _write_json(
            skill_dir / "skill.json",
            {
                "id": skill_id,
                "title": skill_id,
                "version": "0.1.0",
                "description": "same basename external skill",
                "tests": [f"brigade skills lint {skill_id}"],
                "obligations": [{"id": "fresh-verify", "kind": "check", "required": True}],
            },
        )
    label_a = _external_public_label(skill_a)
    label_b = _external_public_label(skill_b)
    assert label_a != label_b
    assert label_a.startswith("external:foo-")
    assert label_b.startswith("external:foo-")
    assert skill_obligations._public_path(workspace, skill_a) == label_a
    assert skill_obligations._public_path(workspace, skill_b) == label_b
    assert skill_obligations._public_path(workspace, skill_a) == label_a

    run_dir = _write_run(
        workspace, "20260801-audit-basename-collision", [str(skill_a.resolve()), str(skill_b.resolve())]
    )
    assert skill_obligations.audit(target=workspace, run=run_dir, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["declared_skill_ids"] == [label_a, label_b]
    identities = [skill["source"]["identity"] for skill in payload["skills"]]
    assert identities == [f"path:{label_a}", f"path:{label_b}"]
    _assert_no_private_absolute_paths(json.dumps(payload), tmp_path, workspace, skill_a, skill_b)


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


def test_producer_run_id_helpers_omit_when_absent_and_stamp_from_env(monkeypatch):
    payload: dict = {}
    receipt_schema.stamp_optional_producer_run_id(payload)
    assert "producer_run_id" not in payload
    monkeypatch.setenv(receipt_schema.BRIGADE_RUN_ID_ENV, "  run-abc  ")
    receipt_schema.stamp_optional_producer_run_id(payload)
    assert payload["producer_run_id"] == "run-abc"
    assert receipt_schema.producer_run_id_from_env() == "run-abc"
