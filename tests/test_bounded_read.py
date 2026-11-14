"""Tests for the bounded, nonblocking regular-file read."""

from __future__ import annotations

import os

import pytest

from brigade import bounded_read
from brigade.bounded_read import read_regular_file
from tests._instruction_fixtures import HAS_FIFO, deadline


def test_reads_a_regular_file_from_one_descriptor(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"hello")
    got = read_regular_file(path, max_bytes=10)
    assert (got.data, got.size, got.reason) == (b"hello", 5, None)


def test_a_file_over_the_ceiling_reports_its_size_and_no_bytes(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"x" * 11)
    got = read_regular_file(path, max_bytes=10)
    assert (got.data, got.size, got.reason) == (None, 11, "too-large")
    assert read_regular_file(path, max_bytes=11).data == b"x" * 11


def test_missing_and_directory_and_dangling_are_named(tmp_path):
    assert read_regular_file(tmp_path / "nope", max_bytes=10).reason == "missing"
    assert read_regular_file(tmp_path / "nope" / "deeper", max_bytes=10).reason == "missing"
    assert read_regular_file(tmp_path, max_bytes=10).reason in ("not-regular", "unreadable")
    assert read_regular_file("bad\0name", max_bytes=10).reason == "unreadable"


@pytest.mark.skipif(not HAS_FIFO, reason="POSIX FIFOs")
def test_a_fifo_is_refused_without_blocking(tmp_path):
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with deadline():
        got = read_regular_file(fifo, max_bytes=10)
    assert (got.data, got.reason) == (None, "not-regular")


@pytest.mark.skipif(not HAS_FIFO, reason="POSIX FIFOs")
def test_without_o_nonblock_the_type_is_checked_before_opening(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "O_NONBLOCK", raising=False)  # as on Windows
    path = tmp_path / "f.txt"
    path.write_bytes(b"ok")
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    with deadline():
        assert read_regular_file(fifo, max_bytes=10).reason == "not-regular"
    assert read_regular_file(path, max_bytes=10).data == b"ok"
    assert read_regular_file(tmp_path / "nope", max_bytes=10).reason == "missing"


def test_a_file_that_changes_during_the_read_fails_only_when_stability_is_required(tmp_path, monkeypatch):
    path = tmp_path / "f.txt"
    path.write_bytes(b"first")
    real_read = os.read

    def growing(fd, count):
        chunk = real_read(fd, count)
        path.write_bytes(b"first and then a lot more")
        return chunk

    monkeypatch.setattr(bounded_read.os, "read", growing)
    assert read_regular_file(path, max_bytes=100, require_stable=True).reason == "changed"
    path.write_bytes(b"first")
    assert read_regular_file(path, max_bytes=100).reason is None


def _swap_after_first_stat(monkeypatch, path, decoy):
    """Make the first `os.stat` of `path` report `decoy`, a regular file, as if `path` were swapped afterwards."""
    real_stat = os.stat
    seen = []

    def stat(target, *args, **kwargs):
        if str(target) == str(path) and not seen:
            seen.append(target)
            return real_stat(decoy)
        return real_stat(target, *args, **kwargs)

    monkeypatch.setattr(os, "stat", stat)


def test_without_o_nonblock_a_path_swapped_for_a_directory_after_the_stat_is_refused(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "O_NONBLOCK", raising=False)  # as on Windows
    decoy = tmp_path / "decoy.txt"
    decoy.write_bytes(b"ok")
    swapped = tmp_path / "swapped"
    swapped.mkdir()
    _swap_after_first_stat(monkeypatch, swapped, decoy)
    got = read_regular_file(swapped, max_bytes=10)
    # The descriptor check after the open still refuses what the early stat let through.
    assert (got.data, got.reason) == (None, "not-regular")


@pytest.mark.skipif(not HAS_FIFO, reason="POSIX FIFOs")
def test_without_o_nonblock_a_swapped_fifo_that_does_not_block_the_open_is_refused(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "O_NONBLOCK", raising=False)
    decoy = tmp_path / "decoy.txt"
    decoy.write_bytes(b"ok")
    fifo = tmp_path / "pipe"
    os.mkfifo(fifo)
    writer = os.open(fifo, os.O_RDWR)  # a writer is present, so the plain open returns
    try:
        _swap_after_first_stat(monkeypatch, fifo, decoy)
        with deadline():
            got = read_regular_file(fifo, max_bytes=10)
    finally:
        os.close(writer)
    # A FIFO with no writer would block this open: that limitation is documented in the helper.
    assert (got.data, got.reason) == (None, "not-regular")
