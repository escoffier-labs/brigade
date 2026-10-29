"""Route shell write evidence to each affected wired worktree."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..wiring import resolve_wired_target


def _rt() -> Any:
    # Runtime owns the shell lexer and state machinery, including test seams.
    from . import runtime

    return runtime


def _literal_path(raw: str, cwd: Path) -> Path | None:
    if not raw or any(char in raw for char in "$`*?[]{}"):
        return None
    return _rt()._resolve_command_path(raw, cwd)


def _git_command(tokens: list[str], cwd: Path) -> tuple[list[str], Path]:
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-C", "-c", "--git-dir", "--work-tree"}:
            if index + 1 >= len(tokens):
                return [], cwd
            if token == "-C":
                path = _literal_path(tokens[index + 1], cwd)
                if path is None:
                    return [], cwd
                cwd = path
            elif token in {"--git-dir", "--work-tree"}:
                return [], cwd  # Nonstandard layouts retain fingerprint fallback.
            index += 2
        elif token.startswith("-C"):
            path = _literal_path(token[2:], cwd)
            if path is None:
                return [], cwd
            cwd = path
            index += 1
        elif token.startswith("-"):
            index += 1
        else:
            return tokens[index:], cwd
    return [], cwd


# Short options whose next token is a value, not a path the command writes
# (`touch -r REF` only reads REF's times). Long `--opt=value` forms are already
# dropped as options.
_OPTION_VALUES = {"touch": "rdt", "truncate": "rs", "mkdir": "m", "mv": "St", "cp": "St", "install": "gmoSt"}
_TARGET_DIRECTORY_COMMANDS = {"cp", "install", "mv"}


def _target_directory(tokens: list[str]) -> str | None:
    """The ``-t DIR`` / ``--target-directory=DIR`` destination of cp, install, or mv."""
    for index, token in enumerate(tokens):
        if token == "--":
            return None
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if token == "--target-directory":
            return following
        if token.startswith("--target-directory="):
            return token.split("=", 1)[1]
        if token.startswith("-") and not token.startswith("--") and "t" in token[1:]:
            rest = token[token.index("t", 1) + 1 :]
            return rest or following
    return None


def _without_option_values(name: str, tokens: list[str]) -> list[str] | None:
    letters = _OPTION_VALUES.get(name, "")
    kept: list[str] = []
    skip = False
    for token in tokens:
        if skip:
            skip = False
            continue
        if token == "--":
            letters = ""
        elif letters and token.startswith("-") and not token.startswith("--") and len(token) > 1:
            skip = token[-1] in letters
        elif token == "--target-directory" and name in _TARGET_DIRECTORY_COMMANDS:
            skip = True  # read separately by _target_directory
        elif token in {"--reference", "--date", "--size", "--mode", "--suffix", "--owner", "--group"}:
            return None  # a separated long-option value: stay conservative
        kept.append(token)
    return kept


def _write_operands(tokens: list[str]) -> list[str] | None:
    rt = _rt()
    name = Path(tokens[0]).name
    operand_tokens = _without_option_values(name, tokens[1:])
    if operand_tokens is None:
        return None
    args = rt._command_path_arguments(operand_tokens)
    if name == "sed":
        if not any(arg.startswith("-i") for arg in tokens[1:]):
            return []
        # With -e/-f the script is an option operand. Otherwise it is the
        # first positional operand. Never mistake the expression for a path.
        if any(arg in {"-e", "-f", "--expression", "--file"} for arg in tokens[1:]):
            return None
        return args[1:] or None
    if name == "ruff":
        if tokens[1:2] != ["format"] or {"--check", "--diff"}.intersection(tokens[2:]):
            return []
        return rt._command_path_arguments(tokens[2:]) or ["."]
    if name in rt._BASH_WRITE_COMMANDS:
        if name in {"apply_patch", "patch", "mktemp"}:
            return None
        if name in _TARGET_DIRECTORY_COMMANDS:
            directory = _target_directory(tokens[1:])
            if directory is not None:
                # cp/install sources are read; mv sources are removed, so they change too.
                return [directory, *args] if name == "mv" else [directory]
            if name in {"cp", "install"}:
                # Source operands are read; the final operand is the destination.
                return args[-1:] or None
        return args if args or name == "tee" else None
    return []


def bash_scopes(command: object, cwd: Path, *, depth: int = 0) -> dict[Path, bool]:
    """Map observation roots to whether an explicit source write names them.

    Observation roots keep fingerprint detection for scripts and subagents.
    Unresolved write operands conservatively retain their command cwd.
    """
    rt = _rt()
    if not isinstance(command, str) or not command.strip():
        return {}
    try:
        tokens = rt._shell_path_tokens(rt._strip_heredoc_bodies(command))
    except ValueError:
        return {}
    scopes: dict[Path, bool] = {}

    def add(path: Path, confident: bool = False) -> None:
        target = resolve_wired_target(str(path))
        if target is not None:
            scopes[target] = scopes.get(target, False) or confident

    def destination(raw: str, effective: Path) -> None:
        path = _literal_path(raw, effective)
        if path is None:
            add(effective, True)
        elif not rt._path_targets_brigade_internal_artifact(resolve_wired_target(str(path)) or effective, str(path)):
            add(path, True)

    segments: list[list[str]] = [[]]
    separators: list[str] = []
    for token in tokens:
        if token in rt._SHELL_SEPARATORS:
            separators.append(token)
            segments.append([])
        else:
            segments[-1].append(token)
    effective = cwd
    for index, segment in enumerate(segments):
        stripped = rt._strip_command_prefixes(segment)
        if not stripped:
            continue
        add(effective)
        for raw in rt._redirect_file_targets(segment):
            destination(raw, effective)
        name = Path(stripped[0]).name
        if name == "cd":
            args = stripped[1:]
            if args[:1] == ["--"]:
                args = args[1:]
            piped = (index > 0 and separators[index - 1] in rt._PIPELINE_SEPARATORS) or (
                index < len(separators) and separators[index] in rt._PIPELINE_SEPARATORS
            )
            path = _literal_path(args[0], effective) if len(args) == 1 and args[0] != "-" else None
            if path is not None and not piped:
                effective = path
                add(effective)
            continue
        nested = rt._nested_shell_script(stripped)
        if nested is not None and depth < 4:
            for target, confident in bash_scopes(nested, effective, depth=depth + 1).items():
                add(target, confident)
            continue
        if name == "git":
            subcommand, git_cwd = _git_command(stripped, effective)
            add(git_cwd, bool(subcommand and subcommand[0] in rt._GIT_WRITE_COMMANDS))
            continue
        operands = _write_operands(stripped)
        if operands is None:
            add(effective, True)
            # Still observe every literal path operand, so a fingerprint catches a
            # write the parser could not place.
            for raw in rt._command_path_arguments(stripped[1:]):
                path = _literal_path(raw, effective)
                if path is not None:
                    add(path)
        else:
            for operand in operands:
                destination(operand, effective)
    return scopes


def touches_worktree(target: Path, command: object, *, cwd: object = None) -> bool:
    effective = Path(cwd) if isinstance(cwd, str) and cwd else target
    return bash_scopes(command, effective).get(target.resolve(), False)


def handle_event(event: str, payload: dict[str, Any], *, pin: Path | None) -> dict[str, Any] | None:
    rt = _rt()
    primary = rt.effective_hook_target(payload, pin=pin)
    if (
        primary is None
        or event not in {"PreToolUse", "PostToolUse", "PostToolUseFailure"}
        or payload.get("tool_name") != "Bash"
    ):
        return rt._handle_event(event, payload, pin=pin)
    command = rt._as_tool_input(payload.get("tool_input")).get("command")
    raw_cwd = payload.get("cwd")
    cwd = Path(raw_cwd) if isinstance(raw_cwd, str) and raw_cwd else primary
    scopes = bash_scopes(command, cwd)
    targets = [primary, *scopes]
    result = None
    for target in dict.fromkeys(targets):
        # Reuse the normal pin/home policy for every additional root.
        if target != primary and rt.effective_hook_target({"cwd": str(target)}, pin=pin) != target:
            continue
        if target != primary and event != "PreToolUse" and not scopes.get(target):
            # A post event without a corresponding pre event has no baseline
            # to compare in an observation-only root. Do not create state there.
            state = rt.read_session_state(target, str(payload.get("session_id") or ""))
            if not state or not state.get("pending_bash_fingerprint"):
                continue
        current = rt._handle_event(event, payload, pin=pin, target_override=target)
        if current is not None:
            result = current
            if current.get("hookSpecificOutput", {}).get("permissionDecision") == "deny":
                break
    return result
