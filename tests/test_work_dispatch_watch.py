"""Supervised dispatch and receipt regressions with no real worker launches."""
# ruff: noqa: F811

from __future__ import annotations

import json
import os

import pytest

from brigade import cli, localio, work_cmd
from brigade.work_cmd import claiming, dispatch_apply as apply, dispatch_state as store
from tests.test_work_dispatch import _tasks, dispatch_target  # noqa: F401
from tests.test_work_dispatch_apply import apply_target  # noqa: F401


def test_non_race_claim_refusal_is_not_retried_as_infrastructure(apply_target, monkeypatch, capsys):
    def refuse(*args, **kwargs):
        raise claiming.ClaimError("start gate refused", reason="not_ready", exit_code=2)

    monkeypatch.setattr(apply.locking, "_claim_task", refuse)
    assert cli.main(["work", "dispatch", "--target", str(apply_target), "--worker", "coder"]) == 1
    capsys.readouterr()
    rows = store.intents(apply_target)
    assert len(rows) == 1
    terminal = rows[0][1]["terminal"]
    assert terminal["failure_kind"] == "claim-refused"
    assert terminal["infrastructure"] is False


def test_reset_prefers_exact_task_id(dispatch_target, capsys):
    tasks = _tasks(dispatch_target, [["a.py"], ["b.py"]])
    tasks[1]["id"] = "task-0-extra"
    work_cmd._write_task_ledger(dispatch_target, {"version": 2, "tasks": tasks, "edges": []})
    assert store.reset(target=dispatch_target, task_id="task-0", reason="inspected", json_output=True) == 0
    assert json.loads(capsys.readouterr().out)["task_id"] == "task-0"


def test_intent_events_reconstruct_identity_and_terminal_taxonomy(tmp_path):
    path = tmp_path / "intents" / "intent.json"
    path.parent.mkdir()
    row = dict(intent_id="intent", task_id="task", claim_id="claim", run_id="run", branch="branch")
    terminal = dict(failure_kind="claim-refused", failure_phase="dispatch", infrastructure=False)
    store.transition(path, row, "failed", terminal=terminal)
    payload = store.events(tmp_path)[0]["payload"]
    for key in ("task_id", "claim_id", "run_id", "branch"):
        assert payload[key] == row[key]
    assert payload["terminal"] == terminal


def test_session_event_read_is_bounded_before_allocation(tmp_path, monkeypatch):
    (tmp_path / "events.jsonl").write_bytes(b"x" * 100)
    monkeypatch.setattr(store, "MAX_EVENT_BYTES", 64, raising=False)
    with pytest.raises(ValueError, match="bound"):
        store.events(tmp_path, repair_tail=True)
    assert (tmp_path / "events.jsonl").stat().st_size == 100


def test_session_show_reads_verified_prefix_with_partial_tail(dispatch_target, capsys):
    directory = store.root(dispatch_target) / "dispatch-test"
    directory.mkdir(parents=True)
    localio.write_json(directory / "session.json", {"created_at": "2026-01-01", "dispatch_id": "dispatch-test"})
    localio.write_json(directory / "plan.json", {})
    store.event(directory, "work.dispatch.planned", {})
    with (directory / "events.jsonl").open("ab") as stream:
        stream.write(b'{"partial":')
    assert store.status(target=dispatch_target, dispatch_id="latest", json_output=True) == 0
    assert len(json.loads(capsys.readouterr().out)["sessions"][0]["events"]) == 1


@pytest.fixture
def fake_watch(apply_target, monkeypatch):
    import signal
    from types import SimpleNamespace
    from brigade.work_cmd import dispatch_watch as watch

    clock = [0.0]
    sleeps = []
    launches = []
    hooks = []

    def sleep(seconds):
        assert 0 < seconds <= 0.1
        clock[0] += seconds
        sleeps.append(seconds)
        for hook in list(hooks):
            hook()

    fake_time = SimpleNamespace(monotonic=lambda: clock[0], sleep=sleep)
    monkeypatch.setattr(watch, "time", fake_time)
    monkeypatch.setattr(apply, "time", fake_time)
    monkeypatch.setattr(watch, "installed_version", lambda: "test-version")
    monkeypatch.setattr(store, "child_alive", lambda row: row["state"] == "running")

    def advance(engine, path, row, session):
        if row["state"] != "planned":
            return
        claim = apply.locking._claim_task(engine.target, row["task_id"], actor=row["seat"], claim_id=row["claim_id"])
        store.transition(
            path,
            row,
            "running",
            run_id=row["intent_id"],
            run_dir=str(apply_target / "runs" / row["intent_id"]),
            claimed_item_revision=claim["claim"]["item_revision"],
            launched_at=clock[0],
            pid=123,
        )
        engine.last_launch = clock[0]
        launches.append((row["task_id"], clock[0]))

    def refresh(engine, path, row, session):
        if row["state"] == "running" and clock[0] >= row["launched_at"] + 0.5:
            store.transition(path, row, "completed", terminal={"run_status": "ok"})
        return row["state"] in store.TERMINAL

    monkeypatch.setattr(apply.Apply, "_advance", advance)
    monkeypatch.setattr(apply.Apply, "_refresh", refresh)
    monkeypatch.setattr(apply.Apply, "observe", lambda self: None)

    def stop(signum=signal.SIGTERM):
        assert callable(signal.getsignal(signum))
        os.kill(os.getpid(), signum)

    return SimpleNamespace(clock=clock, sleeps=sleeps, launches=launches, hooks=hooks, stop=stop, module=watch)


def _watch(target, capsys, *options):
    code = cli.main(["work", "dispatch", "--watch", "--target", str(target), "--worker", "coder", "--json", *options])
    captured = capsys.readouterr()
    assert code == 0, captured
    payload = json.loads(captured.out)
    return store.root(target) / payload["dispatch_id"]


def test_watch_refills_capacity_records_ticks_and_drains(apply_target, fake_watch, capsys):
    _tasks(apply_target, [["a.py"], ["b.py"], ["c.py"]])
    directory = _watch(apply_target, capsys, "--max", "1", "--interval", "0.2", "--max-total", "3", "--drain-wait")
    assert [task for task, _ in fake_watch.launches] == ["task-0", "task-1", "task-2"]
    assert all(row["state"] == "completed" for _, row in store.intents(apply_target))
    events = store.events(directory)
    ticks = [row["payload"] for row in events if row["event_type"] == "work.dispatch.tick"]
    assert sum(len(tick["launched"]) for tick in ticks) == 3
    assert sum(len(tick["finalized"]) for tick in ticks) == 3
    assert events[-1]["event_type"] == "work.dispatch.closed"
    assert events[-1]["payload"]["reason"] == "max-total"
    assert fake_watch.sleeps


@pytest.mark.parametrize("drain", [False, True])
@pytest.mark.parametrize("signame", ["SIGTERM", "SIGINT"])
def test_watch_signals_stop_launches_and_restore_handlers(apply_target, fake_watch, capsys, drain, signame):
    import signal

    _tasks(apply_target, [["a.py"], ["b.py"]])
    signum = getattr(signal, signame)
    previous = signal.getsignal(signum)

    def stop_once():
        if fake_watch.launches:
            fake_watch.hooks.remove(stop_once)
            fake_watch.stop(signum)

    fake_watch.hooks.append(stop_once)
    directory = _watch(apply_target, capsys, "--max", "1", *(["--drain-wait"] if drain else []))
    assert len(fake_watch.launches) == 1
    assert signal.getsignal(signum) == previous
    assert store.intents(apply_target)[0][1]["state"] == ("completed" if drain else "running")
    assert store.read(directory / "session.json")["close_reason"] == signame


def test_watch_version_change_closes_without_another_launch(apply_target, fake_watch, capsys):
    _tasks(apply_target, [["a.py"], ["b.py"]])
    fake_watch.module.installed_version = lambda: "changed" if fake_watch.clock[0] > 0 else "test-version"
    directory = _watch(apply_target, capsys, "--max", "1", "--interval", "0.2")
    assert len(fake_watch.launches) == 1
    assert store.read(directory / "session.json")["close_reason"] == "version-changed"


def test_watch_idle_sleeps_and_holds_lease(apply_target, fake_watch, capsys):
    _tasks(apply_target, [])
    checked = []

    def probe():
        with pytest.raises(ValueError, match="dispatch.lock is held"):
            with store.dispatch_lock(apply_target):
                pass
        checked.append(True)
        fake_watch.stop()

    fake_watch.hooks.append(probe)
    _watch(apply_target, capsys)
    assert checked and fake_watch.sleeps
    with store.dispatch_lock(apply_target):
        pass


def test_watch_spawn_delay_can_be_interrupted_before_next_launch(apply_target, fake_watch, capsys):
    _tasks(apply_target, [["a.py"], ["b.py"]])
    fake_watch.hooks.append(lambda: fake_watch.stop() if fake_watch.launches else None)
    _watch(apply_target, capsys, "--spawn-delay", "2")
    assert len(fake_watch.launches) == 1


def test_watch_spawn_delay_spans_ticks(apply_target, fake_watch, capsys):
    _tasks(apply_target, [["a.py"], ["b.py"]])
    _watch(apply_target, capsys, "--max", "1", "--max-total", "2", "--spawn-delay", "1", "--interval", "0.2")
    assert fake_watch.launches[1][1] - fake_watch.launches[0][1] >= 1


@pytest.mark.parametrize(
    "options",
    [["--interval", "0"], ["--interval", "nan"], ["--watch", "--dry-run"], ["--watch", "--no-wait"], ["--drain-wait"]],
)
def test_watch_rejects_invalid_modes_before_receipts(dispatch_target, options, capsys):
    assert cli.main(["work", "dispatch", "--target", str(dispatch_target), "--worker", "coder", *options]) == 2
    capsys.readouterr()
    assert not store.sessions(dispatch_target)


def test_watch_deadline_is_session_wide_and_sleeps_when_idle(apply_target, fake_watch, capsys):
    _tasks(apply_target, [])
    directory = _watch(apply_target, capsys, "--deadline", "0.3", "--interval", "0.2")
    assert fake_watch.clock[0] >= 0.3
    assert store.read(directory / "session.json")["close_reason"] == "deadline"
    denied = [row for row in store.events(directory) if row["event_type"] == "run_budget.reservation_denied"]
    assert len(denied) == 1
    assert denied[0]["payload"]["dimension"] == "wall_clock_seconds"


def test_watch_waits_for_pinned_wave_to_exist(apply_target, fake_watch, capsys):
    _tasks(apply_target, [])
    fake_watch.hooks.append(fake_watch.stop)
    directory = _watch(apply_target, capsys, "--wave", "1")
    assert not fake_watch.launches
    assert store.read(directory / "session.json")["close_reason"] == "SIGTERM"


def test_event_append_refuses_bounds_without_damaging_prefix(tmp_path, monkeypatch):
    store.event(tmp_path, "work.dispatch.planned", {})
    original = (tmp_path / "events.jsonl").read_bytes()
    monkeypatch.setattr(store, "MAX_EVENTS", 1)
    with pytest.raises(ValueError, match="bound"):
        store.event(tmp_path, "work.dispatch.closed", {})
    assert (tmp_path / "events.jsonl").read_bytes() == original


def test_watch_does_not_restart_exhausted_infrastructure_task_each_tick(apply_target, fake_watch, monkeypatch, capsys):
    def fail(engine, path, row, session):
        if row["state"] == "running" and fake_watch.clock[0] >= row["launched_at"] + 0.5:
            store.transition(path, row, "failed", terminal={"failure_kind": "provider-error", "infrastructure": True})
            apply._release(engine.target, row)
        return row["state"] in store.TERMINAL

    monkeypatch.setattr(apply.Apply, "_refresh", fail)
    assert (
        cli.main(
            [
                "work",
                "dispatch",
                "--watch",
                "--target",
                str(apply_target),
                "--worker",
                "coder",
                "--max",
                "1",
                "--interval",
                "0.2",
                "--deadline",
                "3",
            ]
        )
        == 1
    )
    capsys.readouterr()
    assert len(fake_watch.launches) == 3


def test_watch_spaces_resumed_prelaunch_intents(apply_target, fake_watch, capsys):
    _tasks(apply_target, [["a.py"], ["b.py"]])
    assert (
        cli.main(
            ["work", "dispatch", "--dry-run", "--target", str(apply_target), "--worker", "coder", "--spawn-delay", "1"]
        )
        == 0
    )
    capsys.readouterr()
    (directory,) = store.sessions(apply_target)
    session = store.read(directory / "session.json")
    session["dry_run"] = False
    localio.write_json(directory / "session.json", session)
    plan = store.read(directory / "plan.json")
    for item in plan["selected"]:
        apply._new_intent(apply_target, directory, session, plan, item)
    _watch(apply_target, capsys, "--max-total", "2", "--spawn-delay", "1")
    assert fake_watch.launches[1][1] - fake_watch.launches[0][1] >= 1


def test_watch_does_not_report_unspawned_run_directory_as_launch(apply_target, fake_watch, monkeypatch, capsys):
    def interrupted(engine, path, row, session):
        store.transition(path, row, "workspace_ready", run_id="reserved", run_dir=str(apply_target / "reserved"))
        fake_watch.stop()

    monkeypatch.setattr(apply.Apply, "_advance", interrupted)
    directory = _watch(apply_target, capsys)
    ticks = [row["payload"] for row in store.events(directory) if row["event_type"] == "work.dispatch.tick"]
    assert not any(tick["launched"] for tick in ticks)


def test_watch_pause_never_sleeps_negative_when_clock_crosses_boundary(apply_target, fake_watch, monkeypatch):
    from types import SimpleNamespace

    watcher = fake_watch.module.Watch(apply.Apply(apply_target), apply_target, {}, interval=0.1, drain_wait=False)
    values = iter([0, 0.05, 0.2])
    sleeps = []

    def sleep(seconds):
        assert seconds > 0
        sleeps.append(seconds)

    monkeypatch.setattr(fake_watch.module, "time", SimpleNamespace(monotonic=lambda: next(values, 0.2), sleep=sleep))
    watcher.pause(0.1)
    assert sum(sleeps) <= 0.1


def test_watch_deadline_drain_does_not_cancel_children(apply_target, fake_watch, monkeypatch, capsys):
    def cancel(*args, **kwargs):
        pytest.fail("drain must observe the running child without cancellation")

    def expire_saved_deadline():
        for directory in store.sessions(apply_target):
            session = store.read(directory / "session.json")
            if session.get("parent_watch"):
                session["deadline"] = 0
                localio.write_json(directory / "session.json", session)

    fake_watch.hooks.append(expire_saved_deadline)
    monkeypatch.setattr(apply.Apply, "cancel", cancel)
    directory = _watch(apply_target, capsys, "--deadline", "0.1", "--drain-wait", "--interval", "0.1")
    assert store.read(directory / "session.json")["close_reason"] == "deadline"
    assert store.intents(apply_target)[0][1]["state"] == "completed"


def test_later_invocation_keeps_detached_watch_children_after_deadline(apply_target, fake_watch, monkeypatch, capsys):
    def cancel(*args, **kwargs):
        pytest.fail("an expired watch must not cancel detached children on later reconciliation")

    monkeypatch.setattr(apply.Apply, "cancel", cancel)
    directory = _watch(apply_target, capsys, "--deadline", "0.1", "--max-total", "2", "--interval", "0.1")
    (tick_dir,) = [path for path in store.sessions(apply_target) if path != directory]
    session = store.read(tick_dir / "session.json")
    session["created_at"] = "2000-01-01T00:00:00+00:00"
    localio.write_json(tick_dir / "session.json", session)

    assert apply.Apply(apply_target).reconcile(no_wait=True) == 0
    assert store.intents(apply_target)[0][1]["state"] == "running"
    assert session["deadline"] is None
    assert session["max_total"] is None
    (task,) = work_cmd._read_task_ledger(apply_target)["tasks"]
    assert task["claim"]


@pytest.mark.parametrize("signame", ["SIGINT", "SIGTERM"])
def test_second_signal_ends_drain_wait_and_records_close(apply_target, fake_watch, capsys, signame):
    import signal

    signum = getattr(signal, signame)
    previous = signal.getsignal(signum)
    signals = []

    def stop_twice():
        signals.append(fake_watch.clock[0])
        if len(signals) == 2:
            fake_watch.hooks.remove(stop_twice)
        fake_watch.stop(signum)

    fake_watch.hooks.append(stop_twice)
    directory = _watch(apply_target, capsys, "--drain-wait", "--interval", "0.1")
    assert len(signals) == 2
    assert fake_watch.clock[0] < 0.5
    assert store.intents(apply_target)[0][1]["state"] == "running"
    session = store.read(directory / "session.json")
    assert session["finished_at"] and session["close_reason"] == signame
    closed = store.events(directory)[-1]
    assert closed["event_type"] == "work.dispatch.closed"
    assert closed["payload"]["live_children"] == 1
    assert signal.getsignal(signum) == previous


@pytest.mark.parametrize("failure_at", ["advance", "reconcile"])
def test_watch_records_tick_error_and_continues_without_canceling_siblings(
    apply_target, fake_watch, monkeypatch, capsys, failure_at
):
    _tasks(apply_target, [["a.py"], ["b.py"], ["c.py"]])
    advance, refresh = apply.Apply._advance, apply.Apply._refresh
    failures = []

    def broken_advance(engine, path, row, session):
        if failure_at == "advance" and row["task_id"] == "task-1" and not failures:
            failures.append(row["intent_id"])
            raise KeyError("damaged intent")
        return advance(engine, path, row, session)

    def broken_refresh(engine, path, row, session):
        if failure_at == "reconcile" and row["task_id"] == "task-0" and fake_watch.clock[0] >= 0.2 and not failures:
            failures.append(row["intent_id"])
            raise KeyError("damaged intent")
        return refresh(engine, path, row, session)

    def cancel(*args, **kwargs):
        pytest.fail("a tick error must preserve children and their claims")

    monkeypatch.setattr(apply.Apply, "_advance", broken_advance)
    monkeypatch.setattr(apply.Apply, "_refresh", broken_refresh)
    monkeypatch.setattr(apply.Apply, "cancel", cancel)

    def stop_once():
        if fake_watch.clock[0] >= 1.5:
            fake_watch.hooks.remove(stop_once)
            fake_watch.stop()

    fake_watch.hooks.append(stop_once)
    assert (
        cli.main(
            [
                "work",
                "dispatch",
                "--watch",
                "--target",
                str(apply_target),
                "--worker",
                "coder",
                "--max",
                "2",
                "--interval",
                "0.2",
                "--json",
            ]
        )
        == 1
    )
    directory = store.root(apply_target) / json.loads(capsys.readouterr().out)["dispatch_id"]
    assert failures
    assert len(fake_watch.launches) == 3
    assert all(row["state"] == "completed" for _, row in store.intents(apply_target))
    assert all(task["claim"] for task in work_cmd._read_task_ledger(apply_target)["tasks"])
    errors = [
        event
        for path in store.sessions(apply_target)
        for event in store.events(path)
        if event["event_type"] == "work.dispatch.tick_error"
    ]
    assert len(errors) == 1
    assert errors[0]["payload"]["error_type"] == "KeyError"
    assert "damaged intent" in errors[0]["payload"]["error"]
    assert store.read(directory / "session.json")["close_reason"] == "SIGTERM"


@pytest.mark.parametrize("state", ["claimed", "workspace_ready"])
def test_watch_close_reports_claimed_prelaunch_intents(apply_target, fake_watch, monkeypatch, capsys, state):
    def interrupted(engine, path, row, session):
        apply.locking._claim_task(engine.target, row["task_id"], actor=row["seat"], claim_id=row["claim_id"])
        store.transition(path, row, state)
        fake_watch.stop()

    monkeypatch.setattr(apply.Apply, "_advance", interrupted)
    directory = _watch(apply_target, capsys)
    closed = store.events(directory)[-1]["payload"]
    assert closed["live_children"] == 0
    assert closed["claimed_prelaunch_intents"] == 1
    (task,) = work_cmd._read_task_ledger(apply_target)["tasks"]
    assert task["claim"]


def test_watch_real_child_releases_each_intent_once_and_reports_outcome(apply_target, monkeypatch, capsys):
    from pathlib import Path

    monkeypatch.setenv("FAKE_DISPATCH_STATUS", "failed")
    claims, releases = [], []
    claim, release = apply.locking._claim_task, apply.locking._release_tasks

    def counted_claim(*args, **kwargs):
        result = claim(*args, **kwargs)
        claims.append(kwargs["claim_id"])
        return result

    def counted_release(*args, **kwargs):
        result = release(*args, **kwargs)
        releases.extend(kwargs["claim_ids"])
        return result

    monkeypatch.setattr(apply.locking, "_claim_task", counted_claim)
    monkeypatch.setattr(apply.locking, "_release_tasks", counted_release)
    assert (
        cli.main(
            [
                "work",
                "dispatch",
                "--watch",
                "--target",
                str(apply_target),
                "--worker",
                "coder",
                "--max",
                "1",
                "--interval",
                "0.05",
                "--max-total",
                "2",
                "--drain-wait",
                "--json",
            ]
        )
        == 1
    )
    payload = json.loads(capsys.readouterr().out)
    rows = store.intents(apply_target)
    assert len(rows) == 2
    assert len(claims) == len(set(claims)) == 2
    assert sorted(releases) == sorted(claims)
    assert all(row["state"] == "failed" and row["released"] for _, row in rows)
    assert all((Path(row["run_dir"]) / "launch-count").read_text() == "launch\n" for _, row in rows)
    assert payload["launched"] == 2
    assert payload["close_reason"] == "max-total"
    assert set(payload["tick_sessions"]) == {path.parent.parent.name for path, _ in rows}
    assert "plan" not in payload
    events = store.events(store.root(apply_target) / payload["dispatch_id"])
    ticks = [row["payload"] for row in events if row["event_type"] == "work.dispatch.tick"]
    assert sum(len(tick["launched"]) for tick in ticks) == 2
    assert sum(len(tick["finalized"]) for tick in ticks) == 2


def test_watch_pause_does_not_rescan_history_or_poll_metadata_on_every_wake(apply_target, fake_watch, monkeypatch):
    watcher = fake_watch.module.Watch(
        apply.Apply(apply_target), apply_target, {"max_total": 2}, interval=5, drain_wait=False
    )
    scans, versions = [], []
    intents = store.intents

    def scan(target):
        scans.append(target)
        return intents(target)

    def version():
        versions.append(fake_watch.clock[0])
        return "test-version"

    monkeypatch.setattr(store, "intents", scan)
    monkeypatch.setattr(fake_watch.module, "installed_version", version)
    watcher.pause(0.9)
    assert not scans
    assert len(versions) <= 1


def test_watch_ceiling_counts_launches_within_one_tick(apply_target, fake_watch, capsys):
    _tasks(apply_target, [["a.py"], ["b.py"], ["c.py"]])
    directory = _watch(apply_target, capsys, "--max", "3", "--max-total", "1")
    assert len(fake_watch.launches) == 1
    assert store.read(directory / "session.json")["launched"] == 1


def test_watch_ceiling_includes_resumed_reserved_children(apply_target, monkeypatch, capsys):
    import subprocess

    from tests.test_work_dispatch_apply import DispatchCrash

    _tasks(apply_target, [["a.py"], ["b.py"]])
    assert cli.main(["work", "dispatch", "--dry-run", "--target", str(apply_target), "--worker", "coder"]) == 0
    capsys.readouterr()
    (directory,) = store.sessions(apply_target)
    session = store.read(directory / "session.json")
    session.update(dry_run=False, no_wait=True)
    localio.write_json(directory / "session.json", session)
    plan = store.read(directory / "plan.json")
    engine = apply.Apply(apply_target)
    popen = subprocess.Popen

    def interrupted(argv, *args, **kwargs):
        if "brigade.work_cmd.dispatch_child" in argv:
            raise DispatchCrash()
        return popen(argv, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", interrupted)
        for item in plan["selected"]:
            path, row = apply._new_intent(apply_target, directory, session, plan, item)
            with pytest.raises(DispatchCrash):
                engine.advance(path, row, session)
    assert len(store.intents(apply_target)) == 2
    assert all(row["run_dir"] and not row["pid"] for _, row in store.intents(apply_target))

    watch_dir = _watch(apply_target, capsys, "--max-total", "1", "--drain-wait", "--interval", "0.05")
    rows = store.intents(apply_target)
    assert store.read(watch_dir / "session.json")["launched"] == 1
    assert sum(row["state"] == "completed" for _, row in rows) == 1
    assert sum(row["state"] == "launching" and not row["pid"] for _, row in rows) == 1


def test_watch_restart_excludes_bootstrapped_child_from_fresh_budget(apply_target, monkeypatch, capsys):
    import subprocess
    from pathlib import Path

    from tests.test_work_dispatch_apply import DispatchCrash

    _tasks(apply_target, [["a.py"], ["b.py"], ["c.py"]])
    assert cli.main(["work", "dispatch", "--dry-run", "--target", str(apply_target), "--worker", "coder"]) == 0
    capsys.readouterr()
    (directory,) = store.sessions(apply_target)
    session = store.read(directory / "session.json")
    session.update(dry_run=False, no_wait=True)
    localio.write_json(directory / "session.json", session)
    plan = store.read(directory / "plan.json")
    path, row = apply._new_intent(apply_target, directory, session, plan, plan["selected"][0])
    popen = subprocess.Popen

    def interrupted(argv, *args, **kwargs):
        if "brigade.work_cmd.dispatch_child" in argv:
            raise DispatchCrash()
        return popen(argv, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(subprocess, "Popen", interrupted)
        with pytest.raises(DispatchCrash):
            apply.Apply(apply_target).advance(path, row, session)
    assert row["state"] == "launching" and row["pid"] is None
    run_dir = Path(row["run_dir"])
    localio.write_json(run_dir / "dispatch-launch.json", {"pid": 2147483647, "process_token": "exited-child"})
    localio.write_json(
        run_dir / "run.json",
        {"status": "ok", "cwd": row["worktree"], "finished_at": localio.utc_now_iso()},
    )
    assert not store.child_alive(row)

    watch_dir = _watch(apply_target, capsys, "--max-total", "2", "--drain-wait", "--interval", "0.05")
    assert store.read(path)["pid"] == 2147483647  # Real reconciliation adopted the bootstrap identity.
    ticks = [event["payload"] for event in store.events(watch_dir) if event["event_type"] == "work.dispatch.tick"]
    launched = [intent for tick in ticks for intent in tick["launched"]]
    assert row["intent_id"] not in launched
    new_rows = [intent for _, intent in store.intents(apply_target) if intent["intent_id"] != row["intent_id"]]
    assert len(new_rows) == len(launched) == 2
    assert all(intent["pid"] and intent["state"] == "completed" for intent in new_rows)
    assert store.read(watch_dir / "session.json")["launched"] == 2
    assert store.read(watch_dir / "session.json")["close_reason"] == "max-total"


@pytest.mark.parametrize("pid", [True, False, 0, -1, "123", None, 1.5, {}])
@pytest.mark.parametrize("source", ["intent", "bootstrap"])
def test_watch_rejects_invalid_pid_launch_evidence(apply_target, capsys, pid, source):
    from brigade.work_cmd.dispatch_watch import Watch

    assert cli.main(["work", "dispatch", "--dry-run", "--target", str(apply_target), "--worker", "coder"]) == 0
    capsys.readouterr()
    (directory,) = store.sessions(apply_target)
    session, plan = store.read(directory / "session.json"), store.read(directory / "plan.json")
    path, row = apply._new_intent(apply_target, directory, session, plan, plan["selected"][0])
    run_dir = apply_target / "reserved"
    run_dir.mkdir()
    store.transition(path, row, "launching", run_dir=str(run_dir), pid=pid if source == "intent" else None)
    if source == "bootstrap":
        localio.write_json(run_dir / "dispatch-launch.json", {"pid": pid})
    engine = apply.Apply(apply_target)
    watcher = Watch(engine, directory, session, interval=0.1, drain_wait=False)
    assert not watcher.snapshot()[row["intent_id"]][0]
    if source == "bootstrap":
        assert engine.refresh(path, row, session)
        assert row["state"] == "unknown"
        assert not watcher.snapshot()[row["intent_id"]][0]


def test_event_appends_scan_existing_chain_once_per_lease(tmp_path, monkeypatch):
    directory = store.root(tmp_path) / "dispatch-cache"
    directory.mkdir(parents=True)
    store.event(directory, "work.dispatch.planned", {})
    validate = store.events
    scans = []

    def counted(path, **kwargs):
        scans.append((path / "events.jsonl").stat().st_size)
        return validate(path, **kwargs)

    monkeypatch.setattr(store, "events", counted)
    with store.dispatch_lock(tmp_path):
        for tick in range(30):
            store.event(directory, "work.dispatch.tick", {"tick": tick})
    assert len(scans) == 1
    assert len(validate(directory)) == 31
    with store.dispatch_lock(tmp_path):
        store.event(directory, "work.dispatch.closed", {})
    assert len(scans) == 2
    store.event(directory, "work.dispatch.closed", {})
    store.event(directory, "work.dispatch.closed", {})
    assert len(scans) == 4
    assert len(validate(directory)) == 34


@pytest.mark.parametrize("change", ["replace", "rewrite", "append", "truncate"])
def test_event_lease_revalidates_unexpected_file_changes(tmp_path, change):
    directory = store.root(tmp_path) / "dispatch-cache"
    directory.mkdir(parents=True)
    log = directory / "events.jsonl"
    with store.dispatch_lock(tmp_path):
        store.event(directory, "work.dispatch.planned", {})
        original = log.read_bytes()
        if change == "replace":
            replacement = directory / "replacement"
            replacement.write_bytes(original.replace(b"planned", b"changed"))
            replacement.replace(log)
        elif change == "rewrite":
            st = log.stat()
            log.write_bytes(original.replace(b"planned", b"changed"))
            os.utime(log, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        elif change == "append":
            with log.open("ab") as stream:
                stream.write(b'{"partial":')
        else:
            log.write_bytes(b"")
        if change in {"replace", "rewrite"}:
            with pytest.raises(ValueError, match="digest mismatch"):
                store.event(directory, "work.dispatch.tick", {})
            assert log.read_bytes() == original.replace(b"planned", b"changed")
        else:
            store.event(directory, "work.dispatch.tick", {})
            rows = store.events(directory)
            assert len(rows) == (2 if change == "append" else 1)
            assert rows[-1]["event_type"] == "work.dispatch.tick"


@pytest.mark.parametrize("bound", ["count", "bytes"])
def test_watch_closes_before_parent_event_capacity_is_exhausted(apply_target, fake_watch, capsys, monkeypatch, bound):
    _tasks(apply_target, [[f"file-{i}.py"] for i in range(20)])
    monkeypatch.setattr(store, "MAX_EVENT_LINE_BYTES", 1024)
    monkeypatch.setattr(store, "MAX_EVENTS", 12 if bound == "count" else 100)
    monkeypatch.setattr(store, "MAX_EVENT_BYTES", 9000 if bound == "bytes" else 100_000)
    directory = _watch(apply_target, capsys, "--max", "1", "--interval", "0.2", "--deadline", "20", "--drain-wait")
    session = store.read(directory / "session.json")
    assert session["close_reason"] == "event-capacity"
    assert session["finished_at"]
    assert 1 < len(fake_watch.launches) < 20
    rows = store.events(directory)
    assert len(rows) <= store.MAX_EVENTS
    assert (directory / "events.jsonl").stat().st_size <= store.MAX_EVENT_BYTES
    assert rows[-1]["event_type"] == "work.dispatch.closed"
    assert rows[-1]["payload"]["reason"] == "event-capacity"
    assert sum(row["event_type"] == "work.dispatch.planned" for row in rows) >= 3
    assert not any(row["event_type"] == "work.dispatch.tick_error" for row in rows)
    ticks = [row["payload"] for row in rows if row["event_type"] == "work.dispatch.tick"]
    assert sum(len(row["launched"]) for row in ticks) == len(fake_watch.launches)
    assert sum(len(row["finalized"]) for row in ticks) == len(fake_watch.launches)
    assert all(row["state"] == "completed" for _, row in store.intents(apply_target))


def test_watch_persists_close_receipt_when_log_has_no_room(apply_target, fake_watch, monkeypatch, capsys):
    _tasks(apply_target, [])

    def fill_bound():
        parent = next(p for p in store.sessions(apply_target) if store.read(p / "session.json").get("watch"))
        monkeypatch.setattr(store, "MAX_EVENTS", len(store.events(parent)))
        fake_watch.hooks.remove(fill_bound)
        fake_watch.stop()

    fake_watch.hooks.append(fill_bound)
    directory = _watch(apply_target, capsys, "--interval", "0.2")
    session = store.read(directory / "session.json")
    assert session["finished_at"] and session["close_reason"] == "SIGTERM"
    assert len(store.events(directory)) <= store.MAX_EVENTS


def test_watch_close_failure_preserves_original_exception_and_session_receipt(apply_target, monkeypatch):
    from brigade.work_cmd.dispatch_watch import Watch

    directory = store.root(apply_target) / "dispatch-close-error"
    directory.mkdir(parents=True)
    session = {"dispatch_id": directory.name}
    localio.write_json(directory / "session.json", session)
    watcher = Watch(apply.Apply(apply_target), directory, session, interval=0.1, drain_wait=False)
    event = store.event

    def fail_close(path, name, payload):
        if name == "work.dispatch.closed":
            raise OSError("closing storage failed")
        return event(path, name, payload)

    def interrupted():
        raise KeyboardInterrupt("original interruption")

    monkeypatch.setattr(store, "event", fail_close)
    with pytest.raises(KeyboardInterrupt, match="original interruption"):
        watcher.run(interrupted)
    saved = store.read(directory / "session.json")
    assert saved["finished_at"] and saved["close_reason"] == "error"


@pytest.mark.parametrize("bound", ["count", "bytes"])
@pytest.mark.parametrize("phase", ["main", "drain"])
def test_foreign_session_capacity_is_not_a_graceful_watch_close(apply_target, monkeypatch, capsys, bound, phase):
    from brigade.work_cmd.dispatch_watch import Watch

    assert cli.main(["work", "dispatch", "--target", str(apply_target), "--worker", "coder", "--dry-run"]) == 0
    capsys.readouterr()
    (child,) = store.sessions(apply_target)
    child_session = store.read(child / "session.json")
    child_session.update(dry_run=False, deadline=0 if phase == "main" else None)
    localio.write_json(child / "session.json", child_session)
    if phase == "drain":
        plan = store.read(child / "plan.json")
        path, row = apply._new_intent(apply_target, child, child_session, plan, plan["selected"][0])
        run_dir = child / "finished-run"
        run_dir.mkdir()
        store.transition(path, row, "running", pid=2147483647, run_id="finished-run", run_dir=str(run_dir))
        localio.write_json(
            run_dir / "run.json",
            {"status": "ok", "cwd": row["worktree"], "finished_at": localio.utc_now_iso()},
        )
        assert not child_session.get("watch") and not child_session.get("finished_at")
        assert store.supervised(row) and not store.child_alive(row)
        assert apply._terminal_meta(run_dir)["status"] == "ok"
    monkeypatch.setattr(store, "MAX_EVENT_LINE_BYTES", 1024)
    monkeypatch.setattr(store, "MAX_EVENTS", 12 if bound == "count" else 100)
    monkeypatch.setattr(store, "MAX_EVENT_BYTES", 9000 if bound == "bytes" else 100_000)
    while True:
        try:
            store.event(child, "work.dispatch.tick", {})
        except store.EventCapacityError:
            break
    parent = store.root(apply_target) / "dispatch-parent"
    parent.mkdir()
    session = {"created_at": "2026-01-01", "dispatch_id": parent.name, "watch": True}
    localio.write_json(parent / "session.json", session)
    engine = apply.Apply(apply_target)
    watcher = Watch(engine, parent, session, interval=0.1, drain_wait=True)
    if phase == "drain":
        watcher.reason = "error"
        with pytest.raises(store.EventCapacityError, match="bound exceeded") as caught:
            watcher.run(lambda: pytest.fail("main loop must be skipped during drain"))
        assert caught.value.directory == child.resolve()
    else:
        # Stop after the first failed tick; drain must not launch a new intent.
        monkeypatch.setattr(watcher, "pause", lambda seconds: setattr(watcher, "reason", "SIGTERM"))
        assert watcher.run(lambda: engine.reconcile(no_wait=True)) != 0
    saved = store.read(parent / "session.json")
    rows = store.events(parent)
    assert saved["finished_at"] and saved["close_reason"] == ("error" if phase == "drain" else "SIGTERM")
    assert saved["close_reason"] != "event-capacity"
    assert all(event["payload"].get("reason") != "event-capacity" for event in rows)
    assert rows[-1]["event_type"] == "work.dispatch.closed"
    assert rows[-1]["payload"]["reason"] == saved["close_reason"]
    assert rows[-1]["payload"]["drain_wait"] is True
    errors = [row for row in rows if row["event_type"] == "work.dispatch.tick_error"]
    assert len(errors) == (1 if phase == "main" else 0)
    if phase == "main":
        assert errors[0]["payload"]["error_type"] == "EventCapacityError"
        assert "bound exceeded" in errors[0]["payload"]["error"]


def test_watch_clean_close_io_failure_records_error(apply_target, fake_watch, monkeypatch):
    from brigade.work_cmd.dispatch_watch import Watch

    _tasks(apply_target, [])
    directory = store.root(apply_target) / "dispatch-close-io-error"
    directory.mkdir(parents=True)
    session = {"created_at": "2026-01-01", "dispatch_id": directory.name, "watch": True}
    localio.write_json(directory / "session.json", session)
    watcher = Watch(apply.Apply(apply_target), directory, session, interval=0.1, drain_wait=False)
    event = store.event

    def fail_close(path, name, payload):
        if name == "work.dispatch.closed":
            raise OSError("closing storage failed")
        return event(path, name, payload)

    def stop_normally():
        watcher.stop_waiting = True
        return 0

    monkeypatch.setattr(store, "event", fail_close)
    monkeypatch.setattr(watcher, "capacity_allows", lambda: False)
    with pytest.raises(OSError, match="closing storage failed"):
        watcher.run(stop_normally)
    saved = store.read(directory / "session.json")
    assert saved["finished_at"] and saved["close_reason"] == "error"


@pytest.mark.parametrize("bound", ["count", "bytes"])
def test_capacity_failure_keeps_intent_session_and_claim_consistent(apply_target, monkeypatch, capsys, bound):
    assert cli.main(["work", "dispatch", "--target", str(apply_target), "--worker", "coder", "--dry-run"]) == 0
    capsys.readouterr()
    (directory,) = store.sessions(apply_target)
    session = store.read(directory / "session.json")
    session.update(dry_run=False, deadline=None)
    localio.write_json(directory / "session.json", session)
    plan = store.read(directory / "plan.json")
    path, row = apply._new_intent(apply_target, directory, session, plan, plan["selected"][0])
    claim = apply.locking._claim_task(apply_target, row["task_id"], actor=row["seat"], claim_id=row["claim_id"])
    run_dir = directory / "finished-run"
    run_dir.mkdir()
    store.transition(
        path,
        row,
        "running",
        pid=2147483647,
        run_id="finished-run",
        run_dir=str(run_dir),
        claimed_item_revision=claim["claim"]["item_revision"],
    )
    localio.write_json(run_dir / "run.json", {"status": "ok", "finished_at": localio.utc_now_iso()})
    before = dict(row)
    ledger = apply.locking._read_task_ledger(apply_target)
    log = (directory / "events.jsonl").read_bytes()
    if bound == "count":
        monkeypatch.setattr(store, "MAX_EVENTS", len(store.events(directory)))
    else:
        monkeypatch.setattr(store, "MAX_EVENT_BYTES", len(log))
    engine = apply.Apply(apply_target)
    engine.continue_on_error = True
    observed = []
    refresh = engine.refresh

    def capture_refresh(path, current, session):
        observed.append(current)
        return refresh(path, current, session)

    monkeypatch.setattr(engine, "refresh", capture_refresh)
    with pytest.raises(store.EventCapacityError, match="bound exceeded"):
        engine.session(directory, no_wait=True, launch=False)
    assert observed == [before]
    assert row == store.read(path) == before
    assert row["state"] == "running" and store.supervised(observed[0])
    assert store.read(directory / "session.json") == session
    assert not session.get("finished_at")
    assert apply.locking._read_task_ledger(apply_target) == ledger
    task = next(task for task in ledger["tasks"] if task["id"] == row["task_id"])
    assert claiming.claim_record(task)["claim_id"] == row["claim_id"]
    assert (directory / "events.jsonl").read_bytes() == log


def test_transition_write_failure_keeps_snapshot_and_retry_sequence(tmp_path, monkeypatch):
    path = tmp_path / "intents" / "intent.json"
    path.parent.mkdir()
    row = {"intent_id": "intent", "state": "running", "state_seq": 3, "updated_at": "before", "terminal": None}
    localio.write_json(path, row)
    before = dict(row)
    write = localio.write_json

    def fail_write(destination, value):
        if destination == path:
            raise OSError("snapshot replacement failed")
        return write(destination, value)

    with monkeypatch.context() as patch:
        patch.setattr(localio, "write_json", fail_write)
        with pytest.raises(OSError, match="snapshot replacement failed"):
            store.transition(path, row, "completed", terminal={"run_status": "ok"})
    assert row == store.read(path) == before
    (first,) = store.events(tmp_path)
    assert first["payload"]["state"] == "completed"
    assert first["payload"]["state_seq"] == 4
    store.transition(path, row, "completed", terminal={"run_status": "ok"})
    assert row == store.read(path)
    assert row["state"] == "completed" and row["state_seq"] == 4
    events = store.events(tmp_path)
    assert len(events) == 2
    assert events[0]["payload"] == events[1]["payload"]


@pytest.fixture
def exit_after_observation(apply_target, fake_watch, monkeypatch):
    """A child that exits right after the drain reconcile saw it alive."""
    exited = []
    drains = []
    reconcile = apply.Apply.reconcile

    def observing_reconcile(engine, *, no_wait, launch=True):
        result = reconcile(engine, no_wait=no_wait, launch=launch)
        if not launch:
            drains.append((no_wait, launch, bool(exited)))
            if not exited:
                exited.append(True)  # Between this reconcile and the live-children check.
                if fake_watch.interrupt:
                    fake_watch.stop()
        return result

    def refresh(engine, path, row, session):
        if row["state"] == "running" and exited:
            store.transition(path, row, "completed", terminal={"run_status": "ok"})
        return row["state"] in store.TERMINAL

    fake_watch.interrupt = False
    monkeypatch.setattr(store, "child_alive", lambda row: row["state"] == "running" and not exited)
    monkeypatch.setattr(apply.Apply, "_refresh", refresh)
    monkeypatch.setattr(apply.Apply, "reconcile", observing_reconcile)
    return drains


def test_drain_finalizes_child_that_exits_after_the_last_observation(
    apply_target, fake_watch, exit_after_observation, capsys
):
    _tasks(apply_target, [["a.py"]])
    directory = _watch(apply_target, capsys, "--max-total", "1", "--drain-wait", "--interval", "0.1")
    # The quiescent exit costs exactly one further non-waiting, non-launching pass.
    assert exit_after_observation == [(True, False, False), (True, False, True)]
    assert len(fake_watch.launches) == 1
    ((_, intent),) = store.intents(apply_target)
    assert intent["state"] == "completed" and intent["terminal"] == {"run_status": "ok"}
    session = store.read(directory / "session.json")
    assert session["close_reason"] == "max-total" and session["launched"] == 1 and session["finished_at"]
    events = store.events(directory)
    assert events[-1]["event_type"] == "work.dispatch.closed"
    assert events[-1]["payload"]["live_children"] == 0
    finalized = [
        key for event in events if event["event_type"] == "work.dispatch.tick" for key in event["payload"]["finalized"]
    ]
    assert finalized == [intent["intent_id"]]


def test_drain_hard_interrupt_skips_the_quiescence_reconcile(apply_target, fake_watch, exit_after_observation, capsys):
    _tasks(apply_target, [["a.py"]])
    fake_watch.interrupt = True
    directory = _watch(apply_target, capsys, "--max-total", "1", "--drain-wait", "--interval", "0.1")
    assert exit_after_observation == [(True, False, False)]
    assert store.intents(apply_target)[0][1]["state"] == "running"
    assert store.read(directory / "session.json")["finished_at"]


def test_exclusions_read_only_own_unsettled_ticks(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from brigade.work_cmd import dispatch_watch

    reads = []
    root = tmp_path / ".brigade/dispatch"
    for name, finished in (("dispatch-a", True), ("dispatch-b", False)):
        (root / name / "intents").mkdir(parents=True)
        localio.write_json(root / name / "session.json", {"finished_at": "now" if finished else None})
        localio.write_json(
            root / name / "intents" / "x.json", {"task_id": f"task-{name}", "retry_exhausted": name == "dispatch-a"}
        )
    (root / "dispatch-old").mkdir()  # Unrelated history must never be read.
    original = dispatch_watch.store.read
    monkeypatch.setattr(dispatch_watch.store, "read", lambda p: reads.append(p) or original(p))
    watch = SimpleNamespace(
        engine=SimpleNamespace(target=tmp_path),
        session={"tick_sessions": ["dispatch-a", "dispatch-b"]},
        exhausted={},
        settled_ticks=set(),
    )
    assert dispatch_watch.Watch.exclusions(watch) == {"task-dispatch-a": "infra-retries-exhausted"}
    first = len(reads)
    assert dispatch_watch.Watch.exclusions(watch) == {"task-dispatch-a": "infra-retries-exhausted"}
    assert len(reads) - first < first  # The finished tick is not reread.
    assert watch.settled_ticks == {"dispatch-a"}


def test_final_spawn_gate_rechecks_reservation(apply_target, monkeypatch, capsys):
    import subprocess

    assert cli.main(["work", "dispatch", "--target", str(apply_target), "--worker", "coder", "--dry-run"]) == 0
    capsys.readouterr()
    (directory,) = store.sessions(apply_target)
    session = store.read(directory / "session.json")
    session.update(dry_run=False, deadline=None)
    localio.write_json(directory / "session.json", session)
    engine = apply.Apply(apply_target)
    answers = iter([True, False])
    engine.reservation_allowed = lambda: next(answers, False)
    spawned = []
    popen = subprocess.Popen

    def guarded(argv, *args, **kwargs):
        if "brigade.work_cmd.dispatch_child" in argv:
            spawned.append(argv)
        return popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded)
    engine.session(directory, no_wait=True)
    assert not spawned
    assert not [row for _, row in store.intents(apply_target) if row.get("pid")]
