import threading

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
