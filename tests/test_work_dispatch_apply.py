"""Apply and recovery use real linked worktrees and a fake run executable."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from brigade import cli, localio, runguard, work_cmd
from tests._posix import requires_process_groups
from tests.test_work_dispatch import _tasks, dispatch_target, requires_dispatch_apply  # noqa: F401
from tests.work_cmd_test_helpers import _init_git_repo


@pytest.fixture
def apply_target(dispatch_target, monkeypatch, tmp_path):  # noqa: F811
    target = dispatch_target
    _init_git_repo(target)
    (target / ".gitignore").write_text(".brigade/\n")
    (target / "base.txt").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=target, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "test base"],
        cwd=target,
        check=True,
    )
    monkeypatch.setattr(runguard, "brigade_worktree_root", lambda: tmp_path / "worktrees")
    executable = tmp_path / "bin" / "fake_run.py"
    executable.parent.mkdir()
    executable.write_text(
        f"#!{sys.executable}\n"
        + """import json, os, sys, time
from pathlib import Path
from brigade import runguard
args = sys.argv
run = Path(args[args.index('--output-dir') + 1])
cwd = Path(args[args.index('--cwd') + 1])
run.mkdir(parents=True, exist_ok=True)
with runguard.run_lock(cwd, run_dir=run, wait_seconds=0):
    with (run / 'launch-count').open('a') as f: f.write('launch\\n')
    (run / 'argv.json').write_text(json.dumps(args))
    now = '2026-01-01T00:00:00+00:00'
    (run / 'run.json').write_text(json.dumps({'status': 'running', 'cwd': str(cwd), 'lock_workspace': str(cwd), 'started_at': now}))
    time.sleep(float(os.environ.get('FAKE_DISPATCH_SLEEP', '0')))
    (cwd / 'base.txt').write_text('changed\\n')
    status = os.environ.get('FAKE_DISPATCH_STATUS', 'ok')
    meta = {'status': status, 'cwd': str(cwd), 'lock_workspace': str(cwd), 'started_at': now, 'finished_at': now}
    if os.environ.get('FAKE_DISPATCH_INFRA'): meta['failure_kind'] = 'provider-error'
    meta['failure_kind'] = os.environ.get('FAKE_DISPATCH_KIND', meta.get('failure_kind'))
    (run / 'run.json').write_text(json.dumps(meta))
    release_file = os.environ.get('FAKE_DISPATCH_TERMINAL_RELEASE_FILE')
    if release_file:
        deadline = time.monotonic() + 30
        while not Path(release_file).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
    time.sleep(float(os.environ.get('FAKE_DISPATCH_AFTER_TERMINAL', '0')))
"""
    )
    executable.chmod(0o755)
    if sys.platform != "win32":
        popen = subprocess.Popen

        def launch(argv, *args, **kwargs):
            if "brigade.work_cmd.dispatch_child" in argv:
                child_start = argv.index("brigade.work_cmd.dispatch_child") + 2
                child_argv = argv[child_start:]
                argv = [*argv[:child_start], sys.executable, str(executable), *child_argv[child_argv.index("run") :]]
            return popen(argv, *args, **kwargs)

        monkeypatch.setattr(subprocess, "Popen", launch)
    _tasks(target, [["base.txt"]])
    return target


def _apply(target, capsys, *options, expected=0):
    code = cli.main(["work", "dispatch", "--target", str(target), "--worker", "coder", "--json", *options])
    captured = capsys.readouterr()
    assert code == expected, captured
    return json.loads(captured.out)


def _intents(target):
    return [
        json.loads(path.read_text())
        for path in sorted((target / ".brigade/dispatch").glob("dispatch-*/intents/*.json"))
    ]


@requires_dispatch_apply
def test_large_terminal_metadata_is_bounded_in_event_envelope(apply_target, capsys, monkeypatch):
    from brigade.work_cmd import dispatch_apply as apply, dispatch_state as store

    huge = "x" * (2 * store.MAX_EVENT_LINE_BYTES)
    real_git = apply._git

    def fake_git(worktree, *args):
        if args[:1] == ("diff",) and "--stat" in args or args[:1] == ("ls-files",):
            return huge
        return real_git(worktree, *args)

    monkeypatch.setattr(apply, "_git", fake_git)
    _apply(apply_target, capsys)
    (intent,) = _intents(apply_target)
    assert intent["state"] == "completed"
    changes = intent["terminal"]["changes"]
    assert len(changes["diff_stat"]) < 20_000
    assert "truncated" in changes["untracked"]


@requires_dispatch_apply
def test_reconcile_skips_settled_sessions_and_completed_rows(apply_target, capsys, monkeypatch):
    from brigade.work_cmd import dispatch_apply as apply

    _apply(apply_target, capsys)
    engine = apply.Apply(apply_target)
    refreshed = []
    original = engine.refresh
    monkeypatch.setattr(engine, "refresh", lambda *a, **k: refreshed.append(a[0]) or original(*a, **k))
    engine.reconcile(no_wait=True, launch=False)
    engine.reconcile(no_wait=True, launch=False)
    assert refreshed == []
    assert engine.settled


@requires_dispatch_apply
def test_failed_terminal_with_one_event_slot_releases_in_one_transition(apply_target, capsys, monkeypatch):
    from brigade.work_cmd import dispatch_state as store

    monkeypatch.setenv("FAKE_DISPATCH_STATUS", "failed")
    original = store.event_room
    monkeypatch.setattr(
        store,
        "event_room",
        lambda directory, *, reserve_events: reserve_events < 2 and original(directory, reserve_events=reserve_events),
    )
    _apply(apply_target, capsys, expected=1)
    ((path, row),) = store.intents(apply_target)
    assert row["state"] == "failed"
    assert row["released"] is True
    assert row["terminal"]


@requires_dispatch_apply
def test_terminal_release_recovers_legacy_receipts_and_retries_only_after_failure(apply_target, capsys, monkeypatch):
    from brigade.work_cmd import dispatch_apply as apply, dispatch_state as store

    monkeypatch.setenv("FAKE_DISPATCH_STATUS", "failed")
    _apply(apply_target, capsys, expected=1)
    ((path, row),) = store.intents(apply_target)
    row.pop("released", None)  # Legacy receipt or crash before recording release.
    localio.write_json(path, row)
    release = apply._release
    attempts = []

    def flaky_release(target, intent):
        attempts.append(intent["claim_id"])
        if len(attempts) == 1:
            raise OSError("release unavailable")
        release(target, intent)

    monkeypatch.setattr(apply, "_release", flaky_release)
    with pytest.raises(OSError, match="release unavailable"):
        apply._finish(apply_target, path, row, "failed", {})
    assert not store.read(path).get("released")
    apply.Apply(apply_target).reconcile(no_wait=True, launch=False)
    assert store.read(path)["released"] is True
    apply.Apply(apply_target).reconcile(no_wait=True, launch=False)
    assert len(attempts) == 2


@requires_dispatch_apply
def test_apply_keeps_claim_and_branch_with_uncommitted_diff(apply_target, capsys):
    result = _apply(apply_target, capsys)
    (intent,) = _intents(apply_target)
    assert intent["state"] == "completed"
    assert intent["claim_id"] == "dispatch-" + intent["intent_id"]
    assert intent["terminal"]["changes"]["diff_stat"]
    (task,) = work_cmd._read_task_ledger(apply_target)["tasks"]
    assert task["status"] == "in_progress"
    assert task["claim"]["claim_id"] == intent["claim_id"]
    worktree = Path(intent["worktree"])
    assert (
        subprocess.check_output(["git", "branch", "--show-current"], cwd=worktree, text=True).strip()
        == intent["branch"]
    )
    assert subprocess.check_output(["git", "diff"], cwd=worktree, text=True)
    assert (apply_target / "base.txt").read_text() == "base\n"
    argv = json.loads((Path(intent["run_dir"]) / "argv.json").read_text())
    assert "--roster" in argv and "--wait" in argv
    assert "Leave changes uncommitted" in argv[-1] and "Do not push" in argv[-1]
    events = apply_target / ".brigade/dispatch" / result["dispatch_id"] / "events.jsonl"
    previous = None
    for line in events.read_text().splitlines():
        event = json.loads(line)
        digest = event.pop("digest")
        assert event["previous_digest"] == previous
        assert (
            digest
            == hashlib.sha256(
                json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            ).hexdigest()
        )
        previous = digest


@requires_dispatch_apply
def test_failure_releases_only_its_claim_and_keeps_worktree(apply_target, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DISPATCH_STATUS", "failed")
    _apply(apply_target, capsys, expected=1)
    (intent,) = _intents(apply_target)
    assert intent["state"] == "failed"
    assert Path(intent["worktree"]).is_dir()
    (task,) = work_cmd._read_task_ledger(apply_target)["tasks"]
    assert task["status"] == "pending" and not task.get("claim")


@requires_dispatch_apply
def test_no_wait_then_next_invocation_finalizes(apply_target, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DISPATCH_SLEEP", "0.2")
    _apply(apply_target, capsys, "--no-wait")
    (intent,) = _intents(apply_target)
    assert intent["state"] in {"launching", "running"}
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        meta = localio.read_json_dict(Path(intent["run_dir"]) / "run.json")
        if meta and meta.get("status") == "ok":
            break
        time.sleep(0.02)
    _apply(apply_target, capsys)
    (intent,) = _intents(apply_target)
    assert intent["state"] == "completed"
    assert (Path(intent["run_dir"]) / "launch-count").read_text() == "launch\n"


@requires_dispatch_apply
def test_breaker_and_explicit_reset(apply_target, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DISPATCH_STATUS", "failed")
    for _ in range(3):
        _apply(apply_target, capsys, expected=1)
    result = _apply(apply_target, capsys)
    assert result["plan"]["selected"] == []
    assert {"task_id": "task-0", "reason": "circuit-breaker"} in result["plan"]["skipped"]
    assert (
        cli.main(["work", "dispatch-reset", "task-0", "--target", str(apply_target), "--reason", "Reviewed failure"])
        == 0
    )
    capsys.readouterr()
    monkeypatch.setenv("FAKE_DISPATCH_STATUS", "ok")
    _apply(apply_target, capsys)
    assert _intents(apply_target)[-1]["state"] == "completed" or any(
        i["state"] == "completed" for i in _intents(apply_target)
    )


@requires_dispatch_apply
def test_infrastructure_retries_are_bounded_and_do_not_trip(apply_target, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DISPATCH_STATUS", "failed")
    monkeypatch.setenv("FAKE_DISPATCH_INFRA", "1")
    _apply(apply_target, capsys, expected=1)
    intents = _intents(apply_target)
    assert len(intents) == 3
    assert all(i["terminal"]["infrastructure"] for i in intents)
    assert cli.main(["work", "dispatches", "--target", str(apply_target), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["tripped_tasks"] == []


def test_dispatch_lock_refuses_second_holder(apply_target):
    from brigade.work_cmd.dispatch_state import dispatch_lock

    with dispatch_lock(apply_target):
        with pytest.raises(ValueError, match=rf"dispatch\.lock is held; last recorded owner pid {os.getpid()}"):
            with dispatch_lock(apply_target):
                pytest.fail("second lock acquisition unexpectedly succeeded")
    assert _intents(apply_target) == []


def test_dispatch_lock_metadata_uses_held_descriptor(apply_target, monkeypatch):
    from brigade.work_cmd.dispatch_state import dispatch_lock

    lock_path = apply_target / ".brigade/dispatch/dispatch.lock"
    original_write_text = Path.write_text

    def reject_reopened_lock_write(path, *args, **kwargs):
        if path == lock_path:
            raise PermissionError("dispatch lock path cannot be reopened while its byte range is locked")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", reject_reopened_lock_write)
    with dispatch_lock(apply_target):
        from brigade.work_cmd.dispatch_state import _dispatch_lock_holder

        assert _dispatch_lock_holder(lock_path) == str(os.getpid())
    with dispatch_lock(apply_target):
        pass
    assert lock_path.read_bytes() == b"\0BRIGADE-DISPATCH-LOCK-V1:" + f"{os.getpid()}\n".encode("ascii")


def test_dispatch_lock_legacy_metadata_is_unknown(tmp_path):
    from brigade.work_cmd.dispatch_state import _dispatch_lock_holder

    lock_path = tmp_path / "dispatch.lock"
    lock_path.write_bytes(b"59648\n")
    assert _dispatch_lock_holder(lock_path) == "unknown"


def test_dispatch_lock_partial_or_malformed_metadata_is_unknown(tmp_path):
    from brigade.work_cmd.dispatch_state import _dispatch_lock_holder

    lock_path = tmp_path / "dispatch.lock"
    for record in (
        b"\0BRIGADE-DISPATCH-LOCK-V1:123",
        b"\0" + b"123",
        b"\0BRIGADE-DISPATCH-LOCK-V1:x\n",
    ):
        lock_path.write_bytes(record)
        assert _dispatch_lock_holder(lock_path) == "unknown"


def test_dispatch_lock_contention_calls_metadata_last_recorded(tmp_path):
    from brigade.inbox_lock import held_file_lock_descriptor
    from brigade.work_cmd.dispatch_state import dispatch_lock

    lock_path = tmp_path / ".brigade/dispatch/dispatch.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.write_bytes(b"\0BRIGADE-DISPATCH-LOCK-V1:123\n")
    with held_file_lock_descriptor(lock_path, deadline_seconds=0):
        with pytest.raises(ValueError, match=r"last recorded owner pid 123"):
            with dispatch_lock(tmp_path):
                pytest.fail("contended dispatch lock was acquired")


def test_held_file_lock_descriptor_unlocks_acquired_byte_after_seek(tmp_path):
    from brigade.inbox_lock import held_file_lock_descriptor

    lock_path = tmp_path / "state.lock"
    with held_file_lock_descriptor(lock_path, deadline_seconds=1) as held:
        assert held is not None
        os.lseek(held.fd, 8, os.SEEK_SET)
    with held_file_lock_descriptor(lock_path, deadline_seconds=1):
        pass


class DispatchCrash(BaseException):
    pass


@pytest.mark.parametrize(
    "point",
    [
        "plan",
        "planned",
        "claim-side-effect",
        "claimed",
        "worktree-side-effect",
        "workspace_ready",
        "run-dir",
        "launching",
        "spawn-before-pid",
        "running",
        "terminal-before-intent",
    ],
)
@requires_dispatch_apply
def test_crash_recovery_has_one_claim_worktree_and_child(apply_target, capsys, monkeypatch, point):
    from brigade.work_cmd import dispatch_apply as apply, dispatch_state as store

    with monkeypatch.context() as patch:
        transition = store.transition
        event = store.event
        claim = apply.locking._claim_task
        git = apply._git
        popen = subprocess.Popen
        finish = apply._finish

        def interrupted_transition(path, row, state, **updates):
            transition(path, row, state, **updates)
            match = state == point and (state != "workspace_ready" or not row.get("run_dir"))
            if match or (point == "run-dir" and state == "workspace_ready" and row.get("run_dir")):
                raise DispatchCrash()

        def interrupted_event(directory, name, payload):
            event(directory, name, payload)
            if point == "plan" and name == "work.dispatch.planned":
                raise DispatchCrash()

        def interrupted_claim(*args, **kwargs):
            result = claim(*args, **kwargs)
            if point == "claim-side-effect":
                raise DispatchCrash()
            return result

        def interrupted_git(cwd, *args):
            result = git(cwd, *args)
            if point == "worktree-side-effect" and args[:2] == ("worktree", "add"):
                raise DispatchCrash()
            return result

        def interrupted_popen(argv, *args, **kwargs):
            child = popen(argv, *args, **kwargs)
            if point == "spawn-before-pid" and "brigade.work_cmd.dispatch_child" in argv:
                raise DispatchCrash()
            return child

        def interrupted_finish(*args, **kwargs):
            if point == "terminal-before-intent":
                raise DispatchCrash()
            return finish(*args, **kwargs)

        patch.setattr(store, "transition", interrupted_transition)
        patch.setattr(store, "event", interrupted_event)
        patch.setattr(apply.locking, "_claim_task", interrupted_claim)
        patch.setattr(apply, "_git", interrupted_git)
        patch.setattr(subprocess, "Popen", interrupted_popen)
        patch.setattr(apply, "_finish", interrupted_finish)
        with pytest.raises(DispatchCrash):
            cli.main(["work", "dispatch", "--target", str(apply_target), "--worker", "coder"])
    capsys.readouterr()
    _apply(apply_target, capsys)
    (intent,) = _intents(apply_target)
    assert intent["state"] == "completed"
    assert (Path(intent["run_dir"]) / "launch-count").read_text() == "launch\n"
    (task,) = work_cmd._read_task_ledger(apply_target)["tasks"]
    assert task["claim"]["claim_id"] == intent["claim_id"]
    listing = subprocess.check_output(["git", "worktree", "list", "--porcelain"], cwd=apply_target, text=True)
    assert listing.count("worktree ") == 2


@requires_dispatch_apply
def test_lost_race_fills_capacity_from_next_wave_candidate(apply_target, capsys, monkeypatch):
    from brigade.work_cmd import dispatch_apply as apply

    _tasks(apply_target, [["a.py"], ["b.py"]])
    original = apply.locking._claim_task

    def race(target, task_id, **kwargs):
        if task_id == "task-0":
            original(target, task_id, actor="other", claim_id="other-claim")
        return original(target, task_id, **kwargs)

    monkeypatch.setattr(apply.locking, "_claim_task", race)
    _apply(apply_target, capsys, "--max", "1")
    rows = _intents(apply_target)
    assert {i["task_id"]: i["state"] for i in rows} == {"task-0": "lost_race", "task-1": "completed"}


@requires_dispatch_apply
def test_late_failure_never_releases_a_newer_claim(apply_target, capsys, monkeypatch):
    from brigade.work_cmd import dispatch_apply as apply

    monkeypatch.setenv("FAKE_DISPATCH_STATUS", "failed")
    release = apply._release

    def replaced_claim(target, row):
        ledger = work_cmd._read_task_ledger(target)
        ledger["tasks"][0]["claim"]["claim_id"] = "new-generation"
        work_cmd._write_task_ledger(target, ledger)
        release(target, row)

    monkeypatch.setattr(apply, "_release", replaced_claim)
    _apply(apply_target, capsys, expected=1)
    (task,) = work_cmd._read_task_ledger(apply_target)["tasks"]
    assert task["status"] == "in_progress" and task["claim"]["claim_id"] == "new-generation"


@requires_dispatch_apply
def test_ctrl_c_terminates_children_and_releases_claim(apply_target, capsys, monkeypatch):
    from brigade.work_cmd import dispatch_apply as apply

    monkeypatch.setenv("FAKE_DISPATCH_SLEEP", "60")
    refresh = apply.Apply.refresh

    def interrupt(self, path, row, session):
        if row["state"] == "running":
            raise KeyboardInterrupt()
        return refresh(self, path, row, session)

    monkeypatch.setattr(apply.Apply, "refresh", interrupt)
    _apply(apply_target, capsys, expected=130)
    (intent,) = _intents(apply_target)
    assert intent["state"] == "canceled"
    assert not runguard._pid_is_active(intent["pid"])
    assert work_cmd._read_task_ledger(apply_target)["tasks"][0]["status"] == "pending"


@requires_dispatch_apply
def test_deadline_cancels_child(apply_target, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DISPATCH_SLEEP", "60")
    _apply(apply_target, capsys, "--deadline", "0.3", expected=1)
    (intent,) = _intents(apply_target)
    assert intent["terminal"]["run_status"] == "timeout"
    assert not runguard._pid_is_active(intent["pid"])


@requires_dispatch_apply
def test_max_total_denial_uses_budget_payload(apply_target, capsys):
    result = _apply(apply_target, capsys, "--max-total", "0")
    assert _intents(apply_target) == []
    directory = apply_target / ".brigade/dispatch" / result["dispatch_id"]
    rows = [json.loads(line) for line in (directory / "events.jsonl").read_text().splitlines()]
    (payload,) = [r["payload"] for r in rows if r["event_type"] == "run_budget.reservation_denied"]
    assert payload == {
        "dimension": "worker_dispatch_count",
        "mode": "enforceable",
        "declared": 0,
        "used": 0,
        "remaining": 0,
        "request_id": result["dispatch_id"],
        "reason_class": "reservation_denied",
    }


@requires_dispatch_apply
def test_live_child_consumes_capacity_in_next_plan(apply_target, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DISPATCH_SLEEP", "4")
    _tasks(apply_target, [["a.py"], ["b.py"]])
    _apply(apply_target, capsys, "--max", "1", "--no-wait")
    assert cli.main(["work", "dispatch", "--target", str(apply_target), "--max", "1", "--dry-run", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)["plan"]
    assert plan["live_children"] == 1 and plan["capacity"] == 0
    _apply(apply_target, capsys, "--max", "0")


@requires_process_groups
@requires_dispatch_apply
def test_orphaned_child_is_finalized_and_claim_released(apply_target, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DISPATCH_SLEEP", "60")
    _apply(apply_target, capsys, "--no-wait")
    (intent,) = _intents(apply_target)
    run_dir = Path(intent["run_dir"])
    deadline = time.monotonic() + 5
    while not (run_dir / "run.json").exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    os.killpg(intent["pid"], 9)
    os.waitpid(intent["pid"], 0)
    _apply(apply_target, capsys, "--max", "0", expected=1)
    assert _intents(apply_target)[0]["state"] == "orphaned"
    assert json.loads((run_dir / "run.json").read_text())["status"] == "orphaned"
    assert work_cmd._read_task_ledger(apply_target)["tasks"][0]["status"] == "pending"


@requires_dispatch_apply
def test_status_show_reads_intents_and_receipts(apply_target, capsys):
    result = _apply(apply_target, capsys)
    assert cli.main(["work", "dispatch-show", result["dispatch_id"], "--target", str(apply_target), "--json"]) == 0
    (session,) = json.loads(capsys.readouterr().out)["sessions"]
    assert session["intents"][0]["state"] == "completed"
    assert session["events"][-1]["payload"]["state"] == "completed"


@requires_dispatch_apply
def test_spawn_error_is_receipted_released_and_retried(apply_target, capsys, monkeypatch):
    popen = subprocess.Popen

    def unavailable(argv, *args, **kwargs):
        if "brigade.work_cmd.dispatch_child" in argv:
            raise OSError("child launcher unavailable")
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", unavailable)
    _apply(apply_target, capsys, expected=1)
    rows = _intents(apply_target)
    assert len(rows) == 3
    assert all(row["state"] == "failed" and row["terminal"]["infrastructure"] for row in rows)
    assert work_cmd._read_task_ledger(apply_target)["tasks"][0]["status"] == "pending"


@requires_dispatch_apply
def test_partial_event_tail_recovers_without_duplicate_child(apply_target, capsys, monkeypatch):
    from brigade.work_cmd import dispatch_state as store

    transition = store.transition
    with monkeypatch.context() as patch:

        def crash(path, row, state, **updates):
            transition(path, row, state, **updates)
            if state == "claimed":
                raise DispatchCrash()

        patch.setattr(store, "transition", crash)
        with pytest.raises(DispatchCrash):
            cli.main(["work", "dispatch", "--target", str(apply_target), "--worker", "coder"])
    capsys.readouterr()
    (events,) = (apply_target / ".brigade/dispatch").glob("dispatch-*/events.jsonl")
    with events.open("ab") as stream:
        stream.write(b'{"sequence":')
    _apply(apply_target, capsys)
    assert store.events(events.parent)
    (intent,) = _intents(apply_target)
    assert (Path(intent["run_dir"]) / "launch-count").read_text() == "launch\n"


@requires_dispatch_apply
def test_full_event_digest_corruption_refuses_resume(apply_target, capsys, monkeypatch):
    from brigade.work_cmd import dispatch_state as store

    event = store.event
    with monkeypatch.context() as patch:

        def crash(directory, name, payload):
            event(directory, name, payload)
            raise DispatchCrash()

        patch.setattr(store, "event", crash)
        with pytest.raises(DispatchCrash):
            cli.main(["work", "dispatch", "--target", str(apply_target), "--worker", "coder"])
    capsys.readouterr()
    (events,) = (apply_target / ".brigade/dispatch").glob("dispatch-*/events.jsonl")
    events.write_text(events.read_text().replace("work.dispatch.planned", "work.dispatch.changed"))
    _apply(apply_target, capsys, expected=2)
    assert not list((apply_target / ".brigade/runs").glob("*/launch-count"))


@requires_dispatch_apply
def test_terminal_run_waits_for_child_exit_before_closeout(apply_target, capsys, monkeypatch, tmp_path):
    from brigade.work_cmd import dispatch_state as store

    release_file = tmp_path / "release-terminal-child"
    monkeypatch.setenv("FAKE_DISPATCH_TERMINAL_RELEASE_FILE", str(release_file))
    try:
        _apply(apply_target, capsys, "--no-wait")
        (intent,) = _intents(apply_target)
        run_dir = Path(intent["run_dir"])
        deadline = time.monotonic() + 5
        meta = None
        while time.monotonic() < deadline:
            meta = localio.read_json_dict(run_dir / "run.json")
            if meta and meta.get("status") == "ok":
                break
            time.sleep(0.01)
        assert meta and meta.get("status") == "ok"
        assert store.child_alive(intent)

        _apply(apply_target, capsys, "--no-wait", "--max", "0")
        assert _intents(apply_target)[0]["state"] == "running"
        assert store.child_alive(intent)

        release_file.touch()
        deadline = time.monotonic() + 5
        while store.child_alive(intent) and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not store.child_alive(intent)
        _apply(apply_target, capsys, "--max", "0")
        assert _intents(apply_target)[0]["state"] == "completed"
    finally:
        release_file.touch()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            rows = _intents(apply_target)
            if not rows or not store.child_alive(rows[0]):
                break
            time.sleep(0.01)


@requires_dispatch_apply
def test_branch_head_drift_is_kept_for_review(apply_target, capsys, monkeypatch):
    monkeypatch.setenv("FAKE_DISPATCH_STATUS", "failed")
    monkeypatch.setenv("FAKE_DISPATCH_KIND", "branch-head-drift")
    _apply(apply_target, capsys)
    (intent,) = _intents(apply_target)
    assert intent["state"] == "completed" and intent["terminal"]["changes"]["needs_review"]
    assert work_cmd._read_task_ledger(apply_target)["tasks"][0]["status"] == "in_progress"


@requires_dispatch_apply
def test_spawn_delay_uses_remaining_interval(apply_target, capsys, monkeypatch):
    from types import SimpleNamespace
    from brigade.work_cmd import dispatch_apply as apply

    _tasks(apply_target, [["a.py"], ["b.py"]])
    clock = [0.0]
    sleeps = []
    launches = []
    original = apply._new_intent

    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
        time.sleep(min(seconds, 0.02))

    def record(*args):
        launches.append(clock[0])
        return original(*args)

    monkeypatch.setattr(apply, "time", SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep))
    monkeypatch.setattr(apply, "_new_intent", record)
    _apply(apply_target, capsys, "--spawn-delay", "2")
    assert launches[1] - launches[0] >= 2
    assert 2 in sleeps


@requires_dispatch_apply
def test_resume_cancellation_waits_for_adopted_child(apply_target, capsys, monkeypatch):
    from brigade.work_cmd import dispatch_apply as apply

    monkeypatch.setenv("FAKE_DISPATCH_SLEEP", "60")
    _apply(apply_target, capsys, "--no-wait")
    (intent,) = _intents(apply_target)
    refresh = apply.Apply.refresh

    def interrupt(self, path, row, session):
        if row["state"] == "running":
            raise KeyboardInterrupt()
        return refresh(self, path, row, session)

    monkeypatch.setattr(apply.Apply, "refresh", interrupt)
    _apply(apply_target, capsys, expected=130)
    assert not runguard._pid_is_active(intent["pid"])
    assert _intents(apply_target)[0]["state"] == "canceled"


@requires_dispatch_apply
def test_terminal_journal_wins_over_nonterminal_snapshot(apply_target, capsys):
    from brigade import run_journal

    _apply(apply_target, capsys)
    (path,) = (apply_target / ".brigade/dispatch").glob("dispatch-*/intents/*.json")
    row = json.loads(path.read_text())
    run_dir = Path(row["run_dir"])
    run_journal.append_event(
        run_dir / "events/lifecycle.jsonl",
        run_id=run_dir.name,
        event_type="run.completed",
        payload={"status": "ok"},
        idempotency_key="finished",
        expected_previous_sequence=0,
    )
    meta = json.loads((run_dir / "run.json").read_text())
    meta.pop("finished_at")
    meta["status"] = "running"
    localio.write_json(run_dir / "run.json", meta)
    row["state"] = "running"
    localio.write_json(path, row)
    session_path = path.parent.parent / "session.json"
    session = json.loads(session_path.read_text())
    session.pop("finished_at")
    localio.write_json(session_path, session)
    _apply(apply_target, capsys, "--max", "0")
    assert _intents(apply_target)[0]["state"] == "completed"


def test_dispatch_lock_refuses_independent_process(apply_target, capsys):
    holder = (
        "from pathlib import Path\nfrom brigade.work_cmd.dispatch_state import dispatch_lock\n"
        "import sys\nwith dispatch_lock(Path(sys.argv[1])):\n"
        ' print("ready", flush=True)\n sys.stdin.readline()\n'
    )
    contender = (
        "from pathlib import Path\nfrom brigade.work_cmd.dispatch_state import dispatch_lock\n"
        "import sys\ntry:\n with dispatch_lock(Path(sys.argv[1])): pass\n"
        "except ValueError as exc:\n print(exc)\n sys.exit(0)\n"
        "sys.exit(1)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", holder, str(apply_target)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout.readline().strip() == "ready"
        result = subprocess.run(
            [sys.executable, "-c", contender, str(apply_target)], capture_output=True, text=True, check=True
        )
        assert f"dispatch.lock is held; last recorded owner pid {child.pid}" in result.stdout
    finally:
        child.communicate("\n", timeout=5)


@requires_dispatch_apply
def test_duplicate_bootstrap_never_executes_same_child_twice(apply_target, capsys):
    _apply(apply_target, capsys)
    (intent,) = _intents(apply_target)
    run_dir = Path(intent["run_dir"])
    argv = json.loads((run_dir / "argv.json").read_text())
    from brigade.work_cmd import dispatch_apply as apply, dispatch_state as store

    session = store.read(next((apply_target / ".brigade/dispatch").glob("dispatch-*/session.json")))
    command = apply._child_argv(intent, session, argv[-1])
    child = subprocess.run(
        [sys.executable, "-m", "brigade.work_cmd.dispatch_child", str(run_dir), *command], check=False
    )
    assert child.returncode == 0
    assert (run_dir / "launch-count").read_text() == "launch\n"


@requires_dispatch_apply
def test_local_commit_runs_only_after_child_exit(apply_target, capsys):
    subprocess.run(["git", "config", "user.name", "Test"], cwd=apply_target, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=apply_target, check=True)
    _apply(apply_target, capsys, "--commit")
    (intent,) = _intents(apply_target)
    assert intent["terminal"]["changes"]["commit"]
    assert not subprocess.check_output(["git", "diff", "--stat"], cwd=intent["worktree"], text=True)
    assert (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=apply_target, text=True).strip() == intent["base_sha"]
    )


@requires_dispatch_apply
def test_resumed_intent_does_not_launch_after_claim_reassignment(apply_target, capsys, monkeypatch):
    from brigade.work_cmd import dispatch_state as store

    transition = store.transition
    with monkeypatch.context() as patch:

        def crash(path, row, state, **updates):
            transition(path, row, state, **updates)
            if state == "claimed":
                raise DispatchCrash()

        patch.setattr(store, "transition", crash)
        with pytest.raises(DispatchCrash):
            cli.main(["work", "dispatch", "--target", str(apply_target), "--worker", "coder"])
    capsys.readouterr()
    ledger = work_cmd._read_task_ledger(apply_target)
    ledger["tasks"][0]["claim"].update(actor="other", claim_id="new-generation")
    work_cmd._write_task_ledger(apply_target, ledger)
    _apply(apply_target, capsys, "--max", "0")
    (intent,) = _intents(apply_target)
    assert intent["state"] == "lost_race"
    assert not list((apply_target / ".brigade/runs").glob("*/launch-count"))
    assert work_cmd._read_task_ledger(apply_target)["tasks"][0]["claim"]["claim_id"] == "new-generation"


@requires_dispatch_apply
def test_child_rejected_before_run_metadata_is_infrastructure(apply_target, capsys):
    executable = apply_target / "bin/fake_run.py"
    executable.write_text(f"#!{sys.executable}\nraise SystemExit(2)\n")
    _apply(apply_target, capsys, expected=1)
    rows = _intents(apply_target)
    assert len(rows) == 3
    assert all(row["state"] == "failed" and row["terminal"]["infrastructure"] for row in rows)


@requires_dispatch_apply
def test_expired_session_does_not_spawn_a_saved_planned_intent(apply_target, capsys, monkeypatch):
    from brigade.work_cmd import dispatch_state as store

    transition = store.transition
    with monkeypatch.context() as patch:

        def crash(path, row, state, **updates):
            transition(path, row, state, **updates)
            if state == "planned":
                raise DispatchCrash()

        patch.setattr(store, "transition", crash)
        with pytest.raises(DispatchCrash):
            cli.main(["work", "dispatch", "--target", str(apply_target), "--worker", "coder", "--deadline", "10"])
    capsys.readouterr()
    (session_path,) = (apply_target / ".brigade/dispatch").glob("dispatch-*/session.json")
    session = json.loads(session_path.read_text())
    session["created_at"] = "2026-01-01T00:00:00+00:00"
    localio.write_json(session_path, session)
    launches = []
    popen = subprocess.Popen

    def record(argv, *args, **kwargs):
        if "brigade.work_cmd.dispatch_child" in argv:
            launches.append(argv)
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", record)
    _apply(apply_target, capsys, "--max", "0", expected=1)
    assert launches == []
    assert work_cmd._read_task_ledger(apply_target)["tasks"][0]["status"] == "pending"


@pytest.mark.parametrize("reason", ["SIGTERM", "version-changed"])
def test_stop_during_prompt_preparation_leaves_claim_resumable(apply_target, capsys, monkeypatch, reason):
    from brigade.work_cmd import dispatch_apply as apply, dispatch_state as store
    from brigade.work_cmd.dispatch_watch import Watch

    _apply(apply_target, capsys, "--dry-run")
    (directory,) = store.sessions(apply_target)
    session = store.read(directory / "session.json")
    session.update(dry_run=False, no_wait=True)
    localio.write_json(directory / "session.json", session)
    plan = store.read(directory / "plan.json")
    engine = apply.Apply(apply_target)
    watcher = Watch(engine, directory, session, interval=0.1, drain_wait=False)
    engine.can_launch = watcher.can_launch
    engine.reservation_allowed = watcher.reserve
    render = apply._render_task_run_prompt
    launches = []
    popen = subprocess.Popen

    def stop_preparing(task):
        watcher.reason = reason
        return render(task)

    def counted(argv, *args, **kwargs):
        if "brigade.work_cmd.dispatch_child" in argv:
            launches.append(argv)
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", counted)
    monkeypatch.setattr(apply, "_render_task_run_prompt", stop_preparing)
    path, row = apply._new_intent(apply_target, directory, session, plan, plan["selected"][0])
    engine.advance(path, row, session)
    assert not launches
    saved = store.read(path)
    assert saved["state"] == "launching" and not saved["pid"]
    assert not saved.get("released")
    claim = apply.locking._read_task_ledger(apply_target)["tasks"][0]["claim"]["claim_id"]
    assert claim == saved["claim_id"]
    watcher.count_launches(watcher.snapshot())
    assert watcher.launched() == 0

    monkeypatch.setattr(apply, "_render_task_run_prompt", render)
    assert apply.Apply(apply_target).session(directory) == 0
    saved = store.read(path)
    assert saved["state"] == "completed" and saved["claim_id"] == claim
    assert len(launches) == 1
    assert (Path(saved["run_dir"]) / "launch-count").read_text() == "launch\n"
