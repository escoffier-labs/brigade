"""RED regression tests for issue 266 work-status memory churn."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from brigade import center_cmd
from brigade import daily_cmd
from brigade import repos_cmd
from brigade.repos_cmd import fleet as repos_fleet
from brigade.repos_cmd import sweeps as repos_sweeps
from tests.issue266_fixture import (
    FLEET_REPO_COUNT,
    FLEET_SWEEP_HISTORY_COUNT,
    HEALTH_COMMAND_LABEL,
    OPERATOR_REPORT_HISTORY_COUNT,
    build_daily_status_workspace,
    build_fleet_workspace,
    seed_fleet_repo_dirs,
    write_json,
)


def _counting_wrapper(original, counter: dict[str, int], key: str):
    def wrapped(*args, **kwargs):
        counter[key] += 1
        return original(*args, **kwargs)

    return wrapped


def test_repos_health_does_not_decode_full_sweep_history(tmp_path, monkeypatch):
    target = build_fleet_workspace(
        tmp_path / "fleet-history-ws",
        sweep_history_count=FLEET_SWEEP_HISTORY_COUNT,
    )
    counter = {"sweep_json_decodes": 0}
    monkeypatch.setattr(
        repos_sweeps,
        "_read_sweep",
        _counting_wrapper(repos_sweeps._read_sweep, counter, "sweep_json_decodes"),
    )

    repos_cmd.health(target)

    assert counter["sweep_json_decodes"] == 1, (
        "repos health should decode only the newest sweep needed for health-command "
        "receipts, not every timestamp-prefixed sweep on disk; "
        f"got {counter['sweep_json_decodes']} decodes across "
        f"{FLEET_SWEEP_HISTORY_COUNT} historical sweeps covering {FLEET_REPO_COUNT} repos "
        "(expected root cause: fleet_health.health calls sweeps._sweeps, which reads and "
        "decodes every sweep.json under .brigade/repos/sweeps)"
    )


def test_repos_health_loads_fleet_config_once(tmp_path, monkeypatch):
    target = build_fleet_workspace(tmp_path / "fleet-config-ws")
    counter = {"fleet_config_loads": 0}
    monkeypatch.setattr(
        repos_fleet,
        "_load_config",
        _counting_wrapper(repos_fleet._load_config, counter, "fleet_config_loads"),
    )

    repos_cmd.health(target)

    assert counter["fleet_config_loads"] == 1, (
        "repos health should load repos.toml once per invocation; "
        f"got {counter['fleet_config_loads']} fleet._load_config calls "
        "(expected root cause: fleet.scan_payload and "
        "_health_command_registry_payload each call fleet._load_config)"
    )


def test_repos_health_decodes_shared_fleet_sweep_once(tmp_path, monkeypatch):
    target = build_fleet_workspace(tmp_path / "fleet-ws")
    counter = {"sweep_json_decodes": 0}
    monkeypatch.setattr(
        repos_sweeps,
        "_read_sweep",
        _counting_wrapper(repos_sweeps._read_sweep, counter, "sweep_json_decodes"),
    )

    repos_cmd.health(target)

    assert counter["sweep_json_decodes"] == 1, (
        "repos health should decode the shared fleet sweep artifact once; "
        f"got {counter['sweep_json_decodes']} decodes for {FLEET_REPO_COUNT} configured repos "
        "(expected root cause: _health_command_registry_payload calls _latest_command_receipt per "
        "repo-command and each call reloads all sweep JSON via _sweeps)"
    )


def test_repos_health_falls_back_for_receipt_missing_from_newest_sweep(tmp_path, monkeypatch):
    target = build_fleet_workspace(
        tmp_path / "fleet-fallback-ws",
        repo_count=2,
        sweep_history_count=3,
    )
    newest_dir, fallback_dir, unused_dir = repos_sweeps._list_sweep_dirs_newest_first(target)
    newest = repos_sweeps._read_sweep(newest_dir)
    assert newest is not None
    newest["repos"][1]["commands"] = []
    write_json(newest_dir / "sweep.json", newest)
    counter = {"sweep_json_decodes": 0}
    monkeypatch.setattr(
        repos_sweeps,
        "_read_sweep",
        _counting_wrapper(repos_sweeps._read_sweep, counter, "sweep_json_decodes"),
    )

    payload = repos_cmd.health(target)

    assert counter["sweep_json_decodes"] == 2
    assert payload["sweep"]["latest"]["sweep_id"] == newest_dir.name
    health_by_repo = {
        repo["repo_id"]: repo["health_commands"][0]["latest_receipt"] for repo in payload["health_commands"]["repos"]
    }
    assert health_by_repo["fake-repo-001"]["sweep_id"] == newest_dir.name
    assert health_by_repo["fake-repo-002"]["sweep_id"] == fallback_dir.name
    assert all(receipt["sweep_id"] != unused_dir.name for receipt in health_by_repo.values())


def test_repos_health_orders_legacy_sweep_dirs_by_payload_timestamp(tmp_path):
    target = build_fleet_workspace(
        tmp_path / "fleet-legacy-order-ws",
        repo_count=1,
        sweep_history_count=2,
    )
    older_dir, newer_dir = repos_sweeps._list_sweep_dirs_newest_first(target)
    older = repos_sweeps._read_sweep(older_dir)
    newer = repos_sweeps._read_sweep(newer_dir)
    assert older is not None
    assert newer is not None
    older["started_at"] = "2026-07-16T12:00:00+00:00"
    newer["started_at"] = "2026-07-16T13:00:00+00:00"
    older_legacy_dir = older_dir.with_name("zzz-renamed-older")
    newer_legacy_dir = newer_dir.with_name("aaa-renamed-newer")
    older_dir.rename(older_legacy_dir)
    newer_dir.rename(newer_legacy_dir)
    write_json(older_legacy_dir / "sweep.json", older)
    write_json(newer_legacy_dir / "sweep.json", newer)

    payload = repos_cmd.health(target)

    assert payload["sweep"]["latest"]["sweep_id"] == newer["sweep_id"]
    latest_receipt = payload["health_commands"]["repos"][0]["health_commands"][0]["latest_receipt"]
    assert latest_receipt["sweep_id"] == newer["sweep_id"]


def test_health_sweep_snapshot_retains_only_required_receipts(tmp_path):
    target = build_fleet_workspace(
        tmp_path / "fleet-required-receipts-ws",
        repo_count=1,
    )
    sweep_dir = repos_sweeps._list_sweep_dirs_newest_first(target)[0]
    sweep = repos_sweeps._read_sweep(sweep_dir)
    assert sweep is not None
    sweep["repos"][0]["commands"].extend(
        {"label": f"removed-health-{index}", "status": "completed", "exit_code": 0} for index in range(20)
    )
    write_json(sweep_dir / "sweep.json", sweep)
    entries, issues, _config = repos_fleet._load_config(target)
    assert issues == []

    snapshot = repos_sweeps._health_sweep_snapshot(target, entries)

    assert set(snapshot.receipt_index) == {("fake-repo-001", HEALTH_COMMAND_LABEL)}


def test_seed_fleet_repo_dirs_preserves_explicit_empty_ids(tmp_path):
    target = tmp_path / "empty-fleet"

    assert seed_fleet_repo_dirs(target, []) == []
    assert not (target / "fixtures" / "repos").exists()


def test_build_fleet_workspace_rejects_nonempty_target(tmp_path):
    target = tmp_path / "occupied"
    target.mkdir()
    marker = target / "keep.txt"
    marker.write_text("do not overwrite\n")

    try:
        build_fleet_workspace(target)
    except ValueError as exc:
        assert "empty" in str(exc)
    else:
        raise AssertionError("expected a non-empty benchmark target to be rejected")

    assert marker.read_text() == "do not overwrite\n"


def test_daily_fixture_uses_one_current_base_time_for_evidence(tmp_path):
    before = datetime.now(timezone.utc) - timedelta(seconds=1)
    target = build_daily_status_workspace(
        tmp_path / "current-evidence-ws",
        repo_count=1,
        report_count=2,
        sweep_history_count=2,
    )
    after = datetime.now(timezone.utc) + timedelta(seconds=1)

    newest_sweep_dir = repos_sweeps._list_sweep_dirs_newest_first(target)[0]
    newest_sweep = repos_sweeps._read_sweep(newest_sweep_dir)
    assert newest_sweep is not None
    newest_report_dir = max((target / ".brigade" / "center" / "reports").iterdir())
    newest_report = json.loads((newest_report_dir / "CENTER_EVIDENCE.json").read_text())

    base_time = datetime.fromisoformat(newest_sweep["started_at"])
    command = newest_sweep["repos"][0]["commands"][0]
    assert before <= base_time <= after
    assert datetime.fromisoformat(newest_report["created_at"]) == base_time
    assert datetime.fromisoformat(command["started_at"]) == base_time + timedelta(seconds=1)
    assert datetime.fromisoformat(command["completed_at"]) == base_time + timedelta(seconds=5)
    assert datetime.fromisoformat(newest_sweep["completed_at"]) == base_time + timedelta(seconds=10)


def test_operator_report_health_does_not_decode_full_report_history(tmp_path, monkeypatch):
    target = build_daily_status_workspace(tmp_path / "report-ws")
    counter = {"operator_report_decodes": 0}
    monkeypatch.setattr(
        center_cmd,
        "_read_report",
        _counting_wrapper(center_cmd._read_report, counter, "operator_report_decodes"),
    )

    center_cmd.report_health(target)

    assert counter["operator_report_decodes"] <= 2, (
        "operator report health should decode at most latest and compare metadata; "
        f"got {counter['operator_report_decodes']} decodes across "
        f"{OPERATOR_REPORT_HISTORY_COUNT} historical reports "
        "(expected root cause: report_health calls latest_report and then _reports, "
        "each decoding the full operator report history)"
    )


def test_daily_status_does_not_decode_full_operator_report_history(tmp_path, monkeypatch):
    target = build_daily_status_workspace(tmp_path / "daily-ws")
    counter = {"operator_report_decodes": 0}
    monkeypatch.setattr(
        center_cmd,
        "_read_report",
        _counting_wrapper(center_cmd._read_report, counter, "operator_report_decodes"),
    )

    daily_cmd.status_payload(target)

    assert counter["operator_report_decodes"] <= 2, (
        "daily status should not repeatedly decode full operator report history when only "
        "latest/two-latest metadata is needed; "
        f"got {counter['operator_report_decodes']} decodes across "
        f"{OPERATOR_REPORT_HISTORY_COUNT} historical reports "
        "(expected root cause: status_payload calls latest_report and candidate gathering "
        "re-enters report_health)"
    )
