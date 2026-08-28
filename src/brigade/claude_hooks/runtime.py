"""Claude Code hook runtime for the Brigade work loop."""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from datetime import datetime
from queue import Empty
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator

from .. import localio
from ..component_paths import cache_root
from ..work_cmd.verification import _tree_fingerprint
from ..wiring import resolve_wired_target
from . import compaction_marker, envelope
from .package import PACKAGE_REF
from .paths import is_operator_home, resolved_path
from .session_state import (
    _ANNOUNCE_CLAIMED_KEY,
    _claim_timeout_announce,
    _clear_hook_timeouts,
    _read_hook_latch,
    _record_hook_timeout,
    _session_state_lock,  # noqa: F401 - runtime compatibility alias
    _write_session_state_preserving_latch,
)

BRIEF_TIMEOUT_SECONDS = 10
HOOK_TIMEOUT_LATCH_AFTER = 2
_TIMEOUT_FOLLOWUP_SECONDS = 0.5
# Inner lock acquire only. Must stay strictly below the 0.5s outer follow-up
# so a contended lock fails open to the journal path instead of consuming
# the whole parent wait. Do not add observe slack or a second sequential
# announce wait: 12s worker + 1.2s terminate + 0.5s claim + 0.5s follow-up
# = 14.2s under the 15s handler.
_SESSION_STATE_LOCK_SECONDS = 0.25
_HOOK_OUTCOME_KINDS = frozenset({"ok", "latched", "latched_silent"})

MAX_RECENT_SESSION_STATES = 512
MAX_HOOK_STDIN_BYTES = 1_048_576
MAX_HOOK_STDIN_STRING_CHARS = 262_144
MAX_HOOK_STDIN_COLLECTION_ITEMS = 4096
MAX_HOOK_STDIN_NESTING_DEPTH = 32
_HOOK_STDIN_READ_CHUNK = 4096
CLAUDE_SESSION_ENV = "BRIGADE_CLAUDE_SESSION"
_HOOK_WORKER_EVENT_ENV = "BRIGADE_HOOK_WORKER_EVENT"
_HOOK_PIN_ENV = "BRIGADE_HOOK_PIN_TARGET"
_HOOK_TEST_HANG_ENV = "BRIGADE_HOOK_TEST_HANG_SECONDS"
_HOOK_TIMEOUT_ENV = "BRIGADE_HOOK_TIMEOUT_SECONDS"
_HOOK_WORKER_COMMAND = (
    "import json, os, sys, time; from brigade.claude_hooks.runtime import _hook_worker_main; _hook_worker_main()"
)


class HookDegraded(Exception):
    """Internal hook failure that must fail open with a doctor pointer."""

    def __init__(self, message: str, *, log_target: Path | None = None) -> None:
        super().__init__(message)
        self.log_target = log_target


_SHELL_SEPARATORS = {"&&", "||", ";", "|", "|&", "&", "\n"}
_PIPELINE_SEPARATORS = {"|", "|&"}
_SHELL_WRAPPERS = {"bash", "dash", "ksh", "sh", "zsh"}
_SHELL_CONTROL_PREFIXES = {"!", "{", "do", "elif", "if", "then", "time", "until", "while"}
_SHELL_CONTROL_TOKENS = _SHELL_CONTROL_PREFIXES | {"}", "case", "done", "else", "esac", "fi", "for", "select"}
_HEREDOC_DELIMITER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}
_SNAPSHOT_IGNORE_DIRS = {
    ".brigade",
    ".git",
    ".hg",
    ".svn",
    ".tox",
    ".venv",
    "__pycache__",
    "node_modules",
}
_SNAPSHOT_GIT_TIMEOUT_SECONDS = 3
_UNAVAILABLE_FINGERPRINT = "unavailable"
_BASH_WRITE_COMMANDS = {
    "apply_patch",
    "cp",
    "install",
    "mkdir",
    "mktemp",
    "mv",
    "patch",
    "rm",
    "rmdir",
    "tee",
    "touch",
    "truncate",
}
_GIT_WRITE_COMMANDS = {
    "add",
    "apply",
    "checkout",
    "cherry-pick",
    "clean",
    "commit",
    "merge",
    "mv",
    "rebase",
    "reset",
    "restore",
    "revert",
    "rm",
    "switch",
}


def _sessions_root(target: Path) -> Path:
    return target / ".brigade" / "work" / "claude-hooks" / "sessions"


def _state_path(target: Path, session_id: str) -> Path:
    slug = localio.slugify(session_id, fallback="session")[:80]
    suffix = localio.stable_hash(session_id)[:8]
    return _sessions_root(target) / f"{slug}-{suffix}.json"


# SessionStart sources that begin a new dispatched task. Fleet dispatchers
# reuse one Claude session id across unrelated tasks, so the session id alone
# cannot scope work-loop state; each of these sources marks a task boundary.
# ``resume`` and ``compact`` continue the same logical task and keep its state.
_TASK_BOUNDARY_SOURCES = {"clear", "startup"}


def _task_epoch_path(session_id: str) -> Path:
    root = Path(cache_root()) / "brigade" / "claude-hooks" / "task-epochs"
    return root / f"{localio.stable_hash(session_id)}.json"


def _current_task_epoch(session_id: str) -> str | None:
    try:
        record = localio.read_json_dict(_task_epoch_path(session_id))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict):
        return None
    epoch = record.get("task_epoch")
    return epoch if isinstance(epoch, str) and epoch else None


def _begin_task_epoch(session_id: str) -> str | None:
    epoch = f"{localio.utc_now_iso()}#{os.getpid()}"
    try:
        localio.write_json(_task_epoch_path(session_id), {"session_id": session_id, "task_epoch": epoch})
    except (OSError, ValueError):
        # Fail open: without a durable boundary marker the hook falls back to
        # per-session scoping instead of guessing at task membership.
        return _current_task_epoch(session_id)
    return epoch


def _task_epoch_for_event(event: str, payload: dict[str, Any], session_id: str) -> str | None:
    if event == "SessionStart" and payload.get("source") in _TASK_BOUNDARY_SOURCES:
        return _begin_task_epoch(session_id)
    return _current_task_epoch(session_id)


def read_session_state(target: Path, session_id: str) -> dict[str, Any] | None:
    return localio.read_json_dict(_state_path(target.expanduser().resolve(), session_id))


def write_session_state(target: Path, session_id: str, payload: dict[str, Any]) -> None:
    localio.write_json(_state_path(target.expanduser().resolve(), session_id), payload)


def iter_session_states(
    target: Path,
    *,
    modified_since: datetime | None = None,
    limit: int | None = None,
) -> Iterator[dict[str, Any]]:
    root = _sessions_root(target.expanduser().resolve())
    if not root.is_dir():
        return
    candidates: list[tuple[float, Path]] = []
    threshold = modified_since.timestamp() if modified_since is not None else None
    for path in root.glob("*.json"):
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if threshold is not None and modified < threshold:
            continue
        candidates.append((modified, path))
    candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    if limit is not None:
        candidates = candidates[: max(limit, 0)]
    for _, path in candidates:
        state = localio.read_json_dict(path)
        if isinstance(state, dict):
            yield state


def _advance_quote_state(text: str, quote: str | None) -> str | None:
    index = 0
    while index < len(text):
        char = text[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if char == "\\" and index + 1 < len(text):
                index += 2
                continue
            if char == '"':
                quote = None
            index += 1
            continue
        if char == "#" and (index == 0 or text[index - 1] in " \t"):
            break
        if char == "'":
            quote = "'"
            index += 1
            continue
        if char == '"':
            quote = '"'
            index += 1
            continue
        index += 1
    return quote


def _strip_shell_comment_suffix(line: str) -> str:
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if char == "\\" and index + 1 < len(line):
                index += 2
                continue
            if char == '"':
                quote = None
            index += 1
            continue
        if char == "#" and (index == 0 or line[index - 1] in " \t"):
            return line[:index].rstrip(" \t")
        if char == "'":
            quote = "'"
            index += 1
            continue
        if char == '"':
            quote = '"'
            index += 1
            continue
        index += 1
    return line


def _parse_heredoc_delimiter(text: str, start: int) -> tuple[str, bool, int] | None:
    index = start + 2
    strip_tabs = False
    while index < len(text) and text[index] in " \t":
        index += 1
    if index < len(text) and text[index] == "-":
        strip_tabs = True
        index += 1
        while index < len(text) and text[index] in " \t":
            index += 1
    if index >= len(text):
        return None
    char = text[index]
    if char == "'":
        end = text.find("'", index + 1)
        if end < 0:
            return None
        return text[index + 1 : end], strip_tabs, end + 1
    if char == '"':
        end = index + 1
        value: list[str] = []
        while end < len(text):
            if text[end] == "\\" and end + 1 < len(text):
                value.append(text[end + 1])
                end += 2
                continue
            if text[end] == '"':
                return "".join(value), strip_tabs, end + 1
            value.append(text[end])
            end += 1
        return None
    if char == "\\":
        match = _HEREDOC_DELIMITER.match(text, index + 1)
        if not match:
            return None
        return match.group(0), strip_tabs, match.end()
    match = _HEREDOC_DELIMITER.match(text, index)
    if not match:
        return None
    return match.group(0), strip_tabs, match.end()


def _heredoc_delimiters_on_line(line: str) -> list[tuple[str, bool]]:
    tags: list[tuple[str, bool]] = []
    quote: str | None = None
    index = 0
    while index < len(line):
        char = line[index]
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if quote == '"':
            if char == "\\" and index + 1 < len(line):
                index += 2
                continue
            if char == '"':
                quote = None
            index += 1
            continue
        if char == "'" and quote is None:
            quote = "'"
            index += 1
            continue
        if char == '"' and quote is None:
            quote = '"'
            index += 1
            continue
        if char == "#" and quote is None and (index == 0 or line[index - 1] in " \t"):
            break
        if line.startswith("<<", index) and quote is None:
            parsed = _parse_heredoc_delimiter(line, index)
            if parsed is None:
                index += 2
                continue
            tag, strip_tabs, end = parsed
            tags.append((tag, strip_tabs))
            index = end
            continue
        index += 1
    return tags


def _strip_heredoc_bodies(command: str) -> str:
    """Drop heredoc document lines so their content is data, not commands."""
    lines = command.split("\n")
    kept: list[str] = []
    terminators: list[tuple[str, bool]] = []
    quote: str | None = None
    for line in lines:
        if terminators:
            tag, strip_tabs = terminators[0]
            body = line.lstrip("\t") if strip_tabs else line
            if body == tag or (not strip_tabs and line.rstrip("\r") == tag):
                terminators.pop(0)
            continue
        if quote is not None:
            kept.append(line)
            quote = _advance_quote_state(line, quote)
            continue
        kept.append(_strip_shell_comment_suffix(line))
        quote = _advance_quote_state(line, None)
        if quote is not None:
            continue
        terminators.extend(_heredoc_delimiters_on_line(line))
    return "\n".join(kept)


def _resolve_command_path(raw: str, cwd: Path) -> Path:
    expanded = Path(os.path.expandvars(raw)).expanduser()
    if not expanded.is_absolute():
        return (cwd / expanded).resolve(strict=False)
    return expanded.resolve(strict=False)


def _unwrap_command_tokens(tokens: list[str]) -> list[str]:
    result = list(tokens)
    while result and Path(result[0]).name in {"tokenjuice", "token-glace"} and "--" in result:
        result = result[result.index("--") + 1 :]
    return result


def _strip_command_prefixes(tokens: list[str]) -> list[str]:
    result = _unwrap_command_tokens(tokens)
    while result and _is_env_assignment(result[0]):
        result.pop(0)
    while result and Path(result[0]).name in {"command", "env", "nice", "sudo", "time"}:
        result.pop(0)
        while result and result[0].startswith("-"):
            result.pop(0)
        while result and _is_env_assignment(result[0]):
            result.pop(0)
    return result


def _nested_shell_script(tokens: list[str]) -> str | None:
    return _shell_wrapper_payload(_strip_command_prefixes(tokens))


def _command_segment_groups(command: str) -> tuple[bool, list[tuple[str | None, list[str]]]]:
    try:
        tokens = _shell_tokens(command)
    except ValueError:
        return False, []
    groups: list[tuple[str | None, list[str]]] = []
    current: list[str] = []
    separator: str | None = None
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            if current:
                groups.append((separator, current))
                current = []
            separator = token
        else:
            current.append(token)
    if current:
        groups.append((separator, current))
    return True, groups


def _segment_effective_cwd(segment: list[str], cwd: Path) -> Path:
    index = 0
    while index < len(segment):
        if segment[index] == "cd" and index + 1 < len(segment):
            raw = segment[index + 1]
            if raw != "-":
                cwd = _resolve_command_path(raw, cwd)
            index += 2
            continue
        index += 1
    return cwd


def _segment_cd_targets(segment: list[str], cwd: Path) -> list[Path]:
    targets: list[Path] = []
    index = 0
    effective = cwd
    while index < len(segment):
        if segment[index] == "cd" and index + 1 < len(segment):
            raw = segment[index + 1]
            if raw != "-":
                effective = _resolve_command_path(raw, effective)
                targets.append(effective)
            index += 2
            continue
        index += 1
    return targets


def _effective_cwd_at_position(command: str, cwd: Path, position: int) -> Path:
    tokenized, groups = _command_segment_groups(command[:position])
    if not tokenized:
        return cwd
    effective = cwd
    for separator, segment in groups:
        if separator in _PIPELINE_SEPARATORS:
            effective = cwd
        effective = _segment_effective_cwd(segment, effective)
    return effective


def _verifier_target_from_tokens(tokens: list[str], cwd: Path) -> Path | None:
    stripped = _strip_env(tokens)
    if not _is_brigade_verify(stripped):
        return None
    for index, token in enumerate(stripped[:-1]):
        if token in ("--target", "-t"):
            return _resolve_command_path(stripped[index + 1], cwd)
    return cwd.resolve(strict=False)


def _verifier_target_from_command(command: str, cwd: Path, *, depth: int = 0) -> Path | None:
    stripped_command = _strip_heredoc_bodies(command)
    try:
        tokens = shlex.split(stripped_command, posix=True)
    except ValueError:
        tokens = []
    nested = _nested_shell_script(tokens)
    if nested is not None and depth < 4:
        found = _verifier_target_from_command(nested, cwd, depth=depth + 1)
        if found is not None:
            return found
    tokenized, groups = _command_segment_groups(stripped_command)
    if not tokenized:
        return None
    if depth < 4:
        for position, nested in _nested_shell_command_spans(stripped_command):
            nested_cwd = _effective_cwd_at_position(stripped_command, cwd, position)
            found = _verifier_target_from_command(nested, nested_cwd, depth=depth + 1)
            if found is not None:
                return found
    effective = cwd
    for separator, segment in groups:
        if separator in _PIPELINE_SEPARATORS:
            effective = cwd
        effective = _segment_effective_cwd(segment, effective)
        nested_segment = _nested_shell_script(_strip_command_prefixes(segment))
        if nested_segment is not None and depth < 4:
            found = _verifier_target_from_command(nested_segment, effective, depth=depth + 1)
            if found is not None:
                return found
        target = _verifier_target_from_tokens(segment, effective)
        if target is not None:
            return target
    return None


def _command_candidate_paths(command: str, cwd: Path, *, depth: int = 0, include_cwd: bool = False) -> list[Path]:
    command = _strip_heredoc_bodies(command)
    candidates: list[Path] = []
    tokenized, groups = _command_segment_groups(command)
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        tokens = []
    nested = _nested_shell_script(tokens)
    if nested is not None and depth < 4:
        candidates.extend(_command_candidate_paths(nested, cwd, depth=depth + 1, include_cwd=include_cwd))
    if tokenized:
        effective = cwd
        for separator, segment in groups:
            if separator in _PIPELINE_SEPARATORS:
                effective = cwd
            candidates.extend(_segment_cd_targets(segment, effective))
            effective = _segment_effective_cwd(segment, effective)
    for flag in ("--target", "-t"):
        for index, token in enumerate(tokens[:-1]):
            if token == flag:
                candidates.append(_resolve_command_path(tokens[index + 1], cwd))
    for index, token in enumerate(tokens[:-1]):
        if Path(token).name == "git" and tokens[index + 1] == "-C" and index + 2 < len(tokens):
            candidates.append(_resolve_command_path(tokens[index + 2], cwd))
    if include_cwd:
        # Session cwd takes precedence over incidental bare paths so a command
        # that merely mentions another wired repo (e.g. `rg pattern {other}/file`)
        # is still attributed to the session repo, not the mentioned path.
        candidates.append(cwd)
    for token in tokens:
        if token.startswith("-") or token in _SHELL_SEPARATORS:
            continue
        if any(ch.isspace() for ch in token):
            continue
        if token in {".", ".."} or "/" in token or "\\" in token or token.startswith("~"):
            candidates.append(_resolve_command_path(token, cwd))
    return candidates


def _as_tool_input(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def wired_target_from_payload(payload: dict[str, Any]) -> Path | None:
    try:
        cwd = Path(str(payload.get("cwd") or ".")).expanduser().resolve(strict=False)
    except OSError:
        return resolve_wired_target(payload.get("cwd"))
    has_cwd = bool(payload.get("cwd"))
    tool_name = str(payload.get("tool_name") or "")
    tool_input = _as_tool_input(payload.get("tool_input"))
    if tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        verifier_target = _verifier_target_from_command(command, cwd)
        if verifier_target is not None:
            wired = resolve_wired_target(str(verifier_target))
            if wired is not None:
                return wired
        for candidate in _command_candidate_paths(command, cwd, include_cwd=has_cwd):
            wired = resolve_wired_target(str(candidate))
            if wired is not None:
                return wired
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            wired = resolve_wired_target(str(_resolve_command_path(value, cwd)))
            if wired is not None:
                return wired
    return resolve_wired_target(payload.get("cwd"))


def _path_is_within(inner: Path, outer: Path) -> bool:
    resolved_inner = resolved_path(inner)
    resolved_outer = resolved_path(outer)
    if resolved_inner is None or resolved_outer is None:
        return False
    try:
        return resolved_inner.is_relative_to(resolved_outer)
    except ValueError:
        return False


def _hook_pin_from_env() -> Path | None:
    raw = os.environ.get(_HOOK_PIN_ENV)
    if raw is None or not raw.strip():
        return None
    return resolved_path(Path(raw))


def _resolve_pin(target: Path | None) -> Path | None:
    if target is None:
        return _hook_pin_from_env()
    resolved = resolved_path(target)
    if resolved is None or is_operator_home(resolved):
        return None
    return resolved


def _session_id_from_payload(payload: dict[str, Any]) -> str:
    raw = payload.get("session_id")
    if isinstance(raw, str) and raw.strip():
        return raw
    return "unknown"


def effective_hook_target(payload: dict[str, Any], *, pin: Path | None = None) -> Path | None:
    """Return the workspace a hook may operate on, or None to no-op.

    User-scope installs fire for every Claude session. Home is never a work
    target (even when wired): fingerprinting and brief against that tree is
    what times out on every tool call. An optional ``--target`` pin restricts
    work to that wired workspace and no-ops everywhere else.
    """
    derived = wired_target_from_payload(payload)
    resolved_pin = _resolve_pin(pin)
    if resolved_pin is not None:
        if derived is not None and _path_is_within(derived, resolved_pin):
            return None if is_operator_home(derived) else derived
        raw_cwd = payload.get("cwd")
        if isinstance(raw_cwd, str) and raw_cwd.strip():
            cwd = resolved_path(Path(raw_cwd))
            if cwd is not None and _path_is_within(cwd, resolved_pin) and not is_operator_home(resolved_pin):
                return resolved_pin if resolve_wired_target(str(resolved_pin)) is not None else None
        return None
    if derived is None or is_operator_home(derived):
        return None
    return derived


def _session_repo_union(session_id: str, *seeds: Path, task_epoch: str | None = None) -> list[str]:
    repos: set[str] = set()
    pending = [seed.expanduser().resolve() for seed in seeds]
    seen: set[str] = set()
    while pending:
        current = pending.pop()
        key = str(current)
        if key in seen:
            continue
        seen.add(key)
        repos.add(key)
        state = read_session_state(current, session_id)
        if state is None:
            continue
        if task_epoch is not None and state.get("task_epoch") != task_epoch:
            # An earlier dispatched task's repo graph; a reused session id must
            # not pull those repos back into the current task's scope (#992).
            continue
        raw_repos = state.get("session_repos")
        if not isinstance(raw_repos, list):
            continue
        for raw in raw_repos:
            if isinstance(raw, str) and raw and raw not in seen:
                pending.append(Path(raw))
    return sorted(repos)


def _link_session_targets(session_id: str, *targets: Path, task_epoch: str | None = None) -> None:
    sorted_repos = _session_repo_union(session_id, *targets, task_epoch=task_epoch)
    for repo in sorted_repos:
        target = Path(repo)
        state = read_session_state(target, session_id)
        if state is None:
            continue
        if task_epoch is not None and state.get("task_epoch") != task_epoch:
            # This repo is entering the current task, so its stale entry from a
            # previous task expires now instead of latching write_observed.
            state = _normalize_state(target, session_id, state, task_epoch=task_epoch)
        if state.get("session_repos") == sorted_repos:
            continue
        updated = dict(state)
        updated["session_repos"] = sorted_repos
        _write_session_state_preserving_latch(target, session_id, updated)


def _touch_session_targets(
    session_id: str,
    target: Path,
    payload: dict[str, Any],
    *,
    task_epoch: str | None = None,
) -> None:
    cwd_target = resolve_wired_target(payload.get("cwd"))
    if cwd_target is not None:
        _link_session_targets(session_id, target, cwd_target, task_epoch=task_epoch)
    else:
        _link_session_targets(session_id, target, task_epoch=task_epoch)


def _stop_targets(payload: dict[str, Any], session_id: str, *, task_epoch: str | None = None) -> list[Path]:
    cwd_target = resolve_wired_target(payload.get("cwd"))
    ordered: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path.expanduser().resolve())
        if key in seen:
            return
        seen.add(key)
        ordered.append(path)

    if cwd_target is None:
        return ordered
    state = read_session_state(cwd_target, session_id)
    raw_repos = state.get("session_repos") if isinstance(state, dict) else None
    if task_epoch is not None and isinstance(state, dict) and state.get("task_epoch") != task_epoch:
        # A previous task's repo graph: evaluating it would demand wiring,
        # verification, or handoffs for directories this task never touched.
        raw_repos = None
    if isinstance(raw_repos, list):
        for raw in raw_repos:
            if isinstance(raw, str) and raw:
                add(Path(raw))
    add(cwd_target)
    return ordered


def _run_brief(target: Path) -> str:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "brigade", "work", "brief", "--target", str(target)],
            cwd=target,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=BRIEF_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HookDegraded(f"brief unavailable: {exc}") from exc
    text = result.stdout.strip()
    if result.returncode != 0 or not text:
        detail = (result.stderr or "").strip() or f"exit {result.returncode}"
        raise HookDegraded(f"brief failed: {detail}")
    return text


def _run_recall(target: Path, payload: dict[str, Any]) -> str:
    """Bounded memory recall for SessionStart; empty string on any failure."""
    from .. import memory_hooks

    raw_cwd = payload.get("cwd")
    cwd: Path | None
    if isinstance(raw_cwd, str) and raw_cwd.strip():
        try:
            cwd = Path(raw_cwd).expanduser().resolve(strict=False)
        except OSError:
            cwd = None
    else:
        cwd = None
    try:
        return memory_hooks.recall_text_for_hook(wired_target=target, cwd=cwd)
    except Exception:  # noqa: BLE001 - recall must never block session start
        return ""


def _brief_records(text: str) -> list[str]:
    """Split a work brief into whole-record units for capped injection."""
    lines = [line.rstrip() for line in text.splitlines()]
    records = [line for line in lines if line.strip()]
    return records if records else [text]


def _is_env_assignment(token: str) -> bool:
    if "=" not in token:
        return False
    key = token.split("=", 1)[0]
    return bool(key and (key[0].isalpha() or key[0] == "_") and all(char.isalnum() or char == "_" for char in key))


def _strip_env(tokens: list[str]) -> list[str]:
    remaining = list(tokens)
    while remaining and _is_env_assignment(remaining[0]):
        remaining.pop(0)

    if remaining and Path(remaining[0]).name == "env":
        remaining.pop(0)
        while remaining:
            option = remaining[0]
            if option == "--":
                remaining.pop(0)
                break
            if _is_env_assignment(option):
                remaining.pop(0)
                continue
            if option in {
                "-i",
                "--ignore-environment",
                "-0",
                "--null",
                "-v",
                "--debug",
                "--block-signal",
                "--default-signal",
                "--ignore-signal",
                "--list-signal-handling",
            }:
                remaining.pop(0)
                continue
            if (
                len(option) > 1
                and option.startswith("-")
                and not option.startswith("--")
                and all(flag in {"i", "0", "v"} for flag in option[1:])
            ):
                remaining.pop(0)
                continue
            if option in {"-u", "--unset", "-C", "--chdir", "-a", "--argv0"}:
                if len(remaining) < 2:
                    return []
                del remaining[:2]
                continue
            if option.startswith(
                (
                    "--unset=",
                    "--chdir=",
                    "--argv0=",
                    "--block-signal=",
                    "--default-signal=",
                    "--ignore-signal=",
                )
            ) or (option.startswith(("-a", "-C", "-u")) and option not in {"-a", "-C", "-u"}):
                remaining.pop(0)
                continue
            if option in {"-S", "--split-string"}:
                if len(remaining) < 2:
                    return []
                try:
                    split = shlex.split(remaining[1], posix=os.name != "nt")
                except ValueError:
                    return []
                return _strip_env([*split, *remaining[2:]])
            if option.startswith("--split-string=") or (option.startswith("-S") and option != "-S"):
                value = option.split("=", 1)[1] if option.startswith("--split-string=") else option[2:]
                try:
                    split = shlex.split(value, posix=os.name != "nt")
                except ValueError:
                    return []
                return _strip_env([*split, *remaining[1:]])
            if option.startswith("-"):
                return []
            break

    while remaining and _is_env_assignment(remaining[0]):
        remaining.pop(0)

    if remaining and Path(remaining[0]).name == "command":
        remaining.pop(0)
        if remaining[:1] == ["-p"]:
            remaining.pop(0)
        elif remaining[:1] in (["-v"], ["-V"]):
            return []
        if remaining[:1] == ["--"]:
            remaining.pop(0)
        elif remaining and remaining[0].startswith("-"):
            return []
    return remaining


def _strip_npx_options(tokens: list[str]) -> tuple[list[str], bool]:
    remaining = list(tokens)
    no_value = {
        "-y",
        "--yes",
        "--no-install",
        "--ignore-existing",
        "--prefer-offline",
        "--prefer-online",
        "--offline",
        "--foreground-scripts",
        "--ignore-scripts",
        "-q",
        "--quiet",
    }
    with_value = {"-p", "--package", "--shell", "--prefix", "--node-options"}
    while remaining:
        option = remaining[0]
        if option == "--":
            remaining.pop(0)
            break
        if option in no_value:
            remaining.pop(0)
            continue
        if option in with_value:
            if len(remaining) < 2:
                return [], True
            del remaining[:2]
            continue
        if option.startswith(("--package=", "--shell=", "--prefix=", "--node-options=")):
            remaining.pop(0)
            continue
        if option.startswith("-"):
            return remaining, True
        break
    return remaining, False


def _package_runner_script(tokens: list[str]) -> str | None:
    remaining = [Path(token).name for token in tokens]
    if remaining[:1] == ["test"]:
        return "test"
    if remaining[:1] != ["run"]:
        return None
    remaining.pop(0)
    no_value = {"-s", "--silent", "--if-present", "--ignore-scripts", "--foreground-scripts"}
    while remaining:
        option = remaining[0]
        if option == "--":
            remaining.pop(0)
            continue
        if option in no_value:
            remaining.pop(0)
            continue
        if option.startswith("-"):
            return None
        break
    return remaining[0] if remaining else None


def _strip_make_global_options(tokens: list[str]) -> tuple[list[str], bool]:
    remaining = list(tokens)
    no_value = {
        "-k",
        "-n",
        "-q",
        "-s",
        "-t",
        "--always-make",
        "--dry-run",
        "--ignore-errors",
        "--just-print",
        "--keep-going",
        "--no-builtin-rules",
        "--no-print-directory",
        "--print-directory",
        "--question",
        "--quiet",
        "--recon",
        "--silent",
        "--touch",
        "--version",
        "-v",
        "-w",
        "-p",
    }
    with_value = {
        "-I",
        "--include-dir",
        "-C",
        "--directory",
        "-j",
        "--jobs",
        "-o",
        "--old-file",
        "-W",
        "--what-if",
        "--load-average",
    }
    while remaining:
        option = remaining[0]
        if option == "--":
            remaining.pop(0)
            break
        if option in no_value:
            remaining.pop(0)
            continue
        if option in with_value:
            if len(remaining) < 2:
                return [], True
            del remaining[:2]
            continue
        if any(option.startswith(f"{known}=") for known in with_value):
            remaining.pop(0)
            continue
        if option.startswith("-j") and len(option) > 2 and option[2:].isdigit():
            remaining.pop(0)
            continue
        if option.startswith(("-C", "-I", "-o", "-W")) and len(option) > 2:
            remaining.pop(0)
            continue
        if option.startswith("-"):
            return remaining, True
        break
    return remaining, False


def _strip_runner_global_options(command: str, tokens: list[str]) -> tuple[list[str], bool]:
    no_value: dict[str, set[str]] = {
        "uv": {"-q", "--quiet", "-v", "--verbose", "--no-config", "--offline", "--no-cache"},
        "poetry": {"-n", "--no-interaction", "--no-ansi", "-v", "-vv", "-vvv"},
        "npm": {"-s", "--silent"},
        "pnpm": {"-s", "--silent", "-r", "--recursive"},
        "yarn": {"-s", "--silent"},
        "bun": {"--silent"},
    }
    with_value: dict[str, set[str]] = {
        "uv": {"--directory", "--project", "--config-file", "--cache-dir", "--color"},
        "poetry": {"-C", "--directory", "-P", "--project"},
        "npm": {"-w", "--workspace", "--prefix"},
        "pnpm": {"-F", "--filter", "-C", "--dir", "--workspace-dir"},
        "yarn": {"--cwd"},
        "bun": {"--cwd"},
    }
    remaining = list(tokens)
    command_no_value = no_value.get(command, set())
    command_with_value = with_value.get(command, set())
    while remaining:
        option = remaining[0]
        if option == "--":
            remaining.pop(0)
            break
        if option in command_no_value:
            remaining.pop(0)
            continue
        if option in command_with_value:
            if len(remaining) < 2:
                return [], True
            del remaining[:2]
            continue
        if any(
            option.startswith(f"{known}=")
            or (
                known.startswith("-")
                and not known.startswith("--")
                and option.startswith(known)
                and len(option) > len(known)
            )
            for known in command_with_value
        ):
            remaining.pop(0)
            continue
        if option.startswith("-"):
            return remaining, True
        break
    return remaining, False


def _strip_runner_run_options(command: str, tokens: list[str]) -> tuple[list[str], bool]:
    no_value: dict[str, set[str]] = {
        "uv": {
            "--no-sync",
            "--locked",
            "--frozen",
            "--isolated",
            "--active",
            "--no-project",
            "--exact",
            "--inexact",
            "--no-editable",
            "--compile-bytecode",
            "--no-compile-bytecode",
            "--no-sources",
        },
        "poetry": {"-n", "--no-interaction", "--no-ansi", "-v", "-vv", "-vvv"},
    }
    with_value: dict[str, set[str]] = {
        "uv": {
            "--python",
            "--with",
            "--with-editable",
            "--with-requirements",
            "--project",
            "--directory",
            "--env-file",
        },
        "poetry": {"-C", "--directory", "-P", "--project"},
    }
    remaining = list(tokens)
    command_no_value = no_value.get(command, set())
    command_with_value = with_value.get(command, set())
    while remaining:
        option = remaining[0]
        if option == "--":
            remaining.pop(0)
            break
        if option in command_no_value:
            remaining.pop(0)
            continue
        if option in command_with_value:
            if len(remaining) < 2:
                return [], True
            del remaining[:2]
            continue
        if any(
            option.startswith(f"{known}=")
            or (
                known.startswith("-")
                and not known.startswith("--")
                and option.startswith(known)
                and len(option) > len(known)
            )
            for known in command_with_value
        ):
            remaining.pop(0)
            continue
        if option.startswith("-"):
            return remaining, True
        break
    return remaining, False


def _ambiguous_tail_contains_verifier(tokens: list[str], *, depth: int) -> bool:
    return any(_segment_is_verifier(tokens[index:], depth=depth + 1) for index in range(1, len(tokens)))


def _runner_options_ambiguous(command: str, tokens: list[str]) -> bool:
    tail, global_ambiguous = _strip_runner_global_options(command, tokens)
    if global_ambiguous:
        return True
    if command in {"uv", "poetry"} and tail[:1] == ["run"]:
        return _strip_runner_run_options(command, tail[1:])[1]
    return False


def _ambiguous_runner_contains_verifier(command: str, tokens: list[str], *, depth: int) -> bool:
    names = [Path(token).name for token in tokens]
    for index, name in enumerate(names):
        if name != "run":
            continue
        tail = tokens[index + 1 :]
        if tail[:1] == ["--"]:
            tail = tail[1:]
        if command in {"uv", "poetry"}:
            if _segment_is_verifier(tail, depth=depth + 1):
                return True
        else:
            script = _package_runner_script(tokens[index:])
            if script and (script in {"test", "check"} or script.startswith(("test:", "check:"))):
                return True
    return False


def _shell_tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=os.name != "nt", punctuation_chars=";&|<>\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _read_parenthesized(command: str, start: int) -> tuple[str, int] | None:
    depth = 1
    quote: str | None = None
    escaped = False
    index = start
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote is not None:
            if char == quote:
                quote = None
            elif quote == '"' and char == "$" and command[index + 1 : index + 2] == "(":
                nested = _read_parenthesized(command, index + 2)
                if nested is None:
                    return None
                _, end = nested
                index = end + 1
                continue
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return command[start:index], index
        index += 1
    return None


def _nested_shell_command_spans(command: str) -> list[tuple[int, str]]:
    spans: list[tuple[int, str]] = []
    quote: str | None = None
    escaped = False
    index = 0
    while index < len(command):
        char = command[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote != "'":
            escaped = True
            index += 1
            continue
        if quote == "'":
            if char == "'":
                quote = None
            index += 1
            continue
        if char == "'" and quote is None:
            quote = "'"
            index += 1
            continue
        if char == '"':
            quote = None if quote == '"' else '"'
            index += 1
            continue
        if char == "$" and command[index + 1 : index + 2] == "(":
            parsed = _read_parenthesized(command, index + 2)
            if parsed is None:
                return spans
            text, end = parsed
            spans.append((index + 2, text))
            index = end + 1
            continue
        if char == "`":
            end = index + 1
            while end < len(command):
                if command[end] == "`" and command[end - 1] != "\\":
                    spans.append((index + 1, command[index + 1 : end]))
                    index = end + 1
                    break
                end += 1
            else:
                return spans
            continue
        if char == "(" and quote is None:
            parsed = _read_parenthesized(command, index + 1)
            if parsed is None:
                return spans
            text, end = parsed
            spans.append((index, text))
            index = end + 1
            continue
        index += 1
    return spans


def _nested_shell_commands(command: str) -> list[str]:
    return [text for _, text in _nested_shell_command_spans(command)]


def _python_module_invocation(tokens: list[str]) -> tuple[str, list[str]] | None:
    if not tokens or not Path(tokens[0]).name.startswith("python"):
        return None
    index = 1
    simple_flags = set("bBdEiIOPqRsSuvx")
    while index < len(tokens):
        option = tokens[index]
        if option == "-m":
            if index + 1 >= len(tokens):
                return None
            return tokens[index + 1], tokens[index + 2 :]
        if option in {"-W", "-X", "--check-hash-based-pycs"}:
            if index + 1 >= len(tokens):
                return None
            index += 2
            continue
        if option.startswith(("-W", "-X")) and len(option) > 2:
            index += 1
            continue
        if option.startswith("--check-hash-based-pycs="):
            index += 1
            continue
        if option.startswith("-") and not option.startswith("--") and all(flag in simple_flags for flag in option[1:]):
            index += 1
            continue
        return None
    return None


def _is_brigade_verify(tokens: list[str]) -> bool:
    names = [Path(token).name for token in tokens]
    if names[:3] == ["brigade", "work", "verify"]:
        return True
    invocation = _python_module_invocation(tokens)
    return bool(invocation and invocation[0] == "brigade" and invocation[1][:2] == ["work", "verify"])


def _is_brigade_run_tokens(tokens: list[str]) -> bool:
    names = [Path(token).name for token in tokens]
    if names[:2] == ["brigade", "run"]:
        return True
    invocation = _python_module_invocation(tokens)
    return bool(invocation and invocation[0] == "brigade" and invocation[1][:1] == ["run"])


def _is_brigade_run(command: object) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    try:
        tokens = _shell_tokens(command)
    except ValueError:
        return False
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return any(_is_brigade_run_tokens(_strip_env(segment)) for segment in segments if segment)


def _is_brigade_internal_artifact_relative(relative: Path) -> bool:
    parts = relative.parts
    return bool(parts) and parts[0] == ".brigade"


def _relative_path_under_target(target: Path, raw: object) -> Path | None:
    if not isinstance(raw, str) or not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = target / path
    try:
        return path.resolve().relative_to(target.expanduser().resolve())
    except (OSError, ValueError):
        return None


def _path_targets_brigade_internal_artifact(target: Path, raw: object) -> bool:
    relative = _relative_path_under_target(target, raw)
    return relative is not None and _is_brigade_internal_artifact_relative(relative)


def _bash_confident_write_touches_worktree(target: Path, command: object) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    try:
        tokens = _shell_tokens(command)
    except ValueError:
        return True
    if any(">" in token for token in tokens if token and set(token) <= set(";&|<>")):
        return True
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    touches_worktree = False
    for segment in segments:
        stripped = _strip_env(segment)
        if not stripped:
            continue
        names = [Path(token).name for token in stripped]
        command_name = names[0]
        if command_name in _BASH_WRITE_COMMANDS:
            candidates = [token for token in stripped[1:] if not token.startswith("-")]
            if not candidates:
                return True
            if any(not _path_targets_brigade_internal_artifact(target, token) for token in candidates):
                touches_worktree = True
        elif command_name == "sed" and any(arg == "-i" or arg.startswith("-i") for arg in stripped[1:]):
            candidates = [token for token in stripped[1:] if not token.startswith("-")]
            if not candidates:
                return True
            if any(not _path_targets_brigade_internal_artifact(target, token) for token in candidates):
                touches_worktree = True
        elif command_name == "ruff" and names[1:2] == ["format"] and not {"--check", "--diff"}.intersection(names[2:]):
            candidates = [token for token in stripped[2:] if not token.startswith("-")]
            if not candidates:
                return True
            if any(not _path_targets_brigade_internal_artifact(target, token) for token in candidates):
                touches_worktree = True
        elif command_name == "git" and len(names) > 1 and names[1] in _GIT_WRITE_COMMANDS:
            touches_worktree = True
    return touches_worktree


def _shell_wrapper_payload(tokens: list[str]) -> str | None:
    if not tokens or Path(tokens[0]).name not in _SHELL_WRAPPERS:
        return None
    index = 1
    while index < len(tokens):
        option = tokens[index]
        if option == "--command" or (option.startswith("-") and not option.startswith("--") and "c" in option[1:]):
            return tokens[index + 1] if index + 1 < len(tokens) else None
        if option in {"-o", "+o", "-O", "+O", "--init-file", "--rcfile"}:
            if index + 1 >= len(tokens):
                return None
            index += 2
            continue
        if option.startswith(("-o", "+o", "-O", "+O")) and len(option) > 2:
            index += 1
            continue
        if option == "--" or not option.startswith(("-", "+")):
            return None
        index += 1
    return None


def _segment_is_verifier(segment: list[str], *, depth: int = 0) -> bool:
    tokens = _strip_env(segment)
    while tokens and tokens[0] in _SHELL_CONTROL_PREFIXES:
        tokens = tokens[1:]
    if not tokens or _is_brigade_verify(tokens):
        return False
    names = [Path(token).name for token in tokens]
    command = names[0]
    if command in {"pytest", "py.test", "nose2", "tox", "nox", "vitest", "jest", "mocha", "rspec", "phpunit", "ctest"}:
        return True
    python_invocation = _python_module_invocation(tokens)
    if python_invocation and python_invocation[0] in {"pytest", "unittest", "nose", "nose2"}:
        return True
    if command in {"npm", "pnpm", "yarn", "bun"}:
        tail, ambiguous = _strip_runner_global_options(command, tokens[1:])
        script = _package_runner_script(tail)
        if script and (script in {"test", "check"} or script.startswith(("test:", "check:"))):
            return True
        return ambiguous and _ambiguous_runner_contains_verifier(command, tail, depth=depth)
    if command in {"uv", "poetry"}:
        tail, ambiguous = _strip_runner_global_options(command, tokens[1:])
        if tail[:1] != ["run"]:
            return ambiguous and _ambiguous_runner_contains_verifier(command, tail, depth=depth)
        tail, run_ambiguous = _strip_runner_run_options(command, tail[1:])
        if _segment_is_verifier(tail, depth=depth + 1):
            return True
        return run_ambiguous and _ambiguous_tail_contains_verifier(tail, depth=depth)
    if command == "npx":
        tail, ambiguous = _strip_npx_options(tokens[1:])
        if _segment_is_verifier(tail, depth=depth + 1):
            return True
        return ambiguous and any(_segment_is_verifier(tail[index:], depth=depth + 1) for index in range(1, len(tail)))
    shell_payload = _shell_wrapper_payload(tokens)
    if shell_payload is not None:
        return depth < 4 and _is_raw_verification_text(shell_payload, depth=depth + 1)
    if command == "go" and names[1:2] in (["test"], ["vet"]):
        return True
    if command == "cargo" and names[1:2] in (["test"], ["clippy"]):
        return True
    if command == "make":
        tail, ambiguous = _strip_make_global_options(tokens[1:])
        if ambiguous:
            return _ambiguous_tail_contains_verifier(tokens[1:], depth=depth)
        tail_names = [Path(token).name for token in tail]
        return bool(tail_names and tail_names[0] in {"test", "check", "verify"})
    if command == "ruff":
        tail = names[1:]
        return tail[:1] == ["check"] or (tail[:1] == ["format"] and "--check" in tail[1:])
    if command in {"mypy", "pyright", "eslint", "tsc"}:
        return True
    if command == "pre-commit" and names[1:2] == ["run"]:
        return True
    normalized = tokens[0].replace("\\", "/")
    return normalized in {"scripts/verify", "scripts/verify-focused"} or normalized.endswith(
        ("/scripts/verify", "/scripts/verify-focused")
    )


def _is_raw_verification_text(command: str, *, depth: int) -> bool:
    command = _strip_heredoc_bodies(command)
    try:
        tokens = _shell_tokens(command)
    except ValueError:
        return False
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    if any(_segment_is_verifier(segment, depth=depth) for segment in segments):
        return True
    if depth >= 4:
        return False
    return any(_is_raw_verification_text(nested, depth=depth + 1) for nested in _nested_shell_commands(command))


def is_raw_verification(command: object) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    return _is_raw_verification_text(command, depth=0)


def _has_unsupported_verifier_structure(command: str) -> bool:
    if _nested_shell_commands(command):
        return True
    try:
        tokens = _shell_tokens(command)
    except ValueError:
        return False
    if any(
        token and set(token) <= set(";&|<>") and token != "&&" and any(operator in token for operator in "|<>")
        for token in tokens
    ):
        return True
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    if any(segment and segment[0] in _SHELL_CONTROL_TOKENS for segment in segments):
        return True
    for index, segment in enumerate(segments):
        if not _segment_is_verifier(segment):
            continue
        if index > 0:
            return True
        if any(any(character in token for character in "$`*?[]{}~") for token in segment):
            return True
        if _shell_wrapper_payload(_strip_env(segment)) is not None:
            return True
        stripped = _strip_env(segment)
        if stripped and Path(stripped[0]).name == "npx" and _strip_npx_options(stripped[1:])[1]:
            return True
        if stripped and Path(stripped[0]).name in {"uv", "poetry", "npm", "pnpm", "yarn", "bun"}:
            runner = Path(stripped[0]).name
            if _runner_options_ambiguous(runner, stripped[1:]):
                return True
    return False


def _first_verifier_command(command: str) -> str:
    try:
        tokens = _shell_tokens(command)
    except ValueError:
        return command
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    for segment in segments:
        if _segment_is_verifier(segment):
            runnable = list(segment)
            command_index = 0
            while command_index < len(runnable) and _is_env_assignment(runnable[command_index]):
                command_index += 1
            if command_index < len(runnable) and Path(runnable[command_index]).name == "command":
                stripped = _strip_env(runnable[command_index:])
                runnable = [*runnable[:command_index], *stripped]
            if runnable:
                return shlex.join(runnable)
    return command


def _is_routed_verify(command: object) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    try:
        return _is_brigade_verify(_strip_env(_shell_tokens(command)))
    except ValueError:
        return False


def _is_confident_bash_write(command: object) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    try:
        tokens = _shell_tokens(command)
    except ValueError:
        return False
    if any(">" in token for token in tokens if token and set(token) <= set(";&|<>")):
        return True
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    for segment in segments:
        stripped = _strip_env(segment)
        if not stripped:
            continue
        names = [Path(token).name for token in stripped]
        command_name = names[0]
        if command_name in _BASH_WRITE_COMMANDS:
            return True
        if command_name == "sed" and any(arg == "-i" or arg.startswith("-i") for arg in stripped[1:]):
            return True
        if command_name == "ruff" and names[1:2] == ["format"] and not {"--check", "--diff"}.intersection(names[2:]):
            return True
        if command_name == "git" and len(names) > 1 and names[1] in _GIT_WRITE_COMMANDS:
            return True
    return False


def _bash_write_targets_handoffs(target: Path, command: object) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    try:
        tokens = _shell_tokens(command)
    except ValueError:
        return False
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    found_target = False
    output_only_commands = {"cat", "echo", "printf"}
    for segment in segments:
        stripped = _strip_env(segment)
        if not stripped:
            continue
        if Path(stripped[0]).name not in output_only_commands:
            return False
        targets: list[str] = []
        for index, token in enumerate(stripped[:-1]):
            if token and set(token) <= set(";&|<>") and ">" in token:
                targets.append(stripped[index + 1])
        if not targets:
            return False
        found_target = True
        if any(not _is_handoff_path(target, path) for path in targets):
            return False
    return found_target


def _snapshot_ignore_relative(path: Path) -> bool:
    parts = path.parts
    if not parts:
        return True
    if parts[0] in _SNAPSHOT_IGNORE_DIRS:
        return True
    return any(part in _SNAPSHOT_IGNORE_DIRS for part in parts)


def _porcelain_path(line: str) -> str:
    path = line[3:]
    if len(path) >= 2 and path[0] == '"' and path[-1] == '"':
        return bytes(path[1:-1], "utf-8").decode("unicode_escape")
    return path


def _snapshot_git_args(target: Path, *git_args: str) -> list[str]:
    return ["git", "-C", str(target), *git_args]


def _snapshot_pathspec_excludes() -> list[str]:
    excludes: list[str] = []
    for name in sorted(_SNAPSHOT_IGNORE_DIRS):
        excludes.append(f":(exclude){name}")
        excludes.append(f":(exclude){name}/**")
    return excludes


def _run_snapshot_git(target: Path, *git_args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            _snapshot_git_args(target, *git_args),
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=_SNAPSHOT_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _git_worktree_lines(target: Path) -> list[str] | None:
    result = _run_snapshot_git(target, "status", "--porcelain", "--untracked-files=all", "--no-renames")
    if result is None or result.returncode != 0:
        return None
    lines = [line.rstrip() for line in result.stdout.splitlines() if line.strip()]
    filtered = [line for line in lines if not _snapshot_ignore_relative(Path(_porcelain_path(line)))]
    return filtered


def _git_diff_head(target: Path) -> str | None:
    head = _run_snapshot_git(target, "rev-parse", "--verify", "HEAD")
    if head is None or head.returncode != 0:
        return ""
    result = _run_snapshot_git(
        target,
        "diff",
        "HEAD",
        "--no-renames",
        "--",
        ".",
        *_snapshot_pathspec_excludes(),
    )
    if result is None or result.returncode != 0:
        return None
    return result.stdout


def _confirmed_git_worktree(target: Path) -> bool | None:
    result = _run_snapshot_git(target, "rev-parse", "--is-inside-work-tree")
    if result is None:
        return None
    if result.returncode != 0:
        return False
    return result.stdout.strip() == "true"


def _git_untracked_content_signature(target: Path, relative: Path) -> str | None:
    result = _run_snapshot_git(target, "hash-object", "--", relative.as_posix())
    if result is None or result.returncode != 0:
        return None
    digest = result.stdout.strip()
    return digest or None


def _git_worktree_fingerprint_lines(target: Path) -> list[str] | None:
    status_lines = _git_worktree_lines(target)
    if status_lines is None:
        return None
    lines = [f"status\t{line}" for line in sorted(status_lines)]
    diff = _git_diff_head(target)
    if diff is None:
        return None
    lines.append(f"diff\t{localio.stable_hash(diff)}")
    for line in status_lines:
        if not line.startswith("??"):
            continue
        relative = Path(_porcelain_path(line))
        if _snapshot_ignore_relative(relative):
            continue
        signature = _git_untracked_content_signature(target, relative)
        if signature is None:
            return None
        lines.append(f"untracked\t{relative.as_posix()}\t{signature}")
    return lines


def _directory_worktree_lines(target: Path) -> list[str] | None:
    entries: list[str] = []
    try:
        for path in target.rglob("*"):
            if not path.is_file():
                continue
            try:
                relative = path.relative_to(target)
            except ValueError:
                continue
            if _snapshot_ignore_relative(relative):
                continue
            stat = path.stat()
            entries.append(f"{relative.as_posix()}\t{stat.st_mtime_ns}\t{stat.st_size}")
    except OSError:
        return None
    return entries


def repo_worktree_fingerprint(target: Path) -> str | None:
    git_worktree = _confirmed_git_worktree(target)
    if git_worktree is True:
        lines = _git_worktree_fingerprint_lines(target)
    elif git_worktree is False:
        lines = _directory_worktree_lines(target)
    else:
        return None
    if lines is None:
        return None
    return localio.stable_hash(sorted(lines))


def _session_id_for_fingerprint(target: Path, session_fingerprint: str) -> str | None:
    for state in iter_session_states(target, limit=MAX_RECENT_SESSION_STATES):
        if state.get("session_fingerprint") != session_fingerprint:
            continue
        session_id = state.get("session_id")
        if isinstance(session_id, str) and session_id:
            return session_id
    return None


def _receipt_target_matches(receipt: dict[str, Any], target: Path) -> bool:
    receipt_target = receipt.get("target")
    if not isinstance(receipt_target, str):
        return False
    try:
        return Path(receipt_target).expanduser().resolve() == target
    except OSError:
        return False


def _iter_verify_receipts(target: Path) -> Iterator[dict[str, Any]]:
    root = target / ".brigade" / "work" / "verify-runs"
    if not root.is_dir():
        return
    for path in root.glob("*/receipt.json"):
        receipt = localio.read_json_dict(path)
        if isinstance(receipt, dict):
            yield receipt


def _foreign_receipt_since(target: Path, session_fingerprint: str, since: datetime) -> bool:
    """True when another harness or session completed a verify at or after ``since``."""
    for receipt in _iter_verify_receipts(target):
        if receipt.get("status") != "completed":
            continue
        completed = localio.parse_iso_datetime(receipt.get("completed_at"))
        started = localio.parse_iso_datetime(receipt.get("started_at"))
        stamped = completed or started
        if stamped is None or stamped < since:
            continue
        harness_session = receipt.get("harness_session")
        if isinstance(harness_session, dict) and harness_session.get("fingerprint") == session_fingerprint:
            continue
        return True
    return False


def _foreign_write_since(
    target: Path,
    session_id: str,
    since: datetime,
    *,
    session_fingerprint: str | None = None,
) -> bool:
    """True when another actor wrote in this target at or after ``since``.

    Shared workspaces mix Claude sessions and other harnesses. A worktree
    delta in that window is not safely this session's write work (#959).
    Only write evidence counts: ``write_observed`` plus ``last_write_at``,
    an in-flight ``pending_write_at``, or another harness/session receipt.
    """
    for other_state in iter_session_states(target, limit=MAX_RECENT_SESSION_STATES):
        if other_state.get("session_id") == session_id:
            continue
        other_write = localio.parse_iso_datetime(other_state.get("last_write_at"))
        if other_write is not None and other_write >= since and other_state.get("write_observed") is True:
            return True
        pending_write = localio.parse_iso_datetime(other_state.get("pending_write_at"))
        if pending_write is not None and pending_write >= since:
            return True
    fingerprint = session_fingerprint if isinstance(session_fingerprint, str) else _session_fingerprint(session_id)
    return _foreign_receipt_since(target, fingerprint, since)


def _session_has_completed_receipt(
    target: Path,
    *,
    session_fingerprint: str,
    since: object,
) -> bool:
    started = localio.parse_iso_datetime(since)
    if started is None:
        return False
    target = target.expanduser().resolve()
    for receipt in _iter_verify_receipts(target):
        if receipt.get("status") != "completed":
            continue
        harness_session = receipt.get("harness_session")
        if not isinstance(harness_session, dict):
            continue
        if harness_session.get("harness") != "claude" or harness_session.get("fingerprint") != session_fingerprint:
            continue
        if not _receipt_target_matches(receipt, target):
            continue
        receipt_started = localio.parse_iso_datetime(receipt.get("started_at"))
        completed = localio.parse_iso_datetime(receipt.get("completed_at"))
        if receipt_started is None or completed is None or receipt_started < started or completed < receipt_started:
            continue
        return True
    return False


def _session_has_unverified_attributed_write(
    target: Path,
    state: dict[str, Any],
    *,
    session_fingerprint: str,
) -> bool:
    """True when this session's own Write/Edit/Bash PostToolUse is still unverified.

    A Write or Edit PostToolUse is direct attribution, not a shared-tree
    inference. Those writes must still hard-block even when neighbors are
    alive (#959 review).
    """
    attributed = state.get("last_attributed_write_at")
    if localio.parse_iso_datetime(attributed) is None:
        return False
    return not _receipt_since(target, attributed, session_fingerprint=session_fingerprint)


def _shared_tree_block_is_foreign(
    target: Path,
    session_id: str,
    state: dict[str, Any],
) -> bool:
    """True when a Stop block would be controlled by another session's writes.

    The session already captured after it started, and another actor wrote at
    or after this session's receipt threshold. A hard block then cannot be
    cleared from inside the session (#959). This session's own directly
    attributed post-receipt writes still own the block.
    """
    fingerprint = state.get("session_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        fingerprint = _session_fingerprint(session_id)
    if not _session_has_completed_receipt(target, session_fingerprint=fingerprint, since=state.get("started_at")):
        return False
    if _session_has_unverified_attributed_write(target, state, session_fingerprint=fingerprint):
        return False
    threshold = localio.parse_iso_datetime(
        state.get("last_verification_write_at") or state.get("last_write_at") or state.get("started_at")
    )
    if threshold is None:
        return False
    return _foreign_write_since(target, session_id, threshold, session_fingerprint=fingerprint)


def _bash_write_detected(
    target: Path,
    baseline: object,
    *,
    session_id: str,
    started_at: object,
) -> bool:
    if not isinstance(baseline, str) or not baseline:
        return False
    if baseline == _UNAVAILABLE_FINGERPRINT:
        return True
    current = repo_worktree_fingerprint(target)
    if current is None:
        return True
    if current == baseline:
        return False
    started = localio.parse_iso_datetime(started_at)
    if started is None:
        return True
    # Attribute by concurrent write window, not exact fingerprint equality.
    # Another live session may have written again (or a third write may have
    # interleaved) after recording an older repo_fingerprint; requiring an
    # exact match re-arms this session's closeout gate (#704 / #380 follow-up).
    # Cross-harness writers never appear in Claude session state; treat their
    # receipts and another session's write evidence (write_observed /
    # last_write_at / pending_write_at) in the same window as foreign so a
    # shared hub cannot pin this session's last_write_at (#959). A read-only
    # neighbor that only touches its session file is not write evidence.
    if _foreign_write_since(target, session_id, started):
        return False
    return True


def _session_fingerprint(session_id: str) -> str:
    return localio.stable_hash({"claude_session_id": session_id})


def _skill_id_from_skill_path(target: Path, raw_path: object) -> str | None:
    """Return an installed target skill id when ``raw_path`` names its ``SKILL.md``."""
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = target / path
    parts = path.parts
    for index, part in enumerate(parts):
        if part == "skills" and index + 2 < len(parts) and parts[index + 2] == "SKILL.md":
            skill_id = parts[index + 1]
            if skill_id and skill_id not in {".", ".."}:
                from .. import outcome_cmd

                if outcome_cmd._artifact_known(target, skill_id, "skill"):
                    return skill_id
    return None


def _parse_capture_flag(command: object) -> str | None:
    """Extract ``--capture <id>`` from a verify command string when present."""
    if not isinstance(command, str) or not command.strip():
        return None
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return None
    for index, token in enumerate(tokens):
        if token == "--capture" and index + 1 < len(tokens):
            value = tokens[index + 1]
            if value and not value.startswith("-"):
                return value
        if token.startswith("--capture="):
            value = token.split("=", 1)[1]
            if value:
                return value
    return None


def _record_exercised_artifact(state: dict[str, Any], artifact_id: str | None, *, kind: str = "skill") -> bool:
    """Persist the most specific non-generic exercised artifact on session state."""
    from .. import outcome_cmd

    if not isinstance(artifact_id, str):
        return False
    trimmed = artifact_id.strip()
    if not trimmed:
        return False
    current = state.get("exercised_artifact_id")
    if (
        trimmed == outcome_cmd.DEFAULT_CAPTURE_ARTIFACT_ID
        and isinstance(current, str)
        and current.strip()
        and current.strip() != outcome_cmd.DEFAULT_CAPTURE_ARTIFACT_ID
    ):
        return False
    if current == trimmed and state.get("exercised_artifact_kind") == kind:
        return False
    state["exercised_artifact_id"] = trimmed
    state["exercised_artifact_kind"] = kind
    return True


def exercised_artifact_for_fingerprint(target: Path, session_fingerprint: str) -> str | None:
    """Look up an exercised artifact id for a Claude session fingerprint."""
    for state in iter_session_states(target, limit=MAX_RECENT_SESSION_STATES):
        if state.get("session_fingerprint") != session_fingerprint:
            continue
        artifact_id = state.get("exercised_artifact_id")
        if isinstance(artifact_id, str) and artifact_id.strip():
            return artifact_id.strip()
    return None


def _verify_replacement(
    target: Path,
    command: str,
    session_fingerprint: str,
    *,
    capture_artifact_id: str | None = None,
) -> str:
    from .. import outcome_cmd

    artifact_id = outcome_cmd.resolve_capture_artifact_id(
        capture_artifact_id,
        exercised_artifact_for_fingerprint(target, session_fingerprint),
    )
    return (
        f"{CLAUDE_SESSION_ENV}={shlex.quote(session_fingerprint)} "
        f"brigade work verify run --target {shlex.quote(str(target))} "
        f"--command {shlex.quote(command)} --capture {shlex.quote(artifact_id)}"
    )


def _new_state(target: Path, session_id: str, *, task_epoch: str | None = None) -> dict[str, Any]:
    state: dict[str, Any] = {
        "session_id": session_id,
        "session_fingerprint": _session_fingerprint(session_id),
        "target": str(target),
        "started_at": localio.utc_now_iso(),
        "briefed": False,
        "write_observed": False,
        "verify_denied_count": 0,
        "hook_timeout_count": 0,
        "hook_latched": False,
        "hook_latch_announced": False,
        "repo_fingerprint": repo_worktree_fingerprint(target),
    }
    if task_epoch is not None:
        state["task_epoch"] = task_epoch
    return state


def _is_handoff_path(target: Path, raw_path: object) -> bool:
    if not isinstance(raw_path, str) or not raw_path:
        return False
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = target / path
    try:
        path = path.resolve()
        inbox = (target / ".claude" / "memory-handoffs").resolve()
        return path.is_relative_to(inbox)
    except OSError:
        return False


def _is_handoff_tool_write(target: Path, tool_input: dict[str, Any]) -> bool:
    return _is_handoff_path(target, tool_input.get("file_path") or tool_input.get("notebook_path"))


def _normalize_state(
    target: Path,
    session_id: str,
    payload: dict[str, Any] | None,
    *,
    task_epoch: str | None = None,
) -> dict[str, Any]:
    normalized = _new_state(target, session_id, task_epoch=task_epoch)
    if not isinstance(payload, dict):
        return normalized
    if task_epoch is not None and payload.get("task_epoch") != task_epoch:
        # State written by an earlier dispatched task under the same reused
        # session id. ``write_observed`` must be re-earned by this task's own
        # writes and another task's repo graph must not re-arm this task's
        # stop gate (#992). Only the brief marker survives: the session id
        # already received its once-per-repo brief injection.
        normalized["briefed"] = payload.get("briefed") is True
        return normalized
    now = localio.utc_now()
    started = localio.parse_iso_datetime(payload.get("started_at"))
    if started is not None and started <= now:
        normalized["started_at"] = started.isoformat()
    normalized["briefed"] = payload.get("briefed") is True
    normalized["write_observed"] = payload.get("write_observed") is True
    repo_fp = payload.get("repo_fingerprint")
    if isinstance(repo_fp, str) and repo_fp:
        normalized["repo_fingerprint"] = repo_fp
    pending_fp = payload.get("pending_bash_fingerprint")
    if isinstance(pending_fp, str) and pending_fp:
        normalized["pending_bash_fingerprint"] = pending_fp
    pending_started = localio.parse_iso_datetime(payload.get("pending_bash_started_at"))
    if pending_started is not None and pending_started <= now:
        normalized["pending_bash_started_at"] = pending_started.isoformat()
    pending_write = localio.parse_iso_datetime(payload.get("pending_write_at"))
    if pending_write is not None and pending_write <= now:
        normalized["pending_write_at"] = pending_write.isoformat()
    denied = payload.get("verify_denied_count")
    if isinstance(denied, int) and not isinstance(denied, bool) and denied >= 0:
        normalized["verify_denied_count"] = denied
    timeout_count = payload.get("hook_timeout_count")
    if isinstance(timeout_count, int) and not isinstance(timeout_count, bool) and timeout_count >= 0:
        normalized["hook_timeout_count"] = timeout_count
    normalized["hook_latched"] = payload.get("hook_latched") is True
    normalized["hook_latch_announced"] = payload.get("hook_latch_announced") is True
    last_write = localio.parse_iso_datetime(payload.get("last_write_at"))
    if last_write is not None and last_write <= now:
        normalized["last_write_at"] = last_write.isoformat()
    last_verification_write = localio.parse_iso_datetime(payload.get("last_verification_write_at"))
    if last_verification_write is not None and last_verification_write <= now:
        normalized["last_verification_write_at"] = last_verification_write.isoformat()
    last_attributed_write = localio.parse_iso_datetime(payload.get("last_attributed_write_at"))
    if last_attributed_write is not None and last_attributed_write <= now:
        normalized["last_attributed_write_at"] = last_attributed_write.isoformat()
    session_repos = payload.get("session_repos")
    if isinstance(session_repos, list) and all(isinstance(item, str) for item in session_repos):
        normalized["session_repos"] = list(session_repos)
    exercised = payload.get("exercised_artifact_id")
    if isinstance(exercised, str) and exercised.strip():
        normalized["exercised_artifact_id"] = exercised.strip()
        kind = payload.get("exercised_artifact_kind")
        normalized["exercised_artifact_kind"] = kind if isinstance(kind, str) and kind.strip() else "skill"
    if task_epoch is None:
        persisted_epoch = payload.get("task_epoch")
        if isinstance(persisted_epoch, str) and persisted_epoch:
            normalized["task_epoch"] = persisted_epoch
    return normalized


def _receipt_since(target: Path, started_at: object, *, session_fingerprint: str | None = None) -> bool:
    """Return whether a completed verify receipt covers the current write boundary.

    A timestamp by itself is not evidence that the verification exercised this
    session's source tree.  Bind the receipt to the routed Claude session, the
    target, and (when Git can provide it) the exact tree that is being audited.
    """
    started = localio.parse_iso_datetime(started_at.isoformat() if isinstance(started_at, datetime) else started_at)
    if started is None:
        return False
    target = target.expanduser().resolve()
    # Snapshotting is an optional strengthening signal.  ``None`` preserves
    # hook operation in non-Git workspaces and on local Git/tool errors.
    audited_tree = _tree_fingerprint(target)
    session_id = (
        _session_id_for_fingerprint(target, session_fingerprint) if isinstance(session_fingerprint, str) else None
    )
    drifted: list[datetime] = []
    for receipt in _iter_verify_receipts(target):
        if receipt.get("status") != "completed":
            continue
        if session_fingerprint is not None:
            harness_session = receipt.get("harness_session")
            if not isinstance(harness_session, dict):
                continue
            if harness_session.get("harness") != "claude" or harness_session.get("fingerprint") != session_fingerprint:
                continue
        if not _receipt_target_matches(receipt, target):
            continue
        receipt_started = localio.parse_iso_datetime(receipt.get("started_at"))
        completed = localio.parse_iso_datetime(receipt.get("completed_at"))
        if receipt_started is None or completed is None or receipt_started < started or completed < receipt_started:
            continue
        if audited_tree is None or receipt.get("tree_fingerprint") == audited_tree:
            return True
        drifted.append(completed)
    # A time-valid session receipt that no longer matches the live tree still
    # covers this session when another actor mutated the shared worktree after
    # capture. Requiring an exact live-tree match is unsatisfiable in a hub
    # with concurrent writers (#959).
    if drifted and session_id:
        earliest = min(drifted)
        if _foreign_write_since(target, session_id, earliest, session_fingerprint=session_fingerprint):
            return True
    return False


def _handoff_since(target: Path, started_at: object) -> bool:
    started = localio.parse_iso_datetime(started_at)
    inbox = target / ".claude" / "memory-handoffs"
    if started is None or not inbox.is_dir():
        return False
    for path in inbox.glob("*.md"):
        if path.name == "TEMPLATE.md":
            continue
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if modified >= started.timestamp():
            return True
    return False


def _additional_context(
    event: str,
    text: str,
    *,
    target: Path,
    session_id: str,
    records: list[str] | None = None,
) -> dict[str, Any]:
    max_chars, max_items = envelope.resolve_caps()
    payload_records = records if records is not None else [text]
    return envelope.additional_context_envelope(
        event,
        payload_records,
        target=target,
        session_id=session_id,
        max_chars=max_chars,
        max_items=max_items,
    )


def _unwired_init_hint(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Suggest wiring an unwired git repo at session start.

    The user-scope hooks promise one-and-done onboarding: an agent entering an
    unwired repo must be told the exact init command instead of silence. Only
    SessionStart hints, only for a real git checkout, and never for the user's
    home directory itself.
    """
    raw_cwd = payload.get("cwd")
    if not isinstance(raw_cwd, str) or not raw_cwd:
        return None
    try:
        cwd = Path(raw_cwd).expanduser().resolve(strict=False)
    except OSError:
        return None
    if cwd == Path.home() or not (cwd / ".git").exists():
        return None
    command = (
        f"brigade init --target {shlex.quote(str(cwd))} --depth repo --harnesses claude --owner claude --git-exclude"
    )
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        session_id = "unwired"
    return _additional_context(
        "SessionStart",
        "",
        target=cwd,
        session_id=session_id,
        records=[
            "This git repository is not Brigade-wired. Configure it before real work so "
            f"verification and memory are captured: {command}",
            "Then start with: brigade work brief --target .",
        ],
    )


def _restore_brief_records(brief_text: str) -> list[str]:
    return [compaction_marker.RESTORE_PREFIX, *_brief_records(brief_text)]


def _attach_claim(result: dict[str, Any], claim: compaction_marker.CompactionClaim) -> dict[str, Any]:
    attached = dict(result)
    attached[compaction_marker.CLAIM_KEY] = str(claim.path)
    return attached


def _strip_claim(result: dict[str, Any] | None) -> tuple[dict[str, Any] | None, Path | None]:
    if not isinstance(result, dict) or compaction_marker.CLAIM_KEY not in result:
        return result, None
    cleaned = dict(result)
    raw = cleaned.pop(compaction_marker.CLAIM_KEY)
    if not isinstance(raw, str) or not raw:
        return cleaned, None
    return cleaned, Path(raw)


def _handle_pre_compact(payload: dict[str, Any], *, pin: Path | None = None) -> dict[str, Any] | None:
    """Record a pending restore marker; PreCompact cannot inject context."""
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    target = effective_hook_target(payload, pin=pin)
    if target is None:
        return None
    trigger_detail = payload.get("trigger")
    detail = trigger_detail if isinstance(trigger_detail, str) and trigger_detail.strip() else None
    try:
        compaction_marker.write_pending(
            session_id,
            target,
            trigger="PreCompact",
            trigger_detail=detail,
        )
    except OSError as exc:
        raise HookDegraded(f"compaction marker write failed: {exc}") from exc
    return None


def _handle_user_prompt_submit(payload: dict[str, Any], *, pin: Path | None = None) -> dict[str, Any] | None:
    """Restore the brief once after compaction; absent markers stay near-free."""
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None
    workspace = compaction_marker.cheap_workspace_root(payload.get("cwd"))
    if workspace is None:
        return None
    key = compaction_marker.marker_key(session_id, workspace)
    if not compaction_marker.marker_present(key):
        return None
    # Confirm the workspace is still a Claude-wired Brigade repo before claiming.
    target = effective_hook_target(payload, pin=pin)
    if target is None:
        return None
    try:
        claim = compaction_marker.try_claim(session_id, target)
    except OSError as exc:
        raise HookDegraded(f"compaction marker claim failed: {exc}") from exc
    if claim is None:
        return None
    try:
        brief_text = _run_brief(target)
        result = _additional_context(
            "UserPromptSubmit",
            brief_text,
            target=target,
            session_id=session_id,
            records=_restore_brief_records(brief_text),
        )
        return _attach_claim(result, claim)
    except Exception:
        compaction_marker.release_claim(claim)
        raise


def handle_payload(event: str, payload: dict[str, Any], *, pin: Path | None = None) -> dict[str, Any] | None:
    pin = pin if pin is not None else _hook_pin_from_env()
    if event == "PreCompact":
        return _handle_pre_compact(payload, pin=pin)
    if event == "UserPromptSubmit":
        return _handle_user_prompt_submit(payload, pin=pin)

    target = effective_hook_target(payload, pin=pin)
    if target is None:
        if event == "SessionStart" and pin is None:
            return _unwired_init_hint(payload)
        return None
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None

    task_epoch = _task_epoch_for_event(event, payload, session_id)
    persisted_state = read_session_state(target, session_id)
    state = _normalize_state(target, session_id, persisted_state, task_epoch=task_epoch)
    if persisted_state != state:
        _write_session_state_preserving_latch(target, session_id, state, log_target=target)
    if event != "Stop":
        _touch_session_targets(session_id, target, payload, task_epoch=task_epoch)
    if event == "SessionStart":
        # True starts and Claude's inject-capable compact source both clear any
        # leftover marker so UserPromptSubmit does not double-inject.
        try:
            compaction_marker.clear_markers(session_id, target)
        except OSError:
            pass
        source = payload.get("source")
        if source == "compact":
            # Compaction restart: restore the work brief (#736) and keep #834
            # bounded recall on the same SessionStart injection.
            brief_text = _run_brief(target)
            recall_text = _run_recall(target, payload)
            state["briefed"] = True
            _write_session_state_preserving_latch(target, session_id, state, log_target=target)
            records = _restore_brief_records(brief_text)
            if recall_text.strip():
                records.extend(_brief_records(recall_text))
            combined = brief_text if not recall_text.strip() else f"{brief_text}\n{recall_text}"
            return _additional_context(
                "SessionStart",
                combined,
                target=target,
                session_id=session_id,
                records=records,
            )
        if state.get("briefed"):
            return None
        brief_text = _run_brief(target)
        recall_text = _run_recall(target, payload)
        state["briefed"] = True
        _write_session_state_preserving_latch(target, session_id, state, log_target=target)
        records = _brief_records(brief_text)
        if recall_text.strip():
            records.extend(_brief_records(recall_text))
        return _additional_context(
            "SessionStart",
            brief_text if not recall_text.strip() else f"{brief_text}\n{recall_text}",
            target=target,
            session_id=session_id,
            records=records,
        )

    if event == "PreToolUse":
        tool_name = payload.get("tool_name")
        if tool_name in _WRITE_TOOLS:
            # Heartbeat so a concurrent session can attribute the coming write
            # to this session instead of a shared worktree delta (#959).
            state["pending_write_at"] = localio.utc_now_iso()
            _write_session_state_preserving_latch(target, session_id, state, log_target=target)
            return None
        if tool_name != "Bash":
            return None
        raw_tool_input = payload.get("tool_input")
        tool_input: dict[str, Any] = raw_tool_input if isinstance(raw_tool_input, dict) else {}
        command = tool_input.get("command")
        baseline = repo_worktree_fingerprint(target)
        state["pending_bash_fingerprint"] = baseline or _UNAVAILABLE_FINGERPRINT
        state["pending_bash_started_at"] = localio.utc_now_iso()
        _write_session_state_preserving_latch(target, session_id, state, log_target=target)
        if not is_raw_verification(command):
            return None
        state["verify_denied_count"] = int(state.get("verify_denied_count") or 0) + 1
        _write_session_state_preserving_latch(target, session_id, state, log_target=target)
        capture_artifact_id = state.get("exercised_artifact_id")
        if not isinstance(capture_artifact_id, str):
            capture_artifact_id = None
        if _has_unsupported_verifier_structure(str(command)):
            from .. import outcome_cmd

            capture_id = outcome_cmd.resolve_capture_artifact_id(capture_artifact_id)
            reason = (
                "Route verification through Brigade so failed, rejected, and passing results create receipts.\n"
                "Split shell grouping, command substitution, pipelines, redirection, or complex directory changes "
                f"from the verifier, then run that verifier with `brigade work verify run --capture {capture_id}`."
            )
        else:
            replacement = _verify_replacement(
                target,
                _first_verifier_command(str(command)),
                str(state["session_fingerprint"]),
                capture_artifact_id=capture_artifact_id,
            )
            reason = (
                "Route verification through Brigade so failed, rejected, and passing results create receipts.\n"
                f"Use: {replacement}"
            )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }

    if event == "PostToolUse":
        tool_name = payload.get("tool_name")
        raw_post_tool_input = payload.get("tool_input")
        post_tool_input: dict[str, Any] = raw_post_tool_input if isinstance(raw_post_tool_input, dict) else {}
        command = post_tool_input.get("command")
        if tool_name == "Read":
            skill_id = _skill_id_from_skill_path(target, post_tool_input.get("file_path"))
            if _record_exercised_artifact(state, skill_id):
                _write_session_state_preserving_latch(target, session_id, state, log_target=target)
            return None
        if tool_name == "Bash" and (_is_routed_verify(command) or _is_brigade_run(command)):
            if _is_routed_verify(command):
                _record_exercised_artifact(state, _parse_capture_flag(command))
            state.pop("pending_bash_fingerprint", None)
            state.pop("pending_bash_started_at", None)
            _write_session_state_preserving_latch(target, session_id, state, log_target=target)
            return None
        wrote = tool_name in _WRITE_TOOLS or (
            tool_name == "Bash"
            and _is_confident_bash_write(command)
            and _bash_confident_write_touches_worktree(target, command)
        )
        if not wrote and tool_name == "Bash":
            wrote = _bash_write_detected(
                target,
                state.get("pending_bash_fingerprint"),
                session_id=session_id,
                started_at=state.get("pending_bash_started_at"),
            )
        if wrote:
            state["write_observed"] = True
            written_at = localio.utc_now_iso()
            state["last_write_at"] = written_at
            state["last_attributed_write_at"] = written_at
            handoff_write = (tool_name in _WRITE_TOOLS and _is_handoff_tool_write(target, post_tool_input)) or (
                tool_name == "Bash" and _bash_write_targets_handoffs(target, post_tool_input.get("command"))
            )
            if not handoff_write:
                state["last_verification_write_at"] = written_at
            updated_fp = repo_worktree_fingerprint(target)
            if updated_fp is not None:
                state["repo_fingerprint"] = updated_fp
            state.pop("pending_bash_fingerprint", None)
            state.pop("pending_bash_started_at", None)
            state.pop("pending_write_at", None)
            _write_session_state_preserving_latch(target, session_id, state, log_target=target)
        elif state.get("pending_bash_fingerprint") is not None:
            state.pop("pending_bash_fingerprint", None)
            state.pop("pending_bash_started_at", None)
            _write_session_state_preserving_latch(target, session_id, state, log_target=target)
        return None

    if event == "PostToolUseFailure":
        raw_tool_input = payload.get("tool_input")
        tool_input = raw_tool_input if isinstance(raw_tool_input, dict) else {}
        command = tool_input.get("command")
        if payload.get("tool_name") == "Bash" and (is_raw_verification(command) or _is_routed_verify(command)):
            from .. import outcome_cmd

            capture_id = outcome_cmd.resolve_capture_artifact_id(
                _parse_capture_flag(command),
                state.get("exercised_artifact_id") if isinstance(state.get("exercised_artifact_id"), str) else None,
            )
            return _additional_context(
                "PostToolUseFailure",
                "",
                target=target,
                session_id=session_id,
                records=[
                    "The failed or rejected verification must remain recorded in Brigade before retrying. "
                    "Inspect the receipt, fix the cause, then rerun through "
                    f"`brigade work verify run --capture {capture_id}`.",
                ],
            )
        return None

    if event == "Stop":
        if payload.get("stop_hook_active") is True:
            return None
        blocking_failures: list[str] = []
        shared_tree_notes: list[str] = []
        handoff_target: Path | None = None
        context_target: Path | None = None
        for stop_target in _stop_targets(payload, session_id, task_epoch=task_epoch):
            if not stop_target.is_dir():
                continue
            stop_state = read_session_state(stop_target, session_id)
            stop_state = _normalize_state(stop_target, session_id, stop_state, task_epoch=task_epoch)
            if not stop_state.get("write_observed"):
                continue
            fingerprint = stop_state.get("session_fingerprint")
            if not isinstance(fingerprint, str):
                fingerprint = _session_fingerprint(session_id)
            receipt_threshold = (
                stop_state.get("last_verification_write_at")
                or stop_state.get("last_write_at")
                or stop_state.get("started_at")
            )
            if not _receipt_since(stop_target, receipt_threshold, session_fingerprint=fingerprint):
                if _shared_tree_block_is_foreign(stop_target, session_id, stop_state):
                    # Another session owns the live tree. A hard block here is
                    # unsatisfiable: capture, foreign write, re-arm (#959).
                    shared_tree_notes.append(
                        f"{stop_target}: a concurrent session has written since this session's last capture; "
                        "the shared tree is not this session's unverified work."
                    )
                    context_target = stop_target
                    if not _handoff_since(stop_target, stop_state.get("started_at")):
                        handoff_target = stop_target
                    continue
                exercised = stop_state.get("exercised_artifact_id")
                replacement = _verify_replacement(
                    stop_target,
                    "<test>",
                    fingerprint,
                    capture_artifact_id=exercised if isinstance(exercised, str) else None,
                )
                blocking_failures.append(f"{stop_target}: run `{replacement}`")
            elif not _handoff_since(stop_target, stop_state.get("started_at")):
                handoff_target = stop_target
        if blocking_failures:
            return {
                "decision": "block",
                "reason": (
                    "Recent write work in Brigade-wired repos has no verification receipt for this session.\n- "
                    + "\n- ".join(blocking_failures)
                ),
            }
        if shared_tree_notes:
            records = [
                "Verification is recorded for this session. Closeout is not blocked because a concurrent "
                "session changed the shared worktree.",
                *shared_tree_notes,
            ]
            if handoff_target is not None:
                records.append(
                    "If this work produced durable knowledge, write a Memory Handoff in "
                    "`.claude/memory-handoffs/` before finishing."
                )
            return _additional_context(
                "Stop",
                "",
                target=context_target or handoff_target or target,
                session_id=session_id,
                records=records,
            )
        if handoff_target is not None:
            return _additional_context(
                "Stop",
                "",
                target=handoff_target,
                session_id=session_id,
                records=[
                    "Verification is recorded. If this work produced durable knowledge, write a Memory Handoff in "
                    "`.claude/memory-handoffs/` before finishing.",
                ],
            )
    return None


def _read_limited_binary(stream: BinaryIO, *, limit: int = MAX_HOOK_STDIN_BYTES) -> bytes:
    """Read at most ``limit`` bytes from a binary stream; reject overflow.

    Stops one byte past the limit so oversized input is detected without
    retaining or decoding the rest of the stream.
    """
    if limit < 0:
        raise HookDegraded("hook stdin limit is invalid")
    chunks: list[bytes] = []
    total = 0
    while True:
        want = min(_HOOK_STDIN_READ_CHUNK, limit - total + 1)
        try:
            chunk = stream.read(want)
        except OSError as exc:
            raise HookDegraded(f"hook stdin read failed: {exc}") from exc
        if not chunk:
            break
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise HookDegraded("hook stdin must be read as binary")
        data = bytes(chunk)
        total += len(data)
        if total > limit:
            raise HookDegraded(f"hook stdin exceeds {limit} bytes")
        chunks.append(data)
    return b"".join(chunks)


def _read_hook_stdin_bytes(*, stdin_text: str | None) -> bytes:
    if stdin_text is not None:
        try:
            raw = stdin_text.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise HookDegraded(f"hook stdin is not valid UTF-8: {exc}") from None
        if len(raw) > MAX_HOOK_STDIN_BYTES:
            raise HookDegraded(f"hook stdin exceeds {MAX_HOOK_STDIN_BYTES} bytes")
        return raw
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        return _read_limited_binary(buffer, limit=MAX_HOOK_STDIN_BYTES)
    return _read_limited_text_stdin(sys.stdin, limit=MAX_HOOK_STDIN_BYTES)


def _read_limited_text_stdin(stream: Any, *, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            piece = stream.read(_HOOK_STDIN_READ_CHUNK)
        except OSError as exc:
            raise HookDegraded(f"hook stdin read failed: {exc}") from exc
        if not piece:
            break
        if isinstance(piece, str):
            try:
                encoded = piece.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise HookDegraded(f"hook stdin is not valid UTF-8: {exc}") from None
        elif isinstance(piece, (bytes, bytearray, memoryview)):
            encoded = bytes(piece)
        else:
            raise HookDegraded("hook stdin must be read as binary")
        total += len(encoded)
        if total > limit:
            raise HookDegraded(f"hook stdin exceeds {limit} bytes")
        chunks.append(encoded)
    return b"".join(chunks)


def _enforce_hook_stdin_shape(value: object, *, depth: int = 0) -> None:
    if depth > MAX_HOOK_STDIN_NESTING_DEPTH:
        raise HookDegraded(f"hook stdin nesting exceeds {MAX_HOOK_STDIN_NESTING_DEPTH}")
    if isinstance(value, str):
        if len(value) > MAX_HOOK_STDIN_STRING_CHARS:
            raise HookDegraded(f"hook stdin string exceeds {MAX_HOOK_STDIN_STRING_CHARS} characters")
        return
    if isinstance(value, dict):
        if len(value) > MAX_HOOK_STDIN_COLLECTION_ITEMS:
            raise HookDegraded(f"hook stdin object exceeds {MAX_HOOK_STDIN_COLLECTION_ITEMS} items")
        for key, item in value.items():
            if not isinstance(key, str) or len(key) > MAX_HOOK_STDIN_STRING_CHARS:
                raise HookDegraded("hook stdin object key is invalid")
            _enforce_hook_stdin_shape(item, depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > MAX_HOOK_STDIN_COLLECTION_ITEMS:
            raise HookDegraded(f"hook stdin array exceeds {MAX_HOOK_STDIN_COLLECTION_ITEMS} items")
        for item in value:
            _enforce_hook_stdin_shape(item, depth=depth + 1)


def _parse_hook_stdin(raw: bytes) -> dict[str, Any]:
    """Decode bounded hook stdin, reject trailing input, and cap nested sizes."""
    if len(raw) > MAX_HOOK_STDIN_BYTES:
        raise HookDegraded(f"hook stdin exceeds {MAX_HOOK_STDIN_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HookDegraded(f"hook stdin is not valid UTF-8: {exc}") from None
    stripped = text.strip()
    if not stripped:
        return {}
    decoder = json.JSONDecoder()
    try:
        parsed, end = decoder.raw_decode(stripped)
    except json.JSONDecodeError as exc:
        raise HookDegraded(f"malformed hook stdin: {exc}") from None
    except RecursionError as exc:
        raise HookDegraded("hook stdin nesting exceeds limit") from exc
    if stripped[end:].strip():
        raise HookDegraded("hook stdin has trailing input")
    if not isinstance(parsed, dict):
        raise HookDegraded("hook stdin must be a JSON object")
    _enforce_hook_stdin_shape(parsed)
    return parsed


def _load_hook_stdin(*, stdin_text: str | None = None) -> tuple[dict[str, Any], str]:
    raw = _read_hook_stdin_bytes(stdin_text=stdin_text)
    payload = _parse_hook_stdin(raw)
    return payload, raw.decode("utf-8")


def _hook_timeout_seconds() -> float:
    raw = os.environ.get(_HOOK_TIMEOUT_ENV)
    if raw is not None and raw.strip():
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if value > 0:
            return value
    return float(envelope.HOOK_TIMEOUT_SECONDS)


def _maybe_test_hang() -> None:
    raw = os.environ.get(_HOOK_TEST_HANG_ENV)
    if raw is None or not raw.strip():
        return
    try:
        seconds = float(raw)
    except ValueError:
        return
    if seconds > 0:
        time.sleep(seconds)


def _isolated_handle_payload(event: str, payload: dict[str, Any], *, pin: Path | None = None) -> dict[str, Any] | None:
    _maybe_test_hang()
    return handle_payload(event, payload, pin=pin)


def _raw_pin_target_from_env() -> Path | None:
    """Return the pin path from the worker env without touching the filesystem."""
    raw = os.environ.get(_HOOK_PIN_ENV)
    if raw is None or not raw.strip():
        return None
    return Path(raw)


def _path_or_none(value: object) -> Path | None:
    if isinstance(value, Path):
        return value
    if isinstance(value, str) and value:
        return Path(value)
    return None


def _hook_run_body(
    event: str,
    payload: dict[str, Any],
    *,
    pin_target: Path | None,
    on_prologue: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Run every filesystem-touching ``hook_run`` step.

    The parent process must not call this. Pin resolution, target discovery,
    log-target lookup, latch I/O, ``.git`` checks, and ``handle_payload`` all
    belong inside the timed worker so a stalled filesystem cannot outlive the
    configured hook timeout.
    """
    pin = _resolve_pin(pin_target)
    session_id = _session_id_from_payload(payload)
    log_target = _resolve_log_target(payload, pin=pin)
    latched = False
    announce = False
    if log_target is not None:
        latched, announce = _read_hook_latch(log_target, session_id)
    if on_prologue is not None:
        on_prologue(
            {
                "log_target": str(log_target) if log_target is not None else None,
                "session_id": session_id,
                "latched": latched,
                "announce": announce,
            }
        )
    if latched and log_target is not None:
        if announce and _claim_timeout_announce(log_target, session_id):
            envelope.append_log(log_target, f"{event}: latched after repeated timeouts")
            return {
                "kind": "latched",
                "result": envelope.latched_envelope(event),
                "log_target": str(log_target),
                "claim_path": None,
                "session_id": session_id,
            }
        return {
            "kind": "latched_silent",
            "result": envelope.empty_envelope(event),
            "log_target": str(log_target),
            "claim_path": None,
            "session_id": session_id,
        }

    result = _isolated_handle_payload(event, payload, pin=pin)
    result, claim_path = _strip_claim(result)
    if log_target is not None:
        _clear_hook_timeouts(log_target, session_id)
        if result is None:
            envelope.append_log(log_target, f"{event}: empty envelope")
    return {
        "kind": "ok",
        "result": result,
        "log_target": str(log_target) if log_target is not None else None,
        "claim_path": str(claim_path) if claim_path is not None else None,
        "session_id": session_id,
    }


def _hook_worker_main() -> None:
    """Run the full hook body in an isolated child process (issue #735)."""
    event = os.environ.get(_HOOK_WORKER_EVENT_ENV, "")
    payload, _raw = _load_hook_stdin()

    def on_prologue(info: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps({"phase": "prologue", **info}) + "\n")
        sys.stdout.flush()

    outcome = _hook_run_body(
        event,
        payload,
        pin_target=_raw_pin_target_from_env(),
        on_prologue=on_prologue,
    )
    sys.stdout.write(json.dumps({"phase": "done", **outcome}) + "\n")


def _terminate_process(proc: mp.Process | mp.context.ForkProcess) -> None:
    if not proc.is_alive():
        return
    proc.terminate()
    proc.join(timeout=1)
    if proc.is_alive():
        proc.kill()
        proc.join(timeout=0.2)


def _recv_timed_handle_payload_result(
    queue: mp.Queue,
    proc: mp.Process | mp.context.ForkProcess,
    *,
    timeout: float,
) -> dict[str, Any] | None:
    try:
        status, value = queue.get(timeout=timeout)
    except Empty as exc:
        _terminate_process(proc)
        raise HookDegraded("hook operation timed out") from exc
    proc.join(timeout=1)
    if proc.is_alive():
        _terminate_process(proc)
    if status == "ok":
        return value if isinstance(value, dict) else None
    if isinstance(value, BaseException):
        raise value
    raise RuntimeError(str(value))


def _recv_timed_hook_run_result(
    queue: mp.Queue,
    proc: mp.Process | mp.context.ForkProcess,
    *,
    timeout: float,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    try:
        status, value = queue.get(timeout=timeout)
    except Empty as exc:
        _terminate_process(proc)
        raise HookDegraded("hook operation timed out") from exc

    log_target: Path | None = None
    if status == "prologue":
        if isinstance(value, dict):
            log_target = _path_or_none(value.get("log_target"))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate_process(proc)
            raise HookDegraded("hook operation timed out", log_target=log_target)
        try:
            status, value = queue.get(timeout=remaining)
        except Empty as exc:
            _terminate_process(proc)
            raise HookDegraded("hook operation timed out", log_target=log_target) from exc

    proc.join(timeout=1)
    if proc.is_alive():
        _terminate_process(proc)
    if status == "ok":
        if isinstance(value, dict):
            return value
        raise RuntimeError("timed hook worker returned a non-object result")
    if isinstance(value, BaseException):
        if isinstance(value, HookDegraded) and value.log_target is None:
            value.log_target = log_target
        raise value
    raise RuntimeError(str(value))


def _fork_timed_handle_payload_target(event: str, payload: dict[str, Any], queue: mp.Queue, pin: Path | None) -> None:
    try:
        result = _isolated_handle_payload(event, payload, pin=pin)
        queue.put(("ok", result))
    except Exception as exc:  # noqa: BLE001 - marshal failure back to parent
        queue.put(("err", exc))


def _fork_timed_handle_payload_worker(
    event: str, raw: str, *, timeout: float, pin: Path | None = None
) -> dict[str, Any] | None:
    payload = _parse_hook_stdin(raw.encode("utf-8"))

    ctx = mp.get_context("fork")
    queue: mp.Queue = ctx.Queue()
    proc = ctx.Process(
        target=_fork_timed_handle_payload_target,
        args=(event, payload, queue, pin),
        daemon=True,
    )
    try:
        proc.start()
        return _recv_timed_handle_payload_result(queue, proc, timeout=timeout)
    finally:
        if proc.is_alive():
            _terminate_process(proc)
        queue.close()
        queue.join_thread()


def _fork_timed_hook_run_target(
    event: str,
    payload: dict[str, Any],
    queue: mp.Queue,
    pin_target: Path | None,
) -> None:
    try:

        def on_prologue(info: dict[str, Any]) -> None:
            queue.put(("prologue", info))

        outcome = _hook_run_body(event, payload, pin_target=pin_target, on_prologue=on_prologue)
        queue.put(("ok", outcome))
    except Exception as exc:  # noqa: BLE001 - marshal failure back to parent
        queue.put(("err", exc))


def _fork_timed_hook_run_worker(
    event: str, raw: str, *, timeout: float, pin_target: Path | None = None
) -> dict[str, Any]:
    payload = _parse_hook_stdin(raw.encode("utf-8"))

    ctx = mp.get_context("fork")
    queue: mp.Queue = ctx.Queue()
    proc = ctx.Process(
        target=_fork_timed_hook_run_target,
        args=(event, payload, queue, pin_target),
        daemon=True,
    )
    try:
        proc.start()
        return _recv_timed_hook_run_result(queue, proc, timeout=timeout)
    finally:
        if proc.is_alive():
            _terminate_process(proc)
        queue.close()
        queue.join_thread()


def _log_target_from_worker_stdout(stdout: str | None) -> Path | None:
    if not stdout:
        return None
    for line in stdout.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and parsed.get("phase") == "prologue":
            return _path_or_none(parsed.get("log_target"))
    return None


def _outcome_from_worker_stdout(stdout: str | None) -> dict[str, Any] | None:
    if not stdout or not stdout.strip():
        return None
    done: dict[str, Any] | None = None
    for line in stdout.splitlines():
        text = line.strip()
        if not text or text == "null":
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        if parsed.get("phase") == "done" or parsed.get("kind") in _HOOK_OUTCOME_KINDS:
            done = parsed
            continue
        if parsed.get("phase") == "prologue":
            continue
        done = {
            "kind": "ok",
            "result": parsed,
            "log_target": None,
            "claim_path": None,
            "session_id": "unknown",
        }
    return done


def _subprocess_timed_handle_payload(
    event: str, raw: str, *, timeout: float, pin: Path | None = None
) -> dict[str, Any] | None:
    outcome = _subprocess_timed_hook_run(event, raw, timeout=timeout, pin_target=pin)
    raw_result = outcome.get("result")
    return raw_result if isinstance(raw_result, dict) else None


def _subprocess_timed_hook_run(
    event: str, raw: str, *, timeout: float, pin_target: Path | None = None
) -> dict[str, Any]:
    env = os.environ.copy()
    env[_HOOK_WORKER_EVENT_ENV] = event
    if pin_target is not None:
        env[_HOOK_PIN_ENV] = str(pin_target)
    else:
        env.pop(_HOOK_PIN_ENV, None)
    try:
        completed = subprocess.run(
            [sys.executable, "-c", _HOOK_WORKER_COMMAND],
            input=raw,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        captured = exc.stdout if isinstance(exc.stdout, str) else ""
        raise HookDegraded(
            "hook operation timed out",
            log_target=_log_target_from_worker_stdout(captured),
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or f"exit {completed.returncode}"
        raise RuntimeError(detail)
    outcome = _outcome_from_worker_stdout(completed.stdout)
    if outcome is None:
        return {
            "kind": "ok",
            "result": None,
            "log_target": None,
            "claim_path": None,
            "session_id": "unknown",
        }
    return outcome


def _run_timed_handle_payload(event: str, raw: str, *, pin: Path | None = None) -> dict[str, Any] | None:
    """Run the full hook body under the configured timeout.

    ``pin`` is the unresolved CLI/env target. The worker resolves it. The
    parent must not call ``_resolve_pin`` / ``_resolve_log_target`` first.
    """
    timeout = _hook_timeout_seconds()
    if sys.platform == "win32":
        return _subprocess_timed_hook_run(event, raw, timeout=timeout, pin_target=pin)
    return _fork_timed_hook_run_worker(event, raw, timeout=timeout, pin_target=pin)


def _normalize_timed_outcome(value: object) -> dict[str, Any]:
    if value is None:
        return {
            "kind": "ok",
            "result": None,
            "log_target": None,
            "claim_path": None,
            "session_id": "unknown",
        }
    if isinstance(value, dict) and value.get("kind") in _HOOK_OUTCOME_KINDS:
        return value
    if isinstance(value, dict):
        return {
            "kind": "ok",
            "result": value,
            "log_target": None,
            "claim_path": None,
            "session_id": "unknown",
        }
    raise TypeError(f"timed hook worker returned {type(value).__name__}")


def _resolve_log_target(payload: dict[str, Any] | None, *, pin: Path | None = None) -> Path | None:
    if not isinstance(payload, dict):
        return None
    return effective_hook_target(payload, pin=pin)


def _is_timeout_degraded(exc: BaseException) -> bool:
    return "timed out" in str(exc)


def _run_best_effort_bounded(
    fn: Callable[[], Any],
    *,
    timeout: float,
    default: Any,
) -> Any:
    """Run ``fn`` in a daemon thread; return ``default`` if it exceeds ``timeout``.

    Timeout-path latch/state writes on the target workspace, that path's
    target-tree hook.log, and claim cleanup must not run on the
    ``hook_run`` parent stack. A stalled target filesystem (or Windows,
    where ``fork`` is unavailable and spawn exceeds this budget) degrades
    instead of hanging Claude Code past the hook timeout.

    The #735 doctor-pointer / error diagnostic write is not this helper.
    That local ``hook.log`` is a required contract file and is written
    synchronously by ``_append_degraded_diagnostic``.
    """
    box: list[Any] = [default]
    done = threading.Event()

    def _run() -> None:
        try:
            box[0] = fn()
        except Exception:  # noqa: BLE001 - parent must still emit a valid envelope
            box[0] = default
        finally:
            done.set()

    thread = threading.Thread(target=_run, name="brigade-hook-best-effort", daemon=True)
    thread.start()
    if not done.wait(timeout=max(timeout, 0.0)):
        return default
    return box[0]


def _apply_timeout_followup_inprocess(
    event: str,
    target: Path,
    session_id: str,
    detail: str,
    *,
    publish: Callable[[str], None] | None = None,
) -> str:
    try:
        state = _record_hook_timeout(target, session_id, announce=True)
    except OSError:
        return "degraded"
    if state.get(_ANNOUNCE_CLAIMED_KEY) is True:
        outcome = "latched"
    elif state.get("hook_latched") is True:
        outcome = "latched_silent"
    else:
        outcome = "degraded"
    if publish is not None:
        publish(outcome)
    try:
        envelope.append_log(target, f"{event}: degraded: {detail}")
        if outcome == "latched":
            envelope.append_log(target, f"{event}: latched after repeated timeouts")
    except OSError:
        pass
    return outcome


def _append_degraded_diagnostic(event: str, log_target: Path | None, detail: object) -> None:
    """Write the #735 doctor-pointer ``hook.log`` before ``hook_run`` returns.

    Callers and the envelope tests read
    ``.brigade/work/claude-hooks/hook.log`` immediately after the hook
    exits. Creating the parent directory and appending the line must
    finish on this stack — a best-effort daemon thread can lose the write
    or race the reader. Use this only on the error / doctor-pointer /
    degraded path, where the timed worker already returned (or never
    started). Timeout latch/state on a possibly stalled target stays
    behind ``_run_best_effort_bounded``.
    """
    try:
        if log_target is not None:
            envelope.append_log(log_target, f"{event}: degraded: {detail}")
            return
        cwd = Path.cwd()
        if not is_operator_home(cwd):
            envelope.append_log(cwd, f"{event}: degraded: {detail}")
    except OSError:
        return


def _append_error_diagnostic(event: str, log_target: Path | None, exc: BaseException) -> None:
    """Write the #735 error ``hook.log`` before ``hook_run`` returns."""
    if log_target is None:
        return
    try:
        envelope.append_log(log_target, f"{event}: error: {type(exc).__name__}: {exc}")
    except OSError:
        return


def _bounded_timeout_followup(event: str, log_target: Path, session_id: str, exc: BaseException) -> str:
    """Record timeout latch/state without blocking past a short budget.

    The path was already resolved inside the timed worker. Latch-state
    writes (and that path's target-tree hook.log) still touch that tree;
    they run best-effort under a hard cap so a later stall cannot hang
    the parent. The latch outcome is published as soon as state is known
    so a later log stall cannot hide it. Non-timeout doctor-pointer logs
    use ``_append_degraded_diagnostic`` instead.
    """
    detail = str(exc)
    published = ["degraded"]

    def publish(outcome: str) -> None:
        published[0] = outcome

    _run_best_effort_bounded(
        lambda: _apply_timeout_followup_inprocess(event, log_target, session_id, detail, publish=publish),
        timeout=_TIMEOUT_FOLLOWUP_SECONDS,
        default="degraded",
    )
    return published[0]


def _bounded_release_claim(path: Path | None) -> None:
    if path is None:
        return
    _run_best_effort_bounded(
        lambda: compaction_marker.release_claim_path(path),
        timeout=_TIMEOUT_FOLLOWUP_SECONDS,
        default=None,
    )


def _bounded_complete_claim(path: Path | None) -> None:
    if path is None:
        return
    _run_best_effort_bounded(
        lambda: compaction_marker.complete_claim_path(path),
        timeout=_TIMEOUT_FOLLOWUP_SECONDS,
        default=None,
    )


def hook_run(
    *,
    event: str,
    package: str,
    stdin_text: str | None = None,
    target: Path | None = None,
) -> int:
    """Run one managed Claude hook under the #735 output contract.

    Always exits 0. Stdout is exactly one schema-valid JSON object (empty
    envelope, degraded doctor pointer, latched notice, or a rendered
    decision/injection). Diagnostics go to the local hook log, never to the
    session stream.

    The parent reads bounded stdin, spawns the timed worker, and enforces
    the timeout. Pin resolution, target discovery, log-target lookup, latch
    I/O, and ``handle_payload`` run only inside that worker. Post-timeout
    latch/state persistence and claim cleanup are best-effort under a hard
    cap so a stalled tree cannot hang the parent after the worker returns.
    The doctor-pointer / error ``hook.log`` write is synchronous and
    finishes before this function returns.
    """
    if package != PACKAGE_REF:
        return 0

    raw = ""
    claim_path: Path | None = None
    log_target: Path | None = None
    session_id = "unknown"
    result: dict[str, Any] | None = None
    try:
        payload, raw = _load_hook_stdin(stdin_text=stdin_text)
        session_id = _session_id_from_payload(payload)
        outcome = _normalize_timed_outcome(_run_timed_handle_payload(event, raw, pin=target))
        log_target = _path_or_none(outcome.get("log_target"))
        session_id = str(outcome.get("session_id") or session_id)
        claim_path = _path_or_none(outcome.get("claim_path"))
        kind = str(outcome.get("kind") or "ok")
        raw_result = outcome.get("result")
        result = raw_result if isinstance(raw_result, dict) else None
        if kind == "latched":
            envelope.emit_stdout(envelope.latched_envelope(event))
            return 0
        if kind == "latched_silent":
            envelope.emit_stdout(envelope.empty_envelope(event))
            return 0
    except HookDegraded as exc:
        _bounded_release_claim(claim_path)
        claim_path = None
        followup_target = exc.log_target if exc.log_target is not None else log_target
        if followup_target is not None and _is_timeout_degraded(exc):
            followup = _bounded_timeout_followup(event, followup_target, session_id, exc)
            if followup == "latched":
                envelope.emit_stdout(envelope.latched_envelope(event))
                return 0
            if followup == "latched_silent":
                envelope.emit_stdout(envelope.empty_envelope(event))
                return 0
        else:
            _append_degraded_diagnostic(event, followup_target, exc)
        envelope.emit_stdout(envelope.degraded_envelope(event))
        return 0
    except Exception as exc:  # noqa: BLE001 - hooks must fail open instead of breaking the harness
        _bounded_release_claim(claim_path)
        claim_path = None
        _append_error_diagnostic(event, log_target, exc)
        envelope.emit_stdout(envelope.degraded_envelope(event))
        return 0

    try:
        envelope.emit_stdout(result if result is not None else envelope.empty_envelope(event))
    except Exception as exc:  # noqa: BLE001 - stdout failure still fails open
        _bounded_release_claim(claim_path)
        claim_path = None
        if log_target is not None:
            try:
                envelope.append_log(log_target, f"{event}: emit failed: {exc}")
            except OSError:
                pass
        try:
            envelope.emit_stdout(envelope.degraded_envelope(event))
        except Exception:  # noqa: BLE001
            pass
        return 0
    _bounded_complete_claim(claim_path)
    return 0
