"""Recovery and maintenance of leases stored beside automatic worktrees."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import runguard


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    return runguard.brigade_worktree_root() / "example-run"


def _write_dead_lock(lock, run_dir):
    lock.mkdir(parents=True)
    (lock / "pid").write_text("99999999\n")
    (lock / "owner.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run_lock.v1",
                "owner_token": "dead-owner",
                "pid": 99999999,
                "run_dir": str(run_dir.resolve()),
                "acquired_at": "2026-07-16T00:00:00+00:00",
            }
        )
    )


def test_recovery_receipt_names_checkout(checkout, tmp_path, monkeypatch):
    checkout.mkdir(parents=True)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"schema": "brigade.run.v1", "status": "dispatching"}))
    lock = runguard.worktree_lock_dir(checkout) / "run.lock"
    _write_dead_lock(lock, run_dir)
    monkeypatch.setattr(runguard, "_pid_is_active", lambda pid: False)

    assert runguard.recover_stale_run(checkout, run_dir, before_terminalize=lambda owner: None)
    receipt = json.loads((run_dir / "run.json").read_text())
    assert receipt["failure"]["lock_workspace"] == str(checkout.resolve())


@pytest.mark.parametrize("lock_name", ["run.lock", ".run.lock.retained-test.stale"])
def test_retained_lease_requires_recovery_after_checkout_removed(checkout, tmp_path, lock_name):
    run_dir = tmp_path / "run"
    _write_dead_lock(runguard.worktree_lock_dir(checkout) / lock_name, run_dir)

    assert not checkout.exists()
    assert runguard.run_recovery_status(checkout, run_dir) == "required"
    assert runguard.run_recovery_status(checkout, tmp_path / "other-run") == "cleared"
