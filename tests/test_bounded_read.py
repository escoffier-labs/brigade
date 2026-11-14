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
