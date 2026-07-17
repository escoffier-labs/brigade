"""Claude Code hook runtime for the Brigade work loop."""

from __future__ import annotations

import json
import os
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
_SHELL_WRAPPERS = {"bash", "dash", "ksh", "sh", "zsh"}
_SHELL_CONTROL_PREFIXES = {"!", "{", "do", "elif", "if", "then", "time", "until", "while"}
_SHELL_CONTROL_TOKENS = _SHELL_CONTROL_PREFIXES | {"}", "case", "done", "else", "esac", "fi", "for", "select"}
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


def _nested_shell_commands(command: str) -> list[str]:
    nested: list[str] = []
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
                return nested
            text, end = parsed
            nested.append(text)
            index = end + 1
            continue
        if char == "`":
            end = index + 1
            while end < len(command):
                if command[end] == "`" and command[end - 1] != "\\":
                    nested.append(command[index + 1 : end])
                    index = end + 1
                    break
                end += 1
            else:
                return nested
            continue
        if char == "(" and quote is None:
            parsed = _read_parenthesized(command, index + 1)
            if parsed is None:
                return nested
            text, end = parsed
            nested.append(text)
            index = end + 1
            continue
        index += 1
    return nested


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
    all_target_commands = {"mkdir", "rm", "rmdir", "tee", "touch"}
    last_target_commands = {"cp", "install", "mv", "truncate"}
    for segment in segments:
        stripped = _strip_env(segment)
        if not stripped:
            continue
        targets: list[str] = []
        redirected_indexes: set[int] = set()
        for index, token in enumerate(stripped[:-1]):
            if token and set(token) <= set(";&|<>") and ">" in token:
                targets.append(stripped[index + 1])
                redirected_indexes.add(index + 1)
        command_name = Path(stripped[0]).name
        positionals = [
            token
            for index, token in enumerate(stripped[1:], start=1)
            if index not in redirected_indexes
            and not token.startswith("-")
            and not (token and set(token) <= set(";&|<>"))
        ]
        if command_name in all_target_commands:
            targets.extend(positionals)
        elif command_name in last_target_commands and positionals:
            targets.append(positionals[-1])
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


def _bash_write_detected(target: Path, baseline: object) -> bool:
    if not isinstance(baseline, str) or not baseline:
        return False
    current = repo_worktree_fingerprint(target)
    return current is not None and current != baseline


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
    denied = payload.get("verify_denied_count")
    if isinstance(denied, int) and not isinstance(denied, bool) and denied >= 0:
        normalized["verify_denied_count"] = denied
    last_write = localio.parse_iso_datetime(payload.get("last_write_at"))
    if last_write is not None and last_write <= now:
        normalized["last_write_at"] = last_write.isoformat()
    last_verification_write = localio.parse_iso_datetime(payload.get("last_verification_write_at"))
    if last_verification_write is not None and last_verification_write <= now:
        normalized["last_verification_write_at"] = last_verification_write.isoformat()
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
    target = resolve_wired_target(payload.get("cwd"))
    if target is None:
        return None
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        return None

    persisted_state = read_session_state(target, session_id)
    state = _normalize_state(target, session_id, persisted_state)
    if persisted_state != state:
        write_session_state(target, session_id, state)
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
            wrote = _bash_write_detected(target, state.get("pending_bash_fingerprint"))
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
            write_session_state(target, session_id, state)
        elif state.get("pending_bash_fingerprint") is not None:
            state.pop("pending_bash_fingerprint", None)
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
        if payload.get("stop_hook_active") is True or not state.get("write_observed"):
            return None
        fingerprint = state.get("session_fingerprint")
        if not isinstance(fingerprint, str):
            fingerprint = _session_fingerprint(session_id)
        receipt_threshold = (
            state.get("last_verification_write_at") or state.get("last_write_at") or state.get("started_at")
        )
        if not _receipt_since(target, receipt_threshold, session_fingerprint=fingerprint):
            replacement = _verify_replacement(target, "<test>", fingerprint)
            return {
                "decision": "block",
                "reason": (
                    "Recent write work in this Brigade-wired repo has no verification receipt for this session. "
                    f"Run `{replacement}`, then finish again."
                ),
            }
        if not _handoff_since(target, state.get("started_at")):
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
