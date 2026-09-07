"""Shared POSIX-only skip markers for the test suite (issue #1478 round 3).

Windows has no ``os.O_DIRECTORY`` / ``os.O_NOFOLLOW`` / ``os.O_PATH``,
no ``dir_fd`` support in ``os.open`` et al, and no ``os.getpgid`` /
``os.killpg``. Tests that exercise those primitives directly must skip
with a reason naming the missing primitive instead of raising
``AttributeError`` at collection or call time.

Use ``dirfd.posix_available()`` (not ``dirfd.available()``) for the
descriptor marker: ``available()`` is also true on Windows when the
``nt_dirfd`` handle backend is present, while a test calling raw
``os.open`` with ``O_DIRECTORY`` still crashes there. ``posix_available()``
is false on every non-POSIX platform, which is exactly the skip condition
for direct ``os.open`` / ``dir_fd`` use.
"""

from __future__ import annotations

import os

import pytest

from brigade import dirfd

requires_dirfd = pytest.mark.skipif(
    not dirfd.posix_available(),
    reason="descriptor-relative primitives (O_DIRECTORY/O_NOFOLLOW/dir_fd) are POSIX-only",
)

requires_process_groups = pytest.mark.skipif(
    not hasattr(os, "killpg") or not hasattr(os, "getpgid"),
    reason="process groups (os.killpg/os.getpgid) are POSIX-only",
)

requires_opath = pytest.mark.skipif(
    os.name != "posix" or not getattr(os, "O_PATH", 0),
    reason="O_PATH directory descriptors are POSIX-only",
)
