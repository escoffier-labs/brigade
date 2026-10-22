"""Bound checkpoint publication must work through the NT-capable backend."""

import errno
import hashlib
import os
import socket
import stat
from pathlib import Path

import pytest

from brigade import dirfd, run_checkpoint, run_dirfd, run_journal


@pytest.fixture
def native_bound_run(tmp_path, monkeypatch):
    """Use real held descriptors, with only NT capability reporting enabled."""

    def deny_network(*args, **kwargs):
        raise AssertionError("checkpoint tests must not connect to a network")

    monkeypatch.setattr(socket.socket, "connect", deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", deny_network)
    monkeypatch.setattr(socket, "create_connection", deny_network)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with run_dirfd.bound_run_dir(run_dir) as bound:
        bound.dir_fd("events", run_checkpoint.CHECKPOINT_DIR_NAME, create=True)
        if dirfd.posix_available():
            # Exercise NT dispatch deterministically on POSIX using its real
            # descriptor-relative backend, including consuming the source.
            monkeypatch.setattr(dirfd, "_nt_open_child_directory", dirfd._posix_open_child_directory)

            def open_file(parent, name, flags, mode, *, share_write=True):
                assert share_write
                return dirfd._posix_open_child_file(parent, name, flags, mode)

            monkeypatch.setattr(dirfd, "_nt_open_child_file", open_file)
            monkeypatch.setattr(dirfd, "_nt_replace_children", dirfd._posix_replace_children)
            monkeypatch.setattr(dirfd, "_nt_unlink_child", dirfd._posix_unlink_child)
        monkeypatch.setattr(dirfd, "posix_available", lambda: False)
        monkeypatch.setattr(dirfd, "nt_available", lambda: True)
        monkeypatch.setattr(os, "supports_dir_fd", set())
        calls = []
        real_open = dirfd.open_child_file
        real_publish = dirfd.replace_children
        real_unlink = dirfd.unlink_child

        def open_child(parent, name, flags, mode=0o600, **kwargs):
            if flags & os.O_CREAT:
                assert flags & os.O_EXCL
                assert mode == 0o600
                calls.append("create")
            return real_open(parent, name, flags, mode, **kwargs)

        def publish(parent, source, destination, *, replace=True):
            assert replace is False
            calls.append("publish")
            real_publish(parent, source, destination, replace=replace)
            assert not (run_checkpoint.checkpoint_dir(run_dir) / source).exists()

        def unlink(parent, name):
            calls.append("cleanup")
            return real_unlink(parent, name)

        monkeypatch.setattr(dirfd, "open_child_file", open_child)
        monkeypatch.setattr(dirfd, "replace_children", publish)
        monkeypatch.setattr(dirfd, "unlink_child", unlink)
        yield run_dir, calls


def test_bound_publish_without_posix_support_is_repeatable(native_bound_run):
    run_dir, calls = native_bound_run
    body = b'{"status":"orphaned"}\n'
    final = run_checkpoint.publish_checkpoint_file(run_dir, body)
    identity = final.stat().st_ino
    assert final.read_bytes() == body
    assert final.stat().st_nlink == 1
    assert run_checkpoint.publish_checkpoint_file(run_dir, body) == final
    assert final.stat().st_ino == identity
    assert list(final.parent.iterdir()) == [final]
    assert calls == ["create", "publish", "cleanup"] * 2


@pytest.mark.parametrize("collision", ["bytes", "hardlink", "directory"])
def test_bound_publish_refuses_unsafe_or_different_collision(native_bound_run, collision):
    run_dir, _ = native_bound_run
    body = b'{"status":"orphaned"}\n'
    final = run_checkpoint.checkpoint_path(run_dir, hashlib.sha256(body).hexdigest())
    if collision == "directory":
        final.mkdir()
    else:
        final.write_bytes(body if collision == "hardlink" else b"x" * len(body))
        if collision == "hardlink":
            os.link(final, run_dir / "extra.json")
    identity = final.stat().st_ino
    with pytest.raises(run_checkpoint.CheckpointError) as raised:
        run_checkpoint.publish_checkpoint_file(run_dir, body)
    assert raised.value.category == ("collision-mismatch" if collision == "bytes" else "collision-unsafe")
    assert final.stat().st_ino == identity
    assert list(final.parent.iterdir()) == [final]


def test_bound_publish_preserves_write_failure_when_cleanup_fails(native_bound_run, monkeypatch):
    run_dir, _ = native_bound_run

    def fail_write(*args):
        raise OSError(errno.EIO, "simulated write failure")

    def fail_cleanup(*args):
        raise OSError(errno.EACCES, "simulated cleanup failure")

    monkeypatch.setattr(os, "write", fail_write)
    monkeypatch.setattr(dirfd, "unlink_child", fail_cleanup)
    with pytest.raises(run_checkpoint.CheckpointError) as raised:
        run_checkpoint.publish_checkpoint_file(run_dir, b"{}\n")
    assert raised.value.category == "io"
    assert not list(run_checkpoint.checkpoint_dir(run_dir).glob("*.json"))


@pytest.mark.parametrize("swapped_directory", ["run", "checkpoint"])
def test_bound_temp_mode_never_uses_swapped_path_without_fchmod(tmp_path, monkeypatch, swapped_directory):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    cp_dir = run_checkpoint.checkpoint_dir(run_dir)
    swapped = run_dir if swapped_directory == "run" else cp_dir
    moved = swapped.with_name(f"{swapped.name}.held")
    held_cp = moved / cp_dir.relative_to(run_dir) if swapped_directory == "run" else moved
    body = b'{"status":"orphaned"}\n'
    real_open = dirfd.open_child_file
    real_lstat = os.lstat
    real_chmod = os.chmod
    temp_paths = []
    replacement_before = {}
    rename_denials = []
    if os.name == "nt":
        cp_dir.mkdir(parents=True)
        canary = swapped / "canary.txt"
        canary.write_bytes(b"replacement must stay untouched\n")
        canary_stat = canary.stat()
        canary_before = (canary_stat.st_dev, canary_stat.st_ino, canary_stat.st_mode, canary_stat.st_size)
        original_entries = set([swapped, *swapped.rglob("*")])
        original_directories = {}
        for path in original_entries:
            if path.is_dir():
                metadata = path.stat()
                original_directories[path] = (metadata.st_dev, metadata.st_ino, metadata.st_mode)

    def open_then_swap(parent, name, flags, mode=0o600, **kwargs):
        fd = real_open(parent, name, flags, mode, **kwargs)
        if flags & os.O_CREAT:
            assert flags & os.O_EXCL
            assert mode == 0o600
            temp_paths.append(cp_dir / name)
            if os.name == "nt":
                before_rename = swapped.stat()
                entries_before_rename = set(swapped.rglob("*"))
                try:
                    swapped.rename(moved)
                except PermissionError as exc:
                    if exc.winerror != 5:
                        raise
                    rename_denials.append(exc.winerror)
                else:
                    pytest.fail("NT must deny renaming the held directory")
                after_rename = swapped.stat()
                assert (after_rename.st_dev, after_rename.st_ino, after_rename.st_mode, after_rename.st_size) == (
                    before_rename.st_dev,
                    before_rename.st_ino,
                    before_rename.st_mode,
                    before_rename.st_size,
                )
                assert set(swapped.rglob("*")) == entries_before_rename
                assert not moved.exists()
            else:
                swapped.rename(moved)
                cp_dir.mkdir(parents=True)
                # A matching lexical temp would let a pathname chmod reach the
                # replacement tree even though the real temp is under held_cp.
                temp_paths[0].write_bytes(b"replacement must stay untouched\n")
                for path in [swapped, *swapped.rglob("*")]:
                    metadata = path.stat()
                    replacement_before[path] = (metadata.st_ino, metadata.st_mode, metadata.st_size)
        return fd

    def reject_temp_lstat(path, *args, **kwargs):
        assert Path(path) not in temp_paths, "bound temp used lexical lstat after directory swap"
        return real_lstat(path, *args, **kwargs)

    def reject_temp_chmod(path, *args, **kwargs):
        assert isinstance(path, int) or Path(path) not in temp_paths, (
            "bound temp used lexical chmod after directory swap"
        )
        return real_chmod(path, *args, **kwargs)

    monkeypatch.setattr(run_journal, "_HAS_FCHMOD", False)
    monkeypatch.setattr(dirfd, "open_child_file", open_then_swap)
    with run_dirfd.bound_run_dir(run_dir) as bound:
        bound.dir_fd("events", run_checkpoint.CHECKPOINT_DIR_NAME, create=True)
        with monkeypatch.context() as guarded:
            guarded.setattr(os, "lstat", reject_temp_lstat)
            guarded.setattr(os, "chmod", reject_temp_chmod)
            try:
                final = run_checkpoint.publish_checkpoint_file(run_dir, body)
            finally:
                assert len(temp_paths) == 1, "real exclusive temp creation must precede the swap"
                if os.name == "nt":
                    assert rename_denials == [5], "held-directory rename must be denied exactly once"
                    assert not moved.exists()
                    assert canary.read_bytes() == b"replacement must stay untouched\n"
                    metadata = canary.stat()
                    assert (metadata.st_dev, metadata.st_ino, metadata.st_mode, metadata.st_size) == canary_before
                    for path, expected in original_directories.items():
                        metadata = path.stat()
                        assert (metadata.st_dev, metadata.st_ino, metadata.st_mode) == expected
                else:
                    assert temp_paths[0].read_bytes() == b"replacement must stay untouched\n"
                    assert set([swapped, *swapped.rglob("*")]) == set(replacement_before)
                    for path, expected in replacement_before.items():
                        metadata = path.stat()
                        assert (metadata.st_ino, metadata.st_mode, metadata.st_size) == expected

        held_final = (cp_dir if os.name == "nt" else held_cp) / final.name
        assert held_final.read_bytes() == body
        assert held_final.stat().st_nlink == 1
        if os.name == "posix":
            assert stat.S_IMODE(held_final.stat().st_mode) == 0o600
        if os.name == "nt":
            assert set([swapped, *swapped.rglob("*")]) == original_entries | {held_final}
            assert set(cp_dir.iterdir()) == {held_final, *([canary] if swapped_directory == "checkpoint" else [])}
        else:
            assert list(held_cp.iterdir()) == [held_final]


@pytest.mark.skipif(not dirfd.posix_available(), reason="requires POSIX descriptor-relative links")
def test_bound_posix_publish_fsyncs_final_link_before_unlinking_temp(tmp_path, monkeypatch):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    events = []
    real_link = dirfd.link_child
    real_unlink = dirfd.unlink_child
    real_fsync = run_checkpoint._fsync_directory

    def link(*args):
        events.append("link")
        return real_link(*args)

    def unlink(*args):
        events.append("unlink")
        return real_unlink(*args)

    def fsync(path):
        events.append("fsync")
        return real_fsync(path)

    monkeypatch.setattr(dirfd, "link_child", link)
    monkeypatch.setattr(dirfd, "unlink_child", unlink)
    monkeypatch.setattr(run_checkpoint, "_fsync_directory", fsync)
    with run_dirfd.bound_run_dir(run_dir) as bound:
        bound.dir_fd("events", run_checkpoint.CHECKPOINT_DIR_NAME, create=True)
        final = run_checkpoint.publish_checkpoint_file(run_dir, b'{"status":"orphaned"}\n')
    assert events == ["link", "fsync", "unlink", "fsync"]
    assert final.stat().st_nlink == 1
    assert list(final.parent.iterdir()) == [final]


@pytest.mark.skipif(not dirfd.posix_available(), reason="requires POSIX descriptor-relative links")
def test_bound_posix_publish_requires_link_dir_fd_support(monkeypatch):
    assert run_checkpoint._descriptor_relative_publish_available()
    monkeypatch.setattr(os, "supports_dir_fd", set(os.supports_dir_fd) - {os.link})
    assert not run_checkpoint._descriptor_relative_publish_available()
