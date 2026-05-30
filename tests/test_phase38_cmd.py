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


def _init_git(path: Path):
    subprocess.run(["git", "init"], cwd=path, check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["git", "config", "user.email", "dev@example.invalid"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Dev"], cwd=path, check=True)
    (path / "README.md").write_text("readme\n")
    (path / "CHANGELOG.md").write_text("## [Unreleased]\n\n- Report review.\n")
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


def test_report_review_groups_actionable_items_and_commands(tmp_path, capsys):
    _seed_imports(tmp_path)
    assert center_cmd.report_build(target=tmp_path, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)

    assert center_cmd.report_review(target=tmp_path, report_id=report["report_id"], json_output=True) == 0
    review = json.loads(capsys.readouterr().out)
    groups = review["action_plan"]["groups"]
    assert groups["urgent_blockers"][0]["local_id"] == "import-high"
    assert {item["local_id"] for item in groups["pending_work_imports"]} >= {"import-high", "import-normal"}
    assert "brigade work import plan import-high" in review["suggested_next_commands"]["urgent_blockers"]
    assert center_cmd.report_review(target=tmp_path, report_id=report["report_id"]) == 0
    out = capsys.readouterr().out
    assert "urgent_blockers:" in out
    assert "brigade work import plan import-high" in out
    assert cli.main(["center", "report", "review", report["report_id"], "--target", str(tmp_path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["report_id"] == report["report_id"]


def test_report_closeout_states_and_metadata(tmp_path, capsys):
    _seed_imports(tmp_path)
    assert center_cmd.report_build(target=tmp_path, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)

    assert center_cmd.report_closeout(
        target=tmp_path,
        report_id=report["report_id"],
        status="deferred",
        reason="reviewed for today",
        deferred_item_ids=["import-normal"],
        json_output=True,
    ) == 0
    closeout = json.loads(capsys.readouterr().out)
    assert closeout["status"] == "deferred"
    assert closeout["deferred_item_ids"] == ["import-normal"]
    assert closeout["unresolved_item_count"] >= 2
    assert closeout["report_fingerprint"]
    stored = json.loads((Path(report["path"]) / "CENTER_EVIDENCE.json").read_text())
    assert stored["closeout"]["status"] == "deferred"

    health = center_cmd.report_health(tmp_path)
    names = {check["name"] for check in health["checks"]}
    assert "operator_report_unclosed" not in names
    assert cli.main(["center", "report", "closeout", report["report_id"], "--target", str(tmp_path), "--status", "reviewed", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "reviewed"


def test_report_compare_detects_changed_head_missing_receipts_new_activity_and_review_queue(tmp_path, capsys):
    _init_git(tmp_path)
    _seed_imports(tmp_path)
    assert center_cmd.report_build(target=tmp_path, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    evidence = Path(report["path"]) / "CENTER_EVIDENCE.json"
    payload = json.loads(evidence.read_text())
    payload["created_at"] = "2026-01-01T00:00:00+00:00"
    payload["generated_at"] = "2026-01-01T00:00:00+00:00"
    payload["receipt_references"] = [str(tmp_path / ".brigade" / "missing" / "receipt.json")]
    evidence.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    _write_json(
        tmp_path / ".brigade" / "work" / "verify-runs" / "verify-new" / "receipt.json",
        {
            "run_id": "verify-new",
            "status": "completed",
            "started_at": "2026-05-29T12:10:00+00:00",
            "completed_at": "2026-05-29T12:10:10+00:00",
        },
    )
    _seed_imports(tmp_path)
    inbox = tmp_path / ".brigade" / "work" / "imports" / "inbox.jsonl"
    inbox.write_text(inbox.read_text() + json.dumps({"id": "import-new", "text": "New import", "kind": "task", "source": "manual", "status": "pending"}) + "\n")
    (tmp_path / "README.md").write_text("changed\n")
    subprocess.run(["git", "add", "README.md"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "change readme"], cwd=tmp_path, check=True, stdout=subprocess.DEVNULL)

    assert center_cmd.report_compare(target=tmp_path, report_id=report["report_id"], json_output=True) == 1
    compare = json.loads(capsys.readouterr().out)
    names = {issue["name"] for issue in compare["issues"]}
    assert "operator_report_head_changed" in names
    assert "operator_report_missing_receipt" in names
    assert "operator_report_newer_activity" in names
    assert "newer_verification" in names
    assert "operator_report_review_queue_changed" in names


def test_report_closeout_integrates_with_work_and_release_compare(tmp_path, monkeypatch, capsys):
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

    assert center_cmd.report_build(target=tmp_path, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["operator_report"]["top_issue"]["name"] == "operator_report_unclosed"
    assert center_cmd.report_closeout(target=tmp_path, report_id=report["report_id"], status="reviewed", json_output=True) == 0
    capsys.readouterr()
    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["operator_report"]["issue_count"] == 0

    assert release_cmd.run(target=tmp_path, base_ref=None, json_output=True) == 0
    capsys.readouterr()
    assert release_cmd.candidate_build(target=tmp_path, base_ref=None, json_output=True) == 0
    candidate = json.loads(capsys.readouterr().out)
    assert release_cmd.candidate_compare(target=tmp_path, candidate_id=candidate["candidate_id"], json_output=True) == 0
    capsys.readouterr()
    assert center_cmd.report_build(target=tmp_path, json_output=True) == 0
    new_report = json.loads(capsys.readouterr().out)
    assert release_cmd.candidate_compare(target=tmp_path, candidate_id=candidate["candidate_id"], json_output=True) == 1
    compare = json.loads(capsys.readouterr().out)
    names = {issue["name"] for issue in compare["issues"]}
    assert "newer_operator_report" in names or "operator_report_health" in names
    assert new_report["report_id"]
