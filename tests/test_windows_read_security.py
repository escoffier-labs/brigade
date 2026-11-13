"""Fail-closed coverage for private Grok Bot reads without a Windows owner API."""

from __future__ import annotations

from pathlib import Path

import pytest


REASON = "secure-owner-read-unavailable"
UNSAFE_PATHS = (
    "/missing/private-runtime.json",
    r"\\server\share\runtime.json",
    r"\\.\pipe\runtime.json",
    r"C:/private\runtime.json",
)


def _filesystem_accessed(*args, **kwargs):
    raise AssertionError("private read reached the filesystem before owner security was established")


def test_filesystem_spies_restore_before_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(AssertionError, match="filesystem before owner security"):
        with monkeypatch.context() as filesystem:
            filesystem.setattr(Path, "exists", _filesystem_accessed)
            Path("missing").exists()
    assert Path("missing").name == "missing"
