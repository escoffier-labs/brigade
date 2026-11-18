"""Cleanup for directories left by interrupted automatic worktree setup."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import cli, runguard
from tests.test_run_worktree import _age_directory, _git_repo


def test_prune_only_reaps_aged_empty_setup_directories(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    root = runguard.brigade_worktree_root()
    old_checkout = root / "repo-interrupted"
    old_reservation = runguard.worktree_lock_dir(old_checkout)
    reservation_only = runguard.worktree_lock_dir(root / "repo-early-interrupt")
    fresh_reservation = runguard.worktree_lock_dir(root / "repo-starting")
    other_repo_reservation = runguard.worktree_lock_dir(root / "other-run")
    nonempty_checkout = root / "repo-nonempty"
    kept = fresh_reservation, other_repo_reservation, nonempty_checkout
    removable = old_checkout, old_reservation, reservation_only
    for path in removable + kept:
        path.mkdir(parents=True)
        if path != fresh_reservation:
            _age_directory(path, 15)
    (nonempty_checkout / "keep.txt").write_text("keep")
    _age_directory(nonempty_checkout, 15)

    args = ["runs", "prune-worktrees", "--target", str(repo), "--older-than", "0", "--json"]
    assert cli.main(args) == 0
    dry_run = json.loads(capsys.readouterr().out)
    assert {entry["path"] for entry in dry_run["worktrees"] if entry["removable"]} == set(map(str, removable))
    assert all(path.exists() for path in removable + kept)

    assert cli.main([*args, "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert set(applied["removed"]) == set(map(str, removable))
    assert not any(path.exists() for path in removable)
    assert all(path.exists() for path in kept)
