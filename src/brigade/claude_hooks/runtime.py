"""Claude Code hook runtime for the Brigade work loop."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .. import localio
from ..config import load_config
from .package import PACKAGE_REF

BRIEF_TIMEOUT_SECONDS = 10
BRIEF_MAX_CHARS = 8_000
MAX_RECENT_SESSION_STATES = 512
CLAUDE_SESSION_ENV = "BRIGADE_CLAUDE_SESSION"
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


def resolve_wired_target(cwd: object) -> Path | None:
    if not isinstance(cwd, str) or not cwd.strip():
        return None
    try:
        current = Path(cwd).expanduser().resolve()
    except OSError:
        return None
    if not current.is_dir():
        current = current.parent
    for candidate in (current, *current.parents):
        if not (candidate / ".brigade" / "config.json").is_file():
            continue
        try:
            config = load_config(candidate)
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        if config is not None and "claude" in config.selection.harnesses:
            return candidate
        return None
    return None


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


def _session_repo_union(session_id: str, *seeds: Path) -> list[str]:
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
        raw_repos = state.get("session_repos")
        if not isinstance(raw_repos, list):
            continue
        for raw in raw_repos:
            if isinstance(raw, str) and raw and raw not in seen:
                pending.append(Path(raw))
    return sorted(repos)


def _link_session_targets(session_id: str, *targets: Path) -> None:
    sorted_repos = _session_repo_union(session_id, *targets)
    for repo in sorted_repos:
        target = Path(repo)
        state = read_session_state(target, session_id)
        if state is None:
            continue
        if state.get("session_repos") == sorted_repos:
            continue
        updated = dict(state)
        updated["session_repos"] = sorted_repos
        write_session_state(target, session_id, updated)


def _touch_session_targets(session_id: str, target: Path, payload: dict[str, Any]) -> None:
    cwd_target = resolve_wired_target(payload.get("cwd"))
    if cwd_target is not None:
        _link_session_targets(session_id, target, cwd_target)
    else:
        _link_session_targets(session_id, target)


def _stop_targets(payload: dict[str, Any], session_id: str) -> list[Path]:
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
    except (OSError, subprocess.TimeoutExpired):
        return f"Brigade is wired for this repo. Run `brigade work brief --target {target}` before real work."
    text = result.stdout.strip()
    if result.returncode != 0 or not text:
        return f"Brigade is wired for this repo. Run `brigade work brief --target {target}` before real work."
    if len(text) > BRIEF_MAX_CHARS:
        text = text[:BRIEF_MAX_CHARS] + "\n[Brigade brief truncated]"
    return text


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
    return normalized.endswith("/scripts/verify") or normalized == "scripts/verify"


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


def _bash_write_detected(
    target: Path,
    baseline: object,
    *,
    session_id: str,
    started_at: object,
) -> bool:
    if not isinstance(baseline, str) or not baseline:
        return False
    current = repo_worktree_fingerprint(target)
    if current is None or current == baseline:
        return False
    started = localio.parse_iso_datetime(started_at)
    if started is None:
        return True
    for other_state in iter_session_states(target, limit=MAX_RECENT_SESSION_STATES):
        if other_state.get("session_id") == session_id:
            continue
        if other_state.get("write_observed") is not True or other_state.get("repo_fingerprint") != current:
            continue
        other_write = localio.parse_iso_datetime(other_state.get("last_write_at"))
        if other_write is not None and other_write >= started:
            return False
    return True


def _session_fingerprint(session_id: str) -> str:
    return localio.stable_hash({"claude_session_id": session_id})


def _verify_replacement(target: Path, command: str, session_fingerprint: str) -> str:
    return (
        f"{CLAUDE_SESSION_ENV}={shlex.quote(session_fingerprint)} "
        f"brigade work verify run --target {shlex.quote(str(target))} "
        f"--command {shlex.quote(command)} --capture brigade-work"
    )


def _new_state(target: Path, session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "session_fingerprint": _session_fingerprint(session_id),
        "target": str(target),
        "started_at": localio.utc_now_iso(),
        "briefed": False,
        "write_observed": False,
        "verify_denied_count": 0,
        "repo_fingerprint": repo_worktree_fingerprint(target),
    }


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


def _normalize_state(target: Path, session_id: str, payload: dict[str, Any] | None) -> dict[str, Any]:
    normalized = _new_state(target, session_id)
    if not isinstance(payload, dict):
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
    denied = payload.get("verify_denied_count")
    if isinstance(denied, int) and not isinstance(denied, bool) and denied >= 0:
        normalized["verify_denied_count"] = denied
    last_write = localio.parse_iso_datetime(payload.get("last_write_at"))
    if last_write is not None and last_write <= now:
        normalized["last_write_at"] = last_write.isoformat()
    last_verification_write = localio.parse_iso_datetime(payload.get("last_verification_write_at"))
    if last_verification_write is not None and last_verification_write <= now:
        normalized["last_verification_write_at"] = last_verification_write.isoformat()
    session_repos = payload.get("session_repos")
    if isinstance(session_repos, list) and all(isinstance(item, str) for item in session_repos):
        normalized["session_repos"] = list(session_repos)
    return normalized


def _receipt_since(target: Path, started_at: object, *, session_fingerprint: str | None = None) -> bool:
    started = localio.parse_iso_datetime(started_at)
    if started is None:
        return False
    root = target / ".brigade" / "work" / "verify-runs"
    if not root.is_dir():
        return False
    for path in root.glob("*/receipt.json"):
        receipt = localio.read_json_dict(path)
        if not receipt or receipt.get("status") not in {"completed", "failed", "rejected"}:
            continue
        if session_fingerprint is not None:
            harness_session = receipt.get("harness_session")
            if not isinstance(harness_session, dict):
                continue
            if harness_session.get("harness") != "claude" or harness_session.get("fingerprint") != session_fingerprint:
                continue
        receipt_started = localio.parse_iso_datetime(receipt.get("started_at"))
        if receipt_started is not None and receipt_started >= started:
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


def _additional_context(event: str, text: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}


def handle_payload(event: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    target = wired_target_from_payload(payload)
    if target is None:
        return None
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None

    persisted_state = read_session_state(target, session_id)
    state = _normalize_state(target, session_id, persisted_state)
    if persisted_state != state:
        write_session_state(target, session_id, state)
    if event != "Stop":
        _touch_session_targets(session_id, target, payload)
    if event == "SessionStart":
        if state.get("briefed"):
            return None
        state["briefed"] = True
        write_session_state(target, session_id, state)
        return _additional_context("SessionStart", _run_brief(target))

    if event == "PreToolUse":
        tool_name = payload.get("tool_name")
        if tool_name in _WRITE_TOOLS:
            return None
        if tool_name != "Bash":
            return None
        raw_tool_input = payload.get("tool_input")
        tool_input: dict[str, Any] = raw_tool_input if isinstance(raw_tool_input, dict) else {}
        command = tool_input.get("command")
        baseline = repo_worktree_fingerprint(target)
        if baseline is not None:
            state["pending_bash_fingerprint"] = baseline
            state["pending_bash_started_at"] = localio.utc_now_iso()
            write_session_state(target, session_id, state)
        if not is_raw_verification(command):
            return None
        state["verify_denied_count"] = int(state.get("verify_denied_count") or 0) + 1
        write_session_state(target, session_id, state)
        if _has_unsupported_verifier_structure(str(command)):
            reason = (
                "Route verification through Brigade so failed, rejected, and passing results create receipts.\n"
                "Split shell grouping, command substitution, pipelines, redirection, or complex directory changes "
                "from the verifier, then run that verifier with `brigade work verify run --capture brigade-work`."
            )
        else:
            replacement = _verify_replacement(
                target,
                _first_verifier_command(str(command)),
                str(state["session_fingerprint"]),
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
        wrote = tool_name in _WRITE_TOOLS or (
            tool_name == "Bash" and _is_confident_bash_write(post_tool_input.get("command"))
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
            write_session_state(target, session_id, state)
        elif state.get("pending_bash_fingerprint") is not None:
            state.pop("pending_bash_fingerprint", None)
            state.pop("pending_bash_started_at", None)
            write_session_state(target, session_id, state)
        return None

    if event == "PostToolUseFailure":
        raw_tool_input = payload.get("tool_input")
        tool_input = raw_tool_input if isinstance(raw_tool_input, dict) else {}
        command = tool_input.get("command")
        if payload.get("tool_name") == "Bash" and (is_raw_verification(command) or _is_routed_verify(command)):
            return _additional_context(
                "PostToolUseFailure",
                "The failed or rejected verification must remain recorded in Brigade before retrying. Inspect the receipt, fix the cause, then rerun through `brigade work verify run --capture brigade-work`.",
            )
        return None

    if event == "Stop":
        if payload.get("stop_hook_active") is True:
            return None
        blocking_failures: list[str] = []
        handoff_target: Path | None = None
        for stop_target in _stop_targets(payload, session_id):
            if not stop_target.is_dir():
                continue
            stop_state = read_session_state(stop_target, session_id)
            stop_state = _normalize_state(stop_target, session_id, stop_state)
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
                replacement = _verify_replacement(stop_target, "<test>", fingerprint)
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
        if handoff_target is not None:
            return _additional_context(
                "Stop",
                "Verification is recorded. If this work produced durable knowledge, write a Memory Handoff in `.claude/memory-handoffs/` before finishing.",
            )
    return None


def hook_run(*, event: str, package: str, stdin_text: str | None = None) -> int:
    if package != PACKAGE_REF:
        return 0
    try:
        raw = sys.stdin.read() if stdin_text is None else stdin_text
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return 0
        result = handle_payload(event, payload)
    except Exception:  # noqa: BLE001 - hooks must fail open instead of breaking the harness
        return 0
    if result is not None:
        print(json.dumps(result, separators=(",", ":"), sort_keys=True))
    return 0
