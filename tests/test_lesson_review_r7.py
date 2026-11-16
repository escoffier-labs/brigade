"""Interrupted publication and regular-file archival regressions."""

import hashlib
import os
import subprocess
import sys

import pytest

from brigade import dirfd, lesson_activity as activity, lesson_io as io, lesson_review as review
from tests.test_lesson_review import event, proposal, seat
from tests.test_lesson_review_r4 import scope as scope


@pytest.mark.skipif(not dirfd.posix_available(), reason="POSIX hard-link publication required")
def test_exit_between_link_and_unlink_delivers_on_restart(scope, monkeypatch):
    event(scope)
    event(scope)
    seat(monkeypatch, proposal(scope))
    write = io.write
    interrupted = []

    def crash(path, data, *, exclusive=False):
        if exclusive and path.parent.name == "outbox" and not interrupted:
            interrupted.append(path)
            script = """
import os
import sys
from pathlib import Path
from brigade import lesson_io as io
unlink = os.unlink
def crash(name, *args, **kwargs):
    if str(name).endswith('.tmp'):
        os._exit(91)
    return unlink(name, *args, **kwargs)
# Leave the initial absent-temporary cleanup intact.
link = os.link
def publish(*args, **kwargs):
    link(*args, **kwargs)
    os.unlink = crash
os.link = publish
io.write(Path(sys.argv[1]), sys.stdin.buffer.read(), exclusive=True)
"""
            result = subprocess.run([sys.executable, "-c", script, str(path)], input=data)
            assert result.returncode == 91
            assert path.stat().st_nlink == 2
            raise OSError("interrupted after publication")
        return write(path, data, exclusive=exclusive)

    monkeypatch.setattr(io, "write", crash)
    assert review.run(scope, now=111)["status"] == "retry"
    assert review.run(scope, now=500)["status"] == "committed"
    assert "quarantine" not in activity.load(scope)
    assert not list(interrupted[0].parent.glob(".lesson-*.tmp"))
    archived = list((interrupted[0].parent / "processed").glob("*.md"))
    copies = list((scope / "memory/handoff-inbox").glob("lesson-*.md"))
    assert len(archived) == len(copies) == 1
    assert io.read_bytes(archived[0]) == io.read_bytes(copies[0])


@pytest.mark.skipif(not dirfd.posix_available(), reason="POSIX hard links required")
@pytest.mark.parametrize("matching_temp", [False, True])
def test_recovery_refuses_unrelated_hard_links(tmp_path, matching_temp):
    path = tmp_path / "draft.md"
    data = b"draft"
    path.write_bytes(data)
    os.link(path, tmp_path / "unrelated.md")
    temporary = tmp_path / f".lesson-{hashlib.sha256(data).hexdigest()}.tmp"
    if matching_temp:
        os.link(path, temporary)
    io.recover_publication(path, data)
    with pytest.raises(OSError, match="single-link"):
        io.read_bytes(path)
    assert (tmp_path / "unrelated.md").exists()
    assert temporary.exists() == matching_temp


@pytest.mark.skipif(os.open not in os.supports_dir_fd, reason="stand-in NT helpers need POSIX dir_fd")
def test_lesson_file_moves_use_nt_regular_file_path(scope, monkeypatch):
    from brigade.work_cmd import nt_dirfd

    moves = []

    def replace(parent, source, destination, *, replace=True, destination_parent=None, directory=False):
        assert not directory
        assert not replace
        moves.append((source, destination))
        os.rename(source, destination, src_dir_fd=parent, dst_dir_fd=destination_parent)

    # Force only the move dispatcher down its Windows branch. Other I/O stays
    # on real POSIX descriptors so the lesson still crosses the full boundary.
    rename = dirfd.rename_file_child

    def windows_move(*args):
        with monkeypatch.context() as patch:
            patch.setattr(dirfd, "posix_available", lambda: False)
            patch.setattr(dirfd, "nt_available", lambda: True)
            patch.setattr(nt_dirfd, "replace_children", replace)
            rename(*args)

    monkeypatch.setattr(dirfd, "rename_file_child", windows_move)
    event(scope)
    event(scope)
    seat(monkeypatch, proposal(scope))
    assert review.run(scope, now=111)["status"] == "committed"
    spool = scope / activity.ROOT / "prompts.json"
    io.write(spool, b"invalid json")
    assert activity._read_prompts(spool) == []
    assert len(moves) == 2
    assert len(list(spool.parent.glob("*.quarantine"))) == 1


@pytest.mark.skipif(not dirfd.available(), reason="descriptor-relative directory operations required")
def test_regular_file_move_between_held_directories(tmp_path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "lesson.md").write_bytes(b"lesson")
    with io.directory(source) as parent, io.directory(destination) as processed:
        dirfd.rename_file_child(parent, "lesson.md", processed, "lesson.md")
    assert not (source / "lesson.md").exists()
    assert io.read_bytes(destination / "lesson.md") == b"lesson"
