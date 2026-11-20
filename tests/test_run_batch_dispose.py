"""Disposal of batch task worktrees and recovery of interrupted collection (#2050)."""

from __future__ import annotations

import json
import os

import pytest

from tests.test_run_batch_integrate import (
    events,
    integrate,
    ref_or_none,
    run_batch,
    snapshot,
    write_files,
)
from tests.test_run_batch_worktrees import Site, _files, _git, invoke
from tests.test_run_batch_worktrees import site as worktree_site  # noqa: F401  (fixture for getfixturevalue)

from brigade import agents, cli, run_batch_artifacts, run_batch_integrate, run_events, run_lifecycle
from brigade import run_batch_dispose as dispose_module
from brigade import run_tasks
from brigade import run_batch_workspace as workspace
from brigade import runguard

DISPOSE = dispose_module
posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX worktree lease and lock layout")


@pytest.fixture(autouse=True)
def _quarantine_root(tmp_path, monkeypatch):
    root = tmp_path / "q"
    root.mkdir()
    monkeypatch.setattr(run_batch_integrate, "QUARANTINE_ROOT", root)
    return root


@pytest.fixture
def site(request):
    """The per-task worktree site from the worktree suite, unchanged."""
    return request.getfixturevalue("worktree_site")


def dispose(site: Site, capsys, *task_ids: str, confirm: bool = False, discard=(), delete_branch: bool = False):
    argv = ["runs", "tasks", "dispose", str(site.run_dir()), *task_ids]
    if confirm:
        argv.append("--confirm")
    for task_id in discard:
        argv.extend(["--discard", task_id])
    if delete_branch:
        argv.append("--delete-branch")
    argv.append("--json")
    rc = cli.main(argv)
    captured = capsys.readouterr()
    assert captured.out, captured.err
    return rc, json.loads(captured.out)


def collect(site: Site, capsys, task_id: str = "a"):
    rc = cli.main(["runs", "tasks", "collect", str(site.run_dir()), task_id, "--json"])
    captured = capsys.readouterr()
    assert captured.out, captured.err
    return rc, json.loads(captured.out)


def facts(site: Site, event_type: str):
    return [event for event in events(site) if event.event_type == event_type]


def branch_tip(site: Site, task_id: str = "a") -> str | None:
    return ref_or_none(site.repo, f"refs/heads/{workspace.branch_name(site.run_dir().name, task_id)}")


def lease_dir(site: Site, task_id: str = "a"):
    return runguard.worktree_lock_dir(site.path_for(task_id))


def sidecar_path(site: Site, task_id: str = "a"):
    return site.run_dir() / run_batch_artifacts.sidecar_name(task_id)


def task_row(payload: dict, task_id: str) -> dict:
    return next(row for row in payload["tasks"] if row["task_id"] == task_id)


def test_dispose_after_integrate_removes_the_worktree_and_keeps_the_branch(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}})
    checkout = site.path_for("a")
    lease = lease_dir(site)
    before = snapshot(site)
    rc, integrated = integrate(site, capsys, "a")
    assert rc == 0, integrated
    candidate = integrated["candidate_commits"][0]["candidate_commit"]

    assert lease.is_dir()
    rc, payload = dispose(site, capsys, "a", confirm=True)
    assert rc == 0, payload
    row = task_row(payload, "a")
    assert row["status"] == "disposed"
    assert row["mode"] == "integrated"
    assert row["branch_kept"] == "kept"
    assert row["refusal"] is None

    assert not checkout.exists()
    assert branch_tip(site) == candidate
    assert ref_or_none(site.repo, integrated["ref"]) == integrated["integration_commit"]
    # The reservation lease is released once the checkout and its lock are gone.
    assert not lease.exists()

    recorded = facts(site, "task.disposed")
    assert [fact.payload["task_id"] for fact in recorded] == ["a"]
    assert recorded[0].schema_version == run_events.WORKSPACE_SCHEMA_VERSION
    assert recorded[0].payload == {"task_id": "a", "mode": "integrated", "branch_kept": "kept"}
    assert run_tasks.read_tasks(site.run_dir())[0].disposal == run_tasks.TaskDisposal("integrated", "kept")

    # The caller checkout is byte-identical: only Brigade's own run state moved.
    assert snapshot(site) == before


def test_delete_branch_after_integrate_deletes_only_the_task_branch(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}})
    rc, integrated = integrate(site, capsys, "a")
    assert rc == 0, integrated

    rc, payload = dispose(site, capsys, "a", confirm=True, delete_branch=True)
    assert rc == 0, payload
    assert task_row(payload, "a")["branch_kept"] == "deleted"

    assert not site.path_for("a").exists()
    assert branch_tip(site) is None
    # The integration ref holds the work, so it survives; Brigade never deletes it.
    assert ref_or_none(site.repo, integrated["ref"]) == integrated["integration_commit"]
    assert facts(site, "task.disposed")[0].payload["branch_kept"] == "deleted"


def test_retained_task_disposes_and_keeps_its_branch(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}})
    candidate = branch_tip(site)
    assert candidate is not None

    rc, payload = dispose(site, capsys, "a", confirm=True)
    assert rc == 0, payload
    assert task_row(payload, "a")["mode"] == "retained"
    assert task_row(payload, "a")["branch_kept"] == "kept"
    assert not site.path_for("a").exists()
    # The retained copy is the branch, which is still there at its candidate.
    assert branch_tip(site) == candidate
    assert _git(site.repo, "cat-file", "-t", candidate) == "commit"


def test_worker_commit_after_collection_refuses_branch_moved(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}})
    checkout = site.path_for("a")
    (checkout / "late.txt").write_text("late\n")
    _git(checkout, "add", "-A")
    _git(checkout, "commit", "-q", "-m", "late")

    rc, payload = dispose(site, capsys, "a", confirm=True)
    assert rc == 2
    assert task_row(payload, "a")["refusal"]["code"] == "branch-moved"
    assert checkout.is_dir()
    assert facts(site, "task.disposed") == []


def test_ignored_file_refuses_and_discard_removes_it(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}})
    checkout = site.path_for("a")
    # Ignored files are never collected, so removal would delete the only copy.
    (checkout / ".gitignore").write_text("*.log\n")
    (checkout / "kept.log").write_text("worker log\n")

    rc, payload = dispose(site, capsys, "a", confirm=True)
    assert rc == 2
    assert task_row(payload, "a")["refusal"]["code"] == "ignored-files"
    assert (checkout / "kept.log").is_file()
    assert facts(site, "task.disposed") == []

    rc, payload = dispose(site, capsys, "a", confirm=True, discard=["a"])
    assert rc == 0, payload
    assert task_row(payload, "a")["mode"] == "discarded"
    assert not checkout.exists()
    assert facts(site, "task.disposed")[0].payload["mode"] == "discarded"


def test_no_changes_task_disposes_without_a_candidate(site, capsys, monkeypatch):
    # A worker that writes nothing leaves no candidate: the delivery is no-changes.
    monkeypatch.setattr(agents, "run_agent", write_files({"a": {}}))
    rc, result, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err
    assert result["tasks"]["a"]["artifact"]["delivery"] == "no-changes"
    assert branch_tip(site) == site.base_sha

    rc, payload = dispose(site, capsys, "a", confirm=True)
    assert rc == 0, payload
    assert task_row(payload, "a")["mode"] == "no-changes"
    assert task_row(payload, "a")["branch_kept"] == "kept"
    assert not site.path_for("a").exists()
    assert branch_tip(site) == site.base_sha


def test_dirty_worktree_without_a_retained_proof_refuses_not_retained(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}})
    checkout = site.path_for("a")
    sidecar_path(site).unlink()
    (checkout / "uncollected.txt").write_text("only copy\n")

    rc, payload = dispose(site, capsys, "a", confirm=True)
    assert rc == 2
    assert task_row(payload, "a")["refusal"]["code"] == "not-retained"
    assert (checkout / "uncollected.txt").is_file()
    assert facts(site, "task.disposed") == []


def test_dry_run_without_confirm_changes_nothing(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}})
    checkout = site.path_for("a")
    journal = run_lifecycle._journal_path(site.run_dir())
    before = journal.read_bytes()
    listing = sorted(entry.name for entry in site.run_dir().iterdir())

    rc, payload = dispose(site, capsys, "a")
    assert rc == 0, payload
    assert payload["confirm"] is False
    assert task_row(payload, "a")["status"] == "planned"
    assert task_row(payload, "a")["mode"] == "retained"
    assert checkout.is_dir()
    assert journal.read_bytes() == before
    assert sorted(entry.name for entry in site.run_dir().iterdir()) == listing
    assert facts(site, "task.disposed") == []


@posix_only
def test_live_task_lock_refuses_task_busy(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}})
    checkout = site.path_for("a")
    with runguard.run_lock(checkout, run_dir=site.run_dir()):
        rc, payload = dispose(site, capsys, "a", confirm=True)
    assert rc == 2
    assert task_row(payload, "a")["refusal"]["code"] == "task-busy"
    assert checkout.is_dir()
    assert facts(site, "task.disposed") == []


@posix_only
def test_crash_between_removal_and_the_fact_is_repaired_by_rerunning(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}})
    checkout = site.path_for("a")
    candidate = branch_tip(site)
    lease = lease_dir(site)
    assert lease.is_dir()
    # The crash window: the checkout is gone and the binding is still recorded.
    _git(site.repo, "worktree", "remove", str(checkout))
    assert not checkout.exists()

    rc, payload = dispose(site, capsys, "a", confirm=True)
    assert rc == 0, payload
    assert task_row(payload, "a")["status"] == "disposed"
    assert task_row(payload, "a")["mode"] == "retained"
    assert not lease.exists()
    assert facts(site, "task.disposed")[0].payload["mode"] == "retained"
    assert branch_tip(site) == candidate


def test_already_disposed_reports_and_writes_nothing(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}})
    rc, first = dispose(site, capsys, "a", confirm=True)
    assert rc == 0, first
    journal = run_lifecycle._journal_path(site.run_dir())
    before = journal.read_bytes()

    rc, payload = dispose(site, capsys, "a", confirm=True)
    assert rc == 0, payload
    assert task_row(payload, "a")["status"] == "already-disposed"
    assert task_row(payload, "a")["mode"] == "retained"
    assert journal.read_bytes() == before
    assert len(facts(site, "task.disposed")) == 1


def test_missing_integration_ref_falls_back_to_the_retained_proof_or_refuses(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    rc, integrated = integrate(site, capsys, "a", "b")
    assert rc == 0, integrated
    _git(site.repo, "update-ref", "-d", integrated["ref"])
    # With the integration ref gone, the artifact sidecar is the only proof.
    sidecar_path(site, "a").unlink()

    rc, payload = dispose(site, capsys, "a", "b", confirm=True)
    assert rc == 2
    assert task_row(payload, "a")["refusal"]["code"] == "not-retained"
    assert site.path_for("a").is_dir()
    assert task_row(payload, "b")["status"] == "disposed"
    assert task_row(payload, "b")["mode"] == "retained"
    assert not site.path_for("b").exists()
    # Neither the branch nor the work is gone for the refused task.
    assert branch_tip(site, "a") == integrated["candidate_commits"][0]["candidate_commit"]
    assert _files(site.path_for("a"))["alpha.txt"] == b"alpha\n"


def _crash_before_collection(monkeypatch, changes: dict[str, dict[str, str]]) -> None:
    """Make the batch die between the worker exit and its artifact collection."""

    def explode(*args, **kwargs):
        raise RuntimeError("crash between worker exit and collection")

    monkeypatch.setattr(run_batch_artifacts, "collect", explode)
    monkeypatch.setattr(agents, "run_agent", write_files(changes))


def _crash_after_collection(monkeypatch, changes: dict[str, dict[str, str]]) -> None:
    """Make the batch record a receipt-only observation: the sidecar cannot be written."""

    def explode(*args, **kwargs):
        raise OSError("artifact sidecar could not be written")

    monkeypatch.setattr(run_batch_artifacts, "write_sidecar", explode)
    monkeypatch.setattr(agents, "run_agent", write_files(changes))


def test_collect_recovers_a_crash_before_observation_and_integrate_then_succeeds(site, capsys, monkeypatch):
    real_collect = run_batch_artifacts.collect
    _crash_before_collection(monkeypatch, {"a": {"alpha.txt": "alpha\n"}})
    rc, _result, _err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    # The observation failure fails the batch's finalization, but the journal
    # keeps the attempt and the durable receipt: exactly the crash state.
    assert rc == 2
    assert facts(site, "task.observed") == []
    assert not sidecar_path(site).exists()
    monkeypatch.setattr(run_batch_artifacts, "collect", real_collect)

    rc, payload = collect(site, capsys, "a")
    assert rc == 0, payload
    assert payload["status"] == "collected"
    assert payload["delivery"] == "retained"
    assert payload["candidate_commit"] == branch_tip(site)

    observed = facts(site, "task.observed")
    assert len(observed) == 1
    assert observed[0].payload["status"] == "completed"
    assert observed[0].payload["artifact_kind"] == "commit"
    assert observed[0].payload["artifact_digest"] == payload["artifact_digest"]

    rc, integrated = integrate(site, capsys, "a")
    assert rc == 0, integrated
    assert integrated["status"] == "created"


def test_collect_recovers_a_receipt_only_observation(site, capsys, monkeypatch):
    real_write = run_batch_artifacts.write_sidecar
    _crash_after_collection(monkeypatch, {"a": {"alpha.txt": "alpha\n"}})
    rc, _result, _err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0
    monkeypatch.setattr(run_batch_artifacts, "write_sidecar", real_write)
    observed = facts(site, "task.observed")
    assert len(observed) == 1
    assert observed[0].payload["artifact_kind"] == "receipt"

    rc, payload = collect(site, capsys, "a")
    assert rc == 0, payload
    assert payload["status"] == "collected"
    assert payload["delivery"] == "retained"
    candidate = payload["candidate_commit"]
    assert candidate == branch_tip(site)

    collected = facts(site, "task.artifact-collected")
    assert len(collected) == 1
    assert collected[0].schema_version == run_events.WORKSPACE_SCHEMA_VERSION
    assert collected[0].payload == {
        "task_id": "a",
        "attempt_id": run_tasks.read_tasks(site.run_dir())[0].handle.attempt_id,
        "artifact_digest": payload["artifact_digest"],
        "candidate_commit": candidate,
        "delivery": "retained",
    }
    # The existing observation is never rewritten.
    assert len(facts(site, "task.observed")) == 1
    assert facts(site, "task.observed")[0].payload["artifact_kind"] == "receipt"

    rc, integrated = integrate(site, capsys, "a")
    assert rc == 0, integrated
    assert integrated["candidate_commits"][0]["candidate_commit"] == candidate

    # A second collect finds the recorded artifact and writes nothing.
    journal = run_lifecycle._journal_path(site.run_dir())
    before = journal.read_bytes()
    rc, again = collect(site, capsys, "a")
    assert rc == 0, again
    assert again["status"] == "already-collected"
    assert journal.read_bytes() == before


def test_collect_refuses_a_commit_artifact_whose_sidecar_is_missing(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}})
    journal = run_lifecycle._journal_path(site.run_dir())
    before = journal.read_bytes()
    sidecar_path(site).unlink()

    rc, payload = collect(site, capsys, "a")
    assert rc == 2
    assert payload["refusal"]["code"] == "artifact-mismatch"
    assert journal.read_bytes() == before


def test_collect_never_launches_a_worker(site, capsys, monkeypatch):
    real_collect = run_batch_artifacts.collect
    _crash_before_collection(monkeypatch, {"a": {"alpha.txt": "alpha\n"}})
    rc, _result, _err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 2
    monkeypatch.setattr(run_batch_artifacts, "collect", real_collect)

    def refuse_launch(*args, **kwargs):
        raise AssertionError("collect must never launch a worker")

    monkeypatch.setattr(agents, "run_agent", refuse_launch)
    rc, payload = collect(site, capsys, "a")
    assert rc == 0, payload
    assert payload["status"] == "collected"


def test_dispose_refuses_an_unknown_task_and_still_disposes_the_named_one(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}})
    rc, payload = dispose(site, capsys, "a", "missing", confirm=True)
    assert rc == 2
    assert task_row(payload, "missing")["refusal"]["code"] == "unknown-task"
    assert task_row(payload, "a")["status"] == "disposed"
    assert not site.path_for("a").exists()


def test_dispose_and_collect_reject_a_bad_discard_name(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}})
    rc, payload = dispose(site, capsys, "a", confirm=True, discard=["b"])
    assert rc == 2
    assert payload["refusal"]["code"] == "unknown-task"
    assert site.path_for("a").is_dir()
