"""Timeout-latch state remains monotonic when best-effort writers overlap.

``test_stale_clear_cannot_clobber_latch`` reproduces the CI failure from #1236
deterministically and fails on main: a ``_clear_hook_timeouts`` that read the
pre-latch state writes it back after the latch landed. It passes once the whole
read-modify-write sequence is serialized per session.
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing as mp
import os
import threading
import time
from pathlib import Path

import pytest

from brigade.claude_hooks import envelope, runtime
from brigade.work_cmd import inbox_lock
from tests.test_claude_hooks_user_target import PACKAGE_REF, _payload, _wired_claude


def _wait_for_timeout_followups() -> None:
    deadline = time.monotonic() + 10
    while any(thread.name == "brigade-hook-best-effort" for thread in threading.enumerate()):
        assert time.monotonic() < deadline, "timeout follow-up thread did not finish"
        time.sleep(0.02)


def _record_hook_timeout_in_fork(target: Path, session_id: str) -> None:
    runtime._record_hook_timeout(target, session_id)


def test_session_state_helpers_resolve_runtime_primitives_at_call_time(tmp_path: Path, monkeypatch) -> None:
    from brigade.claude_hooks import session_state

    calls: list[tuple[Path, str]] = []
    monkeypatch.setattr(
        runtime,
        "read_session_state",
        lambda target, session_id: calls.append((target, session_id)) or {"hook_latched": True},
    )

    assert session_state._read_hook_latch(tmp_path, "s") == (True, True)
    assert calls == [(tmp_path, "s")]


def test_lock_gap_does_not_leak_process_lock(tmp_path: Path, monkeypatch) -> None:
    from brigade.claude_hooks import session_state

    @contextlib.contextmanager
    def failing_file_lock(*_args, **_kwargs):
        raise RuntimeError("file lock entry failed")
        yield

    monkeypatch.setattr(inbox_lock, "held_file_lock", failing_file_lock)

    with pytest.raises(RuntimeError, match="file lock entry failed"):
        with session_state._session_state_lock(tmp_path, "s"):
            pass

    state_path = runtime._state_path(tmp_path.expanduser().resolve(), "s")
    assert session_state._session_state_process_lock(state_path).locked() is False


def test_persistent_lock_timeout_still_latches(tmp_path: Path, monkeypatch) -> None:
    from brigade.claude_hooks import session_state

    target = _wired_claude(tmp_path)

    @contextlib.contextmanager
    def timeout_lock(_target: Path, _session_id: str):
        raise inbox_lock.InboxLockTimeout("busy")
        yield

    with monkeypatch.context() as patch:
        patch.setattr(runtime, "_session_state_lock", timeout_lock)
        runtime._record_hook_timeout(target, "s")
        second = runtime._record_hook_timeout(target, "s")

        assert second["hook_latched"] is True
        assert runtime._read_hook_latch(target, "s")[0] is True

    recovered = runtime._record_hook_timeout(target, "s")
    assert recovered["hook_timeout_count"] == 3
    assert recovered["hook_latched"] is True
    assert session_state._journal_timeout_count(target, "s") == 0


def test_clear_after_journal_latch_keeps_latch(tmp_path: Path) -> None:
    from brigade.claude_hooks import session_state

    target = _wired_claude(tmp_path)
    session_state._append_timeout_marker(target, "s")
    session_state._append_timeout_marker(target, "s")

    runtime._clear_hook_timeouts(target, "s")

    assert runtime.read_session_state(target, "s") is None
    assert session_state._journal_timeout_count(target, "s") == 2


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


def test_preserving_writer_keeps_latch_from_a_stale_state(tmp_path: Path) -> None:
    target = _wired_claude(tmp_path)
    stale = {"session_id": "s", "hook_timeout_count": 0, "hook_latched": False, "stale_field": "written"}
    runtime.write_session_state(target, "s", stale)
    stale = runtime.read_session_state(target, "s")
    assert stale is not None
    runtime._record_hook_timeout(target, "s")
    runtime._record_hook_timeout(target, "s")
    assert runtime._write_session_state_preserving_latch(target, "s", stale)

    preserved = runtime.read_session_state(target, "s")
    assert preserved is not None
    assert preserved["hook_timeout_count"] == 2
    assert preserved["hook_latched"] is True
    assert preserved["stale_field"] == "written"

    # The raw primitive documents the race: a stale writer erases the latch.
    raw_stale = {"session_id": "raw", "hook_timeout_count": 0, "hook_latched": False}
    runtime.write_session_state(target, "raw", raw_stale)
    raw_stale = runtime.read_session_state(target, "raw")
    assert raw_stale is not None
    runtime._record_hook_timeout(target, "raw")
    runtime._record_hook_timeout(target, "raw")
    runtime.write_session_state(target, "raw", raw_stale)
    clobbered = runtime.read_session_state(target, "raw")
    assert clobbered is not None
    assert clobbered["hook_timeout_count"] == 0
    assert clobbered["hook_latched"] is False


def test_preserving_writer_logs_when_the_session_lock_times_out(tmp_path: Path, monkeypatch) -> None:
    target = _wired_claude(tmp_path)
    logged: list[tuple[Path, str]] = []

    @contextlib.contextmanager
    def timeout_lock(_target: Path, _session_id: str):
        raise inbox_lock.InboxLockTimeout("busy")
        yield

    monkeypatch.setattr(runtime, "_session_state_lock", timeout_lock)
    monkeypatch.setattr(envelope, "append_log", lambda path, message: logged.append((path, message)))

    assert runtime._write_session_state_preserving_latch(target, "s", {"session_id": "s"}, log_target=target) is False
    assert logged == [(target, "session state write skipped after lock timeout: s")]


@pytest.mark.skipif(os.name != "posix", reason="fork is only available on POSIX")
def test_forked_timeout_writer_does_not_inherit_held_session_lock(tmp_path: Path) -> None:
    target = _wired_claude(tmp_path)
    acquired = threading.Event()
    release = threading.Event()

    def hold_session_lock() -> None:
        with runtime._session_state_lock(target, "s"):
            acquired.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=hold_session_lock)
    holder.start()
    assert acquired.wait(timeout=2)
    process = mp.get_context("fork").Process(target=_record_hook_timeout_in_fork, args=(target, "s"))
    try:
        process.start()
        process.join(timeout=3)
        assert not process.is_alive(), "forked timeout writer inherited a permanently-held lock"
        assert process.exitcode == 0
    finally:
        release.set()
        holder.join(timeout=2)
        if process.is_alive():
            process.terminate()
            process.join(timeout=2)


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
    its read-modify-write, the 0.3 s gate (inside the 0.5 s lock budget) times
    out, and the records that were waiting on the lock then re-derive the
    latch.
    """
    target = _wired_claude(tmp_path)
    runtime.write_session_state(target, "s", {"session_id": "s", "hook_timeout_count": 1})
    records_done = threading.Event()
    clear_started = threading.Event()
    original_write = runtime.write_session_state

    def gated_write(target_: Path, session_id: str, state: dict) -> None:
        if state.get("hook_timeout_count") == 0 and state.get("hook_latched") is not True:
            clear_started.set()
            records_done.wait(timeout=0.3)
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
