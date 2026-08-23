"""Shared helpers and constants for the test suite."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest


PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700


def assert_private_mode(path: Path, expected: int) -> None:
    """Assert a POSIX mode, or skip where Windows does not expose mode bits."""
    if os.name == "nt":
        pytest.skip("Windows does not expose POSIX permission bits")

    actual = stat.S_IMODE(path.stat().st_mode)
    assert actual == expected, f"expected {path} mode {expected:#o}, got {actual:#o}"
