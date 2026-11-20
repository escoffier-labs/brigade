"""`brigade init` turns on extras and installs the care schedule by default (#2589).

The suite never reaches a real scheduler: conftest points HOME and
XDG_CONFIG_HOME at per-test temp dirs and replaces ``care_enable.run_systemctl``
(the ``systemctl_calls`` fixture), so the in-process care install writes unit
files under the temp config dir and records each systemctl call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from brigade import care_cmd, care_enable, cli, extras, init_care


def _init(target: Path, *extra: str, depth: str = "repo") -> int:
    return cli.main(["init", "--target", str(target), "--depth", depth, "--harnesses", "claude", *extra])


def _unit_dir() -> Path:
    return extras.marker_path().parent.parent / "systemd" / "user"


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
    assert f"brigade care install --target {target.resolve()}" in out
    assert "care schedule skipped (--no-care)" in out


def test_dry_run_reports_the_plan_and_writes_nothing(tmp_path, systemctl_calls, capsys):
    target = tmp_path / "ws"

    assert _init(target, "--dry-run") == 0

    out = capsys.readouterr().out
    assert "[dry-run] would turn on extras and install the care schedule" in out
    assert f"brigade care install --target {target.resolve()}" in out
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
        f"run `brigade care install --target {target.resolve()}` later"
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
    assert payload["care"]["command"] == f"brigade care install --target {target.resolve()}"


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
