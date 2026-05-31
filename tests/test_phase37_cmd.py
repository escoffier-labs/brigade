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


def _seed_task_and_import(path: Path):
    _write_json(
        path / ".brigade" / "work" / "tasks.json",
        {
            "version": 1,
            "tasks": [
                {
                    "id": "task-one",
                    "text": "Review operator report",
                    "status": "pending",
                    "acceptance": ["Report includes review queue."],
                    "created_at": "2026-05-29T12:00:00+00:00",
                }
            ],
        },
    )
    inbox = path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(
        json.dumps(
            {
                "id": "import-one",
                "text": "Review local operator issue",
                "kind": "task",
                "source": "security-scan",
                "status": "pending",
                "priority": "high",
                "metadata": {"source_fingerprint": "fp-one"},
                "created_at": "2026-05-29T12:01:00+00:00",
            },
            sort_keys=True,
        )
        + "\n"
    )


def _init_git(path: Path):
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "dev@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=path, check=True)
    (path / "README.md").write_text("readme\n")
    (path / "CHANGELOG.md").write_text("## [Unreleased]\n\n- Operator report.\n")
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


def test_center_json_items_have_stable_schema_and_drilldown_fields(tmp_path, capsys):
    _seed_task_and_import(tmp_path)
    _write_json(
        tmp_path / ".brigade" / "work" / "verify-runs" / "verify-one" / "receipt.json",
        {"run_id": "verify-one", "status": "completed", "started_at": "2026-05-29T12:02:00+00:00"},
    )

    assert center_cmd.status(target=tmp_path, json_output=True) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["schema_version"] == 1
    assert "operator_report" in status

    assert center_cmd.activity(target=tmp_path, json_output=True) == 0
    activity = json.loads(capsys.readouterr().out)
    assert activity["schema"]["name"] == "center-activity"
    verify = [item for item in activity["activity"] if item["subsystem"] == "verification-run"][0]
    assert verify["local_id"] == "verify-one"
    assert verify["receipt_path"]
    assert verify["suggested_next_command"] == "brigade work verify show verify-one"

    assert center_cmd.reviews(target=tmp_path, json_output=True) == 0
    reviews = json.loads(capsys.readouterr().out)
    assert reviews["schema"]["name"] == "center-reviews"
    first = reviews["reviews"][0]
    for key in ("subsystem", "local_id", "status", "safe_summary", "suggested_next_command"):
        assert key in first


def test_center_schema_manifest_is_stable_and_read_only(tmp_path, capsys):
    before = {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")}

    assert center_cmd.schema(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    after = {str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*")}

    assert after == before
    assert payload["schema"]["name"] == "center-schema-manifest"
    assert payload["read_only"] is True
    assert payload["write_required"] is False
    schema_ids = {schema["id"] for schema in payload["schemas"]}
    assert {
        "center-status",
        "center-activity",
        "center-reviews",
        "center-templates",
        "center-report",
        "center-report-review",
        "center-report-diff",
        "center-actions",
        "center-contract-health",
    } <= schema_ids
    schemas = {schema["id"]: schema for schema in payload["schemas"]}
    status_fields = {field["name"] for field in schemas["center-status"]["top_level_fields"]}
    assert {"target", "pending_task_count", "pending_import_count", "review_queue_count", "operator_report", "action_queue"} <= status_fields
    activity_fields = {field["name"] for field in schemas["center-activity"]["item_fields"]}
    assert {"subsystem", "local_id", "status", "safe_summary", "receipt_path", "suggested_next_command"} <= activity_fields
    action_fields = {field["name"] for field in schemas["center-actions"]["action_fields"]}
    assert {"action_id", "source_report_id", "source_group", "source_subsystem", "source_local_id", "source_fingerprint"} <= action_fields
    assert all(check["status"] == "ok" for check in payload["checks"])

    assert center_cmd.schema(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "center schema manifest:" in out
    assert "- center-actions: brigade center actions list --json" in out
    assert cli.main(["center", "schema", "--target", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["schema_count"] == payload["schema_count"]

    contract = center_cmd._center_contract_health(tmp_path)
    assert contract["issue_count"] == 0
    assert "center-status" in contract["schema_ids"]
    assert {"subsystem", "local_id", "status", "safe_summary", "suggested_next_command"} <= set(contract["required_item_fields"])


def test_center_report_plan_build_list_show_archive_and_cli(tmp_path, capsys):
    _seed_task_and_import(tmp_path)
    assert center_cmd.report_plan(target=tmp_path, json_output=True) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["report_id"] == "planned"
    assert "OPERATOR_REPORT.md" in plan["bundle_files"]
    assert "OPERATOR_REPORT.html" in plan["bundle_files"]

    assert center_cmd.report_build(target=tmp_path, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    report_dir = Path(report["path"])
    assert (report_dir / "OPERATOR_REPORT.md").is_file()
    assert (report_dir / "OPERATOR_REPORT.html").is_file()
    assert (report_dir / "CENTER_EVIDENCE.json").is_file()
    assert "Review Queue" in (report_dir / "OPERATOR_REPORT.md").read_text()
    assert "&lt;" in (report_dir / "OPERATOR_REPORT.html").read_text() or "<pre>" in (report_dir / "OPERATOR_REPORT.html").read_text()

    assert center_cmd.report_list(target=tmp_path, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["report_count"] == 1
    assert center_cmd.report_show(target=tmp_path, report_id=report["report_id"], json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["report"]["report_id"] == report["report_id"]
    assert cli.main(["center", "report", "show", report["report_id"], "--target", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["report"]["report_id"] == report["report_id"]
    assert center_cmd.report_archive(target=tmp_path, report_id=report["report_id"], json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "archived"


def test_center_report_health_detects_stale_missing_receipt_and_changed_head(tmp_path, capsys):
    _init_git(tmp_path)
    _seed_task_and_import(tmp_path)
    assert center_cmd.report_build(target=tmp_path, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    evidence = Path(report["path"]) / "CENTER_EVIDENCE.json"
    payload = json.loads(evidence.read_text())
    payload["created_at"] = "2026-01-01T00:00:00+00:00"
    payload["generated_at"] = "2026-01-01T00:00:00+00:00"
    payload["receipt_references"] = [str(tmp_path / ".brigade" / "missing" / "receipt.json")]
    evidence.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    (tmp_path / "README.md").write_text("changed\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "change readme"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL)

    health = center_cmd.report_health(tmp_path)
    names = {check["name"] for check in health["checks"]}
    assert "operator_report_stale" in names
    assert "operator_report_missing_receipt" in names
    assert "operator_report_head_changed" in names


def test_center_report_integrates_with_work_and_release(tmp_path, monkeypatch, capsys):
    _init_git(tmp_path)
    _seed_release_prereqs(tmp_path)
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
        },
    )
    monkeypatch.setattr(
        handoff_cmd,
        "draft_queue_payload",
        lambda target: {"counts": {"pending": 0}, "issue_count": 0, "top_issue": None, "latest_ingest_run": None, "drafts": []},
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

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["operator_report"]["issue_count"] >= 1

    assert release_cmd.doctor(target=tmp_path, base_ref=None, json_output=True) in {0, 1}
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["evidence"]["operator_report"]["issue_count"] >= 1
    assert any("operator report" in warning for warning in doctor["warnings"])

    assert center_cmd.report_build(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    assert release_cmd.candidate_build(target=tmp_path, base_ref=None, json_output=True) == 0
    candidate = json.loads(capsys.readouterr().out)
    assert candidate["operator_report"]["latest"]["report_id"]
