"""Integration tests for scripts/check_public_files.py against a temp repository."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from tests._home import set_home

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_public_files.py"

GITIGNORE = """\
/benchmarks/model-ratings*/
/docs/model-ratings*.md
!.claude/
.claude/*
!.claude/memory-handoffs/
.claude/memory-handoffs/*
!.claude/memory-handoffs/TEMPLATE.md
!.claude/memory-handoffs/.gitkeep
"""
PUBLIC = ["README.md", "src/pkg/mod.py", ".claude/memory-handoffs/TEMPLATE.md", ".claude/memory-handoffs/.gitkeep"]
PRIVATE = [
    ".claude/memory-handoffs/2026-01-01-private-session.md",
    "benchmarks/model-ratings-2026-07/results.json",
    "docs/model-ratings.md",
]


def _load():
    spec = importlib.util.spec_from_file_location("check_public_files_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, text=True, capture_output=True, check=False)


def _ignored_by_plain_git(repo: Path, *paths: str) -> set[str]:
    return set(_git(repo, "check-ignore", "--no-index", "--", *paths).stdout.split())


def _write(repo: Path, rel: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"placeholder {rel}\n")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(home / ".gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "repo"
    repo.mkdir()
    assert _git(repo, "init", "-q").returncode == 0
    (repo / ".gitignore").write_text(GITIGNORE)
    for rel in PUBLIC:
        _write(repo, rel)
    assert _git(repo, "add", "--", ".gitignore", *PUBLIC).returncode == 0
    return repo


def _run(repo: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    status = _load().main(["--root", str(repo)])
    captured = capsys.readouterr()
    return status, captured.out, captured.err


def test_ordinary_public_files_and_allowed_handoff_files_pass(repo, capsys):
    status, out, err = _run(repo, capsys)
    assert status == 0, err
    assert f"{len(PUBLIC) + 1} tracked files, none ignored" in out
    assert err == ""


def test_force_added_private_files_fail_and_are_listed_with_remediation(repo, capsys):
    for rel in PRIVATE:
        _write(repo, rel)
    assert _git(repo, "add", "-f", "--", *PRIVATE).returncode == 0
    # Prove the setup: git itself considers these ignored.
    assert _ignored_by_plain_git(repo, *PRIVATE) == set(PRIVATE)

    status, out, err = _run(repo, capsys)
    assert status == 1
    assert out == ""
    assert [line.strip() for line in err.splitlines() if line.startswith("  ") and "/" in line] == sorted(PRIVATE)
    for rel in PUBLIC:
        assert rel not in err
    assert "git rm --cached" in err
    assert "3 tracked file(s) are ignored" in err


@pytest.mark.parametrize("global_source", ["core.excludesFile", "xdg-default"])
def test_global_and_local_excludes_do_not_cause_failure(repo, capsys, tmp_path, global_source):
    home = tmp_path / "home"
    if global_source == "core.excludesFile":
        excludes = home / "gitignore_global"
        (home / ".gitconfig").write_text(f"[core]\n\texcludesFile = {excludes}\n")
    else:  # ~/.config/git/ignore applies only while core.excludesFile is unset
        excludes = home / ".config/git/ignore"
        excludes.parent.mkdir(parents=True)
    excludes.write_text("*.md\nsrc/\n")
    (repo / ".git/info/exclude").write_text(".gitignore\n")
    # Prove the setup: plain git now ignores tracked files via both machine-local sources.
    machine_local = {"README.md", "src/pkg/mod.py", ".gitignore"}
    assert _ignored_by_plain_git(repo, *machine_local) == machine_local

    status, out, err = _run(repo, capsys)
    assert status == 0, err
    assert "none ignored" in out


def test_git_errors_fail_closed(tmp_path, capsys, monkeypatch):
    module = _load()
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    assert module.main(["--root", str(not_a_repo)]) == 2
    assert "failing closed" in capsys.readouterr().err

    def _missing_git(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(module.subprocess, "run", _missing_git)
    assert module.main(["--root", str(tmp_path)]) == 2
    assert "failing closed" in capsys.readouterr().err
