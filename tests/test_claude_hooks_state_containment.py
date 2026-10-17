"""Session state and timeout sidecars stay inside their validated directory."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from brigade.claude_hooks import runtime, session_state


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

    if operation == "read":
        assert runtime.read_session_state(target, "s") is None
    elif operation == "list":
        assert list(runtime.iter_session_states(target)) == []
    elif operation == "journal":
        assert session_state._journal_timeout_count(target, "s") == 0
        assert session_state._append_timeout_marker(target, "s") == 0
    elif operation == "pending":
        assert session_state._pending_timeout_count(target, "s") == 0
        assert session_state._append_pending_increment(target, "s") is False
    else:
        with pytest.raises(OSError), runtime._session_state_lock(target, "s"):
            pytest.fail("linked sessions directory accepted for locking")
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
        parent.rename(parent.with_name(f"{parent.name}-held"))
        _directory_link(parent, outside)
        if operation == "write":
            with pytest.raises(OSError):
                runtime.write_session_state(target, "s", {"session_id": "redirected"})
        elif operation == "read":
            assert runtime.read_session_state(target, "s") is None
        elif operation == "journal":
            assert session_state._append_timeout_marker(target, "s") == 0
        elif operation == "pending":
            assert session_state._append_pending_increment(target, "s") is False
        else:
            assert list(runtime.iter_session_states(target)) == []

    assert json.loads((forged_sessions / state_path.name).read_text()) == {"session_id": "forged"}
    assert sorted(str(path.relative_to(outside)) for path in outside.rglob("*")) == original_files


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows handle-relative backend")
def test_windows_unavailable_backend_never_falls_back_to_paths(state_tree, monkeypatch, capsys) -> None:
    from brigade.work_cmd import nt_dirfd

    target, sessions, outside = state_tree
    monkeypatch.setattr(nt_dirfd, "available", lambda: False)

    with pytest.raises(OSError, match="unavailable"):
        runtime.write_session_state(target, "s", {"session_id": "s"})
    assert runtime.read_session_state(target, "s") is None
    assert session_state._append_pending_increment(target, "s") is False
    assert list(runtime.iter_session_states(target)) == []
    assert "session listing refused" in capsys.readouterr().err
    assert list(sessions.iterdir()) == []
    assert list(outside.iterdir()) == []
