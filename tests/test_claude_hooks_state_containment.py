"""Session state and timeout sidecars stay inside their validated directory."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

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


@pytest.mark.parametrize("component", ["repo", ".brigade", "work", "claude-hooks", "sessions"])
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
        except PermissionError as exc:
            # Native Windows can deny relocation while the sibling lock is
            # open. That denial must preserve the original state and tree.
            assert os.name == "nt"
            assert exc.winerror == 5
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


@pytest.mark.parametrize("append", ["journal", "pending"])
def test_sidecars_stay_in_held_directory_after_alias_retarget(state_tree, tmp_path, append) -> None:
    target, sessions, outside = state_tree
    alias = tmp_path / "alias"
    _directory_link(alias, target)
    original_files = sorted(str(path.relative_to(outside)) for path in outside.rglob("*"))

    with runtime._session_state_lock(alias, "s"):
        if os.name == "nt":
            os.rmdir(alias)
        else:
            alias.unlink()
        _directory_link(alias, outside)
        if append == "journal":
            session_state._append_timeout_marker(alias, "s")
        else:
            session_state._append_pending_increment(alias, "s")

    assert sorted(str(path.relative_to(outside)) for path in outside.rglob("*")) == original_files
    suffix = ".timeouts" if append == "journal" else ".timeouts.pending"
    assert (sessions / f"{runtime._state_path(target, 's').name}{suffix}").exists()


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows handle-relative backend")
def test_windows_unavailable_backend_never_falls_back_to_paths(state_tree, monkeypatch, capsys) -> None:
    from brigade.work_cmd import nt_dirfd

    target, sessions, outside = state_tree
    monkeypatch.setattr(nt_dirfd, "available", lambda: False)

    with pytest.raises(OSError, match="unavailable"):
        runtime.write_session_state(target, "s", {"session_id": "s"})
    with pytest.raises(OSError, match="unavailable") as caught:
        runtime.read_session_state(target, "s")
    assert not isinstance(caught.value, anchored_io.SessionIntegrityError)
    assert session_state._append_pending_increment(target, "s") is False
    assert list(runtime.iter_session_states(target)) == []
    assert "unavailable" in capsys.readouterr().err
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
                try:
                    runtime.write_session_state(target, "s", {"write_observed": True, "generation": generation})
                except OSError as exc:
                    assert os.name == "nt" and exc.errno == errno.EBUSY
                    assert not isinstance(exc, anchored_io.SessionIntegrityError)
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


def test_read_trusts_held_file_replaced_before_initial_fstat(state_tree, monkeypatch) -> None:
    target, _sessions, _outside = state_tree
    runtime.write_session_state(target, "s", {"write_observed": True, "generation": 0})
    name = runtime._state_path(target, "s").name
    original_open = dirfd.open_child_file
    replacements = 0
    opens = 0

    def racing_open(parent, child, flags, *args, **kwargs):
        nonlocal replacements, opens
        fd = original_open(parent, child, flags, *args, **kwargs)
        if child == name and not flags & (os.O_WRONLY | os.O_RDWR):
            opens += 1
            try:
                # Replace on every open, so a reader retry cannot escape the race.
                runtime.write_session_state(target, "s", {"write_observed": True, "generation": opens})
            except OSError as exc:
                # Classic Windows rename can refuse replacement of a held reader.
                assert os.name == "nt" and exc.errno == errno.EBUSY
                assert not isinstance(exc, anchored_io.SessionIntegrityError)
            else:
                replacements += 1
        return fd

    with monkeypatch.context() as patch:
        patch.setattr(dirfd, "open_child_file", racing_open)
        assert runtime.read_session_state(target, "s") == {"write_observed": True, "generation": 0}
    assert opens == 1
    if replacements == 0:
        assert os.name == "nt"
    else:
        assert replacements == 1
    assert runtime.read_session_state(target, "s") == {"write_observed": True, "generation": replacements}


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
            try:
                runtime.write_session_state(target, "s", {"write_observed": True, "generation": 2})
            except OSError as exc:
                # Classic Windows rename cannot replace this held reader.
                assert os.name == "nt" and exc.errno == errno.EBUSY
                assert not isinstance(exc, anchored_io.SessionIntegrityError)
        return data

    with monkeypatch.context() as patch:
        patch.setattr(os, "read", racing_read)
        assert runtime.read_session_state(target, "s") == {"write_observed": True, "generation": 1}
    runtime.write_session_state(target, "s", {"write_observed": True, "generation": 2})
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
    rename_refused = False

    def tracked_read(fd, count):
        nonlocal read_seen
        data = original_read(fd, count)
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) == state_identity:
            read_seen = True
        return data

    def racing_stat(parent, name, **kwargs):
        nonlocal swapped, rename_refused
        if read_seen and name == "sessions" and not (swapped or rename_refused):
            try:
                sessions.rename(held)
            except PermissionError as exc:
                assert os.name == "nt" and exc.winerror == 5
                rename_refused = True
                return original_stat(parent, name, **kwargs)
            swapped = True
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
        try:
            result = runtime.handle_payload("Stop", {"cwd": str(target), "session_id": "s", "stop_hook_active": False})
        except anchored_io.SessionIntegrityError:
            assert swapped
        else:
            assert rename_refused and result is not None and result["decision"] == "block"
    assert swapped or rename_refused
    assert path.read_bytes() == before
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "event,stop_active", [("Stop", False), ("Stop", True), ("PreToolUse", False), ("PostToolUse", False)]
)
def test_integrity_refusal_blocks_hook_without_rebuilding_state(tmp_path, capsys, event, stop_active) -> None:
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
            event=event,
            package=PACKAGE_REF,
            stdin_text=json.dumps({"cwd": str(target), "session_id": "s", "stop_hook_active": stop_active}),
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    if event == "Stop" and not stop_active:
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


@pytest.mark.parametrize("failure", [errno.EACCES, errno.EIO, "NTSTATUS=0xC0000043"])
def test_successful_publication_does_not_unlink_temporary(state_tree, monkeypatch, failure) -> None:
    target, sessions, _outside = state_tree

    def unavailable(*_args, **_kwargs):
        if isinstance(failure, int):
            raise OSError(failure, "cleanup unavailable")
        raise OSError(failure)

    with anchored_io.session_directory(target) as anchor:
        monkeypatch.setattr(dirfd, "unlink_child", unavailable)
        anchor.write_json("s.json", {"generation": 2})
        assert json.loads(anchor.read_bytes("s.json")) == {"generation": 2}
    assert sorted(path.name for path in sessions.iterdir()) == ["s.json"]


def test_failed_publication_cleans_up_temporary(state_tree, monkeypatch) -> None:
    target, sessions, _outside = state_tree
    original_unlink = dirfd.unlink_child
    cleaned = []

    def unavailable(*_args, **_kwargs):
        raise OSError(errno.ENOSPC, "publication unavailable")

    def cleanup(parent, name):
        cleaned.append(name)
        original_unlink(parent, name)

    with anchored_io.session_directory(target) as anchor:
        anchor.write_json("s.json", {"generation": 1})
        monkeypatch.setattr(dirfd, "replace_children", unavailable)
        monkeypatch.setattr(dirfd, "unlink_child", cleanup)
        with pytest.raises(OSError, match="publication unavailable") as caught:
            anchor.write_json("s.json", {"generation": 2})
        assert caught.value.errno == errno.ENOSPC
        assert json.loads(anchor.read_bytes("s.json")) == {"generation": 1}
    assert len(cleaned) == 1
    assert cleaned[0].endswith(".tmp")
    assert sorted(path.name for path in sessions.iterdir()) == ["s.json"]


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


@pytest.mark.parametrize("failure", ["access", "sharing", "ntstatus", "exhausted"])
def test_windows_publication_retries_contention_with_a_small_budget(state_tree, monkeypatch, failure) -> None:
    target, sessions, _outside = state_tree
    runtime.write_session_state(target, "s", {"generation": 1})
    monkeypatch.setattr(anchored_io, "os", SimpleNamespace(**(vars(os) | {"name": "nt"})))
    original_replace = dirfd.replace_children
    calls = []
    sleeps = []

    def contended(parent, source, destination, **kwargs):
        calls.append((parent, source, destination))
        if len(calls) <= 2 or failure == "exhausted":
            if failure == "ntstatus":
                raise OSError("Windows no-follow directory operation failed: NTSTATUS=0xC0000043")
            error = (
                OSError("sharing violation")
                if failure == "sharing"
                else PermissionError("path component access denied")
            )
            if failure == "sharing":
                error.winerror = 32
            raise error
        original_replace(parent, source, destination, **kwargs)

    monkeypatch.setattr(dirfd, "replace_children", contended)
    monkeypatch.setattr(anchored_io.time, "sleep", sleeps.append)
    if failure == "exhausted":
        with pytest.raises(OSError) as caught:
            runtime.write_session_state(target, "s", {"generation": 2})
        assert caught.value.errno == errno.EBUSY
        assert not isinstance(caught.value, anchored_io.SessionIntegrityError)
        assert runtime.read_session_state(target, "s") == {"generation": 1}
        assert len(calls) == 5
    else:
        runtime.write_session_state(target, "s", {"generation": 2})
        assert runtime.read_session_state(target, "s") == {"generation": 2}
        assert len(calls) == 3
    assert sum(sleeps) <= 0.04
    assert not list(sessions.glob("*.tmp"))


@pytest.mark.parametrize("failure", [errno.ENOSPC, errno.EROFS])
def test_resource_write_error_fails_open_with_doctor_pointer(tmp_path, monkeypatch, capsys, failure) -> None:
    target = _wired_claude(tmp_path)
    runtime.write_session_state(target, "s", {"write_observed": True})
    path = runtime._state_path(target, "s")
    before = path.read_bytes()
    original_open = dirfd.open_child_file

    def full_disk(parent, name, flags, *args, **kwargs):
        if name.endswith(".tmp"):
            raise OSError(failure, "session storage unavailable")
        return original_open(parent, name, flags, *args, **kwargs)

    monkeypatch.setattr(dirfd, "open_child_file", full_disk)
    monkeypatch.setattr(
        runtime,
        "_run_timed_handle_payload",
        lambda event, raw, *, pin: runtime._hook_run_body(event, json.loads(raw), pin_target=pin),
    )
    capsys.readouterr()
    assert (
        runtime.hook_run(
            event="Stop", package=PACKAGE_REF, stdin_text=json.dumps({"cwd": str(target), "session_id": "s"})
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result == runtime.envelope.degraded_envelope("Stop")
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "operation,error_number",
    [
        ("directory", errno.EROFS),
        ("read", errno.EACCES),
        ("temp", errno.ENOSPC),
        ("lock", errno.EMFILE),
        ("cleanup", errno.EIO),
        ("validate", getattr(errno, "EDQUOT", errno.ENOSPC)),
        ("file_stat", errno.EIO),
    ],
)
def test_session_resource_errors_remain_plain_oserrors(state_tree, monkeypatch, operation, error_number) -> None:
    target, _sessions, _outside = state_tree
    runtime.write_session_state(target, "s", {"write_observed": True})
    with anchored_io.session_directory(target) as anchor:

        def unavailable(*_args, **_kwargs):
            raise OSError(error_number, "resource unavailable")

        with pytest.raises(OSError) as caught:
            if operation == "directory":
                monkeypatch.setattr(dirfd, "open_directory_nofollow", unavailable)
                runtime.read_session_state(target, "s")
            elif operation == "validate":
                monkeypatch.setattr(dirfd, "stat_child", unavailable)
                anchor.validate()
            elif operation == "file_stat":
                monkeypatch.setattr(dirfd, "stat_child", unavailable)
                anchor.validate_file("s.json", anchor.fd)
            elif operation == "cleanup":

                def failed_replace(*_args, **_kwargs):
                    raise OSError(errno.ENOSPC, "publication unavailable")

                monkeypatch.setattr(dirfd, "replace_children", failed_replace)
                monkeypatch.setattr(dirfd, "unlink_child", unavailable)
                anchor.write_json("s.json", {})
            else:
                monkeypatch.setattr(
                    dirfd,
                    "open_child_lock_file" if operation == "lock" and os.name == "nt" else "open_child_file",
                    unavailable,
                )
                if operation == "read":
                    anchor.read_bytes(runtime._state_path(target, "s").name)
                elif operation == "temp":
                    anchor.write_json("s.json", {})
                else:
                    with anchor.held_lock("s.lock", deadline_seconds=1):
                        pytest.fail("lock opener unexpectedly succeeded")
        assert caught.value.errno == error_number
        assert not isinstance(caught.value, anchored_io.SessionIntegrityError)


@pytest.mark.parametrize("substitute", ["directory", "hardlink"])
def test_windows_publication_contention_refuses_non_regular_target(state_tree, monkeypatch, substitute) -> None:
    target, sessions, outside = state_tree
    published = runtime._state_path(target, "s")
    if substitute == "directory":
        published.mkdir()
    else:
        victim = outside / "victim"
        victim.write_text('{"generation": 1}')
        os.link(victim, published)
    monkeypatch.setattr(anchored_io, "os", SimpleNamespace(**(vars(os) | {"name": "nt"})))
    calls = []
    sleeps = []

    def contended(*args, **_kwargs):
        calls.append(args)
        raise PermissionError("path component access denied")

    monkeypatch.setattr(dirfd, "replace_children", contended)
    monkeypatch.setattr(anchored_io.time, "sleep", sleeps.append)
    with pytest.raises(anchored_io.SessionIntegrityError):
        runtime.write_session_state(target, "s", {"generation": 2})
    assert len(calls) == 1
    assert sleeps == []
    assert not list(sessions.glob("*.tmp"))


@pytest.mark.skipif(os.name != "nt", reason="requires the native NTFS rename refusal")
def test_windows_directory_publish_target_is_an_integrity_refusal(state_tree, monkeypatch) -> None:
    target, sessions, _outside = state_tree
    published = runtime._state_path(target, "s")
    published.mkdir()
    sleeps = []
    monkeypatch.setattr(anchored_io.time, "sleep", sleeps.append)
    with pytest.raises(anchored_io.SessionIntegrityError):
        runtime.write_session_state(target, "s", {"generation": 1})
    assert sleeps == []
    assert sorted(path.name for path in sessions.iterdir()) == [published.name]


def test_cleanup_failure_keeps_the_integrity_refusal_cause(state_tree, monkeypatch) -> None:
    target, sessions, _outside = state_tree
    refusal = OSError(errno.ELOOP, "publication refused")

    def refused(*_args, **_kwargs):
        raise refusal

    def unavailable(*_args, **_kwargs):
        raise OSError(errno.EACCES, "cleanup unavailable")

    with anchored_io.session_directory(target) as anchor:
        monkeypatch.setattr(dirfd, "replace_children", refused)
        monkeypatch.setattr(dirfd, "unlink_child", unavailable)
        with pytest.raises(anchored_io.SessionIntegrityError) as caught:
            anchor.write_json("s.json", {"generation": 1})
    assert caught.value.__cause__ is refusal
    assert caught.value.__context__.errno == errno.EACCES


def test_cleanup_failure_keeps_the_contention_behind_a_target_refusal(state_tree, monkeypatch) -> None:
    target, _sessions, _outside = state_tree
    runtime._state_path(target, "s").mkdir()
    monkeypatch.setattr(anchored_io, "os", SimpleNamespace(**(vars(os) | {"name": "nt"})))
    contention = PermissionError("path component access denied")

    def contended(*_args, **_kwargs):
        raise contention

    def unavailable(*_args, **_kwargs):
        raise OSError(errno.EACCES, "cleanup unavailable")

    monkeypatch.setattr(dirfd, "replace_children", contended)
    monkeypatch.setattr(dirfd, "unlink_child", unavailable)
    with pytest.raises(anchored_io.SessionIntegrityError) as caught:
        runtime.write_session_state(target, "s", {"generation": 1})
    assert caught.value.__cause__ is contention
    assert caught.value.__context__.errno == errno.EACCES


def test_cleanup_failure_keeps_an_uncaused_refusal_visible(state_tree, monkeypatch) -> None:
    target, _sessions, _outside = state_tree
    refusal = anchored_io.SessionIntegrityError("Claude hook target directory changed during session I/O")

    def refused(*_args, **_kwargs):
        raise refusal

    def unavailable(*_args, **_kwargs):
        raise OSError(errno.EACCES, "cleanup unavailable")

    with anchored_io.session_directory(target) as anchor:
        monkeypatch.setattr(dirfd, "replace_children", refused)
        monkeypatch.setattr(dirfd, "unlink_child", unavailable)
        with pytest.raises(anchored_io.SessionIntegrityError) as caught:
            anchor.write_json("s.json", {"generation": 1})
    assert caught.value is refusal
    assert caught.value.__cause__ is None
    assert not caught.value.__suppress_context__
    assert caught.value.__context__.errno == errno.EACCES
