from __future__ import annotations

import json

from brigade import cli, operator_cmd, work_cmd


def _fake_surface_run(argv, timeout=8):
    if argv == ["crontab", "-l"]:
        return {
            "ok": True,
            "stdout": "*/5 * * * * brigade-example-task --secret-path /example/private\n# example comment\n",
            "error": None,
        }
    if argv == ["openclaw", "--no-color", "cron", "status", "--json"]:
        return {"ok": True, "stdout": json.dumps({"enabled": True, "job_count": 2}), "error": None}
    if argv == ["openclaw", "--no-color", "cron", "list", "--json"]:
        return {
            "ok": True,
            "stdout": json.dumps(
                {
                    "jobs": [
                        {"name": "example-backup", "command": "backup --to /example/private", "status": "ok"},
                        {"name": "example-ingest", "command": "ingest --from /example/private", "status": "idle"},
                    ]
                }
            ),
            "error": None,
        }
    if argv == ["pm2", "jlist"]:
        return {
            "ok": True,
            "stdout": json.dumps(
                [
                    {"name": "example-service", "pm2_env": {"status": "online", "EXAMPLE_ENV": "abc"}},
                    {
                        "name": "example-api",
                        "pm2_env": {"status": "stopped", "pm_exec_path": "/example/private/api.js"},
                    },
                ]
            ),
            "error": None,
        }
    return {"ok": False, "stdout": "", "error": "command not found"}


def test_operator_plan_lists_safe_local_configs(tmp_path, capsys):
    assert operator_cmd.plan(target=tmp_path, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    ids = {row["id"] for row in payload["steps"]}
    assert {"daily", "handoff-sources", "work-scanners", "security", "tools"} <= ids
    assert "Does not start services." in payload["boundaries"]
    assert payload["profile"] == "local-operator"


def test_operator_internal_dogfood_plan_includes_dogfood(tmp_path, capsys):
    assert operator_cmd.plan(target=tmp_path, profile="internal-dogfood", json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    ids = {row["id"] for row in payload["steps"]}
    assert "dogfood" in ids
    assert payload["profile"] == "internal-dogfood"
    assert any("security evidence" in boundary for boundary in payload["boundaries"])


def test_operator_adoption_plan_summarizes_existing_workspace_without_raw_surface_details(
    tmp_path, capsys, monkeypatch
):
    (tmp_path / "AGENTS.md").write_text("Use the private operator workflow.\n")
    (tmp_path / "MEMORY.md").write_text("Private memory index.\n")
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "logs").mkdir()

    def fake_run(argv, timeout=8):
        if argv == ["crontab", "-l"]:
            return {
                "ok": True,
                "stdout": "*/5 * * * * brigade-example-task\n# example comment\n",
                "error": None,
            }
        if argv == ["openclaw", "--no-color", "cron", "status", "--json"]:
            return {"ok": True, "stdout": json.dumps({"enabled": True, "job_count": 9}), "error": None}
        if argv == ["openclaw", "--no-color", "cron", "list", "--json"]:
            return {
                "ok": True,
                "stdout": json.dumps(
                    {
                        "jobs": [
                            {"name": "example-backup", "status": "ok"},
                            {"name": "example-ingest", "status": "idle"},
                        ]
                    }
                ),
                "error": None,
            }
        if argv == ["pm2", "jlist"]:
            return {
                "ok": True,
                "stdout": json.dumps(
                    [
                        {"name": "example-service", "pm2_env": {"status": "online"}},
                        {"name": "example-api", "pm2_env": {"status": "stopped"}},
                    ]
                ),
                "error": None,
            }
        return {"ok": False, "stdout": "", "error": "command not found"}

    monkeypatch.setattr(operator_cmd, "_run_read_only_command", fake_run)

    assert operator_cmd.adoption_plan(target=tmp_path, json_output=True) == 0
    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert payload["status"] == "needs-adoption"
    assert payload["privacy"]["raw_crontab_lines_included"] is False
    assert payload["surfaces"]["shell_crontab"]["active_count"] == 1
    assert payload["surfaces"]["openclaw_cron"]["count"] == 2
    assert payload["surfaces"]["openclaw_cron"]["status_counts"] == {"idle": 1, "ok": 1}
    assert payload["surfaces"]["pm2"]["status_counts"] == {"online": 1, "stopped": 1}
    assert payload["workspace"]["harnesses"]["handoff_inbox_count"] == 1
    assert "brigade_operator_config_missing" in {issue["name"] for issue in payload["issues"]}
    assert "operator_surfaces_unmodeled" in {issue["name"] for issue in payload["issues"]}
    assert "example-service" not in payload_text
    assert "example-api" not in payload_text
    assert "brigade-example-task" not in payload_text


def test_operator_adoption_plan_text_output_omits_raw_surface_details(tmp_path, capsys, monkeypatch):
    def fake_run(argv, timeout=8):
        if argv == ["crontab", "-l"]:
            return {"ok": True, "stdout": "* * * * * brigade-example-task\n", "error": None}
        return {"ok": False, "stdout": "", "error": "command not found"}

    monkeypatch.setattr(operator_cmd, "_run_read_only_command", fake_run)

    assert operator_cmd.adoption_plan(target=tmp_path, json_output=False) == 0
    out = capsys.readouterr().out
    assert "operator adoption plan:" in out
    assert "shell_crontab_active: 1" in out
    assert "brigade-example-task" not in out
    assert "raw scheduler and process details are omitted" in out


def test_operator_adoption_plan_managed_workspace_without_external_surfaces(tmp_path, capsys, monkeypatch):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "config.json").write_text("{}\n")

    monkeypatch.setattr(
        operator_cmd,
        "_run_read_only_command",
        lambda argv, timeout=8: {"ok": False, "stdout": "", "error": "command not found"},
    )

    assert operator_cmd.adoption_plan(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "managed"
    assert payload["issue_count"] == 0
    assert payload["suggested_next_commands"] == ["brigade operator doctor --target . --profile local-operator"]


def test_operator_adoption_capture_writes_redacted_snapshot(tmp_path, capsys, monkeypatch):
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)

    def fake_run(argv, timeout=8):
        if argv == ["crontab", "-l"]:
            return {"ok": True, "stdout": "* * * * * brigade-example-task\n", "error": None}
        return {"ok": False, "stdout": "", "error": "command not found"}

    monkeypatch.setattr(operator_cmd, "_run_read_only_command", fake_run)

    assert operator_cmd.adoption_capture(target=tmp_path, json_output=True) == 0
    result = json.loads(capsys.readouterr().out)
    latest_path = tmp_path / ".brigade" / "operator" / "adoption" / "latest.json"
    assert result["capture_path"] == str(latest_path)
    assert latest_path.is_file()
    snapshot = json.loads(latest_path.read_text())
    assert snapshot["status"] == "needs-adoption"
    assert snapshot["surfaces"]["shell_crontab"]["active_count"] == 1
    assert snapshot["privacy"]["raw_crontab_lines_included"] is False
    assert "brigade-example-task" not in latest_path.read_text()

    assert operator_cmd.adoption_plan(target=tmp_path, json_output=True) == 0
    plan_after_capture = json.loads(capsys.readouterr().out)
    assert plan_after_capture["status"] == "needs-adoption"
    assert "brigade_operator_config_missing" in {issue["name"] for issue in plan_after_capture["issues"]}


def test_operator_adoption_import_issues_uses_work_inbox_and_dedupes(tmp_path, capsys, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("Use existing workflow.\n")
    (tmp_path / ".claude" / "memory-handoffs").mkdir(parents=True)

    def fake_run(argv, timeout=8):
        if argv == ["crontab", "-l"]:
            return {"ok": True, "stdout": "* * * * * brigade-example-task\n", "error": None}
        if argv == ["pm2", "jlist"]:
            return {
                "ok": True,
                "stdout": json.dumps([{"name": "example-service", "pm2_env": {"status": "online"}}]),
                "error": None,
            }
        return {"ok": False, "stdout": "", "error": "command not found"}

    monkeypatch.setattr(operator_cmd, "_run_read_only_command", fake_run)

    assert operator_cmd.adoption_capture(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    assert operator_cmd.adoption_import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "operator-adoption"
    assert payload["candidate_count"] >= 3
    assert payload["imported"] == payload["candidate_count"]
    imports = work_cmd._read_imports(tmp_path)
    assert len(imports) == payload["candidate_count"]
    assert {item["source"] for item in imports} == {"operator-adoption"}
    assert all((item.get("metadata") or {}).get("source_fingerprint") for item in imports)
    assert all("example-service" not in json.dumps(item) for item in imports)
    assert all("brigade-example-task" not in json.dumps(item) for item in imports)

    assert operator_cmd.adoption_import_issues(target=tmp_path, json_output=True) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["imported"] == 0
    assert second["skipped"] == payload["candidate_count"]


def test_operator_migration_status_rolls_up_adoption_surfaces_reviews_and_work(tmp_path, capsys, monkeypatch):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "config.json").write_text("{}\n")
    monkeypatch.setattr(operator_cmd, "_run_read_only_command", _fake_surface_run)

    assert operator_cmd.surfaces_capture(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    assert (
        operator_cmd.surfaces_review(
            target=tmp_path,
            surface="shell_crontab",
            status="needs-owner",
            all_records=True,
            reason="needs-owner-before-migration",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    assert operator_cmd.surfaces_import_issues(target=tmp_path, json_output=True) == 0
    capsys.readouterr()

    assert operator_cmd.migration_status(target=tmp_path, json_output=True) == 0
    payload_text = capsys.readouterr().out
    payload = json.loads(payload_text)
    assert payload["status"] == "in-progress"
    assert payload["ready"] is False
    assert payload["surfaces"]["record_count"] == 5
    assert payload["surfaces"]["reviewed_count"] == 1
    assert payload["surfaces"]["unreviewed_count"] == 4
    assert payload["work"]["pending_import_count"] == 4
    gap_names = {gap["name"] for gap in payload["gaps"]["items"]}
    assert {"surface_reviews_missing", "operator_migration_imports_pending", "surface_records_need_owner"} <= gap_names
    assert payload["gaps"]["blocking_count"] == 0
    assert payload["gaps"]["remaining_count"] >= 3
    assert "brigade-example-task" not in payload_text
    assert "example-service" not in payload_text
    assert "/example/private" not in payload_text

    assert operator_cmd.migration_doctor(target=tmp_path, json_output=True) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["status"] == "in-progress"
    assert doctor["blocking_issue_count"] == 0
    assert doctor["remaining_issue_count"] >= 3


def test_operator_migration_doctor_blocks_without_operator_config_or_capture(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(operator_cmd, "_run_read_only_command", _fake_surface_run)

    assert operator_cmd.migration_doctor(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["blocking_issue_count"] >= 2
    assert {issue["name"] for issue in payload["issues"]} >= {"operator_config_not_adopted", "surface_capture_missing"}


def test_operator_migration_import_issues_dedupes(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(operator_cmd, "_run_read_only_command", _fake_surface_run)

    assert operator_cmd.migration_import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "operator-migration"
    assert payload["candidate_count"] >= 2
    assert payload["imported"] == payload["candidate_count"]
    imports = work_cmd._read_imports(tmp_path)
    assert {item["source"] for item in imports} == {"operator-migration"}
    assert all((item.get("metadata") or {}).get("source_fingerprint") for item in imports)

    assert operator_cmd.migration_import_issues(target=tmp_path, json_output=True) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["imported"] == 0
    assert second["skipped"] == payload["candidate_count"]


def test_operator_migration_status_counts_promoted_rollup_tasks(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(operator_cmd, "_run_read_only_command", _fake_surface_run)

    assert operator_cmd.migration_import_issues(target=tmp_path, json_output=True) == 0
    imported = json.loads(capsys.readouterr().out)
    rollup = next(item for item in imported["imports"] if item["source"] == "operator-migration")

    assert work_cmd.import_promote(target=tmp_path, import_id=rollup["id"]) == 0
    capsys.readouterr()

    assert operator_cmd.migration_status(target=tmp_path, json_output=True) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["work"]["pending_task_count"] == 1
    assert status["work"]["pending_tasks_by_source"] == {"import:operator-migration": 1}


def test_operator_migration_import_issues_supersedes_changed_rollups(tmp_path, capsys, monkeypatch):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "config.json").write_text("{}\n")
    monkeypatch.setattr(operator_cmd, "_run_read_only_command", _fake_surface_run)
    assert operator_cmd.surfaces_capture(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    assert (
        operator_cmd.surfaces_review(
            target=tmp_path,
            surface="shell_crontab",
            status="needs-owner",
            all_records=True,
            reason="needs-owner-before-migration",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    assert operator_cmd.migration_import_issues(target=tmp_path, json_output=True) == 0
    first = json.loads(capsys.readouterr().out)
    first_ids = [item["id"] for item in first["imports"]]
    assert first_ids

    assert (
        operator_cmd.surfaces_review(
            target=tmp_path,
            surface="pm2",
            status="needs-owner",
            all_records=True,
            reason="needs-owner-before-migration",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    assert operator_cmd.migration_import_issues(target=tmp_path, json_output=True) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["superseded"] >= 1
    imports = work_cmd._read_imports(tmp_path)
    superseded = [item for item in imports if item.get("id") in second["superseded_import_ids"]]
    assert superseded
    assert all(item["status"] == "dismissed" for item in superseded)
    assert all(item["dismiss_reason"] == "superseded-by-current-migration-rollup" for item in superseded)


def test_operator_migration_import_issues_supersedes_stale_source_imports(tmp_path, capsys, monkeypatch):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "config.json").write_text("{}\n")
    monkeypatch.setattr(operator_cmd, "_run_read_only_command", _fake_surface_run)

    assert operator_cmd.surfaces_capture(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    assert (
        operator_cmd.surfaces_review(
            target=tmp_path,
            surface="shell_crontab",
            status="needs-owner",
            all_records=True,
            reason="needs-owner-before-migration",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()

    imports = [
        work_cmd._make_import(
            "Route old adoption gap.",
            kind="task",
            source="operator-adoption",
            metadata={"source_item_key": "operator-adoption:stale_gap"},
        ),
        work_cmd._make_import(
            "Route current adoption gap.",
            kind="task",
            source="operator-adoption",
            metadata={"source_item_key": "operator-adoption:external_surfaces_present"},
        ),
        work_cmd._make_import(
            "Review shell crontab surface.",
            kind="task",
            source="operator-surface",
            metadata={"surface": "shell_crontab", "source_item_key": "operator-surface:shell_crontab"},
        ),
        work_cmd._make_import(
            "Review pm2 surface.",
            kind="task",
            source="operator-surface",
            metadata={"surface": "pm2", "source_item_key": "operator-surface:pm2"},
        ),
    ]
    work_cmd._write_imports(tmp_path, imports)

    assert operator_cmd.migration_import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["superseded_source_imports"] == 2

    by_text = {item["text"]: item for item in work_cmd._read_imports(tmp_path)}
    stale_adoption = by_text["Route old adoption gap."]
    reviewed_surface = by_text["Review shell crontab surface."]
    current_adoption = by_text["Route current adoption gap."]
    unreviewed_surface = by_text["Review pm2 surface."]

    assert stale_adoption["status"] == "dismissed"
    assert stale_adoption["dismiss_reason"] == "superseded-by-current-migration-status"
    assert (stale_adoption.get("metadata") or {}).get("superseded_by_source") == "operator-migration"
    assert reviewed_surface["status"] == "dismissed"
    assert reviewed_surface["dismiss_reason"] == "superseded-by-reviewed-surface-rollup"
    assert (reviewed_surface.get("metadata") or {}).get("superseded_by_source") == "operator-migration"
    assert current_adoption["status"] == "pending"
    assert unreviewed_surface["status"] == "pending"


def test_operator_migration_consolidate_dismisses_tiny_surface_review_imports(tmp_path, capsys, monkeypatch):
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "config.json").write_text("{}\n")
    monkeypatch.setattr(operator_cmd, "_run_read_only_command", _fake_surface_run)
    assert operator_cmd.surfaces_capture(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    assert (
        operator_cmd.surfaces_review(
            target=tmp_path,
            surface="shell_crontab",
            status="needs-owner",
            all_records=True,
            reason="needs-owner-before-migration",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()
    assert operator_cmd.surfaces_import_issues(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    assert operator_cmd.migration_import_issues(target=tmp_path, json_output=True) == 0
    capsys.readouterr()

    assert (
        operator_cmd.migration_consolidate(
            target=tmp_path, surface="shell_crontab", review_status="needs-owner", dry_run=True, json_output=True
        )
        == 0
    )
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["candidate_count"] == 1
    assert dry_run["dismissed"] == 0

    assert (
        operator_cmd.migration_consolidate(
            target=tmp_path, surface="shell_crontab", review_status="needs-owner", json_output=True
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_count"] == 1
    assert payload["dismissed"] == 1
    imports = work_cmd._read_imports(tmp_path)
    dismissed = [
        item
        for item in imports
        if item.get("source") == "operator-surface-review" and item.get("status") == "dismissed"
    ]
    assert len(dismissed) == 1
    assert dismissed[0]["dismiss_reason"] == "superseded-by-migration-rollup"
    assert (dismissed[0].get("metadata") or {}).get("superseded_by_source") == "operator-migration"


def test_operator_migration_consolidate_requires_rollup_import(tmp_path, capsys):
    assert operator_cmd.migration_consolidate(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["blocked"] is True
    assert payload["rollup_import_count"] == 0


def test_operator_surfaces_capture_lists_and_doctors_redacted_records(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(operator_cmd, "_run_read_only_command", _fake_surface_run)

    assert operator_cmd.surfaces_capture(target=tmp_path, json_output=True) == 0
    result = json.loads(capsys.readouterr().out)
    latest_path = tmp_path / ".brigade" / "operator" / "surfaces" / "latest.json"
    assert result["capture_path"] == str(latest_path)
    assert result["record_count"] == 5
    assert result["surface_count"] == 5
    assert result["privacy"]["raw_crontab_lines_included"] is False
    assert latest_path.is_file()
    snapshot_text = latest_path.read_text()
    snapshot = json.loads(snapshot_text)
    assert snapshot["surfaces"]["shell_crontab"]["active_count"] == 1
    assert snapshot["surfaces"]["openclaw_cron"]["status_counts"] == {"idle": 1, "ok": 1}
    assert snapshot["surfaces"]["pm2"]["status_counts"] == {"online": 1, "stopped": 1}
    assert [record["record_label"] for record in snapshot["records"]] == [
        "shell-crontab-001",
        "openclaw-cron-001",
        "openclaw-cron-002",
        "pm2-001",
        "pm2-002",
    ]
    assert all(record["raw_included"] is False for record in snapshot["records"])
    assert all(record["command_included"] is False for record in snapshot["records"])
    assert all(record["env_included"] is False for record in snapshot["records"])
    assert "example-service" not in snapshot_text
    assert "example-api" not in snapshot_text
    assert "example-backup" not in snapshot_text
    assert "brigade-example-task" not in snapshot_text
    assert "/example/private" not in snapshot_text

    assert operator_cmd.surfaces_list(target=tmp_path, json_output=True) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["status"] == "captured"
    assert listed["record_count"] == 5
    assert listed["records"][0]["record_label"] == "shell-crontab-001"

    assert operator_cmd.surfaces_doctor(target=tmp_path, json_output=True) == 1
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["ready"] is False
    assert any(issue["name"] == "surface_reviews_missing" for issue in doctor["issues"])
    assert "example-service" not in json.dumps(doctor)

    assert (
        operator_cmd.surfaces_review(
            target=tmp_path,
            surface="shell_crontab",
            status="external-ok",
            all_records=True,
            reason="reviewed-external-ownership",
            json_output=True,
        )
        == 0
    )
    review = json.loads(capsys.readouterr().out)
    assert review["reviewed_count"] == 1
    assert (tmp_path / ".brigade" / "operator" / "surfaces" / "reviews").is_dir()
    assert operator_cmd.surfaces_doctor(target=tmp_path, surface="shell_crontab", json_output=True) == 0
    shell_doctor = json.loads(capsys.readouterr().out)
    assert shell_doctor["ready"] is True
    assert shell_doctor["surface_filter"] == "shell_crontab"
    assert shell_doctor["review_summary"]["reviewed_count"] == 1
    assert shell_doctor["next_command"] == "brigade operator surfaces import-issues --target . --json"

    assert operator_cmd.surfaces_reviews(target=tmp_path, surface="shell_crontab", json_output=True) == 0
    reviews = json.loads(capsys.readouterr().out)
    assert reviews["reviewed_count"] == 1
    assert reviews["unreviewed_count"] == 0
    assert reviews["surfaces"][0]["status_counts"] == {"external-ok": 1}


def test_operator_surfaces_doctor_warns_when_capture_missing(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(operator_cmd, "_run_read_only_command", _fake_surface_run)

    assert operator_cmd.surfaces_doctor(target=tmp_path, json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["issues"][0]["name"] == "surfaces_capture_missing"
    assert payload["next_command"] == "brigade operator surfaces capture --target . --json"


def test_operator_surfaces_import_issues_uses_work_inbox_and_dedupes(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(operator_cmd, "_run_read_only_command", _fake_surface_run)

    assert operator_cmd.surfaces_capture(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    assert operator_cmd.surfaces_import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["source"] == "operator-surface"
    assert payload["candidate_count"] == 3
    assert payload["imported"] == 3
    imports = work_cmd._read_imports(tmp_path)
    assert len(imports) == 3
    assert {item["source"] for item in imports} == {"operator-surface"}
    assert {(item.get("metadata") or {}).get("surface") for item in imports} == {
        "shell_crontab",
        "openclaw_cron",
        "pm2",
    }
    assert all((item.get("metadata") or {}).get("source_fingerprint") for item in imports)
    rendered = json.dumps(imports)
    assert "example-service" not in rendered
    assert "example-api" not in rendered
    assert "example-backup" not in rendered
    assert "brigade-example-task" not in rendered
    assert "/example/private" not in rendered

    assert operator_cmd.surfaces_import_issues(target=tmp_path, json_output=True) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["imported"] == 0
    assert second["skipped"] == 3


def test_operator_surfaces_review_rejects_secret_like_reason(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(operator_cmd, "_run_read_only_command", _fake_surface_run)
    assert operator_cmd.surfaces_capture(target=tmp_path, json_output=True) == 0
    capsys.readouterr()

    assert (
        operator_cmd.surfaces_review(
            target=tmp_path,
            surface="shell_crontab",
            status="external-ok",
            all_records=True,
            # content-guard: allow api-key-assignment
            reason="token=REDACTED_EXAMPLE_VALUE",
            json_output=True,
        )
        == 2
    )
    assert "secret-looking" in capsys.readouterr().err


def test_operator_surfaces_review_imports_actionable_records(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(operator_cmd, "_run_read_only_command", _fake_surface_run)
    assert operator_cmd.surfaces_capture(target=tmp_path, json_output=True) == 0
    capsys.readouterr()
    assert (
        operator_cmd.surfaces_review(
            target=tmp_path,
            surface="shell_crontab",
            status="brigade-runbook-candidate",
            all_records=True,
            reason="candidate-for-runbook",
            json_output=True,
        )
        == 0
    )
    capsys.readouterr()

    assert operator_cmd.surfaces_import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidate_count"] == 4
    imports = work_cmd._read_imports(tmp_path)
    assert any(item["source"] == "operator-surface-review" for item in imports)
    review_import = next(item for item in imports if item["source"] == "operator-surface-review")
    assert (review_import.get("metadata") or {}).get("record_label") == "shell-crontab-001"
    assert (review_import.get("metadata") or {}).get("review_status") == "brigade-runbook-candidate"
    assert "brigade-example-task" not in json.dumps(imports)


def test_operator_surfaces_cli_dispatch(tmp_path, monkeypatch):
    seen = {}

    def fake_capture(**kwargs):
        seen["capture"] = kwargs
        return 0

    def fake_list(**kwargs):
        seen["list"] = kwargs
        return 0

    def fake_doctor(**kwargs):
        seen["doctor"] = kwargs
        return 0

    def fake_review(**kwargs):
        seen["review"] = kwargs
        return 0

    def fake_reviews(**kwargs):
        seen["reviews"] = kwargs
        return 0

    def fake_import_issues(**kwargs):
        seen["import"] = kwargs
        return 0

    monkeypatch.setattr(operator_cmd, "surfaces_capture", fake_capture)
    monkeypatch.setattr(operator_cmd, "surfaces_list", fake_list)
    monkeypatch.setattr(operator_cmd, "surfaces_doctor", fake_doctor)
    monkeypatch.setattr(operator_cmd, "surfaces_review", fake_review)
    monkeypatch.setattr(operator_cmd, "surfaces_reviews", fake_reviews)
    monkeypatch.setattr(operator_cmd, "surfaces_import_issues", fake_import_issues)
    assert cli.main(["operator", "surfaces", "capture", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["operator", "surfaces", "list", "--target", str(tmp_path), "--json"]) == 0
    assert (
        cli.main(["operator", "surfaces", "doctor", "--target", str(tmp_path), "--surface", "shell_crontab", "--json"])
        == 0
    )
    assert (
        cli.main(
            [
                "operator",
                "surfaces",
                "review",
                "--target",
                str(tmp_path),
                "--surface",
                "shell_crontab",
                "--status",
                "external-ok",
                "--all",
                "--reason",
                "reviewed",
                "--json",
            ]
        )
        == 0
    )
    assert (
        cli.main(["operator", "surfaces", "reviews", "--target", str(tmp_path), "--surface", "shell_crontab", "--json"])
        == 0
    )
    assert cli.main(["operator", "surfaces", "import-issues", "--target", str(tmp_path), "--dry-run", "--json"]) == 0
    assert seen["capture"] == {"target": tmp_path, "json_output": True}
    assert seen["list"] == {"target": tmp_path, "json_output": True}
    assert seen["doctor"] == {"target": tmp_path, "surface": "shell_crontab", "json_output": True}
    assert seen["review"] == {
        "target": tmp_path,
        "surface": "shell_crontab",
        "status": "external-ok",
        "all_records": True,
        "record_labels": [],
        "reason": "reviewed",
        "json_output": True,
    }
    assert seen["reviews"] == {"target": tmp_path, "surface": "shell_crontab", "json_output": True}
    assert seen["import"] == {"target": tmp_path, "dry_run": True, "json_output": True}


def test_operator_adopt_cli_dispatch(tmp_path, monkeypatch):
    seen = {}

    def fake_adoption_plan(**kwargs):
        seen["plan"] = kwargs
        return 0

    def fake_adoption_capture(**kwargs):
        seen["capture"] = kwargs
        return 0

    def fake_adoption_import_issues(**kwargs):
        seen["import"] = kwargs
        return 0

    monkeypatch.setattr(operator_cmd, "adoption_plan", fake_adoption_plan)
    monkeypatch.setattr(operator_cmd, "adoption_capture", fake_adoption_capture)
    monkeypatch.setattr(operator_cmd, "adoption_import_issues", fake_adoption_import_issues)
    assert cli.main(["operator", "adopt", "plan", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["operator", "adopt", "capture", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["operator", "adopt", "import-issues", "--target", str(tmp_path), "--dry-run", "--json"]) == 0
    assert seen["plan"] == {"target": tmp_path, "json_output": True}
    assert seen["capture"] == {"target": tmp_path, "json_output": True}
    assert seen["import"] == {"target": tmp_path, "dry_run": True, "json_output": True}


def test_operator_migration_cli_dispatch(tmp_path, monkeypatch):
    seen = {}

    def fake_status(**kwargs):
        seen["status"] = kwargs
        return 0

    def fake_doctor(**kwargs):
        seen["doctor"] = kwargs
        return 0

    def fake_import_issues(**kwargs):
        seen["import"] = kwargs
        return 0

    def fake_consolidate(**kwargs):
        seen["consolidate"] = kwargs
        return 0

    monkeypatch.setattr(operator_cmd, "migration_status", fake_status)
    monkeypatch.setattr(operator_cmd, "migration_doctor", fake_doctor)
    monkeypatch.setattr(operator_cmd, "migration_import_issues", fake_import_issues)
    monkeypatch.setattr(operator_cmd, "migration_consolidate", fake_consolidate)
    assert cli.main(["operator", "migration", "status", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["operator", "migration", "doctor", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["operator", "migration", "import-issues", "--target", str(tmp_path), "--dry-run", "--json"]) == 0
    assert (
        cli.main(
            [
                "operator",
                "migration",
                "consolidate",
                "--target",
                str(tmp_path),
                "--surface",
                "shell_crontab",
                "--review-status",
                "needs-owner",
                "--reason",
                "superseded",
                "--dry-run",
                "--json",
            ]
        )
        == 0
    )
    assert seen["status"] == {"target": tmp_path, "json_output": True}
    assert seen["doctor"] == {"target": tmp_path, "json_output": True}
    assert seen["import"] == {"target": tmp_path, "dry_run": True, "json_output": True}
    assert seen["consolidate"] == {
        "target": tmp_path,
        "surface": "shell_crontab",
        "review_status": "needs-owner",
        "reason": "superseded",
        "dry_run": True,
        "json_output": True,
    }


def test_operator_guide_json_and_cli_dispatch(capsys):
    assert operator_cmd.guide(profile="internal-dogfood", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"] == "internal-dogfood"
    assert "brigade operator status --profile internal-dogfood --target ." in payload["startup_commands"]
    assert "brigade daily plan --target ." in payload["startup_commands"]
    assert payload["tool_sync_command"] == "brigade operator sync-tools --target ."
    assert any("does not publish" in item.lower() for item in payload["boundaries"])

    assert cli.main(["operator", "guide", "--profile", "local-operator"]) == 0
    out = capsys.readouterr().out
    assert "operator guide" in out
    assert "profile: local-operator" in out


def test_operator_init_dry_run_does_not_write(tmp_path, capsys):
    assert operator_cmd.init(target=tmp_path, dry_run=True, json_output=True) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert not (tmp_path / ".brigade" / "daily.toml").exists()


def test_operator_init_merges_existing_handoff_source_coverage(tmp_path, capsys):
    path = tmp_path / ".brigade" / "handoff-sources.json"
    path.parent.mkdir()
    path.write_text(
        json.dumps(
            {
                "custom": {"keep": True},
                "sources": [
                    {"root": ".", "inboxes": [".codex/memory-handoffs"]},
                    {"root": "../shared", "inboxes": ["team/handoffs"]},
                ],
            }
        )
        + "\n"
    )

    assert (
        operator_cmd.init(
            target=tmp_path,
            handoff_inboxes=[".grok/memory-handoffs"],
            json_output=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    sources_step = next(row for row in payload["results"] if row["id"] == "handoff-sources")
    sources = json.loads(path.read_text())

    assert sources_step["status"] == "written"
    assert sources["custom"] == {"keep": True}
    assert sources["sources"][0]["inboxes"] == [".codex/memory-handoffs", ".grok/memory-handoffs"]
    assert sources["sources"][1] == {"root": "../shared", "inboxes": ["team/handoffs"]}


def test_operator_internal_dogfood_init_and_status(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(
        "brigade.scrub.hook_status",
        lambda target: {
            "available": True,
            "scanner_dir": "/tools/content-guard",
            "policy": "public-repo",
            "pre_push_hook_enabled": True,
        },
    )

    assert (
        operator_cmd.init(target=tmp_path, profile="internal-dogfood", waive_public_release=True, json_output=True) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["profile"] == "internal-dogfood"
    assert (tmp_path / ".brigade" / "dogfood.toml").is_file()
    assert (tmp_path / ".brigade" / "security" / "latest" / "security-report.json").is_file()
    assert any(row["id"] == "security-scan" for row in payload["post_actions"])
    assert any(row["id"] == "public-release-readiness-waiver" for row in payload["post_actions"])

    assert operator_cmd.status(target=tmp_path, profile="internal-dogfood", json_output=True) in {0, 1}
    status = json.loads(capsys.readouterr().out)
    assert status["profile"] == "internal-dogfood"
    assert status["dogfood"]["ready"] is True
    assert status["brigade"]["version"]
    assert status["repo"]["missing_config_count"] == 0
    assert status["security"]["issue_count"] == 0
    assert status["content_guard"]["available"] is True
    assert status["machine"]["content_guard_installed"] is True


def test_operator_status_content_guard_missing_unconfigured_is_nonblocking(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "dogfood.toml").write_text("[dogfood]\n")
    monkeypatch.setattr(
        "brigade.daily_cmd.health",
        lambda target: {"issue_count": 0, "top_issue": None, "latest_plan": None, "latest_run": None},
    )
    monkeypatch.setattr(
        "brigade.security_cmd.health", lambda target: {"issue_count": 0, "top_issue": None, "evidence": None}
    )
    monkeypatch.setattr(
        "brigade.center_cmd._readiness_payload",
        lambda target: {"status": "ready", "blocker_count": 0, "warning_count": 0, "waived_count": 0, "blockers": []},
    )
    monkeypatch.setattr(
        "brigade.notifications_cmd.health",
        lambda target: {"installed": False, "configured": False, "config_path": None},
    )
    monkeypatch.setattr(
        "brigade.scrub.hook_status",
        lambda target: {
            "available": False,
            "scanner_dir": "/missing/content-guard",
            "policy": "public-repo",
            "pre_push_hook_enabled": False,
            "pre_push_hook_exists": False,
            "configured_pre_push_hook_exists": False,
            "git_pre_push_hook_exists": False,
            "hooks_path": None,
            "checks": [
                {"status": "warn", "name": "content_guard_missing", "detail": "content-guard not found"},
                {
                    "status": "warn",
                    "name": "content_guard_hook_not_enabled",
                    "detail": "no executable pre-push hook found",
                },
            ],
            "suggested_commands": ["clone content-guard"],
        },
    )

    assert operator_cmd.status(target=tmp_path, profile="internal-dogfood", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["issue_count"] == 0
    assert payload["content_guard"]["available"] is False
    assert payload["content_guard"]["checks"][0]["name"] == "content_guard_missing"


def test_operator_status_generated_content_guard_hook_unconfigured_is_nonblocking(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "dogfood.toml").write_text("[dogfood]\n")
    monkeypatch.setattr(
        "brigade.daily_cmd.health",
        lambda target: {"issue_count": 0, "top_issue": None, "latest_plan": None, "latest_run": None},
    )
    monkeypatch.setattr(
        "brigade.security_cmd.health", lambda target: {"issue_count": 0, "top_issue": None, "evidence": None}
    )
    monkeypatch.setattr(
        "brigade.center_cmd._readiness_payload",
        lambda target: {"status": "ready", "blocker_count": 0, "warning_count": 0, "waived_count": 0, "blockers": []},
    )
    monkeypatch.setattr(
        "brigade.notifications_cmd.health",
        lambda target: {"installed": False, "configured": False, "config_path": None},
    )
    monkeypatch.setattr(
        "brigade.scrub.hook_status",
        lambda target: {
            "available": False,
            "scanner_dir": "/missing/content-guard",
            "policy": "public-repo",
            "pre_push_hook_enabled": False,
            "pre_push_hook_exists": True,
            "configured_pre_push_hook_exists": False,
            "git_pre_push_hook_exists": False,
            "hooks_path": None,
            "checks": [
                {"status": "warn", "name": "content_guard_missing", "detail": "content-guard not found"},
                {
                    "status": "warn",
                    "name": "content_guard_hook_not_enabled",
                    "detail": "no executable pre-push hook found",
                },
            ],
            "suggested_commands": ["clone content-guard", "git config core.hooksPath hooks"],
        },
    )
    monkeypatch.setattr(
        "brigade.daily_cmd.status_payload",
        lambda target: {"daily_health": {"issue_count": 0}, "next_recommended_command": "brigade daily plan"},
    )
    monkeypatch.setattr(
        "brigade.tools_cmd.health", lambda target: {"issue_count": 0, "tool_count": 4, "top_issue": None}
    )

    assert operator_cmd.doctor(target=tmp_path, profile="local-operator", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["blocking_issue_count"] == 0
    assert payload["content_guard"]["pre_push_hook_exists"] is True


def test_operator_status_cli_dispatch(tmp_path, monkeypatch):
    seen = {}

    def fake_status(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(operator_cmd, "status", fake_status)
    assert cli.main(["operator", "status", "--target", str(tmp_path), "--profile", "internal-dogfood", "--json"]) == 0
    assert seen == {"target": tmp_path, "profile": "internal-dogfood", "json_output": True}


def test_operator_doctor_json_and_cli_dispatch(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        operator_cmd,
        "status_payload",
        lambda target, profile="internal-dogfood": {
            "target": str(tmp_path),
            "profile": profile,
            "issue_count": 0,
            "checks": [],
            "dogfood": {"ready": True},
            "repo": {"missing_config_count": 0, "not_gitignored_count": 0},
            "security": {"issue_count": 0},
            "daily": {"issue_count": 0},
            "content_guard": {"available": True, "pre_push_hook_enabled": True, "policy": "public-repo"},
        },
    )
    monkeypatch.setattr(
        "brigade.daily_cmd.status_payload",
        lambda target: {
            "daily_health": {"issue_count": 0},
            "selected_action": {"action_type": "build-operator-report"},
            "next_recommended_command": "brigade center report build",
        },
    )
    monkeypatch.setattr(
        "brigade.tools_cmd.health",
        lambda target: {"issue_count": 0, "tool_count": 2, "top_issue": None},
    )

    assert operator_cmd.doctor(target=tmp_path, profile="internal-dogfood", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["blocking_issue_count"] == 0
    assert payload["next_command"] == "brigade center report build"
    assert payload["content_guard"]["available"] is True
    assert any("tools/" in item for item in payload["tracked_vs_generated"])

    assert cli.main(["operator", "doctor", "--target", str(tmp_path), "--profile", "internal-dogfood"]) == 0
    out = capsys.readouterr().out
    assert "operator doctor:" in out
    assert "ready: yes" in out


def test_operator_doctor_blocks_on_tool_projection_health(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        operator_cmd,
        "status_payload",
        lambda target, profile="internal-dogfood": {
            "target": str(tmp_path),
            "profile": profile,
            "issue_count": 0,
            "checks": [],
            "dogfood": {"ready": True},
            "repo": {"missing_config_count": 0, "not_gitignored_count": 0},
            "security": {"issue_count": 0},
            "daily": {"issue_count": 0},
        },
    )
    monkeypatch.setattr(
        "brigade.daily_cmd.status_payload",
        lambda target: {"daily_health": {"issue_count": 0}, "next_recommended_command": "brigade daily plan"},
    )
    monkeypatch.setattr(
        "brigade.tools_cmd.health",
        lambda target: {
            "issue_count": 1,
            "tool_count": 2,
            "top_issue": {"detail": "claude: projection will be created"},
        },
    )

    assert operator_cmd.doctor(target=tmp_path, profile="internal-dogfood", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["blocking_issue_count"] == 1
    assert payload["next_command"] == "brigade operator sync-tools --target ."
    assert payload["blockers"][0]["name"] == "tool_projection_health"


def test_operator_verify_harness_hermes_after_handoff_init_and_draft(tmp_path, capsys):
    assert cli.main(["init", "--target", str(tmp_path), "--depth", "workspace", "--harnesses", "hermes"]) == 0
    capsys.readouterr()
    assert cli.main(["handoff", "sources", "init", "--target", str(tmp_path), "--json"]) == 0
    capsys.readouterr()
    assert (
        cli.main(
            [
                "handoff",
                "draft",
                "--target",
                str(tmp_path),
                "--inbox",
                "hermes",
                "--title",
                "Hermes verification",
                "--summary",
                "Hermes has a local handoff draft.",
                "--content",
                "### Hermes verification\n\nUse the Hermes inbox for local handoffs.",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert operator_cmd.verify_harness(target=tmp_path, harness="hermes", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is True
    assert payload["handoff_inbox"]["relative_path"] == ".hermes/memory-handoffs"
    assert payload["handoff_inbox"]["watched"] is True
    assert any(
        row["name"] == "hermes_adapter_workspace_handoff_inbox" and row["status"] == "ok" for row in payload["checks"]
    )
    assert any(row["name"] == "handoff_lint" and row["status"] == "ok" for row in payload["checks"])


def test_operator_verify_harness_recommends_additive_source_init(tmp_path, capsys):
    assert cli.main(["init", "--target", str(tmp_path), "--depth", "repo", "--harnesses", "grok"]) == 0
    capsys.readouterr()

    assert operator_cmd.verify_harness(target=tmp_path, harness="grok", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)

    assert payload["handoff_inbox"]["watched"] is False
    assert payload["next_command"] == "brigade handoff sources init --target ."


def test_operator_verify_harness_hermes_fails_broken_adapter_inbox(tmp_path, capsys):
    assert cli.main(["init", "--target", str(tmp_path), "--depth", "workspace", "--harnesses", "hermes"]) == 0
    capsys.readouterr()
    assert cli.main(["handoff", "sources", "init", "--target", str(tmp_path), "--json"]) == 0
    capsys.readouterr()
    workspace_path = tmp_path / ".brigade" / "hermes" / "workspace.harness.json"
    workspace = json.loads(workspace_path.read_text())
    workspace["workspace"]["handoff_inbox"] = ".claude/memory-handoffs"
    workspace_path.write_text(json.dumps(workspace))

    assert operator_cmd.verify_harness(target=tmp_path, harness="hermes", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ready"] is False
    assert payload["next_command"] == "brigade hermes-fragments --out .brigade/hermes"
    assert any(
        row["name"] == "hermes_adapter_workspace_handoff_inbox"
        and row["status"] == "fail"
        and ".claude/memory-handoffs" in row["detail"]
        for row in payload["checks"]
    )


def test_operator_verify_harness_cli_dispatch(tmp_path, monkeypatch):
    seen = {}

    def fake_verify_harness(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(operator_cmd, "verify_harness", fake_verify_harness)
    assert cli.main(["operator", "verify-harness", "--target", str(tmp_path), "--harness", "hermes", "--json"]) == 0
    assert seen == {"target": tmp_path, "harness": "hermes", "json_output": True}


def test_operator_sync_tools_cli_dispatch(tmp_path, monkeypatch):
    seen = {}

    def fake_sync_tools(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(operator_cmd, "sync_tools", fake_sync_tools)
    assert cli.main(["operator", "sync-tools", "--target", str(tmp_path), "--dry-run", "--force", "--json"]) == 0
    assert seen == {"target": tmp_path, "dry_run": True, "force": True, "json_output": True}


def test_operator_bootstrap_portable_cli_dispatch(tmp_path, monkeypatch):
    seen = {}

    def fake_bootstrap_portable(**kwargs):
        seen.update(kwargs)
        return 0

    monkeypatch.setattr(operator_cmd, "bootstrap_portable", fake_bootstrap_portable)
    assert (
        cli.main(
            [
                "operator",
                "bootstrap-portable",
                "--target",
                str(tmp_path),
                "--tool-pack",
                str(tmp_path / "tool-pack"),
                "--skill-pack",
                str(tmp_path / "skill-pack"),
                "--dry-run",
                "--force",
                "--json",
            ]
        )
        == 0
    )
    assert seen == {
        "target": tmp_path,
        "tool_pack": tmp_path / "tool-pack",
        "skill_pack": tmp_path / "skill-pack",
        "dry_run": True,
        "force": True,
        "json_output": True,
    }


def test_operator_bootstrap_portable_syncs_builtins(tmp_path, capsys):
    assert cli.main(["operator", "bootstrap-portable", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert [step["id"] for step in payload["steps"]] == ["operator-sync-tools", "tools-doctor", "skills-doctor"]
    assert payload["steps"][0]["payload"]["status"] == "ok"
    assert (tmp_path / ".claude" / "commands" / "simplify.md").is_file()
    assert (tmp_path / ".codex" / "skills" / "frontend" / "SKILL.md").is_file()
    assert (tmp_path / "tools" / "antislop.md").is_file()


def test_operator_quickstart_minimal_default_footprint(tmp_path, capsys):
    # Repo depth defaults to the minimal footprint (audit 2026-07-02, item 6):
    # no rules/, hooks/, tools/, scripts/, or INSTALL_FOR_AGENTS.md unless --full.
    assert cli.main(["operator", "quickstart", "--target", str(tmp_path), "--harnesses", "codex", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    portable = next(step for step in payload["steps"] if step["id"] == "portable-bootstrap")
    assert portable["status"] == "skipped"
    assert (tmp_path / "AGENTS.md").is_file()
    assert (tmp_path / "SAFETY_RULES.md").is_file()
    for extra in ("rules", "hooks", "tools", "scripts", "INSTALL_FOR_AGENTS.md"):
        assert not (tmp_path / extra).exists(), f"{extra} should need --full"
    # the work-loop skills are still wired
    assert (tmp_path / ".codex" / "skills" / "brigade-work" / "SKILL.md").is_file()

    assert cli.main(["operator", "doctor", "--target", str(tmp_path), "--profile", "local-operator", "--json"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["ready"] is True


def test_operator_quickstart_prepares_new_user_workspace(tmp_path, capsys):
    assert (
        cli.main(
            ["operator", "quickstart", "--target", str(tmp_path), "--harnesses", "codex,opencode", "--full", "--json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["owner"] == "codex"
    assert [step["id"] for step in payload["steps"]][:3] == ["brigade-init", "operator-init", "portable-bootstrap"]
    assert any(step["id"] == "verify-codex" for step in payload["steps"])
    assert any(step["id"] == "verify-opencode" for step in payload["steps"])
    assert payload["issue_report"]["brigade_version"]
    assert payload["issue_report"]["status"] == "ok"
    assert payload["issue_report"]["github_issue_url"].endswith("/issues/new/choose")
    assert (tmp_path / ".brigade" / "config.json").is_file()
    assert (tmp_path / ".brigade" / "daily.toml").is_file()
    assert (tmp_path / ".brigade" / "reviews.toml").is_file()
    gitignore = (tmp_path / ".gitignore").read_text()
    assert ".brigade/" in gitignore
    assert ".brigade/daily.toml" in gitignore
    assert ".brigade/reviews.toml" in gitignore
    assert (tmp_path / ".codex" / "memory-handoffs").is_dir()
    assert (tmp_path / ".opencode" / "memory-handoffs").is_dir()
    sources = json.loads((tmp_path / ".brigade" / "handoff-sources.json").read_text())
    assert sources["sources"][0]["inboxes"] == [".codex/memory-handoffs", ".opencode/memory-handoffs"]
    assert (tmp_path / ".brigade" / "handoff-ingest" / "latest.log").is_file()
    assert (tmp_path / ".codex" / "skills" / "frontend" / "SKILL.md").is_file()
    assert (tmp_path / "tools" / "antislop.md").is_file()

    assert cli.main(["operator", "doctor", "--target", str(tmp_path), "--profile", "local-operator", "--json"]) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["ready"] is True
    assert doctor["next_command"] == "brigade daily plan --target ."

    assert cli.main(["handoff", "doctor", "--target", str(tmp_path), "--json"]) == 0
    handoff_doctor = json.loads(capsys.readouterr().out)
    assert handoff_doctor["warnings"] == []


def test_operator_quickstart_scaffolds_mcp_onramp(tmp_path, capsys):
    assert cli.main(["operator", "quickstart", "--target", str(tmp_path), "--harnesses", "codex", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    # The README leads with MCP/tool sync, so quickstart must scaffold the
    # canonical catalog and surface the MCP commands on the golden path.
    assert (tmp_path / ".brigade" / "mcp.json").is_file()
    mcp_step = next((step for step in payload["steps"] if step["id"] == "mcp-init"), None)
    assert mcp_step is not None
    assert mcp_step["status"] == "ok"
    assert any("brigade mcp init" in command for command in payload["next_commands"])
    assert any("brigade mcp sync --write" in command for command in payload["next_commands"])


def test_operator_quickstart_arms_dogfood_loop(tmp_path, capsys):
    assert cli.main(["operator", "quickstart", "--target", str(tmp_path), "--harnesses", "codex", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    # Without a dogfood config the work station is wired but dormant, so a fresh
    # repo never captures runs or feeds its outcome ledger. Quickstart must arm it.
    assert (tmp_path / ".brigade" / "dogfood.toml").is_file()
    dogfood_step = next((step for step in payload["steps"] if step["id"] == "dogfood-init"), None)
    assert dogfood_step is not None
    assert dogfood_step["status"] == "ok"


def test_operator_quickstart_dry_run_plans_dogfood_without_writing(tmp_path, capsys):
    assert (
        cli.main(["operator", "quickstart", "--target", str(tmp_path), "--harnesses", "codex", "--dry-run", "--json"])
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    dogfood_step = next((step for step in payload["steps"] if step["id"] == "dogfood-init"), None)
    assert dogfood_step is not None
    assert dogfood_step["status"] == "planned"
    assert not (tmp_path / ".brigade" / "dogfood.toml").exists()


def test_operator_quickstart_mcp_onramp_does_not_write_tool_configs(tmp_path, capsys):
    assert cli.main(["operator", "quickstart", "--target", str(tmp_path), "--harnesses", "codex", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    # The on-ramp is dry-run by default: it scaffolds the canonical catalog but
    # must never write a harness MCP config automatically.
    assert (tmp_path / ".brigade" / "mcp.json").is_file()
    assert not (tmp_path / ".mcp.json").exists()
    # codex's real MCP target is .codex/config.toml; the on-ramp must not write
    # mcp_servers into it (sync is never invoked, only init + a read-only plan).
    codex_cfg = tmp_path / ".codex" / "config.toml"
    assert not codex_cfg.exists() or "mcp_servers" not in codex_cfg.read_text()


def test_operator_quickstart_dry_run_does_not_write(tmp_path, capsys):
    assert (
        cli.main(
            ["operator", "quickstart", "--target", str(tmp_path), "--harnesses", "codex,claude", "--dry-run", "--json"]
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["status"] == "ok"
    assert payload["issue_report"]["dry_run"] is True
    assert any(step["status"] == "planned" for step in payload["steps"])
    assert not (tmp_path / ".brigade").exists()
    assert not (tmp_path / ".codex").exists()


def test_operator_quickstart_dry_run_prints_planned_files(tmp_path, capsys):
    # The README promises a file-by-file plan; step names alone are not one
    # (audit 2026-07-02, backlog item 4).
    assert cli.main(["operator", "quickstart", "--target", str(tmp_path), "--harnesses", "codex", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert str(tmp_path / "AGENTS.md") in out
    assert str(tmp_path / "SAFETY_RULES.md") in out
    assert str(tmp_path / ".codex/memory-handoffs/TEMPLATE.md") in out
    assert str(tmp_path / ".brigade/mcp.json") in out
    # operator-init planned artifacts are listed too
    assert ".brigade/config.json" in out or "handoff-sources" in out


def test_operator_sync_tools_projects_tracked_sources(tmp_path, capsys):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "simplify.md").write_text("# Simplify\n\nSummarize clearly.\n")
    (tools_dir / "superpowers.md").write_text("# Superpowers\n\nShare capabilities across harnesses.\n")
    (tools_dir / "frontend.md").write_text("# Frontend\n\nBuild usable interfaces.\n")
    (tools_dir / "antislop.md").write_text("# Anti-Slop\n\nRemove vague unfinished work.\n")

    assert cli.main(["operator", "init", "--target", str(tmp_path), "--json"]) == 0
    capsys.readouterr()
    assert cli.main(["operator", "sync-tools", "--target", str(tmp_path), "--dry-run", "--json"]) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["dry_run"] is True
    assert dry_run["apply"]["applied_count"] == 84
    assert not (tmp_path / ".claude" / "commands" / "simplify.md").exists()

    assert cli.main(["operator", "sync-tools", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["apply"]["applied_count"] == 84
    assert payload["tool_health"]["issue_count"] == 0
    assert (tmp_path / ".claude" / "commands" / "simplify.md").is_file()
    assert (tmp_path / ".claude" / "commands" / "superpowers.md").is_file()
    assert (tmp_path / ".codex" / "skills" / "simplify" / "SKILL.md").is_file()
    assert (tmp_path / ".codex" / "skills" / "superpowers" / "SKILL.md").is_file()
    assert (tmp_path / ".opencode" / "commands" / "simplify.md").is_file()
    assert (tmp_path / ".opencode" / "superpowers" / "superpowers.md").is_file()
    assert (tmp_path / ".antigravity" / "commands" / "simplify.md").is_file()
    assert (tmp_path / ".antigravity" / "superpowers" / "superpowers.md").is_file()
    assert (tmp_path / ".pi" / "commands" / "simplify.md").is_file()
    assert (tmp_path / ".pi" / "superpowers" / "superpowers.md").is_file()
    assert (tmp_path / ".cursor" / "rules" / "simplify.md").is_file()
    assert (tmp_path / ".cursor" / "rules" / "superpowers.md").is_file()
    assert (tmp_path / ".hermes" / "commands" / "simplify.md").is_file()
    assert (tmp_path / ".hermes" / "superpowers" / "superpowers.md").is_file()
    assert (tmp_path / ".openclaw" / "commands" / "simplify.md").is_file()
    assert (tmp_path / ".openclaw" / "superpowers" / "superpowers.md").is_file()
    assert (tmp_path / ".mcp" / "simplify.md").is_file()
    assert (tmp_path / ".mcp" / "superpowers.md").is_file()
    assert (tmp_path / ".claude" / "commands" / "frontend.md").is_file()
    assert (tmp_path / ".codex" / "skills" / "frontend" / "SKILL.md").is_file()
    assert (tmp_path / ".opencode" / "commands" / "frontend.md").is_file()
    assert (tmp_path / ".antigravity" / "commands" / "frontend.md").is_file()
    assert (tmp_path / ".pi" / "commands" / "frontend.md").is_file()
    assert (tmp_path / ".cursor" / "rules" / "frontend.md").is_file()
    assert (tmp_path / ".mcp" / "frontend.md").is_file()
    assert (tmp_path / ".claude" / "commands" / "antislop.md").is_file()
    assert (tmp_path / ".codex" / "skills" / "antislop" / "SKILL.md").is_file()
    assert (tmp_path / ".opencode" / "commands" / "antislop.md").is_file()
    assert (tmp_path / ".antigravity" / "commands" / "antislop.md").is_file()
    assert (tmp_path / ".pi" / "commands" / "antislop.md").is_file()
    assert (tmp_path / ".cursor" / "rules" / "antislop.md").is_file()
    assert (tmp_path / ".mcp" / "antislop.md").is_file()
    assert (tmp_path / "scripts" / "simplify.md").is_file()
    assert (tmp_path / "scripts" / "superpowers.md").is_file()
    assert (tmp_path / "scripts" / "frontend.md").is_file()
    assert (tmp_path / "scripts" / "antislop.md").is_file()


def test_operator_sync_tools_merges_current_builtins_for_stale_catalog(tmp_path, capsys):
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    for name in ("simplify", "superpowers", "frontend", "antislop"):
        (tools_dir / f"{name}.md").write_text(f"# {name}\n")
    config = tmp_path / ".brigade" / "tools.toml"
    config.parent.mkdir()
    config.write_text(
        """
[[tool]]
id = "simplify"
name = "Simplify"
family = "slash-command"
enabled = true
description = "old"
source_path = "tools/simplify.md"
supported_harnesses = ["claude"]
projections = { claude = ".claude/commands/simplify.md" }
"""
    )

    assert cli.main(["operator", "sync-tools", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["defaults"]["updated"] == ["simplify"]
    assert set(payload["defaults"]["added"]) == {"superpowers", "frontend", "antislop"}
    assert payload["apply"]["applied_count"] == 84
    assert (tmp_path / ".codex" / "skills" / "frontend" / "SKILL.md").is_file()
    assert (tmp_path / ".codex" / "skills" / "antislop" / "SKILL.md").is_file()


def test_internal_dogfood_fresh_repo_onboarding_loop(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    (tmp_path / "README.md").write_text("# Test repo\n")
    (tmp_path / "ROADMAP.md").write_text("# Roadmap\n")
    tools_dir = tmp_path / "tools"
    tools_dir.mkdir()
    (tools_dir / "simplify.md").write_text("# Simplify\n\nSummarize clearly.\n")
    (tools_dir / "superpowers.md").write_text("# Superpowers\n\nShare capabilities across harnesses.\n")
    (tools_dir / "frontend.md").write_text("# Frontend\n\nBuild usable interfaces.\n")
    (tools_dir / "antislop.md").write_text("# Anti-Slop\n\nRemove vague unfinished work.\n")

    assert cli.main(["roadmap", "commands", "--target", str(tmp_path), "--write", "--json"]) == 0
    capsys.readouterr()
    assert (
        cli.main(
            [
                "operator",
                "init",
                "--profile",
                "internal-dogfood",
                "--target",
                str(tmp_path),
                "--waive-public-release",
                "--json",
            ]
        )
        == 0
    )
    init_payload = json.loads(capsys.readouterr().out)
    assert init_payload["profile"] == "internal-dogfood"

    assert cli.main(["operator", "sync-tools", "--target", str(tmp_path), "--json"]) == 0
    sync_payload = json.loads(capsys.readouterr().out)
    assert sync_payload["status"] == "ok"
    assert sync_payload["apply"]["applied_count"] == 84

    assert cli.main(["operator", "status", "--profile", "internal-dogfood", "--target", str(tmp_path), "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["issue_count"] == 0
    assert status["dogfood"]["ready"] is True
    assert status["repo"]["missing_config_count"] == 0
    assert status["security"]["issue_count"] == 0

    assert cli.main(["daily", "status", "--target", str(tmp_path), "--json"]) == 0
    daily_status = json.loads(capsys.readouterr().out)
    assert daily_status["daily_health"]["issue_count"] == 0


def test_tools_defaults_scopes_projections_to_configured_selection(tmp_path, capsys):
    from brigade.install import install_selection
    from brigade.selection import Selection

    install_selection(tmp_path, Selection(depth="repo", harnesses=["codex"], owner="codex", includes=[]))
    capsys.readouterr()
    assert cli.main(["tools", "defaults", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    toml_text = (tmp_path / ".brigade" / "tools.toml").read_text()
    assert ".codex/" in toml_text
    assert "scripts/" in toml_text
    assert ".qwen/" not in toml_text
    assert ".adal/" not in toml_text
    assert ".mcp/" not in toml_text


def test_operator_quickstart_scopes_projections_to_selected_harnesses(tmp_path, capsys):
    assert (
        cli.main(["operator", "quickstart", "--target", str(tmp_path), "--harnesses", "codex", "--full", "--json"]) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert (tmp_path / ".codex" / "skills" / "simplify" / "SKILL.md").is_file()
    assert (tmp_path / "scripts" / "simplify.md").is_file()
    for unselected in (".claude", ".qwen", ".adal", ".antigravity", ".cursor", ".mcp"):
        assert not (tmp_path / unselected).exists(), f"{unselected} should not be created"


def test_operator_quickstart_gitignore_covers_all_selected_inboxes(tmp_path, capsys):
    assert cli.main(["operator", "quickstart", "--target", str(tmp_path), "--harnesses", "codex,claude", "--json"]) == 0
    capsys.readouterr()
    gitignore = (tmp_path / ".gitignore").read_text()
    assert ".codex/memory-handoffs/*" in gitignore
    assert ".claude/memory-handoffs/*" in gitignore


def test_verify_harness_warns_when_inbox_template_shadowed_by_external_ignore(tmp_path, capsys):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert cli.main(["operator", "quickstart", "--target", str(tmp_path), "--harnesses", "codex", "--json"]) == 0
    capsys.readouterr()

    cli.main(["operator", "verify-harness", "--target", str(tmp_path), "--harness", "codex", "--json"])
    payload = json.loads(capsys.readouterr().out)
    shadow = [c for c in payload["checks"] if c["name"] == "handoff_template_shadowed"]
    assert shadow and shadow[0]["status"] == "ok"

    (tmp_path / ".git" / "info" / "exclude").write_text(".codex/\n")
    cli.main(["operator", "verify-harness", "--target", str(tmp_path), "--harness", "codex", "--json"])
    payload = json.loads(capsys.readouterr().out)
    shadow = [c for c in payload["checks"] if c["name"] == "handoff_template_shadowed"]
    assert shadow and shadow[0]["status"] == "warn"
    assert "shadow" in shadow[0]["detail"] or "global" in shadow[0]["detail"]
    # A portability advisory must not flip readiness: warns inform, fails block.
    assert payload["ready"] is True
    assert payload["issue_count"] == 0
    assert payload["warning_count"] >= 1


def test_local_operator_doctor_does_not_block_on_inactive_content_guard_hook(tmp_path, capsys, monkeypatch):
    assert cli.main(["operator", "quickstart", "--target", str(tmp_path), "--harnesses", "codex", "--json"]) == 0
    capsys.readouterr()

    def fake_hook_status(target, policy="public-repo"):
        # A hook file is wired into the repo but not enabled. The embedded
        # guard makes "available" true on every install, so only explicit
        # hook signals count as configured.
        return {
            "available": True,
            "hooks_path": None,
            "configured_pre_push_hook_exists": True,
            "git_pre_push_hook_exists": False,
            "pre_push_hook_enabled": False,
            "pre_push_hook_mode": "not-enabled",
            "checks": [
                {
                    "status": "warn",
                    "name": "content_guard_hook_not_enabled",
                    "detail": "no executable pre-push hook found in the active Git hooks path",
                },
            ],
            "suggested_commands": ["git config core.hooksPath hooks"],
            "last_scan": None,
        }

    monkeypatch.setattr(operator_cmd.scrub, "hook_status", fake_hook_status)

    assert cli.main(["operator", "doctor", "--target", str(tmp_path), "--profile", "local-operator", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    blocker_names = [b.get("name") for b in payload["blockers"]]
    assert "content_guard_hook_not_enabled" not in blocker_names
    assert payload["ready"] is True

    cli.main(["operator", "doctor", "--target", str(tmp_path), "--profile", "internal-dogfood", "--json"])
    payload = json.loads(capsys.readouterr().out)
    blocker_names = [b.get("name") for b in payload["blockers"]]
    assert "content_guard_hook_not_enabled" in blocker_names


def test_adopt_plan_counts_guidance_files_and_dirs_separately(tmp_path, capsys):
    (tmp_path / "CLAUDE.md").write_text("# rules\n")
    (tmp_path / "memory" / "cards").mkdir(parents=True)

    assert cli.main(["operator", "adopt", "plan", "--target", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    guidance = payload["workspace"]["guidance"]
    assert guidance["present_count"] == 1
    assert guidance["present_dir_count"] == 1
