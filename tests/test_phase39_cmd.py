import json
import subprocess
from pathlib import Path

from brigade import center_cmd
from brigade import cli
from brigade import handoff_cmd
from brigade import release_cmd
from brigade import security_cmd
from brigade import work_cmd


def _write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _seed_imports(path: Path):
    inbox = path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    records = [
        {
            "id": "import-high",
            "text": "Fix high risk finding",
            "kind": "task",
            "source": "security-scan",
            "status": "pending",
            "priority": "high",
            "metadata": {"source_fingerprint": "fp-high"},
            "created_at": "2026-05-29T12:01:00+00:00",
        },
        {
            "id": "import-normal",
            "text": "Review project candidate",
            "kind": "task",
            "source": "project-consolidation",
            "status": "pending",
            "priority": "normal",
            "metadata": {"source_fingerprint": "fp-project"},
            "created_at": "2026-05-29T12:02:00+00:00",
        },
    ]
    inbox.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records))


def _build_reviewed_report(path: Path, capsys):
    _seed_imports(path)
    assert center_cmd.report_build(target=path, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert center_cmd.report_closeout(target=path, report_id=report["report_id"], status="reviewed", json_output=True) == 0
    capsys.readouterr()
    return report


def _init_git(path: Path):
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "dev@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=path, check=True)
    (path / "README.md").write_text("readme\n")
    (path / "CHANGELOG.md").write_text("## [Unreleased]\n\n- Action queue.\n")
    (path / "ROADMAP.md").write_text("# Roadmap\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, stdout=subprocess.DEVNULL)


def _seed_release_prereqs(path: Path):
    _write_json(
        path / ".brigade" / "work" / "verify-runs" / "verify-one" / "receipt.json",
        {
            "run_id": "verify-one",
            "status": "completed",
            "started_at": "2026-05-29T12:02:00+00:00",
            "completed_at": "2026-05-29T12:02:10+00:00",
            "path": str(path / ".brigade" / "work" / "verify-runs" / "verify-one"),
        },
    )
    _write_json(
        path / ".brigade" / "work" / "closeouts" / "closeout-one" / "closeout.json",
        {
            "closeout_id": "closeout-one",
            "ready": True,
            "status": "ready",
            "created_at": "2026-05-29T12:03:00+00:00",
            "path": str(path / ".brigade" / "work" / "closeouts" / "closeout-one" / "closeout.json"),
        },
    )


def _patch_release_health(monkeypatch):
    monkeypatch.setattr(
        security_cmd,
        "health",
        lambda target: {
            "config_path": str(target / ".brigade" / "security.toml"),
            "valid": True,
            "issue_count": 0,
            "top_issue": None,
            "top_finding": None,
            "evidence": {"ready": True, "finding_count": 0},
            "checks": [],
        },
    )
    monkeypatch.setattr(
        handoff_cmd,
        "draft_queue_payload",
        lambda target, **kwargs: {"counts": {"pending": 0}, "issue_count": 0, "top_issue": None, "latest_ingest_run": None, "drafts": [], "checks": []},
    )
    monkeypatch.setattr(
        work_cmd,
        "_scanner_sweep_health",
        lambda target: {
            "sweeps_root": str(target / ".brigade" / "scanners" / "sweeps"),
            "latest": None,
            "review": {"issue_count": 0},
            "due_count": 0,
            "checks": [],
            "suggested_command": None,
        },
    )
    monkeypatch.setattr(
        work_cmd,
        "_review_health",
        lambda target: {"latest_run": None, "latest_success": None, "latest_unclosed_run": None, "unresolved_finding_count": 0, "pending_finding_count": 0, "top_pending_finding": None, "top_unresolved_finding": None, "checks": [], "config_path": None},
    )
    monkeypatch.setattr(release_cmd, "_run_content_guard_check", lambda *args, **kwargs: {"name": "content_guard_tip", "status": "ok", "detail": "clean"})
    monkeypatch.setattr(release_cmd, "_content_guard_available", lambda target: True)


def test_center_actions_plan_build_list_show_and_cli(tmp_path, capsys):
    report = _build_reviewed_report(tmp_path, capsys)

    assert center_cmd.actions_plan(target=tmp_path, report_id=report["report_id"], json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["report_review_status"] == "reviewed"
    assert plan["action_count"] >= 2
    import_actions = [action for action in plan["actions"] if action["source_subsystem"] == "work-import"]
    assert {action["source_local_id"] for action in import_actions} == {"import-high", "import-normal"}
    assert [action for action in import_actions if action["source_local_id"] == "import-high"][0]["source_group"] == "urgent_blockers"

    assert center_cmd.actions_build(target=tmp_path, report_id=report["report_id"], json_output=True) == 0
    build = json.loads(capsys.readouterr().out)
    assert build["created_count"] >= 2
    action_id = build["created_actions"][0]["action_id"]
    assert (tmp_path / ".brigade" / "center" / "actions" / "actions.json").is_file()

    assert center_cmd.actions_list(target=tmp_path, json_output=True) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["action_count"] >= 2
    assert center_cmd.actions_show(target=tmp_path, action_id=action_id, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["action"]["action_id"] == action_id
    assert cli.main(["center", "actions", "show", action_id, "--target", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["action"]["action_id"] == action_id


def test_center_actions_require_reviewed_report_unless_allowed_and_dedupe(tmp_path, capsys):
    _seed_imports(tmp_path)
    assert center_cmd.report_build(target=tmp_path, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)

    assert center_cmd.actions_build(target=tmp_path, report_id=report["report_id"], json_output=True) == 2
    capsys.readouterr()
    assert center_cmd.actions_build(target=tmp_path, report_id=report["report_id"], allow_unreviewed=True, json_output=True) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["created_count"] >= 2
    assert center_cmd.actions_build(target=tmp_path, report_id=report["report_id"], allow_unreviewed=True, json_output=True) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["created_count"] == 0
    assert second["skipped_count"] == first["created_count"]
    assert cli.main(["center", "actions", "build", report["report_id"], "--target", str(tmp_path), "--allow-unreviewed", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["created_count"] == 0


def test_center_actions_state_transitions_and_archive(tmp_path, capsys):
    report = _build_reviewed_report(tmp_path, capsys)
    assert center_cmd.actions_build(target=tmp_path, report_id=report["report_id"], json_output=True) == 0
    build = json.loads(capsys.readouterr().out)
    action_id = build["created_actions"][0]["action_id"]
    defer_id = build["created_actions"][1]["action_id"]

    assert center_cmd.actions_start(target=tmp_path, action_id=action_id, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["action"]["status"] == "active"
    assert center_cmd.actions_done(target=tmp_path, action_id=action_id, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["action"]["status"] == "done"
    assert center_cmd.actions_defer(target=tmp_path, action_id=defer_id, reason="not today", json_output=True) == 0
    deferred = json.loads(capsys.readouterr().out)["action"]
    assert deferred["status"] == "deferred"
    assert deferred["defer_reason"] == "not today"

    assert center_cmd.actions_archive_completed(target=tmp_path, json_output=True) == 0
    archive = json.loads(capsys.readouterr().out)
    assert archive["archived_count"] == 1
    assert Path(archive["archive_path"]).is_file()
    remaining = json.loads((tmp_path / ".brigade" / "center" / "actions" / "actions.json").read_text())["actions"]
    assert "done" not in {action["status"] for action in remaining}
    assert any(action["status"] == "deferred" for action in remaining)
    assert center_cmd.actions_build(target=tmp_path, report_id=report["report_id"], json_output=True) == 0
    rebuild = json.loads(capsys.readouterr().out)
    assert rebuild["created_count"] == 0


def test_center_actions_integrate_with_center_work_and_release(tmp_path, monkeypatch, capsys):
    _init_git(tmp_path)
    _seed_release_prereqs(tmp_path)
    _patch_release_health(monkeypatch)
    report = _build_reviewed_report(tmp_path, capsys)
    assert center_cmd.actions_build(target=tmp_path, report_id=report["report_id"], json_output=True) == 0
    capsys.readouterr()

    assert center_cmd.status(target=tmp_path, json_output=True) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["action_queue"]["open_count"] >= 2
    assert center_cmd.reviews(target=tmp_path, json_output=True) == 0
    reviews = json.loads(capsys.readouterr().out)
    assert any(item["subsystem"] == "center-action" for item in reviews["reviews"])

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["operator_actions"]["open_count"] >= 2
    assert work_cmd.doctor(target=tmp_path) == 1
    assert "center_actions_open" in capsys.readouterr().out

    assert release_cmd.doctor(target=tmp_path, base_ref=None, json_output=True) == 0
    release = json.loads(capsys.readouterr().out)
    assert release["evidence"]["operator_actions"]["open_count"] >= 2
    assert any("operator action queue" in warning for warning in release["warnings"])
