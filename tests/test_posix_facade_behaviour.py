"""Behavioural pins for the POSIX-only facade routing (issue #1478 follow-up).

Companion to ``test_posix_facade_guard.py`` (which scans sources): every new
fail-closed branch is reachable on POSIX with ``monkeypatch``, so these tests
exercise the behaviour directly instead of needing a Windows runner.
"""

from __future__ import annotations

import os

import pytest

from brigade import dirfd
from brigade import grokbot_jobs
from brigade import grokbot_reconcile
from brigade import proc
from brigade.grokbot_job_validation import GrokbotJobError
from brigade.grokbot_reconcile import ReconcileError


def test_directory_flags_match_legacy_expression() -> None:
    """The facade refactor must not change the POSIX flag computation."""
    expected = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    assert dirfd.directory_flags() == expected


def test_directory_flags_nofollow_false_matches_legacy_expression() -> None:
    """Root-anchor opens skip O_NOFOLLOW exactly like the old inline code."""
    expected = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0)
    assert dirfd.directory_flags(nofollow=False) == expected


def test_file_flags_match_legacy_expression() -> None:
    """The new file facade pins the old ``mode | O_NOFOLLOW | O_CLOEXEC`` shape."""
    mode = os.O_RDONLY
    expected = mode | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    assert dirfd.file_flags(mode) == expected


def test_directory_flags_fail_closed_without_o_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing O_DIRECTORY raises OSError, never AttributeError."""
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)
    with pytest.raises(OSError, match="unavailable"):
        dirfd.directory_flags()


def test_directory_flags_nofollow_false_still_fail_closed_without_o_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The root-anchor path also fails closed when O_DIRECTORY is gone."""
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)
    with pytest.raises(OSError, match="unavailable"):
        dirfd.directory_flags(nofollow=False)


def test_directory_flags_fail_closed_without_o_nofollow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing O_NOFOLLOW raises OSError, never AttributeError."""
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    with pytest.raises(OSError, match="unavailable"):
        dirfd.directory_flags()


def test_file_flags_fail_closed_without_o_nofollow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """File opens must not silently drop the symlink guard on Windows."""
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    with pytest.raises(OSError, match="unavailable"):
        dirfd.file_flags(os.O_RDONLY)


def test_grokbot_jobs_directory_flags_maps_to_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The jobs store surfaces the facade failure as unsafe-storage."""
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)
    with pytest.raises(GrokbotJobError, match="unsafe-storage"):
        grokbot_jobs._directory_flags()


def test_grokbot_reconcile_directory_flags_maps_to_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reconcile surfaces the facade failure as its own typed error."""
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)
    with pytest.raises(ReconcileError):
        grokbot_reconcile._directory_flags()


def test_process_group_id_matches_getpgid_on_posix() -> None:
    """The proc facade returns the real pgid where getpgid exists."""
    assert proc.process_group_id(os.getpid()) == os.getpgid(os.getpid())


def test_process_group_id_none_without_getpgid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows has no os.getpgid: the facade returns None instead of raising."""
    monkeypatch.delattr(os, "getpgid", raising=False)
    assert proc.process_group_id(os.getpid()) is None
