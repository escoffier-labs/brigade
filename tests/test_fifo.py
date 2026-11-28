"""Tests for filesystem-aware FIFO fixture creation."""

import errno

import pytest

from tests import _fifo


def test_make_fifo_skips_when_filesystem_refuses_fifo(monkeypatch, tmp_path):
    def refuse_fifo(path, mode):
        raise OSError(errno.EPERM, "operation not permitted")

    monkeypatch.setattr(_fifo.os, "mkfifo", refuse_fifo, raising=False)

    with pytest.raises(pytest.skip.Exception, match="filesystem does not support FIFO creation"):
        _fifo.make_fifo(tmp_path / "marker")


def test_make_fifo_propagates_unrelated_os_error(monkeypatch, tmp_path):
    def fail_fifo(path, mode):
        raise OSError(errno.ENOSPC, "no space left")

    monkeypatch.setattr(_fifo.os, "mkfifo", fail_fifo, raising=False)

    with pytest.raises(OSError) as raised:
        _fifo.make_fifo(tmp_path / "marker")

    assert raised.value.errno == errno.ENOSPC


def test_make_fifo_skips_when_mkfifo_is_missing(monkeypatch, tmp_path):
    monkeypatch.delattr(_fifo.os, "mkfifo", raising=False)

    with pytest.raises(pytest.skip.Exception, match="FIFO creation is unavailable"):
        _fifo.make_fifo(tmp_path / "marker")
