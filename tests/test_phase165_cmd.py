import json
from datetime import datetime, timedelta, timezone

from brigade import center_cmd, cli, daily_cmd, phases_cmd, work_cmd


def test_phase_ledger_init_plan_list_show_start_complete_and_defer(tmp_path, capsys):
    assert cli.main(["work", "phases", "init", "--target", str(tmp_path), "--json"]) == 0
    init_payload = json.loads(capsys.readouterr().out)
    assert init_payload["written"] is True
    assert (tmp_path / ".brigade" / "work" / "phases" / "index.json").is_file()

    assert cli.main(
        [
            "work",
            "phases",
            "plan",
            "--target",
            str(tmp_path),
            "--phase-id",
            "phase-165",
            "--title",
            "AFK ledger",
            "--goal",
            "phase execution evidence",
            "--json",
        ]
    ) == 0
    plan_payload = json.loads(capsys.readouterr().out)
    assert plan_payload["created_count"] == 1
    assert plan_payload["created"][0]["phase_id"] == "phase-165"

    assert cli.main(["work", "phases", "list", "--target", str(tmp_path), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["record_count"] == 1

    assert cli.main(["work", "phases", "start", "phase-165", "--target", str(tmp_path), "--json"]) == 0
    started = json.loads(capsys.readouterr().out)
    assert started["status"] == "in-progress"
    assert started["started_at"]

    assert cli.main(
        [
            "work",
            "phases",
            "complete",
            "phase-165",
            "--target",
            str(tmp_path),
            "--status",
            "pushed",
            "--summary",
            "Implemented the ledger.",
            "--file",
            "src/brigade/phases_cmd.py",
            "--test",
            "pytest tests/test_phase165_cmd.py -q",
            "--test-result",
            "passed",
            "--commit",
            "abc1234",
            "--push-ref",
            "main",
            "--next",
            "Use the ledger for unattended phase ranges.",
            "--json",
        ]
    ) == 0
    completed = json.loads(capsys.readouterr().out)
    assert completed["status"] == "pushed"
    assert completed["commit_hash"] == "abc1234"
    assert completed["push_ref"] == "main"

    assert cli.main(["work", "phases", "show", "phase-165", "--target", str(tmp_path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["implementation_summary"] == "Implemented the ledger."

    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--phase-id", "phase-166", "--title", "Deferred", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "defer", "phase-166", "--target", str(tmp_path), "--reason", "Not needed in this tranche.", "--json"]) == 0
    deferred = json.loads(capsys.readouterr().out)
    assert deferred["status"] == "deferred"
    assert deferred["deferred_items"] == ["Not needed in this tranche."]


def test_phase_ledger_doctor_range_and_evidence_warnings(tmp_path, capsys):
    assert phases_cmd.plan(target=tmp_path, phase_id="phase-170", title="No tests", source_goal="audit", json_output=True) == 0
    capsys.readouterr()
    assert phases_cmd.complete(target=tmp_path, phase_id="phase-170", summary="Done", json_output=True) == 0
    capsys.readouterr()

    assert phases_cmd.plan(target=tmp_path, phase_id="phase-171", title="No commit", source_goal="audit", json_output=True) == 0
    capsys.readouterr()
    assert phases_cmd.complete(target=tmp_path, phase_id="phase-171", status="committed", files_changed=["file.py"], tests_run=["pytest"], json_output=True) == 0
    capsys.readouterr()

    assert phases_cmd.plan(target=tmp_path, phase_id="phase-172", title="No push ref", source_goal="audit", json_output=True) == 0
    capsys.readouterr()
    assert phases_cmd.complete(target=tmp_path, phase_id="phase-172", status="pushed", files_changed=["file.py"], tests_run=["pytest"], commit_hash="abc123", json_output=True) == 0
    capsys.readouterr()

    assert phases_cmd.doctor(target=tmp_path, phase_range="170-173", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    names = {check["name"] for check in payload["checks"]}
    assert "phase_range_missing_records" in names
    assert "phase_complete_without_tests" in names
    assert "phase_complete_without_changes_or_deferral" in names
    assert "phase_committed_without_hash" in names
    assert "phase_pushed_without_ref" in names


def test_phase_ledger_explicit_group_and_silent_compression_detection(tmp_path, capsys):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "180-182", "--grouped", "--title", "Grouped hardening", "--goal", "audit", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    created_ids = {item["phase_id"] for item in payload["created"]}
    assert {"phase-180-182-group", "phase-180", "phase-181", "phase-182"} <= created_ids

    rogue = tmp_path / ".brigade" / "work" / "phases" / "records" / "phase-190-195.json"
    rogue.write_text(
        json.dumps(
            {
                "phase_id": "phase-190-195",
                "kind": "phase",
                "status": "implemented",
                "phase_range": "190-195",
                "explicit_grouping": False,
                "files_changed": ["file.py"],
                "tests_run": ["pytest"],
            }
        )
        + "\n"
    )

    assert phases_cmd.doctor(target=tmp_path, json_output=True) == 0
    doctor_payload = json.loads(capsys.readouterr().out)
    assert any(check["name"] == "phase_range_compressed_without_group" for check in doctor_payload["checks"])


def test_phase_ledger_stale_in_progress_and_blocked_without_next_step(tmp_path, capsys):
    assert phases_cmd.plan(target=tmp_path, phase_id="phase-200", title="Stale", source_goal="audit", json_output=True) == 0
    capsys.readouterr()
    assert phases_cmd.start(target=tmp_path, phase_id="phase-200", json_output=True) == 0
    started = json.loads(capsys.readouterr().out)
    path = tmp_path / ".brigade" / "work" / "phases" / "records" / "phase-200.json"
    started["started_at"] = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    path.write_text(json.dumps(started) + "\n")

    assert phases_cmd.plan(target=tmp_path, phase_id="phase-201", title="Blocked", source_goal="audit", json_output=True) == 0
    blocked = json.loads(capsys.readouterr().out)
    blocked_path = tmp_path / ".brigade" / "work" / "phases" / "records" / "phase-201.json"
    blocked_record = json.loads(blocked_path.read_text())
    blocked_record["status"] = "blocked"
    blocked_record["blocker_reason"] = "Need input."
    blocked_path.write_text(json.dumps(blocked_record) + "\n")

    assert phases_cmd.doctor(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    names = {check["name"] for check in payload["checks"]}
    assert "phase_stale_in_progress" in names
    assert "phase_blocked_without_next_step" in names


def test_phase_ledger_daily_work_and_center_integration(tmp_path, capsys):
    assert phases_cmd.plan(target=tmp_path, phase_id="phase-210", title="Warn", source_goal="audit", json_output=True) == 0
    capsys.readouterr()
    assert phases_cmd.complete(target=tmp_path, phase_id="phase-210", summary="No evidence", json_output=True) == 0
    capsys.readouterr()

    assert daily_cmd.status(target=tmp_path, json_output=True) == 0
    daily_status = json.loads(capsys.readouterr().out)
    assert daily_status["phase_ledger"]["issue_count"] >= 1

    assert daily_cmd.doctor(target=tmp_path, json_output=True) == 0
    daily_doctor = json.loads(capsys.readouterr().out)
    assert any(check["name"] == "phase_ledger_issue" for check in daily_doctor["checks"])

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["phase_ledger"]["issue_count"] >= 1

    assert work_cmd.doctor(target=tmp_path) in {0, 1}
    assert "phase_complete_without_tests" in capsys.readouterr().out

    assert center_cmd.status(target=tmp_path, json_output=True) == 0
    center_status = json.loads(capsys.readouterr().out)
    assert center_status["phase_ledger"]["issue_count"] >= 1


def test_phase_ledger_schema_status_next_report_and_imports(tmp_path, capsys):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "220-222", "--title", "Range", "--goal", "audit", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "complete", "phase-220", "--target", str(tmp_path), "--summary", "Done", "--file", "file.py", "--test", "pytest", "--json"]) == 0
    capsys.readouterr()

    assert cli.main(["work", "phases", "schema", "--target", str(tmp_path), "--json"]) == 0
    schema_payload = json.loads(capsys.readouterr().out)
    schema_names = {item["name"] for item in schema_payload["schemas"]}
    assert {"phase-record", "phase-ledger-status", "phase-ledger-report"} <= schema_names

    assert cli.main(["work", "phases", "status", "--target", str(tmp_path), "--range", "220-222", "--json"]) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["record_count"] == 3
    assert status_payload["open_count"] == 2
    assert status_payload["next_phase"]["phase_id"] == "phase-221"

    assert cli.main(["work", "phases", "next", "--target", str(tmp_path), "--range", "220-222", "--json"]) == 0
    next_payload = json.loads(capsys.readouterr().out)
    assert next_payload["phase"]["phase_id"] == "phase-221"

    assert cli.main(["work", "phases", "report", "build", "--target", str(tmp_path), "--range", "220-222", "--json"]) == 0
    report_payload = json.loads(capsys.readouterr().out)
    report_id = report_payload["report_id"]
    assert (tmp_path / ".brigade" / "work" / "phases" / "reports" / report_id / "PHASE_REPORT.md").is_file()

    assert cli.main(["work", "phases", "report", "list", "--target", str(tmp_path), "--json"]) == 0
    reports = json.loads(capsys.readouterr().out)
    assert reports["report_count"] == 1

    assert cli.main(["work", "phases", "report", "show", "latest", "--target", str(tmp_path), "--json"]) == 0
    shown_report = json.loads(capsys.readouterr().out)
    assert shown_report["report_id"] == report_id

    assert cli.main(["work", "phases", "import-issues", "--target", str(tmp_path), "--range", "220-223", "--json"]) == 0
    imports = json.loads(capsys.readouterr().out)
    assert imports["created_count"] >= 1
    pending = work_cmd._pending_imports(tmp_path)
    assert any(item["source"] == "phase-ledger" for item in pending)

    assert cli.main(["work", "phases", "import-issues", "--target", str(tmp_path), "--range", "220-223", "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["created_count"] == 0
    assert second["skipped_count"] >= 1


def test_phase_ledger_closeout_records_review_state_and_doctor_warns_on_stale_unreviewed(tmp_path, capsys):
    assert phases_cmd.plan(target=tmp_path, phase_id="phase-230", title="Review me", source_goal="audit", json_output=True) == 0
    capsys.readouterr()
    assert phases_cmd.complete(
        target=tmp_path,
        phase_id="phase-230",
        status="pushed",
        summary="Done",
        files_changed=["file.py"],
        tests_run=["pytest"],
        commit_hash="abc123",
        push_ref="main",
        json_output=True,
    ) == 0
    completed = json.loads(capsys.readouterr().out)
    completed["completed_at"] = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    path = tmp_path / ".brigade" / "work" / "phases" / "records" / "phase-230.json"
    path.write_text(json.dumps(completed) + "\n")

    assert phases_cmd.doctor(target=tmp_path, json_output=True) == 0
    doctor_payload = json.loads(capsys.readouterr().out)
    assert any(check["name"] == "phase_stale_unreviewed_completed" for check in doctor_payload["checks"])

    assert cli.main(["work", "phases", "closeout", "phase-230", "--target", str(tmp_path), "--status", "reviewed", "--reason", "Checked evidence.", "--json"]) == 0
    closeout = json.loads(capsys.readouterr().out)
    assert closeout["status"] == "reviewed"
    assert closeout["phase_ids"] == ["phase-230"]
    assert closeout["reason"] == "Checked evidence."
    assert closeout["source_fingerprint"]
    assert (tmp_path / ".brigade" / "work" / "phases" / "closeouts" / f"{closeout['closeout_id']}.json").is_file()

    assert phases_cmd.doctor(target=tmp_path, json_output=True) == 0
    reviewed_doctor = json.loads(capsys.readouterr().out)
    assert not any(check["name"] == "phase_stale_unreviewed_completed" for check in reviewed_doctor["checks"])


def test_phase_ledger_closeout_range_and_deferred_state(tmp_path, capsys):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "240-241", "--title", "Range", "--goal", "audit", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "defer", "phase-241", "--target", str(tmp_path), "--reason", "Later", "--json"]) == 0
    capsys.readouterr()

    assert cli.main(["work", "phases", "closeout", "240-241", "--target", str(tmp_path), "--status", "deferred", "--reason", "Reviewed as a range.", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "deferred"
    assert payload["phase_ids"] == ["phase-240", "phase-241"]
    assert payload["deferred_phase_ids"] == ["phase-240", "phase-241"]
    assert payload["unresolved_issue_count"] >= 0
