"""Timer enablement and schedule-health tests for ``brigade care install`` (#2589)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from brigade import care_cmd, care_enable, care_receipts, care_schedules, cli, doctor, extras, work_cmd
from tests.test_care_cmd import _write_runbook_receipt
from tests.work_cmd_test_helpers import _init_git_repo


def _unit_dir(home: Path) -> Path:
    return home / ".config" / "systemd" / "user"


def _wants_dir(home: Path) -> Path:
    return _unit_dir(home) / care_enable.WANTS_DIRNAME


def _unit_path(home: Path, target: Path, entry_id: str, suffix: str) -> Path:
    identity = care_cmd._target_identity(target)
    return _unit_dir(home) / f"brigade-care-{identity}-{entry_id}{suffix}"


def _timer_name(target: Path, entry_id: str) -> str:
    return f"brigade-care-{care_cmd._target_identity(target)}-{entry_id}.timer"


@pytest.fixture
def care_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    target = tmp_path / "ws"
    target.mkdir()
    monkeypatch.setattr(care_cmd, "_is_windows", lambda: False)
    monkeypatch.setattr(care_cmd, "_ensure_care_runbooks", lambda target: 0)
    monkeypatch.setattr(care_schedules, "effective_systemd_calendar", lambda timer_name: (None, None))
    monkeypatch.setattr(extras, "enabled", lambda: True)
    return home, target


def _install(home: Path, target: Path, **kwargs) -> None:
    assert care_cmd.install(target=target, backend="systemd", home=home, **kwargs) == 0


def test_systemd_install_links_each_timer_by_default(care_home, capsys):
    home, target = care_home
    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    by_name = {item["name"]: item for item in payload["enable"]["timers"]}
    for entry_id in ("daily-care", "ingest-sweep", "weekly-outcome-ratchet", "daily-observability", "nightly-ops"):
        name = _timer_name(target, entry_id)
        link = _wants_dir(home) / name
        assert link.is_symlink()
        assert os.readlink(link) == f"../{name}"
        assert link.resolve() == _unit_path(home, target, entry_id, ".timer")
        assert by_name[name]["action"] == "enable"
        assert by_name[name]["status"] == "linked"
        assert by_name[name]["enabled"] is True
    assert payload["enable"]["enabled"] is True
    assert payload["enable"]["conflicts"] == []
    assert [command for command in payload["next_commands"] if "enable --now" in command] == []


def test_systemd_reinstall_is_idempotent(care_home, capsys):
    home, target = care_home
    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True) == 0
    capsys.readouterr()
    link = _wants_dir(home) / _timer_name(target, "daily-care")

    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    by_name = {item["name"]: item for item in payload["enable"]["timers"]}
    record = by_name[_timer_name(target, "daily-care")]
    assert record["action"] == "none"
    assert record["status"] == "already"
    assert record["enabled"] is True
    assert link.is_symlink()
    assert payload["enable"]["enabled"] is True


def test_existing_foreign_link_is_reported_as_conflict_and_untouched(care_home, capsys):
    home, target = care_home
    wants = _wants_dir(home)
    wants.mkdir(parents=True)
    name = _timer_name(target, "daily-care")
    foreign = wants / name
    foreign.write_text("foreign entry\n", encoding="utf-8")

    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["enable"]["conflicts"] == [name]
    record = next(item for item in payload["enable"]["timers"] if item["name"] == name)
    assert record["action"] == "conflict"
    assert record["status"] == "conflict"
    assert record["enabled"] is False
    assert foreign.is_file() and not foreign.is_symlink()
    assert foreign.read_text(encoding="utf-8") == "foreign entry\n"
    assert f"systemctl --user enable --now {name}" in payload["next_commands"]
    assert payload["enable"]["enabled"] is False

    capsys.readouterr()
    assert care_cmd.install(target=target, backend="systemd", home=home) == 0
    assert "foreign entry" in capsys.readouterr().out


def test_blocked_install_enables_nothing(care_home, capsys):
    home, target = care_home
    assert care_cmd.install(target=target, backend="systemd", home=home, entry_ids=["care-scan"]) == 0
    timer = _unit_path(home, target, "care-scan", ".timer")
    that = _wants_dir(home) / timer.name
    that.unlink()
    edited = timer.read_text(encoding="utf-8").replace("memory care scan timer", "hand-edited timer")
    timer.write_text(edited, encoding="utf-8")
    capsys.readouterr()

    assert care_cmd.install(target=target, backend="systemd", home=home, entry_ids=["care-scan"], json_output=True) == 1
    payload = json.loads(capsys.readouterr().out)
    record = next(item for item in payload["enable"]["timers"] if item["name"] == timer.name)
    assert record["action"] == "skip"
    assert record["enabled"] is False
    assert "blocked" in record["detail"]
    assert not that.is_symlink()


def test_adopted_registration_is_not_enabled(care_home, capsys):
    home, target = care_home
    assert care_cmd.install(target=target, backend="systemd", home=home, entry_ids=["care-scan"], json_output=True) == 0
    link = _wants_dir(home) / _timer_name(target, "care-scan")
    link.unlink()
    capsys.readouterr()

    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    adopted = {item["requested"]: item["using"] for item in payload.get("adopted") or []}
    assert adopted["daily-care"] == "care-scan"

    record = next(item for item in payload["enable"]["timers"] if item["name"] == _timer_name(target, "care-scan"))
    assert record["action"] == "skip"
    assert record["enabled"] is False
    assert "adopted" in record["detail"]
    assert not link.is_symlink()
    assert f"systemctl --user enable --now {_timer_name(target, 'care-scan')}" in payload["next_commands"]


def test_no_enable_writes_units_without_links(care_home, capsys):
    home, target = care_home
    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True, enable=False) == 0
    payload = json.loads(capsys.readouterr().out)

    assert not _wants_dir(home).exists()
    assert payload["enable"]["enabled"] is False
    assert all(item["action"] == "skip" for item in payload["enable"]["timers"])
    assert all(item["detail"] == "--no-enable" for item in payload["enable"]["timers"])
    enable_commands = [command for command in payload["next_commands"] if "enable --now" in command]
    assert len(enable_commands) == len(payload["enable"]["timers"])

    capsys.readouterr()
    assert care_cmd.install(target=target, backend="systemd", home=home, enable=False) == 0
    out = capsys.readouterr().out
    assert "6 timer(s) remain disabled" in out
    assert "wrote unit files only" not in out


def test_no_enable_cli_flag_writes_nothing_to_wants(care_home, monkeypatch, capsys):
    home, target = care_home
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))

    assert cli.main(["care", "install", "--target", str(target), "--backend", "systemd", "--no-enable"]) == 0
    assert not _wants_dir(home).exists()
    assert _unit_path(home, target, "daily-care", ".timer").is_file()


def test_dry_run_reports_enable_action_and_writes_nothing(care_home, capsys):
    home, target = care_home
    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True, dry_run=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["enable"]["timers"]
    assert all(item["action"] == "enable" for item in payload["enable"]["timers"])
    assert all(item["status"] == "planned" for item in payload["enable"]["timers"])
    assert not _wants_dir(home).exists()
    assert not _unit_path(home, target, "daily-care", ".timer").exists()


def test_enable_timers_skips_absent_and_hand_edited_timers(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    absent = "brigade-care-abc-daily-care.timer"
    hand_edited = "brigade-care-abc-ingest-sweep.timer"
    (unit_dir / hand_edited).write_text("[Timer]\nOnCalendar=daily\n", encoding="utf-8")

    enable = care_enable.enable_timers(
        unit_dir=unit_dir,
        results=[
            {"name": absent, "action": "create"},
            {"name": hand_edited, "action": "create"},
        ],
        identity="abc",
    )

    details = {item["name"]: item["detail"] for item in enable["timers"]}
    assert "absent" in details[absent]
    assert "not a Brigade-owned managed block" in details[hand_edited]
    assert enable["enabled"] is False
    assert not (unit_dir / care_enable.WANTS_DIRNAME).exists()


def test_status_payload_aggregate_is_false_when_an_installed_timer_is_disabled(care_home, monkeypatch):
    home, target = care_home
    monkeypatch.setattr(care_cmd, "_read_crontab", lambda: ("", None))
    _install(home, target)
    enabled = care_cmd.status_payload(target=target, home=home)
    assert enabled["enabled"] is True
    assert enabled["installed_disabled_timers"] == []

    name = _timer_name(target, "weekly-outcome-ratchet")
    (_wants_dir(home) / name).unlink()
    disabled = care_cmd.status_payload(target=target, home=home)
    assert disabled["enabled"] is False
    assert disabled["installed_disabled_timers"] == [name]


def test_schedule_health_warns_for_each_disabled_timer(care_home):
    home, target = care_home
    _install(home, target)
    name = _timer_name(target, "weekly-outcome-ratchet")
    (_wants_dir(home) / name).unlink()

    health = care_receipts.schedule_health(target, home=home, extras_on=True)

    assert [warning["code"] for warning in health["warnings"]] == [care_receipts.SCHEDULE_WARNING_TIMER_DISABLED]
    warning = health["warnings"][0]
    assert warning["entry"] == "weekly-outcome-ratchet"
    assert name in warning["detail"]
    assert warning["fix_command"] == f"systemctl --user enable --now {name}"


def test_schedule_health_warns_when_installed_timer_never_recorded_a_receipt(care_home):
    home, target = care_home
    _install(home, target, entry_ids=["daily-care"])
    timer = _unit_path(home, target, "daily-care", ".timer")
    stamp = time.time()
    old = stamp - 30 * 24 * 60 * 60
    os.utime(timer, (old, old))

    health = care_receipts.schedule_health(target, home=home, extras_on=True, now=stamp)

    codes = [warning["code"] for warning in health["warnings"]]
    assert codes == [care_receipts.SCHEDULE_WARNING_NEVER_RAN]
    warning = health["warnings"][0]
    assert warning["entry"] == "daily-care"
    assert "30 days" in warning["detail"]
    assert warning["fix_command"] == (
        f"systemctl --user start brigade-care-{care_cmd._target_identity(target)}-daily-care.service"
    )


def test_schedule_health_ignores_young_timers_and_recorded_receipts(care_home):
    home, target = care_home
    _install(home, target, entry_ids=["daily-care"])
    _write_runbook_receipt(
        target, runbook_id="daily-care-pass", status="completed", started_at="2026-11-16T06:15:00+00:00"
    )

    health = care_receipts.schedule_health(target, home=home, extras_on=True)

    assert health["warnings"] == []


def test_schedule_health_warns_when_runbook_entries_run_with_extras_off(care_home):
    home, target = care_home
    _install(home, target, entry_ids=["daily-care"])

    health = care_receipts.schedule_health(target, home=home, extras_on=False)

    assert [warning["code"] for warning in health["warnings"]] == [care_receipts.SCHEDULE_WARNING_EXTRAS_OFF]
    warning = health["warnings"][0]
    assert warning["entry"] == "daily-care"
    assert warning["fix_command"] == "brigade extras on"
    assert care_receipts.schedule_health(target, home=home, extras_on=True)["warnings"] == []


def test_work_brief_prints_schedule_health_warning(care_home, monkeypatch, capsys):
    home, target = care_home
    _init_git_repo(target)
    monkeypatch.setattr(work_cmd.helpers.shutil, "which", lambda name: f"/usr/bin/{name}")
    _install(home, target, entry_ids=["care-scan"])
    name = _timer_name(target, "care-scan")
    (_wants_dir(home) / name).unlink()
    unit_dir = _unit_dir(home)
    monkeypatch.setattr(care_cmd, "_systemd_user_dir", lambda home=None: unit_dir)

    capsys.readouterr()
    assert work_cmd.brief(target=target, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["care_schedule_health"]["count"] == 1
    assert payload["care_schedule_health"]["warnings"][0]["entry"] == "care-scan"

    assert work_cmd.brief(target=target) == 0
    out = capsys.readouterr().out
    assert f"care_schedule_warning: care-scan {care_receipts.SCHEDULE_WARNING_TIMER_DISABLED}" in out
    assert f"enable --now {name}" in out


def test_doctor_reports_schedule_health_as_warn(care_home, monkeypatch):
    home, target = care_home
    _install(home, target, entry_ids=["care-scan"])
    name = _timer_name(target, "care-scan")
    unit_dir = _unit_dir(home)

    clean = doctor._check_care_schedule_health(target)
    assert clean == [(doctor.OK, "care: schedule health", "installed timers are enabled and reporting")]

    (_wants_dir(home) / name).unlink()
    monkeypatch.setattr(care_cmd, "_systemd_user_dir", lambda home=None: unit_dir)
    level, check_name, detail = doctor._check_care_schedule_health(target)[0]
    assert level == doctor.WARN
    assert check_name == "care: schedule health (care-scan)"
    assert f"systemctl --user enable --now {name}" in detail
