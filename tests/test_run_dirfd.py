"""Descriptor-bound run directory transactions stay on the held inode."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from brigade import dirfd, localio, run_dirfd


def _require_bind(run_dir: Path) -> run_dirfd.BoundRunDir:
    bound = run_dirfd.bind_run_dir(run_dir)
    if bound is None:
        pytest.skip("descriptor-bound run directories are unavailable")
    return bound


def _identity(path: Path) -> tuple[int, int]:
    metadata = path.lstat()
    return metadata.st_dev, metadata.st_ino


@pytest.mark.skipif(not dirfd.available(), reason="descriptor-relative primitives required")
def test_bound_write_lands_on_original_inode_after_pathname_symlink_swap(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    original = run_dir / "run.json"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (attacker / "run.json").write_text("outside\n", encoding="utf-8")

    with run_dirfd.bound_run_dir(run_dir) as bound:
        localio.write_text_atomic(original, "inside-before\n")
        held = bound.identity
        assert _identity(run_dir) == held

        relocated = tmp_path / "run.orig"
        run_dir.rename(relocated)
        run_dir.symlink_to(attacker, target_is_directory=True)

        assert bound.still_bound() is False
        localio.write_text_atomic(original, "inside-after\n")

        assert (relocated / "run.json").read_bytes() == b"inside-after\n"
        assert (attacker / "run.json").read_bytes() == b"outside\n"
        assert _identity(relocated) == held


@pytest.mark.skipif(not dirfd.available(), reason="descriptor-relative primitives required")
def test_nested_events_and_recovery_checkpoints_stay_on_bound_inode(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    checkpoint = run_dir / "events" / "recovery-checkpoints" / "abc.json"
    journal = run_dir / "events" / "lifecycle.jsonl"
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (attacker / "events").mkdir()
    (attacker / "events" / "recovery-checkpoints").mkdir()
    (attacker / "events" / "lifecycle.jsonl").write_text("leaked\n", encoding="utf-8")
    (attacker / "events" / "recovery-checkpoints" / "abc.json").write_text("leaked\n", encoding="utf-8")

    with run_dirfd.bound_run_dir(run_dir) as bound:
        localio.write_text_atomic(journal, "event-one\n")
        localio.write_bytes_atomic(checkpoint, b'{"ok":true}\n')
        relocated = tmp_path / "run.orig"
        run_dir.rename(relocated)
        run_dir.symlink_to(attacker, target_is_directory=True)
        localio.write_text_atomic(journal, "event-two\n")
        localio.write_bytes_atomic(checkpoint, b'{"ok":false}\n')

        assert bound.still_bound() is False
        assert (relocated / "events" / "lifecycle.jsonl").read_bytes() == b"event-two\n"
        assert (relocated / "events" / "recovery-checkpoints" / "abc.json").read_bytes() == b'{"ok":false}\n'
        assert (attacker / "events" / "lifecycle.jsonl").read_bytes() == b"leaked\n"
        assert (attacker / "events" / "recovery-checkpoints" / "abc.json").read_bytes() == b"leaked\n"


@pytest.mark.skipif(not dirfd.available(), reason="descriptor-relative primitives required")
def test_symlinked_intermediate_is_rejected(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (run_dir / "events").symlink_to(outside, target_is_directory=True)

    bound = _require_bind(run_dir)
    try:
        with pytest.raises(OSError):
            bound.dir_fd("events")
        with pytest.raises(OSError):
            bound.write_text_atomic(("events",), "lifecycle.jsonl", "nope\n")
        assert list(outside.iterdir()) == []
    finally:
        bound.close()


@pytest.mark.skipif(not dirfd.available(), reason="descriptor-relative primitives required")
def test_invalid_components_are_rejected(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    with run_dirfd.bound_run_dir(run_dir) as bound:
        with pytest.raises(OSError):
            bound.dir_fd("..")
        with pytest.raises(OSError):
            bound.dir_fd(".")
        with pytest.raises(OSError):
            bound.write_text_atomic(("events/nested",), "run.json", "nope\n")
        with pytest.raises(OSError):
            bound.write_bytes_atomic((), "", b"nope")
        with pytest.raises(OSError):
            bound.read_bytes("..", "passwd")
        with pytest.raises(OSError):
            run_dirfd.active_binding_for(run_dir / ".." / "escape" / "x")


@pytest.mark.skipif(not dirfd.available(), reason="descriptor-relative primitives required")
def test_active_binding_for_is_lexical_and_does_not_resolve_symlinks(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run.json").write_text("inside\n", encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(run_dir, target_is_directory=True)

    with run_dirfd.bound_run_dir(run_dir):
        found = run_dirfd.active_binding_for(run_dir / "run.json")
        assert found is not None
        bound, components, name = found
        assert components == ()
        assert name == "run.json"
        assert bound.read_bytes("run.json") == b"inside\n"
        assert run_dirfd.active_binding_for(alias / "run.json") is None
        assert run_dirfd.active_binding_for(tmp_path / "other.json") is None
        with pytest.raises(OSError):
            run_dirfd.active_binding_for(run_dir / ".." / "escape.json")


@pytest.mark.skipif(not dirfd.available(), reason="descriptor-relative primitives required")
def test_close_and_error_paths_release_every_fd(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    bound = _require_bind(run_dir)
    root_fd = bound.dir_fd()
    events_fd = bound.dir_fd("events", create=True)
    try:
        with pytest.raises(OSError):
            bound.write_text_atomic(("events",), "..", "nope\n")
    finally:
        bound.close()
    with pytest.raises(OSError) as root_exc:
        os.fstat(root_fd)
    with pytest.raises(OSError) as events_exc:
        os.fstat(events_fd)
    assert root_exc.value.errno == errno.EBADF
    assert events_exc.value.errno == errno.EBADF


@pytest.mark.skipif(not dirfd.posix_available(), reason="POSIX O_CREAT|O_EXCL atomicity")
def test_posix_atomic_write_cleans_temp_and_leaves_original(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    target = run_dir / "run.json"
    target.write_bytes(b"keep\n")
    real_replace = dirfd.replace_children

    def boom(parent: int, source: str, destination: str) -> None:
        del parent, source, destination
        raise OSError("simulated replace failure")

    monkeypatch.setattr(dirfd, "replace_children", boom)
    bound = _require_bind(run_dir)
    try:
        with pytest.raises(OSError, match="simulated replace failure"):
            bound.write_text_atomic((), "run.json", "new\n")
        assert target.read_bytes() == b"keep\n"
        leftovers = [path.name for path in run_dir.iterdir() if path.name != "run.json"]
        assert leftovers == []
    finally:
        bound.close()
        monkeypatch.setattr(dirfd, "replace_children", real_replace)


@pytest.mark.skipif(not dirfd.available(), reason="descriptor-relative primitives required")
def test_bind_run_dir_fails_closed_on_symlink(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert run_dirfd.bind_run_dir(link) is None


@pytest.mark.skipif(not dirfd.available(), reason="descriptor-relative primitives required")
def test_bound_run_dir_context_closes_on_error(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    held: list[int] = []
    with pytest.raises(RuntimeError, match="boom"):
        with run_dirfd.bound_run_dir(run_dir) as bound:
            held.append(bound.dir_fd())
            raise RuntimeError("boom")
    with pytest.raises(OSError) as excinfo:
        os.fstat(held[0])
    assert excinfo.value.errno == errno.EBADF
