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
    assert {
        "target",
        "pending_task_count",
        "pending_import_count",
        "review_queue_count",
        "operator_report",
        "action_queue",
    } <= status_fields
    activity_fields = {field["name"] for field in schemas["center-activity"]["item_fields"]}
    assert {
        "subsystem",
        "local_id",
        "status",
        "safe_summary",
        "receipt_path",
        "suggested_next_command",
    } <= activity_fields
    action_fields = {field["name"] for field in schemas["center-actions"]["action_fields"]}
    assert {
        "action_id",
        "source_report_id",
        "source_group",
        "source_subsystem",
        "source_local_id",
        "source_fingerprint",
    } <= action_fields
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
    assert {"subsystem", "local_id", "status", "safe_summary", "suggested_next_command"} <= set(
        contract["required_item_fields"]
    )


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
    assert (
        "&lt;" in (report_dir / "OPERATOR_REPORT.html").read_text()
        or "<pre>" in (report_dir / "OPERATOR_REPORT.html").read_text()
    )

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
    missing = next(check for check in health["checks"] if check["name"] == "operator_report_missing_receipt")
    assert missing["detail"] == "missing receipt reference: .brigade/missing/receipt.json"
    assert str(tmp_path) not in missing["detail"]


def test_center_report_health_accepts_processed_handoff_receipt(tmp_path, capsys):
    _init_git(tmp_path)
    _seed_task_and_import(tmp_path)
    assert center_cmd.report_build(target=tmp_path, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    evidence = Path(report["path"]) / "CENTER_EVIDENCE.json"
    handoff_path = tmp_path / ".claude" / "memory-handoffs" / "example.md"
    processed_path = handoff_path.parent / "processed" / handoff_path.name
    processed_path.parent.mkdir(parents=True)
    processed_path.write_text("# Memory Handoff\n")
    payload = json.loads(evidence.read_text())
    payload["receipt_references"] = [str(handoff_path)]
    evidence.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    health = center_cmd.report_health(tmp_path)
    names = {check["name"] for check in health["checks"]}
    assert "operator_report_missing_receipt" not in names


def test_center_report_health_accepts_archived_handoff_receipt(tmp_path, capsys):
    _init_git(tmp_path)
    _seed_task_and_import(tmp_path)
    assert center_cmd.report_build(target=tmp_path, json_output=True) == 0
    report = json.loads(capsys.readouterr().out)
    evidence = Path(report["path"]) / "CENTER_EVIDENCE.json"
    handoff_path = tmp_path / ".hermes" / "memory-handoffs" / "example.md"
    archived_path = tmp_path / ".brigade" / "handoffs" / "archive" / "2026-06-08" / handoff_path.name
    archived_path.parent.mkdir(parents=True)
    archived_path.write_text("# Memory Handoff\n")
    payload = json.loads(evidence.read_text())
    payload["receipt_references"] = [str(handoff_path)]
    evidence.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")

    health = center_cmd.report_health(tmp_path)
    names = {check["name"] for check in health["checks"]}
    assert "operator_report_missing_receipt" not in names


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
            "evidence": {
                "ready": True,
                "finding_count": 0,
                "candidate_commit": subprocess.run(
                    ["git", "rev-parse", "HEAD"], cwd=target, check=True, capture_output=True, text=True
                ).stdout.strip(),
            },
        },
    )
    monkeypatch.setattr(
        handoff_cmd,
        "draft_queue_payload",
        lambda target: {
            "counts": {"pending": 0},
            "issue_count": 0,
            "top_issue": None,
            "latest_ingest_run": None,
            "drafts": [],
        },
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
        lambda target: {
            "latest_run": None,
            "latest_success": None,
            "latest_unclosed_run": None,
            "unresolved_finding_count": 0,
            "pending_finding_count": 0,
            "top_pending_finding": None,
            "top_unresolved_finding": None,
            "checks": [],
            "config_path": None,
        },
    )
    monkeypatch.setattr(
        release_cmd,
        "_run_content_guard_check",
        lambda *args, **kwargs: {"name": "content_guard_tip", "status": "ok", "detail": "clean"},
    )
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


def test_center_report_build_size_does_not_grow_with_history(tmp_path, capsys):
    _seed_task_and_import(tmp_path)
    sizes: list[int] = []
    for _ in range(3):
        assert center_cmd.report_build(target=tmp_path, json_output=True) == 0
        capsys.readouterr()
        reports = sorted((tmp_path / ".brigade" / "center" / "reports").iterdir())
        newest = max(reports, key=lambda path: path.stat().st_mtime)
        evidence = json.loads((newest / "CENTER_EVIDENCE.json").read_text())
        sizes.append((newest / "CENTER_EVIDENCE.json").stat().st_size)
    # Size must not grow with report history; tiny deltas from transient
    # health-check churn are tolerated, unbounded nesting is not.
    assert max(sizes) - min(sizes) < sizes[0] // 20
    latest_ref = evidence["status"]["operator_report"]["latest"]
    assert isinstance(latest_ref, dict)
    assert latest_ref["report_id"]
    assert latest_ref["digest"]
    assert "reviews" not in latest_ref
    assert "activity" not in latest_ref
    assert "status" not in latest_ref


def test_center_report_build_rotates_old_reports_down_to_keep_count(tmp_path, capsys):
    _seed_task_and_import(tmp_path)
    reports_root = tmp_path / ".brigade" / "center" / "reports"
    archive_root = tmp_path / ".brigade" / "center" / "reports-archive"

    built_ids: list[str] = []
    for _ in range(4):
        assert center_cmd.report_build(target=tmp_path, json_output=True, keep=2) == 0
        rep = json.loads(capsys.readouterr().out)
        built_ids.append(rep["report_id"])
        # Close each report so it is eligible for rotation
        assert (
            center_cmd.report_closeout(
                target=tmp_path,
                report_id=rep["report_id"],
                status="reviewed",
                json_output=True,
            )
            == 0
        )
        capsys.readouterr()

    # Build a 5th report with keep=2
    assert center_cmd.report_build(target=tmp_path, json_output=True, keep=2) == 0
    fifth = json.loads(capsys.readouterr().out)
    built_ids.append(fifth["report_id"])

    # Exactly 2 reports should remain in reports/
    active = [p.name for p in reports_root.iterdir() if p.is_dir() and not p.name.endswith("archive")]
    assert len(active) == 2
    assert set(active) == {built_ids[-1], built_ids[-2]}

    # The 3 older reports should have been archived under reports-archive/
    archived = [p.name for p in archive_root.iterdir() if p.is_dir()]
    assert len(archived) == 3
    assert set(archived) == {built_ids[0], built_ids[1], built_ids[2]}


def test_center_report_build_never_deletes_or_rotates_unclosed_report(tmp_path, capsys):
    _seed_task_and_import(tmp_path)
    reports_root = tmp_path / ".brigade" / "center" / "reports"
    archive_root = tmp_path / ".brigade" / "center" / "reports-archive"

    built_ids: list[str] = []
    # Build report 0 (closed)
    assert center_cmd.report_build(target=tmp_path, json_output=True, keep=10) == 0
    r0 = json.loads(capsys.readouterr().out)["report_id"]
    built_ids.append(r0)
    assert center_cmd.report_closeout(target=tmp_path, report_id=r0, status="reviewed") == 0
    capsys.readouterr()

    # Build report 1 (UNCLOSED - no closeout recorded)
    assert center_cmd.report_build(target=tmp_path, json_output=True, keep=10) == 0
    r1 = json.loads(capsys.readouterr().out)["report_id"]
    built_ids.append(r1)
    capsys.readouterr()

    # Build report 2 (closed)
    assert center_cmd.report_build(target=tmp_path, json_output=True, keep=10) == 0
    r2 = json.loads(capsys.readouterr().out)["report_id"]
    built_ids.append(r2)
    assert center_cmd.report_closeout(target=tmp_path, report_id=r2, status="reviewed") == 0
    capsys.readouterr()

    # Build report 3 (closed) with keep=2
    # Candidates beyond newest 2 are r1 and r0.
    # r1 is UNCLOSED, so it MUST NOT be rotated.
    # r0 is closed, so it SHOULD be rotated.
    assert center_cmd.report_build(target=tmp_path, json_output=True, keep=2) == 0
    r3 = json.loads(capsys.readouterr().out)["report_id"]
    built_ids.append(r3)

    active = {p.name for p in reports_root.iterdir() if p.is_dir() and not p.name.endswith("archive")}
    # r1 (unclosed) MUST still be in active reports
    assert r1 in active
    # r2 and r3 (newest) must be in active reports
    assert r2 in active
    assert r3 in active
    # r0 (closed and beyond keep=2) must have been rotated
    assert r0 not in active

    archived = {p.name for p in archive_root.iterdir() if p.is_dir()}
    assert r0 in archived
    assert r1 not in archived


def test_center_report_build_dry_run_lists_candidates_and_deletes_nothing(tmp_path, capsys):
    _seed_task_and_import(tmp_path)
    reports_root = tmp_path / ".brigade" / "center" / "reports"
    archive_root = tmp_path / ".brigade" / "center" / "reports-archive"

    built_ids: list[str] = []
    for _ in range(3):
        assert center_cmd.report_build(target=tmp_path, json_output=True, keep=10) == 0
        rep = json.loads(capsys.readouterr().out)
        built_ids.append(rep["report_id"])
        assert center_cmd.report_closeout(target=tmp_path, report_id=rep["report_id"], status="reviewed") == 0
        capsys.readouterr()

    # Dry run with keep=1: should report that 2 oldest reports would be rotated
    assert center_cmd.report_build(target=tmp_path, dry_run=True, keep=1, json_output=True) == 0
    dry_json = json.loads(capsys.readouterr().out)
    assert dry_json["dry_run"] is True
    assert dry_json["retention"] == 1
    assert dry_json["would_rotate_count"] == 2
    assert set(dry_json["would_rotate"]) == {built_ids[0], built_ids[1]}

    # Assert NOTHING was moved or deleted
    active = {p.name for p in reports_root.iterdir() if p.is_dir() and not p.name.endswith("archive")}
    assert active == set(built_ids)
    assert not archive_root.exists() or len(list(archive_root.iterdir())) == 0

    # Test text output in dry run
    assert center_cmd.report_build(target=tmp_path, dry_run=True, keep=1, json_output=False) == 0
    out = capsys.readouterr().out
    assert "operator report build (dry run):" in out
    assert "would rotate: 2" in out
    assert f"- {built_ids[0]}" in out
    assert f"- {built_ids[1]}" in out


def test_center_report_build_reads_retention_from_daily_toml(tmp_path, capsys):
    _seed_task_and_import(tmp_path)
    reports_root = tmp_path / ".brigade" / "center" / "reports"
    archive_root = tmp_path / ".brigade" / "center" / "reports-archive"

    # Set retention in .brigade/daily.toml alongside allow_operator_report_build
    (tmp_path / ".brigade").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".brigade" / "daily.toml").write_text(
        "allow_operator_report_build = true\noperator_report_retention = 2\n"
    )

    built_ids: list[str] = []
    for _ in range(3):
        assert center_cmd.report_build(target=tmp_path, json_output=True) == 0
        rep = json.loads(capsys.readouterr().out)
        built_ids.append(rep["report_id"])
        assert center_cmd.report_closeout(target=tmp_path, report_id=rep["report_id"], status="reviewed") == 0
        capsys.readouterr()

    # Now exactly 2 reports should remain because retention=2 was read from daily.toml
    active = {p.name for p in reports_root.iterdir() if p.is_dir() and not p.name.endswith("archive")}
    assert len(active) == 2
    assert built_ids[0] not in active
    assert {built_ids[1], built_ids[2]} == active

    archived = {p.name for p in archive_root.iterdir() if p.is_dir()}
    assert built_ids[0] in archived
