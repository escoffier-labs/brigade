"""Metered recognition and target resolution for observed shell checks."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Generator, Literal

from .. import work_mode
from .command_tokens import (
    EVENT_BUDGET,
    MAX_COMMAND_CHARS,
    Recognition,
    RecognitionBudget,
    RecognitionLimit,
    TokenView,
    UnreadableCommand,
    _is_env_assignment,
    _package_runner_script as package_runner_script,
    _strip_env,
    _strip_make_global_options as strip_make_global_options,
    _strip_npx_options as strip_npx_options,
    _strip_runner_global_options as strip_runner_global_options,
    _strip_runner_run_options as strip_runner_run_options,
    charge_event,
    command_event,
)
from .command_tokens import event_wired_target as resolve_wired_target
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
    if EVENT_BUDGET.get() is not None:
        return _verifier_cwds(command, cwd, depth=depth)
    budget = RecognitionBudget()
    context = EVENT_BUDGET.set(budget)
    try:
        return _verifier_cwds(command, cwd, depth=depth)
    except RecognitionLimit:
        return [None]
    finally:
        EVENT_BUDGET.reset(context)
        budget.release()


def _verifier_cwds(command: str, cwd: Path | None, *, depth: int) -> list[Path | None]:
    rt = _rt()
    charge_event(2 * len(command))
    try:
        tokens, segments, separators = rt._segmented_command(rt._strip_heredoc_bodies(command))
    except ValueError:
        return [None]
    # Judged on token text: `(cd x`, `{ cd x`, `$(cd x` and a backtick cd hide it from a segment-head check.
    changes_directory = any(token.lstrip("({`$") in {"cd", "pushd", "popd"} for token in tokens)
    if changes_directory and not (_plain_and_chain(command, segments, separators) and _cd_then_checks(segments)):
        return [None]
    found: list[Path | None] = []
    effective = cwd
    for segment in segments:
        stripped = rt._strip_command_prefixes(segment, keywords=_KEYWORDS)
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
                found.extend(_verifier_cwds(nested, start, depth=depth + 1))
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
    for path in dict.fromkeys(verifier_cwds(command, start)):
        charge_event(1)
        wired = resolve_wired_target(str(path), harness=harness) if path is not None else None
        target = wired if wired is not None else root
        if target not in modes:
            modes[target] = work_mode.resolve_mode(target)
    return list(modes.items())


def _classify_segment(segment: list[str], depth: int, budget: RecognitionBudget) -> bool:
    key = id(segment), depth
    cached = budget.segment_results.get(key)
    if cached is not None:
        return cached[1]
    pending = len(budget.shell_pending)
    result = _scan_verifier(TokenView(segment, budget), depth=depth)
    if pending == len(budget.shell_pending):
        budget.segment_results[key] = segment, result
    return result


def _segment_is_verifier(segment: list[str]) -> bool:
    active = EVENT_BUDGET.get()
    with command_event() as budget:
        try:
            if _classify_segment(segment, 0, budget):
                return True
            return bool(budget.shell_pending) and _is_raw_verification_text(
                budget.shell_pending.pop(), depth=0, budget=budget
            )
        except RecognitionLimit:
            if active is not None:
                raise
            return False
        finally:
            budget.shell_pending.clear()


_ScanKind = Literal["command", "tail", "run", "package"]
_ScanRequest = tuple[TokenView, int, _ScanKind]
_ScanGenerator = Generator[_ScanRequest, bool, bool]


def _scan_verifier(segment: TokenView, *, depth: int) -> bool:
    """Evaluate suffix dependencies once without using Python's call stack."""
    scan_position = _rt()._scan_position
    results: dict[tuple[object, int, int, _ScanKind], bool] = {}
    stack: list[tuple[tuple[object, int, int, _ScanKind], _ScanGenerator, bool]] = []

    def push(request: _ScanRequest) -> bool:
        tokens, count, kind = request
        storage, index = tokens.position
        key = (storage, index, count, kind)
        if key in results:
            return results[key]
        tokens.budget.charge(1)
        evaluate = scan_position(tokens, depth=count) if kind == "command" else _scan_candidates(tokens, count, kind)
        stack.append((key, evaluate, False))
        return False

    answer = push((segment, depth, "command"))
    while stack:
        key, evaluate, started = stack[-1]
        stack[-1] = key, evaluate, True
        try:
            answer = push(evaluate.send(answer) if started else next(evaluate))
        except StopIteration as done:
            answer = bool(done.value)
            results[key] = answer
            stack.pop()
    return answer


def _scan_candidates(tokens: TokenView, depth: int, kind: _ScanKind) -> _ScanGenerator:
    if not tokens:
        return False
    if kind == "tail":
        if (yield tokens, depth, "command"):
            return True
    elif Path(tokens[0]).name == "run":
        if kind == "package":
            if _test_script(package_runner_script(tokens)):
                return True
        else:
            tail = tokens.drop(1)
            if tail and tail[0] == "--":
                tail = tail.drop(1)
            if (yield tail, depth, "command"):
                return True
    return (yield tokens.drop(1), depth, kind)


def _test_script(script: str | None) -> bool:
    return bool(script and (script in {"test", "check"} or script.startswith(("test:", "check:"))))


def _scan_position(segment: TokenView, *, depth: int) -> _ScanGenerator:
    rt = _rt()
    if not segment:
        return False
    tokens = segment
    head = tokens[0]
    command = Path(head).name
    if "=" in head or command in {"env", "command"}:
        tokens = _strip_env(tokens)
        if not tokens:
            return False
        head = tokens[0]
        command = Path(head).name
    if head in SHELL_CONTROL_PREFIXES:
        tokens = tokens.skip_while("shell control", SHELL_CONTROL_PREFIXES.__contains__)
        if not tokens:
            return False
        head = tokens[0]
        command = Path(head).name
    next_depth = depth
    if command in {"pytest", "py.test", "nose2", "tox", "nox", "vitest", "jest", "mocha", "rspec", "phpunit", "ctest"}:
        return True
    if command.startswith("python"):
        python_invocation = rt._python_module_invocation(tokens)
        if python_invocation and python_invocation[0] in {"pytest", "unittest", "nose", "nose2"}:
            return True
    if command in {"npm", "pnpm", "yarn", "bun"}:
        tail, ambiguous = strip_runner_global_options(command, tokens[1:])
        return _test_script(package_runner_script(tail)) or (ambiguous and (yield tail, next_depth, "package"))
    if command in {"uv", "poetry"}:
        tail, ambiguous = strip_runner_global_options(command, tokens[1:])
        if not tail or tail[0] != "run":
            return ambiguous and (yield tail, next_depth, "run")
        tail, ambiguous = strip_runner_run_options(command, tail[1:])
        return (yield tail, next_depth, "command") or (ambiguous and (yield tail[1:], next_depth, "tail"))
    if command == "npx":
        tail, ambiguous = strip_npx_options(tokens[1:])
        return (yield tail, next_depth, "command") or (ambiguous and (yield tail[1:], next_depth, "tail"))
    if command in rt._SHELL_WRAPPERS:
        shell_payload = rt._shell_wrapper_payload(tokens)
        if shell_payload is not None:
            tokens.budget.shell_pending.append(shell_payload)
            return False
    if command == "go":
        return len(tokens) > 1 and Path(tokens[1]).name in {"test", "vet"}
    if command == "cargo":
        return len(tokens) > 1 and Path(tokens[1]).name in {"test", "clippy"}
    if command == "make":
        tail, ambiguous = strip_make_global_options(tokens[1:])
        if ambiguous:
            return (yield tokens[2:], next_depth, "tail")
        return bool(tail and Path(tail[0]).name in {"test", "check", "verify"})
    if command == "ruff" and len(tokens) > 1:
        subcommand = Path(tokens[1]).name
        return subcommand == "check" or (
            subcommand == "format"
            and bool(tokens[2:].skip_while("ruff check", lambda token: Path(token).name != "--check"))
        )
    if command in {"mypy", "pyright", "eslint", "tsc"}:
        return True
    if command == "pre-commit" and len(tokens) > 1 and Path(tokens[1]).name == "run":
        return True
    normalized = head.replace("\\", "/")
    return normalized in {"scripts/verify", "scripts/verify-focused"} or normalized.endswith(
        ("/scripts/verify", "/scripts/verify-focused")
    )


def _is_raw_verification_text(command: str, *, depth: int, budget: RecognitionBudget) -> bool:
    rt = _rt()
    pending = budget.shell_pending
    pending.append(command)
    seen: set[str] = set()
    try:
        while pending:
            text = pending.pop()
            if text in seen:
                continue
            seen.add(text)
            if EVENT_BUDGET.get() is None:
                budget.charge(1 + 5 * len(text))
            text = rt._strip_heredoc_bodies(text)
            try:
                tokens, segments, _ = rt._segmented_command(text)
            except ValueError:
                continue
            if EVENT_BUDGET.get() is None:
                budget.charge(len(tokens))
            if any(_classify_segment(segment, depth, budget) for segment in segments):
                return True
            pending.extend(rt._nested_shell_commands(text))
        return False
    finally:
        pending.clear()


def _recognize(command: object, budget: RecognitionBudget | None = None) -> Recognition:
    if not isinstance(command, str):
        return Recognition.NO
    if len(command) > MAX_COMMAND_CHARS:
        return Recognition.UNDECIDED
    if not command.strip():
        return Recognition.NO
    active = EVENT_BUDGET.get()
    budget = budget if budget is not None else (active if active is not None else RecognitionBudget())
    with budget.in_phase("classification"):
        if command not in budget.verdicts:
            try:
                budget.charge(len(command))
                budget.unreadable |= bool(re.search(r"[\x00\ud800-\udfff]", command))
                yes = _is_raw_verification_text(command, depth=0, budget=budget)
                budget.verdicts[command] = (
                    Recognition.YES if yes else Recognition.UNDECIDED if budget.unreadable else Recognition.NO
                )
            except (RecognitionLimit, UnreadableCommand):
                budget.verdicts[command] = Recognition.UNDECIDED
        verdict = budget.verdicts[command]
    if active is None:
        budget.release()
    return verdict


def is_raw_verification(command: object) -> bool:
    return _recognize(command) is Recognition.YES


def first_verifier_command(command: str) -> str:
    rt = _rt()
    charge_event(len(command))
    try:
        _, segments, _ = rt._segmented_command(command)
    except ValueError:
        return command
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
