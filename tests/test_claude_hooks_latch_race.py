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
    assert recovered["hook_timeout_journal_folded"] == 2
    assert recovered["hook_latched"] is True
    assert session_state._journal_timeout_count(target, "s") == 2


def test_effective_count_sums_state_and_journal(tmp_path: Path) -> None:
    from brigade.claude_hooks import session_state

    target = _wired_claude(tmp_path)
    session_state._append_timeout_marker(target, "s")
    runtime.write_session_state(target, "s", {"session_id": "s", "hook_timeout_count": 1})

    assert runtime._read_hook_latch(target, "s")[0] is True

    runtime.write_session_state(
        target,
        "s",
        {"session_id": "s", "hook_timeout_count": 1, "hook_timeout_journal_folded": 1},
    )
    assert runtime._read_hook_latch(target, "s")[0] is False


def test_marker_appended_during_fold_is_not_lost(tmp_path: Path) -> None:
    from brigade.claude_hooks import session_state

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
    try:
        assert session_state._append_timeout_marker(target, "s") == 1
        assert session_state._append_timeout_marker(target, "s") == 2
    finally:
        release.set()
        holder.join(timeout=2)
    assert not holder.is_alive()

    first = runtime._record_hook_timeout(target, "s")
    assert first["hook_timeout_count"] == 3
    assert first["hook_timeout_journal_folded"] == 2

    assert session_state._append_timeout_marker(target, "s") == 3
    second = runtime._record_hook_timeout(target, "s")
    assert second["hook_timeout_count"] == 5
    assert second["hook_timeout_journal_folded"] == 3
    assert session_state._journal_timeout_count(target, "s") == 3


def test_clear_consumes_journal_without_truncating(tmp_path: Path) -> None:
    from brigade.claude_hooks import session_state

    target = _wired_claude(tmp_path)
    runtime.write_session_state(target, "s", {"session_id": "s", "hook_timeout_count": 0})
    assert session_state._append_timeout_marker(target, "s") == 1

    runtime._clear_hook_timeouts(target, "s")

    cleared = runtime.read_session_state(target, "s")
    assert cleared is not None
    assert cleared["hook_timeout_count"] == 0
    assert cleared["hook_timeout_journal_folded"] == 1
    assert session_state._journal_timeout_count(target, "s") == 1

    recorded = runtime._record_hook_timeout(target, "s")
    assert recorded["hook_timeout_count"] == 1
    assert recorded["hook_timeout_journal_folded"] == 1


def test_journal_refuses_hardlink_and_caps_growth(tmp_path: Path) -> None:
    from brigade.claude_hooks import session_state

    target = _wired_claude(tmp_path)
    assert session_state._append_timeout_marker(target, "hardlink") == 1
    journal = session_state._journal_path(target, "hardlink")
    hardlink = journal.with_name("hardlink-copy")
    try:
        os.link(journal, hardlink)
    except OSError:
        if os.name != "nt":
            raise
    else:
        assert session_state._journal_timeout_count(target, "hardlink") == 0
        assert session_state._append_timeout_marker(target, "hardlink") == 0

    for _ in range(session_state._TIMEOUT_JOURNAL_MAX_LINES + 5):
        session_state._append_timeout_marker(target, "capped")
    assert session_state._journal_timeout_count(target, "capped") == session_state._TIMEOUT_JOURNAL_MAX_LINES


def test_journal_read_is_bounded_and_append_stops_at_byte_cap(tmp_path: Path) -> None:
    from brigade.claude_hooks import session_state

    target = _wired_claude(tmp_path)
    journal = session_state._journal_path(target, "byte-capped")
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_bytes((b"x" * 19_999 + b"\n") * 10)

    count = session_state._journal_timeout_count(target, "byte-capped")
    assert count <= session_state._TIMEOUT_JOURNAL_MAX_BYTES

    before = journal.stat().st_size
    assert session_state._append_timeout_marker(target, "byte-capped") == count
    assert journal.stat().st_size == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics required")
def test_journal_refuses_symlink_after_open_identity_check(tmp_path: Path, monkeypatch) -> None:
    from brigade.claude_hooks import session_state

    target = _wired_claude(tmp_path)
    journal = session_state._journal_path(target, "symlink")
    journal.parent.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside-timeouts"
    outside.write_bytes(b"outside\n")
    journal.symlink_to(outside)
    monkeypatch.setattr(os, "O_NOFOLLOW", 0, raising=False)
    # Simulate the path swap after the pre-open refusal check.
    monkeypatch.setattr(session_state, "_journal_path_is_safe_without_nofollow", lambda _path: True)

    assert session_state._journal_timeout_count(target, "symlink") == 0
    assert session_state._append_timeout_marker(target, "symlink") == 0
    assert outside.read_bytes() == b"outside\n"


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

    from brigade.claude_hooks import session_state

    assert results == [{**initial, "hook_timeout_count": 3, session_state._ANNOUNCE_CLAIMED_KEY: False}]
    assert runtime.read_session_state(target, "s") == initial


def test_stale_clear_cannot_clobber_latch(tmp_path: Path, monkeypatch) -> None:
    """A clear that read pre-latch state must not overwrite a latch that landed later.

    On main the clear thread's write is held until both timeout records have
    landed, so its stale state (count 0, no latch) is written last and the
    latch is lost. With the per-session lock the clear holds the lock through
    its read-modify-write, the gate (inside ``_SESSION_STATE_LOCK_SECONDS``)
    times out, and the records that were waiting on the lock then re-derive
    the latch.
    """
    target = _wired_claude(tmp_path)
    runtime.write_session_state(target, "s", {"session_id": "s", "hook_timeout_count": 1})
    records_done = threading.Event()
    clear_started = threading.Event()
    original_write = runtime.write_session_state

    def gated_write(target_: Path, session_id: str, state: dict) -> None:
        if state.get("hook_timeout_count") == 0 and state.get("hook_latched") is not True:
            clear_started.set()
            records_done.wait(timeout=0.15)
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


def test_timeout_followup_budget_keeps_handler_headroom() -> None:
    """Inner lock is shorter than the 0.5s outer follow-up; stacked waits stay safe.

    Claude's handler budget is 15s. The worker is 12s and termination is up
    to 1.2s (join 1.0 + kill join 0.2). Claim release and latch follow-up
    each use ``_TIMEOUT_FOLLOWUP_SECONDS``. Observe slack or a second
    sequential announce wait would erase the remaining headroom.
    """
    from brigade.claude_hooks.package import DEFAULT_HOOK_TIMEOUT_SECONDS

    lock_budget = runtime._SESSION_STATE_LOCK_SECONDS
    followup = runtime._TIMEOUT_FOLLOWUP_SECONDS
    assert lock_budget < followup
    worker = envelope.HOOK_TIMEOUT_SECONDS
    terminate = 1.2
    worst = worker + terminate + followup + followup
    assert worst <= DEFAULT_HOOK_TIMEOUT_SECONDS - 0.5
    assert not hasattr(runtime, "_TIMEOUT_FOLLOWUP_OBSERVE_SLACK_SECONDS")


def test_bounded_followup_latches_when_session_lock_times_out(tmp_path: Path, monkeypatch) -> None:
    """Latch decision uses the journal path, not leftover observe slack."""
    target = _wired_claude(tmp_path)
    first = runtime._record_hook_timeout(target, "s")
    assert first.get("hook_latched") is not True

    @contextlib.contextmanager
    def timeout_lock(_target: Path, _session_id: str):
        raise inbox_lock.InboxLockTimeout("busy")
        yield

    monkeypatch.setattr(runtime, "_session_state_lock", timeout_lock)

    outcome = runtime._bounded_timeout_followup(
        "PreToolUse",
        target,
        "s",
        runtime.HookDegraded("hook operation timed out"),
    )
    assert outcome == "latched"
    assert runtime._read_hook_latch(target, "s")[0] is True


def test_timeout_followup_does_not_hide_latch_behind_log_stall(tmp_path: Path, monkeypatch) -> None:
    """A known latch stays latched when hook.log append blocks."""
    target = _wired_claude(tmp_path)
    first = runtime._record_hook_timeout(target, "s")
    assert first.get("hook_latched") is not True

    release_log = threading.Event()

    def hang_log(*_args, **_kwargs):
        release_log.wait(timeout=5)

    monkeypatch.setattr(envelope, "append_log", hang_log)
    monkeypatch.setattr(runtime, "_TIMEOUT_FOLLOWUP_SECONDS", 0.05)

    try:
        outcome = runtime._bounded_timeout_followup(
            "PreToolUse",
            target,
            "s",
            runtime.HookDegraded("hook operation timed out"),
        )
        assert outcome == "latched"
        assert runtime._read_hook_latch(target, "s")[0] is True
    finally:
        release_log.set()


def test_timeout_followup_claims_announce_in_journal_not_state(tmp_path: Path, monkeypatch) -> None:
    """The locked path latches in state, then claims announce in the journal after the lock."""
    from brigade.claude_hooks import session_state

    target = _wired_claude(tmp_path)
    first = runtime._record_hook_timeout(target, "s")
    assert first.get("hook_latched") is not True

    writes: list[dict] = []
    original_write = runtime.write_session_state

    def spy_write(write_target: Path, session_id: str, state: dict) -> None:
        writes.append(dict(state))
        original_write(write_target, session_id, state)

    monkeypatch.setattr(runtime, "write_session_state", spy_write)

    outcome = runtime._bounded_timeout_followup(
        "PreToolUse",
        target,
        "s",
        runtime.HookDegraded("hook operation timed out"),
    )
    assert outcome == "latched"
    latching = [payload for payload in writes if payload.get("hook_latched") is True]
    assert latching
    assert all(payload.get("hook_latch_announced") is not True for payload in latching)
    state = runtime.read_session_state(target, "s")
    assert state is not None
    assert state.get("hook_latched") is True
    assert state.get("hook_latch_announced") is not True
    assert runtime._read_hook_latch(target, "s") == (True, False)
    journal = session_state._journal_path(target, "s")
    assert journal.is_file()
    claim_lines = [
        line for line in journal.read_bytes().splitlines() if line.split()[:1] == [session_state._ANNOUNCE_TOKEN]
    ]
    assert len(claim_lines) == 1


def test_lock_timeout_journal_claims_announce_so_next_hook_is_silent(tmp_path: Path, monkeypatch) -> None:
    """A journal fallback that crosses the latch also claims the one-shot announce."""
    from brigade.claude_hooks import session_state

    target = _wired_claude(tmp_path)
    first = runtime._record_hook_timeout(target, "s")
    assert first.get("hook_latched") is not True
    persisted = runtime.read_session_state(target, "s")
    assert persisted is not None
    assert persisted.get("hook_latch_announced") is not True

    @contextlib.contextmanager
    def timeout_lock(_target: Path, _session_id: str):
        raise inbox_lock.InboxLockTimeout("busy")
        yield

    monkeypatch.setattr(runtime, "_session_state_lock", timeout_lock)

    outcome = runtime._bounded_timeout_followup(
        "PreToolUse",
        target,
        "s",
        runtime.HookDegraded("hook operation timed out"),
    )
    assert outcome == "latched"
    assert runtime.read_session_state(target, "s") == persisted
    assert session_state._journal_timeout_count(target, "s") == 1
    assert runtime._read_hook_latch(target, "s") == (True, False)

    log = envelope.log_path(target).read_text(encoding="utf-8")
    assert log.count("latched after repeated timeouts") == 1

    body = runtime._hook_run_body(
        "PreToolUse",
        _payload(target, "PreToolUse", session_id="s"),
        pin_target=target,
    )
    assert body["kind"] == "latched_silent"
    assert body["result"] == envelope.empty_envelope("PreToolUse")
    log = envelope.log_path(target).read_text(encoding="utf-8")
    assert log.count("latched after repeated timeouts") == 1


def test_concurrent_fallback_writers_only_one_claims_announce(tmp_path: Path, monkeypatch) -> None:
    """Only the journal marker that crosses the latch threshold announces."""
    from brigade.claude_hooks import session_state

    target = _wired_claude(tmp_path)
    first = runtime._record_hook_timeout(target, "s")
    assert first.get("hook_latched") is not True

    @contextlib.contextmanager
    def timeout_lock(_target: Path, _session_id: str):
        raise inbox_lock.InboxLockTimeout("busy")
        yield

    monkeypatch.setattr(runtime, "_session_state_lock", timeout_lock)

    results: list[dict] = []

    def record() -> None:
        results.append(runtime._record_hook_timeout(target, "s", announce=True))

    threads = [threading.Thread(target=record) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    claimed = [payload for payload in results if payload.get(session_state._ANNOUNCE_CLAIMED_KEY) is True]
    assert len(claimed) == 1
    assert all(payload.get("hook_latched") is True for payload in results)
    assert runtime._read_hook_latch(target, "s") == (True, False)
    persisted = runtime.read_session_state(target, "s")
    assert persisted is None or session_state._ANNOUNCE_CLAIMED_KEY not in persisted


def _assert_one_latched_one_silent(outcomes: list[str], target: Path, session_id: str) -> None:
    from brigade.claude_hooks import session_state

    assert outcomes.count("latched") == 1
    assert outcomes.count("latched_silent") == 1
    log = envelope.log_path(target).read_text(encoding="utf-8")
    assert log.count("latched after repeated timeouts") == 1
    persisted = runtime.read_session_state(target, session_id)
    if persisted is not None:
        assert session_state._ANNOUNCE_CLAIMED_KEY not in persisted
    journal = session_state._journal_path(target, session_id)
    if journal.is_file():
        assert session_state._ANNOUNCE_CLAIMED_KEY.encode() not in journal.read_bytes()


def test_concurrent_locked_followups_one_latched_one_silent(tmp_path: Path) -> None:
    """The locked-path announcer is the not-announced to announced transition."""
    target = _wired_claude(tmp_path)
    first = runtime._record_hook_timeout(target, "s")
    assert first.get("hook_latched") is not True

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def follow() -> None:
        barrier.wait(timeout=2)
        outcomes.append(
            runtime._apply_timeout_followup_inprocess(
                "PreToolUse",
                target,
                "s",
                "hook operation timed out",
            )
        )

    threads = [threading.Thread(target=follow) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    _assert_one_latched_one_silent(outcomes, target, "s")


def test_concurrent_fallback_followups_one_latched_one_silent(tmp_path: Path, monkeypatch) -> None:
    """The journal first-claim result is the only fallback announcer."""
    target = _wired_claude(tmp_path)
    first = runtime._record_hook_timeout(target, "s")
    assert first.get("hook_latched") is not True

    @contextlib.contextmanager
    def timeout_lock(_target: Path, _session_id: str):
        raise inbox_lock.InboxLockTimeout("busy")
        yield

    monkeypatch.setattr(runtime, "_session_state_lock", timeout_lock)

    barrier = threading.Barrier(2)
    outcomes: list[str] = []

    def follow() -> None:
        barrier.wait(timeout=2)
        outcomes.append(
            runtime._apply_timeout_followup_inprocess(
                "PreToolUse",
                target,
                "s",
                "hook operation timed out",
            )
        )

    threads = [threading.Thread(target=follow) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    _assert_one_latched_one_silent(outcomes, target, "s")


def test_hook_run_timeout_emits_empty_envelope_for_latched_silent(tmp_path: Path, monkeypatch, capsys) -> None:
    """An already-latched unclaimed timeout must not emit degraded or a second notice."""
    target = _wired_claude(tmp_path)

    def boom(*_args, **_kwargs):
        raise runtime.HookDegraded("hook operation timed out", log_target=target)

    monkeypatch.setattr(runtime, "_run_timed_handle_payload", boom)
    monkeypatch.setattr(runtime, "_bounded_timeout_followup", lambda *_args, **_kwargs: "latched_silent")

    capsys.readouterr()
    assert (
        runtime.hook_run(
            event="PreToolUse",
            package=PACKAGE_REF,
            stdin_text=json.dumps(_payload(target, "PreToolUse", session_id="s")),
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == envelope.empty_envelope("PreToolUse")


def test_timestamp_only_journal_still_requests_announce(tmp_path: Path) -> None:
    """Older timestamp-only markers still latch and leave the one-shot pending."""
    from brigade.claude_hooks import session_state

    target = _wired_claude(tmp_path)
    assert session_state._append_timeout_marker(target, "s") == 1
    assert session_state._append_timeout_marker(target, "s") == 2
    journal = session_state._journal_path(target, "s")
    assert all(line.strip() and b" announced" not in line for line in journal.read_bytes().splitlines() if line.strip())
    assert runtime._read_hook_latch(target, "s") == (True, True)


def test_announce_claim_lines_do_not_count_as_timeouts(tmp_path: Path) -> None:
    """Standalone journal claim lines latch-announce without inflating the timeout count."""
    from brigade.claude_hooks import session_state

    target = _wired_claude(tmp_path)
    assert session_state._append_timeout_marker(target, "s") == 1
    assert session_state._claim_timeout_announce(target, "s") is True
    assert session_state._journal_timeout_count(target, "s") == 1
    assert runtime._read_hook_latch(target, "s") == (False, False)


def test_mixed_locked_and_journal_followups_one_latched_one_silent(tmp_path: Path, monkeypatch) -> None:
    """A locked slow write and a journal fallback elect exactly one announcement."""
    target = _wired_claude(tmp_path)
    first = runtime._record_hook_timeout(target, "s")
    assert first.get("hook_latched") is not True

    original_write = runtime.write_session_state
    entered = threading.Event()
    release = threading.Event()

    def gated_write(write_target: Path, session_id: str, state: dict) -> None:
        if state.get("hook_timeout_count") == 2:
            entered.set()
            release.wait(timeout=10)
        original_write(write_target, session_id, state)

    monkeypatch.setattr(runtime, "write_session_state", gated_write)

    outcomes: list[str] = []

    def locked_follow() -> None:
        outcomes.append(
            runtime._apply_timeout_followup_inprocess(
                "PreToolUse",
                target,
                "s",
                "hook operation timed out",
            )
        )

    def fallback_follow() -> None:
        assert entered.wait(timeout=2)
        outcomes.append(
            runtime._apply_timeout_followup_inprocess(
                "PreToolUse",
                target,
                "s",
                "hook operation timed out",
            )
        )

    locked = threading.Thread(target=locked_follow)
    fallback = threading.Thread(target=fallback_follow)
    locked.start()
    assert entered.wait(timeout=2)
    fallback.start()
    fallback.join(timeout=5)
    assert not fallback.is_alive()
    release.set()
    locked.join(timeout=5)
    assert not locked.is_alive()

    _assert_one_latched_one_silent(outcomes, target, "s")

    body = runtime._hook_run_body(
        "PreToolUse",
        _payload(target, "PreToolUse", session_id="s"),
        pin_target=target,
    )
    assert body["kind"] == "latched_silent"
    log = envelope.log_path(target).read_text(encoding="utf-8")
    assert log.count("latched after repeated timeouts") == 1


def test_concurrent_hook_run_bodies_one_latched_one_silent(tmp_path: Path) -> None:
    """Two hook bodies seeing latched-but-unannounced elect one journal claim."""
    target = _wired_claude(tmp_path)
    runtime.write_session_state(
        target,
        "s",
        {"session_id": "s", "hook_timeout_count": 2, "hook_latched": True},
    )
    assert runtime._read_hook_latch(target, "s") == (True, True)

    barrier = threading.Barrier(2)
    kinds: list[str] = []

    def run_body() -> None:
        barrier.wait(timeout=2)
        body = runtime._hook_run_body(
            "PreToolUse",
            _payload(target, "PreToolUse", session_id="s"),
            pin_target=target,
        )
        kinds.append(str(body["kind"]))

    threads = [threading.Thread(target=run_body) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    _assert_one_latched_one_silent(kinds, target, "s")
