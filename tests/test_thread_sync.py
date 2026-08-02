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
