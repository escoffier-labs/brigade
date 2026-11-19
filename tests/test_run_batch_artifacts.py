"""Collection of each batch worktree task's candidate commit and patch (#2050)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

import pytest

from brigade import agents, cli, proc, run_batch_artifacts, run_journal, run_lifecycle, run_tasks
from brigade import run_batch_workspace as workspace
from tests.test_run_batch_worktrees import Site, _files, _git, invoke
from tests.test_run_batch_worktrees import site as worktree_site  # noqa: F401  (fixture for getfixturevalue)

ARTIFACT = run_batch_artifacts


@pytest.fixture
def site(request):
    """The per-task worktree site from the worktree suite, unchanged."""
    return request.getfixturevalue("worktree_site")


def edit_worker(site: Site, monkeypatch, *, ok: bool = True, task: str = "a"):
    """A worker that edits a tracked file and adds an untracked one in its checkout."""

    def model(agent, prompt, **kwargs):
        cwd = Path(kwargs["cwd"])
        if f"Batch task_id: {task}" in prompt:
            (cwd / "README.md").write_text("worker edit\n")
            (cwd / "new.txt").write_text("untracked content\n")
        return agents.AgentResult(text="done", ok=ok)

    monkeypatch.setattr(agents, "run_agent", model)


def sidecar(site: Site, task_id: str = "a") -> dict:
    path = site.run_dir() / ARTIFACT.sidecar_name(task_id)
    return json.loads(path.read_text())


def observation(site: Site, task_id: str = "a"):
    events = run_journal.read_journal_bounded(run_lifecycle._journal_path(site.run_dir())).events
    found = [event for event in events if event.event_type == "task.observed" and event.payload["task_id"] == task_id]
    assert len(found) == 1, [event.event_type for event in events]
    return found[0]


def branch_tip(site: Site, task_id: str = "a") -> str:
    run_id = site.run_dir().name
    return _git(site.repo, "rev-parse", workspace.branch_name(run_id, task_id))


def scratch_clone(site: Site, tmp_path: Path) -> Path:
    clone = tmp_path / "scratch"
    _git(tmp_path, "clone", "-q", str(site.repo), str(clone))
    return clone


def test_uncommitted_work_becomes_one_candidate_commit(site, capsys, tmp_path, monkeypatch):
    edit_worker(site, monkeypatch)
    rc, result, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err

    run_dir = site.run_dir()
    checkout = site.path_for("a")
    record = sidecar(site)
    candidate = branch_tip(site)

    assert record["schema"] == ARTIFACT.SCHEMA
    assert record["task_id"] == "a"
    assert record["attempt_id"] == run_tasks.read_tasks(run_dir)[0].handle.attempt_id
    assert record["execution"] == "completed"
    assert record["delivery"] == "retained"
    assert record["reason"] is None
    assert record["base_sha"] == site.base_sha
    assert record["branch"] == workspace.branch_name(run_dir.name, "a")
    assert record["candidate_commit"] == candidate
    assert record["patch_ref"] == ARTIFACT.patch_name("a")
    assert record["changed_paths"] == 2
    assert record["ignored_left"] == 0
    assert candidate != site.base_sha

    # One commit on the task branch, parented on the pinned base, authored by Brigade.
    assert _git(checkout, "rev-parse", "HEAD^") == site.base_sha
    assert _git(checkout, "symbolic-ref", "--short", "HEAD") == workspace.branch_name(run_dir.name, "a")
    assert _git(checkout, "log", "-1", "--format=%s") == "brigade batch task a"
    assert _git(checkout, "log", "-1", "--format=%an <%ae>") == "Brigade <brigade@localhost>"
    assert _git(checkout, "status", "--porcelain") == ""
    tracked = _git(checkout, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert "new.txt" in tracked and "README.md" in tracked

    # The bounded patch is exactly the base-to-candidate diff and applies to base.
    patch = run_dir / str(record["patch_ref"])
    assert patch.is_file()
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == record["patch_sha256"]
    assert patch.stat().st_size == record["patch_bytes"]
    clone = scratch_clone(site, tmp_path)
    _git(clone, "checkout", "-q", site.base_sha)
    _git(clone, "apply", "--check", str(patch))

    # The journal names the commit artifact by its sidecar digest.
    event = observation(site)
    assert event.payload["artifact_kind"] == "commit"
    assert (
        event.payload["artifact_digest"]
        == hashlib.sha256((run_dir / ARTIFACT.sidecar_name("a")).read_bytes()).hexdigest()
    )
    # The sanitized receipt sidecar is still written alongside it.
    assert (run_dir / f"batch-result-{hashlib.sha256(b'a').hexdigest()}.json").is_file()

    assert result["tasks"]["a"]["artifact"] == {
        "delivery": "retained",
        "candidate_commit": candidate,
        "patch_ref": record["patch_ref"],
        "patch_sha256": record["patch_sha256"],
    }


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_patch_file_is_owner_only(site, capsys, monkeypatch):
    edit_worker(site, monkeypatch)
    rc, _, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err
    patch = site.run_dir() / ARTIFACT.patch_name("a")
    assert stat.S_IMODE(patch.stat().st_mode) == 0o600


def test_a_workers_own_commit_is_the_candidate(site, capsys, monkeypatch):
    def model(agent, prompt, **kwargs):
        cwd = Path(kwargs["cwd"])
        (cwd / "README.md").write_text("worker commit\n")
        _git(cwd, "add", "-A")
        _git(cwd, "commit", "-q", "-m", "worker commit")
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    rc, result, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err
    checkout = site.path_for("a")
    record = sidecar(site)
    worker_commit = _git(checkout, "rev-parse", "HEAD")
    assert record["candidate_commit"] == worker_commit
    assert _git(checkout, "log", "-1", "--format=%s") == "worker commit"
    assert record["delivery"] == "retained"
    assert record["changed_paths"] == 1
    # Nothing was left to commit, so collection added no commit of its own.
    assert _git(checkout, "rev-list", "--count", f"{site.base_sha}..HEAD") == "1"
    assert result["tasks"]["a"]["artifact"]["candidate_commit"] == worker_commit


def test_untouched_checkout_is_no_changes(site, capsys, monkeypatch):
    monkeypatch.setattr(agents, "run_agent", lambda *a, **k: agents.AgentResult(text="done", ok=True))
    rc, result, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err
    run_dir = site.run_dir()
    record = sidecar(site)
    assert record["delivery"] == "no-changes"
    assert record["candidate_commit"] is None
    assert record["patch_ref"] is None and record["patch_sha256"] is None and record["patch_bytes"] is None
    assert not list(run_dir.glob("batch-artifact-*.patch"))
    # No candidate means the journal keeps the receipt artifact kind and the
    # receipt's own digest, never the artifact sidecar's.
    event = observation(site)
    assert event.payload["artifact_kind"] == "receipt"
    receipt = run_dir / f"batch-result-{hashlib.sha256(b'a').hexdigest()}.json"
    assert event.payload["artifact_digest"] == hashlib.sha256(receipt.read_bytes()).hexdigest()
    artifact_digest = hashlib.sha256((run_dir / ARTIFACT.sidecar_name("a")).read_bytes()).hexdigest()
    assert event.payload["artifact_digest"] != artifact_digest
    assert result["tasks"]["a"]["artifact"] == {
        "delivery": "no-changes",
        "candidate_commit": None,
        "patch_ref": None,
        "patch_sha256": None,
    }


def test_failed_worker_still_has_its_work_retained(site, capsys, monkeypatch):
    edit_worker(site, monkeypatch, ok=False)
    rc, result, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 1, err
    record = sidecar(site)
    assert record["execution"] == "failed"
    assert record["delivery"] == "retained"
    assert record["candidate_commit"] == branch_tip(site)
    assert (site.run_dir() / ARTIFACT.patch_name("a")).is_file()
    assert observation(site).payload["artifact_kind"] == "commit"
    assert result["tasks"]["a"]["status"] == "failed"
    assert result["tasks"]["a"]["artifact"]["candidate_commit"] == record["candidate_commit"]


def test_a_reset_to_an_unrelated_commit_is_base_diverged(site, capsys, monkeypatch):
    _git(site.repo, "checkout", "-q", "--orphan", "unrelated")
    _git(site.repo, "commit", "-q", "-m", "unrelated root")
    unrelated = _git(site.repo, "rev-parse", "HEAD")
    _git(site.repo, "checkout", "-q", "-f", "main")
    _git(site.repo, "reset", "-q", "--hard", site.base_sha)

    def model(agent, prompt, **kwargs):
        _git(Path(kwargs["cwd"]), "reset", "-q", "--hard", unrelated)
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    rc, result, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err
    record = sidecar(site)
    assert record["delivery"] == "base-diverged"
    assert record["candidate_commit"] == unrelated
    # A diff that does not descend from the base is not a patch of that base.
    assert record["patch_ref"] is None and record["patch_sha256"] is None
    assert not list(site.run_dir().glob("batch-artifact-*.patch"))
    assert result["tasks"]["a"]["artifact"]["delivery"] == "base-diverged"


def test_oversize_patch_keeps_only_the_commit(site, capsys, monkeypatch):
    edit_worker(site, monkeypatch)
    monkeypatch.setattr(ARTIFACT, "PATCH_MAX_BYTES", 1)
    rc, result, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err
    record = sidecar(site)
    assert record["delivery"] == "retained"
    assert record["reason"] == "patch-oversize"
    assert record["candidate_commit"] == branch_tip(site)
    assert record["patch_ref"] is None and record["patch_sha256"] is None
    assert not list(site.run_dir().glob("batch-artifact-*.patch"))
    assert observation(site).payload["artifact_kind"] == "commit"
    assert result["tasks"]["a"]["artifact"]["candidate_commit"] == record["candidate_commit"]


def test_oversize_patch_stops_the_diff_before_it_is_written(site, capsys, monkeypatch):
    cap = 64 * 1024
    monkeypatch.setattr(ARTIFACT, "PATCH_MAX_BYTES", cap)

    def model(agent, prompt, **kwargs):
        # A change several times the cap, so reading to EOF would show up.
        (Path(kwargs["cwd"]) / "big.txt").write_text("x" * (cap * 8) + "\n")
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    reads: list[int] = []
    written: list[int] = []
    real_stream = workspace.git_stream
    real_unlink = ARTIFACT._unlink_partial

    def watched_stream(*args, **kwargs):
        result = real_stream(*args, **kwargs)
        if any(part == "diff" for part in args[0]):
            reads.append(result.stdout_bytes)
            assert result.stopped
        return result

    def watched_unlink(path):
        written.append(path.stat().st_size)
        real_unlink(path)

    monkeypatch.setattr(workspace, "git_stream", watched_stream)
    monkeypatch.setattr(ARTIFACT, "_unlink_partial", watched_unlink)
    rc, _, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err
    record = sidecar(site)
    assert record["delivery"] == "retained"
    assert record["reason"] == "patch-oversize"
    assert record["candidate_commit"] == branch_tip(site)
    # Git was stopped once the next chunk would pass the cap: it delivered at
    # most one chunk past it, not the whole diff, and nothing was written past
    # the cap at all. The partial file this call created is gone.
    assert len(reads) == 1 and reads[0] <= cap + proc._READ_CHUNK_BYTES
    assert reads[0] < cap * 8
    assert written and written[0] <= cap + proc._READ_CHUNK_BYTES
    assert not list(site.run_dir().glob("batch-artifact-*.patch"))


def test_ignored_files_are_counted_and_never_committed(site, capsys, monkeypatch):
    exclude = site.repo / ".git" / "info" / "exclude"
    exclude.write_text(exclude.read_text() + "ignored.bin\n")

    def model(agent, prompt, **kwargs):
        cwd = Path(kwargs["cwd"])
        (cwd / "README.md").write_text("worker edit\n")
        (cwd / "ignored.bin").write_text("ignored payload\n")
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    rc, _, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err
    record = sidecar(site)
    assert record["ignored_left"] == 1
    assert record["changed_paths"] == 1
    checkout = site.path_for("a")
    tracked = _git(checkout, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert "ignored.bin" not in tracked
    patch = (site.run_dir() / ARTIFACT.patch_name("a")).read_text()
    assert "ignored payload" not in patch
    # The ignored file itself is left alone in the checkout.
    assert (checkout / "ignored.bin").is_file()


@pytest.mark.skipif(os.name != "posix", reason="hook and fsmonitor shims need a POSIX shell")
def test_configured_hooks_and_fsmonitor_never_run(site, capsys, monkeypatch):
    marker_dir = site.home / "collection-markers"
    marker_dir.mkdir()
    fsmonitor_log = marker_dir / "fsmonitor.log"
    hooks = site.repo / ".git" / "hooks-brigade"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch '{marker_dir}/pre-commit'\n")
    hook.chmod(0o755)
    fsmonitor = site.home / "fsmonitor.sh"
    fsmonitor.write_text(f"#!/bin/sh\necho \"$PWD\" >> '{fsmonitor_log}'\nexit 1\n")
    fsmonitor.chmod(0o755)
    _git(site.repo, "config", "core.hooksPath", str(hooks))
    _git(site.repo, "config", "core.fsmonitor", str(fsmonitor))

    edit_worker(site, monkeypatch)
    rc, _, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err
    # The candidate commit came from collection: no configured hook ran, and no
    # fsmonitor process was started in the worker's own checkout. The caller
    # workspace is outside this rule; only the task checkout is collected.
    assert sidecar(site)["delivery"] == "retained"
    assert not (marker_dir / "pre-commit").exists()
    recorded = fsmonitor_log.read_text().splitlines() if fsmonitor_log.exists() else []
    assert str(site.path_for("a").resolve()) not in {str(Path(line).resolve()) for line in recorded}


def test_collection_never_touches_the_caller_checkout(site, capsys, monkeypatch):
    before_files = _files(site.repo)
    before_head = _git(site.repo, "rev-parse", "HEAD")
    before_branches = _git(site.repo, "branch", "--list", "--format=%(refname)")
    edit_worker(site, monkeypatch)
    rc, _, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err
    assert _files(site.repo) == before_files
    assert _git(site.repo, "rev-parse", "HEAD") == before_head
    after_branches = _git(site.repo, "branch", "--list", "--format=%(refname)")
    added = set(after_branches.splitlines()) - set(before_branches.splitlines())
    assert added == {f"refs/heads/{workspace.branch_name(site.run_dir().name, 'a')}"}
    changed = [
        line for line in _git(site.repo, "status", "--porcelain").splitlines() if not line[3:].startswith(".brigade/")
    ]
    assert changed == []


def test_shared_task_keeps_no_artifact(site, capsys, monkeypatch):
    edit_worker(site, monkeypatch)
    rc, result, err = invoke(
        site,
        capsys,
        [site.task("a", isolation="worktree"), site.task("b", read_only=True, depends_on=["a"])],
    )
    assert rc == 0, err
    assert result["tasks"]["b"]["workspace"] == "shared"
    assert result["tasks"]["b"]["artifact"] is None
    assert result["tasks"]["a"]["artifact"]["delivery"] == "retained"
    assert not (site.run_dir() / ARTIFACT.sidecar_name("b")).exists()


def test_runs_tasks_show_reports_the_artifact_sidecar(site, capsys, monkeypatch):
    edit_worker(site, monkeypatch)
    rc, _, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err
    run_dir = site.run_dir()
    name = ARTIFACT.sidecar_name("a")
    digest = hashlib.sha256((run_dir / name).read_bytes()).hexdigest()

    assert cli.main(["runs", "tasks", "show", str(run_dir), "a", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["task"]["artifact_kind"] == "commit"
    assert payload["task"]["artifact_ref"] == name
    assert payload["task"]["artifact_sha256"] == digest

    assert cli.main(["runs", "tasks", "show", str(run_dir), "a"]) == 0
    output = capsys.readouterr().out
    assert f"artifact_ref: {name}" in output
    assert f"artifact_sha256: {digest}" in output


@pytest.mark.skipif(os.name != "posix", reason="the signing shim needs a POSIX shell")
def test_configured_gpg_signing_never_runs_during_collection(site, capsys, monkeypatch):
    marker = site.home / "gpg-ran"
    script = site.home / "fake-gpg.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    script.chmod(0o755)
    _git(site.repo, "config", "commit.gpgSign", "true")
    _git(site.repo, "config", "gpg.program", str(script))

    edit_worker(site, monkeypatch)
    rc, _, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err
    assert sidecar(site)["delivery"] == "retained"
    assert branch_tip(site) != site.base_sha
    assert not marker.exists()


def test_untracked_files_are_collected_despite_status_config(site, capsys, monkeypatch):
    _git(site.repo, "config", "status.showUntrackedFiles", "no")

    def model(agent, prompt, **kwargs):
        (Path(kwargs["cwd"]) / "new.txt").write_text("untracked content\n")
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    rc, _, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err
    record = sidecar(site)
    assert record["delivery"] == "retained"
    assert record["changed_paths"] == 1
    assert "new.txt" in _git(site.path_for("a"), "ls-tree", "-r", "--name-only", "HEAD")


@pytest.mark.skipif(os.name != "posix", reason="the textconv shim needs a POSIX shell")
def test_configured_textconv_never_runs_during_collection(site, capsys, monkeypatch):
    marker = site.home / "textconv-ran"
    script = site.home / "textconv.sh"
    script.write_text(f"#!/bin/sh\ntouch '{marker}'\ncat \"$1\"\n")
    script.chmod(0o755)
    _git(site.repo, "config", "diff.big.textconv", str(script))

    def model(agent, prompt, **kwargs):
        cwd = Path(kwargs["cwd"])
        (cwd / ".gitattributes").write_text("*.big diff=big\n")
        (cwd / "data.big").write_text("payload\n")
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    rc, _, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err
    assert sidecar(site)["delivery"] == "retained"
    assert not marker.exists()
    # The driver is live in this checkout: an unhardened diff runs it.
    _git(site.path_for("a"), "diff", site.base_sha, "HEAD")
    assert marker.exists()


def binding_for(site, task_id: str = "a") -> workspace.Binding:
    path = site.path_for(task_id)
    info = path.lstat()
    run_id = site.run_dir().name
    return workspace.Binding(
        task_id=task_id,
        run_id=run_id,
        base_sha=site.base_sha,
        branch=workspace.branch_name(run_id, task_id),
        path=path,
        dev=info.st_dev,
        ino=info.st_ino,
    )


def attempt_of(site, task_id: str = "a") -> str | None:
    return next(
        task.handle.attempt_id for task in run_tasks.read_tasks(site.run_dir()) if task.handle.spec.task_id == task_id
    )


def test_second_collection_reuses_the_artifact_for_the_same_attempt(site, capsys, monkeypatch):
    edit_worker(site, monkeypatch)
    rc, _, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err
    run_dir = site.run_dir()
    name = ARTIFACT.sidecar_name("a")
    before = (run_dir / name).read_bytes()
    patch = run_dir / ARTIFACT.patch_name("a")
    patch_before = patch.read_bytes()

    record = ARTIFACT.collect(binding_for(site), site.repo, run_dir, execution="completed", attempt_id=attempt_of(site))
    assert record["delivery"] == "retained"
    assert record["candidate_commit"] == branch_tip(site)
    assert json.loads(before) == record
    assert (run_dir / name).read_bytes() == before
    assert patch.read_bytes() == patch_before

    # A crash before the sidecar was recorded leaves only the patch: the same
    # diff is re-adopted instead of refusing with patch-exists.
    (run_dir / name).unlink()
    replayed = ARTIFACT.collect(
        binding_for(site), site.repo, run_dir, execution="completed", attempt_id=attempt_of(site)
    )
    assert replayed["delivery"] == "retained"
    assert replayed["patch_sha256"] == json.loads(before)["patch_sha256"]
    assert patch.read_bytes() == patch_before


def test_mismatched_artifact_refuses_and_touches_nothing(site, capsys, monkeypatch):
    edit_worker(site, monkeypatch)
    rc, _, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err
    run_dir = site.run_dir()
    name = ARTIFACT.sidecar_name("a")
    path = run_dir / name
    document = json.loads(path.read_text())
    document["attempt_id"] = "attempt-from-somewhere-else"
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n")
    before = path.read_bytes()
    patch = run_dir / ARTIFACT.patch_name("a")
    patch_before = patch.read_bytes()
    checkout = site.path_for("a")
    head_before = _git(checkout, "rev-parse", "HEAD")
    status_before = _git(checkout, "status", "--porcelain")

    record = ARTIFACT.collect(binding_for(site), site.repo, run_dir, execution="completed", attempt_id=attempt_of(site))
    assert record["delivery"] == "collection-failed"
    assert record["reason"] == "artifact-conflict"
    assert record["candidate_commit"] is None
    assert path.read_bytes() == before
    assert patch.read_bytes() == patch_before
    assert _git(checkout, "rev-parse", "HEAD") == head_before
    assert _git(checkout, "status", "--porcelain") == status_before


def test_switched_branch_fails_collection_and_leaves_the_checkout_alone(site, capsys, monkeypatch):
    def model(agent, prompt, **kwargs):
        cwd = Path(kwargs["cwd"])
        (cwd / "README.md").write_text("worker edit\n")
        (cwd / "left-over.txt").write_text("keep me\n")
        _git(cwd, "checkout", "-q", "-b", "worker-side")
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    rc, result, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err
    run_dir = site.run_dir()
    checkout = site.path_for("a")
    record = sidecar(site)
    assert record["delivery"] == "collection-failed"
    assert record["reason"] == "branch"
    assert record["candidate_commit"] is None
    assert not list(run_dir.glob("batch-artifact-*.patch"))
    # Exactly as the worker left it: its branch, its base, its files, its status.
    assert _git(checkout, "symbolic-ref", "--short", "HEAD") == "worker-side"
    assert _git(checkout, "rev-parse", "HEAD") == site.base_sha
    assert (checkout / "README.md").read_text() == "worker edit\n"
    assert (checkout / "left-over.txt").read_text() == "keep me\n"
    # The status keeps both the worker's edit and its untracked file (the
    # leading porcelain column space is stripped by the helper).
    assert _git(checkout, "status", "--porcelain") == "M README.md\n?? left-over.txt"
    # No candidate: the observation keeps the receipt kind and digest.
    event = observation(site)
    receipt = run_dir / f"batch-result-{hashlib.sha256(b'a').hexdigest()}.json"
    assert event.payload["artifact_kind"] == "receipt"
    assert event.payload["artifact_digest"] == hashlib.sha256(receipt.read_bytes()).hexdigest()
    assert result["tasks"]["a"]["artifact"]["delivery"] == "collection-failed"
