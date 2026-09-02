"""Tests for Brigade worktree lifecycle (issue #1376)."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from brigade import aboyeur, cli, proc, runguard


def _git(repo: Path, *args: str) -> proc.Result:
    result = proc.run(["git", *args], cwd=repo)
    assert result.code == 0, result.stderr
    return result


def _git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test User")
    (repo / "tracked.txt").write_text("base\n")
    (repo / ".brigade").mkdir()
    (repo / ".brigade" / "roster.toml").write_text(
        """
orchestrator = "chef"

[agents.chef]
cli = "codex"
role = "plan"

[agents.coder]
cli = "codex"
role = "code"
"""
    )
    _git(repo, "add", "tracked.txt", ".brigade/roster.toml")
    _git(repo, "commit", "-m", "initial")
    return repo


def _write_successful_worktree_run(output_dir: Path, cwd: Path, *, final: str = "done") -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "run.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run.v1",
                "task": "x",
                "cwd": str(cwd),
                "status": "artifact-collection",
                "started_at": "2026-07-09T12:00:00Z",
                "artifacts": str(output_dir),
            }
        )
        + "\n"
    )
    (output_dir / "final.txt").write_text(final + "\n")


def _write_terminal_failed_worktree_run(
    output_dir: Path,
    cwd: Path,
    *,
    final: str = "provider diagnostic",
    phase: str = "dispatch",
    kind: str = "provider-error",
    detail: str = "provider inference failed",
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "run.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run.v1",
                "task": "x",
                "cwd": str(cwd),
                "status": "failed",
                "started_at": "2026-07-09T12:00:00Z",
                "finished_at": "2026-07-09T12:00:01Z",
                "duration_seconds": 1,
                "artifacts": str(output_dir),
                "error": detail,
                "failure_phase": phase,
                "failure": {
                    "phase": phase,
                    "kind": kind,
                    "detail": detail,
                },
            }
        )
        + "\n"
    )
    (output_dir / "final.txt").write_text(final + "\n")


def _checkout_path(tmp_path: Path, repo: Path, output_dir: Path) -> Path:
    return (tmp_path / "home" / ".cache" / "brigade" / "worktrees" / f"{repo.name}-{output_dir.name}").resolve()


def _age_directory(path: Path, days: int) -> None:
    old = time.time() - days * 86400
    os.utime(path, (old, old))


class TestRunWorktreeLifecycle:
    def test_success_clean_and_branch_backed_is_removed(self, tmp_path, monkeypatch, capsys):
        repo = _git_repo(tmp_path)
        output_dir = tmp_path / "run"

        def fake_run(task: str, loaded_roster: Any, **kwargs: Any) -> int:
            _write_successful_worktree_run(kwargs["output_dir"], kwargs["cwd"])
            return 0

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        monkeypatch.setattr(aboyeur, "run", fake_run)

        rc = cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree"])

        checkout = _checkout_path(tmp_path, repo, output_dir)
        err = capsys.readouterr().err
        assert rc == 0
        assert not checkout.exists()
        assert f"worktree removed: {checkout}" in err
        run_meta = json.loads((output_dir / "run.json").read_text())
        assert run_meta["worktree_removal"] == {
            "status": "removed",
            "path": str(checkout),
            "reason": "clean and branch-backed",
        }

    def test_success_dirty_is_kept(self, tmp_path, monkeypatch, capsys):
        repo = _git_repo(tmp_path)
        output_dir = tmp_path / "run"

        def fake_run(task: str, loaded_roster: Any, **kwargs: Any) -> int:
            (kwargs["cwd"] / "tracked.txt").write_text("changed in worktree\n")
            _write_successful_worktree_run(kwargs["output_dir"], kwargs["cwd"])
            return 0

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        monkeypatch.setattr(aboyeur, "run", fake_run)

        rc = cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree"])

        checkout = _checkout_path(tmp_path, repo, output_dir)
        err = capsys.readouterr().err
        assert rc == 0
        assert checkout.exists()
        assert (checkout / "tracked.txt").read_text() == "changed in worktree\n"
        assert f"worktree kept for recovery: {checkout} (dirty)" in err
        assert "worktree_removal" not in json.loads((output_dir / "run.json").read_text())

    def test_failure_is_kept(self, tmp_path, monkeypatch, capsys):
        repo = _git_repo(tmp_path)
        output_dir = tmp_path / "run"

        def fake_run(task: str, loaded_roster: Any, **kwargs: Any) -> int:
            _write_terminal_failed_worktree_run(kwargs["output_dir"], kwargs["cwd"])
            return 2

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        monkeypatch.setattr(aboyeur, "run", fake_run)

        rc = cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree"])

        checkout = _checkout_path(tmp_path, repo, output_dir)
        err = capsys.readouterr().err
        assert rc == 2
        assert checkout.exists()
        assert f"worktree kept for recovery: {checkout} (run failed)" in err

    def test_success_detached_unreachable_is_kept(self, tmp_path, monkeypatch, capsys):
        repo = _git_repo(tmp_path)
        output_dir = tmp_path / "run"

        def fake_run(task: str, loaded_roster: Any, **kwargs: Any) -> int:
            cwd = kwargs["cwd"]
            _git(cwd, "commit", "--allow-empty", "-m", "extra")
            _write_successful_worktree_run(kwargs["output_dir"], cwd)
            return 0

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        monkeypatch.setattr(aboyeur, "run", fake_run)

        rc = cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree"])

        checkout = _checkout_path(tmp_path, repo, output_dir)
        err = capsys.readouterr().err
        assert rc == 0
        assert checkout.exists()
        assert f"worktree kept for recovery: {checkout} (detached HEAD with unreachable commits)" in err
        assert "worktree_removal" not in json.loads((output_dir / "run.json").read_text())

    def test_removal_failure_keeps_worktree_and_does_not_record_removal(self, tmp_path, monkeypatch, capsys):
        repo = _git_repo(tmp_path)
        output_dir = tmp_path / "run"

        def fake_run(task: str, loaded_roster: Any, **kwargs: Any) -> int:
            _write_successful_worktree_run(kwargs["output_dir"], kwargs["cwd"])
            return 0

        def failing_remove(repo_root: Path, path: Path, *, force: bool = False) -> None:
            raise runguard.RunGuardError("simulated removal failure")

        monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
        monkeypatch.setattr(aboyeur, "run", fake_run)
        monkeypatch.setattr(runguard, "remove_worktree", failing_remove)

        rc = cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree"])

        checkout = _checkout_path(tmp_path, repo, output_dir)
        err = capsys.readouterr().err
        assert rc == 0
        assert checkout.exists()
        assert f"worktree kept for recovery: {checkout}" in err
        assert "removal failed: simulated removal failure" in err
        assert "worktree_removal" not in json.loads((output_dir / "run.json").read_text())


class TestRunsPruneWorktrees:
    def _create_brigade_worktree(self, repo: Path, root: Path, name: str) -> Path:
        path = root / f"{repo.name}-{name}"
        runguard.create_detached_worktree(repo, path)
        return path.resolve()

    def _setup(self, tmp_path, monkeypatch):
        repo = _git_repo(tmp_path)
        home = tmp_path / "home"
        root = home / ".cache" / "brigade" / "worktrees"
        root.mkdir(parents=True)
        monkeypatch.setattr(Path, "home", lambda: home)
        return repo, home, root

    def test_dry_run_classifies_worktrees(self, tmp_path, monkeypatch, capsys):
        repo, _home, root = self._setup(tmp_path, monkeypatch)

        old_clean = self._create_brigade_worktree(repo, root, "old-clean")
        _age_directory(old_clean, 15)

        young_clean = self._create_brigade_worktree(repo, root, "young-clean")
        _age_directory(young_clean, 1)

        old_dirty = self._create_brigade_worktree(repo, root, "old-dirty")
        (old_dirty / "dirt.txt").write_text("dirt")
        _age_directory(old_dirty, 15)

        non_brigade = root / "not-brigade"
        non_brigade.mkdir()

        rc = cli.main(["runs", "prune-worktrees", "--target", str(repo), "--older-than", "14"])

        out = capsys.readouterr().out
        assert rc == 0
        assert old_clean.exists()
        assert young_clean.exists()
        assert old_dirty.exists()
        assert non_brigade.exists()
        assert f"removable: {old_clean} (15" in out
        assert f"kept: {young_clean} (younger than 14 days)" in out
        assert f"kept: {old_dirty} (dirty)" in out
        assert "not-brigade" not in out

    def test_apply_removes_only_removable(self, tmp_path, monkeypatch, capsys):
        repo, _home, root = self._setup(tmp_path, monkeypatch)

        old_clean = self._create_brigade_worktree(repo, root, "old-clean")
        _age_directory(old_clean, 15)

        young_clean = self._create_brigade_worktree(repo, root, "young-clean")
        _age_directory(young_clean, 1)

        old_dirty = self._create_brigade_worktree(repo, root, "old-dirty")
        (old_dirty / "dirt.txt").write_text("dirt")
        _age_directory(old_dirty, 15)

        rc = cli.main(["runs", "prune-worktrees", "--target", str(repo), "--older-than", "14", "--apply"])

        out = capsys.readouterr().out
        assert rc == 0
        assert not old_clean.exists()
        assert young_clean.exists()
        assert old_dirty.exists()
        assert f"removed: {old_clean} (15" in out
        assert f"kept: {young_clean} (younger than 14 days)" in out
        assert f"kept: {old_dirty} (dirty)" in out

    def test_rejects_non_git_target(self, tmp_path, capsys):
        target = tmp_path / "not-a-repo"
        target.mkdir()
        rc = cli.main(["runs", "prune-worktrees", "--target", str(target)])
        assert rc == 2
        assert "not a git worktree" in capsys.readouterr().err

    def test_rejects_negative_older_than(self, tmp_path, capsys):
        target = tmp_path / "not-a-repo"
        target.mkdir()
        rc = cli.main(["runs", "prune-worktrees", "--target", str(target), "--older-than", "-1"])
        assert rc == 2
        assert "--older-than must be non-negative" in capsys.readouterr().err
