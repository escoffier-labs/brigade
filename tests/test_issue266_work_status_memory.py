"""RED regression tests for issue 266 work-status memory churn."""

from __future__ import annotations

from brigade import center_cmd
from brigade import daily_cmd
from brigade import repos_cmd
from brigade.repos_cmd import fleet as repos_fleet
from brigade.repos_cmd import sweeps as repos_sweeps
from tests.issue266_fixture import (
    FLEET_REPO_COUNT,
    FLEET_SWEEP_HISTORY_COUNT,
    OPERATOR_REPORT_HISTORY_COUNT,
    build_daily_status_workspace,
    build_fleet_workspace,
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
