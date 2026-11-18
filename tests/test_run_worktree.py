"""Tests for Brigade worktree lifecycle (issue #1376)."""

from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock
from typing import Any

import pytest

from brigade import (
    aboyeur,
    agent_request,
    agents,
    cli,
    doctor,
    fleet_client,
    fleet_home_guard,
    handoff_worktrees,
    proc,
    runguard,
    worktree_lease,
)
from brigade.fleet_claim_target import ClaimTargetError
from brigade.roster import load_roster


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


@pytest.mark.parametrize("inside_repo", [False, True])
def test_worktree_run_rejects_parent_file_edit(tmp_path, monkeypatch, capsys, inside_repo):
    repo = _git_repo(tmp_path)
    output_dir = (repo if inside_repo else tmp_path) / "run"
    checkout = _checkout_path(tmp_path, repo, output_dir)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    def fake_run_agent(cli_ref, prompt, *, cwd, **kwargs):
        assert cwd == checkout
        (repo / "tracked.txt").write_text("edited outside assigned worktree\n")
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", fake_run_agent)
    rc = cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worker", "coder", "--worktree"])

    assert rc == 2
    assert "isolation breach: modified files outside assigned worktree: tracked.txt" in capsys.readouterr().err
    receipt = json.loads((output_dir / "run.json").read_text())
    assert receipt["status"] == "failed"
    assert receipt["failure"]["kind"] == "isolation-breach"


@pytest.mark.parametrize(
    ("mutation", "worktree", "failure_kind", "error"),
    [
        ("upstream", True, None, None),
        ("upstream_ancestor", True, None, None),
        ("upstream_then_edit", True, "isolation-breach", "modified files outside assigned worktree: tracked.txt"),
        ("local_commit", True, "branch-head-drift", "git HEAD drifted during run"),
        ("branch_switch", True, "branch-head-drift", "git branch drifted during run"),
        ("upstream", False, "branch-head-drift", "git HEAD drifted during run"),
    ],
)
def test_run_checks_parent_head_movement(tmp_path, monkeypatch, capsys, mutation, worktree, failure_kind, error):
    repo = _git_repo(tmp_path)
    # A real remote-tracking ref, with two commits so a partial fast-forward
    # can also be distinguished from one that reaches the upstream tip.
    upstream = tmp_path / "upstream"
    _git(tmp_path, "clone", str(repo), str(upstream))
    branch = _git(repo, "branch", "--show-current").stdout.strip()
    tracking_ref = f"origin/{branch}"
    for content in ("first upstream change\n", "second upstream change\n"):
        (upstream / "tracked.txt").write_text(content)
        _git(
            upstream,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Test User",
            "commit",
            "-am",
            content.strip(),
        )
    _git(repo, "remote", "add", "origin", str(upstream))
    _git(repo, "fetch", "origin")
    _git(repo, "branch", "--set-upstream-to", tracking_ref)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    output_dir = repo / "run"
    expected_cwd = _checkout_path(tmp_path, repo, output_dir) if worktree else repo

    def fake_run_agent(cli_ref, prompt, *, cwd, **kwargs):
        assert cwd == expected_cwd
        if mutation == "local_commit":
            _git(repo, "commit", "--allow-empty", "-m", "local change")
        elif mutation == "branch_switch":
            _git(repo, "switch", "-c", "other")
        else:
            target = f"{tracking_ref}^" if mutation == "upstream_ancestor" else tracking_ref
            _git(repo, "merge", "--ff-only", target)
            if mutation == "upstream_then_edit":
                (repo / "tracked.txt").write_text("edited after fast-forward\n")
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", fake_run_agent)
    argv = ["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worker", "coder"]
    rc = cli.main([*argv, "--worktree"] if worktree else argv)
    receipt = json.loads((output_dir / "run.json").read_text())
    stderr = capsys.readouterr().err
    if failure_kind is None:
        assert rc == 0, stderr
        assert receipt["status"] == "ok"
    else:
        assert rc == 2, stderr
        assert error in stderr
        assert receipt["status"] == "failed"
        assert receipt["failure"]["kind"] == failure_kind


@pytest.mark.parametrize("isolated", [True, False])
def test_sibling_runs_lock_only_their_checkout(tmp_path, monkeypatch, isolated):
    repo = _git_repo(tmp_path)
    _git(repo, "remote", "add", "origin", "https://github.com/example/project.git")
    (repo / ".brigade" / "node.toml").write_text("# Workspace identity marker\n")
    (tmp_path / ".brigade").mkdir()
    (tmp_path / ".brigade" / "node.toml").write_text("# Enclosing identity marker\n")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    entered, finish = Event(), Event()
    claims: set[str] = set()
    claim_guard = Lock()

    @contextmanager
    def claim(target, *, lock_owner, **kwargs):
        with claim_guard:
            assert target not in claims
            assert lock_owner["run_dir"]
            claims.add(target)
        try:
            yield
        finally:
            with claim_guard:
                claims.remove(target)

    def fake_run(task, loaded_roster, *, cwd, output_dir, **kwargs):
        meta = json.loads((output_dir / "run.json").read_text())
        assert runguard.resolve_run_lock_workspace(meta, output_dir) == cwd
        assert runguard.is_active_run_owner(cwd, output_dir)
        aboyeur.record_run_start(output_dir, task=task, cwd=cwd, roster=loaded_roster, read_only=False, worker="coder")
        if task == "first":
            assert doctor._recovery_checkpoint_run_verdict(repo, output_dir) == ("ok", "live owner")
            if isolated:
                (cwd / ".brigade" / "node.toml").write_text("# Identity written during the run\n")
                assert fleet_client.resolve_claim_target(cwd) == f"example/project@{cwd.name}"
                (cwd / ".brigade" / "node.toml").unlink()
            entered.set()
            assert finish.wait(30)
        elif isolated:
            first_cwd = _checkout_path(tmp_path, repo, tmp_path / "first")
            assert runguard.is_active_run_owner(first_cwd, tmp_path / "first")
            assert runguard.lock_path(first_cwd) != runguard.lock_path(cwd)
            with claim_guard:
                assert f"example/project@{first_cwd.name}" in claims
                expected = f"example/project@{cwd.name}" if cwd != repo else repo.name
                assert expected in claims
        if cwd != repo:
            meta = json.loads((output_dir / "run.json").read_text())
            aboyeur._write_json(output_dir / "run.json", {**meta, "status": "artifact-collection"})
        return 0

    monkeypatch.setattr(fleet_client, "repo_claim", claim)
    monkeypatch.setattr(aboyeur, "run", fake_run)

    def launch(task, *, worktree):
        argv = ["run", task, "--cwd", str(repo), "--output-dir", str(tmp_path / task), "--worker", "coder"]
        return cli.main([*argv, "--worktree"] if worktree else argv)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(launch, "first", worktree=isolated)
        try:
            assert entered.wait(10), first.result(timeout=1)
            if isolated:
                first_checkout = _checkout_path(tmp_path, repo, tmp_path / "first")
                assert not runguard.lock_path(repo).exists()
                assert cli.main(["runs", "prune-worktrees", "--target", str(repo), "--older-than", "0", "--apply"]) == 0
                assert first_checkout.is_dir()
                assert launch("second", worktree=True) == 0
                assert launch("in-place", worktree=False) == 0
            else:
                assert launch("second", worktree=False) == 2
        finally:
            finish.set()
        assert first.result(timeout=10) == 0
    assert not runguard.lock_path(repo).exists()
    if isolated:
        for name in ("first", "second"):
            output_dir = tmp_path / name
            checkout = _checkout_path(tmp_path, repo, output_dir)
            assert not checkout.exists()
            assert not runguard.lock_path(checkout).exists()
            assert (output_dir / "changes.patch").read_text() == ""
            assert doctor._recovery_checkpoint_run_verdict(repo, output_dir) == (
                "ok",
                "projected snapshot matches run.json",
            )


def test_failed_worktree_creation_does_not_take_a_lock_or_leave_a_checkout(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    output_dir = tmp_path / "run"
    checkout = _checkout_path(tmp_path, repo, output_dir)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    create = runguard.create_detached_worktree
    before = _git(repo, "worktree", "list", "--porcelain").stdout

    def fail_after_add(repo, path):
        assert not runguard.lock_path(repo).exists()
        assert not runguard.lock_path(checkout).exists()
        create(repo, path)
        raise runguard.RunGuardError("simulated checkout hook failure")

    monkeypatch.setattr(runguard, "create_detached_worktree", fail_after_add)
    assert cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree"]) == 2
    assert not checkout.exists()
    assert not runguard.lock_path(repo).exists()
    assert not runguard.worktree_lock_dir(checkout).exists()
    assert _git(repo, "worktree", "list", "--porcelain").stdout == before
    assert "simulated checkout hook failure" in (output_dir / "run.json").read_text()


@pytest.mark.parametrize(
    "module, method, error, extra_args",
    [
        (fleet_client, "repo_claim", fleet_client.FleetClaimHeldError, []),
        (fleet_client, "repo_claim", fleet_client.FleetClaimQuotaError, []),
        (fleet_client, "repo_claim", fleet_client.FleetClaimAuthError, []),
        (fleet_client, "resolve_claim_target", ClaimTargetError, []),
        (fleet_client, "resolve_claim_target", fleet_home_guard.RelativeBrigadeHomeError, ["--require-fleet-claim"]),
        (agent_request, "record_request", agent_request.AgentRequestError, ["--requester-key", "test-key"]),
        (runguard, "run_lock", runguard.RunLockError, []),
    ],
)
def test_startup_refusal_discards_checkout(tmp_path, monkeypatch, module, method, error, extra_args):
    repo = _git_repo(tmp_path)
    output_dir = tmp_path / "run"
    checkout = _checkout_path(tmp_path, repo, output_dir)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    def fail(*args, **kwargs):
        assert (checkout / ".git").is_file()
        raise error("simulated startup refusal")

    monkeypatch.setattr(module, method, fail)
    assert cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree", *extra_args]) == 2
    assert not checkout.exists()
    assert not runguard.worktree_lock_dir(checkout).exists()
    assert str(checkout) not in _git(repo, "worktree", "list", "--porcelain").stdout


def test_requester_signing_failure_releases_worktree_claim(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    output_dir = tmp_path / "run"
    checkout = _checkout_path(tmp_path, repo, output_dir)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda **kw: {"hub_url": "https://hub.example.invalid"})
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda base_path: "11111111-1111-4111-8111-111111111111")
    monkeypatch.setattr(fleet_client, "report_journal_event", lambda *args, **kwargs: False)
    active = set()

    def acquire(target, *, holder, **kwargs):
        active.add((target, holder))
        return fleet_client.ClaimDecision(True, "granted")

    def release(target, *, holder, **kwargs):
        active.remove((target, holder))
        return fleet_client.ClaimDecision(True, "released")

    def fail_signing(**kwargs):
        assert len(active) == 1
        raise agent_request.AgentRequestError("simulated signing failure")

    monkeypatch.setattr(fleet_client, "acquire_claim", acquire)
    monkeypatch.setattr(fleet_client, "release_claim", release)
    monkeypatch.setattr(agent_request, "record_request", fail_signing)
    rc = cli.main(
        ["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree", "--requester-key", "test-key"]
    )
    assert rc == 2
    assert not active
    assert not checkout.exists()
    assert not runguard.worktree_lock_dir(checkout).exists()


def test_interrupt_during_handoff_rescue_keeps_checkout(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path)
    output_dir = tmp_path / "run"
    checkout = _checkout_path(tmp_path, repo, output_dir)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    def fake_run(task, loaded_roster, *, cwd, output_dir, **kwargs):
        _write_successful_worktree_run(output_dir, cwd)
        return 0

    def interrupted_rescue(source, dest, *, dest_fd):
        # A pending handoff appeared after the clean check, before removal.
        handoff = source / ".claude" / "memory-handoffs" / "pending.md"
        handoff.parent.mkdir(parents=True)
        handoff.write_text("undelivered")
        raise KeyboardInterrupt

    monkeypatch.setattr(aboyeur, "run", fake_run)
    monkeypatch.setattr(handoff_worktrees, "rescue_pending_handoffs", interrupted_rescue)
    assert cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree"]) == 130
    assert (checkout / ".claude" / "memory-handoffs" / "pending.md").read_text() == "undelivered"
    assert f"worktree kept for recovery: {checkout}" in capsys.readouterr().err


def test_startup_cleanup_oserror_preserves_exit_code(tmp_path, monkeypatch, capsys):
    repo = _git_repo(tmp_path)
    output_dir = tmp_path / "run"
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    def fail_creation(repo, path):
        raise runguard.RunGuardError("checkout setup failed")

    def fail_cleanup(path, **kwargs):
        raise PermissionError("checkout is busy")

    monkeypatch.setattr(runguard, "create_detached_worktree", fail_creation)
    monkeypatch.setattr(worktree_lease.shutil, "rmtree", fail_cleanup)
    assert cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree"]) == 2
    assert "checkout is busy" in capsys.readouterr().err


@pytest.mark.parametrize("dispatched", [False, True])
def test_rmtree_fallback_prunes_git_registration(tmp_path, monkeypatch, dispatched):
    repo = _git_repo(tmp_path)
    checkout = tmp_path / "checkout"
    runguard.create_detached_worktree(repo, checkout)
    run = proc.run

    def refuse_git_remove(argv, **kwargs):
        if argv[:3] == ["git", "worktree", "remove"]:
            return proc.Result(1, "", "simulated Git removal refusal")
        return run(argv, **kwargs)

    monkeypatch.setattr(proc, "run", refuse_git_remove)
    remove = runguard.remove_worktree if dispatched else runguard.discard_incomplete_worktree
    remove(repo, checkout)
    assert not checkout.exists()
    assert str(checkout) not in _git(repo, "worktree", "list", "--porcelain").stdout


def test_removal_receipt_failure_retains_the_worktree_lock(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    output_dir = tmp_path / "run"
    checkout = _checkout_path(tmp_path, repo, output_dir)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")

    def fake_run(task, loaded_roster, *, cwd, output_dir, **kwargs):
        aboyeur.record_run_start(output_dir, task=task, cwd=cwd, roster=loaded_roster, read_only=False)
        meta = json.loads((output_dir / "run.json").read_text())
        aboyeur._write_json(output_dir / "run.json", {**meta, "status": "artifact-collection"})
        return 0

    def fail_removal_receipt(output_dir, *, path, reason):
        assert not path.exists()
        assert runguard.is_active_run_owner(path, output_dir)
        raise runguard.RetainRunLockError("simulated removal receipt failure")

    monkeypatch.setattr(aboyeur, "run", fake_run)
    monkeypatch.setattr(aboyeur, "record_worktree_removal", fail_removal_receipt)
    assert cli.main(["run", "x", "--cwd", str(repo), "--output-dir", str(output_dir), "--worktree"]) == 2
    assert not checkout.exists()
    assert not runguard.lock_path(repo).exists()
    assert runguard.run_lock_state(checkout, output_dir) == "live"


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

    @pytest.mark.parametrize("owner_state", ["live", "dead", "retained"])
    def test_prune_uses_external_lease_liveness(self, tmp_path, monkeypatch, capsys, owner_state):
        repo, _home, root = self._setup(tmp_path, monkeypatch)
        checkout = self._create_brigade_worktree(repo, root, "run")
        _age_directory(checkout, 15)
        output_dir = tmp_path / "run"
        roster = load_roster(repo / ".brigade" / "roster.toml")
        lease = runguard.worktree_lock_dir(checkout)
        lease.mkdir(parents=True)
        with pytest.raises(runguard.RetainRunLockError):
            with runguard.run_lock(checkout, run_dir=output_dir):
                aboyeur.record_run_start(output_dir, task="x", cwd=checkout, roster=roster, read_only=False)
                raise runguard.RetainRunLockError("simulated interrupted run")
        if owner_state == "retained":
            (lease / "run.lock").rename(lease / ".run.lock.retained-test.stale")
        elif owner_state == "dead":
            owner = json.loads((lease / "run.lock" / "owner.json").read_text())
            owner["pid"] = 99999999
            (lease / "run.lock" / "owner.json").write_text(json.dumps(owner))
            (lease / "run.lock" / "pid").write_text("99999999\n")

        assert cli.main(["runs", "prune-worktrees", "--target", str(repo), "--apply"]) == 0
        if owner_state == "dead":
            assert not checkout.exists()
            assert not lease.exists()
            receipt = json.loads((output_dir / "run.json").read_text())
            assert receipt["failure"]["lock_workspace"] == str(checkout)
        else:
            assert checkout.exists()
            assert lease.exists()
            assert "run lock is present" in capsys.readouterr().out

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
