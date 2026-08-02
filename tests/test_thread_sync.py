import threading
import time

import pytest

from tests import thread_sync


def test_wait_for_event_unblocks_when_set():
    event = threading.Event()

    def setter():
        event.set()

    worker = thread_sync.start_thread(setter)
    thread_sync.wait_for_event(event, description="setter thread")
    thread_sync.join_thread(worker, description="setter thread")


def test_wait_for_predicate_unblocks_when_true():
    state = {"ready": False}

    def flip():
        state["ready"] = True

    worker = thread_sync.start_thread(flip)
    thread_sync.wait_for_predicate(lambda: state["ready"], description="state ready")
    thread_sync.join_thread(worker, description="flip thread")


def test_wait_for_event_fails_on_hard_timeout():
    with pytest.raises(AssertionError, match="timed out waiting for event"):
        thread_sync.wait_for_event(threading.Event(), description="never set", hard_timeout=0.05)


def test_join_thread_reraises_worker_exception():
    def fail():
        raise ValueError("worker failed")

    worker = thread_sync.start_thread(fail)

    with pytest.raises(ValueError, match="worker failed"):
        thread_sync.join_thread(worker, description="failing worker")


def test_start_thread_uses_daemon_workers():
    started = threading.Event()

    def mark_started() -> None:
        started.set()

    worker = thread_sync.start_thread(mark_started)
    thread_sync.wait_for_event(started, description="daemon worker started")
    assert worker.daemon is True
    thread_sync.join_thread(worker, description="daemon worker")


def test_join_thread_hard_timeout_cancels_daemon_worker():
    started = threading.Event()

    def spin_until_cancelled() -> None:
        started.set()
        while not thread_sync.current_thread_cancelled():
            time.sleep(0.001)

    worker = thread_sync.start_thread(spin_until_cancelled)
    thread_sync.wait_for_event(started, description="blocked worker started")
    assert worker.daemon is True

    with pytest.raises(AssertionError, match="timed out waiting for thread"):
        thread_sync.join_thread(worker, description="blocked worker", hard_timeout=0.05)

    thread_sync.wait_for_predicate(
        lambda: not worker.is_alive(),
        description="cancelled daemon worker exited",
        hard_timeout=1.0,
    )


def test_thread_gate_open_close_handoff():
    gate = thread_sync.ThreadGate()
    observed = threading.Event()

    def worker():
        gate.wait_open(description="gate opened")
        observed.set()

    thread = thread_sync.start_thread(worker)
    gate.open()
    thread_sync.wait_for_event(observed, description="worker passed gate")
    thread_sync.join_thread(thread, description="gate worker")


def test_note_cleanup_failure_uses_add_note_when_available(monkeypatch):
    primary = RuntimeError("primary failure")
    cleanup = ValueError("cleanup failure")
    notes: list[str] = []

    def fake_add_note(message: str) -> None:
        notes.append(message)

    monkeypatch.setattr(primary, "add_note", fake_add_note)
    thread_sync.note_cleanup_failure(primary, cleanup)
    assert notes == ["cleanup failed: ValueError('cleanup failure')"]


def test_note_cleanup_failure_appends_to_args_without_add_note(monkeypatch):
    import builtins

    primary = RuntimeError("primary failure")
    cleanup = ValueError("cleanup failure")
    real_getattr = builtins.getattr
    sentinel = object()

    def getattr_without_add_note(obj, name, default=sentinel):
        if obj is primary and name == "add_note":
            if default is not sentinel:
                return default
            raise AttributeError(name)
        if default is sentinel:
            return real_getattr(obj, name)
        return real_getattr(obj, name, default)

    monkeypatch.setattr(builtins, "getattr", getattr_without_add_note)
    thread_sync.note_cleanup_failure(primary, cleanup)
    assert primary.args == ("primary failure (cleanup failed: ValueError('cleanup failure'))",)


def test_cancel_before_join_suppresses_worker_exception():
    gate = threading.Event()

    def fail_when_released():
        gate.wait()
        raise ValueError("worker failed")

    worker = thread_sync.start_thread(fail_when_released)
    thread_sync.cancel_thread(worker)
    gate.set()
    thread_sync.join_thread(worker, description="cancelled failing worker")


def test_wait_for_terminal_outcome_not_intermediate_signal():
    attempted = threading.Event()
    contended = threading.Event()

    def slow_acquire():
        attempted.set()
        time.sleep(0.05)
        contended.set()

    worker = thread_sync.start_thread(slow_acquire)
    thread_sync.wait_for_predicate(
        lambda: contended.is_set() or not worker.is_alive(),
        description="lock attempt outcome",
    )
    assert attempted.is_set()
    assert contended.is_set()
    thread_sync.join_thread(worker, description="slow acquire")
