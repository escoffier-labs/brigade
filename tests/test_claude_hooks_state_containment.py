"""Session state and timeout sidecars stay inside their validated directory."""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from brigade import dirfd
from brigade.claude_hooks import anchored_io, runtime, session_state
from brigade.claude_hooks.package import PACKAGE_REF
from tests.test_claude_hooks_user_target import _wired_claude


def _directory_link(link: Path, destination: Path) -> None:
    if os.name == "nt":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(destination)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(destination, target_is_directory=True)


@pytest.fixture
def state_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    target = tmp_path / "repo"
    sessions = runtime._sessions_root(target)
    sessions.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    return target, sessions, outside


def test_linked_sessions_refuses_state_write(state_tree) -> None:
    target, sessions, outside = state_tree
    sessions.rmdir()
    _directory_link(sessions, outside)

    with pytest.raises(OSError):
        runtime.write_session_state(target, "s", {"session_id": "s"})

    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("operation", ["read", "list", "journal", "pending", "lock"])
def test_linked_sessions_refuses_forged_state_and_sidecars(state_tree, operation) -> None:
    target, sessions, outside = state_tree
    name = runtime._state_path(target, "s").name
    (outside / name).write_text(json.dumps({"session_id": "forged"}))
    (outside / f"{name}.timeouts").write_bytes(b"forged\n")
    (outside / f"{name}.timeouts.pending").write_bytes(b"forged\n")
    sessions.rmdir()
    _directory_link(sessions, outside)

    with pytest.raises(anchored_io.SessionIntegrityError):
        if operation == "read":
            runtime.read_session_state(target, "s")
        elif operation == "list":
            list(runtime.iter_session_states(target))
        elif operation == "journal":
            session_state._journal_timeout_count(target, "s")
        elif operation == "pending":
            session_state._pending_timeout_count(target, "s")
        else:
            with runtime._session_state_lock(target, "s"):
                pytest.fail("linked sessions directory accepted for locking")
    for append in (session_state._append_timeout_marker, session_state._append_pending_increment):
        with pytest.raises(anchored_io.SessionIntegrityError):
            append(target, "s")
    assert not (outside / f"{name}.lock").exists()
    assert (outside / f"{name}.timeouts").read_bytes() == b"forged\n"
    assert (outside / f"{name}.timeouts.pending").read_bytes() == b"forged\n"


@pytest.mark.parametrize("component", [".brigade", "work", "claude-hooks", "sessions"])
@pytest.mark.parametrize("operation", ["write", "read", "journal", "pending", "list"])
def test_parent_swap_during_locked_transaction_fails_closed(state_tree, component, operation) -> None:
    target, sessions, outside = state_tree
    state_path = runtime._state_path(target, "s")
    state_path.write_text(json.dumps({"session_id": "original"}))
    parent = sessions
    while parent.name != component:
        parent = parent.parent
    forged_sessions = outside / sessions.relative_to(parent)
    forged_sessions.mkdir(parents=True, exist_ok=True)
    (forged_sessions / state_path.name).write_text(json.dumps({"session_id": "forged"}))
    original_files = sorted(str(path.relative_to(outside)) for path in outside.rglob("*"))

    with runtime._session_state_lock(target, "s"):
        try:
            parent.rename(parent.with_name(f"{parent.name}-held"))
        except PermissionError:
            # Native Windows can deny relocation while the sibling lock is
            # open. That denial must preserve the original state and tree.
            assert os.name == "nt"
            assert runtime.read_session_state(target, "s") == {"session_id": "original"}
        else:
            _directory_link(parent, outside)
            with pytest.raises(anchored_io.SessionIntegrityError):
                if operation == "write":
                    runtime.write_session_state(target, "s", {"session_id": "redirected"})
                elif operation == "read":
                    runtime.read_session_state(target, "s")
                elif operation == "journal":
                    session_state._append_timeout_marker(target, "s")
                elif operation == "pending":
                    session_state._append_pending_increment(target, "s")
                else:
                    list(runtime.iter_session_states(target))

    assert json.loads((forged_sessions / state_path.name).read_text()) == {"session_id": "forged"}
    assert sorted(str(path.relative_to(outside)) for path in outside.rglob("*")) == original_files


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows handle-relative backend")
def test_windows_unavailable_backend_never_falls_back_to_paths(state_tree, monkeypatch, capsys) -> None:
    from brigade.work_cmd import nt_dirfd

    target, sessions, outside = state_tree
    monkeypatch.setattr(nt_dirfd, "available", lambda: False)

    with pytest.raises(OSError, match="unavailable"):
        runtime.write_session_state(target, "s", {"session_id": "s"})
    with pytest.raises(anchored_io.SessionIntegrityError):
        runtime.read_session_state(target, "s")
    with pytest.raises(anchored_io.SessionIntegrityError):
        session_state._append_pending_increment(target, "s")
    with pytest.raises(anchored_io.SessionIntegrityError):
        list(runtime.iter_session_states(target))
    assert list(sessions.iterdir()) == []
    assert list(outside.iterdir()) == []


def test_read_during_atomic_replace_never_loses_state(state_tree) -> None:
    target, _sessions, _outside = state_tree
    runtime.write_session_state(target, "s", {"write_observed": True, "generation": 0})
    stop = threading.Event()
    started = threading.Event()
    failures = []

    def writer() -> None:
        try:
            generation = 0
            while not stop.is_set():
                generation += 1
                runtime.write_session_state(target, "s", {"write_observed": True, "generation": generation})
                started.set()
        except Exception as exc:
            failures.append(exc)
            started.set()

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        assert started.wait(10)
        for _ in range(3000):
            state = runtime.read_session_state(target, "s")
            assert state is not None
            assert state["write_observed"] is True
    finally:
        stop.set()
        thread.join(20)
    assert not thread.is_alive()
    assert failures == []


def test_read_trusts_held_file_after_atomic_replace(state_tree, monkeypatch) -> None:
    target, _sessions, _outside = state_tree
    runtime.write_session_state(target, "s", {"write_observed": True, "generation": 1})
    original_read = os.read
    replaced = False

    def racing_read(fd, count):
        nonlocal replaced
        data = original_read(fd, count)
        if not replaced:
            replaced = True
            runtime.write_session_state(target, "s", {"write_observed": True, "generation": 2})
        return data

    with monkeypatch.context() as patch:
        patch.setattr(os, "read", racing_read)
        assert runtime.read_session_state(target, "s") == {"write_observed": True, "generation": 1}
    assert runtime.read_session_state(target, "s") == {"write_observed": True, "generation": 2}


def test_transient_read_swap_never_rebuilds_write_observed(tmp_path, monkeypatch) -> None:
    target = _wired_claude(tmp_path)
    runtime.handle_payload(
        "PostToolUse",
        {"cwd": str(target), "session_id": "s", "tool_name": "Write", "tool_input": {"file_path": "code.py"}},
    )
    path = runtime._state_path(target, "s")
    before = path.read_bytes()
    assert json.loads(before)["write_observed"] is True
    sessions = path.parent
    held = sessions.with_name("sessions-held")
    outside = tmp_path / "outside"
    outside.mkdir()
    state_identity = (path.stat().st_dev, path.stat().st_ino)
    original_read = os.read
    original_stat = dirfd.stat_child
    read_seen = False
    swapped = False

    def tracked_read(fd, count):
        nonlocal read_seen
        data = original_read(fd, count)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == state_identity:
            read_seen = True
        return data

    def racing_stat(parent, name, **kwargs):
        nonlocal swapped
        if read_seen and name == "sessions" and not swapped:
            swapped = True
            sessions.rename(held)
            _directory_link(sessions, outside)
            try:
                return original_stat(parent, name, **kwargs)
            finally:
                if os.name == "nt":
                    sessions.rmdir()
                else:
                    sessions.unlink()
                held.rename(sessions)
        return original_stat(parent, name, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os, "read", tracked_read)
        patch.setattr(dirfd, "stat_child", racing_stat)
        with pytest.raises(OSError, match="session directory"):
            runtime.handle_payload("Stop", {"cwd": str(target), "session_id": "s", "stop_hook_active": False})
    assert swapped
    assert path.read_bytes() == before
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("event", ["Stop", "PreToolUse", "PostToolUse"])
def test_integrity_refusal_blocks_hook_without_rebuilding_state(tmp_path, capsys, event) -> None:
    target = _wired_claude(tmp_path)
    runtime.write_session_state(target, "s", {"write_observed": True})
    sessions = runtime._sessions_root(target)
    held = sessions.with_name("sessions-held")
    sessions.rename(held)
    outside = tmp_path / "outside"
    outside.mkdir()
    _directory_link(sessions, outside)
    capsys.readouterr()
    assert (
        runtime.hook_run(
            event=event, package=PACKAGE_REF, stdin_text=json.dumps({"cwd": str(target), "session_id": "s"})
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    if event == "Stop":
        assert result["decision"] == "block"
    elif event == "PreToolUse":
        assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    else:
        assert result["continue"] is False
    assert json.loads((held / runtime._state_path(target, "s").name).read_text())["write_observed"] is True
    assert list(outside.iterdir()) == []


def test_publication_detects_substituted_temporary_entry(state_tree, monkeypatch) -> None:
    target, sessions, _outside = state_tree
    original_replace = dirfd.replace_children

    def substituted(parent, source, destination, **kwargs):
        (sessions / source).rename(sessions / "saved-temp")
        (sessions / source).write_text('{"forged": true}')
        original_replace(parent, source, destination, **kwargs)

    monkeypatch.setattr(dirfd, "replace_children", substituted)
    with pytest.raises(OSError, match="session file"):
        runtime.write_session_state(target, "s", {"write_observed": True})


@pytest.mark.parametrize("operation", ["read", "append"])
def test_sidecar_file_swap_after_open_uses_held_descriptor(state_tree, monkeypatch, operation) -> None:
    target, sessions, outside = state_tree
    path = session_state._journal_path(target, "s")
    path.write_bytes(b"original\n")
    victim = outside / "victim"
    victim.write_bytes(b"forged\n")
    original_open = dirfd.open_child_file

    def swapped_open(parent, name, flags, *args, **kwargs):
        fd = original_open(parent, name, flags, *args, **kwargs)
        if name == path.name:
            path.rename(sessions / "original-sidecar")
            os.link(victim, path)
        return fd

    monkeypatch.setattr(dirfd, "open_child_file", swapped_open)
    if operation == "read":
        assert anchored_io.read_sidecar(path, 100) == b"original\n"
    else:
        with pytest.raises(OSError, match="session file"):
            anchored_io.append_sidecar(path, b"appended\n", max_bytes=100)
    assert victim.read_bytes() == b"forged\n"


def test_session_anchor_reused_through_resolved_alias(state_tree, tmp_path) -> None:
    target, _sessions, _outside = state_tree
    alias = tmp_path / "alias"
    _directory_link(alias, target)
    with anchored_io.session_directory(target, create=True) as anchor:
        with anchor.held_lock("outer.lock", deadline_seconds=1):
            with anchored_io.session_directory(alias) as reused:
                assert reused is anchor


@pytest.mark.parametrize("operation", ["read", "append"])
def test_sidecar_rejects_unexpected_layout(tmp_path, operation) -> None:
    path = tmp_path / "state.timeouts"
    with pytest.raises(OSError, match="layout"):
        if operation == "read":
            anchored_io.read_sidecar(path, 100)
        else:
            anchored_io.append_sidecar(path, b"increment\n", max_bytes=100)


def test_nested_anchor_lock_is_refused(state_tree) -> None:
    target, _sessions, _outside = state_tree
    with anchored_io.session_directory(target, create=True) as anchor:
        with anchor.held_lock("outer.lock", deadline_seconds=1):
            with pytest.raises(RuntimeError, match="nested"):
                with anchor.held_lock("inner.lock", deadline_seconds=1):
                    pass


def test_write_does_not_create_missing_target(tmp_path) -> None:
    target = tmp_path / "missing"
    with pytest.raises(OSError):
        runtime.write_session_state(target, "s", {})
    assert not target.exists()
