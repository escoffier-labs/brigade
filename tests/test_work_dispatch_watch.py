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
        with pytest.raises(ValueError, match="held by pid"):
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
