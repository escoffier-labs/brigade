"""Deterministic concurrency checks for shared model listings."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from brigade import model_inventory, proc


def test_different_environments_progress_while_first_listing_is_blocked(monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    now = [0.0]
    budgets = []

    def run(argv, *, timeout, env):
        budgets.append(timeout)
        if env["MODEL_SOURCE"] == "first":
            entered.set()
            assert release.wait(5)
        return proc.Result(0, "Available models:\n* grok-test\n", "")

    monkeypatch.setattr(model_inventory.proc, "run", run)
    monkeypatch.setattr(model_inventory.time, "monotonic", lambda: now[0])
    inspector = model_inventory.ModelInventoryInspector()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(inspector.inspect, "grok", "grok-test", env={"MODEL_SOURCE": "first"})
        try:
            assert entered.wait(5)
            now[0] = 100.0
            second = executor.submit(inspector.inspect, "grok", "grok-test", env={"MODEL_SOURCE": "second"})
            assert second.result(timeout=5).state == "exact"
        finally:
            release.set()
        assert first.result(timeout=5).state == "exact"
    assert budgets == [15.0, 15.0]


@pytest.mark.parametrize("second_model", ["grok-test:cloud", "other-test:cloud"])
def test_same_key_concurrent_resolution_lists_and_checks_cloud_model_once(monkeypatch, second_model):
    entered = threading.Event()
    release = threading.Event()
    waiting = threading.Event()
    calls = []
    now = [0.0]
    real_lock = threading.Lock()

    class ObservedLock:
        def __enter__(self):
            if real_lock.locked():
                waiting.set()
            real_lock.acquire()

        def __exit__(self, *args):
            real_lock.release()

    def run(argv, *, timeout, env):
        calls.append(argv)
        assert timeout == 15.0
        if argv == ["ollama", "list"]:
            return proc.Result(
                0,
                "NAME ID SIZE MODIFIED\ngrok-test:cloud abcdef123456 - now now\nother-test:cloud abcdef123456 - now now\n",
                "",
            )
        entered.set()
        assert release.wait(5)
        return proc.Result(0, "", "")

    monkeypatch.setattr(model_inventory.proc, "run", run)
    monkeypatch.setattr(model_inventory.time, "monotonic", lambda: now[0])
    inspector = model_inventory.ModelInventoryInspector()
    inspector._key_locks[("ollama", None, "default")] = ObservedLock()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(inspector.inspect, "ollama:grok-test:cloud", "grok-test:cloud")
        try:
            assert entered.wait(5)
            second = executor.submit(inspector.inspect, f"ollama:{second_model}", second_model)
            assert waiting.wait(5)
            # Queueing must not consume the second caller's listing budget.
            now[0] = 100.0
        finally:
            release.set()
        assert first.result(timeout=5).state == second.result(timeout=5).state == "exact"
    expected = [["ollama", "list"], ["ollama", "show", "grok-test:cloud"]]
    if second_model != "grok-test:cloud":
        expected.append(["ollama", "show", second_model])
    assert calls == expected
