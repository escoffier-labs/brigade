"""Per-task worktrees wired into explicit batch execution and the transport."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import Mock

import pytest

from tests._home import set_home

from brigade import aboyeur, agents, cli, runguard, run_cassette, run_tasks
from brigade import run_batch_workspace as workspace
from brigade import runs_cmd_worktrees
from brigade.run_batch import SCHEMA

_GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    # Fixed dates keep a worktree commit equal to the same content elsewhere.
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+00:00",
}


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False, env={**os.environ, **_GIT_ENV}
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _files(root: Path) -> dict[str, bytes]:
    """Every working-tree file under root, excluding Git metadata and Brigade run state."""
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.relative_to(root).parts[0] not in {".git", ".brigade"}
    }


@dataclass
class Site:
    repo: Path
    base_sha: str
    home: Path
    cache: Path
    notes: dict[str, object] = field(default_factory=dict)

    def task(self, name: str, **kwargs: object) -> dict[str, object]:
        return {"task_id": name, "seat": "coder", "text": f"Do the work for {name}", **kwargs}

    def run_dir(self) -> Path:
        return next((self.repo / ".brigade" / "runs").glob("batch-*"))

    def path_for(self, task_id: str) -> Path:
        return workspace.expected_path(self.repo, self.run_dir().name, task_id)


@pytest.fixture
def site(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("base\n")
    source = repo / "src"
    source.mkdir()
    (source / "app.py").write_text("print('base')\n")
    (repo / ".brigade").mkdir()
    (repo / ".brigade" / "roster.toml").write_text(
        """orchestrator = "chef"
[limits]
max_workers = 2
[agents.chef]
cli = "codex"
role = "plan"
[agents.coder]
cli = "codex"
role = "implement"
"""
    )
    (repo / ".git" / "info" / "exclude").write_text(".brigade/runs/\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    # Batch execution never plans or synthesizes through the orchestrator.
    monkeypatch.setattr(aboyeur.planning, "plan", Mock(side_effect=AssertionError("planner called")))
    monkeypatch.setattr(aboyeur.planning, "_run_orchestrator", Mock(side_effect=AssertionError("synthesis called")))
    return Site(repo=repo, base_sha=base_sha, home=home, cache=workspace.cache_root())


def invoke(site: Site, capsys, tasks: list[dict[str, object]], *, request: str = "req"):
    path = site.home / "batch.json"
    path.write_text(json.dumps({"schema": SCHEMA, "request_id": request, "tasks": tasks}))
    rc = cli.main(["run", "batch", str(path), "--cwd", str(site.repo), "--json"])
    captured = capsys.readouterr()
    assert captured.out, captured.err
    return rc, json.loads(captured.out), captured.err


def record_cwd(monkeypatch) -> dict[str, Path]:
    seen: dict[str, Path] = {}
    lock = threading.Lock()

    def model(agent, prompt, **kwargs):
        name = "b" if "Batch task_id: b" in prompt else "a"
        with lock:
            seen[name] = Path(kwargs["cwd"])
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    return seen


def test_two_writable_worktree_tasks_run_in_separate_worktrees(site, capsys, monkeypatch):
    seen = record_cwd(monkeypatch)
    before = _files(site.repo)
    rc, result, err = invoke(
        site,
        capsys,
        [
            site.task("a", isolation="worktree"),
            site.task("b", isolation="worktree"),
        ],
    )
    assert rc == 0, err
    first, second = seen["a"], seen["b"]
    assert first != second
    assert {first.parent, second.parent} == {site.cache}
    for path in (first, second):
        assert path.is_dir()
        assert Path(_git(path, "rev-parse", "--show-toplevel")).resolve() == path.resolve()
        assert _git(path, "rev-parse", "HEAD") == site.base_sha
        assert _git(path, "symbolic-ref", "--short", "HEAD").startswith(workspace.BRANCH_PREFIX)
    # The caller checkout is untouched, and both checkouts and branches survive the run.
    assert _files(site.repo) == before
    assert _git(site.repo, "status", "--porcelain") == ""
    assert _git(site.repo, "rev-parse", "HEAD") == site.base_sha
    assert first.is_dir() and second.is_dir()
    assert result["tasks"]["a"]["workspace"] == "worktree"
    assert result["tasks"]["b"]["workspace"] == "worktree"


def test_shared_task_still_gets_the_caller_workspace(site, capsys, monkeypatch):
    seen = record_cwd(monkeypatch)
    rc, result, err = invoke(
        site,
        capsys,
        [
            site.task("a", isolation="worktree"),
            site.task("b", read_only=True, depends_on=["a"]),
        ],
    )
    assert rc == 0, err
    assert seen["a"] != site.repo and seen["a"].parent == site.cache
    assert seen["b"].resolve() == site.repo.resolve()
    assert result["tasks"]["a"]["workspace"] == "worktree"
    assert result["tasks"]["b"]["workspace"] == "shared"


def test_caller_cwd_and_run_lock_resolution_keep_the_batch_workspace(site, capsys, monkeypatch):
    recordings: list[tuple[Path, Path | None]] = []
    real_record = run_cassette.record_result

    def record(**kwargs):
        recordings.append((Path(kwargs["workspace"]), kwargs.get("policy_workspace")))
        return None

    seen = record_cwd(monkeypatch)
    monkeypatch.setattr(run_cassette, "record_result", record)
    try:
        rc, result, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    finally:
        monkeypatch.setattr(run_cassette, "record_result", real_record)
    assert rc == 0, err
    # Cassette publication is keyed to the caller workspace, not the task worktree.
    assert [path.resolve() for path, _ in recordings] == [site.repo.resolve()]
    run_meta = json.loads((site.run_dir() / "run.json").read_text())
    assert Path(run_meta["lock_workspace"]).resolve() == site.repo.resolve()
    assert not (seen["a"] / ".brigade" / "run.lock").exists()


def test_moved_base_refuses_the_whole_batch_before_any_launch(site, capsys, monkeypatch):
    from brigade import run_batch

    calls: list[str] = []
    monkeypatch.setattr(agents, "run_agent", lambda *a, **k: calls.append("launched"))
    pinned = site.base_sha
    # The request was pinned before the caller advanced HEAD; the run must refuse
    # the whole batch instead of building checkouts from a stale revision.
    (site.repo / "late.txt").write_text("late\n")
    _git(site.repo, "add", "late.txt")
    _git(site.repo, "commit", "-q", "-m", "late")
    monkeypatch.setattr(run_batch, "_pinned_base_sha", lambda canonical, cwd: pinned)
    rc, result, err = invoke(
        site,
        capsys,
        [site.task("a", isolation="worktree"), site.task("b", read_only=True)],
    )
    assert rc == 2, err
    assert result["error"]["code"] == "source-changed"
    assert calls == []
    assert not list(site.cache.glob("*"))


def test_crash_after_binding_replays_the_same_worktree_once(site, capsys, monkeypatch):
    from brigade import run_journal, run_lifecycle

    real_dispatch = aboyeur.planning.dispatch
    monkeypatch.setattr(aboyeur.planning, "dispatch", Mock(side_effect=KeyboardInterrupt))
    rc, first, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 1, err
    path = site.path_for("a")
    assert path.is_dir()
    run_dir = site.run_dir()
    state = run_tasks.read_tasks(run_dir)[0]
    assert state.workspace is not None and state.handle.attempt_id is None
    events = run_journal.read_journal_bounded(run_lifecycle._journal_path(run_dir)).events
    assert [e.event_type for e in events].count("task.workspace-bound") == 1

    seen = record_cwd(monkeypatch)
    monkeypatch.setattr(aboyeur.planning, "dispatch", real_dispatch)
    rc, second, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 0, err
    # The recorded checkout is validated and reused; nothing is recreated.
    assert seen["a"].resolve() == path.resolve()
    assert [p for p in site.cache.iterdir() if p.name.startswith(path.name)] == [path]
    events = run_journal.read_journal_bounded(run_lifecycle._journal_path(run_dir)).events
    assert [e.event_type for e in events].count("task.workspace-bound") == 1
    assert second["tasks"]["a"]["status"] == "completed"


def test_crash_after_prepare_never_creates_a_second_worktree(site, capsys, monkeypatch):
    def crash(agent, prompt, **kwargs):
        site.notes["launches"] = int(site.notes.get("launches", 0)) + 1
        raise KeyboardInterrupt

    monkeypatch.setattr(agents, "run_agent", crash)
    rc, first, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 1, err
    run_dir = site.run_dir()
    state = run_tasks.read_tasks(run_dir)[0]
    assert state.handle.attempt_id is not None and state.status == "unknown"
    path = site.path_for("a")
    assert path.is_dir()
    before = sorted(p.name for p in site.cache.iterdir())

    seen = record_cwd(monkeypatch)
    rc, second, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    # The existing relaunch fence refuses replay, so no checkout is prepared again.
    assert rc == 1, err
    assert second == first
    assert sorted(p.name for p in site.cache.iterdir()) == before
    assert site.notes["launches"] == 1
    assert seen == {}
    assert path.is_dir()


def test_file_added_after_creation_refuses_that_task(site, capsys, monkeypatch):
    from brigade.run_batch_execute import BatchExecution

    calls: list[str] = []
    monkeypatch.setattr(agents, "run_agent", lambda *a, **k: calls.append("launched"))
    refusals: dict[str, tuple[str, str]] = {}
    real_validate = BatchExecution._validate_at_launch

    def validate(self, task_id):
        try:
            real_validate(self, task_id)
        finally:
            if task_id in self.workspace_refusals:
                refusals[task_id] = self.workspace_refusals[task_id]

    real_prepare = BatchExecution.prepare_workspaces

    def prepare(self, stack):
        real_prepare(self, stack)
        # A file appears in the checkout between creation and launch.
        for binding in self.workspaces.values():
            (binding.path / "scratch.txt").write_text("added late\n")

    monkeypatch.setattr(BatchExecution, "_validate_at_launch", validate)
    monkeypatch.setattr(BatchExecution, "prepare_workspaces", prepare)
    rc, result, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 2, err
    assert calls == []
    assert refusals == {"a": (workspace.WORKSPACE_INVALID, "dirty")}
    assert result["tasks"]["a"]["status"] == "queued"
    assert result["tasks"]["a"]["not_run"] is True
    assert site.path_for("a").is_dir()
    receipts = json.loads((site.run_dir() / "worker-results.json").read_text())
    assert [r["detail"] for r in receipts["results"]] == ["task a workspace workspace-invalid: dirty"]


def test_remote_routing_refuses_worktree_tasks_before_any_launch(site, capsys, monkeypatch):
    from brigade.aboyeur import orchestrator

    calls: list[str] = []
    monkeypatch.setattr(agents, "run_agent", lambda *a, **k: calls.append("launched"))
    monkeypatch.setattr(orchestrator, "_worker_remote_transport", lambda **kwargs: Mock(name="remote"))
    rc, result, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 2, err
    assert result["error"]["code"] == "isolation-remote-unsupported"
    assert calls == []
    assert not list(site.cache.glob("*"))


def test_prune_worktrees_skips_a_live_task_worktree(site, capsys, monkeypatch):
    classifications: list[dict[str, object]] = []
    lock = threading.Lock()

    def model(agent, prompt, **kwargs):
        buffer = io.StringIO()
        with lock, contextlib.redirect_stdout(buffer):
            runs_cmd_worktrees.prune_worktrees(site.repo, older_than_days=0, apply=True, json_output=True)
        classifications.append(json.loads(buffer.getvalue()))
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    rc, result, err = invoke(
        site,
        capsys,
        [site.task("a", isolation="worktree"), site.task("b", isolation="worktree")],
    )
    assert rc == 0, err
    assert classifications
    entries = {Path(entry["path"]).resolve(): entry for entry in classifications[0]["worktrees"]}
    for task_id in ("a", "b"):
        path = site.path_for(task_id)
        assert path.is_dir()
        assert entries[path.resolve()]["removable"] is False
        assert "run lock is present" in entries[path.resolve()]["reasons"]
    assert classifications[0]["removed"] == []
    # After the run the leases are released, so nothing is left fenced.
    for task_id in ("a", "b"):
        assert runguard.run_lock_state(site.path_for(task_id), site.run_dir()) == "absent"


def test_worktree_task_worktree_is_never_removed_on_failure(site, capsys, monkeypatch):
    monkeypatch.setattr(agents, "run_agent", Mock(return_value=agents.AgentResult(text="no", ok=False)))
    rc, result, err = invoke(site, capsys, [site.task("a", isolation="worktree")])
    assert rc == 1, err
    assert result["tasks"]["a"]["status"] == "failed"
    path = site.path_for("a")
    assert path.is_dir()
    branches = _git(site.repo, "branch", "--list", workspace.branch_name(site.run_dir().name, "a"))
    assert workspace.branch_name(site.run_dir().name, "a") in branches
