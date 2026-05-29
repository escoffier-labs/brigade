import json
import subprocess
from pathlib import Path

from brigade import cli
from brigade import handoff_cmd
from brigade import release_cmd
from brigade import security_cmd
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


def test_release_blocks_missing_closeout_failed_verification_unclosed_review_dirty_handoff_and_content_guard(tmp_path, monkeypatch, capsys):
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

    monkeypatch.setattr(release_cmd, "plan", fake_plan)
    monkeypatch.setattr(release_cmd, "doctor", fake_doctor)
    monkeypatch.setattr(release_cmd, "run", fake_run)
    monkeypatch.setattr(release_cmd, "runs", fake_runs)
    monkeypatch.setattr(release_cmd, "show", fake_show)

    assert cli.main(["release", "plan", "--target", str(tmp_path), "--base-ref", "main", "--json"]) == 0
    assert cli.main(["release", "doctor", "--target", str(tmp_path), "--base-ref", "main", "--json"]) == 0
    assert cli.main(["release", "run", "--target", str(tmp_path), "--base-ref", "main", "--json"]) == 0
    assert cli.main(["release", "runs", "--target", str(tmp_path), "--limit", "3", "--json"]) == 0
    assert cli.main(["release", "show", "latest", "--target", str(tmp_path), "--json"]) == 0
    assert seen == [
        ("plan", {"target": tmp_path, "base_ref": "main", "json_output": True}),
        ("doctor", {"target": tmp_path, "base_ref": "main", "json_output": True}),
        ("run", {"target": tmp_path, "base_ref": "main", "json_output": True}),
        ("runs", {"target": tmp_path, "limit": 3, "json_output": True}),
        ("show", {"target": tmp_path, "run_id": "latest", "json_output": True}),
    ]
