import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from brigade import cli
from brigade import handoff_cmd
from brigade import phases_cmd
from brigade import release_cmd
from brigade import security_cmd
from brigade import tools_cmd
from brigade import work_cmd


def _write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _init_repo(path: Path):
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "dev@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=path, check=True)
    (path / "README.md").write_text("readme\n")
    (path / "CHANGELOG.md").write_text("changelog\n")
    (path / "ROADMAP.md").write_text("roadmap\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, stdout=subprocess.DEVNULL)


def _seed_ready_evidence(path: Path):
    _write_json(
        path / ".brigade" / "work" / "verify-runs" / "verify-one" / "receipt.json",
        {
            "run_id": "verify-one",
            "status": "completed",
            "started_at": "2026-05-29T12:00:00+00:00",
            "commands": [{"command": "python3 -m pytest -q", "status": "completed", "exit_code": 0}],
            "path": str(path / ".brigade" / "work" / "verify-runs" / "verify-one"),
        },
    )
    _write_json(
        path / ".brigade" / "work" / "closeouts" / "closeout-one" / "closeout.json",
        {
            "closeout_id": "closeout-one",
            "ready": True,
            "status": "ready",
            "created_at": "2026-05-29T12:01:00+00:00",
            "path": str(path / ".brigade" / "work" / "closeouts" / "closeout-one" / "closeout.json"),
        },
    )


def _patch_clean_health(monkeypatch):
    monkeypatch.setattr(
        security_cmd,
        "health",
        lambda target: {
            "valid": True,
            "issue_count": 0,
            "top_issue": None,
            "top_finding": None,
            "evidence": {"ready": True, "finding_count": 0},
        },
    )
    monkeypatch.setattr(
        handoff_cmd,
        "draft_queue_payload",
        lambda target: {
            "counts": {"pending": 0, "total": 0},
            "issue_count": 0,
            "top_issue": None,
            "latest_ingest_run": None,
        },
    )
    monkeypatch.setattr(
        work_cmd,
        "_scanner_sweep_health",
        lambda target: {
            "latest": {"sweep_id": "sweep-one", "status": "completed"},
            "review": {"issue_count": 0, "top_issue": None},
            "due_count": 0,
        },
    )
    monkeypatch.setattr(
        work_cmd,
        "_review_health",
        lambda target: {
            "latest_run": {"run_id": "review-one", "status": "completed", "closeout": {"resolved": True}},
            "latest_unclosed_run": None,
            "unresolved_finding_count": 0,
            "top_unresolved_finding": None,
        },
    )


def _patch_content_guard(monkeypatch, *, tip_status="ok", introduced_status="ok"):
    def fake_check(target, *, name, policy, base_ref=None):
        status = tip_status if name == "tip" else introduced_status
        return {
            "name": f"content_guard_{name}",
            "status": status,
            "available": True,
            "exit_code": 0 if status == "ok" else 1,
            "detail": "clean" if status == "ok" else "content-guard reported findings",
        }

    monkeypatch.setattr(release_cmd, "_run_content_guard_check", fake_check)
    monkeypatch.setattr(release_cmd, "_content_guard_available", lambda target: True)


def test_release_guard_defaults_to_embedded_brigade_modules(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTENT_GUARD_DIR", raising=False)
    tip, tip_env, tip_error = release_cmd._content_guard_command(
        tmp_path, policy="public-repo", introduced=False, base_ref=None
    )
    history, history_env, history_error = release_cmd._content_guard_command(
        tmp_path, policy="public-repo", introduced=True, base_ref="origin/main"
    )
    assert tip_error is None
    assert tip[:3] == [release_cmd.sys.executable, "-m", "brigade.guard"]
    assert history[:3] == [release_cmd.sys.executable, "-m", "brigade.guard.git_scan"]
    assert tip_env == history_env == {}
    assert "CONTENT_GUARD_DIR" not in " ".join(tip + history)


def test_release_guard_external_checkout_requires_explicit_override(tmp_path, monkeypatch):
    checkout = tmp_path / "content-guard"
    (checkout / "src").mkdir(parents=True)
    (checkout / "policies").mkdir()
    (checkout / "policies" / "public-repo.json").write_text("{}")
    monkeypatch.setenv("CONTENT_GUARD_DIR", str(checkout))
    argv, env, error = release_cmd._content_guard_command(
        tmp_path, policy="public-repo", introduced=False, base_ref=None
    )
    assert error is None
    assert argv[:3] == [release_cmd.sys.executable, "-m", "content_guard"]
    assert env == {"PYTHONPATH": str(checkout / "src")}


def test_release_plan_run_runs_show_clean_ready(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _seed_ready_evidence(tmp_path)
    _patch_clean_health(monkeypatch)
    _patch_content_guard(monkeypatch)

    assert release_cmd.plan(target=tmp_path, base_ref=None, json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["ready"] is True
    assert plan["blockers"] == []

    assert release_cmd.doctor(target=tmp_path, base_ref=None, json_output=True) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["checks"][0]["name"] == "content_guard_tip"

    assert release_cmd.run(target=tmp_path, base_ref=None, json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["ready"] is True
    assert receipt["status"] == "ready"
    assert Path(receipt["path"], "receipt.json").is_file()
    assert Path(receipt["path"], "summary.md").is_file()

    assert release_cmd.runs(target=tmp_path, json_output=True) == 0
    runs = json.loads(capsys.readouterr().out)
    assert runs["runs"][0]["run_id"] == receipt["run_id"]

    assert release_cmd.show(target=tmp_path, run_id="latest") == 0
    out = capsys.readouterr().out
    assert f"release run: {receipt['run_id']}" in out


def test_release_evidence_includes_tool_pack_parity_and_sync_state(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _seed_ready_evidence(tmp_path)
    _patch_clean_health(monkeypatch)
    _patch_content_guard(monkeypatch)
    source = tmp_path / "tools" / "portable.md"
    source.parent.mkdir()
    source.write_text("portable source\n")
    unmanaged = tmp_path / ".claude" / "commands" / "portable.md"
    unmanaged.parent.mkdir(parents=True)
    unmanaged.write_text("user projection\n")
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        """
[[tool]]
id = "portable"
name = "Portable"
family = "slash-command"
enabled = true
description = "Portable projection."
source_path = "tools/portable.md"
supported_harnesses = ["claude", "codex"]
projections = { claude = ".claude/commands/portable.md", codex = ".codex/skills/portable/SKILL.md" }
"""
    )

    assert tools_cmd.parity_closeout(target=tmp_path, reason="reviewed", json_output=True) == 0
    capsys.readouterr()
    assert tools_cmd.pack_build(target=tmp_path, json_output=True) == 0
    pack = json.loads(capsys.readouterr().out)
    source.write_text("portable source changed\n")

    assert release_cmd.plan(target=tmp_path, base_ref=None, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    tool_catalog = payload["evidence"]["tool_catalog"]
    assert tool_catalog["packs"]["latest"]["pack_id"] == pack["pack_id"]
    assert tool_catalog["packs"]["issue_count"] == 1
    assert tool_catalog["packs"]["top_issue"]["issue_type"] == "pack_stale"
    assert tool_catalog["parity"]["latest_closeout"]["status"] == "reviewed"
    assert tool_catalog["parity"]["changed_issue_count"] >= 1
    assert tool_catalog["sync_plan"]["blocker_count"] >= 1
    assert tool_catalog["sync_plan"]["dry_run_default"] is True
    assert tool_catalog["sync_plan"]["delete_supported"] is False

    assert release_cmd.run(target=tmp_path, base_ref=None, json_output=True) == 0
    capsys.readouterr()
    assert release_cmd.candidate_build(target=tmp_path, base_ref=None, json_output=True) == 0
    candidate = json.loads(capsys.readouterr().out)
    evidence = json.loads(Path(candidate["path"], "EVIDENCE.json").read_text())
    assert evidence["tool_catalog"]["packs"]["latest"]["pack_id"] == pack["pack_id"]
    assert evidence["tool_catalog"]["sync_plan"]["blocker_count"] >= 1


def test_release_blocks_missing_closeout_failed_verification_unclosed_review_dirty_handoff_and_content_guard(
    tmp_path, monkeypatch, capsys
):
    _init_repo(tmp_path)
    _seed_ready_evidence(tmp_path)
    _patch_clean_health(monkeypatch)
    _patch_content_guard(monkeypatch, tip_status="fail")
    (tmp_path / "README.md").write_text("changed\n")
    _write_json(
        tmp_path / ".brigade" / "work" / "verify-runs" / "verify-two" / "receipt.json",
        {
            "run_id": "verify-two",
            "status": "failed",
            "started_at": "2026-05-29T12:02:00+00:00",
            "commands": [{"command": "false", "status": "failed", "exit_code": 1}],
        },
    )
    (tmp_path / ".brigade" / "work" / "closeouts" / "closeout-one" / "closeout.json").unlink()
    monkeypatch.setattr(
        work_cmd,
        "_review_health",
        lambda target: {
            "latest_run": {"run_id": "review-one", "status": "completed"},
            "latest_unclosed_run": {"run_id": "review-one"},
            "unresolved_finding_count": 0,
            "top_unresolved_finding": None,
        },
    )
    monkeypatch.setattr(
        handoff_cmd,
        "draft_queue_payload",
        lambda target: {
            "counts": {"pending": 1, "total": 1},
            "issue_count": 1,
            "top_issue": {"name": "handoff_draft_stale"},
            "latest_ingest_run": None,
        },
    )

    assert release_cmd.run(target=tmp_path, base_ref=None, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    blockers = "\n".join(payload["blockers"])
    assert "tracked files are dirty" in blockers
    assert "missing work closeout" in blockers
    assert "latest verification did not complete" in blockers
    assert "review run is not closed out" in blockers
    assert "handoff draft queue has issue" in blockers
    assert "content_guard_tip" in blockers


def test_release_evidence_includes_hardened_task_acceptance_rollup(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _seed_ready_evidence(tmp_path)
    _patch_clean_health(monkeypatch)
    _patch_content_guard(monkeypatch)
    work_cmd._write_task_ledger(
        tmp_path,
        {
            "version": 1,
            "tasks": [
                {
                    "id": "pending-missing",
                    "text": "Pending missing acceptance",
                    "status": "pending",
                },
                {
                    "id": "done-missing-completed-acceptance",
                    "text": "Done missing completed acceptance",
                    "status": "done",
                    "acceptance": ["Completion should preserve acceptance."],
                    "completion": {"session_path": ".brigade/work/session-one"},
                },
            ],
        },
    )

    assert release_cmd.plan(target=tmp_path, base_ref=None, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert any("task acceptance has issue(s)" in blocker for blocker in payload["blockers"])
    acceptance = payload["evidence"]["task_acceptance"]
    assert acceptance["pending_missing_acceptance"] == ["pending-missing"]
    assert acceptance["done_missing_completed_acceptance"] == ["done-missing-completed-acceptance"]
    assert acceptance["coverage"]["review_findings_unresolved"] == 0
    assert acceptance["latest_work_closeout"]["closeout_id"] == "closeout-one"

    assert release_cmd.candidate_build(target=tmp_path, base_ref=None, json_output=True) == 0
    candidate = json.loads(capsys.readouterr().out)
    evidence = json.loads(Path(candidate["path"], "EVIDENCE.json").read_text())
    assert evidence["task_acceptance"]["pending_missing_acceptance"] == ["pending-missing"]
    assert evidence["task_acceptance"]["done_missing_completed_acceptance"] == ["done-missing-completed-acceptance"]


def test_release_reports_introduced_clean_but_tip_blocked(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _seed_ready_evidence(tmp_path)
    _patch_clean_health(monkeypatch)
    _patch_content_guard(monkeypatch, tip_status="fail", introduced_status="ok")

    assert release_cmd.doctor(target=tmp_path, base_ref="HEAD", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    checks = {check["name"]: check["status"] for check in payload["checks"]}
    assert checks["content_guard_tip"] == "fail"
    assert checks["content_guard_introduced"] == "ok"


def test_release_docs_warnings_for_user_facing_changes(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _seed_ready_evidence(tmp_path)
    _patch_clean_health(monkeypatch)
    _patch_content_guard(monkeypatch)
    code = tmp_path / "src" / "brigade"
    code.mkdir(parents=True)
    (code / "cli.py").write_text("print('initial')\n")
    subprocess.run(["git", "add", "src/brigade/cli.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "add cli"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL)
    (code / "cli.py").write_text("print('changed')\n")

    assert release_cmd.plan(target=tmp_path, base_ref=None, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any("README.md" in warning for warning in payload["warnings"])
    assert any("CHANGELOG.md" in warning for warning in payload["warnings"])
    assert any("ROADMAP.md" in warning for warning in payload["warnings"])


def test_release_cli(tmp_path, monkeypatch):
    seen = []

    def fake_plan(**kwargs):
        seen.append(("plan", kwargs))
        return 0

    def fake_doctor(**kwargs):
        seen.append(("doctor", kwargs))
        return 0

    def fake_run(**kwargs):
        seen.append(("run", kwargs))
        return 0

    def fake_runs(**kwargs):
        seen.append(("runs", kwargs))
        return 0

    def fake_show(**kwargs):
        seen.append(("show", kwargs))
        return 0

    def fake_schema(**kwargs):
        seen.append(("schema", kwargs))
        return 0

    def fake_candidate_plan(**kwargs):
        seen.append(("candidate_plan", kwargs))
        return 0

    def fake_candidate_build(**kwargs):
        seen.append(("candidate_build", kwargs))
        return 0

    def fake_candidate_list(**kwargs):
        seen.append(("candidate_list", kwargs))
        return 0

    def fake_candidate_show(**kwargs):
        seen.append(("candidate_show", kwargs))
        return 0

    def fake_candidate_archive(**kwargs):
        seen.append(("candidate_archive", kwargs))
        return 0

    def fake_candidate_audit(**kwargs):
        seen.append(("candidate_audit", kwargs))
        return 0

    def fake_candidate_import_issues(**kwargs):
        seen.append(("candidate_import_issues", kwargs))
        return 0

    monkeypatch.setattr(release_cmd, "plan", fake_plan)
    monkeypatch.setattr(release_cmd, "doctor", fake_doctor)
    monkeypatch.setattr(release_cmd, "run", fake_run)
    monkeypatch.setattr(release_cmd, "runs", fake_runs)
    monkeypatch.setattr(release_cmd, "show", fake_show)
    monkeypatch.setattr(release_cmd, "schema", fake_schema)
    monkeypatch.setattr(release_cmd, "candidate_plan", fake_candidate_plan)
    monkeypatch.setattr(release_cmd, "candidate_build", fake_candidate_build)
    monkeypatch.setattr(release_cmd, "candidate_list", fake_candidate_list)
    monkeypatch.setattr(release_cmd, "candidate_show", fake_candidate_show)
    monkeypatch.setattr(release_cmd, "candidate_archive", fake_candidate_archive)
    monkeypatch.setattr(release_cmd, "candidate_audit", fake_candidate_audit)
    monkeypatch.setattr(release_cmd, "candidate_import_issues", fake_candidate_import_issues)

    assert cli.main(["release", "plan", "--target", str(tmp_path), "--base-ref", "main", "--json"]) == 0
    assert cli.main(["release", "doctor", "--target", str(tmp_path), "--base-ref", "main", "--json"]) == 0
    assert cli.main(["release", "run", "--target", str(tmp_path), "--base-ref", "main", "--json"]) == 0
    assert cli.main(["release", "runs", "--target", str(tmp_path), "--limit", "3", "--json"]) == 0
    assert cli.main(["release", "show", "latest", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["release", "schema", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["release", "candidate", "plan", "--target", str(tmp_path), "--base-ref", "main", "--json"]) == 0
    assert cli.main(["release", "candidate", "build", "--target", str(tmp_path), "--base-ref", "main", "--json"]) == 0
    assert cli.main(["release", "candidate", "list", "--target", str(tmp_path), "--limit", "4", "--json"]) == 0
    assert cli.main(["release", "candidate", "show", "latest", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["release", "candidate", "archive", "latest", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["release", "candidate", "audit", "latest", "--target", str(tmp_path), "--json"]) == 0
    assert (
        cli.main(["release", "candidate", "import-issues", "latest", "--target", str(tmp_path), "--dry-run", "--json"])
        == 0
    )
    assert seen == [
        ("plan", {"target": tmp_path, "base_ref": "main", "json_output": True}),
        ("doctor", {"target": tmp_path, "base_ref": "main", "json_output": True}),
        ("run", {"target": tmp_path, "base_ref": "main", "json_output": True}),
        ("runs", {"target": tmp_path, "limit": 3, "json_output": True}),
        ("show", {"target": tmp_path, "run_id": "latest", "json_output": True}),
        ("schema", {"target": tmp_path, "json_output": True}),
        ("candidate_plan", {"target": tmp_path, "base_ref": "main", "json_output": True}),
        ("candidate_build", {"target": tmp_path, "base_ref": "main", "json_output": True}),
        ("candidate_list", {"target": tmp_path, "limit": 4, "json_output": True}),
        ("candidate_show", {"target": tmp_path, "candidate_id": "latest", "json_output": True}),
        ("candidate_archive", {"target": tmp_path, "candidate_id": "latest", "json_output": True}),
        ("candidate_audit", {"target": tmp_path, "candidate_id": "latest", "json_output": True}),
        (
            "candidate_import_issues",
            {"target": tmp_path, "candidate_id": "latest", "dry_run": True, "json_output": True},
        ),
    ]


def test_release_candidate_plan_build_list_show_archive(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _seed_ready_evidence(tmp_path)
    _patch_clean_health(monkeypatch)
    _patch_content_guard(monkeypatch)

    assert release_cmd.run(target=tmp_path, base_ref=None, json_output=True) == 0
    release_receipt = json.loads(capsys.readouterr().out)

    assert release_cmd.candidate_plan(target=tmp_path, base_ref=None, json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["candidate_id"] == "planned"
    assert plan["release_readiness"]["run_id"] == release_receipt["run_id"]
    assert "EVIDENCE.json" in plan["bundle_files"]

    assert release_cmd.candidate_build(target=tmp_path, base_ref=None, json_output=True) == 0
    candidate = json.loads(capsys.readouterr().out)
    candidate_dir = Path(candidate["path"])
    assert candidate["ready"] is True
    assert candidate["release_readiness_receipt"]["run_id"] == release_receipt["run_id"]
    assert (candidate_dir / "EVIDENCE.json").is_file()
    assert (candidate_dir / "RELEASE_CANDIDATE.md").is_file()
    assert (candidate_dir / "RELEASE_NOTES_DRAFT.md").is_file()
    assert (candidate_dir / "PUBLISH_PLAN.md").is_file()
    evidence = json.loads((candidate_dir / "EVIDENCE.json").read_text())
    assert evidence["work_closeout"]["closeout_id"] == "closeout-one"
    assert evidence["verification"]["run_id"] == "verify-one"
    assert evidence["release_dogfood"]["issue_count"] >= 0
    assert "Manual-only remote step" in (candidate_dir / "PUBLISH_PLAN.md").read_text()

    assert release_cmd.candidate_list(target=tmp_path, json_output=True) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["candidates"][0]["candidate_id"] == candidate["candidate_id"]

    assert release_cmd.candidate_show(target=tmp_path, candidate_id="latest") == 0
    out = capsys.readouterr().out
    assert f"release candidate: {candidate['candidate_id']}" in out

    assert release_cmd.candidate_archive(target=tmp_path, candidate_id=candidate["candidate_id"], json_output=True) == 0
    archive = json.loads(capsys.readouterr().out)
    assert Path(archive["archive_path"], "EVIDENCE.json").is_file()
    assert not candidate_dir.exists()


def test_release_phase_ledger_closeout_and_report_evidence(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _seed_ready_evidence(tmp_path)
    _patch_clean_health(monkeypatch)
    _patch_content_guard(monkeypatch)
    assert (
        phases_cmd.plan(
            target=tmp_path, phase_id="phase-280", title="Release phase", source_goal="audit", json_output=True
        )
        == 0
    )
    capsys.readouterr()
    assert (
        phases_cmd.complete(
            target=tmp_path,
            phase_id="phase-280",
            status="pushed",
            summary="Release evidence",
            files_changed=["README.md"],
            tests_run=["pytest"],
            commit_hash="abc123",
            push_ref="main",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()

    assert release_cmd.doctor(target=tmp_path, base_ref=None, json_output=True) == 0
    doctor = json.loads(capsys.readouterr().out)
    check_names = {check["name"] for check in doctor["checks"]}
    assert "phase_ledger_unreviewed_pushed_phase" in check_names
    assert "phase_ledger_missing_report" in check_names

    assert (
        phases_cmd.closeout(
            target=tmp_path, selector="phase-280", status="reviewed", reason="Release reviewed.", json_output=True
        )
        == 0
    )
    closeout = json.loads(capsys.readouterr().out)
    assert phases_cmd.report_build(target=tmp_path, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert (
        phases_cmd.report_closeout(
            target=tmp_path,
            report_id=report["report_id"],
            status="reviewed",
            reason="Report reviewed.",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    assert (
        phases_cmd.session_start(target=tmp_path, phase_range="280", source_goal="release session", json_output=True)
        == 0
    )
    session = json.loads(capsys.readouterr().out)
    assert (
        phases_cmd.session_checkpoint(
            target=tmp_path,
            session_id=session["session_id"],
            status="blocked",
            summary="Needs release review.",
            json_output=True,
        )
        == 0
    )
    checkpoint = json.loads(capsys.readouterr().out)
    assert phases_cmd.session_report_build(target=tmp_path, session_id=session["session_id"], json_output=True) == 0
    session_report = json.loads(capsys.readouterr().out)
    assert release_cmd.doctor(target=tmp_path, base_ref=None, json_output=True) == 0
    session_doctor = json.loads(capsys.readouterr().out)
    session_check_names = {check["name"] for check in session_doctor["checks"]}
    assert "phase_session_gate_blocked" in session_check_names
    assert "phase_session_checkpoint_blocked" in session_check_names
    phase_health = phases_cmd.health(tmp_path)
    assert phase_health["latest_session_checkpoint"]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert release_cmd.candidate_build(target=tmp_path, base_ref=None, json_output=True) == 0
    candidate = json.loads(capsys.readouterr().out)
    assert candidate["phase_ledger"]["latest_closeout"]["closeout_id"] == closeout["closeout_id"]
    assert candidate["phase_ledger"]["latest_report"]["report_id"] == report["report_id"]
    assert candidate["phase_ledger"]["latest_report_compare"]["issue_count"] == 0
    assert candidate["phase_ledger"]["latest_session"]["session_id"] == session["session_id"]
    assert candidate["phase_ledger"]["latest_session_checkpoint"]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert candidate["phase_ledger"]["latest_session_checkpoint_compare"]["issue_count"] >= 0
    assert candidate["phase_ledger"]["latest_session_gate"]["safe_to_claim_complete"] is False
    assert candidate["phase_ledger"]["latest_session_report"]["report_id"] == session_report["report_id"]

    assert (
        phases_cmd.closeout(
            target=tmp_path, selector="phase-280", status="blocked", reason="Needs another look.", json_output=True
        )
        == 0
    )
    capsys.readouterr()
    assert phases_cmd.report_build(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    assert (
        phases_cmd.session_start(target=tmp_path, phase_range="280", source_goal="newer session", json_output=True) == 0
    )
    newer_session = json.loads(capsys.readouterr().out)
    assert (
        phases_cmd.session_report_build(target=tmp_path, session_id=newer_session["session_id"], json_output=True) == 0
    )
    capsys.readouterr()
    assert (
        phases_cmd.session_closeout(
            target=tmp_path,
            session_id=newer_session["session_id"],
            status="reviewed",
            reason="Session reviewed.",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    assert release_cmd.candidate_compare(target=tmp_path, candidate_id=candidate["candidate_id"], json_output=True) == 1
    compare = json.loads(capsys.readouterr().out)
    compare_names = {issue["name"] for issue in compare["issues"]}
    assert "newer_phase_closeout" in compare_names
    assert "newer_phase_report" in compare_names
    assert "newer_phase_session" in compare_names
    assert "newer_phase_session_report" in compare_names
    assert "phase_session_checkpoint_changed" in compare_names
    assert "phase_session_checkpoint_compare_changed" in compare_names
    assert "phase_session_gate_changed" in compare_names


def test_release_schema_manifest_reports_contracts_and_latest_receipts(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _seed_ready_evidence(tmp_path)
    _patch_clean_health(monkeypatch)
    _patch_content_guard(monkeypatch)
    assert release_cmd.run(target=tmp_path, base_ref=None, json_output=True) == 0
    release_receipt = json.loads(capsys.readouterr().out)
    assert release_cmd.candidate_build(target=tmp_path, base_ref=None, json_output=True) == 0
    candidate = json.loads(capsys.readouterr().out)

    assert release_cmd.schema(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    schema_ids = {item["id"] for item in payload["schemas"]}
    assert {
        "release-readiness-receipt",
        "release-candidate-evidence",
        "fleet-release-train-evidence",
        "fleet-release-waiver",
        "fleet-release-manual-evidence",
        "release-dogfood-health",
    } <= schema_ids
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["release_readiness_latest"]["status"] == "ok"
    assert checks["release_candidate_latest"]["status"] == "ok"
    assert payload["latest"]["release_readiness"]["id"] == release_receipt["run_id"]
    assert payload["latest"]["release_candidate"]["id"] == candidate["candidate_id"]

    assert release_cmd.schema(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "release schema manifest:" in out
    assert "release-candidate-evidence" in out


def test_release_schema_manifest_detects_missing_receipts(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _seed_ready_evidence(tmp_path)
    _patch_clean_health(monkeypatch)
    _patch_content_guard(monkeypatch)
    assert release_cmd.run(target=tmp_path, base_ref=None, json_output=True) == 0
    capsys.readouterr()
    assert release_cmd.candidate_build(target=tmp_path, base_ref=None, json_output=True) == 0
    candidate = json.loads(capsys.readouterr().out)
    shutil.rmtree(candidate["verification"]["path"])

    assert release_cmd.schema(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    checks = {check["name"]: check for check in payload["checks"]}
    assert checks["release_candidate_missing_verification"]["status"] == "warn"
    assert payload["issue_count"] >= 1


def test_release_candidate_audit_and_import_issues(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _seed_ready_evidence(tmp_path)
    _patch_clean_health(monkeypatch)
    _patch_content_guard(monkeypatch)
    assert release_cmd.run(target=tmp_path, base_ref=None, json_output=True) == 0
    capsys.readouterr()
    assert release_cmd.candidate_build(target=tmp_path, base_ref=None, json_output=True) == 0
    candidate = json.loads(capsys.readouterr().out)
    candidate_dir = Path(candidate["path"])
    evidence_path = candidate_dir / "EVIDENCE.json"
    evidence = json.loads(evidence_path.read_text())
    evidence["command_contract"]["fingerprint"] = "old-contract"
    _write_json(evidence_path, evidence)
    shutil.rmtree(candidate["verification"]["path"])
    (candidate_dir / "RELEASE_NOTES_DRAFT.md").write_text("api_token=123456789abcdef\n")
    future = time.time() + 10
    os.utime(tmp_path / "README.md", (future, future))

    assert release_cmd.candidate_audit(target=tmp_path, candidate_id=candidate["candidate_id"], json_output=True) == 1
    audit = json.loads(capsys.readouterr().out)
    issue_names = {issue["name"] for issue in audit["issues"]}
    assert "missing_verification_receipt" in issue_names
    assert "candidate_docs_changed" in issue_names
    assert "candidate_command_contract_changed" in issue_names
    assert "candidate_privacy_secret_like_value" in issue_names

    assert (
        release_cmd.candidate_import_issues(
            target=tmp_path, candidate_id=candidate["candidate_id"], dry_run=True, json_output=True
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["issues"] == audit["issue_count"]
    assert payload["imported"] == audit["issue_count"]
    first = payload["imports"][0]
    assert first["source"] == "release-candidate"
    assert first["metadata"]["source_item_key"].startswith(f"release-candidate:{candidate['candidate_id']}:")

    assert release_cmd.doctor(target=tmp_path, base_ref=None, json_output=True) == 1
    doctor = json.loads(capsys.readouterr().out)
    check_names = {check["name"] for check in doctor["checks"]}
    assert "release_candidate_audit_missing_verification_receipt" in check_names


def test_release_candidate_notes_and_publish_plan(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _seed_ready_evidence(tmp_path)
    _patch_clean_health(monkeypatch)
    _patch_content_guard(monkeypatch)
    (tmp_path / "CHANGELOG.md").write_text(
        "# Changelog\n\n## [Unreleased]\n- Add release candidate bundles.\n\n## [0.1.0]\n- Initial.\n"
    )
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "release-candidates.md").write_text("docs\n")
    subprocess.run(["git", "add", "CHANGELOG.md", "docs/release-candidates.md"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "add release candidate docs"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL
    )

    assert release_cmd.run(target=tmp_path, base_ref="HEAD~1", json_output=True) == 0
    capsys.readouterr()
    assert release_cmd.candidate_build(target=tmp_path, base_ref="HEAD~1", json_output=True) == 0
    candidate = json.loads(capsys.readouterr().out)
    candidate_dir = Path(candidate["path"])
    notes = (candidate_dir / "RELEASE_NOTES_DRAFT.md").read_text()
    plan = (candidate_dir / "PUBLISH_PLAN.md").read_text()

    assert "Add release candidate bundles." in notes
    assert "add release candidate docs" in notes
    assert "`docs/release-candidates.md`" in notes
    assert "Manual-only remote step" in plan
    assert "git tag <version>" in plan
    assert "git push origin" in plan
    assert "gh release create <version>" in plan


def test_release_candidate_health_warnings(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _seed_ready_evidence(tmp_path)
    _patch_clean_health(monkeypatch)
    _patch_content_guard(monkeypatch, tip_status="fail")

    assert release_cmd.run(target=tmp_path, base_ref=None, json_output=True) == 1
    capsys.readouterr()
    assert release_cmd.candidate_build(target=tmp_path, base_ref=None, json_output=True) == 0
    candidate = json.loads(capsys.readouterr().out)
    evidence_path = Path(candidate["path"], "EVIDENCE.json")
    evidence = json.loads(evidence_path.read_text())
    evidence["created_at"] = "2026-05-01T00:00:00+00:00"
    _write_json(evidence_path, evidence)
    shutil.rmtree(candidate["verification"]["path"])

    (tmp_path / "after-candidate.txt").write_text("changed head\n")
    subprocess.run(["git", "add", "after-candidate.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "change head"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL)
    _patch_content_guard(monkeypatch)

    assert release_cmd.doctor(target=tmp_path, base_ref=None, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    warning_text = "\n".join(payload["warnings"])
    assert "release_candidate_stale" in warning_text
    assert "release_candidate_missing_verification" in warning_text
    assert "release_candidate_head_changed" in warning_text
    assert "release_candidate_blocked" in warning_text


def test_release_candidate_preserves_content_guard_summaries(tmp_path, monkeypatch, capsys):
    _init_repo(tmp_path)
    _seed_ready_evidence(tmp_path)
    _patch_clean_health(monkeypatch)
    _patch_content_guard(monkeypatch, tip_status="fail", introduced_status="ok")

    assert release_cmd.run(target=tmp_path, base_ref="HEAD", json_output=True) == 1
    capsys.readouterr()
    assert release_cmd.candidate_build(target=tmp_path, base_ref="HEAD", json_output=True) == 0
    candidate = json.loads(capsys.readouterr().out)
    evidence = json.loads(Path(candidate["path"], "EVIDENCE.json").read_text())
    checks = evidence["content_guard"]
    assert checks["content_guard_tip"]["status"] == "fail"
    assert checks["content_guard_introduced"]["status"] == "ok"
