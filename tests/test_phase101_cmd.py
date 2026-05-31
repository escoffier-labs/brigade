import json
from pathlib import Path

from brigade import center_cmd, cli, daily_cmd, handoff_cmd, work_cmd


def _write_command_inventory(path, capsys=None):
    (path / "README.md").write_text("Use `brigade roadmap commands`.\n")
    (path / "ROADMAP.md").write_text("# Roadmap\n")
    assert cli.main(["roadmap", "commands", "--target", str(path), "--write", "--json"]) == 0
    if capsys is not None:
        capsys.readouterr()


def _write_release_receipt(path, *, ready=True):
    run_dir = path / ".brigade" / "release" / "runs" / "release-ready"
    run_dir.mkdir(parents=True)
    (run_dir / "receipt.json").write_text(
        json.dumps(
            {
                "run_id": "release-ready",
                "status": "ready" if ready else "blocked",
                "ready": ready,
                "started_at": "2026-05-30T00:00:00+00:00",
                "completed_at": "2026-05-30T00:01:00+00:00",
                "blockers": [] if ready else ["blocked"],
                "warnings": [],
                "checks": [],
            }
        )
    )


def _seed_ready_repo(path, capsys=None):
    _write_command_inventory(path, capsys)
    _write_release_receipt(path)


def _daily_action(action_type, *, source_subsystem="test", source_local_id="item", metadata=None, approval_required=False, risk_level="low"):
    return daily_cmd._candidate(
        target=Path("."),
        action_type=action_type,
        source_subsystem=source_subsystem,
        source_local_id=source_local_id,
        safe_summary=f"{action_type} action",
        suggested_next_command=f"brigade {action_type}",
        score=500,
        ranking_reasons=["test"],
        approval_required=approval_required,
        approval_reason="approval required" if approval_required else None,
        risk_level=risk_level,
        metadata=metadata or {},
    )


def test_daily_status_text_and_json(tmp_path, capsys):
    _seed_ready_repo(tmp_path, capsys)
    work_cmd._add_task(
        tmp_path,
        "Implement daily loop",
        priority="high",
        acceptance=["Daily plan chooses one action."],
    )

    assert daily_cmd.status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["pending_task_count"] == 1
    assert payload["selected_action"]["source_subsystem"] == "work-task"
    assert payload["next_recommended_command"] == "brigade work run"

    assert cli.main(["daily", "status", "--target", str(tmp_path)]) == 0
    assert "daily status:" in capsys.readouterr().out


def test_daily_init_schema_history_show_and_doctor(tmp_path, monkeypatch, capsys):
    _seed_ready_repo(tmp_path, capsys)

    assert cli.main(["daily", "init", "--target", str(tmp_path), "--json"]) == 0
    init_payload = json.loads(capsys.readouterr().out)
    assert init_payload["written"] is True
    assert (tmp_path / ".brigade" / "daily.toml").is_file()

    assert cli.main(["daily", "schema", "--target", str(tmp_path), "--json"]) == 0
    schema_payload = json.loads(capsys.readouterr().out)
    names = {item["name"] for item in schema_payload["schemas"]}
    assert {"daily-status", "daily-plan", "daily-review", "daily-run", "daily-doctor"} <= names

    work_cmd._add_task(tmp_path, "History task", acceptance=["History records exist."])
    monkeypatch.setattr(work_cmd, "run", lambda *args, **kwargs: 0)
    assert daily_cmd.run(target=tmp_path, json_output=True) == 0
    run_receipt = json.loads(capsys.readouterr().out)

    assert cli.main(["daily", "history", "--target", str(tmp_path), "--json"]) == 0
    history_payload = json.loads(capsys.readouterr().out)
    assert history_payload["run_count"] == 1
    assert history_payload["plan_count"] == 1

    assert cli.main(["daily", "show", "latest", "--target", str(tmp_path), "--json"]) == 0
    show_payload = json.loads(capsys.readouterr().out)
    assert show_payload["run_id"] == run_receipt["run_id"]

    assert cli.main(["daily", "doctor", "--target", str(tmp_path), "--json"]) == 0
    doctor_payload = json.loads(capsys.readouterr().out)
    assert doctor_payload["health"]["run_count"] == 1


def test_daily_config_validation_preferred_mode_and_unsafe_warning(tmp_path, capsys):
    _seed_ready_repo(tmp_path, capsys)
    (tmp_path / ".brigade").mkdir(exist_ok=True)
    (tmp_path / ".brigade" / "daily.toml").write_text(
        "\n".join(
            [
                'preferred_mode = "inbox-first"',
                'max_risk_without_approval = "high"',
                "allow_context_pack_build = true",
                "allow_operator_report_build = true",
                "allow_readiness_imports = true",
                "allow_import_promotion_with_approval = true",
                "allow_work_run = true",
                "stale_plan_threshold_hours = 12",
                "stale_run_threshold_hours = 12",
            ]
        )
        + "\n"
    )
    work_cmd._add_task(tmp_path, "Lower task", acceptance=["Task acceptance exists."])
    work_cmd._append_import_records(
        tmp_path,
        [
            {
                "text": "Preferred import",
                "kind": "task",
                "source": "scanner",
                "priority": "high",
                "acceptance": ["Import acceptance exists."],
                "metadata": {"source_fingerprint": "preferred-import"},
            }
        ],
    )

    assert daily_cmd.plan(target=tmp_path, json_output=True) == 0
    plan_payload = json.loads(capsys.readouterr().out)
    assert plan_payload["selected_action"]["source_subsystem"] == "work-import"

    assert daily_cmd.doctor(target=tmp_path, json_output=True) == 0
    doctor_payload = json.loads(capsys.readouterr().out)
    assert any(check["name"] == "daily_risk_policy" for check in doctor_payload["checks"])

    (tmp_path / ".brigade" / "daily.toml").write_text('preferred_mode = "sideways"\n')
    assert daily_cmd.doctor(target=tmp_path, json_output=True) == 1
    invalid_payload = json.loads(capsys.readouterr().out)
    assert any(check["name"] == "daily_preferred_mode" for check in invalid_payload["checks"])


def test_daily_plan_ranks_tasks_imports_center_actions_and_readiness(tmp_path, capsys):
    _seed_ready_repo(tmp_path, capsys)
    task, _ = work_cmd._add_task(
        tmp_path,
        "Top accepted task",
        priority="normal",
        acceptance=["Task acceptance exists."],
    )
    work_cmd._append_import_records(
        tmp_path,
        [
            {
                "text": "High import",
                "kind": "task",
                "source": "scanner",
                "priority": "high",
                "acceptance": ["Import acceptance exists."],
                "metadata": {"source_fingerprint": "import-fp"},
            }
        ],
    )
    center_cmd._write_actions(
        tmp_path,
        [
            {
                "action_id": "action-one",
                "status": "pending",
                "safe_summary": "Reviewed action",
                "source_fingerprint": "action-fp",
                "created_at": "2026-05-30T00:00:00+00:00",
                "updated_at": "2026-05-30T00:00:00+00:00",
            }
        ],
    )

    assert daily_cmd.plan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected_action"]["metadata"]["task_id"] == task["id"]
    subsystems = {item["source_subsystem"] for item in payload["candidate_actions"]}
    assert {"work-task", "work-import", "center-action"} <= subsystems

    blocked = tmp_path / "blocked"
    blocked.mkdir()
    _write_command_inventory(blocked, capsys)
    assert daily_cmd.plan(target=blocked, json_output=True) == 0
    blocked_payload = json.loads(capsys.readouterr().out)
    assert blocked_payload["selected_action"]["source_subsystem"] == "center-readiness"


def test_daily_plan_records_and_review_previews_action(tmp_path, capsys):
    _seed_ready_repo(tmp_path, capsys)
    task, _ = work_cmd._add_task(
        tmp_path,
        "Context backed task",
        acceptance=["Context exists."],
    )

    assert cli.main(["daily", "plan", "--target", str(tmp_path), "--record", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["recorded"] is True
    assert (tmp_path / ".brigade" / "daily" / "plans" / payload["plan_id"] / "plan.json").is_file()

    assert daily_cmd.review(target=tmp_path, json_output=True) == 0
    review = json.loads(capsys.readouterr().out)
    assert review["source_subsystem"] == "work-task"
    assert review["acceptance"] == ["Context exists."]
    assert review["context_pack_plan"]["task"]["id"] == task["id"]
    assert review["approval_boundary"] == "no explicit approval required"
    assert review["selected_adapter"] == "brigade work run"
    assert review["context_pack_would_build"] is True
    assert review["config_blockers"] == []


def test_daily_plan_includes_handoff_memory_and_security_issues(tmp_path, capsys):
    _seed_ready_repo(tmp_path, capsys)
    inbox = tmp_path / ".claude" / "memory-handoffs"
    inbox.mkdir(parents=True)
    (inbox / "broken.md").write_text("# not a valid handoff\n")

    assert daily_cmd.plan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    subsystems = {item["source_subsystem"] for item in payload["candidate_actions"]}
    assert {"handoff", "memory-care", "security"} <= subsystems


def test_daily_run_refuses_approval_required_import_and_promotes_when_approved(tmp_path, capsys):
    _seed_ready_repo(tmp_path, capsys)
    imported, _, _ = work_cmd._append_import_records(
        tmp_path,
        [
            {
                "text": "Promote me",
                "kind": "task",
                "source": "scanner",
                "priority": "high",
                "acceptance": ["Task is promoted."],
                "metadata": {"source_fingerprint": "promote-fp"},
            }
        ],
    )
    import_id = imported[0]["id"]

    assert daily_cmd.run(target=tmp_path, json_output=True) == 1
    blocked = json.loads(capsys.readouterr().out)
    assert blocked["status"] == "blocked"
    assert blocked["approval_id"]
    assert "approval" in blocked["blockers"][0] or "local task ledger" in blocked["blockers"][0]

    assert cli.main(["daily", "run", "--target", str(tmp_path), "--approved", "--json"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "completed"
    assert receipt["selected_action"]["metadata"]["import_id"] == import_id
    assert not any("push" in command["command"] or "tag" in command["command"] for command in receipt["commands_invoked"])
    assert work_cmd._pending_tasks(tmp_path)


def test_daily_approvals_list_show_review_and_no_execution(tmp_path, capsys):
    _seed_ready_repo(tmp_path, capsys)
    imported, _, _ = work_cmd._append_import_records(
        tmp_path,
        [
            {
                "text": "Approval import",
                "kind": "task",
                "source": "scanner",
                "priority": "high",
                "acceptance": ["Approval is reviewed."],
                "metadata": {"source_fingerprint": "approval-fp"},
            }
        ],
    )

    assert daily_cmd.run(target=tmp_path, json_output=True) == 1
    blocked = json.loads(capsys.readouterr().out)
    approval_id = blocked["approval_id"]
    assert approval_id
    assert not work_cmd._pending_tasks(tmp_path)

    assert cli.main(["daily", "approvals", "list", "--target", str(tmp_path), "--json"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["approval_count"] == 1

    assert cli.main(["daily", "approvals", "show", approval_id, "--target", str(tmp_path), "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["selected_adapter"] == "brigade work import promote"
    assert shown["source_local_id"] == imported[0]["id"]

    assert cli.main(["daily", "approvals", "hold", approval_id, "--target", str(tmp_path), "--reason", "later", "--json"]) == 0
    held = json.loads(capsys.readouterr().out)
    assert held["status"] == "held"
    assert not work_cmd._pending_tasks(tmp_path)

    assert cli.main(["daily", "approvals", "reject", approval_id, "--target", str(tmp_path), "--reason", "no", "--json"]) == 0
    rejected = json.loads(capsys.readouterr().out)
    assert rejected["status"] == "rejected"
    assert rejected["review_reason"] == "no"
    assert not work_cmd._pending_tasks(tmp_path)

    assert cli.main(["daily", "approvals", "approve", approval_id, "--target", str(tmp_path), "--json"]) == 0
    approved = json.loads(capsys.readouterr().out)
    assert approved["status"] == "approved"
    assert not work_cmd._pending_tasks(tmp_path)


def test_daily_run_approval_lifecycle_and_idempotency(tmp_path, capsys):
    _seed_ready_repo(tmp_path, capsys)
    work_cmd._append_import_records(
        tmp_path,
        [
            {
                "text": "Approved import",
                "kind": "task",
                "source": "scanner",
                "priority": "high",
                "acceptance": ["Approved import is promoted."],
                "metadata": {"source_fingerprint": "approved-fp"},
            }
        ],
    )

    assert daily_cmd.run(target=tmp_path, json_output=True) == 1
    first = json.loads(capsys.readouterr().out)
    assert daily_cmd.run(target=tmp_path, json_output=True) == 1
    second = json.loads(capsys.readouterr().out)
    assert first["approval_id"] == second["approval_id"]
    approval_id = first["approval_id"]

    assert daily_cmd.run(target=tmp_path, approval_id=approval_id, json_output=True) == 1
    pending = json.loads(capsys.readouterr().out)
    assert "pending" in pending["blockers"][0]

    assert daily_cmd.approvals_approve(target=tmp_path, approval_id=approval_id, json_output=True) == 0
    capsys.readouterr()

    assert daily_cmd.run(target=tmp_path, approval_id=approval_id, json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["approval_id"] == approval_id
    assert receipt["status"] == "completed"
    consumed = daily_cmd._find_approval(tmp_path, approval_id)
    assert consumed["status"] == "consumed"
    assert consumed["consumed_run_id"] == receipt["run_id"]
    assert work_cmd._pending_tasks(tmp_path)

    assert daily_cmd.run(target=tmp_path, approval_id=approval_id, json_output=True) == 1
    reused = json.loads(capsys.readouterr().out)
    assert "consumed" in " ".join(reused["blockers"])


def test_daily_approval_refuses_stale_missing_and_changed_evidence(tmp_path, capsys):
    _seed_ready_repo(tmp_path, capsys)
    imported, _, _ = work_cmd._append_import_records(
        tmp_path,
        [
            {
                "text": "Changed import",
                "kind": "task",
                "source": "scanner",
                "priority": "high",
                "acceptance": ["Changed evidence is blocked."],
                "metadata": {"source_fingerprint": "old-fp"},
            }
        ],
    )
    assert daily_cmd.run(target=tmp_path, json_output=True) == 1
    blocked = json.loads(capsys.readouterr().out)
    approval_id = blocked["approval_id"]
    assert daily_cmd.approvals_approve(target=tmp_path, approval_id=approval_id, json_output=True) == 0
    capsys.readouterr()

    imports = work_cmd._read_imports(tmp_path)
    imports[0]["metadata"]["source_fingerprint"] = "new-fp"
    work_cmd._write_imports(tmp_path, imports)
    assert daily_cmd.run(target=tmp_path, approval_id=approval_id, json_output=True) == 1
    changed = json.loads(capsys.readouterr().out)
    assert any("fingerprint changed" in blocker for blocker in changed["blockers"])

    imports = work_cmd._read_imports(tmp_path)
    imports[0]["metadata"]["source_fingerprint"] = "old-fp"
    work_cmd._write_imports(tmp_path, imports)
    assert daily_cmd.approvals_hold(target=tmp_path, approval_id=approval_id, reason="wait", json_output=True) == 0
    capsys.readouterr()
    assert daily_cmd.run(target=tmp_path, approval_id=approval_id, json_output=True) == 1
    held = json.loads(capsys.readouterr().out)
    assert "held" in held["blockers"][0]

    assert daily_cmd.approvals_reject(target=tmp_path, approval_id=approval_id, reason="no", json_output=True) == 0
    capsys.readouterr()
    assert daily_cmd.run(target=tmp_path, approval_id=approval_id, json_output=True) == 1
    rejected = json.loads(capsys.readouterr().out)
    assert "rejected" in rejected["blockers"][0]

    assert daily_cmd.approvals_approve(target=tmp_path, approval_id=approval_id, json_output=True) == 0
    capsys.readouterr()
    work_cmd._write_imports(tmp_path, [])
    assert daily_cmd.run(target=tmp_path, approval_id=approval_id, json_output=True) == 1
    missing = json.loads(capsys.readouterr().out)
    assert any("not found" in blocker or "no longer available" in blocker for blocker in missing["blockers"])


def test_daily_approval_health_integration(tmp_path, capsys):
    _seed_ready_repo(tmp_path, capsys)
    work_cmd._append_import_records(
        tmp_path,
        [
            {
                "text": "Health approval",
                "kind": "task",
                "source": "scanner",
                "priority": "high",
                "acceptance": ["Approval health is surfaced."],
                "metadata": {"source_fingerprint": "health-fp"},
            }
        ],
    )
    assert daily_cmd.run(target=tmp_path, json_output=True) == 1
    blocked = json.loads(capsys.readouterr().out)
    approval_id = blocked["approval_id"]

    assert daily_cmd.status(target=tmp_path, json_output=True) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["top_pending_approval"]["approval_id"] == approval_id

    assert daily_cmd.review(target=tmp_path, json_output=True) == 0
    review = json.loads(capsys.readouterr().out)
    assert review["approval_request"]["approval_id"] == approval_id

    assert cli.main(["work", "brief", "--target", str(tmp_path), "--json"]) == 0
    brief = json.loads(capsys.readouterr().out)
    assert brief["daily_driver"]["approvals"]["top_pending"]["approval_id"] == approval_id

    assert cli.main(["center", "reviews", "--target", str(tmp_path), "--json"]) == 0
    reviews = json.loads(capsys.readouterr().out)
    assert any(item["subsystem"] == "daily-approval" for item in reviews["reviews"])

    approvals = daily_cmd.approvals_payload(tmp_path)["approvals"]
    approvals[0]["created_at"] = "2026-05-29T00:00:00+00:00"
    daily_cmd._write_approval(tmp_path, approvals[0])
    assert daily_cmd.doctor(target=tmp_path, json_output=True) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert any(check["name"] == "daily_stale_pending_approval" for check in doctor["checks"])


def test_daily_run_disabled_adapters_and_stale_recorded_plan(tmp_path, capsys):
    _seed_ready_repo(tmp_path, capsys)
    (tmp_path / ".brigade").mkdir(exist_ok=True)
    (tmp_path / ".brigade" / "daily.toml").write_text(
        "\n".join(
            [
                'preferred_mode = "task-first"',
                'max_risk_without_approval = "medium"',
                "allow_context_pack_build = true",
                "allow_operator_report_build = true",
                "allow_readiness_imports = true",
                "allow_import_promotion_with_approval = true",
                "allow_work_run = false",
                "stale_plan_threshold_hours = 1",
                "stale_run_threshold_hours = 1",
            ]
        )
        + "\n"
    )
    work_cmd._add_task(tmp_path, "Disabled task", acceptance=["Should not run."])
    assert daily_cmd.run(target=tmp_path, json_output=True) == 1
    blocked = json.loads(capsys.readouterr().out)
    assert "work run adapter disabled" in blocked["blockers"][0]

    stale_plan_dir = tmp_path / ".brigade" / "daily" / "plans" / "stale-plan"
    stale_plan_dir.mkdir(parents=True)
    stale_plan = daily_cmd.plan_payload(tmp_path)
    stale_plan["plan_id"] = "stale-plan"
    stale_plan["created_at"] = "2026-05-29T00:00:00+00:00"
    (stale_plan_dir / "plan.json").write_text(json.dumps(stale_plan))
    assert daily_cmd.run(target=tmp_path, plan_id="stale-plan", json_output=True) == 1
    stale = json.loads(capsys.readouterr().out)
    assert any("stale" in blocker for blocker in stale["blockers"])


def test_daily_doctor_stale_blocked_missing_evidence_and_parse_errors(tmp_path, capsys):
    _seed_ready_repo(tmp_path, capsys)
    assert daily_cmd.init(target=tmp_path) == 0
    capsys.readouterr()
    plan_dir = tmp_path / ".brigade" / "daily" / "plans" / "old-plan"
    plan_dir.mkdir(parents=True)
    (plan_dir / "plan.json").write_text(json.dumps({"plan_id": "old-plan", "created_at": "2026-05-29T00:00:00+00:00"}))
    run_dir = tmp_path / ".brigade" / "daily" / "runs" / "blocked-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "run_id": "blocked-run",
                "status": "blocked",
                "started_at": "2026-05-29T00:00:00+00:00",
                "completed_at": "2026-05-29T00:01:00+00:00",
                "selected_action": _daily_action("run-task", source_subsystem="work-task", source_local_id="missing", metadata={"task_id": "missing"}),
            }
        )
    )
    bad_dir = tmp_path / ".brigade" / "daily" / "runs" / "bad-run"
    bad_dir.mkdir(parents=True)
    (bad_dir / "run.json").write_text("{not json")

    assert daily_cmd.doctor(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    names = {check["name"] for check in payload["checks"]}
    assert {"daily_stale_plan", "daily_blocked_run", "daily_unclosed_run", "daily_missing_evidence", "daily_receipt_parse"} <= names


def test_daily_run_safe_adapters_and_json_cleanliness(tmp_path, monkeypatch, capsys):
    _seed_ready_repo(tmp_path, capsys)
    center_cmd._write_actions(
        tmp_path,
        [
            {
                "action_id": "action-safe",
                "status": "pending",
                "safe_summary": "Start this action",
                "source_fingerprint": "action-safe-fp",
                "created_at": "2026-05-30T00:00:00+00:00",
                "updated_at": "2026-05-30T00:00:00+00:00",
            }
        ],
    )
    calls = []

    def run_one(action):
        monkeypatch.setattr(daily_cmd, "_all_candidates", lambda target: [action])
        assert daily_cmd.run(target=tmp_path, approved=True, json_output=True) == 0
        payload = json.loads(capsys.readouterr().out)
        calls.append(payload["commands_invoked"][-1]["command"])
        return payload

    run_one(_daily_action("start-center-action", source_subsystem="center-action", source_local_id="action-safe", metadata={"action_id": "action-safe"}))

    monkeypatch.setattr(center_cmd, "readiness_import_issues", lambda **kwargs: print("noisy readiness") or 0)
    run_one(_daily_action("import-readiness-issues", source_subsystem="center-readiness", source_local_id="ready"))

    monkeypatch.setattr(center_cmd, "report_build", lambda **kwargs: print("noisy report") or 0)
    run_one(_daily_action("build-operator-report", source_subsystem="center-report", source_local_id="report"))

    assert calls == [
        "brigade center actions start action-safe",
        "brigade center readiness import-issues",
        "brigade center report build",
    ]


def test_daily_selection_skips_remote_mutation_commands():
    selected = daily_cmd._selected(
        [
            {
                "action_id": "remote",
                "score": 999,
                "suggested_next_command": "git push origin main",
            },
            {
                "action_id": "local",
                "score": 100,
                "suggested_next_command": "brigade work run",
            },
        ]
    )
    assert selected["action_id"] == "local"


def test_daily_run_handles_one_task_and_builds_context(tmp_path, monkeypatch, capsys):
    _seed_ready_repo(tmp_path, capsys)
    task, _ = work_cmd._add_task(
        tmp_path,
        "Run one task",
        acceptance=["Run is bounded."],
    )
    calls = []

    def fake_run(task_text, **kwargs):
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(work_cmd, "run", fake_run)

    assert daily_cmd.run(target=tmp_path, json_output=True) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    assert calls[0]["task_id"] == task["id"]
    assert receipt["context_pack_id"]
    assert receipt["task_id"] == task["id"]
    assert any(command["command"] == "brigade context build" for command in receipt["commands_invoked"])
    assert len([command for command in receipt["commands_invoked"] if command["command"] != "brigade context build"]) == 1


def test_daily_health_integration_with_work_and_center(tmp_path, capsys):
    _seed_ready_repo(tmp_path, capsys)
    assert daily_cmd.init(target=tmp_path) == 0
    capsys.readouterr()
    assert cli.main(["work", "brief", "--target", str(tmp_path), "--json"]) == 0
    brief = json.loads(capsys.readouterr().out)
    assert "daily_driver" in brief

    assert cli.main(["center", "status", "--target", str(tmp_path), "--json"]) == 0
    center_status = json.loads(capsys.readouterr().out)
    assert "daily_driver" in center_status

    run_dir = tmp_path / ".brigade" / "daily" / "runs" / "blocked-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"run_id": "blocked-run", "status": "blocked", "started_at": "2026-05-30T00:00:00+00:00"}))
    assert cli.main(["center", "reviews", "--target", str(tmp_path), "--json"]) == 0
    reviews = json.loads(capsys.readouterr().out)
    assert any(item["subsystem"] == "daily-driver" for item in reviews["reviews"])

    assert cli.main(["work", "doctor", "--target", str(tmp_path)]) in {0, 1}
    assert "daily_blocked_run" in capsys.readouterr().out


def test_daily_closeout_states_and_handoff(tmp_path, monkeypatch, capsys):
    _seed_ready_repo(tmp_path, capsys)
    work_cmd._add_task(tmp_path, "Run closeout task", acceptance=["Closeout exists."])

    def fake_run(task_text, **kwargs):
        return 0

    monkeypatch.setattr(work_cmd, "run", fake_run)
    assert daily_cmd.run(target=tmp_path, json_output=True) == 0
    capsys.readouterr()

    for status in ("reviewed", "deferred", "blocked", "archived"):
        assert daily_cmd.closeout(target=tmp_path, status=status, reason=f"{status} reason", json_output=True) == 0
        receipt = json.loads(capsys.readouterr().out)
        assert receipt["closeout_status"] == status

    assert cli.main(["daily", "closeout", "--target", str(tmp_path), "--handoff", "--json"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["handoff_path"]
    assert Path(receipt["handoff_path"]).is_file()
    assert handoff_cmd.lint_file(Path(receipt["handoff_path"])).valid
