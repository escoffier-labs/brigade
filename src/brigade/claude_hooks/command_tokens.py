"""Token cursors and a shared work budget for command recognition."""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from enum import Enum, auto
from pathlib import Path
from typing import TypeVar, overload

# Allow several passes over a maximum-length command, including nested
# shell text. Repeated wrapper scans share this same allowance.
MAX_RECOGNITION_STEPS = 2_097_152


class RecognitionLimit(Exception):
    """Stop recognition when the command exceeds its work budget."""


class Recognition(Enum):
    NO = auto()
    YES = auto()
    UNDECIDED = auto()


class RecognitionBudget:
    def __init__(self) -> None:
        self.steps = 0
        self.incomplete = False
        self.exhausted = False
        self.target: Path | None = None
        self.tokens: dict[tuple[str, str], list[str]] = {}
        self.segments: dict[str, tuple[list[str], list[list[str]], list[str]]] = {}
        self.segment_results: dict[tuple[int, int], tuple[list[str], bool]] = {}
        self.heredocs: dict[str, str] = {}
        self.nested: dict[str, list[tuple[int, str]]] = {}
        self.scopes: dict[tuple[str, Path, int], dict[Path, bool]] = {}
        self.candidates: dict[tuple[str, Path, int, bool], list[Path]] = {}
        self.verifier_targets: dict[tuple[str, Path, int], Path | None] = {}
        self.skips: dict[tuple[object, TokenPosition], TokenView] = {}
        self.expansions: dict[TokenPosition, TokenView] = {}

    def charge(self, steps: int) -> None:
        if self.exhausted or self.steps + steps > MAX_RECOGNITION_STEPS:
            self.exhausted = True
            raise RecognitionLimit
        self.steps += steps

    def release(self) -> None:
        self.skips.clear()
        self.expansions.clear()
        self.tokens.clear()
        self.segments.clear()
        self.segment_results.clear()
        self.heredocs.clear()
        self.nested.clear()
        self.scopes.clear()
        self.candidates.clear()
        self.verifier_targets.clear()


EVENT_BUDGET: ContextVar[RecognitionBudget | None] = ContextVar("claude_command_budget", default=None)


def charge_event(steps: int) -> None:
    budget = EVENT_BUDGET.get()
    if budget is not None:
        budget.charge(steps)


TokenPosition = tuple[object, int]


class TokenView:
    """A persistent token suffix, optionally prefixed with split env words."""

    def __init__(
        self,
        tokens: list[str],
        budget: RecognitionBudget,
        start: int = 0,
        *,
        storage: object | None = None,
        tail: TokenView | None = None,
    ) -> None:
        self._tokens = tokens
        self._start = start
        self._storage = object() if storage is None else storage
        self._tail = tail
        self.budget = budget
        self._length = len(tokens) - start + (0 if tail is None else len(tail))

    @property
    def position(self) -> TokenPosition:
        return self._storage, self._start

    def __len__(self) -> int:
        return self._length

    def drop(self, count: int) -> TokenView:
        self.budget.charge(1)
        current = self
        while count >= len(current._tokens) - current._start and current._tail is not None:
            self.budget.charge(1)
            count -= len(current._tokens) - current._start
            current = current._tail
        start = min(current._start + count, len(current._tokens))
        return TokenView(current._tokens, self.budget, start, storage=current._storage, tail=current._tail)

    @overload
    def __getitem__(self, index: int) -> str: ...

    @overload
    def __getitem__(self, index: slice) -> TokenView: ...

    def __getitem__(self, index: int | slice) -> str | TokenView:
        if isinstance(index, slice):
            if index.stop is not None or index.step not in (None, 1):
                raise ValueError("token views require suffix slices")
            return self.drop(index.start or 0)
        current = self if index == 0 else self.drop(index)
        if not 0 <= index < len(self):
            raise IndexError(index)
        token = current._tokens[current._start]
        self.budget.charge(1 + len(token))
        return token

    def __iter__(self) -> Iterator[str]:
        current = self
        while current:
            yield current[0]
            current = current.drop(1)

    def prepend(self, words: list[str]) -> TokenView:
        # Ownership transfers from shlex; the original suffix is shared.
        return TokenView(words, self.budget, tail=self) if words else self

    def skip(self, kind: object, next_position: Callable[[TokenView], TokenView | None]) -> TokenView:
        """Follow a prefix rule once per position and cache its endpoint."""
        cache = self.budget.skips
        visited: list[TokenPosition] = []
        current = self
        while current:
            self.budget.charge(1)
            position = current.position
            cached = cache.get((kind, position))
            if cached is not None:
                current = cached
                break
            visited.append(position)
            next_view = next_position(current)
            if next_view is None:
                break
            current = next_view
        self.budget.charge(len(visited))
        for position in visited:
            cache[kind, position] = current
        return current

    def skip_while(self, kind: str, predicate: Callable[[str], bool]) -> TokenView:
        return self.skip(kind, lambda view: view.drop(1) if predicate(view[0]) else None)


TokenTail = TypeVar("TokenTail", list[str], TokenView)


def split_posix(command: str, *, whitespace: str = " \t\r\n", punctuation: str = "") -> list[str]:
    """Split with shlex's POSIX whitespace and punctuation rules in linear time."""
    tokens: list[str] = []
    index = 0
    while index < len(command):
        char = command[index]
        if char in whitespace:
            index += 1
            continue
        if char in punctuation:
            start = index
            index += 1
            while index < len(command) and command[index] in punctuation:
                index += 1
            tokens.append(command[start:index])
            continue

        word: list[str] = []
        quote = ""
        while index < len(command):
            char = command[index]
            if not quote and (char in whitespace or char in punctuation):
                break
            if char == "\\" and quote != "'":
                index += 1
                if index == len(command):
                    raise ValueError("No escaped character")
                char = command[index]
                if quote and char not in {'"', "\\"}:
                    word.append("\\")
                word.append(char)
            elif quote:
                if char == quote:
                    quote = ""
                else:
                    word.append(char)
            elif char in {"'", '"'}:
                quote = char
            else:
                word.append(char)
            index += 1
        if quote:
            raise ValueError("No closing quotation")
        tokens.append("".join(word))
    return tokens


def _split_env_words(value: str) -> list[str]:
    if os.name != "nt":
        return split_posix(value)

    # Preserve shlex's non-POSIX mode on Windows. Leading quotes are
    # emitted literally and terminate the token at the matching quote.
    words: list[str] = []
    index = 0
    while index < len(value):
        char = value[index]
        if char in " \t\r\n":
            index += 1
            continue
        if char in {"'", '"'}:
            end = value.find(char, index + 1)
            if end < 0:
                raise ValueError("No closing quotation")
            end += 1
        else:
            end = index + 1
            while end < len(value) and value[end] not in " \t\r\n":
                end += 1
        words.append(value[index:end])
        index = end
    return words


def _is_env_assignment(token: str) -> bool:
    if "=" not in token:
        return False
    key = token.split("=", 1)[0]
    return bool(key and (key[0].isalpha() or key[0] == "_") and all(char.isalnum() or char == "_" for char in key))


@overload
def _strip_env(tokens: list[str]) -> list[str]: ...


@overload
def _strip_env(tokens: TokenView) -> TokenView: ...


def _strip_env(tokens: list[str] | TokenView) -> list[str] | TokenView:
    if isinstance(tokens, TokenView):
        return _strip_env_view(tokens)

    active = EVENT_BUDGET.get()
    budget = active if active is not None else RecognitionBudget()
    try:
        if not tokens:
            return tokens
        budget.charge(1 + len(tokens[0]))
        if not _is_env_assignment(tokens[0]) and Path(tokens[0]).name not in {"env", "command"}:
            return tokens
        remaining = _strip_env_view(TokenView(tokens, budget))
        budget.charge(len(remaining))
        return list(remaining)
    except RecognitionLimit:
        if active is not None:
            raise
        return []
    finally:
        if active is None:
            budget.release()


def _strip_env_view(tokens: TokenView) -> TokenView:
    def expand(view: TokenView) -> TokenView | None:
        current = view.skip_while("assignment", _is_env_assignment)
        if not current or Path(current[0]).name != "env":
            return current if current.position != view.position else None
        option = current.drop(1).skip("env option", _next_env_option)
        expanded = _env_split(option)
        if expanded is not None:
            return expanded
        return current if current.position != view.position else None

    # A split invokes the original prefix rules again. An ordinary env
    # operand ends the env phase, even when that operand is itself env.
    tokens = tokens.skip("env expansion", expand)
    if tokens and Path(tokens[0]).name == "env":
        tokens = tokens.drop(1).skip("env option", _next_env_option)
        if tokens and tokens[0] == "--":
            tokens = tokens.drop(1)
        elif tokens and tokens[0].startswith("-"):
            tokens = tokens.drop(len(tokens))
        tokens = tokens.skip_while("assignment", _is_env_assignment)
    if tokens and Path(tokens[0]).name == "command":
        tokens = tokens.drop(1)
        if tokens and tokens[0] == "-p":
            tokens = tokens.drop(1)
        if tokens and tokens[0] == "--":
            return tokens.drop(1)
        if tokens and tokens[0].startswith("-"):
            return tokens.drop(len(tokens))
    return tokens


def _next_env_option(tokens: TokenView) -> TokenView | None:
    option = tokens[0]
    if _is_env_assignment(option) or option in {
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
        return tokens.drop(1)
    if (
        len(option) > 1
        and option.startswith("-")
        and not option.startswith("--")
        and all(flag in {"i", "0", "v"} for flag in option[1:])
    ):
        return tokens.drop(1)
    if option in {"-u", "--unset", "-C", "--chdir", "-a", "--argv0"}:
        return tokens.drop(2)
    if option.startswith(
        ("--unset=", "--chdir=", "--argv0=", "--block-signal=", "--default-signal=", "--ignore-signal=")
    ) or (option.startswith(("-a", "-C", "-u")) and option not in {"-a", "-C", "-u"}):
        return tokens.drop(1)
    return None


def _env_split(tokens: TokenView) -> TokenView | None:
    if not tokens:
        return None
    option = tokens[0]
    if not option.startswith(("-S", "--split-string=")) and option != "--split-string":
        return None
    cached = tokens.budget.expansions.get(tokens.position)
    if cached is not None:
        return cached
    if option in {"-S", "--split-string"}:
        value = tokens[1] if len(tokens) >= 2 else ""
        tail = tokens.drop(2)
    else:
        value = option.split("=", 1)[1] if option.startswith("--split-string=") else option[2:]
        tail = tokens.drop(1)
    try:
        tokens.budget.charge(2 * len(value))
        expanded = tail.prepend(_split_env_words(value))
    except ValueError:
        expanded = tail.drop(len(tail))
    tokens.budget.expansions[tokens.position] = expanded
    return expanded


def _strip_options(
    tokens: TokenTail,
    kind: str,
    no_value: set[str],
    with_value: set[str],
    attached: Callable[[str], bool],
) -> tuple[TokenTail, bool]:
    def advance(option: str) -> int:
        if option in no_value:
            return 1
        if option in with_value:
            return 2
        return int(attached(option))

    if isinstance(tokens, TokenView):

        def next_option(view: TokenView) -> TokenView | None:
            count = advance(view[0])
            return view.drop(count) if 0 < count <= len(view) else None

        tokens = tokens.skip(kind, next_option)
        index = 0
    else:
        index = 0
        while index < len(tokens):
            count = advance(tokens[index])
            if not 0 < count <= len(tokens) - index:
                break
            index += count

    if index == len(tokens):
        return tokens[index:], False
    option = tokens[index]
    if option == "--":
        return tokens[index + 1 :], False
    if option in with_value:
        return tokens[len(tokens) :], True
    return tokens[index:], option.startswith("-")


def _strip_npx_options(tokens: TokenTail) -> tuple[TokenTail, bool]:
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
    return _strip_options(
        tokens,
        "npx options",
        no_value,
        with_value,
        lambda option: option.startswith(("--package=", "--shell=", "--prefix=", "--node-options=")),
    )


def _package_runner_script(tokens: list[str] | TokenView) -> str | None:
    if not tokens:
        return None
    subcommand = Path(tokens[0]).name
    if subcommand == "test":
        return "test"
    if subcommand != "run":
        return None
    no_value = {"--", "-s", "--silent", "--if-present", "--ignore-scripts", "--foreground-scripts"}
    if isinstance(tokens, TokenView):
        remaining = tokens.drop(1).skip_while("package run options", lambda token: Path(token).name in no_value)
        if remaining:
            option = Path(remaining[0]).name
            return None if option.startswith("-") else option
        return None
    for index in range(1, len(tokens)):
        option = Path(tokens[index]).name
        if option not in no_value:
            return None if option.startswith("-") else option
    return None


def _strip_make_global_options(tokens: TokenTail) -> tuple[TokenTail, bool]:
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

    def attached(option: str) -> bool:
        return (
            any(option.startswith(f"{known}=") for known in with_value)
            or (option.startswith("-j") and option[2:].isdigit())
            or (option.startswith(("-C", "-I", "-o", "-W")) and len(option) > 2)
        )

    return _strip_options(tokens, "make options", no_value, with_value, attached)


def _strip_runner_global_options(command: str, tokens: TokenTail) -> tuple[TokenTail, bool]:
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
    return _strip_runner_options(
        tokens, command + " global", no_value.get(command, set()), with_value.get(command, set())
    )


def _strip_runner_run_options(command: str, tokens: TokenTail) -> tuple[TokenTail, bool]:
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
    return _strip_runner_options(tokens, command + " run", no_value.get(command, set()), with_value.get(command, set()))


def _strip_runner_options(
    tokens: TokenTail, kind: str, no_value: set[str], with_value: set[str]
) -> tuple[TokenTail, bool]:
    def attached(option: str) -> bool:
        return any(
            option.startswith(f"{known}=")
            or (
                known.startswith("-")
                and not known.startswith("--")
                and option.startswith(known)
                and len(option) > len(known)
            )
            for known in with_value
        )

    return _strip_options(tokens, kind, no_value, with_value, attached)
