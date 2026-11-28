"""Portable FIFO fixture creation for tests."""

import errno
import os
from os import PathLike
from typing import TypeAlias

import pytest

_FIFO_UNSUPPORTED_ERRNOS = {
    errno.EPERM,
    errno.EINVAL,
    *(value for name in ("EOPNOTSUPP", "ENOTSUP") if (value := getattr(errno, name, None)) is not None),
}

_FifoPath: TypeAlias = str | bytes | PathLike[str] | PathLike[bytes]


def make_fifo(path: _FifoPath, mode: int = 0o666) -> None:
    """Create a FIFO, or skip when the platform or filesystem cannot."""
    mkfifo = getattr(os, "mkfifo", None)
    if mkfifo is None:
        pytest.skip("FIFO creation is unavailable on this platform")

    try:
        mkfifo(path, mode)
    except OSError as error:
        if error.errno in _FIFO_UNSUPPORTED_ERRNOS:
            pytest.skip(f"filesystem does not support FIFO creation: {error.strerror or error}")
        raise
