"""Regressions for dispatch error isolation and receipt-based recovery."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from brigade import cli, localio, runguard, work_cmd
from brigade.work_cmd import dispatch, dispatch_apply as apply, dispatch_state as store
from tests.test_work_dispatch import _tasks, dispatch_target  # noqa: F401
from tests.test_work_dispatch_apply import _apply, _intents
from tests.test_work_dispatch_apply import apply_target as dispatch_apply_target  # noqa: F401


@pytest.fixture
def apply_target(dispatch_apply_target):  # noqa: F811
    return dispatch_apply_target


def _unfinished(target):
    path = next((target / ".brigade/dispatch").glob("dispatch-*/intents/*.json"))
    row = store.read(path)
    row["state"] = "running"
    localio.write_json(path, row)
    session_path = path.parent.parent / "session.json"
    session = store.read(session_path)
    session.pop("finished_at", None)
    localio.write_json(session_path, session)
    return path, row


@pytest.mark.parametrize("error", [ValueError("worktree add failed"), OSError("worktree inaccessible")])
def test_workspace_error_releases_claim_and_continues_sibling(apply_target, capsys, monkeypatch, error):
    _tasks(apply_target, [["a.py"], ["b.py"]])
    git = apply._git

    def fail_one(cwd, *args):
        if args[:2] == ("worktree", "add") and any("task-0" in arg for arg in args):
            raise error
        return git(cwd, *args)

    monkeypatch.setattr(apply, "_git", fail_one)
    _apply(apply_target, capsys, expected=1)
    rows = _intents(apply_target)
    assert any(row["task_id"] == "task-1" and row["state"] == "completed" for row in rows)
    failed = [row for row in rows if row["task_id"] == "task-0"]
    assert all(row["state"] == "failed" and row["terminal"]["infrastructure"] for row in failed)
    assert all(row["terminal"]["failure_kind"] == "spawn-error" for row in failed)
    assert work_cmd._read_task_ledger(apply_target)["tasks"][0]["status"] == "pending"
    _apply(apply_target, capsys, "--max", "0")


def test_removed_finished_worktree_does_not_wedge_reconciliation(apply_target, capsys):
    _apply(apply_target, capsys)
    _, row = _unfinished(apply_target)
    subprocess.run(["git", "worktree", "remove", "--force", row["worktree"]], cwd=apply_target, check=True)
    _apply(apply_target, capsys, "--max", "0")
    (row,) = _intents(apply_target)
    assert row["state"] == "completed"
    assert row["terminal"]["changes"]["needs_review"] is True
    assert row["terminal"]["changes"]["commit_error"]
    assert work_cmd._read_task_ledger(apply_target)["tasks"][0]["status"] == "in_progress"
    _apply(apply_target, capsys, "--max", "0")


def test_unexpected_session_exception_cancels_supervised_children(apply_target, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DISPATCH_SLEEP", "60")

    def unexpected(*args):
        raise RuntimeError("unexpected supervisor failure")

    monkeypatch.setattr(apply.Apply, "_retry", unexpected)
    with pytest.raises(RuntimeError, match="unexpected supervisor failure"):
        _apply(apply_target, capsys)
    (row,) = _intents(apply_target)
    assert row["state"] == "canceled"
    assert not store.child_alive(row)
    assert work_cmd._read_task_ledger(apply_target)["tasks"][0]["status"] == "pending"


@pytest.mark.parametrize("reopen", [False, True])
def test_completed_task_can_be_dispatched_after_release_or_reopen(apply_target, capsys, reopen):
    _apply(apply_target, capsys)
    assert store.exclusions(apply_target)["task-0"] == "completed-unlanded"
    (row,) = _intents(apply_target)
    work_cmd._release_tasks(apply_target, task_ids=["task-0"], claim_ids=[row["claim_id"]])
    if reopen:
        ledger = work_cmd._read_task_ledger(apply_target)
        ledger["tasks"][0]["item_revision"] += 2
        work_cmd._write_task_ledger(apply_target, ledger)
    _apply(apply_target, capsys)
    assert len(_intents(apply_target)) == 2
    assert all(row["state"] == "completed" for row in _intents(apply_target))


def test_reset_clears_completed_revision_exclusion(apply_target, capsys):
    _apply(apply_target, capsys)
    store.reset(target=apply_target, task_id="task-0", reason="Reviewed for rework")
    assert "task-0" not in store.exclusions(apply_target)


@pytest.mark.parametrize(
    ("state", "kind"),
    [("failed", "budget-exhausted"), ("orphaned", "dispatch-orphaned"), ("canceled", "operator-cancelled")],
)
def test_policy_and_orphan_terminals_do_not_advance_breaker(apply_target, capsys, state, kind):
    _apply(apply_target, capsys)
    path = next((apply_target / ".brigade/dispatch").glob("dispatch-*/intents/*.json"))
    row = store.read(path)
    row.update(state=state, terminal={"failure_kind": kind, "infrastructure": False})
    localio.write_json(path, row)
    assert store.failure_counts(apply_target).get("task-0", 0) == 0


@pytest.mark.parametrize("status", ["ok", "running"])
def test_corrupt_child_journal_holds_unknown_lane_and_continues(apply_target, capsys, status):
    _apply(apply_target, capsys)
    path, row = _unfinished(apply_target)
    run_dir = Path(row["run_dir"])
    journal = run_dir / "events/lifecycle.jsonl"
    journal.parent.mkdir(exist_ok=True)
    journal.write_text('{"partial":')
    meta = localio.read_json_dict(run_dir / "run.json")
    meta["status"] = status
    meta.pop("finished_at", None)
    localio.write_json(run_dir / "run.json", meta)
    ledger = work_cmd._read_task_ledger(apply_target)
    sibling = dict(ledger["tasks"][0], id="task-1", status="pending", metadata={"footprint": {"files": ["b.py"]}})
    sibling.pop("claim")
    ledger["tasks"].append(sibling)
    work_cmd._write_task_ledger(apply_target, ledger)
    _apply(apply_target, capsys, expected=1)
    assert store.read(path)["state"] == "unknown"
    assert "journal" in store.read(path)["terminal"]["error"]
    assert work_cmd._read_task_ledger(apply_target)["tasks"][0]["claim"]["claim_id"] == row["claim_id"]
    assert any(row["task_id"] == "task-1" and row["state"] == "completed" for row in _intents(apply_target))


def test_windows_apply_refused_before_dispatch_writes(apply_target, capsys, monkeypatch):
    monkeypatch.setattr(dispatch, "sys", SimpleNamespace(platform="win32", stderr=sys.stderr))
    result = _apply(apply_target, capsys, expected=2)
    assert "Windows" in result["error"] and "--dry-run" in result["error"]
    assert not (apply_target / ".brigade/dispatch").exists()
    assert cli.main(["work", "dispatch", "--target", str(apply_target), "--dry-run"]) == 0


def test_lost_race_promotion_is_receipted(apply_target, capsys, monkeypatch):
    _tasks(apply_target, [["a.py"], ["b.py"]])
    claim = apply.locking._claim_task

    def race(target, task_id, **kwargs):
        if task_id == "task-0":
            claim(target, task_id, actor="other", claim_id="other-claim")
        return claim(target, task_id, **kwargs)

    monkeypatch.setattr(apply.locking, "_claim_task", race)
    result = _apply(apply_target, capsys, "--max", "1")
    directory = apply_target / ".brigade/dispatch" / result["dispatch_id"]
    assert any(
        row["event_type"] == "work.dispatch.backfill" and row["payload"]["task_id"] == "task-1"
        for row in store.events(directory)
    )


def test_reserve_intent_crash_retains_backfill_receipt(apply_target, capsys, monkeypatch):
    from tests.test_work_dispatch_apply import DispatchCrash

    _tasks(apply_target, [["a.py"], ["b.py"]])
    claim = apply.locking._claim_task
    new_intent = apply._new_intent

    with monkeypatch.context() as patch:

        def race(target, task_id, **kwargs):
            if task_id == "task-0":
                claim(target, task_id, actor="other", claim_id="other-claim")
            return claim(target, task_id, **kwargs)

        def crash(target, directory, session, plan, item):
            result = new_intent(target, directory, session, plan, item)
            if item["task_id"] == "task-1":
                raise DispatchCrash()
            return result

        patch.setattr(apply.locking, "_claim_task", race)
        patch.setattr(apply, "_new_intent", crash)
        with pytest.raises(DispatchCrash):
            _apply(apply_target, capsys, "--max", "1")
    directory = next((apply_target / ".brigade/dispatch").glob("dispatch-*/session.json")).parent
    _apply(apply_target, capsys, "--max", "0")
    assert any(row["task_id"] == "task-1" and row["state"] == "completed" for row in _intents(apply_target))
    assert any(
        row["event_type"] == "work.dispatch.backfill" and row["payload"]["task_id"] == "task-1"
        for row in store.events(directory)
    )


def test_cancellation_uses_shared_taxonomy(apply_target, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DISPATCH_SLEEP", "60")
    refresh = apply.Apply.refresh

    def interrupt(self, path, row, session):
        if row["state"] == "running":
            raise KeyboardInterrupt()
        return refresh(self, path, row, session)

    monkeypatch.setattr(apply.Apply, "refresh", interrupt)
    _apply(apply_target, capsys, expected=130)
    assert _intents(apply_target)[0]["terminal"]["failure_kind"] == "operator-cancelled"


def test_dispatch_worktree_is_discovered_and_prunable_when_clean(apply_target, capsys):
    _apply(apply_target, capsys)
    (row,) = _intents(apply_target)
    worktree = Path(row["worktree"])
    assert worktree in runguard.brigade_worktrees_for_repo(apply_target)
    assert runguard.is_brigade_created_worktree(worktree, apply_target)
    # Restore only fake worker output in this disposable test worktree.
    (worktree / "base.txt").write_text("base\n")
    assert cli.main(["runs", "prune-worktrees", "--target", str(apply_target), "--older-than", "0", "--apply"]) == 0
    assert not worktree.exists()
