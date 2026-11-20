"""`brigade init` turns on extras and installs the care schedule by default (#2589).

The suite never reaches a real scheduler: conftest points HOME and
XDG_CONFIG_HOME at per-test temp dirs and replaces ``care_enable.run_systemctl``
(the ``systemctl_calls`` fixture), so the in-process care install writes unit
files under the temp config dir and records each systemctl call.

The backend is injected, not detected: ``_backend`` pins ``auto`` to systemd so the
same assertions hold on Linux, macOS and Windows. The Windows tests pin schtasks.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from brigade import care_cmd, care_enable, cli, extras, init_care


@pytest.fixture(autouse=True)
def _backend(monkeypatch):
    """Resolve ``auto`` to systemd on every host so these tests never depend on the OS."""
    real = care_cmd._resolve_backend
    monkeypatch.setattr(care_cmd, "_resolve_backend", lambda backend: "systemd" if backend == "auto" else real(backend))


def _init(target: Path, *extra: str, depth: str = "repo") -> int:
    return cli.main(["init", "--target", str(target), "--depth", depth, "--harnesses", "claude", *extra])


def _unit_dir() -> Path:
    return care_cmd._systemd_user_dir()


def _timers(target: Path) -> list[Path]:
    identity = care_cmd._target_identity(target.resolve())
    return sorted(_unit_dir().glob(f"brigade-care-{identity}-*.timer"))


@pytest.mark.parametrize("depth", ["repo", "workspace"])
def test_init_turns_on_extras_and_installs_care(tmp_path, systemctl_calls, capsys, depth):
    target = tmp_path / "ws"
    assert not extras.marker_path().exists()

    assert _init(target, depth=depth) == 0

    out = capsys.readouterr().out
    assert extras.marker_path().is_file()
    assert "brigade: extras enabled" in out
    assert "brigade: care schedule installed (systemd, 6 entries)" in out
    names = [timer.name for timer in _timers(target)]
    assert any(name.endswith("-weekly-outcome-ratchet.timer") for name in names)
    assert (target / ".brigade/memory-care/runbooks/weekly-outcome-ratchet.json").is_file()
    assert systemctl_calls[0] == ["daemon-reload"]
    assert [call[0] for call in systemctl_calls[1:]] == ["start"] * len(names)


def test_no_care_skips_extras_and_schedule(tmp_path, systemctl_calls, capsys):
    target = tmp_path / "ws"

    assert _init(target, "--no-care") == 0

    out = capsys.readouterr().out
    assert not extras.marker_path().exists()
    assert not _unit_dir().exists()
    assert systemctl_calls == []
    assert "care schedule skipped (--no-care)" in out
    assert "`brigade extras on`" in out
    assert f"`{init_care.care_command(target.resolve())}`" in out


def test_dry_run_reports_the_plan_and_writes_nothing(tmp_path, systemctl_calls, capsys):
    target = tmp_path / "ws"

    assert _init(target, "--dry-run") == 0

    out = capsys.readouterr().out
    assert "[dry-run] would turn on extras and install the care schedule" in out
    assert init_care.care_command(target.resolve()) in out
    assert "--no-care" in out
    assert not target.exists()
    assert not extras.marker_path().exists()
    assert not _unit_dir().exists()
    assert systemctl_calls == []


def test_second_init_is_idempotent(tmp_path, systemctl_calls, capsys):
    target = tmp_path / "ws"
    assert _init(target) == 0
    first_units = {path.name: path.read_bytes() for path in _unit_dir().iterdir() if path.is_file()}
    calls_after_first = list(systemctl_calls)
    capsys.readouterr()

    assert _init(target, "--json") == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["extras"]["status"] == "already-enabled"
    assert payload["care"]["status"] == "installed"
    assert {path.name: path.read_bytes() for path in _unit_dir().iterdir() if path.is_file()} == first_units
    assert systemctl_calls == calls_after_first, "a repeat install must not restart already linked timers"


def test_json_stdout_is_one_object_with_both_step_statuses(tmp_path, capsys):
    target = tmp_path / "ws"

    assert _init(target, "--json") == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["exit_code"] == 0
    assert payload["target"] == str(target.resolve())
    assert payload["extras"]["status"] == "enabled"
    assert payload["care"]["status"] == "installed"
    assert payload["care"]["backend"] == "systemd"
    assert "brigade: installed depth=repo" in captured.err


def test_failed_care_install_keeps_init_exit_zero_and_warns_once(tmp_path, monkeypatch, capsys):
    target = tmp_path / "ws"

    def refuse(**kwargs):
        print("error: care scheduler is unsupported on plan9", file=sys.stderr)
        return 3

    monkeypatch.setattr(care_cmd, "install", refuse)

    assert _init(target) == 0

    captured = capsys.readouterr()
    warnings = [line for line in captured.err.splitlines() if line.startswith("warning:")]
    assert warnings == [
        "warning: care schedule not installed (care scheduler is unsupported on plan9); "
        f"run `{init_care.care_command(target.resolve())}` later"
    ]
    assert (target / ".brigade/config.json").is_file(), "the core install still completed"


def test_failed_care_install_reports_status_in_json(tmp_path, monkeypatch, capsys):
    target = tmp_path / "ws"

    def explode(**kwargs):
        raise RuntimeError("no scheduler here")

    monkeypatch.setattr(care_cmd, "install", explode)

    assert _init(target, "--json") == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 0
    assert payload["care"]["status"] == "failed"
    assert "no scheduler here" in payload["care"]["reason"]
    assert payload["care"]["command"] == init_care.care_command(target.resolve())


def test_timers_that_cannot_start_warn_with_the_commands_to_run(tmp_path, monkeypatch, capsys):
    target = tmp_path / "ws"
    monkeypatch.setattr(care_enable, "run_systemctl", lambda args: (False, "Failed to connect to bus"))

    assert _init(target) == 0

    err = capsys.readouterr().err
    assert "care timers were written but systemd could not start them" in err
    assert "systemctl --user daemon-reload" in err


def test_extras_failure_is_a_warning_and_care_still_installs(tmp_path, monkeypatch, capsys):
    target = tmp_path / "ws"

    def read_only(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(extras, "enable", read_only)

    assert _init(target) == 0

    captured = capsys.readouterr()
    assert "warning: could not turn on extras (read-only file system); run `brigade extras on` later" in captured.err
    assert "brigade: care schedule installed" in captured.out


def test_a_real_init_failure_is_not_masked_and_skips_the_care_step(monkeypatch, capsys):
    def never(*args, **kwargs):
        raise AssertionError("care step must not run after a failed init")

    monkeypatch.setattr(init_care, "run", never)

    # Refused: installing straight into $HOME without --allow-home.
    assert _init(Path.home()) == 5

    err = capsys.readouterr().err
    assert "refusing to install directly into $HOME" in err
    assert "care schedule" not in err


def test_failed_init_json_reports_the_care_step_as_not_run(monkeypatch, capsys):
    assert _init(Path.home(), "--json") == 5

    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 5
    assert payload["care"]["status"] == "not-run"


def test_unsupported_windows_scheduler_reports_it_and_still_turns_on_extras(tmp_path, monkeypatch, capsys):
    target = tmp_path / "ws"
    monkeypatch.setattr(care_cmd, "_resolve_backend", lambda backend: care_cmd.SCHTASKS_BACKEND)

    def never(**kwargs):
        raise AssertionError("care install must not run where it cannot schedule")

    monkeypatch.setattr(care_cmd, "install", never)

    assert _init(target) == 0

    captured = capsys.readouterr()
    assert extras.marker_path().is_file()
    assert "brigade: care schedule: not supported on Windows yet, nothing was scheduled" in captured.out
    assert "brigade: extras enabled" in captured.out
    assert "brigade care install" not in captured.out + captured.err
    assert "warning:" not in captured.err


def test_unsupported_windows_scheduler_status_in_json(tmp_path, monkeypatch, capsys):
    target = tmp_path / "ws"
    monkeypatch.setattr(care_cmd, "_resolve_backend", lambda backend: care_cmd.SCHTASKS_BACKEND)

    assert _init(target, "--json") == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 0
    assert payload["extras"]["status"] == "enabled"
    assert payload["care"]["status"] == "unsupported"
    assert payload["care"]["backend"] == "schtasks"
    assert "command" not in payload["care"], "no command can help, so none is suggested"


def test_unstarted_timers_are_reported_as_installed_with_a_not_started_list(tmp_path, monkeypatch, capsys):
    target = tmp_path / "ws"
    monkeypatch.setattr(care_enable, "run_systemctl", lambda args: (False, "Failed to connect to bus"))

    assert _init(target, "--json") == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["care"]["status"] == "installed"
    assert payload["care"]["not_started"], "the timers that did not start are listed"
    assert any(command.startswith("systemctl --user") for command in payload["care"]["next_commands"])
    assert _timers(target), "the unit files were still written"


def test_unstarted_timers_do_not_read_as_a_clean_install_in_prose(tmp_path, monkeypatch, capsys):
    target = tmp_path / "ws"
    monkeypatch.setattr(care_enable, "run_systemctl", lambda args: (False, "Failed to connect to bus"))

    assert _init(target) == 0

    captured = capsys.readouterr()
    out = captured.out
    assert "care schedule written (systemd, 6 entries), 6 timers not started" in out
    assert "care schedule installed" not in out
    err = captured.err
    assert "systemctl --user daemon-reload" in err
    assert f"brigade care status --target {target.resolve()}" in err


def test_a_malformed_config_home_never_fails_init(tmp_path, monkeypatch, capsys):
    target = tmp_path / "ws"

    def malformed(*args, **kwargs):
        raise ValueError("embedded null byte")

    monkeypatch.setattr(extras, "enable", malformed)

    assert _init(target, "--json") == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["exit_code"] == 0
    assert payload["extras"]["status"] == "failed"
    assert "embedded null byte" in payload["extras"]["reason"]


def test_any_exception_in_the_step_becomes_a_failed_status(tmp_path, monkeypatch, capsys):
    target = tmp_path / "ws"

    def boom(*args, **kwargs):
        raise RuntimeError("marker path exploded")

    monkeypatch.setattr(extras, "marker_path", boom)
    monkeypatch.setattr(init_care, "_install_care", boom)

    assert _init(target, "--json") == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["care"]["status"] == "failed"
    assert "marker path exploded" in payload["care"]["reason"]
    assert payload["extras"]["status"] == "failed"
    assert (target / ".brigade/config.json").is_file()


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_interrupts_still_propagate_out_of_the_step(tmp_path, monkeypatch, interrupt):
    def stop(*args, **kwargs):
        raise interrupt()

    monkeypatch.setattr(extras, "enable", stop)

    with pytest.raises(interrupt):
        init_care.run(tmp_path)


def _quickstart(target: Path, *extra: str) -> int:
    return cli.main(["operator", "quickstart", "--target", str(target), "--harnesses", "codex", "--json", *extra])


def _care_step(payload: dict) -> dict:
    return next(step for step in payload["steps"] if step["id"] == "care-schedule")


def test_quickstart_installs_care_after_a_successful_install(tmp_path, systemctl_calls, capsys):
    target = tmp_path / "ws"
    target.mkdir()

    assert _quickstart(target) == 0

    payload = json.loads(capsys.readouterr().out)
    step = _care_step(payload)
    assert step["status"] == "installed"
    assert step["care"]["backend"] == "systemd"
    assert extras.marker_path().is_file()
    assert _timers(target)
    assert systemctl_calls[0] == ["daemon-reload"]


def test_quickstart_no_care_skips_both_steps_and_names_the_commands(tmp_path, systemctl_calls, capsys):
    target = tmp_path / "ws"
    target.mkdir()

    assert _quickstart(target, "--no-care") == 0

    payload = json.loads(capsys.readouterr().out)
    assert _care_step(payload)["status"] == "skipped"
    assert not extras.marker_path().exists()
    assert not _unit_dir().exists()
    assert systemctl_calls == []
    assert "brigade extras on" in payload["next_commands"]
    assert init_care.care_command(target.resolve()) in payload["next_commands"]


def test_quickstart_dry_run_plans_the_care_step_and_writes_nothing(tmp_path, systemctl_calls, capsys):
    target = tmp_path / "ws"
    target.mkdir()

    assert _quickstart(target, "--dry-run") == 0

    payload = json.loads(capsys.readouterr().out)
    assert _care_step(payload)["status"] == "planned"
    assert not extras.marker_path().exists()
    assert not _unit_dir().exists()
    assert systemctl_calls == []


def test_quickstart_fails_open_when_care_cannot_install(tmp_path, monkeypatch, capsys):
    target = tmp_path / "ws"
    target.mkdir()

    def explode(**kwargs):
        raise RuntimeError("no scheduler here")

    monkeypatch.setattr(care_cmd, "install", explode)

    assert _quickstart(target) == 0

    payload = json.loads(capsys.readouterr().out)
    step = _care_step(payload)
    assert step["status"] == "failed"
    assert "no scheduler here" in step["care"]["reason"]
    assert init_care.care_command(target.resolve()) in payload["next_commands"]
    assert (target / ".brigade/config.json").is_file()


def test_quickstart_reports_unsupported_on_windows(tmp_path, monkeypatch, capsys):
    target = tmp_path / "ws"
    target.mkdir()
    monkeypatch.setattr(care_cmd, "_resolve_backend", lambda backend: care_cmd.SCHTASKS_BACKEND)

    assert _quickstart(target) == 0

    payload = json.loads(capsys.readouterr().out)
    step = _care_step(payload)
    assert step["status"] == "unsupported"
    assert "nothing was scheduled" in step["note"]
    assert not any("brigade care install" in command for command in payload["next_commands"])


def test_quickstart_skips_the_care_step_when_the_install_fails(tmp_path, monkeypatch, capsys):
    from brigade.operator_cmd import lifecycle

    target = tmp_path / "ws"
    target.mkdir()

    def never(*args, **kwargs):
        raise AssertionError("care step must not run after a failed install")

    monkeypatch.setattr(lifecycle, "install_selection", lambda **kwargs: 5)
    monkeypatch.setattr(init_care, "run", never)

    assert _quickstart(target) != 0

    payload = json.loads(capsys.readouterr().out)
    assert all(step["id"] != "care-schedule" for step in payload["steps"])


def test_care_command_quotes_for_the_windows_shell(monkeypatch) -> None:
    """A plain Windows path prints as is, a spaced one in double quotes, and an unsafe one is withheld."""
    from brigade import shell_quote

    monkeypatch.setattr(shell_quote, "is_windows", lambda: True)
    assert init_care.care_command(Path(r"D:\work\ws")) == r"brigade care install --target D:\work\ws"
    assert init_care.care_command(Path(r"D:\my work\ws")) == r'brigade care install --target "D:\my work\ws"'
    assert init_care.care_command(Path(r"D:\a&b\ws")) == "brigade care install --target <target>"
