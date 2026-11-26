"""Recognize completed shell checks for observation and retry guidance.

A check whose execution directory is unknown or unwired uses the hook's
target to resolve its work mode.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from .. import work_mode
from ..wiring import resolve_wired_target
from .write_attribution import _literal_path, _rt

SHELL_CONTROL_PREFIXES = {"!", "{", "do", "elif", "if", "then", "time", "until", "while"}
_SHELL_CONTROL_TOKENS = SHELL_CONTROL_PREFIXES | {"}", "case", "done", "else", "esac", "fi", "for", "select"}

_KEYWORDS = {"!", "{", "(", "if", "then", "elif", "else", "do", "while", "until", "time"}
# Text that makes a command more than a plain list: grouping, subshells, command and
# process substitution.
_COMPLEX_MARKERS = ("(", ")", "{", "}", "`", "$(", "<(", ">(")
_MAX_DEPTH = 4
# Options through which a wrapper or tool runs the check somewhere other than the shell's
# directory (`env -C`, `make -C`, `npm --prefix`, `uv --directory`, ...). A verifier that
# carries one has an undetermined directory.
_CWD_OPTIONS = (
    "-C",
    "--chdir",
    "--directory",
    "--cwd",
    "--prefix",
    "--rootdir",
    "--manifest-path",
    # Workspace and project selectors run the check in a member package's directory.
    "-w",
    "--workspace",
    "--workspaces",
    "--filter",
    "--package",
    "--project",
    "--dir",
)
# Subcommands that select a workspace member (`yarn workspace <name> test`).
_WORKSPACE_SUBCOMMANDS = {"workspace", "workspaces"}


def _overrides_cwd(segment: list[str]) -> bool:
    if any(token in _WORKSPACE_SUBCOMMANDS for token in segment[1:]):
        return True
    for token in segment:
        if token in _CWD_OPTIONS or any(token.startswith(f"{option}=") for option in _CWD_OPTIONS if option != "-C"):
            return True
        if token.startswith("-C") and not token.startswith("--"):
            return True  # `-C<dir>` written as one token
    return False


def _plain_and_chain(command: str, segments: list[list[str]], separators: list[str]) -> bool:
    """True when ``command`` is commands joined only by ``&&``, with nothing that hides one.

    In such a chain a ``cd`` that fails stops every later command, so each later
    command runs in the ``cd`` destination or not at all. Any other separator
    (``;``, ``||``, ``&``, a pipe, a newline), a group, a control structure, or a
    substitution can let a command run somewhere else.
    """
    if any(separator != "&&" for separator in separators):
        return False
    if any(marker in command for marker in _COMPLEX_MARKERS):
        return False
    return not any(segment and (segment[0] in _SHELL_CONTROL_TOKENS or segment[0] in _KEYWORDS) for segment in segments)


# Shells and inline-code flags through which a runner (`uv run`, `npx`, `poetry run`, ...)
# can carry a script that changes directory. Checks after the allowlisted cd may use neither.
_SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "pwsh", "powershell", "cmd", "busybox"}
_INLINE_CODE_FLAGS = {"-c", "-e", "--command", "--eval"}


def _carries_script(segment: list[str]) -> bool:
    """True when ``segment`` could run code the walker cannot see, such as ``uv run bash -c '...'``."""
    for token in segment[1:]:
        name = Path(token).name.lower().removesuffix(".exe")
        if name in _SHELLS or token in _INLINE_CODE_FLAGS or any(char.isspace() for char in token):
            return True
    return False


def _cd_then_checks(segments: list[list[str]]) -> bool:
    """True when the command is one ``cd`` to an absolute literal path, then only checks.

    This is an allowlist, not a model of the shell. The ``cd`` comes first, names an
    absolute or ``./`` path with no ``..`` component (so ``CDPATH`` and the logical
    directory play no part), and every later command is a verification with no working-directory
    option and no inline script. Anything else after the ``cd``, such as ``eval``, ``source``, another ``cd``,
    a nested shell, or an ordinary command, could change directory unseen.
    """
    rt = _rt()
    pairs = [(segment, stripped) for segment in segments if (stripped := rt._strip_command_prefixes(segment))]
    if not pairs or Path(pairs[0][1][0]).name != "cd":
        return False
    args = pairs[0][1][1:]
    if args[:1] == ["--"]:
        args = args[1:]
    if len(args) != 1 or ".." in Path(args[0]).parts:
        return False
    # Bash looks a bare relative name up in CDPATH, but never an absolute path or one that
    # starts with `./`, so only those two forms land where the walker resolves them.
    if not (Path(args[0]).is_absolute() or args[0].startswith("./")):
        return False
    for segment, head in pairs[1:]:
        if Path(head[0]).name in {"cd", "pushd", "popd"} or rt._nested_shell_script(head) is not None:
            return False
        if not (_segment_is_verifier(segment) or rt._is_brigade_verify(rt._strip_env(segment))):
            return False
        if _overrides_cwd(segment) or _carries_script(head):
            return False
    return True


def verifier_cwds(command: str, cwd: Path | None, *, depth: int = 0) -> list[Path | None]:
    """Directories in which each verification segment of ``command`` runs.

    ``None`` marks a verification whose directory cannot be determined. An
    empty list means no verification segment was found. A ``cd`` moves later
    verifications only inside a plain ``&&`` chain; a command that changes
    directory any other way marks every verification in it undetermined.
    """
    rt = _rt()
    try:
        tokens = rt._shell_tokens(rt._strip_heredoc_bodies(command))
    except ValueError:
        return [None]
    segments: list[list[str]] = [[]]
    separators: list[str] = []
    for token in tokens:
        if token in rt._SHELL_SEPARATORS:
            separators.append(token)
            segments.append([])
        else:
            segments[-1].append(token)
    # Judged on token text: `(cd x`, `{ cd x`, `$(cd x` and a backtick cd hide it from a segment-head check.
    changes_directory = any(token.lstrip("({`$") in {"cd", "pushd", "popd"} for token in tokens)
    if changes_directory and not (_plain_and_chain(command, segments, separators) and _cd_then_checks(segments)):
        return [None]
    found: list[Path | None] = []
    effective = cwd
    for segment in segments:
        stripped = rt._strip_command_prefixes(segment)
        while stripped and stripped[0] in _KEYWORDS:
            stripped = rt._strip_command_prefixes(stripped[1:])
        if not stripped:
            continue
        if Path(stripped[0]).name == "cd":
            args = stripped[1:]
            if args[:1] == ["--"]:
                args = args[1:]
            path = None
            if effective is not None and len(args) == 1 and args[0] != "-":
                path = _literal_path(args[0], effective)
            effective = path
            continue
        nested = rt._nested_shell_script(stripped)
        if nested is not None:
            if depth >= _MAX_DEPTH:
                found.append(None)
            else:
                # A wrapper such as `env -C dir bash -c ...` starts the nested shell elsewhere.
                start = None if _overrides_cwd(stripped) else effective
                found.extend(verifier_cwds(nested, start, depth=depth + 1))
            continue
        if _segment_is_verifier(segment) or rt._is_brigade_verify(rt._strip_env(segment)):
            found.append(None if _overrides_cwd(segment) else effective)
    return found


def governing_modes(
    command: object, cwd: object, root: Path, *, harness: str | None = "claude"
) -> list[tuple[Path, str]]:
    """Resolve each verifier's governing target, preserving first-seen order."""
    if not isinstance(command, str) or not command.strip():
        return []
    start = Path(cwd) if isinstance(cwd, str) and cwd else root
    modes: dict[Path, str] = {}
    for path in verifier_cwds(command, start):
        wired = resolve_wired_target(str(path), harness=harness) if path is not None else None
        target = wired if wired is not None else root
        modes[target] = work_mode.resolve_mode(target)
    return list(modes.items())


def strip_npx_options(tokens: list[str]) -> tuple[list[str], bool]:
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


def package_runner_script(tokens: list[str]) -> str | None:
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


def strip_make_global_options(tokens: list[str]) -> tuple[list[str], bool]:
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


def strip_runner_global_options(command: str, tokens: list[str]) -> tuple[list[str], bool]:
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


def strip_runner_run_options(command: str, tokens: list[str]) -> tuple[list[str], bool]:
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
            script = package_runner_script(tokens[index:])
            if script and (script in {"test", "check"} or script.startswith(("test:", "check:"))):
                return True
    return False


def _nested_shell_commands(command: str) -> list[str]:
    return [text for _, text in _rt()._nested_shell_command_spans(command)]


def _segment_is_verifier(segment: list[str], *, depth: int = 0) -> bool:
    if depth > _MAX_DEPTH:
        return False
    rt = _rt()
    tokens = rt._strip_env(segment)
    while tokens and tokens[0] in SHELL_CONTROL_PREFIXES:
        tokens = tokens[1:]
    if not tokens or rt._is_brigade_verify(tokens):
        return False
    names = [Path(token).name for token in tokens]
    command = names[0]
    if command in {"pytest", "py.test", "nose2", "tox", "nox", "vitest", "jest", "mocha", "rspec", "phpunit", "ctest"}:
        return True
    python_invocation = rt._python_module_invocation(tokens)
    if python_invocation and python_invocation[0] in {"pytest", "unittest", "nose", "nose2"}:
        return True
    if command in {"npm", "pnpm", "yarn", "bun"}:
        tail, ambiguous = strip_runner_global_options(command, tokens[1:])
        script = package_runner_script(tail)
        if script and (script in {"test", "check"} or script.startswith(("test:", "check:"))):
            return True
        return ambiguous and _ambiguous_runner_contains_verifier(command, tail, depth=depth)
    if command in {"uv", "poetry"}:
        tail, ambiguous = strip_runner_global_options(command, tokens[1:])
        if tail[:1] != ["run"]:
            return ambiguous and _ambiguous_runner_contains_verifier(command, tail, depth=depth)
        tail, run_ambiguous = strip_runner_run_options(command, tail[1:])
        if _segment_is_verifier(tail, depth=depth + 1):
            return True
        return run_ambiguous and _ambiguous_tail_contains_verifier(tail, depth=depth)
    if command == "npx":
        tail, ambiguous = strip_npx_options(tokens[1:])
        if _segment_is_verifier(tail, depth=depth + 1):
            return True
        return ambiguous and any(_segment_is_verifier(tail[index:], depth=depth + 1) for index in range(1, len(tail)))
    shell_payload = rt._shell_wrapper_payload(tokens)
    if shell_payload is not None:
        return _is_raw_verification_text(shell_payload, depth=depth + 1)
    if command == "go" and names[1:2] in (["test"], ["vet"]):
        return True
    if command == "cargo" and names[1:2] in (["test"], ["clippy"]):
        return True
    if command == "make":
        tail, ambiguous = strip_make_global_options(tokens[1:])
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
    if depth > _MAX_DEPTH:
        return False
    rt = _rt()
    command = rt._strip_heredoc_bodies(command)
    try:
        tokens = rt._shell_tokens(command)
    except ValueError:
        return False
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in rt._SHELL_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    if any(_segment_is_verifier(segment, depth=depth) for segment in segments):
        return True
    return any(_is_raw_verification_text(nested, depth=depth + 1) for nested in _nested_shell_commands(command))


def is_raw_verification(command: object) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    return _is_raw_verification_text(command, depth=0)


def first_verifier_command(command: str) -> str:
    """Extract the first recognized check for observed tool labeling."""
    rt = _rt()
    try:
        tokens = rt._shell_tokens(command)
    except ValueError:
        return command
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in rt._SHELL_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    for segment in segments:
        if _segment_is_verifier(segment):
            runnable = list(segment)
            command_index = 0
            while command_index < len(runnable) and rt._is_env_assignment(runnable[command_index]):
                command_index += 1
            if command_index < len(runnable) and Path(runnable[command_index]).name == "command":
                stripped = rt._strip_env(runnable[command_index:])
                runnable = [*runnable[:command_index], *stripped]
            if runnable:
                return shlex.join(runnable)
    return command
