"""Review regressions for dispatch launch identity and terminal ownership."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from brigade import cli, localio, work_cmd
from brigade.work_cmd import dispatch_apply as apply, dispatch_state as store
from tests.test_work_dispatch import _tasks, dispatch_target, requires_dispatch_apply  # noqa: F401
from tests.test_work_dispatch_apply import _apply, _intents
from tests.test_work_dispatch_apply import apply_target as dispatch_apply_target  # noqa: F401
from tests.test_work_dispatch_review import _unfinished


@pytest.fixture
def apply_target(dispatch_apply_target):  # noqa: F811
    return dispatch_apply_target


def test_child_command_uses_current_interpreter_and_real_run_parser(tmp_path):
    import sys

    row = {"seat": "coder", "worktree": str(tmp_path / "tree"), "run_dir": str(tmp_path / "run")}
    session = {"roster_path": str(tmp_path / "roster.toml"), "run_budget": str(tmp_path / "budget.json")}
    argv = apply._child_argv(row, session, "task prompt")
    assert argv[:4] == [sys.executable, "-m", "brigade", "run"]
    parsed = cli._build_parser().parse_args(argv[3:])
    assert parsed.worker == "coder"
    assert Path(parsed.cwd) == tmp_path / "tree"
    assert Path(parsed.output_dir) == tmp_path / "run"
    assert Path(parsed.roster) == tmp_path / "roster.toml"
    assert Path(parsed.run_budget) == tmp_path / "budget.json"
    assert parsed.wait == 0
    assert argv[-1] == "task prompt"


@pytest.mark.parametrize("operation", ["commit", "diff"])
@requires_dispatch_apply
def test_post_success_git_error_keeps_completed_claim(apply_target, capsys, monkeypatch, operation):
    _apply(apply_target, capsys)
    path, row = _unfinished(apply_target)
    git = apply._git

    def rejected(cwd, *args):
        if args[0] == operation:
            raise ValueError("git hook or output rejected")
        return git(cwd, *args)

    monkeypatch.setattr(apply, "_git", rejected)
    assert apply.Apply(apply_target).refresh(path, row, {"commit": True})
    assert row["state"] == "completed"
    changes = row["terminal"]["changes"]
    assert changes["needs_review"] is True
    assert changes["commit_error"] == "git hook or output rejected"
    assert work_cmd._read_task_ledger(apply_target)["tasks"][0]["claim"]["claim_id"] == row["claim_id"]


@pytest.mark.parametrize(("status", "state"), [("completed", "completed"), ("orphaned", "orphaned")])
@requires_dispatch_apply
def test_terminal_reread_has_same_status_mapping(apply_target, capsys, monkeypatch, status, state):
    _apply(apply_target, capsys)
    path, row = _unfinished(apply_target)
    reads = iter([None, {"status": status}])
    monkeypatch.setattr(apply, "_terminal_meta", lambda _: next(reads))
    assert apply.Apply(apply_target).refresh(path, row, {})
    assert row["state"] == state


@pytest.mark.parametrize("timeout", [False, True])
@requires_dispatch_apply
def test_cancel_preserves_child_that_already_succeeded(apply_target, capsys, timeout):
    _apply(apply_target, capsys)
    path, row = _unfinished(apply_target)
    apply.Apply(apply_target).cancel([(path, row)], {}, timeout=timeout)
    assert row["state"] == "completed"
    assert work_cmd._read_task_ledger(apply_target)["tasks"][0]["status"] == "in_progress"


@requires_dispatch_apply
def test_foreground_fills_capacity_before_waiting_for_detached_child(apply_target, capsys, monkeypatch):
    _tasks(apply_target, [["a.py"], ["b.py"]])
    monkeypatch.setenv("FAKE_DISPATCH_SLEEP", "2")
    _apply(apply_target, capsys, "--batch-size", "1", "--no-wait")
    (first,) = _intents(apply_target)
    new_intent = apply._new_intent
    alive_at_launch = []

    def observe(*args):
        alive_at_launch.append(store.child_alive(first))
        return new_intent(*args)

    monkeypatch.setattr(apply, "_new_intent", observe)
    result = _apply(apply_target, capsys, "--max", "2")
    assert alive_at_launch == [True]
    assert result["plan"]["live_children"] == 1
    assert result["plan"]["capacity"] == 1
    assert all(row["state"] == "completed" for row in _intents(apply_target))


def _corrupt_journal(row):
    journal = Path(row["run_dir"]) / "events/lifecycle.jsonl"
    journal.parent.mkdir(exist_ok=True)
    journal.write_text('{"partial":')


@requires_dispatch_apply
def test_unknown_refresh_does_not_repeat_transition(apply_target, capsys):
    _apply(apply_target, capsys)
    path, row = _unfinished(apply_target)
    _corrupt_journal(row)
    engine = apply.Apply(apply_target)
    assert engine.refresh(path, row, {})
    before = len(store.events(path.parent.parent))
    assert engine.refresh(path, row, {})
    assert len(store.events(path.parent.parent)) == before


@requires_dispatch_apply
def test_unknown_live_child_still_obeys_deadline(apply_target, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DISPATCH_SLEEP", "60")
    _apply(apply_target, capsys, "--no-wait", "--deadline", "30")
    (row,) = _intents(apply_target)
    run_dir = Path(row["run_dir"])
    stop = time.monotonic() + 5
    while not (run_dir / "run.json").exists() and time.monotonic() < stop:
        time.sleep(0.02)
    _corrupt_journal(row)
    session_path = next((apply_target / ".brigade/dispatch").glob("dispatch-*/session.json"))
    session = store.read(session_path)
    session["created_at"] = "2026-01-01T00:00:00+00:00"
    localio.write_json(session_path, session)
    try:
        _apply(apply_target, capsys, "--max", "0", expected=1)
        assert not store.child_alive(row)
        assert _intents(apply_target)[0]["state"] == "failed"
    finally:
        if store.child_alive(row):
            apply.Apply(apply_target)._cancel_adopted(row)


@requires_dispatch_apply
def test_reset_can_explicitly_release_dead_unknown_intent(apply_target, capsys):
    _apply(apply_target, capsys)
    path, row = _unfinished(apply_target)
    _corrupt_journal(row)
    apply.Apply(apply_target).refresh(path, row, {})
    assert (
        cli.main(
            [
                "work",
                "dispatch-reset",
                "task-0",
                "--target",
                str(apply_target),
                "--reason",
                "Inspected corrupt receipt",
                "--release-unknown",
                "--json",
            ]
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["released_unknown"] == [row["intent_id"]]
    assert store.read(path)["state"] == "canceled"
    assert "task-0" not in store.exclusions(apply_target)
    assert work_cmd._read_task_ledger(apply_target)["tasks"][0]["status"] == "pending"


@pytest.mark.parametrize("pid", [None, "not-a-pid", True, -1])
@requires_dispatch_apply
def test_bad_launch_pid_is_isolated_to_its_intent(apply_target, capsys, pid):
    _apply(apply_target, capsys)
    path, row = _unfinished(apply_target)
    receipt = {} if pid is None else {"pid": pid}
    localio.write_json(Path(row["run_dir"]) / "dispatch-launch.json", receipt)
    assert apply.Apply(apply_target).refresh(path, row, {})
    assert row["state"] == "unknown"
    assert work_cmd._read_task_ledger(apply_target)["tasks"][0]["status"] == "in_progress"


@requires_dispatch_apply
def test_detached_infrastructure_retry_reports_latest_success(apply_target, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DISPATCH_STATUS", "failed")
    monkeypatch.setenv("FAKE_DISPATCH_INFRA", "1")
    _apply(apply_target, capsys, "--no-wait")
    (first,) = _intents(apply_target)
    stop = time.monotonic() + 5
    while store.child_alive(first) and time.monotonic() < stop:
        time.sleep(0.02)
    monkeypatch.setenv("FAKE_DISPATCH_STATUS", "ok")
    monkeypatch.delenv("FAKE_DISPATCH_INFRA")
    _apply(apply_target, capsys, "--max", "0")
    rows = sorted(_intents(apply_target), key=lambda row: row["attempt_no"])
    assert [row["state"] for row in rows] == ["failed", "completed"]
    assert work_cmd._read_task_ledger(apply_target)["tasks"][0]["status"] == "in_progress"


@requires_dispatch_apply
def test_release_unknown_refuses_live_child_without_writing_reset(apply_target, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DISPATCH_SLEEP", "60")
    _apply(apply_target, capsys, "--no-wait")
    path, row = _unfinished(apply_target)
    _corrupt_journal(row)
    engine = apply.Apply(apply_target)
    try:
        assert not engine.refresh(path, row, {})
        assert row["state"] == "unknown"
        assert (
            cli.main(
                [
                    "work",
                    "dispatch-reset",
                    "task-0",
                    "--target",
                    str(apply_target),
                    "--reason",
                    "Inspecting corrupt receipt",
                    "--release-unknown",
                ]
            )
            == 2
        )
        assert "child is alive" in capsys.readouterr().err
        assert not list((store.root(apply_target) / "resets").glob("*.json"))
        assert store.read(path)["state"] == "unknown"
        assert work_cmd._read_task_ledger(apply_target)["tasks"][0]["status"] == "in_progress"
    finally:
        engine.cancel([(path, row)], {})
