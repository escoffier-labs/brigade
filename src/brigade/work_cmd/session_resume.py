"""Discover public-safe pointers to resumable local harness sessions.

Only the bounded metadata record at the start of a transcript is inspected.
Conversation events are deliberately neither read nor copied into Brigade.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

_MAX_HEADER_BYTES = 64 * 1024


def _header(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            line = handle.readline(_MAX_HEADER_BYTES + 1)
    except (OSError, UnicodeError):
        return None
    if not line or len(line.encode("utf-8")) > _MAX_HEADER_BYTES:
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _metadata(harness: str, value: dict[str, Any]) -> tuple[str, str] | None:
    if harness == "codex":
        if value.get("type") != "session_meta" or not isinstance(value.get("payload"), dict):
            return None
        value = value["payload"]
    session_id = value.get("sessionId") or value.get("session_id") or value.get("id")
    cwd = value.get("cwd")
    if not isinstance(session_id, str) or not session_id.strip() or not isinstance(cwd, str) or not cwd.strip():
        return None
    return session_id.strip(), cwd


def _candidates(home: Path) -> Iterator[tuple[str, Path]]:
    roots = (
        ("claude", home / ".claude" / "projects", "*.jsonl"),
        ("codex", home / ".codex" / "sessions", "*.jsonl"),
        ("codex", home / ".codex" / "archived_sessions", "*.jsonl"),
    )
    for harness, root, pattern in roots:
        if root.is_dir():
            for path in root.rglob(pattern):
                if path.is_file():
                    yield harness, path


def find_session_resume(target: Path, *, home: Path | None = None) -> dict[str, str] | None:
    """Return the newest matching local resume pointer, deterministically."""
    target = target.expanduser().resolve()
    home = (home or Path.home()).expanduser().resolve()
    matches: list[tuple[int, str, str, str]] = []
    for harness, path in _candidates(home):
        header = _header(path)
        parsed = _metadata(harness, header) if header is not None else None
        if parsed is None:
            continue
        session_id, cwd = parsed
        try:
            if Path(cwd).expanduser().resolve() != target:
                continue
            modified = path.stat().st_mtime_ns
            relative_path = path.resolve().relative_to(home)
        except (OSError, ValueError):
            continue
        matches.append((modified, relative_path.as_posix(), harness, session_id))
    if not matches:
        return None
    _modified, relative, harness, session_id = max(matches, key=lambda item: (item[0], item[1]))
    command = f"claude --resume {session_id}" if harness == "claude" else f"codex resume {session_id}"
    return {
        "harness": harness,
        "session_id": session_id,
        "command": command,
        "metadata": f"~/{relative}",
    }
