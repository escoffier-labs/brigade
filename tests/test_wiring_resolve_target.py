"""Wired-target discovery must not treat ~/.brigade as a project (#536)."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import cli
from brigade.config import Config, write_config
from brigade.selection import Selection
from brigade.wiring import resolve_wired_target


def _write_config(target: Path, harnesses: list[str]) -> None:
    write_config(
        target,
        Config(
            version=1,
            selection=Selection(depth="repo", harnesses=harnesses, owner="this-repo", includes=[]),
        ),
    )


def test_home_roster_without_config_is_not_a_wired_target(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    roster = home / ".brigade"
    roster.mkdir()
    (roster / "roster.toml").write_text("# user-level roster\n")
    plain = home / "plain"
    plain.mkdir()
    monkeypatch.setenv("HOME", str(home))

    assert resolve_wired_target(str(home), harness=None) is None
    assert resolve_wired_target(str(plain), harness=None) is None
    assert resolve_wired_target(str(plain), harness="grok") is None


def test_project_with_config_resolves_and_respects_harness(tmp_path: Path):
    repo = tmp_path / "repo"
    nested = repo / "src" / "pkg"
    nested.mkdir(parents=True)
    _write_config(repo, ["claude", "grok"])

    assert resolve_wired_target(str(nested), harness=None) == repo.resolve()
    assert resolve_wired_target(str(nested), harness="grok") == repo.resolve()
    assert resolve_wired_target(str(nested), harness="claude") == repo.resolve()
    assert resolve_wired_target(str(nested), harness="openclaw") is None


def test_home_roster_does_not_shadow_child_project(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    (home / ".brigade").mkdir(parents=True)
    (home / ".brigade" / "roster.toml").write_text("# roster\n")
    repo = home / "repos" / "app"
    repo.mkdir(parents=True)
    _write_config(repo, ["grok"])
    monkeypatch.setenv("HOME", str(home))

    assert resolve_wired_target(str(repo), harness="grok") == repo.resolve()
    assert resolve_wired_target(str(home), harness="grok") is None


def test_work_resolve_target_cli(tmp_path: Path, capsys):
    home = tmp_path / "home"
    (home / ".brigade").mkdir(parents=True)
    (home / ".brigade" / "roster.toml").write_text("# roster\n")
    repo = home / "repo"
    repo.mkdir()
    _write_config(repo, ["grok"])

    assert cli.main(["work", "resolve-target", "--cwd", str(home)]) == 1
    assert cli.main(["work", "resolve-target", "--cwd", str(repo), "--harness", "grok"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == str(repo.resolve())

    assert cli.main(["work", "resolve-target", "--cwd", str(repo), "--harness", "grok", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"ok": True, "target": str(repo.resolve())}
