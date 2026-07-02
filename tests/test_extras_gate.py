"""The extras wall: 18 operator-suite command groups register only when enabled.

Audit 2026-07-02, backlog item 5: the top-level surface (43 groups, 533
commands) dwarfed the core pitch. Core stays always-on; the operator suite
enables with `brigade extras on`, `BRIGADE_EXTRAS=1`, or a config marker.
"""

from __future__ import annotations

import pytest

from brigade import cli, extras


CORE_SAMPLE = ("init", "mcp", "handoff", "work", "outcome", "operator", "doctor", "run")
EXTRAS_SAMPLE = ("release", "center", "repos", "research", "friction", "chat", "learn", "pantry")


@pytest.fixture()
def extras_off(monkeypatch, tmp_path):
    monkeypatch.delenv("BRIGADE_EXTRAS", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    return tmp_path


def test_extras_disabled_by_default(extras_off):
    assert extras.enabled() is False


def test_env_var_enables_extras(extras_off, monkeypatch):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    assert extras.enabled() is True


def test_marker_file_enables_extras(extras_off):
    extras.enable()
    assert extras.marker_path().is_file()
    assert extras.enabled() is True
    extras.disable()
    assert extras.enabled() is False


def test_core_commands_register_without_extras(extras_off):
    parser = cli._build_parser()
    sub = next(a for a in parser._actions if hasattr(a, "choices") and a.choices)
    for name in CORE_SAMPLE:
        assert name in sub.choices


def test_extras_commands_stub_out_with_guidance(extras_off, capsys):
    rc = cli.main(["release", "status"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "brigade extras on" in err


def test_extras_commands_work_when_enabled(extras_off, monkeypatch, capsys):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    # a harmless read-only extras invocation proves real registration
    with pytest.raises(SystemExit) as exc:
        cli.main(["release", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "release" in out


def test_extras_cli_group_toggles(extras_off, capsys):
    assert cli.main(["extras", "status"]) == 0
    assert "disabled" in capsys.readouterr().out
    assert cli.main(["extras", "on"]) == 0
    capsys.readouterr()
    assert cli.main(["extras", "status"]) == 0
    assert "enabled" in capsys.readouterr().out
    assert cli.main(["extras", "off"]) == 0


def test_all_extras_names_are_registered_groups(extras_off, monkeypatch):
    monkeypatch.setenv("BRIGADE_EXTRAS", "1")
    parser = cli._build_parser()
    sub = next(a for a in parser._actions if hasattr(a, "choices") and a.choices)
    for name in extras.EXTRAS_COMMANDS:
        assert name in sub.choices, f"extras command {name} missing when enabled"
    for name in EXTRAS_SAMPLE:
        assert name in extras.EXTRAS_COMMANDS
