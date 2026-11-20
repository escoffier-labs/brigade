"""Explicit integration of selected batch task candidates (#2050)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_run_batch_worktrees import Site, _GIT_ENV, _files, _git, invoke
from tests.test_run_batch_worktrees import site as worktree_site  # noqa: F401  (fixture for getfixturevalue)

from brigade import agents, cli, run_batch_artifacts, run_batch_integrate, run_events, run_journal, run_lifecycle
from brigade import run_batch_workspace as workspace
from brigade import runguard

INTEGRATE = run_batch_integrate


@pytest.fixture
def site(request):
    """The per-task worktree site from the worktree suite, unchanged."""
    return request.getfixturevalue("worktree_site")


def write_files(changes: dict[str, dict[str, str]]):
    """A worker that writes the files its own task names, and touches nothing else."""

    def model(agent, prompt, **kwargs):
        for task_id, files in changes.items():
            if f"Batch task_id: {task_id}" in prompt:
                cwd = Path(kwargs["cwd"])
                for relative, text in files.items():
                    target = cwd / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(text)
                return agents.AgentResult(text="done", ok=True)
        return agents.AgentResult(text="unrelated", ok=True)

    return model


def run_batch(site: Site, capsys, monkeypatch, changes: dict[str, dict[str, str]]) -> None:
    monkeypatch.setattr(agents, "run_agent", write_files(changes))
    rc, result, err = invoke(site, capsys, [site.task(name, isolation="worktree") for name in changes])
    assert rc == 0, err
    assert all(task["artifact"]["delivery"] == "retained" for task in result["tasks"].values())


def integrate(site: Site, capsys, *task_ids: str, check: bool = False) -> tuple[int, dict]:
    argv = ["runs", "tasks", "integrate", str(site.run_dir()), *task_ids]
    if check:
        argv.append("--check")
    argv.append("--json")
    rc = cli.main(argv)
    captured = capsys.readouterr()
    assert captured.out, captured.err
    return rc, json.loads(captured.out)


def events(site: Site):
    return run_journal.read_journal_bounded(run_lifecycle._journal_path(site.run_dir())).events


def refs(repo: Path) -> set[str]:
    return set(_git(repo, "for-each-ref", "--format=%(refname)").splitlines())


def ref_or_none(repo: Path, name: str) -> str | None:
    """The commit a ref holds, or ``None`` when the ref does not exist."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", name],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **_GIT_ENV},
    )
    return result.stdout.strip() or None


def snapshot(site: Site) -> dict[str, object]:
    """Everything the command must leave byte-identical, after a status warm-up."""
    status = _git(site.repo, "status", "--porcelain")
    return {
        "files": _files(site.repo),
        "head": _git(site.repo, "rev-parse", "HEAD"),
        "branch": _git(site.repo, "symbolic-ref", "HEAD"),
        "status": status,
        "index": (site.repo / ".git" / "index").read_bytes(),
    }


def record_path(site: Site, identity: str) -> Path:
    return site.run_dir() / INTEGRATE.record_name(identity)


def identity_for(site: Site, *task_ids: str) -> str:
    return INTEGRATE.integration_id(site.run_dir().name, task_ids, site.base_sha)


def test_two_non_conflicting_tasks_integrate_into_one_commit_on_a_new_ref(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    run_dir = site.run_dir()
    before = snapshot(site)
    refs_before = refs(site.repo)

    rc, payload = integrate(site, capsys, "a", "b")
    assert rc == 0, payload
    identity = payload["integration_id"]
    ref = f"refs/heads/brigade/integrate/{identity}"
    commit = payload["integration_commit"]

    assert payload["schema"] == INTEGRATE.CLI_SCHEMA
    assert payload["status"] == "created"
    assert payload["check"] is False
    assert payload["ref"] == ref
    assert payload["base_sha"] == site.base_sha
    assert payload["task_ids"] == ["a", "b"]
    assert payload["refusal"] is None
    assert [row["task_id"] for row in payload["candidate_commits"]] == ["a", "b"]

    # One commit, parented on the pinned base, whose tree holds both tasks' changes.
    assert _git(site.repo, "rev-parse", ref) == commit
    assert _git(site.repo, "rev-parse", f"{commit}^") == site.base_sha
    assert _git(site.repo, "show", f"{commit}:alpha.txt") == "alpha"
    assert _git(site.repo, "show", f"{commit}:beta.txt") == "beta"
    assert _git(site.repo, "show", f"{commit}:README.md") == "base"
    assert _git(site.repo, "log", "-1", "--format=%s", commit) == "brigade: integrate batch req tasks a b"
    assert _git(site.repo, "log", "-1", "--format=%an <%ae>", commit) == "Brigade <brigade@localhost>"
    # The stamp comes from the pinned base, never from the clock, so this exact
    # selection always recomputes this exact commit.
    assert _git(site.repo, "log", "-1", "--format=%ct", commit) == _git(
        site.repo, "log", "-1", "--format=%ct", site.base_sha
    )

    # The caller's branch, HEAD, index and working tree are byte-identical.
    assert refs(site.repo) - refs_before == {ref}
    assert snapshot(site) == before

    # The record is written before the facts and describes exactly this selection.
    record = json.loads(record_path(site, identity).read_text())
    assert record["schema"] == INTEGRATE.RECORD_SCHEMA
    assert record["status"] == "created"
    assert record["run_id"] == run_dir.name
    assert record["request_id"] == "req"
    assert record["base_sha"] == site.base_sha
    assert record["task_ids"] == ["a", "b"]
    assert record["integration_commit"] == commit
    assert record["ref"] == ref
    assert record["candidate_commits"] == payload["candidate_commits"]

    facts = [event for event in events(site) if event.event_type == "task.integrated"]
    assert [fact.payload["task_id"] for fact in facts] == ["a", "b"]
    assert all(fact.schema_version == run_events.WORKSPACE_SCHEMA_VERSION for fact in facts)
    for fact, candidate in zip(facts, payload["candidate_commits"], strict=True):
        assert fact.payload["integration_id"] == identity
        assert fact.payload["candidate_commit"] == candidate["candidate_commit"]
        assert fact.payload["integration_commit"] == commit

    # The quarantined merge repository is gone.
    assert list(run_dir.glob(f"{INTEGRATE.QUARANTINE_PREFIX}*")) == []


def test_check_validates_the_selection_and_writes_nothing(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    run_dir = site.run_dir()
    journal = run_lifecycle._journal_path(run_dir)
    journal_before = journal.read_bytes()
    listing_before = sorted(entry.name for entry in run_dir.iterdir())
    refs_before = refs(site.repo)

    rc, payload = integrate(site, capsys, "a", "b", check=True)
    assert rc == 0, payload
    assert payload["status"] == "checked"
    assert payload["check"] is True
    assert payload["ref"] is None and payload["integration_commit"] is None
    assert payload["task_ids"] == ["a", "b"]
    assert payload["refusal"] is None

    assert sorted(entry.name for entry in run_dir.iterdir()) == listing_before
    assert journal.read_bytes() == journal_before
    assert refs(site.repo) == refs_before


def test_two_tasks_editing_the_same_lines_refuse_the_whole_integration(site, capsys, monkeypatch):
    run_batch(
        site,
        capsys,
        monkeypatch,
        {"a": {"README.md": "alpha\n"}, "b": {"README.md": "beta\n"}},
    )
    run_dir = site.run_dir()
    journal_before = run_lifecycle._journal_path(run_dir).read_bytes()
    refs_before = refs(site.repo)

    rc, payload = integrate(site, capsys, "a", "b")
    assert rc == 2
    refusal = payload["refusal"]
    assert refusal["code"] == "conflict"
    assert refusal["task_ids"] == ["a", "b"]
    assert "README.md" in refusal["paths"]
    assert "conflict" in refusal["detail"]

    assert refs(site.repo) == refs_before
    assert not list(run_dir.glob("batch-integration-*"))
    assert run_lifecycle._journal_path(run_dir).read_bytes() == journal_before
    assert list(run_dir.glob(f"{INTEGRATE.QUARANTINE_PREFIX}*")) == []


@pytest.mark.skipif(os.name != "posix", reason="the merge driver shim needs a POSIX shell")
def test_a_configured_merge_driver_and_tracked_attributes_never_run(site, capsys, monkeypatch):
    marker = site.home / "merge-driver-ran"
    (site.repo / "shared.txt").write_text("one\ntwo\nthree\n")
    (site.repo / ".gitattributes").write_text("shared.txt merge=brigade-marker\n")
    _git(site.repo, "add", "-A")
    _git(site.repo, "commit", "-q", "-m", "tracked attributes")
    site.base_sha = _git(site.repo, "rev-parse", "HEAD")
    _git(site.repo, "config", "merge.brigade-marker.driver", f"sh -c 'touch {marker}; exit 1'")

    run_batch(
        site,
        capsys,
        monkeypatch,
        {"a": {"shared.txt": "A\ntwo\nthree\n"}, "b": {"shared.txt": "one\ntwo\nB\n"}},
    )
    rc, payload = integrate(site, capsys, "a", "b")
    assert rc == 0, payload
    # The builtin text merge produced both edits, and the configured driver never ran.
    assert not marker.exists()
    assert _git(site.repo, "show", f"{payload['integration_commit']}:shared.txt") == "A\ntwo\nB"
    assert _git(site.repo, "show", f"{payload['integration_commit']}:.gitattributes") == (
        "shared.txt merge=brigade-marker"
    )

    # The driver is live in the caller repository: an unquarantined merge runs it.
    candidates = {row["task_id"]: row["candidate_commit"] for row in payload["candidate_commits"]}
    direct = subprocess.run(
        ["git", "merge-tree", "--write-tree", candidates["a"], candidates["b"]],
        cwd=site.repo,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **_GIT_ENV},
    )
    assert direct.returncode == 1
    assert marker.exists()


def test_a_moved_task_branch_refuses_candidate_moved(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    run_dir = site.run_dir()
    _git(site.repo, "update-ref", f"refs/heads/{workspace.branch_name(run_dir.name, 'a')}", site.base_sha)
    refs_before = refs(site.repo)

    rc, payload = integrate(site, capsys, "a", "b")
    assert rc == 2
    assert payload["refusal"]["code"] == "candidate-moved"
    assert payload["refusal"]["task_ids"] == ["a"]
    assert refs(site.repo) == refs_before
    assert not list(run_dir.glob("batch-integration-*"))


def test_a_changed_artifact_sidecar_refuses_artifact_mismatch(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    run_dir = site.run_dir()
    path = run_dir / run_batch_artifacts.sidecar_name("a")
    document = json.loads(path.read_text())
    document["changed_paths"] = int(document["changed_paths"]) + 1
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    before = path.read_bytes()
    refs_before = refs(site.repo)

    rc, payload = integrate(site, capsys, "a", "b")
    assert rc == 2
    assert payload["refusal"]["code"] == "artifact-mismatch"
    assert payload["refusal"]["task_ids"] == ["a"]
    assert refs(site.repo) == refs_before
    assert not list(run_dir.glob("batch-integration-*"))
    assert path.read_bytes() == before


def test_an_existing_ref_without_a_record_refuses_ref_exists(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    identity = identity_for(site, "a", "b")
    _git(site.repo, "update-ref", INTEGRATE.ref_name(identity), site.base_sha)

    rc, payload = integrate(site, capsys, "a", "b")
    assert rc == 2
    assert payload["refusal"]["code"] == "ref-exists"
    # The foreign ref is left exactly as it was, and no record was written.
    assert _git(site.repo, "rev-parse", INTEGRATE.ref_name(identity)) == site.base_sha
    assert not record_path(site, identity).exists()


def test_a_moved_ref_after_the_record_refuses_ref_conflict(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    rc, payload = integrate(site, capsys, "a", "b")
    assert rc == 0, payload
    identity = payload["integration_id"]
    record_before = record_path(site, identity).read_bytes()
    facts_before = len([e for e in events(site) if e.event_type == "task.integrated"])
    _git(site.repo, "update-ref", payload["ref"], site.base_sha)

    rc, second = integrate(site, capsys, "a", "b")
    assert rc == 2
    assert second["refusal"]["code"] == "ref-conflict"
    assert record_path(site, identity).read_bytes() == record_before
    assert len([e for e in events(site) if e.event_type == "task.integrated"]) == facts_before


def test_a_crash_after_the_ref_and_record_is_repaired_by_rerunning(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    run_dir = site.run_dir()
    real_append = INTEGRATE._append_facts

    def crash(plan, commit):
        raise INTEGRATE.IntegrationError(INTEGRATE.JOURNAL_UNREADABLE, "simulated crash before the facts")

    monkeypatch.setattr(INTEGRATE, "_append_facts", crash)
    rc, first = integrate(site, capsys, "a", "b")
    assert rc == 2
    assert first["refusal"]["code"] == "journal-unreadable"
    monkeypatch.setattr(INTEGRATE, "_append_facts", real_append)

    identity = identity_for(site, "a", "b")
    record = json.loads(record_path(site, identity).read_text())
    assert _git(site.repo, "rev-parse", record["ref"]) == record["integration_commit"]
    assert [e for e in events(site) if e.event_type == "task.integrated"] == []

    # A check never finishes the repair: it reports and writes nothing.
    journal = run_lifecycle._journal_path(site.run_dir())
    crashed_bytes = journal.read_bytes()
    rc, checked = integrate(site, capsys, "a", "b", check=True)
    assert rc == 0
    assert checked["status"] == "checked"
    assert journal.read_bytes() == crashed_bytes

    # The deterministic suffix is appended, in order, without a new commit.
    rc, repaired = integrate(site, capsys, "a", "b")
    assert rc == 0, repaired
    assert repaired["status"] == "created"
    assert repaired["integration_commit"] == record["integration_commit"]
    facts = [e for e in events(site) if e.event_type == "task.integrated"]
    assert [fact.payload["task_id"] for fact in facts] == ["a", "b"]
    assert len(facts) == 2
    assert not list(run_dir.glob(f"{INTEGRATE.QUARANTINE_PREFIX}*"))


def explode_quarantine_cleanup(monkeypatch) -> None:
    """Make only the quarantine cleanup's directory fsync fail."""
    real_rmtree = shutil.rmtree
    real_fsync = run_journal._fsync_directory
    state = {"cleanup": False}

    def rmtree(path, **kwargs):
        state["cleanup"] = True
        return real_rmtree(path, **kwargs)

    def fsync(path):
        if state["cleanup"]:
            raise run_journal.RunJournalError("simulated directory fsync failure")
        return real_fsync(path)

    monkeypatch.setattr(shutil, "rmtree", rmtree)
    monkeypatch.setattr(run_journal, "_fsync_directory", fsync)


def test_a_symlinked_batch_request_marker_is_refused_not_followed(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    marker = site.run_dir() / "batch-request.json"
    elsewhere = site.home / "elsewhere.json"
    elsewhere.write_text(marker.read_text())
    marker.unlink()
    marker.symlink_to(elsewhere)

    rc, payload = integrate(site, capsys, "a", "b")
    assert rc == 2
    # The marker is read like a sidecar: a symlink is not followed.
    assert payload["refusal"]["code"] == "unknown-run"


def test_a_failing_cleanup_fsync_never_masks_the_primary_refusal(site, capsys, monkeypatch):
    run_batch(
        site,
        capsys,
        monkeypatch,
        {"a": {"README.md": "alpha\n"}, "b": {"README.md": "beta\n"}},
    )
    explode_quarantine_cleanup(monkeypatch)

    rc, payload = integrate(site, capsys, "a", "b")
    assert rc == 2
    # The conflict is the answer; the cleanup failure is not allowed to replace it.
    assert payload["refusal"]["code"] == "conflict"


def test_a_failing_cleanup_fsync_with_nothing_else_in_flight_refuses_git_failed(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    refs_before = refs(site.repo)
    explode_quarantine_cleanup(monkeypatch)

    rc, payload = integrate(site, capsys, "a", "b", check=True)
    assert rc == 2
    assert payload["refusal"]["code"] == "git-failed"
    assert refs(site.repo) == refs_before
    assert not list(site.run_dir().glob("batch-integration-*"))
    assert not list(site.run_dir().glob(f"{INTEGRATE.QUARANTINE_PREFIX}*"))


def test_a_record_without_a_ref_is_completed_by_rerunning(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    identity = identity_for(site, "a", "b")
    ref = INTEGRATE.ref_name(identity)
    real_create = INTEGRATE._create_ref

    def crash(plan, commit):
        raise INTEGRATE.IntegrationError(INTEGRATE.GIT_FAILED, "simulated crash before the ref")

    monkeypatch.setattr(INTEGRATE, "_create_ref", crash)
    rc, first = integrate(site, capsys, "a", "b")
    assert rc == 2
    assert first["refusal"]["code"] == "git-failed"
    # The record exists first; the ref and the facts are the missing suffix.
    record = json.loads(record_path(site, identity).read_text())
    assert record["status"] == "created"
    assert record["ref"] == ref
    assert ref_or_none(site.repo, ref) is None
    assert [e for e in events(site) if e.event_type == "task.integrated"] == []

    monkeypatch.setattr(INTEGRATE, "_create_ref", real_create)
    rc, second = integrate(site, capsys, "a", "b")
    assert rc == 0, second
    assert second["integration_commit"] == record["integration_commit"]
    assert ref_or_none(site.repo, ref) == record["integration_commit"]
    facts = [e for e in events(site) if e.event_type == "task.integrated"]
    assert [fact.payload["task_id"] for fact in facts] == ["a", "b"]


def test_a_ref_without_a_record_is_recognized_as_this_integration_and_repaired(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    run_dir = site.run_dir()
    identity = identity_for(site, "a", "b")
    ref = INTEGRATE.ref_name(identity)
    real_write = INTEGRATE._write_record
    real_append = INTEGRATE._append_facts

    def crash_before_record(plan, commit):
        # The order a run that crashed between the ref and the record leaves behind.
        INTEGRATE._create_ref(plan, commit)
        raise INTEGRATE.IntegrationError(INTEGRATE.GIT_FAILED, "simulated crash before the record")

    monkeypatch.setattr(INTEGRATE, "_write_record", crash_before_record)
    monkeypatch.setattr(INTEGRATE, "_append_facts", lambda plan, commit: pytest.fail("facts before the record"))
    rc, first = integrate(site, capsys, "a", "b")
    assert rc == 2
    assert first["refusal"]["code"] == "git-failed"
    assert not record_path(site, identity).exists()
    ref_commit = ref_or_none(site.repo, ref)
    assert ref_commit is not None
    assert [e for e in events(site) if e.event_type == "task.integrated"] == []

    monkeypatch.setattr(INTEGRATE, "_write_record", real_write)
    monkeypatch.setattr(INTEGRATE, "_append_facts", real_append)
    rc, second = integrate(site, capsys, "a", "b")
    assert rc == 0, second
    # The same inputs recompute the same commit, so the ref is this integration's.
    assert second["integration_commit"] == ref_commit
    assert ref_or_none(site.repo, ref) == ref_commit
    assert json.loads(record_path(site, identity).read_text())["integration_commit"] == ref_commit
    facts = [e for e in events(site) if e.event_type == "task.integrated"]
    assert [fact.payload["task_id"] for fact in facts] == ["a", "b"]
    assert not list(run_dir.glob(f"{INTEGRATE.QUARANTINE_PREFIX}*"))


def test_rerunning_after_success_reports_already_integrated_and_writes_nothing(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    rc, payload = integrate(site, capsys, "a", "b")
    assert rc == 0, payload
    identity = payload["integration_id"]
    journal = run_lifecycle._journal_path(site.run_dir())
    journal_before = journal.read_bytes()
    record_before = record_path(site, identity).read_bytes()

    rc, second = integrate(site, capsys, "a", "b")
    assert rc == 0
    assert second["status"] == "already-integrated"
    assert second["integration_commit"] == payload["integration_commit"]
    assert second["ref"] == payload["ref"]
    assert journal.read_bytes() == journal_before
    assert record_path(site, identity).read_bytes() == record_before


def test_a_live_run_lock_refuses_target_busy(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    refs_before = refs(site.repo)
    with runguard.run_lock(site.repo, run_dir=site.run_dir()):
        rc, payload = integrate(site, capsys, "a", "b")
    assert rc == 2
    assert payload["refusal"]["code"] == "target-busy"
    assert refs(site.repo) == refs_before
    assert not list(site.run_dir().glob("batch-integration-*"))


def test_selection_order_is_recorded_and_changes_the_integration_identity(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    rc, first = integrate(site, capsys, "a", "b")
    assert rc == 0, first
    rc, second = integrate(site, capsys, "b", "a")
    assert rc == 0, second

    assert first["integration_id"] != second["integration_id"]
    assert first["task_ids"] == ["a", "b"]
    assert second["task_ids"] == ["b", "a"]
    assert first["integration_commit"] != second["integration_commit"]
    for payload in (first, second):
        record = json.loads(record_path(site, payload["integration_id"]).read_text())
        assert record["task_ids"] == payload["task_ids"]
        assert _git(site.repo, "show", f"{payload['integration_commit']}:alpha.txt") == "alpha"
        assert _git(site.repo, "show", f"{payload['integration_commit']}:beta.txt") == "beta"
    facts = [e for e in events(site) if e.event_type == "task.integrated"]
    assert [fact.payload["integration_id"] for fact in facts] == [
        first["integration_id"],
        first["integration_id"],
        second["integration_id"],
        second["integration_id"],
    ]


def test_duplicate_and_unknown_task_ids_refuse_closed_codes(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    rc, payload = integrate(site, capsys, "a", "a")
    assert rc == 2
    assert payload["refusal"]["code"] == "duplicate-task"
    rc, payload = integrate(site, capsys, "a", "missing")
    assert rc == 2
    assert payload["refusal"]["code"] == "unknown-task"


@pytest.mark.skipif(os.name != "posix", reason="hook shims need a POSIX shell")
def test_no_hook_runs_and_nothing_is_pushed_or_fetched_from_a_remote(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    marker_dir = site.home / "hooks-ran"
    marker_dir.mkdir()
    hooks = site.repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    for name in ("pre-push", "post-checkout", "post-merge", "pre-merge-commit", "reference-transaction"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\ntouch '{marker_dir}/{name}'\n")
        hook.chmod(0o755)
    # Any attempt to reach a remote would fail loudly instead of succeeding.
    _git(site.repo, "remote", "add", "origin", str(site.home / "absent-remote.git"))

    rc, payload = integrate(site, capsys, "a", "b")
    assert rc == 0, payload
    assert sorted(entry.name for entry in marker_dir.iterdir()) == []
    assert not (site.repo / ".git" / "refs" / "remotes").exists()
    pushed = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "refs/remotes/origin/main"],
        cwd=site.repo,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **_GIT_ENV},
    )
    assert pushed.returncode != 0


@pytest.mark.skipif(os.name != "posix", reason="a non-UTF-8 file name needs a POSIX filesystem")
def test_a_conflict_on_a_non_utf8_path_is_reported_as_a_conflict(site, capsys, monkeypatch):
    name = b"caf\xe9.txt"

    def model(agent, prompt, **kwargs):
        for task_id, text in (("a", "alpha\n"), ("b", "beta\n")):
            if f"Batch task_id: {task_id}" in prompt:
                with open(os.path.join(os.fsencode(kwargs["cwd"]), name), "wb") as handle:
                    handle.write(text.encode("ascii"))
                return agents.AgentResult(text="done", ok=True)
        return agents.AgentResult(text="unrelated", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    rc, result, err = invoke(
        site,
        capsys,
        [site.task("a", isolation="worktree"), site.task("b", isolation="worktree")],
    )
    assert rc == 0, err
    assert all(task["artifact"]["delivery"] == "retained" for task in result["tasks"].values())

    rc, payload = integrate(site, capsys, "a", "b")
    assert rc == 2
    refusal = payload["refusal"]
    # A path the child cannot decode is still a conflict, never a Git failure.
    assert refusal["code"] == "conflict"
    assert refusal["task_ids"] == ["a", "b"]
    assert len(refusal["paths"]) == 1
    assert refusal["paths"][0].startswith("caf")
    assert refusal["paths"][0].endswith(".txt")
    assert "\ufffd" in refusal["paths"][0]
    assert not list(site.run_dir().glob("batch-integration-*"))


@pytest.mark.skipif(os.name != "posix", reason="the ext:: and ssh shims need a POSIX shell")
def test_the_caller_git_config_cannot_run_a_transport_rewrite_or_a_hook(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    run_dir = site.run_dir()
    ssh_ran = site.home / "ssh-ran"
    ssh_shim = site.home / "ssh-shim.sh"
    ssh_shim.write_text(f"#!/bin/sh\ntouch '{ssh_ran}'\nexit 1\n")
    ssh_shim.chmod(0o755)
    fsmonitor = site.home / "fsmonitor.sh"
    fsmonitor.write_text(f"#!/bin/sh\ntouch '{site.home}/fsmonitor-ran'\n")
    fsmonitor.chmod(0o755)
    hook_dir = site.home / "hook-dir"
    hook_dir.mkdir()
    hook = hook_dir / "reference-transaction"
    hook.write_text(f"#!/bin/sh\ntouch '{site.home}/hook-ran'\n")
    hook.chmod(0o755)
    include = site.home / "hostile.include"
    include.write_text(f"[core]\n\thooksPath = {hook_dir}\n")

    # The liveness rule proves this class of rewrite runs a program at all.
    live_marker = site.home / "live"
    # The rules that would hijack the quarantine path during an object transfer.
    config = site.repo / ".git" / "config"
    config.write_text(
        config.read_text()
        + f'[url "ext::touch {site.home}/"]\n\tinsteadOf = {site.home}/\n'
        + f'[url "ssh://invalid.example/"]\n\tinsteadOf = {run_dir}/\n'
        + '[protocol "ext"]\n\tallow = always\n'
        + f"[core]\n\tsshCommand = {ssh_shim}\n\tfsmonitor = {fsmonitor}\n"
        + f'[includeIf "gitdir:{site.repo}/"]\n\tpath = {include}\n'
    )

    live = subprocess.run(
        ["git", "ls-remote", str(live_marker)],
        cwd=site.repo,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, **_GIT_ENV},
    )
    assert live.returncode != 0
    # The rewrite ran a real program, so the assertions below are about a live config.
    assert live_marker.exists()

    rc, payload = integrate(site, capsys, "a", "b")
    assert rc == 0, payload
    # No transport ran, so no rewrite, ssh command, fsmonitor or hook fired.
    assert not ssh_ran.exists()
    assert not (site.home / "fsmonitor-ran").exists()
    assert not (site.home / "hook-ran").exists()
    assert payload["ref"] is not None
    assert ref_or_none(site.repo, payload["ref"]) == payload["integration_commit"]
    assert _git(site.repo, "rev-parse", "HEAD") == site.base_sha
    assert _git(site.repo, "show", f"{payload['integration_commit']}:alpha.txt") == "alpha"
    assert _git(site.repo, "show", f"{payload['integration_commit']}:beta.txt") == "beta"


def test_human_output_names_the_ref_and_leaves_merging_to_the_caller(site, capsys, monkeypatch):
    run_batch(site, capsys, monkeypatch, {"a": {"alpha.txt": "alpha\n"}, "b": {"beta.txt": "beta\n"}})
    rc = cli.main(["runs", "tasks", "integrate", str(site.run_dir()), "a", "b", "--check"])
    assert rc == 0
    check_output = capsys.readouterr().out
    assert "merges cleanly" in check_output
    assert "wrote no ref" in check_output
    assert "git merge" not in check_output

    rc = cli.main(["runs", "tasks", "integrate", str(site.run_dir()), "a", "b"])
    assert rc == 0
    output = capsys.readouterr().out
    identity = INTEGRATE.integration_id(site.run_dir().name, ("a", "b"), site.base_sha)
    ref = INTEGRATE.ref_name(identity)
    assert f"ref: {ref}" in output
    assert f"Inspect it with: git log {ref}" in output
    assert f"Merge it yourself with: git merge {ref}" in output
    assert "Brigade did not merge, push or publish anything." in output


def test_a_task_without_a_commit_artifact_refuses(site, capsys, monkeypatch):
    monkeypatch.setattr(agents, "run_agent", write_files({"a": {"alpha.txt": "alpha\n"}}))
    rc, result, err = invoke(
        site,
        capsys,
        [site.task("a", isolation="worktree"), site.task("b", read_only=True, depends_on=["a"])],
    )
    assert rc == 0, err
    assert result["tasks"]["b"]["artifact"] is None
    rc, payload = integrate(site, capsys, "b")
    assert rc == 2
    # A shared task has a receipt artifact, never a retained candidate commit.
    assert payload["refusal"]["code"] == "artifact-mismatch"
