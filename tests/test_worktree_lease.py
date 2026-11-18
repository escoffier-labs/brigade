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


def test_cassette_purge_locks_removed_checkout_without_recreating_it(checkout, tmp_path):
    from brigade import run_cassette

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({"lock_workspace": str(checkout)}))
    lease_dir = runguard.worktree_lock_dir(checkout)

    assert runguard.lock_path(checkout) == lease_dir / "run.lock"
    assert run_cassette.purge_cli(str(run_dir), cwd=tmp_path) == 0
    assert not checkout.exists()
    assert not lease_dir.exists()


@pytest.mark.parametrize("workspace_identity", [False, True])
def test_fleet_events_use_parent_during_start_dispatch_and_removal(checkout, tmp_path, monkeypatch, workspace_identity):
    from brigade import aboyeur, cli, fleet_client
    from tests.test_run_worktree import _git, _git_repo, _write_successful_worktree_run

    repo = _git_repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://github.com/example/project.git")
    if workspace_identity:
        (repo / ".brigade" / "node.toml").write_text("")
        _git(repo, "add", ".brigade/node.toml")
        _git(repo, "commit", "-m", "wire workspace")
    output_dir = tmp_path / "run"
    monkeypatch.setenv("BRIGADE_FLEET_HUB_URL", "https://hub.example.invalid")
    reported = []

    def report(event, *, base_path):
        reported.append((event, base_path))
        return True

    monkeypatch.setattr(fleet_client, "report_event", report)

    def emit(state, workspace):
        assert fleet_client.report_journal_event(
            {
                "run_id": "run",
                "event_type": state,
                "recorded_at": "2026-07-16T00:00:00+00:00",
                "sequence": 1,
                "event_digest": "test-digest",
            },
            workspace=workspace,
        )

    record_start = aboyeur.record_run_start

    def before_checkout(output_dir, **kwargs):
        workspace = kwargs["lock_workspace"]
        assert not (workspace / ".git").exists()
        emit("test.started", workspace)
        return record_start(output_dir, **kwargs)

    def fake_run(task, loaded_roster, *, cwd, output_dir, **kwargs):
        assert cwd.is_dir()
        emit("test.dispatched", cwd)
        _write_successful_worktree_run(output_dir, cwd)
        return 0

    record_removal = aboyeur.record_worktree_removal

    def after_removal(output_dir, *, path, reason):
        assert not path.exists()
        emit("test.removed", path)
        record_removal(output_dir, path=path, reason=reason)

    monkeypatch.setattr(aboyeur, "record_run_start", before_checkout)
    monkeypatch.setattr(aboyeur, "run", fake_run)
    monkeypatch.setattr(aboyeur, "record_worktree_removal", after_removal)
    assert (
        cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree", "--no-fleet-claim"])
        == 0
    )
    assert {"test.started", "test.dispatched", "test.removed"} <= {event["state"] for event, _ in reported}
    assert {base_path for _, base_path in reported} == {repo}
    expected_repo = "repo" if workspace_identity else "example/project"
    assert {event["repo"] for event, _ in reported} == {expected_repo}
    assert {event["repo_identity"] for event, _ in reported} == {"github.com/example/project"}
