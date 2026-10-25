"""Portable relative-path validation and read-only memory glob matching."""

from __future__ import annotations

import re
from pathlib import PureWindowsPath


def relative_path(value: str, *, field: str) -> str:
    clean = value.strip()
    # Windows anchors cover POSIX roots as well as drive-relative, rooted and UNC
    # paths. is_absolute() alone misses rooted paths without a drive on Windows.
    path = PureWindowsPath(clean)
    if path.anchor or ".." in path.parts or not clean:
        raise ValueError(f"{field} must be a non-empty relative path without '..'")
    return clean


def _clean_relative(value: str) -> str:
    """Normalize separators and drop leading ``./`` and trailing ``/``."""
    clean = value.replace("\\", "/").strip()
    while clean.startswith("./"):
        clean = clean[2:]
    return clean.rstrip("/")


def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a path glob: ``*`` and ``?`` stay inside one segment, ``**`` spans segments."""
    clean = _clean_relative(pattern)
    out: list[str] = []
    i = 0
    while i < len(clean):
        char = clean[i]
        if clean.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif clean.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out))


def read_only_pattern(path: str, patterns: tuple[str, ...]) -> str | None:
    """Return the first ``read_only_paths`` pattern that covers ``path``, else None.

    A pattern covers a path when the glob matches the whole relative path or any
    parent directory of it, so ``memory/cards/policy`` protects its descendants.
    """
    parts = _clean_relative(path).split("/")
    candidates = ["/".join(parts[: n + 1]) for n in range(len(parts))]
    for pattern in patterns:
        regex = _glob_regex(pattern)
        if any(regex.fullmatch(candidate) for candidate in candidates):
            return pattern
    return None
