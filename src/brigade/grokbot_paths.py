"""Shared absolute-path root rules for the private Grok Bot packages.

Every Grok Bot package validates operator-supplied state paths with the same
root rules, so the rules live here once instead of in nine near-identical
copies.

These are pure string predicates over stdlib only. Callers pass ``windows``
from their own module-level ``os`` binding and raise their own package error
type, which keeps each package's public error contract intact and keeps the
per-module Windows simulation used by the tests effective.
"""

from __future__ import annotations

__all__ = ["is_drive_rooted", "has_allowed_absolute_root"]


def is_drive_rooted(value: str) -> bool:
    """Return True when ``value`` starts with a drive root, ``X:\\`` or ``X:/``."""
    return len(value) >= 3 and value[0].isascii() and value[0].isalpha() and value[1] == ":" and value[2] in {"\\", "/"}


def has_allowed_absolute_root(value: str, *, windows: bool) -> bool:
    """Return True when ``value`` carries an absolute root this platform accepts.

    UNC and device-namespace roots (``\\\\server\\share``, ``\\\\.\\pipe``,
    ``\\\\?\\C:``) are rejected everywhere.

    On Windows only a drive-absolute root is accepted. A slash-rooted path such
    as ``/var/lib/state`` carries no drive and resolves against the *current*
    drive, so it can name the same tree as ``C:\\var\\lib\\state`` while
    comparing unequal in a disjoint-path check. POSIX keeps accepting
    slash-rooted paths.
    """
    if value.replace("\\", "/").startswith("//"):
        return False
    if windows:
        return is_drive_rooted(value)
    return value.startswith("/")
