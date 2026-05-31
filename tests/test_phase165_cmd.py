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
    assert phases_cmd.actions_build(target=tmp_path, json_output=True) == 0
    action_build = json.loads(capsys.readouterr().out)
    assert action_build["created_count"] >= 1

    assert daily_cmd.status(target=tmp_path, json_output=True) == 0
    daily_status = json.loads(capsys.readouterr().out)
    assert daily_status["phase_ledger"]["issue_count"] >= 1
    assert daily_status["phase_ledger"]["open_action_count"] >= 1

    assert daily_cmd.doctor(target=tmp_path, json_output=True) == 0
    daily_doctor = json.loads(capsys.readouterr().out)
    assert any(check["name"] == "phase_ledger_issue" for check in daily_doctor["checks"])

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["phase_ledger"]["issue_count"] >= 1
    assert brief["phase_ledger"]["open_action_count"] >= 1

    assert work_cmd.brief(target=tmp_path) == 0
    assert "phase_actions:" in capsys.readouterr().out

    assert work_cmd.doctor(target=tmp_path) in {0, 1}
    doctor_out = capsys.readouterr().out
    assert "phase_complete_without_tests" in doctor_out

    assert center_cmd.status(target=tmp_path, json_output=True) == 0
    center_status = json.loads(capsys.readouterr().out)
    assert center_status["phase_ledger"]["issue_count"] >= 1
    assert center_status["phase_ledger"]["open_action_count"] >= 1

    assert center_cmd.status(target=tmp_path) == 0
    assert "phase_actions:" in capsys.readouterr().out


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

    assert cli.main(["work", "phases", "report", "closeout", "latest", "--target", str(tmp_path), "--status", "reviewed", "--reason", "Report reviewed.", "--json"]) == 0
    report_closeout = json.loads(capsys.readouterr().out)
    assert report_closeout["report_id"] == report_id
    assert report_closeout["status"] == "reviewed"
    assert report_closeout["reason"] == "Report reviewed."
    assert (tmp_path / ".brigade" / "work" / "phases" / "reports" / report_id / "CLOSEOUT.json").is_file()

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


def test_phase_ledger_compare_detects_local_evidence_drift(tmp_path, capsys, monkeypatch):
    assert phases_cmd.plan(target=tmp_path, phase_id="phase-250", title="Compare", source_goal="audit", json_output=True) == 0
    capsys.readouterr()
    assert phases_cmd.complete(
        target=tmp_path,
        phase_id="phase-250",
        status="pushed",
        summary="Done",
        files_changed=["missing.py"],
        tests_run=["pytest"],
        commit_hash="old123",
        json_output=True,
    ) == 0
    record = json.loads(capsys.readouterr().out)
    record["completed_at"] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    record["doctor_issue_count"] = 0
    path = tmp_path / ".brigade" / "work" / "phases" / "records" / "phase-250.json"
    path.write_text(json.dumps(record) + "\n")
    assert phases_cmd.report_build(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    monkeypatch.setattr(phases_cmd, "_git_head", lambda target: "new456")

    assert cli.main(["work", "phases", "compare", "phase-250", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    names = {check["name"] for check in payload["checks"]}
    assert "phase_compare_changed_head" in names
    assert "phase_compare_missing_push_ref" in names
    assert "phase_compare_missing_referenced_files" in names
    assert "phase_compare_newer_phase_report" in names
    assert "phase_compare_newer_test_evidence" in names
    assert "phase_compare_changed_doctor_issue_count" in names

    assert cli.main(["work", "phases", "compare", "250-251", "--target", str(tmp_path), "--json"]) == 0
    range_payload = json.loads(capsys.readouterr().out)
    assert any(check["name"] == "phase_compare_missing_records" for check in range_payload["checks"])


def test_phase_ledger_actions_build_dedupe_and_transition(tmp_path, capsys):
    assert phases_cmd.plan(target=tmp_path, phase_id="phase-260", title="Action", source_goal="audit", json_output=True) == 0
    capsys.readouterr()
    assert phases_cmd.complete(target=tmp_path, phase_id="phase-260", summary="No evidence", json_output=True) == 0
    capsys.readouterr()

    assert cli.main(["work", "phases", "actions", "plan", "--target", str(tmp_path), "--json"]) == 0
    plan_payload = json.loads(capsys.readouterr().out)
    assert plan_payload["action_count"] >= 1

    assert cli.main(["work", "phases", "actions", "build", "--target", str(tmp_path), "--json"]) == 0
    build_payload = json.loads(capsys.readouterr().out)
    assert build_payload["created_count"] >= 1
    action_id = build_payload["created"][0]["action_id"]

    assert cli.main(["work", "phases", "actions", "build", "--target", str(tmp_path), "--json"]) == 0
    second_build = json.loads(capsys.readouterr().out)
    assert second_build["created_count"] == 0
    assert second_build["skipped_count"] >= 1

    assert cli.main(["work", "phases", "actions", "list", "--target", str(tmp_path), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["action_count"] >= 1

    assert cli.main(["work", "phases", "actions", "show", action_id, "--target", str(tmp_path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["action_id"] == action_id

    assert cli.main(["work", "phases", "actions", "start", action_id, "--target", str(tmp_path), "--json"]) == 0
    active = json.loads(capsys.readouterr().out)
    assert active["status"] == "active"

    assert cli.main(["work", "phases", "actions", "done", action_id, "--target", str(tmp_path), "--json"]) == 0
    done = json.loads(capsys.readouterr().out)
    assert done["status"] == "done"

    assert cli.main(["work", "phases", "actions", "archive", "--target", str(tmp_path), "--completed", "--json"]) == 0
    archived = json.loads(capsys.readouterr().out)
    assert archived["archived_count"] >= 1


def test_phase_ledger_actions_defer_requires_reason(tmp_path, capsys):
    assert phases_cmd.plan(target=tmp_path, phase_id="phase-261", title="Action", source_goal="audit", json_output=True) == 0
    capsys.readouterr()
    assert phases_cmd.complete(target=tmp_path, phase_id="phase-261", summary="No evidence", json_output=True) == 0
    capsys.readouterr()
    assert phases_cmd.actions_build(target=tmp_path, json_output=True) == 0
    build_payload = json.loads(capsys.readouterr().out)
    action_id = build_payload["created"][0]["action_id"]

    assert cli.main(["work", "phases", "actions", "defer", action_id, "--target", str(tmp_path), "--reason", "Waiting for review.", "--json"]) == 0
    deferred = json.loads(capsys.readouterr().out)
    assert deferred["status"] == "deferred"
    assert deferred["review_reason"] == "Waiting for review."


def test_phase_ledger_actions_import_issues_dedupes_open_actions(tmp_path, capsys):
    assert phases_cmd.plan(target=tmp_path, phase_id="phase-263", title="Import action", source_goal="audit", json_output=True) == 0
    capsys.readouterr()
    assert phases_cmd.complete(target=tmp_path, phase_id="phase-263", summary="No evidence", json_output=True) == 0
    capsys.readouterr()
    assert phases_cmd.actions_build(target=tmp_path, json_output=True) == 0
    build_payload = json.loads(capsys.readouterr().out)
    assert build_payload["created_count"] >= 1

    assert cli.main(["work", "phases", "actions", "import-issues", "--target", str(tmp_path), "--json"]) == 0
    imports = json.loads(capsys.readouterr().out)
    assert imports["created_count"] >= 1
    first = imports["created"][0]
    assert first["source"] == "phase-ledger-action"
    assert first["metadata"]["phase_action_id"]
    assert first["metadata"]["source_item_key"].startswith("phase-ledger-action:")

    assert cli.main(["work", "phases", "actions", "import-issues", "--target", str(tmp_path), "--json"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["created_count"] == 0
    assert second["skipped_count"] >= 1


def test_phase_report_compare_warns_on_missing_closeout_and_stale_report(tmp_path, capsys):
    assert phases_cmd.plan(target=tmp_path, phase_id="phase-262", title="Report compare", source_goal="audit", json_output=True) == 0
    capsys.readouterr()
    assert phases_cmd.complete(target=tmp_path, phase_id="phase-262", summary="Done", files_changed=["file.py"], tests_run=["pytest"], json_output=True) == 0
    capsys.readouterr()
    assert phases_cmd.report_build(target=tmp_path, phase_range="262", json_output=True) == 0
    report = json.loads(capsys.readouterr().out)

    assert cli.main(["work", "phases", "report", "compare", report["report_id"], "--target", str(tmp_path), "--json"]) == 1
    first = json.loads(capsys.readouterr().out)
    assert any(check["name"] == "phase_report_missing_closeout" for check in first["checks"])

    assert phases_cmd.report_closeout(target=tmp_path, report_id=report["report_id"], status="reviewed", reason="Reviewed.", json_output=True) == 0
    capsys.readouterr()
    health = phases_cmd.health(tmp_path)
    assert health["latest_report_compare"]["issue_count"] == 0

    assert cli.main(["work", "phases", "report", "compare", report["report_id"], "--target", str(tmp_path), "--json"]) == 0
    current = json.loads(capsys.readouterr().out)
    assert current["issue_count"] == 0

    record_path = tmp_path / ".brigade" / "work" / "phases" / "records" / "phase-262.json"
    record = json.loads(record_path.read_text())
    record["updated_at"] = datetime.now(timezone.utc).isoformat()
    record["tests_run"].append("pytest again")
    record_path.write_text(json.dumps(record) + "\n")

    assert cli.main(["work", "phases", "report", "compare", report["report_id"], "--target", str(tmp_path), "--json"]) == 1
    stale = json.loads(capsys.readouterr().out)
    names = {check["name"] for check in stale["checks"]}
    assert "phase_report_status_counts_changed" not in names
    assert "phase_report_newer_phase_record" in names


def test_phase_execution_session_lifecycle(tmp_path, capsys):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "211-213", "--title", "Session", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "complete", "phase-211", "--target", str(tmp_path), "--summary", "Done", "--file", "file.py", "--test", "pytest", "--json"]) == 0
    capsys.readouterr()

    assert cli.main(["work", "phases", "session", "start", "--target", str(tmp_path), "--range", "211-213", "--goal", "afk session", "--json"]) == 0
    session = json.loads(capsys.readouterr().out)
    session_id = session["session_id"]
    assert session["phase_range"] == "211-213"
    assert session["current_phase_id"] == "phase-212"
    assert session["phase_status"]["record_count"] == 3
    assert session["commit_summary"]["committed"] == 0
    assert (tmp_path / ".brigade" / "work" / "phases" / "sessions" / f"{session_id}.json").is_file()

    assert cli.main(["work", "phases", "session", "list", "--target", str(tmp_path), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["session_count"] == 1
    assert listed["sessions"][0]["session_id"] == session_id

    assert cli.main(["work", "phases", "session", "show", "latest", "--target", str(tmp_path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["session_id"] == session_id

    assert cli.main(["work", "phases", "session", "closeout", "latest", "--target", str(tmp_path), "--status", "reviewed", "--reason", "Checked session.", "--json"]) == 0
    closed = json.loads(capsys.readouterr().out)
    assert closed["status"] == "closed"
    assert closed["closeout"]["status"] == "reviewed"
    assert closed["closeout"]["reason"] == "Checked session."


def test_phase_session_next_and_resume_classify_safe_step(tmp_path, capsys):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "212-214", "--title", "Resume", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "complete", "phase-212", "--target", str(tmp_path), "--status", "pushed", "--summary", "Done", "--file", "file.py", "--test", "pytest", "--commit", "abc123", "--push-ref", "main", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "session", "start", "--target", str(tmp_path), "--range", "212-214", "--goal", "resume session", "--json"]) == 0
    session = json.loads(capsys.readouterr().out)

    assert cli.main(["work", "phases", "session", "next", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    next_payload = json.loads(capsys.readouterr().out)
    assert next_payload["next_step"]["step_type"] == "unreviewed_pushed_phase"
    assert next_payload["next_step"]["phase_id"] == "phase-212"
    assert "closeout phase-212" in next_payload["suggested_next_command"]

    assert cli.main(["work", "phases", "closeout", "phase-212", "--target", str(tmp_path), "--status", "reviewed", "--reason", "Reviewed.", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "session", "next", "latest", "--target", str(tmp_path), "--json"]) == 0
    next_after_review = json.loads(capsys.readouterr().out)
    assert next_after_review["next_step"]["step_type"] == "pending_phase"
    assert next_after_review["next_step"]["phase_id"] == "phase-213"

    assert cli.main(["work", "phases", "session", "resume", "latest", "--target", str(tmp_path), "--json"]) == 0
    resumed = json.loads(capsys.readouterr().out)
    assert resumed["executed"] is False
    assert resumed["resume"]["next_step"]["phase_id"] == "phase-213"

    assert cli.main(["work", "phases", "session", "show", "latest", "--target", str(tmp_path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["resume_history"][-1]["next_step"]["step_type"] == "pending_phase"


def test_phase_session_checkpoint_records_recovery_metadata(tmp_path, capsys):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "226-227", "--title", "Checkpoint", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "session", "start", "--target", str(tmp_path), "--range", "226-227", "--goal", "checkpoint session", "--json"]) == 0
    session = json.loads(capsys.readouterr().out)

    assert cli.main(
        [
            "work",
            "phases",
            "session",
            "checkpoint",
            session["session_id"],
            "--target",
            str(tmp_path),
            "--status",
            "blocked",
            "--summary",
            "Waiting on focused verification.",
            "--note",
            "No command executed.",
            "--json",
        ]
    ) == 0
    checkpoint = json.loads(capsys.readouterr().out)
    assert checkpoint["session_id"] == session["session_id"]
    assert checkpoint["phase_id"] == "phase-226"
    assert checkpoint["status"] == "blocked"
    assert checkpoint["summary"] == "Waiting on focused verification."
    assert checkpoint["notes"] == ["No command executed."]
    assert "source_fingerprint" in checkpoint
    assert (tmp_path / ".brigade" / "work" / "phases" / "session-checkpoints" / f"{checkpoint['checkpoint_id']}.json").is_file()

    assert cli.main(["work", "phases", "session", "show", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["latest_checkpoint"]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert shown["checkpoint_references"][-1]["status"] == "blocked"

    assert cli.main(["work", "phases", "session", "checkpoints", "list", "--target", str(tmp_path), "--session", session["session_id"], "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["checkpoint_count"] == 1
    assert listed["checkpoints"][0]["checkpoint_id"] == checkpoint["checkpoint_id"]

    assert cli.main(["work", "phases", "session", "checkpoints", "show", "latest", "--target", str(tmp_path), "--json"]) == 0
    checkpoint_shown = json.loads(capsys.readouterr().out)
    assert checkpoint_shown["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert checkpoint_shown["next_step"]["step_type"] == "pending_phase"

    assert cli.main(["work", "phases", "session", "checkpoints", "compare", "latest", "--target", str(tmp_path), "--json"]) == 0
    current_compare = json.loads(capsys.readouterr().out)
    assert current_compare["issue_count"] == 0
    assert current_compare["checks"][0]["name"] == "phase_session_checkpoint_current"

    assert cli.main(["work", "phases", "start", "phase-226", "--target", str(tmp_path), "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "session", "checkpoints", "compare", checkpoint["checkpoint_id"], "--target", str(tmp_path), "--json"]) == 0
    stale_compare = json.loads(capsys.readouterr().out)
    names = {check["name"] for check in stale_compare["checks"]}
    assert "phase_session_checkpoint_step_changed" in names
    assert "phase_session_checkpoint_fingerprint_changed" in names

    assert cli.main(["work", "phases", "session", "next", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    next_with_checkpoint = json.loads(capsys.readouterr().out)
    assert next_with_checkpoint["checkpoint"]["latest_checkpoint"]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert next_with_checkpoint["checkpoint"]["issue_count"] >= 1
    assert next_with_checkpoint["checkpoint"]["suggested_next_command"] == "brigade work phases session checkpoints import-issues latest"

    assert cli.main(["work", "phases", "session", "resume", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    resume_with_checkpoint = json.loads(capsys.readouterr().out)
    assert resume_with_checkpoint["resume"]["checkpoint"]["latest_checkpoint"]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert resume_with_checkpoint["resume"]["checkpoint"]["issue_count"] >= 1

    assert cli.main(["work", "phases", "session", "checkpoints", "import-issues", checkpoint["checkpoint_id"], "--target", str(tmp_path), "--dry-run", "--json"]) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["dry_run"] is True
    assert dry_run["created_count"] >= 1
    assert dry_run["created"][0]["source"] == "phase-session-checkpoint"
    assert work_cmd._read_imports(tmp_path) == []

    assert cli.main(["work", "phases", "session", "checkpoints", "import-issues", checkpoint["checkpoint_id"], "--target", str(tmp_path), "--json"]) == 0
    imported = json.loads(capsys.readouterr().out)
    assert imported["created_count"] >= 1
    imports = work_cmd._read_imports(tmp_path)
    checkpoint_import = next(item for item in imports if item["source"] == "phase-session-checkpoint")
    assert checkpoint_import["metadata"]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert checkpoint_import["metadata"]["session_id"] == session["session_id"]
    assert checkpoint_import["metadata"]["source_item_key"].startswith("phase-session-checkpoint:")
    assert checkpoint_import["acceptance"]

    assert cli.main(["work", "phases", "session", "checkpoints", "import-issues", checkpoint["checkpoint_id"], "--target", str(tmp_path), "--json"]) == 0
    deduped = json.loads(capsys.readouterr().out)
    assert deduped["created_count"] == 0
    assert deduped["skipped_count"] >= 1

    assert cli.main(["work", "phases", "session", "activity", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    activity = json.loads(capsys.readouterr().out)
    assert any(event["event_type"] == "session-checkpoint" and event["local_id"] == checkpoint["checkpoint_id"] for event in activity["events"])


def test_phase_session_recovery_notes_are_reviewable(tmp_path, capsys):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "231-232", "--title", "Recovery", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "session", "start", "--target", str(tmp_path), "--range", "231-232", "--goal", "recovery session", "--json"]) == 0
    session = json.loads(capsys.readouterr().out)

    assert cli.main(
        [
            "work",
            "phases",
            "session",
            "recovery-note",
            session["session_id"],
            "--target",
            str(tmp_path),
            "--summary",
            "Need to resume after focused tests.",
            "--note",
            "Only local metadata was written.",
            "--evidence",
            "tests/test_phase165_cmd.py",
            "--json",
        ]
    ) == 0
    note = json.loads(capsys.readouterr().out)
    assert note["session_id"] == session["session_id"]
    assert note["phase_id"] == "phase-231"
    assert note["status"] == "open"
    assert note["notes"] == ["Only local metadata was written."]
    assert note["evidence"] == ["tests/test_phase165_cmd.py"]
    assert "source_fingerprint" in note

    assert cli.main(["work", "phases", "session", "recovery-notes", "list", "--target", str(tmp_path), "--session", session["session_id"], "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["note_count"] == 1
    assert listed["notes"][0]["note_id"] == note["note_id"]

    assert cli.main(["work", "phases", "session", "recovery-notes", "show", "latest", "--target", str(tmp_path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["note_id"] == note["note_id"]
    assert shown["next_step"]["step_type"] == "pending_phase"

    assert cli.main(["work", "phases", "session", "recovery-notes", "closeout", note["note_id"], "--target", str(tmp_path), "--status", "reviewed", "--reason", "Recovery context reviewed.", "--json"]) == 0
    closeout = json.loads(capsys.readouterr().out)
    assert closeout["note"]["status"] == "reviewed"
    assert closeout["closeout"]["reason"] == "Recovery context reviewed."

    assert cli.main(["work", "phases", "session", "show", "latest", "--target", str(tmp_path), "--json"]) == 0
    session_shown = json.loads(capsys.readouterr().out)
    assert session_shown["latest_recovery_note"]["note_id"] == note["note_id"]
    assert session_shown["latest_recovery_note"]["status"] == "reviewed"

    assert cli.main(["work", "phases", "session", "activity", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    activity = json.loads(capsys.readouterr().out)
    assert any(event["event_type"] == "session-recovery-note" and event["local_id"] == note["note_id"] and event["status"] == "reviewed" for event in activity["events"])


def test_phase_session_risk_summarizes_checkpoint_notes_and_doctor(tmp_path, capsys):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "235-236", "--title", "Risk", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "session", "start", "--target", str(tmp_path), "--range", "235-236", "--goal", "risk session", "--json"]) == 0
    session = json.loads(capsys.readouterr().out)
    assert cli.main(["work", "phases", "session", "checkpoint", session["session_id"], "--target", str(tmp_path), "--status", "blocked", "--summary", "Checkpoint blocked.", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "session", "recovery-note", session["session_id"], "--target", str(tmp_path), "--summary", "Open recovery note.", "--json"]) == 0
    capsys.readouterr()

    assert cli.main(["work", "phases", "session", "risk", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    risk = json.loads(capsys.readouterr().out)
    assert risk["session_id"] == session["session_id"]
    assert risk["risk_level"] == "high"
    names = {item["name"] for item in risk["risks"]}
    assert "phase_session_checkpoint_risk" in names
    assert "phase_session_open_recovery_notes" in names
    assert risk["open_recovery_note_count"] == 1
    assert risk["checkpoint"]["issue_count"] >= 1


def test_phase_session_verification_rollup(tmp_path, capsys):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "236-237", "--title", "Verify Session", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "complete", "phase-236", "--target", str(tmp_path), "--summary", "Done", "--test", "pytest focused", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "verify", "record", "phase-236", "--target", str(tmp_path), "--command", "pytest focused", "--status", "passed", "--summary", "Passed.", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "session", "start", "--target", str(tmp_path), "--range", "236-237", "--goal", "verification session", "--json"]) == 0
    session = json.loads(capsys.readouterr().out)

    assert cli.main(["work", "phases", "session", "verification", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session_id"] == session["session_id"]
    assert payload["record_count"] == 2
    assert payload["status_counts"]["passed"] == 1
    assert "phase-237" in payload["missing_verification_phase_ids"]
    assert payload["issue_count"] == 1
    assert payload["suggested_next_command"] == "brigade work phases verify plan phase-237"


def test_phase_session_privacy_rollup(tmp_path, capsys):
    (tmp_path / "safe.txt").write_text("safe public text\n")
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "237-238", "--title", "Privacy Session", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "evidence", "add", "phase-237", "--target", str(tmp_path), "--file", "safe.txt", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "privacy", "phase-237", "--target", str(tmp_path), "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "session", "start", "--target", str(tmp_path), "--range", "237-238", "--goal", "privacy session", "--json"]) == 0
    session = json.loads(capsys.readouterr().out)

    assert cli.main(["work", "phases", "session", "privacy", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["record_count"] == 2
    assert payload["status_counts"]["clean"] == 1
    assert payload["status_counts"]["missing"] == 1
    assert payload["missing_privacy_phase_ids"] == ["phase-238"]
    assert payload["suggested_next_command"] == "brigade work phases privacy phase-238"


def test_phase_session_report_bundle(tmp_path, capsys):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "213-214", "--title", "Report", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "complete", "phase-213", "--target", str(tmp_path), "--summary", "Done", "--file", "file.py", "--test", "pytest", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "session", "start", "--target", str(tmp_path), "--range", "213-214", "--goal", "report session", "--json"]) == 0
    session = json.loads(capsys.readouterr().out)

    assert cli.main(["work", "phases", "session", "report", "build", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    report_id = report["report_id"]
    report_dir = tmp_path / ".brigade" / "work" / "phases" / "session-reports" / report_id
    assert (report_dir / "SESSION_REPORT.md").is_file()
    assert (report_dir / "SESSION_EVIDENCE.json").is_file()
    assert report["session"]["session_id"] == session["session_id"]
    assert report["next"]["next_step"]["phase_id"] == "phase-214"
    assert "commit_summary" in report
    assert "test_summary" in report

    assert cli.main(["work", "phases", "session", "report", "list", "--target", str(tmp_path), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["report_count"] == 1

    assert cli.main(["work", "phases", "session", "report", "show", "latest", "--target", str(tmp_path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["report_id"] == report_id


def test_phase_session_activity_timeline(tmp_path, capsys):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "221-222", "--title", "Activity", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "start", "phase-221", "--target", str(tmp_path), "--json"]) == 0
    capsys.readouterr()
    assert cli.main(
        [
            "work",
            "phases",
            "complete",
            "phase-221",
            "--target",
            str(tmp_path),
            "--status",
            "committed",
            "--summary",
            "Added a chronological activity timeline.",
            "--file",
            "src/brigade/phases_cmd.py",
            "--test",
            "pytest tests/test_phase165_cmd.py -q",
            "--commit",
            "abc123",
            "--json",
        ]
    ) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "complete", "phase-222", "--target", str(tmp_path), "--summary", "Needs more evidence.", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "handoff", "phase-221", "--target", str(tmp_path), "--lint", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "actions", "build", "--target", str(tmp_path), "--range", "221-222", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "report", "build", "--target", str(tmp_path), "--range", "221-222", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "session", "start", "--target", str(tmp_path), "--range", "221-222", "--goal", "activity session", "--json"]) == 0
    session = json.loads(capsys.readouterr().out)
    assert cli.main(["work", "phases", "session", "resume", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "session", "report", "build", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    capsys.readouterr()

    assert cli.main(["work", "phases", "session", "activity", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    event_types = {event["event_type"] for event in payload["events"]}
    assert {
        "session-started",
        "session-resume",
        "phase-started",
        "phase-completed",
        "phase-test-recorded",
        "phase-commit-recorded",
        "phase-action",
        "phase-report",
        "phase-report-compare",
        "phase-handoff-drafted",
        "session-report",
    } <= event_types
    assert payload["event_count"] == len(payload["events"])


def test_phase_session_progress_summary(tmp_path, capsys):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "222-224", "--title", "Progress", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "complete", "phase-222", "--target", str(tmp_path), "--summary", "Done", "--test", "pytest one", "--commit", "abc222", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "defer", "phase-223", "--target", str(tmp_path), "--reason", "Deferred in test.", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "session", "start", "--target", str(tmp_path), "--range", "222-224", "--goal", "progress session", "--json"]) == 0
    session = json.loads(capsys.readouterr().out)

    assert cli.main(["work", "phases", "session", "progress", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["percent_complete"] == 66.7
    assert payload["status_counts"]["implemented"] == 1
    assert payload["status_counts"]["deferred"] == 1
    assert payload["current_phase_id"] == "phase-224"
    assert payload["test_coverage"]["with_tests"] == 1
    assert payload["commit_summary"]["with_commit"] == 1
    assert payload["push_summary"]["with_push_ref"] == 0
    assert payload["estimated_remaining_local_steps"] >= 1


def test_phase_session_import_issues_dedupes_blockers(tmp_path, capsys):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "223-224", "--title", "Imports", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "complete", "phase-223", "--target", str(tmp_path), "--summary", "Missing tests.", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "session", "start", "--target", str(tmp_path), "--range", "223-224", "--goal", "import session", "--json"]) == 0
    session = json.loads(capsys.readouterr().out)

    assert cli.main(["work", "phases", "session", "import-issues", session["session_id"], "--target", str(tmp_path), "--dry-run", "--json"]) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["created_count"] >= 1
    assert work_cmd._read_imports(tmp_path) == []

    assert cli.main(["work", "phases", "session", "import-issues", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["created_count"] >= 1
    imports = work_cmd._read_imports(tmp_path)
    assert imports[0]["source"] == "phase-session"
    assert imports[0]["metadata"]["session_id"] == session["session_id"]
    assert imports[0]["metadata"]["issue_type"]
    assert imports[0]["acceptance"]

    assert cli.main(["work", "phases", "session", "import-issues", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    skipped = json.loads(capsys.readouterr().out)
    assert skipped["created_count"] == 0
    assert skipped["skipped_count"] >= 1

    imports[0]["status"] = "dismissed"
    work_cmd._write_imports(tmp_path, imports)
    assert cli.main(["work", "phases", "session", "import-issues", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    dismissed_skip = json.loads(capsys.readouterr().out)
    assert dismissed_skip["created_count"] == 0
    assert dismissed_skip["skipped"][0]["status"] == "dismissed"


def test_phase_goal_scaffold_builds_safe_goal_draft(tmp_path, capsys):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "224-225", "--title", "Goal", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "complete", "phase-224", "--target", str(tmp_path), "--summary", "Needs test evidence.", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "session", "start", "--target", str(tmp_path), "--range", "224-225", "--goal", "goal session", "--json"]) == 0
    session = json.loads(capsys.readouterr().out)

    assert cli.main(["work", "phases", "goal", "scaffold", "--target", str(tmp_path), "--range", "224-225", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["phase_range"] == "224-225"
    assert payload["session_id"] == session["session_id"]
    assert payload["blocker_count"] >= 1
    goal_path = tmp_path / payload["path"]
    assert goal_path.is_file()
    content = goal_path.read_text()
    assert content.startswith("/goal Brigade phases 224-225")
    assert "Use docs/phase-execution-ledger.md" in content
    assert str(tmp_path) not in content
    assert "raw logs" in content


def test_phase_session_gate_blocks_then_passes_when_evidence_is_complete(tmp_path, capsys):
    evidence_file = tmp_path / "safe.txt"
    evidence_file.write_text("safe public evidence\n")
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "225-226", "--title", "Gate", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    for phase_id in ("phase-225", "phase-226"):
        assert cli.main(
            [
                "work",
                "phases",
                "complete",
                phase_id,
                "--target",
                str(tmp_path),
                "--status",
                "pushed",
                "--summary",
                "Complete gate evidence.",
                "--file",
                "safe.txt",
                "--test",
                "pytest tests/test_phase165_cmd.py -q",
                "--commit",
                phase_id,
                "--push-ref",
                "main",
                "--json",
            ]
        ) == 0
        capsys.readouterr()
    assert cli.main(["work", "phases", "session", "start", "--target", str(tmp_path), "--range", "225-226", "--goal", "gate session", "--json"]) == 0
    session = json.loads(capsys.readouterr().out)

    assert cli.main(["work", "phases", "session", "gate", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    blocked = json.loads(capsys.readouterr().out)
    blocked_names = {check["name"] for check in blocked["checks"]}
    assert blocked["safe_to_claim_complete"] is False
    assert "phase_session_gate_missing_privacy_check" in blocked_names
    assert "phase_session_gate_missing_handoff" in blocked_names
    assert "phase_session_gate_missing_phase_report" in blocked_names

    assert cli.main(["work", "phases", "privacy", "225-226", "--target", str(tmp_path), "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "handoff", "225-226", "--target", str(tmp_path), "--lint", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "report", "build", "--target", str(tmp_path), "--range", "225-226", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert cli.main(["work", "phases", "report", "closeout", report["report_id"], "--target", str(tmp_path), "--status", "reviewed", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "session", "report", "build", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "session", "closeout", session["session_id"], "--target", str(tmp_path), "--status", "reviewed", "--json"]) == 0
    capsys.readouterr()

    assert cli.main(["work", "phases", "session", "gate", session["session_id"], "--target", str(tmp_path), "--json"]) == 0
    ready = json.loads(capsys.readouterr().out)
    assert ready["safe_to_claim_complete"] is True
    assert ready["blocker_count"] == 0
    assert ready["phase_report"]["report_id"] == report["report_id"]
    assert ready["session_report"]["report_id"]


def test_daily_driver_surfaces_and_runs_phase_session_step(tmp_path, capsys):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "214-215", "--title", "Daily", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "session", "start", "--target", str(tmp_path), "--range", "214-215", "--goal", "daily session", "--json"]) == 0
    session = json.loads(capsys.readouterr().out)

    assert daily_cmd.status(target=tmp_path, json_output=True) == 0
    status_payload = json.loads(capsys.readouterr().out)
    assert status_payload["phase_session"]["session_id"] == session["session_id"]

    assert daily_cmd.plan(target=tmp_path, json_output=True) == 0
    plan_payload = json.loads(capsys.readouterr().out)
    assert plan_payload["selected_action"]["source_subsystem"] == "phase-session"
    assert plan_payload["selected_action"]["action_type"] == "write-phase-session-checkpoint"

    assert daily_cmd.review(target=tmp_path, json_output=True) == 0
    review_payload = json.loads(capsys.readouterr().out)
    assert review_payload["selected_adapter"] == "brigade work phases session checkpoint"

    assert daily_cmd.run(target=tmp_path, json_output=True) == 0
    run_payload = json.loads(capsys.readouterr().out)
    assert run_payload["status"] == "completed"
    assert run_payload["adapter_result"]["action_type"] == "write-phase-session-checkpoint"
    assert (tmp_path / ".brigade" / "work" / "phases" / "session-checkpoints").is_dir()

    assert daily_cmd.doctor(target=tmp_path, json_output=True) == 0
    doctor_payload = json.loads(capsys.readouterr().out)
    assert any(check["name"] == "phase_session_active" for check in doctor_payload["checks"])

    assert work_cmd.brief(target=tmp_path, json_output=True) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["phase_ledger"]["latest_session"]["session_id"] == session["session_id"]

    assert center_cmd.status(target=tmp_path, json_output=True) == 0
    center_status = json.loads(capsys.readouterr().out)
    assert center_status["phase_ledger"]["latest_session"]["session_id"] == session["session_id"]

    assert center_cmd.reviews(target=tmp_path, json_output=True) == 0
    center_reviews = json.loads(capsys.readouterr().out)
    assert any(item["subsystem"] == "phase-session" for item in center_reviews["reviews"])


def test_phase_evidence_add_attaches_metadata_and_doctor_warns_on_missing_refs(tmp_path, capsys):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--phase-id", "phase-216", "--title", "Evidence", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(
        [
            "work",
            "phases",
            "evidence",
            "add",
            "phase-216",
            "--target",
            str(tmp_path),
            "--file",
            "missing.py",
            "--test",
            "pytest tests/test_phase165_cmd.py -q",
            "--test-result",
            "passed",
            "--report-id",
            "report-1",
            "--handoff",
            ".claude/memory-handoffs/missing.md",
            "--note",
            "attached by test",
            "--json",
        ]
    ) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["files_changed"] == ["missing.py"]
    assert record["tests_run"] == ["pytest tests/test_phase165_cmd.py -q"]
    assert record["test_result_summary"] == "passed"
    assert record["evidence_attachments"][0]["report_ids"] == ["report-1"]

    assert cli.main(["work", "phases", "complete", "phase-216", "--target", str(tmp_path), "--summary", "Done", "--json"]) == 0
    capsys.readouterr()
    assert phases_cmd.doctor(target=tmp_path, json_output=True) == 0
    doctor_payload = json.loads(capsys.readouterr().out)
    assert any(check["name"] == "phase_evidence_missing_reference" for check in doctor_payload["checks"])


def test_phase_verification_plan_and_record(tmp_path, capsys):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "217-218", "--title", "Verify", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "complete", "phase-217", "--target", str(tmp_path), "--summary", "Done", "--test", "pytest existing", "--json"]) == 0
    capsys.readouterr()

    assert cli.main(["work", "phases", "verify", "plan", "217-218", "--target", str(tmp_path), "--json"]) == 0
    plan_payload = json.loads(capsys.readouterr().out)
    assert plan_payload["record_count"] == 2
    first = next(record for record in plan_payload["records"] if record["phase_id"] == "phase-217")
    assert first["verification"][0]["command"] == "pytest existing"
    second = next(record for record in plan_payload["records"] if record["phase_id"] == "phase-218")
    assert second["verification"][0]["status"] == "deferred"

    assert cli.main(["work", "phases", "verify", "record", "phase-217", "--target", str(tmp_path), "--command", "pytest existing", "--status", "passed", "--summary", "passed", "--json"]) == 0
    recorded = json.loads(capsys.readouterr().out)
    assert recorded["recorded"]["status"] == "passed"
    assert recorded["recorded"]["summary"] == "passed"

    assert cli.main(["work", "phases", "verify", "plan", "phase-217", "--target", str(tmp_path), "--json"]) == 0
    updated = json.loads(capsys.readouterr().out)
    assert updated["records"][0]["verification"][0]["status"] == "passed"


def test_phase_reconcile_reports_git_evidence_warnings(tmp_path, capsys, monkeypatch):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "218-219", "--title", "Reconcile", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "complete", "phase-218", "--target", str(tmp_path), "--status", "pushed", "--summary", "Done", "--file", "file.py", "--test", "pytest", "--commit", "deadbeef", "--json"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(phases_cmd, "_git_commit_exists", lambda target, commit_hash: False)
    monkeypatch.setattr(phases_cmd, "_git_commit_on_branch", lambda target, commit_hash: False)
    monkeypatch.setattr(phases_cmd, "_git_dirty_paths", lambda target: ["dirty.py"])

    assert cli.main(["work", "phases", "reconcile", "218-219", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    names = {check["name"] for check in payload["checks"]}
    assert "phase_reconcile_dirty_worktree" in names
    assert "phase_reconcile_commit_missing" in names
    assert "phase_reconcile_pushed_without_ref" in names


def test_phase_privacy_scans_evidence_and_records_summary(tmp_path, capsys):
    public_file = tmp_path / "public.txt"
    public_file.write_text("api_" + "key=example\n")
    safe_public_file = tmp_path / "safe-public-url.txt"
    safe_public_file.write_text("See https://example.com/docs for public docs.\n")
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--phase-id", "phase-219", "--title", "Privacy", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "evidence", "add", "phase-219", "--target", str(tmp_path), "--file", "public.txt", "--json"]) == 0
    capsys.readouterr()

    assert cli.main(["work", "phases", "privacy", "phase-219", "--target", str(tmp_path), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "blocked"
    assert payload["finding_count"] == 1
    assert payload["findings"][0]["name"] == "phase_privacy_token_like"
    assert "example" not in payload["findings"][0]["detail"]

    assert cli.main(["work", "phases", "show", "phase-219", "--target", str(tmp_path), "--json"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["privacy_checks"][-1]["status"] == "blocked"

    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--phase-id", "phase-220", "--title", "Safe URL", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "evidence", "add", "phase-220", "--target", str(tmp_path), "--file", "safe-public-url.txt", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["work", "phases", "privacy", "phase-220", "--target", str(tmp_path), "--json"]) == 0
    safe_payload = json.loads(capsys.readouterr().out)
    assert safe_payload["status"] == "clean"


def test_phase_handoff_drafts_and_lints_memory_handoff(tmp_path, capsys):
    assert cli.main(["work", "phases", "plan", "--target", str(tmp_path), "--range", "220-221", "--title", "Handoff", "--goal", "afk", "--json"]) == 0
    capsys.readouterr()
    assert cli.main(
        [
            "work",
            "phases",
            "complete",
            "phase-220",
            "--target",
            str(tmp_path),
            "--summary",
            "Added a reusable phase handoff helper.",
            "--file",
            "src/brigade/phases_cmd.py",
            "--test",
            "pytest tests/test_phase165_cmd.py -q",
            "--json",
        ]
    ) == 0
    capsys.readouterr()

    assert cli.main(["work", "phases", "handoff", "phase-220", "--target", str(tmp_path), "--lint", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["lint"]["status"] == "passed"
    handoff_path = tmp_path / payload["path"]
    assert handoff_path.is_file()
    content = handoff_path.read_text()
    assert "## Recommended memory action\nno-card" in content
    assert "## Target document\n.learnings/LEARNINGS.md" in content

    assert cli.main(["work", "phases", "show", "phase-220", "--target", str(tmp_path), "--json"]) == 0
    record = json.loads(capsys.readouterr().out)
    assert record["phase_handoffs"][-1]["lint"]["status"] == "passed"
    assert record["evidence_attachments"][-1]["handoff_paths"] == [payload["path"]]
