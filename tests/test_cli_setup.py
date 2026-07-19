"""Tests for brigade setup CLI wiring."""

from __future__ import annotations

import argparse

import pytest

import brigade
from brigade import cli, component_manifest
from tests.component_install_helpers import linux_env, write_test_manifest
from tests.test_component_install import _seed_rollback_pair


def _subparsers_action(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError("no subparsers action found")


def _apply_env(monkeypatch, env: dict[str, str]) -> None:
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_setup_parser_flag_defaults():
    parser = cli._build_parser()
    ns = parser.parse_args(["setup"])
    assert ns.dry_run is False
    assert ns.offline is False
    assert ns.rollback is False


def test_setup_command_help_lists_flags(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["setup", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "usage: brigade setup" in out
    assert "--dry-run" in out
    assert "Report planned actions without writing." in out
    assert "--offline" in out
    assert "Use verified cache only; fail if missing." in out
    assert "--rollback" in out
    assert "Restore the previous installed manifest." in out


def test_top_level_help_includes_setup():
    parser = cli._build_parser()
    help_text = parser.format_help()
    assert "setup" in help_text
    assert "Install pinned native Brigade components." in help_text


def test_setup_registered_immediately_after_add():
    parser = cli._build_parser()
    sub = _subparsers_action(parser)
    choices = list(sub.choices)
    assert choices.index("setup") == choices.index("add") + 1


def test_setup_appears_once_in_command_groups():
    grouped = [name for _, names in cli.COMMAND_GROUPS for name in names]
    assert grouped.count("setup") == 1
    stations_group = next(names for title, names in cli.COMMAND_GROUPS if title == "Stations and tools")
    assert stations_group.index("setup") == stations_group.index("add") + 1


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["setup"], {"dry_run": False, "offline": False, "rollback": False}),
        (["setup", "--dry-run"], {"dry_run": True, "offline": False, "rollback": False}),
        (["setup", "--offline"], {"dry_run": False, "offline": True, "rollback": False}),
        (["setup", "--rollback"], {"dry_run": False, "offline": False, "rollback": True}),
    ],
)
def test_cli_setup_forwards_flags_to_engine(monkeypatch, argv, expected):
    captured: dict[str, bool] = {}

    def fake_setup(*, dry_run=False, offline=False, rollback=False, env=None, opener=None, runner=None):
        captured.update(dry_run=dry_run, offline=offline, rollback=rollback)
        return 0

    monkeypatch.setattr("brigade.component_install.setup_native_components", fake_setup)
    assert cli.main(argv) == 0
    assert captured == expected


def test_cli_setup_dry_run_flag(tmp_path, monkeypatch):
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    monkeypatch.setattr(component_manifest, "platform_key", lambda **_kwargs: "linux-amd64")
    _apply_env(monkeypatch, env)
    assert cli.main(["setup", "--dry-run"]) == 0


def test_cli_setup_dry_run_writes_nothing(tmp_path, monkeypatch):
    from pathlib import Path

    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    monkeypatch.setattr(component_manifest, "platform_key", lambda **_kwargs: "linux-amd64")
    _apply_env(monkeypatch, env)
    assert cli.main(["setup", "--dry-run"]) == 0
    assert not (Path(env["XDG_DATA_HOME"]) / "brigade" / "installed.json").exists()
    assert not Path(env["XDG_DATA_HOME"]).exists()


def test_cli_setup_offline_flag(tmp_path, monkeypatch):
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    monkeypatch.setattr(component_manifest, "platform_key", lambda **_kwargs: "linux-amd64")
    _apply_env(monkeypatch, env)
    assert cli.main(["setup", "--offline"]) == 1


def test_cli_setup_rollback_flag(tmp_path, monkeypatch):
    env = linux_env(tmp_path)
    _seed_rollback_pair(env, revision_a="2026-07-18", revision_b="2026-07-19")
    _apply_env(monkeypatch, env)
    assert cli.main(["setup", "--rollback"]) == 0


def test_cli_setup_propagates_engine_error(tmp_path, monkeypatch, capsys):
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version="9.9.9")
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    _apply_env(monkeypatch, env)
    assert cli.main(["setup"]) == 1
    err = capsys.readouterr().err
    assert "brigade_version mismatch" in err
