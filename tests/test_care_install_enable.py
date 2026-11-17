"""Timer enablement and schedule-health tests for ``brigade care install`` (#2589)."""

from __future__ import annotations

import json
import os
import sys
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
    # Callers that pass no home (doctor, work brief) must still inspect the tmp home.
    monkeypatch.setattr(care_cmd, "_systemd_user_dir", lambda h=None: _unit_dir(home if h is None else h))
    monkeypatch.setattr(
        care_cmd, "_launchd_dir", lambda h=None: (home if h is None else h) / "Library" / "LaunchAgents"
    )
    monkeypatch.setattr(care_receipts, "_crontab_spool_text", lambda: None)
    return home, target


# Backdating a symlink's own mtime and parsing systemd calendars are POSIX-only;
# the systemd backend never runs on Windows.
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="systemd timer behavior is POSIX-only")


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
    assert all(item["started"] is True and item["start_error"] is None for item in payload["enable"]["timers"])


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


@posix_only
def test_schedule_health_warns_when_installed_timer_never_recorded_a_receipt(care_home):
    home, target = care_home
    _install(home, target, entry_ids=["daily-care"])
    timer = _unit_path(home, target, "daily-care", ".timer")
    stamp = time.time()
    old = stamp - 30 * 24 * 60 * 60
    os.utime(timer, (old, old))
    os.utime(_wants_dir(home) / timer.name, (old, old), follow_symlinks=False)

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

    capsys.readouterr()
    assert work_cmd.brief(target=target, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["care_schedule_health"]["count"] == 1
    assert payload["care_schedule_health"]["warnings"][0]["entry"] == "care-scan"

    assert work_cmd.brief(target=target) == 0
    out = capsys.readouterr().out
    assert f"care_schedule_warning: care-scan {care_receipts.SCHEDULE_WARNING_TIMER_DISABLED}" in out
    assert f"enable --now {name}" in out


def test_doctor_reports_schedule_health_as_warn(care_home):
    home, target = care_home
    _install(home, target, entry_ids=["care-scan"])
    name = _timer_name(target, "care-scan")

    clean = doctor._check_care_schedule_health(target)
    assert clean == [
        (doctor.OK, "care: schedule health", "1 installed schedule(s) enabled and reporting (crontab not inspected)")
    ]

    (_wants_dir(home) / name).unlink()
    level, check_name, detail = doctor._check_care_schedule_health(target)[0]
    assert level == doctor.WARN
    assert check_name == "care: schedule health (care-scan)"
    assert f"systemctl --user enable --now {name}" in detail


def test_doctor_is_neutral_when_no_care_is_installed(care_home):
    _, target = care_home

    [(level, check_name, detail)] = doctor._check_care_schedule_health(target)

    assert level == doctor.INFO
    assert check_name == "care: schedule health"
    assert detail == "no installed care schedule found (crontab not inspected)"
    assert "enabled and reporting" not in detail


# --- Activation: a new wants link needs daemon-reload and start (finding 1) ---


def test_install_reloads_then_starts_each_newly_linked_timer(care_home, systemctl_calls, capsys):
    home, target = care_home
    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    names = [item["name"] for item in payload["enable"]["timers"]]
    assert systemctl_calls == [["daemon-reload"], *[["start", name] for name in names]]
    assert payload["enable"]["daemon_reload"] == {"ok": True, "error": None}
    assert payload["enable"]["started"] == names
    assert payload["enable"]["start_failed"] == []
    assert not any("daemon-reload" in command or "start" in command for command in payload["next_commands"])


def test_install_keeps_the_link_and_prints_commands_when_systemctl_is_missing(care_home, monkeypatch, capsys):
    home, target = care_home
    monkeypatch.setattr(care_enable, "run_systemctl", lambda args: (False, "systemctl command not found"))

    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    name = _timer_name(target, "daily-care")
    assert (_wants_dir(home) / name).is_symlink()
    record = next(item for item in payload["enable"]["timers"] if item["name"] == name)
    assert record["enabled"] is True
    assert record["started"] is False
    assert "daemon-reload failed" in record["start_error"]
    assert payload["enable"]["daemon_reload"] == {"ok": False, "error": "systemctl command not found"}
    assert payload["next_commands"][0] == "systemctl --user daemon-reload"
    assert f"systemctl --user start {name}" in payload["next_commands"]

    capsys.readouterr()
    assert care_cmd.install(target=target, backend="systemd", home=home, entry_ids=["care-scan"]) == 0
    assert "could not be started now" in capsys.readouterr().out


def test_install_reports_a_failed_start_per_timer(care_home, monkeypatch, capsys):
    home, target = care_home
    bad = _timer_name(target, "nightly-ops")

    def fake(args):
        if args[0] == "start" and args[1] == bad:
            return False, "Unit not found."
        return True, ""

    monkeypatch.setattr(care_enable, "run_systemctl", fake)

    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    by_name = {item["name"]: item for item in payload["enable"]["timers"]}
    assert by_name[bad]["started"] is False
    assert by_name[bad]["start_error"] == "Unit not found."
    assert payload["enable"]["start_failed"] == [bad]
    assert by_name[_timer_name(target, "daily-care")]["started"] is True
    assert f"systemctl --user start {bad}" in payload["next_commands"]
    assert "systemctl --user daemon-reload" not in payload["next_commands"]


def test_dry_run_no_enable_and_reinstall_never_call_systemctl(care_home, systemctl_calls):
    home, target = care_home
    assert care_cmd.install(target=target, backend="systemd", home=home, dry_run=True) == 0
    assert care_cmd.install(target=target, backend="systemd", home=home, enable=False) == 0
    assert systemctl_calls == []

    _install(home, target, entry_ids=["care-scan"])
    systemctl_calls.clear()
    _install(home, target, entry_ids=["care-scan"])
    assert systemctl_calls == []


def test_systemctl_runner_spawns_user_systemctl_and_reports_failures(monkeypatch):
    seen: list[list[str]] = []

    class Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def run(argv, **kwargs):
        seen.append(argv)
        return Done()

    monkeypatch.setattr(care_enable.subprocess, "run", run)
    assert care_enable._systemctl_subprocess(["start", "x.timer"]) == (True, "")
    assert seen == [["systemctl", "--user", "start", "x.timer"]]

    Done.returncode = 5
    Done.stderr = "Unit x.timer not found.\n"
    assert care_enable._systemctl_subprocess(["start", "x.timer"]) == (False, "Unit x.timer not found.")

    def missing(argv, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(care_enable.subprocess, "run", missing)
    assert care_enable._systemctl_subprocess(["daemon-reload"]) == (False, "systemctl command not found")


# --- Uninstall removes the link it created and stops the timer (finding 2) ---


def test_uninstall_removes_the_wants_link_and_stops_the_timer(care_home, systemctl_calls, capsys):
    home, target = care_home
    _install(home, target, entry_ids=["care-scan"])
    name = _timer_name(target, "care-scan")
    link = _wants_dir(home) / name
    assert link.is_symlink()
    systemctl_calls.clear()
    capsys.readouterr()

    assert (
        care_cmd.uninstall(target=target, backend="systemd", home=home, entry_ids=["care-scan"], json_output=True) == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert not link.is_symlink()
    assert payload["enable_links"]["removed"] == [name]
    assert payload["enable_links"]["stopped"] == [name]
    assert ["stop", name] in systemctl_calls

    # A later --no-enable install must not silently re-enable the timer.
    _install(home, target, entry_ids=["care-scan"], enable=False)
    assert not link.is_symlink()


def test_uninstall_dry_run_reports_the_link_and_changes_nothing(care_home, systemctl_calls, capsys):
    home, target = care_home
    _install(home, target, entry_ids=["care-scan"])
    name = _timer_name(target, "care-scan")
    systemctl_calls.clear()
    capsys.readouterr()

    assert (
        care_cmd.uninstall(
            target=target, backend="systemd", home=home, entry_ids=["care-scan"], dry_run=True, json_output=True
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["enable_links"]["removed"] == [name]
    assert payload["enable_links"]["stopped"] == []
    assert (_wants_dir(home) / name).is_symlink()
    assert _unit_path(home, target, "care-scan", ".timer").is_file()
    assert systemctl_calls == []


def test_uninstall_never_removes_foreign_wants_entries(care_home, tmp_path, capsys):
    home, target = care_home
    _install(home, target, entry_ids=["care-scan"], enable=False)
    wants = _wants_dir(home)
    wants.mkdir(parents=True)
    scan = wants / _timer_name(target, "care-scan")
    other = tmp_path / "elsewhere.timer"
    other.write_text("[Timer]\n", encoding="utf-8")
    scan.symlink_to(other)
    capsys.readouterr()

    assert (
        care_cmd.uninstall(target=target, backend="systemd", home=home, entry_ids=["care-scan"], json_output=True) == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert scan.is_symlink() and scan.resolve() == other.resolve()
    assert payload["enable_links"]["kept"] == [scan.name]
    assert payload["enable_links"]["removed"] == []

    scan.unlink()
    scan.write_text("foreign file\n", encoding="utf-8")
    _install(home, target, entry_ids=["care-scan"], enable=False)
    assert care_cmd.uninstall(target=target, backend="systemd", home=home, entry_ids=["care-scan"]) == 0
    assert scan.read_text(encoding="utf-8") == "foreign file\n"


def test_uninstall_clears_a_dangling_link_left_by_an_earlier_uninstall(care_home, capsys):
    home, target = care_home
    name = _timer_name(target, "care-scan")
    wants = _wants_dir(home)
    wants.mkdir(parents=True)
    (wants / name).symlink_to(f"../{name}")
    assert not _unit_path(home, target, "care-scan", ".timer").exists()

    assert (
        care_cmd.uninstall(target=target, backend="systemd", home=home, entry_ids=["care-scan"], json_output=True) == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert not (wants / name).is_symlink()
    assert payload["enable_links"]["removed"] == [name]


# --- Already-linked timers are reported as enabled in every branch (finding 3) ---


def test_no_enable_reinstall_reports_an_existing_link_as_enabled(care_home, capsys):
    home, target = care_home
    _install(home, target, entry_ids=["care-scan"])
    name = _timer_name(target, "care-scan")
    capsys.readouterr()

    assert (
        care_cmd.install(
            target=target, backend="systemd", home=home, entry_ids=["care-scan"], enable=False, json_output=True
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)

    record = next(item for item in payload["enable"]["timers"] if item["name"] == name)
    assert record["status"] == "already"
    assert record["enabled"] is True
    assert "--no-enable did not remove" in record["detail"]
    assert (_wants_dir(home) / name).is_symlink()
    assert not any("enable --now" in command for command in payload["next_commands"])

    capsys.readouterr()
    assert care_cmd.install(target=target, backend="systemd", home=home, entry_ids=["care-scan"], enable=False) == 0
    assert "remain disabled" not in capsys.readouterr().out


def test_blocked_and_adopted_installs_still_report_an_existing_link_as_enabled(care_home, capsys):
    home, target = care_home
    _install(home, target, entry_ids=["care-scan"])
    scan = _timer_name(target, "care-scan")
    capsys.readouterr()

    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True) == 0
    adopted = next(item for item in json.loads(capsys.readouterr().out)["enable"]["timers"] if item["name"] == scan)
    assert adopted["status"] == "already"
    assert adopted["enabled"] is True
    assert "adopted" in adopted["detail"]

    timer = _unit_path(home, target, "care-scan", ".timer")
    timer.write_text(
        timer.read_text(encoding="utf-8").replace("memory care scan timer", "hand-edited"), encoding="utf-8"
    )
    capsys.readouterr()
    assert care_cmd.install(target=target, backend="systemd", home=home, entry_ids=["care-scan"], json_output=True) == 1
    blocked = next(item for item in json.loads(capsys.readouterr().out)["enable"]["timers"] if item["name"] == scan)
    assert blocked["enabled"] is True
    assert "blocked" in blocked["detail"]


# --- A symlinked timers.target.wants is never written through (finding 4) ---


def test_symlinked_wants_dir_is_a_conflict_and_nothing_is_written(care_home, tmp_path, capsys):
    home, target = care_home
    outside = tmp_path / "outside"
    outside.mkdir()
    _unit_dir(home).mkdir(parents=True)
    _wants_dir(home).symlink_to(outside, target_is_directory=True)

    assert care_cmd.install(target=target, backend="systemd", home=home, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)

    assert list(outside.iterdir()) == []
    assert payload["enable"]["enabled"] is False
    assert len(payload["enable"]["conflicts"]) == len(payload["enable"]["timers"])
    assert all("symlink" in item["detail"] for item in payload["enable"]["timers"])
    assert payload["enable"]["started"] == []


def test_uninstall_does_not_follow_a_symlinked_wants_dir(care_home, tmp_path, capsys):
    home, target = care_home
    _install(home, target, entry_ids=["care-scan"], enable=False)
    name = _timer_name(target, "care-scan")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / name).symlink_to(f"../{name}")
    _wants_dir(home).symlink_to(outside, target_is_directory=True)
    capsys.readouterr()

    assert (
        care_cmd.uninstall(target=target, backend="systemd", home=home, entry_ids=["care-scan"], json_output=True) == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert (outside / name).is_symlink()
    assert "symlink" in payload["enable_links"]["conflict"]
    assert payload["enable_links"]["removed"] == []


# --- care status reports the schedule warnings (finding 5) ---


def test_care_status_json_and_text_report_schedule_warnings(care_home, capsys):
    home, target = care_home
    _install(home, target, entry_ids=["care-scan"])
    name = _timer_name(target, "care-scan")
    (_wants_dir(home) / name).unlink()
    capsys.readouterr()

    assert care_cmd.status(target=target, backend="systemd", home=home, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schedule_health"]["count"] == 1
    assert payload["schedule_health"]["warnings"][0]["code"] == care_receipts.SCHEDULE_WARNING_TIMER_DISABLED

    assert care_cmd.status(target=target, backend="systemd", home=home) == 0
    out = capsys.readouterr().out
    assert f"schedule_warning: care-scan timer-disabled: timer {name} is installed but not enabled" in out
    assert f"run `systemctl --user enable --now {name}`" in out


def test_care_status_has_no_warnings_for_a_healthy_install(care_home, capsys):
    home, target = care_home
    _install(home, target, entry_ids=["care-scan"])
    capsys.readouterr()

    assert care_cmd.status(target=target, backend="systemd", home=home, json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["schedule_health"]["warnings"] == []


# --- Crontab and launchd workspaces get schedule warnings (finding 6) ---


def test_crontab_workspace_is_inspected_from_the_spool_file(care_home, monkeypatch):
    home, target = care_home
    block = care_cmd.render_crontab_block(workspace=target, home=home)
    monkeypatch.setattr(care_receipts, "_crontab_spool_text", lambda: block)

    health = care_receipts.schedule_health(target, home=home, extras_on=False)

    assert health["not_inspected"] == []
    assert health["installed"] >= 1
    assert health["warnings"]
    assert {warning["code"] for warning in health["warnings"]} == {care_receipts.SCHEDULE_WARNING_EXTRAS_OFF}
    assert care_receipts.schedule_health(target, home=home, extras_on=True)["warnings"] == []


def test_unreadable_crontab_is_reported_as_not_inspected(care_home):
    home, target = care_home

    health = care_receipts.schedule_health(target, home=home, extras_on=False)

    assert health["not_inspected"] == ["crontab"]
    assert health["installed"] == 0
    assert health["warnings"] == []


def test_launchd_workspace_warns_from_installed_plists(care_home):
    home, target = care_home
    assert care_cmd.install(target=target, backend="launchd", home=home, entry_ids=["daily-care"]) == 0
    identity = care_cmd._target_identity(target)
    plist = home / "Library" / "LaunchAgents" / f"dev.brigade.care.{identity}.daily-care.plist"
    assert plist.is_file()

    off = care_receipts.schedule_health(target, home=home, extras_on=False)
    assert [warning["code"] for warning in off["warnings"]] == [care_receipts.SCHEDULE_WARNING_EXTRAS_OFF]
    assert off["installed"] == 1

    stamp = time.time()
    old = stamp - 30 * 24 * 60 * 60
    os.utime(plist, (old, old))
    health = care_receipts.schedule_health(target, home=home, extras_on=True, now=stamp)
    assert [warning["code"] for warning in health["warnings"]] == [care_receipts.SCHEDULE_WARNING_NEVER_RAN]
    assert health["warnings"][0]["fix_command"] == f"launchctl load -w {plist}"


def test_care_status_launchd_reports_schedule_health(care_home, capsys):
    home, target = care_home
    assert care_cmd.install(target=target, backend="launchd", home=home, entry_ids=["daily-care"]) == 0
    capsys.readouterr()

    assert care_cmd.status(target=target, backend="launchd", home=home, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schedule_health"]["installed"] == 1


# --- Warning text (finding 8) ---


@posix_only
def test_never_ran_detail_formats_sub_day_ages_in_hours(care_home):
    home, target = care_home
    _install(home, target, entry_ids=["ingest-sweep"])
    timer = _unit_path(home, target, "ingest-sweep", ".timer")
    stamp = time.time()
    old = stamp - 7 * 60 * 60
    os.utime(timer, (old, old))
    os.utime(_wants_dir(home) / timer.name, (old, old), follow_symlinks=False)

    health = care_receipts.schedule_health(target, home=home, extras_on=True, now=stamp)

    [warning] = health["warnings"]
    assert warning["code"] == care_receipts.SCHEDULE_WARNING_NEVER_RAN
    assert "7 hours ago" in warning["detail"]
    assert "0 days" not in warning["detail"]


def test_never_ran_is_only_emitted_for_an_enabled_timer_with_extras_on(care_home):
    home, target = care_home
    _install(home, target, entry_ids=["daily-care"])
    timer = _unit_path(home, target, "daily-care", ".timer")
    stamp = time.time()
    old = stamp - 30 * 24 * 60 * 60
    os.utime(timer, (old, old))

    extras_off = care_receipts.schedule_health(target, home=home, extras_on=False, now=stamp)
    assert [warning["code"] for warning in extras_off["warnings"]] == [care_receipts.SCHEDULE_WARNING_EXTRAS_OFF]

    (_wants_dir(home) / timer.name).unlink()
    disabled = care_receipts.schedule_health(target, home=home, extras_on=True, now=stamp)
    assert [warning["code"] for warning in disabled["warnings"]] == [care_receipts.SCHEDULE_WARNING_TIMER_DISABLED]


# --- @claude review on 2db648849: never-ran age, rewritten timers, uninstall stop ---


@posix_only
def test_never_ran_counts_from_when_an_old_timer_was_enabled(care_home):
    home, target = care_home
    _install(home, target, entry_ids=["daily-care"], enable=False)
    timer = _unit_path(home, target, "daily-care", ".timer")
    stamp = time.time()
    old = stamp - 90 * 24 * 60 * 60
    os.utime(timer, (old, old))

    _install(home, target, entry_ids=["daily-care"])
    assert (_wants_dir(home) / timer.name).is_symlink()
    fresh = care_receipts.schedule_health(target, home=home, extras_on=True, now=stamp)
    assert fresh["warnings"] == []

    os.utime(_wants_dir(home) / timer.name, (old, old), follow_symlinks=False)
    stale = care_receipts.schedule_health(target, home=home, extras_on=True, now=stamp)
    assert [warning["code"] for warning in stale["warnings"]] == [care_receipts.SCHEDULE_WARNING_NEVER_RAN]


@posix_only
def test_reinstall_that_rewrites_a_linked_timer_reloads_and_restarts_it(care_home, systemctl_calls):
    home, target = care_home
    _install(home, target, entry_ids=["care-scan"])
    name = _timer_name(target, "care-scan")
    systemctl_calls.clear()

    _install(home, target, entry_ids=["care-scan"], schedule_specs=["care-scan=*-*-* 07:15:00"])

    assert systemctl_calls == [["daemon-reload"], ["restart", name]]


def test_uninstall_does_not_stop_a_timer_brigade_never_linked(care_home, systemctl_calls, capsys):
    home, target = care_home
    _install(home, target, entry_ids=["care-scan"], enable=False)
    systemctl_calls.clear()
    capsys.readouterr()

    assert (
        care_cmd.uninstall(target=target, backend="systemd", home=home, entry_ids=["care-scan"], json_output=True) == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert systemctl_calls == []
    assert payload["enable_links"]["errors"] == {}


def test_uninstall_treats_a_not_loaded_timer_as_stopped(care_home, monkeypatch, capsys):
    home, target = care_home
    _install(home, target, entry_ids=["care-scan"])
    name = _timer_name(target, "care-scan")
    monkeypatch.setattr(care_enable, "run_systemctl", lambda args: (False, f"Unit {name} not loaded."))
    capsys.readouterr()

    assert (
        care_cmd.uninstall(target=target, backend="systemd", home=home, entry_ids=["care-scan"], json_output=True) == 0
    )
    payload = json.loads(capsys.readouterr().out)

    assert payload["enable_links"]["stopped"] == [name]
    assert payload["enable_links"]["errors"] == {}
