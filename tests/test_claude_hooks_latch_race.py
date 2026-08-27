"""Timeout-latch state remains monotonic when best-effort writers overlap.

``test_stale_clear_cannot_clobber_latch`` reproduces the CI failure from #1236
deterministically and fails on main: a ``_clear_hook_timeouts`` that read the
pre-latch state writes it back after the latch landed. It passes once the whole
read-modify-write sequence is serialized per session.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from brigade.claude_hooks import envelope, runtime
from brigade.work_cmd import inbox_lock
from tests.test_claude_hooks_user_target import PACKAGE_REF, _payload, _wired_claude


def _wait_for_timeout_followups() -> None:
    deadline = time.monotonic() + 10
    while any(thread.name == "brigade-hook-best-effort" for thread in threading.enumerate()):
        assert time.monotonic() < deadline, "timeout follow-up thread did not finish"
        time.sleep(0.02)


def test_slow_timeout_writes_eventually_latch_and_hold(tmp_path: Path, monkeypatch, capsys) -> None:
    """Slow state writes still converge on a held latch (consistency check; passes on main too)."""
    target = _wired_claude(tmp_path)
    monkeypatch.setattr(envelope, "HOOK_TIMEOUT_SECONDS", 0.01)
    calls = 0

    def hang(_event: str, _payload: dict, *, pin=None) -> dict | None:
        nonlocal calls
        calls += 1
        time.sleep(1)
        return None

    original_write = runtime.write_session_state

    def slow_write(target: Path, session_id: str, state: dict) -> None:
        time.sleep(0.6)
        original_write(target, session_id, state)

    monkeypatch.setattr(runtime, "handle_payload", hang)
    monkeypatch.setattr(runtime, "write_session_state", slow_write)
    payload = json.dumps(_payload(target, "PreToolUse"))
    capsys.readouterr()

    for _ in range(3):
        assert runtime.hook_run(event="PreToolUse", package=PACKAGE_REF, stdin_text=payload) == 0
        capsys.readouterr()

    _wait_for_timeout_followups()
    state = runtime.read_session_state(target, "session-1")
    assert state is not None
    assert state["hook_latched"] is True
    assert state["hook_timeout_count"] >= 2

    calls = 0
    assert runtime.hook_run(event="PreToolUse", package=PACKAGE_REF, stdin_text=payload) == 0
    output = json.loads(capsys.readouterr().out)
    assert calls == 0
    assert output in (envelope.empty_envelope("PreToolUse"), envelope.latched_envelope("PreToolUse"))


def test_concurrent_timeout_increments_are_not_lost(tmp_path: Path) -> None:
    target = _wired_claude(tmp_path)

    def record() -> None:
        for _ in range(25):
            runtime._record_hook_timeout(target, "s")

    threads = [threading.Thread(target=record) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    state = runtime.read_session_state(target, "s")
    assert state is not None
    assert state["hook_timeout_count"] == 200
    assert state["hook_latched"] is True


def test_session_lock_timeout_preserves_latched_state(tmp_path: Path) -> None:
    target = _wired_claude(tmp_path)
    initial = {"session_id": "s", "hook_timeout_count": 2, "hook_latched": True, "hook_latch_announced": True}
    runtime.write_session_state(target, "s", initial)
    results: list[object] = []

    def mutate_while_locked() -> None:
        runtime._clear_hook_timeouts(target, "s")
        results.append(runtime._record_hook_timeout(target, "s"))

    lock_path = Path(f"{runtime._state_path(target, 's')}.lock")
    with inbox_lock.held_file_lock(lock_path, deadline_seconds=1):
        thread = threading.Thread(target=mutate_while_locked)
        thread.start()
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert results == [initial]
    assert runtime.read_session_state(target, "s") == initial


def test_stale_clear_cannot_clobber_latch(tmp_path: Path, monkeypatch) -> None:
    """A clear that read pre-latch state must not overwrite a latch that landed later.

    On main the clear thread's write is held until both timeout records have
    landed, so its stale state (count 0, no latch) is written last and the
    latch is lost. With the per-session lock the clear holds the lock through
    its read-modify-write, the gate times out, and the records that follow
    re-derive the latch.
    """
    target = _wired_claude(tmp_path)
    runtime.write_session_state(target, "s", {"session_id": "s", "hook_timeout_count": 1})
    records_done = threading.Event()
    clear_started = threading.Event()
    original_write = runtime.write_session_state

    def gated_write(target_: Path, session_id: str, state: dict) -> None:
        if state.get("hook_timeout_count") == 0 and state.get("hook_latched") is not True:
            clear_started.set()
            records_done.wait(timeout=2)
        original_write(target_, session_id, state)

    monkeypatch.setattr(runtime, "write_session_state", gated_write)
    clear = threading.Thread(target=runtime._clear_hook_timeouts, args=(target, "s"))
    clear.start()
    assert clear_started.wait(timeout=2), "clear never reached its write"
    runtime._record_hook_timeout(target, "s")
    runtime._record_hook_timeout(target, "s")
    records_done.set()
    clear.join(timeout=10)
    assert not clear.is_alive()

    state = runtime.read_session_state(target, "s")
    assert state is not None
    assert state.get("hook_latched") is True, state
    assert state["hook_timeout_count"] >= 2
