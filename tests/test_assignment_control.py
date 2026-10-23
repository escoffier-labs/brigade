"""Assignment isolation contracts at the live registry and socket boundaries."""

from __future__ import annotations

import threading

import pytest

from brigade import aboyeur, proc, run_control


class Turn:
    thread_id = "thread-example"

    def __init__(self, *, fail=False, callback=None):
        self.calls = []
        self.fail = fail
        self.callback = callback

    def interrupt(self, turn_id):
        self.calls.append(("interrupt", turn_id))
        if self.callback:
            self.callback()
        if self.fail:
            raise RuntimeError("provider unavailable")

    def steer(self, text, turn_id):
        self.calls.append(("steer", turn_id, text))


def register(registry, task, *, attempt="attempt-1", turn_id="turn-1", worker="coder", **kwargs):
    turn = Turn(**kwargs)
    registry.register(worker, turn, turn_id, task_id=task, attempt_id=attempt)
    return turn


def test_same_seat_tasks_survive_and_exact_interrupt_leaves_sibling_active():
    registry = run_control.LiveTurnRegistry()
    first = register(registry, "task-a")
    second = register(registry, "task-b")

    response = registry.interrupt(task_id="task-a")

    assert response["interrupted"] == 1
    assert first.calls == [("interrupt", "turn-1")]
    assert second.calls == []
    assert registry.steer(text="continue", task_id="task-b")["task_id"] == "task-b"
    # Acknowledgement is not containment: both registrations remain active.
    assert registry.interrupt()["interrupted"] == 2


@pytest.mark.parametrize("op", ["interrupt", "steer"])
def test_legacy_alias_ambiguity_is_stable_and_has_no_provider_side_effect(op):
    registry = run_control.LiveTurnRegistry()
    second = register(registry, "task-b")
    first = register(registry, "task-a")
    with pytest.raises(run_control.ControlError) as exc:
        if op == "interrupt":
            registry.interrupt("coder")
        else:
            registry.steer("coder", "continue")
    assert exc.value.code == "ambiguous-target"
    assert str(exc.value) == ("ambiguous active turn for worker 'coder': task-a/attempt-1, task-b/attempt-1")
    assert first.calls == second.calls == []


def test_explicit_task_namespace_cannot_collide_with_legacy_worker(tmp_path):
    registry = run_control.LiveTurnRegistry()
    legacy = Turn()
    registry.register("task-a", legacy, "legacy-turn")
    assigned = register(registry, "task-a")
    server = run_control.ControlServer(tmp_path / "control.sock", registry)
    transport = server.start()
    try:
        response = run_control.send_request(transport, {"op": "interrupt", "task_id": "task-a"})
        assert response["ok"] is True
        assert response["tasks"] == [{"task_id": "task-a", "attempt_id": "attempt-1"}]
        steer = run_control.send_request(transport, {"op": "steer", "task_id": "task-a", "text": "focus"})
        assert steer["turn_id"] == "turn-1"
        assert steer["attempt_id"] == "attempt-1"
    finally:
        server.close()
    assert assigned.calls == [("interrupt", "turn-1"), ("steer", "turn-1", "focus")]
    assert legacy.calls == []
    registry.interrupt("task-a")
    assert legacy.calls == [("interrupt", "legacy-turn")]


def test_unregister_requires_matching_attempt_worker_and_turn():
    registry = run_control.LiveTurnRegistry()
    register(registry, "task-a", attempt="attempt-old", turn_id="same-turn")
    replacement = register(registry, "task-a", attempt="attempt-new", turn_id="same-turn")
    registry.unregister("coder", "same-turn", task_id="task-a", attempt_id="attempt-old")
    registry.unregister("other", "same-turn", task_id="task-a", attempt_id="attempt-new")
    registry.unregister("coder", "old-turn", task_id="task-a", attempt_id="attempt-new")
    registry.interrupt(task_id="task-a")
    assert replacement.calls == [("interrupt", "same-turn")]
    registry.unregister("coder", "same-turn", task_id="task-a", attempt_id="attempt-new")
    with pytest.raises(run_control.ControlError) as exc:
        registry.interrupt(task_id="task-a")
    assert exc.value.code == run_control.NO_ACTIVE_TURN


def test_reregister_same_attempt_keeps_only_current_turn():
    registry = run_control.LiveTurnRegistry()
    old = register(registry, "task-a", turn_id="old-turn")
    new = register(registry, "task-a", turn_id="new-turn")
    registry.unregister("coder", "old-turn", task_id="task-a", attempt_id="attempt-1")
    assert registry.interrupt()["interrupted"] == 1
    assert old.calls == []
    assert new.calls == [("interrupt", "new-turn")]


@pytest.mark.parametrize("assignment", [{}, {"task_id": "task-a", "attempt_id": "attempt-1"}])
@pytest.mark.parametrize("reuse_thread", [False, True])
def test_stale_registration_cannot_remove_replacement_with_same_ids(assignment, reuse_thread):
    registry = run_control.LiveTurnRegistry()
    first = Turn()
    first.thread_id = "thread-a"
    old = registry.register("coder", first, "same-turn", **assignment)
    replacement = first if reuse_thread else Turn()
    if not reuse_thread:
        replacement.thread_id = "thread-b"
    current = registry.register("coder", replacement, "same-turn", **assignment)

    registry.unregister("coder", "same-turn", registration=old, **assignment)

    response = registry.steer("coder", "continue")
    assert response["thread_id"] == replacement.thread_id
    assert replacement.calls == [("steer", "same-turn", "continue")]
    if not reuse_thread:
        assert first.calls == []
    registry.unregister("coder", "same-turn", registration=current, **assignment)
    assert registry.snapshot() == ()


def test_multiple_attempts_require_exact_attempt_selector():
    registry = run_control.LiveTurnRegistry()
    first = register(registry, "task-a", attempt="attempt-1")
    second = register(registry, "task-a", attempt="attempt-2")
    with pytest.raises(run_control.ControlError) as exc:
        registry.interrupt(task_id="task-a")
    assert exc.value.code == "ambiguous-target"
    registry.interrupt(task_id="task-a", attempt_id="attempt-2")
    assert first.calls == []
    assert second.calls == [("interrupt", "turn-1")]


@pytest.mark.parametrize("seats", [("coder", "coder"), ("coder", "other")])
def test_whole_run_budget_interrupt_visits_all_same_seat_turns_after_error(seats):
    registry = run_control.LiveTurnRegistry()
    first = register(registry, "task-a", fail=True)
    second = register(registry, "task-b")
    other = register(registry, "task-c", worker="other")
    report = aboyeur._observed_budget_cancellation(proc.ProcessRegistry(), registry, seats)
    assert first.calls == second.calls == [("interrupt", "turn-1")]
    expected = [("coder", "interrupt", "error")]
    if "other" in seats:
        assert other.calls == [("interrupt", "turn-1")]
        expected.append(("other", "interrupt", "interrupted"))
    else:
        assert other.calls == []
    assert [(item.seat, item.transport_capability, item.transport_result) for item in report.outcomes] == expected
    assert report.active_seats == tuple(dict.fromkeys(seats))


def test_provider_callbacks_can_mutate_registry_without_lock_or_snapshot_race():
    registry = run_control.LiveTurnRegistry()
    finished = threading.Event()

    def mutate():
        registry.unregister("coder", "turn-1", task_id="task-b", attempt_id="attempt-1")
        register(registry, "task-c")
        finished.set()

    first = register(registry, "task-a", callback=mutate)
    second = register(registry, "task-b")
    thread = threading.Thread(target=registry.interrupt, daemon=True)
    thread.start()
    thread.join(timeout=2)
    assert finished.is_set(), "provider called under registry lock"
    assert not thread.is_alive()
    assert first.calls == second.calls == [("interrupt", "turn-1")]


def test_ambiguity_list_is_sorted_and_bounded():
    registry = run_control.LiveTurnRegistry()
    for i in reversed(range(12)):
        register(registry, f"task-{i:02d}")
    with pytest.raises(run_control.ControlError) as exc:
        registry.interrupt("coder")
    message = str(exc.value)
    assert "task-00/attempt-1, task-01/attempt-1" in message
    assert "task-09/attempt-1" in message
    assert "task-10" not in message
    assert message.endswith(" (+2 more)")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"task_id": ""},
        {"task_id": "x" * 129},
        {"task_id": "task\nunsafe"},
        {"task_id": "task/a"},
        {"task_id": 123},
        {"attempt_id": "attempt-1"},
        {"task_id": "task-a", "attempt_id": ""},
    ],
)
def test_invalid_identity_refuses_before_registration(kwargs):
    registry = run_control.LiveTurnRegistry()
    with pytest.raises(run_control.ControlError) as exc:
        registry.register("coder", Turn(), "turn-1", **kwargs)
    assert exc.value.code == "invalid-target"
    with pytest.raises(run_control.ControlError) as empty:
        registry.interrupt()
    assert empty.value.code == run_control.NO_ACTIVE_TURN


def test_conflicting_socket_selectors_refuse_without_interrupt(tmp_path):
    registry = run_control.LiveTurnRegistry()
    turn = register(registry, "task-a")
    server = run_control.ControlServer(tmp_path / "control.sock", registry)
    transport = server.start()
    try:
        response = run_control.send_request(transport, {"op": "interrupt", "worker": "coder", "task_id": "task-a"})
    finally:
        server.close()
    assert response["ok"] is False
    assert response["code"] == "invalid-target"
    assert turn.calls == []


def test_socket_steer_without_target_returns_invalid_target(tmp_path):
    registry = run_control.LiveTurnRegistry()
    turn = register(registry, "task-a")
    server = run_control.ControlServer(tmp_path / "control.sock", registry)
    transport = server.start()
    try:
        response = run_control.send_request(transport, {"op": "steer", "text": "continue"})
    finally:
        server.close()
    assert response["ok"] is False
    assert response["code"] == "invalid-target"
    assert turn.calls == []
