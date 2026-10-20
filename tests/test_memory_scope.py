"""Repo and user memory store resolution for the memory MCP server."""

from __future__ import annotations

from pathlib import Path

import pytest

from brigade import memory_scope
from tests._home import set_home


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.setenv("BRIGADE_HOME", str(home / ".brigade"))
    return home


def test_explicit_target_is_repo_store_even_when_unwired(home: Path, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    res = memory_scope.resolve(target=repo)
    assert [s.origin for s in res.stores] == ["repo", "user"]
    assert res.repo_root == repo.resolve()
    assert res.repo_wired is False
    assert res.stores[1].root == (home / ".brigade").resolve()


def test_explicit_target_with_unloadable_config_is_not_wired(home: Path, tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".brigade").mkdir(parents=True)
    (repo / ".brigade" / "config.json").write_text("{not json", encoding="utf-8")
    res = memory_scope.resolve(target=repo)
    assert res.repo_root == repo.resolve()
    assert res.repo_wired is False


def test_no_target_and_no_wired_repo_gives_user_store_only(home: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setattr(memory_scope, "resolve_wired_target", lambda cwd, harness=None: None)
    res = memory_scope.resolve(cwd=tmp_path)
    assert [s.origin for s in res.stores] == ["user"]
    assert res.repo_root is None


def test_wired_repo_is_detected_but_home_never_is(home: Path, tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(memory_scope, "resolve_wired_target", lambda cwd, harness=None: repo)
    res = memory_scope.resolve(cwd=repo)
    assert res.repo_root == repo.resolve() and res.repo_wired is True
    monkeypatch.setattr(memory_scope, "resolve_wired_target", lambda cwd, harness=None: home)
    assert memory_scope.resolve(cwd=home).repo_root is None


def test_invalid_repo_config_degrades_to_user_store(home: Path, tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / ".brigade").mkdir(parents=True)
    (repo / ".brigade" / "memory-care.toml").write_text("card_roots = 7\n")
    res = memory_scope.resolve(target=repo)
    assert [s.origin for s in res.stores] == ["user"]
    assert res.repo_config_error
    assert res.repo_root == repo.resolve()


def test_target_equal_to_user_root_is_not_duplicated(home: Path):
    res = memory_scope.resolve(target=home / ".brigade")
    assert [s.origin for s in res.stores] == ["user"]


WIRED_CONFIG = '{"version": 1, "depth": "repo", "harnesses": ["claude"], "owner": "claude"}'


def _marker(root: Path, name: str, text: str = "") -> None:
    (root / ".brigade").mkdir(parents=True, exist_ok=True)
    (root / ".brigade" / name).write_text(text)


def test_memory_care_only_repo_is_detected_without_wiring(home: Path, tmp_path: Path):
    repo = tmp_path / "work" / "repo"
    sub = repo / "src"
    sub.mkdir(parents=True)
    _marker(repo, "memory-care.toml")
    res = memory_scope.resolve(cwd=sub)
    assert res.repo_root == repo.resolve()
    assert res.repo_wired is False
    assert [s.origin for s in res.stores] == ["repo", "user"]


def test_nested_repo_stops_at_the_nearest_marker(home: Path, tmp_path: Path):
    outer = tmp_path / "outer"
    inner = outer / "inner"
    deep = inner / "pkg"
    deep.mkdir(parents=True)
    _marker(outer, "config.json", WIRED_CONFIG)
    _marker(inner, "memory-care.toml")
    res = memory_scope.resolve(cwd=deep)
    assert res.repo_root == inner.resolve()
    assert res.repo_wired is False
    wired = memory_scope.resolve(cwd=outer)
    assert wired.repo_root == outer.resolve() and wired.repo_wired is True


def test_home_directory_markers_are_skipped(home: Path):
    sub = home / "notes"
    sub.mkdir()
    _marker(home, "config.json", WIRED_CONFIG)
    _marker(home, "memory-care.toml")
    res = memory_scope.resolve(cwd=sub)
    assert res.repo_root is None
    assert [s.origin for s in res.stores] == ["user"]
