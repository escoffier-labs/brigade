"""Project hook installation and private write markers for Codex observe mode."""

from __future__ import annotations

import json
import os
import shlex
import stat
import uuid
from pathlib import Path
from typing import Any

from . import agent_identity, dirfd, localio, observed_checks
from .inbox_lock import held_file_lock_descriptor as held_file_lock
from .observe_harness_hooks import CODEX_HOOK_COMMAND, CODEX_POST_TOOL_MATCHER


def _entries(target: Path) -> dict[str, list[dict[str, Any]]]:
    # Match the base Claude project package: resolve Brigade through PATH.
    command = f"{CODEX_HOOK_COMMAND} --target {shlex.quote(str(target.resolve()))}"
    handler = {"type": "command", "command": command, "async": True}
    return {"PostToolUse": [{"matcher": CODEX_POST_TOOL_MATCHER, "hooks": [handler]}], "Stop": [{"hooks": [handler]}]}


def _load(target: Path) -> tuple[str, dict[str, Any], int]:
    path = target / ".codex" / "hooks.json"
    try:
        fd = dirfd.open_file_nofollow(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return "{}", {}, 0o600
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        raw = os.read(fd, 1_048_577)
    finally:
        os.close(fd)
    if len(raw) > 1_048_576:
        raise ValueError("oversized hooks")
    text = raw.decode("utf-8")
    doc = json.loads(text)
    if not isinstance(doc, dict) or not isinstance(doc.get("hooks", {}), dict):
        raise ValueError("invalid hooks")
    for entries in doc.get("hooks", {}).values():
        if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
            raise ValueError("invalid hook group")
    return text, doc, stat.S_IMODE(info.st_mode)


def _children(text: str) -> list[tuple[str | None, int, int]]:
    """Locate container values without translating whitespace or string bytes."""
    decoder = json.JSONDecoder()
    pos = 1
    children: list[tuple[str | None, int, int]] = []
    while True:
        while text[pos].isspace() or text[pos] == ",":
            pos += 1
        if text[pos] in "}]":
            return children
        key = None
        if text[0] == "{":
            key, pos = decoder.raw_decode(text, pos)
            while text[pos].isspace() or text[pos] == ":":
                pos += 1
        start = pos
        _, pos = decoder.raw_decode(text, pos)
        children.append((key, start, pos))


def _surgical(text: str, old: Any, new: Any) -> str:
    if old == new:
        return text
    leading = len(text) - len(text.lstrip())
    trailing = len(text.rstrip())
    if leading or trailing != len(text):
        return text[:leading] + _surgical(text[leading:trailing], old, new) + text[trailing:]
    if isinstance(old, dict) and isinstance(new, dict) and old.keys() <= new.keys():
        edits = [(start, end, _surgical(text[start:end], old[key], new[key])) for key, start, end in _children(text)]
        for start, end, replacement in reversed(edits):
            text = text[:start] + replacement + text[end:]
        additions = [
            json.dumps(key) + ": " + json.dumps(value, ensure_ascii=False)
            for key, value in new.items()
            if key not in old
        ]
        if additions:
            pos = text.rfind("}")
            text = text[:pos] + ("," if old else "") + ",".join(additions) + text[pos:]
        return text
    if isinstance(old, list) and isinstance(new, list):
        children = _children(text)
        edits = []
        pending = list(new)
        kept = []
        for index, (_, start, end) in enumerate(children):
            value = old[index]
            match = next(
                (
                    i
                    for i, item in enumerate(pending)
                    if item == value
                    or (
                        isinstance(value, dict)
                        and isinstance(item, dict)
                        and isinstance(value.get("hooks"), list)
                        and isinstance(item.get("hooks"), list)
                        and all(h in value["hooks"] for h in item["hooks"])
                        and {k: v for k, v in value.items() if k != "hooks"}
                        == {k: v for k, v in item.items() if k != "hooks"}
                    )
                ),
                None,
            )
            if match is not None:
                replacement = pending.pop(match)
                edits.append((start, end, _surgical(text[start:end], value, replacement)))
                kept.append(index)
        # Delete adjacent owned values as one span so commas remain valid.
        index = 0
        while index < len(children):
            if index in kept:
                index += 1
                continue
            first = index
            while index < len(children) and index not in kept:
                index += 1
            start = children[first][1]
            end = children[index - 1][2]
            if index < len(children):
                end = children[index][1]
            elif first:
                start = children[first - 1][2]
            edits.append((start, end, ""))
        for start, end, replacement in sorted(edits, reverse=True):
            text = text[:start] + replacement + text[end:]
        if pending:
            pos = text.rfind("]")
            addition = ",".join(json.dumps(item, ensure_ascii=False) for item in pending)
            text = text[:pos] + ("," if kept else "") + addition + text[pos:]
        return text
    return json.dumps(new, ensure_ascii=False)


def codex_observe_hooks_installed(target: Path) -> bool:
    try:
        hooks = _load(target)[1].get("hooks", {})
        return all(
            all(entry in hooks.get(event, []) for entry in entries) for event, entries in _entries(target).items()
        )
    except (OSError, ValueError, RecursionError):
        return False


def _reconcile(target: Path, *, install: bool) -> str | None:
    try:
        text, doc, mode = _load(target)
        updated = json.loads(json.dumps(doc))
        hooks = updated.setdefault("hooks", {}) if install else updated.get("hooks", {})
        for event, entries in _entries(target).items():
            existing = hooks.get(event, [])
            # Only exact owned groups, including the prior unpinned package.
            legacy = [
                {**entry, "hooks": [{"type": "command", "command": CODEX_HOOK_COMMAND, "async": True}]}
                for entry in entries
            ]
            owned = [entry["hooks"][0] for entry in entries + legacy]
            cleaned = []
            for entry in existing:
                if install and entry in entries:
                    cleaned.append(entry)
                    continue
                handlers = entry.get("hooks")
                if not isinstance(handlers, list):
                    cleaned.append(entry)
                    continue
                remaining = [handler for handler in handlers if handler not in owned]
                if remaining == handlers:
                    cleaned.append(entry)
                elif remaining:
                    cleaned.append({**entry, "hooks": remaining})
            if install:
                cleaned.extend(entry for entry in entries if entry not in cleaned)
            if event in hooks or cleaned:
                hooks[event] = cleaned
        if updated != doc:
            parent = dirfd.open_directory_chain(target / ".codex", create=install)
            try:
                _replace_bytes(parent, "hooks.json", _surgical(text, doc, updated).encode("utf-8"), mode)
            finally:
                os.close(parent)
        return None
    except (OSError, ValueError, RecursionError):
        return "unable to reconcile Codex observe hooks; check .codex/hooks.json"


def install_codex_observe_hooks(target: Path) -> str | None:
    from .claude_hooks.paths import is_operator_home

    # The home directory's hooks file is the user-level one Codex reads for every project.
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    try:
        user_level = (target / ".codex").resolve() == home.expanduser().resolve()
    except OSError:
        user_level = True
    if is_operator_home(target) or user_level:
        return "refusing to install project hooks in the Codex user-level hooks file; choose a project directory"
    return _reconcile(target, install=True)


def remove_codex_observe_hooks(target: Path) -> str | None:
    return _reconcile(target, install=False)


def _replace_json(parent: int, name: str, data: dict[str, Any]) -> None:
    _replace_bytes(parent, name, (json.dumps(data, sort_keys=True, indent=2) + "\n").encode("utf-8"), 0o600)


def _replace_bytes(parent: int, name: str, data: bytes, mode: int) -> None:
    temp = f".{uuid.uuid4().hex}.tmp"
    fd = dirfd.open_child_file(parent, temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            if os.name != "nt":
                os.fchmod(stream.fileno(), mode)
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
