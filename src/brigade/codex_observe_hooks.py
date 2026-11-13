"""Project hook installation and private write markers for Codex observe mode."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from . import agent_identity, dirfd, localio, observed_checks
from .inbox_lock import held_file_lock_descriptor as held_file_lock
from .observe_harness_hooks import CODEX_HOOK_COMMAND, CODEX_POST_TOOL_MATCHER


def _entries() -> dict[str, list[dict[str, Any]]]:
    handler = {"type": "command", "command": CODEX_HOOK_COMMAND, "async": True}
    return {"PostToolUse": [{"matcher": CODEX_POST_TOOL_MATCHER, "hooks": [handler]}], "Stop": [{"hooks": [handler]}]}


def _load(target: Path) -> dict[str, Any]:
    path = target / ".codex" / "hooks.json"
    try:
        fd = dirfd.open_file_nofollow(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return {}
    try:
        import stat

        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("not a regular file")
        raw = os.read(fd, 1_048_577)
    finally:
        os.close(fd)
    if len(raw) > 1_048_576:
        raise ValueError("oversized hooks")
    doc = json.loads(raw)
    if not isinstance(doc, dict) or not isinstance(doc.get("hooks", {}), dict):
        raise ValueError("invalid hooks")
    for entries in doc.get("hooks", {}).values():
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("invalid hook group")
    return doc


def codex_observe_hooks_installed(target: Path) -> bool:
    try:
        hooks = _load(target).get("hooks", {})
        return all(all(entry in hooks.get(event, []) for entry in entries) for event, entries in _entries().items())
    except (OSError, ValueError, RecursionError):
        return False


def install_codex_observe_hooks(target: Path) -> str | None:
    try:
        doc = _load(target)
        hooks = doc.setdefault("hooks", {})
        changed = False
        for event, entries in _entries().items():
            existing = hooks.setdefault(event, [])
            for entry in entries:
                if entry not in existing:
                    existing.append(entry)
                    changed = True
        if changed:
            parent = dirfd.open_directory_chain(target / ".codex", create=True)
            try:
                _replace_json(parent, "hooks.json", doc)
            finally:
                os.close(parent)
        return None
    except (OSError, ValueError, RecursionError):
        return "unable to install Codex observe hooks; check .codex/hooks.json"


def _replace_json(parent: int, name: str, data: dict[str, Any]) -> None:
    temp = f".{uuid.uuid4().hex}.tmp"
    fd = dirfd.open_child_file(parent, temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write((json.dumps(data, sort_keys=True, indent=2) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        observed_checks.check_capture_deadline()
        dirfd.replace_children(parent, temp, name, replace=True)
    finally:
        try:
            dirfd.unlink_child(parent, temp)
        except FileNotFoundError:
            pass


def observe_session_path(target: Path, session_id: str) -> Path:
    key = observed_checks.digest_key_path(target)
    if key is None:
        raise OSError("private state unavailable")
    # Match the check writer's keyed, private session identifier.
    fingerprint = observed_checks._session_digest(target, session_id)
    if fingerprint == "0000000000000000":
        raise OSError("session identity unavailable")
    return key.parent / "sessions" / f"{fingerprint}.json"


def record_write(target: Path, session_id: str) -> None:
    path = observe_session_path(target, session_id)
    parent = observed_checks._state_directory(path.parent.parent, create=True)
    try:
        observed_checks.check_capture_deadline()
        try:
            dirfd.mkdir_child(parent, "sessions", 0o700)
        except FileExistsError:
            pass
        sessions = dirfd.open_child_directory(parent, "sessions")
        try:
            with held_file_lock(path.with_suffix(".lock"), deadline_seconds=0.01):
                observed_checks.check_capture_deadline()
                state: dict[str, Any] = {}
                try:
                    fd = observed_checks._open_regular(sessions, path.name, os.O_RDONLY)
                except FileNotFoundError:
                    pass
                else:
                    try:
                        raw = json.loads(os.read(fd, 16385))
                        if isinstance(raw, dict) and isinstance(raw.get("model"), str):
                            state["model"] = agent_identity.map_identity("codex", raw["model"], "unknown")["model"]
                            if raw.get("model_source") in agent_identity.MODEL_SOURCES:
                                state["model_source"] = raw["model_source"]
                    finally:
                        os.close(fd)
                state["last_write_at"] = localio.utc_now_iso()
                _replace_json(sessions, path.name, state)
        finally:
            os.close(sessions)
    finally:
        os.close(parent)
