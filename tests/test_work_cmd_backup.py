import json
from datetime import datetime, timezone

from brigade import cli
from brigade import dogfood_cmd
from brigade import localio
from brigade import release_cmd
from brigade import work_cmd

from tests.work_cmd_test_helpers import (
    _write_json,
    _init_git_repo,
)


def test_work_backup_init_status_doctor_and_json(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")
    now = datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(work_cmd.helpers, "_now", lambda: now)

    assert work_cmd.backup_init(target=tmp_path) == 0
    out = capsys.readouterr().out
    config = tmp_path / ".brigade" / "backups.toml"
    assert f"backup_config: {config}" in out
    assert "destinations: 2" in out
    assert ".brigade/backups.toml" in (tmp_path / ".gitignore").read_text()

    nas = tmp_path / ".brigade" / "backups" / "nas-summary.json"
    cloud = tmp_path / ".brigade" / "backups" / "cloud-summary.json"
    nas.parent.mkdir(parents=True)
    for path, label in ((nas, "NAS backup"), (cloud, "Cloud backup")):
        _write_json(
            path,
            {
                "destination_label": label,
                "latest_snapshot_at": "2026-05-30T06:00:00+00:00",
                "latest_check_at": "2026-05-29T12:00:00+00:00",
                "latest_check_result": "ok",
                "latest_prune_at": "2026-05-29T12:30:00+00:00",
                "latest_prune_result": "ok",
                "latest_restore_rehearsal_at": "2026-05-01T12:00:00+00:00",
                "latest_restore_rehearsal_result": "ok",
                "summary": f"{label} is current.",
                "evidence_path": f".brigade/backups/{path.name}",
            },
        )

    assert work_cmd.backup_status(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "work backup status:" in out
    assert "- nas [enabled] nas issues=0" in out
    assert "- cloud [enabled] cloud issues=0" in out
    assert "top_issue: none" in out

    assert work_cmd.backup_status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["valid"] is True
    assert payload["issue_count"] == 0

    assert work_cmd.backup_doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[ok] backup_config:" in out
    assert "backup_issues: 0" in out


def test_work_backup_contract_reports_summary_producer_shape(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")

    assert work_cmd.backup_contract(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"]["name"] == "backup-summary-producer-contract"
    assert payload["config_loaded"] is False
    assert payload["destination_count"] == 2
    assert payload["would_write"] is False
    assert payload["manual_only"] is True
    assert "latest_snapshot_at" in payload["required_fields"]
    assert "ok" in payload["accepted_success_results"]

    assert work_cmd.backup_init(target=tmp_path) == 0
    capsys.readouterr()
    assert work_cmd.backup_contract(target=tmp_path, destination_id="nas", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["config_loaded"] is True
    assert payload["destination_count"] == 1
    destination = payload["destinations"][0]
    assert destination["id"] == "nas"
    assert destination["summary_path"].endswith(".brigade/backups/nas-summary.json")
    assert destination["example_summary"]["latest_check_result"] == "ok"
    assert "hostname" in payload["privacy"]["forbidden_field_names"]

    assert work_cmd.backup_contract(target=tmp_path, destination_id="missing", json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"] == ["backup destination not found: missing"]


def test_work_backup_doctor_warns_for_backup_health_issues(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = tmp_path / ".brigade" / "backups.toml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        """
[[destination]]
id = "nas"
kind = "nas"
command_label = "safe summary"
summary_path = ".brigade/backups/nas-summary.json"
snapshot_stale_hours = 24
check_stale_hours = 48
prune_stale_hours = 48
restore_rehearsal_stale_days = 30
enabled = true

[[destination]]
id = "cloud"
kind = "cloud"
command_label = "safe summary"
summary_path = ".brigade/backups/cloud-summary.json"
snapshot_stale_hours = 24
check_stale_hours = 48
prune_stale_hours = 48
restore_rehearsal_stale_days = 30
enabled = true
"""
    )
    nas = tmp_path / ".brigade" / "backups" / "nas-summary.json"
    nas.parent.mkdir(parents=True)
    _write_json(
        nas,
        {
            "destination_label": "NAS backup",
            "latest_snapshot_at": "2026-05-25T12:00:00+00:00",
            "latest_check_at": "2026-05-30T10:00:00+00:00",
            "latest_check_result": "failed",
            "latest_prune_at": "2026-05-20T12:00:00+00:00",
            "latest_prune_result": "ok",
            "latest_restore_rehearsal_at": "2026-04-01T12:00:00+00:00",
            "latest_restore_rehearsal_result": "ok",
            "summary": "NAS backup has stale evidence.",
            "evidence_path": ".brigade/backups/nas-evidence.json",
            "hostname": "private-host",
            "repo_path": "/private/restic/repo",
            "webhook_url": "https://example.invalid/hook",
        },
    )

    assert work_cmd.backup_doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[warn] backup_missing_summary: missing summary:" in out
    assert "[warn] backup_unsafe_summary_fields:" in out
    assert "hostname" in out
    assert "private-host" not in out
    assert "[warn] backup_snapshot_stale: NAS backup latest snapshot is 120.0h old" in out
    assert "[warn] backup_check_failed: NAS backup latest check result is failed" in out
    assert "[warn] backup_prune_stale: NAS backup latest prune is 240.0h old" in out
    assert "[warn] backup_restore_rehearsal_overdue: NAS backup latest restore rehearsal is 59.0d old" in out

    assert work_cmd.backup_doctor(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["issue_count"] >= 6
    rendered = json.dumps(payload, sort_keys=True)
    assert "private-host" not in rendered
    assert "repo_path" in rendered


def test_work_backup_import_issues_dedupes_and_respects_dismissed_until_change(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = tmp_path / ".brigade" / "backups.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
[[destination]]
id = "nas"
kind = "nas"
command_label = "safe summary"
summary_path = ".brigade/backups/nas-summary.json"
snapshot_stale_hours = 24
check_stale_hours = 48
prune_stale_hours = 48
restore_rehearsal_stale_days = 30
enabled = true
"""
    )
    summary = tmp_path / ".brigade" / "backups" / "nas-summary.json"
    summary.parent.mkdir(parents=True)
    _write_json(
        summary,
        {
            "destination_label": "NAS backup",
            "latest_snapshot_at": "2026-05-25T12:00:00+00:00",
            "latest_check_at": "2026-05-30T10:00:00+00:00",
            "latest_check_result": "ok",
            "latest_prune_at": "2026-05-30T10:00:00+00:00",
            "latest_prune_result": "ok",
            "latest_restore_rehearsal_at": "2026-05-01T12:00:00+00:00",
            "latest_restore_rehearsal_result": "ok",
            "summary": "NAS backup snapshot is stale.",
            "evidence_path": ".brigade/backups/nas-evidence.json",
        },
    )

    assert work_cmd.backup_import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1
    item = payload["imports"][0]
    assert item["source"] == "backup-health"
    assert item["metadata"]["backup_destination"] == "nas"
    assert item["metadata"]["backup_issue_type"] == "snapshot_stale"

    assert work_cmd.backup_import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 0
    assert payload["skipped"] == 1

    assert work_cmd.import_dismiss(target=tmp_path, import_id=item["id"], reason="ack") == 0
    capsys.readouterr()
    assert work_cmd.backup_import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 0
    assert payload["dismissed"] == 1

    data = json.loads(summary.read_text())
    data["latest_snapshot_at"] = "2026-05-24T12:00:00+00:00"
    _write_json(summary, data)
    assert work_cmd.backup_import_issues(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["created"] == 1


def test_work_backup_closeout_quiets_reviewed_risk_and_resurfaces_changed_fingerprints(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = tmp_path / ".brigade" / "backups.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
[[destination]]
id = "nas"
kind = "nas"
command_label = "safe summary"
summary_path = ".brigade/backups/nas-summary.json"
snapshot_stale_hours = 24
check_stale_hours = 48
prune_stale_hours = 48
restore_rehearsal_stale_days = 30
enabled = true
"""
    )
    summary = tmp_path / ".brigade" / "backups" / "nas-summary.json"
    summary.parent.mkdir(parents=True)
    _write_json(
        summary,
        {
            "destination_label": "NAS backup",
            "latest_snapshot_at": "2026-05-25T12:00:00+00:00",
            "latest_check_at": "2026-05-30T10:00:00+00:00",
            "latest_check_result": "ok",
            "latest_prune_at": "2026-05-30T10:00:00+00:00",
            "latest_prune_result": "ok",
            "latest_restore_rehearsal_at": "2026-05-01T12:00:00+00:00",
            "latest_restore_rehearsal_result": "ok",
            "summary": "Backup snapshot needs review.",
            "evidence_path": ".brigade/backups/nas-evidence.json",
        },
    )

    assert work_cmd.backup_status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["issue_count"] == 1
    assert payload["raw_issue_count"] == 1

    assert work_cmd.backup_closeout(target=tmp_path, reason="known maintenance", json_output=True) == 0
    closeout = json.loads(capsys.readouterr().out)
    assert closeout["issue_count"] == 1
    assert closeout["source_fingerprints"]

    assert work_cmd.backup_status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["issue_count"] == 0
    assert payload["raw_issue_count"] == 1
    assert payload["quieted_issue_count"] == 1
    assert payload["changed_fingerprint_count"] == 0
    assert (
        payload["operator_summary"]
        == "0 active backup issue(s), 1 reviewed/deferred issue(s), 0 restore rehearsal issue(s)"
    )

    assert work_cmd.backup_doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "backup_issues: 0" in out
    assert "backup_snapshot_stale" not in out

    current = json.loads(summary.read_text())
    current["latest_snapshot_at"] = "2026-05-24T12:00:00+00:00"
    _write_json(summary, current)

    assert work_cmd.backup_status(target=tmp_path, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["issue_count"] == 1
    assert payload["raw_issue_count"] == 1
    assert payload["quieted_issue_count"] == 0
    assert payload["changed_fingerprint_count"] == 1
    assert payload["top_issue"]["issue_type"] == "snapshot_stale"


def test_release_evidence_includes_restore_rehearsal_backup_summary(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        release_cmd,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    config = tmp_path / ".brigade" / "backups.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        """
[[destination]]
id = "cloud"
kind = "cloud"
command_label = "safe summary"
summary_path = ".brigade/backups/cloud-summary.json"
snapshot_stale_hours = 24
check_stale_hours = 48
prune_stale_hours = 48
restore_rehearsal_stale_days = 30
enabled = true
"""
    )
    summary = tmp_path / ".brigade" / "backups" / "cloud-summary.json"
    summary.parent.mkdir(parents=True)
    _write_json(
        summary,
        {
            "destination_label": "Cloud backup",
            "latest_snapshot_at": "2026-05-30T08:00:00+00:00",
            "latest_check_at": "2026-05-30T10:00:00+00:00",
            "latest_check_result": "ok",
            "latest_prune_at": "2026-05-30T10:00:00+00:00",
            "latest_prune_result": "ok",
            "latest_restore_rehearsal_at": "2026-04-01T12:00:00+00:00",
            "latest_restore_rehearsal_result": "ok",
            "summary": "Restore rehearsal needs review.",
            "evidence_path": ".brigade/backups/cloud-evidence.json",
            "hostname": "backup-host.private",
        },
    )

    assert work_cmd.backup_closeout(target=tmp_path, reason="reviewed", json_output=True) == 0
    capsys.readouterr()

    assert release_cmd.plan(target=tmp_path, base_ref=None, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    backup = payload["evidence"]["backup"]
    assert backup["issue_count"] == 0
    assert backup["raw_issue_count"] == 2
    assert backup["quieted_issue_count"] == 2
    assert backup["restore_rehearsal_issue_count"] == 1
    assert "restore rehearsal issue(s)" in backup["operator_summary"]
    rendered = json.dumps(backup, sort_keys=True)
    assert "backup-host.private" not in rendered


def test_work_brief_and_doctor_include_backup_health(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    dogfood_cmd.init(target=tmp_path)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(localio, "check_git_ignored", lambda repo, path: "yes")
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 5, 30, 12, 0, 0, tzinfo=timezone.utc),
    )
    assert work_cmd.backup_init(target=tmp_path, update_gitignore=False) == 0
    capsys.readouterr()

    assert work_cmd.brief(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "backup_config:" in out
    assert "backup_health:" in out
    assert "backup_top_issue:" in out

    assert work_cmd.doctor(target=tmp_path) == 0
    out = capsys.readouterr().out
    assert "[ok] backup_config:" in out
    assert "[warn] backup_missing_summary:" in out


def test_work_backup_cli(tmp_path, monkeypatch):
    seen = []

    def fake_backup_init(**kwargs):
        seen.append(("init", kwargs))
        return 0

    def fake_backup_status(**kwargs):
        seen.append(("status", kwargs))
        return 0

    def fake_backup_contract(**kwargs):
        seen.append(("contract", kwargs))
        return 0

    def fake_backup_doctor(**kwargs):
        seen.append(("doctor", kwargs))
        return 0

    def fake_backup_import_issues(**kwargs):
        seen.append(("import-issues", kwargs))
        return 0

    monkeypatch.setattr(work_cmd, "backup_init", fake_backup_init)
    monkeypatch.setattr(work_cmd, "backup_contract", fake_backup_contract)
    monkeypatch.setattr(work_cmd, "backup_status", fake_backup_status)
    monkeypatch.setattr(work_cmd, "backup_doctor", fake_backup_doctor)
    monkeypatch.setattr(work_cmd, "backup_import_issues", fake_backup_import_issues)

    assert cli.main(["work", "backup", "init", "--target", str(tmp_path), "--force", "--no-gitignore"]) == 0
    assert cli.main(["work", "backup", "contract", "--target", str(tmp_path), "--destination", "nas", "--json"]) == 0
    assert cli.main(["work", "backup", "status", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "backup", "doctor", "--target", str(tmp_path), "--json"]) == 0
    assert cli.main(["work", "backup", "import-issues", "--target", str(tmp_path), "--json"]) == 0
    assert seen == [
        ("init", {"target": tmp_path, "force": True, "update_gitignore": False}),
        ("contract", {"target": tmp_path, "destination_id": "nas", "json_output": True}),
        ("status", {"target": tmp_path, "json_output": True}),
        ("doctor", {"target": tmp_path, "json_output": True}),
        ("import-issues", {"target": tmp_path, "json_output": True}),
    ]
