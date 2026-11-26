"""Token cursors and a shared work budget for command recognition."""

from __future__ import annotations

import os
import shlex
from collections.abc import Iterator
from pathlib import Path
from typing import TypeVar, overload

# Allow several passes over a maximum-length command, including nested
# shell text. Repeated wrapper scans share this same allowance.
MAX_RECOGNITION_STEPS = 2_097_152


class RecognitionLimit(Exception):
    """Stop recognition when the command exceeds its work budget."""


class RecognitionBudget:
    def __init__(self) -> None:
        self.steps = 0

    def charge(self, steps: int) -> None:
        if self.steps + steps > MAX_RECOGNITION_STEPS:
            raise RecognitionLimit
        self.steps += steps


class TokenView:
    """A contiguous token span whose suffixes share storage and a budget."""

    def __init__(self, tokens: list[str], budget: RecognitionBudget, start: int = 0, end: int | None = None) -> None:
        self._tokens = tokens
        self._start = start
        self._end = len(tokens) if end is None else end
        self.budget = budget

    def __len__(self) -> int:
        return self._end - self._start

    @overload
    def __getitem__(self, index: int) -> str: ...

    @overload
    def __getitem__(self, index: slice) -> TokenView: ...

    def __getitem__(self, index: int | slice) -> str | TokenView:
        if isinstance(index, slice):
            start, end, step = index.indices(len(self))
            if step != 1:
                raise ValueError("token views require contiguous slices")
            return TokenView(self._tokens, self.budget, self._start + start, self._start + max(start, end))
        if not 0 <= index < len(self):
            raise IndexError(index)
        token = self._tokens[self._start + index]
        # Token operations such as assignment checks and shlex splitting can
        # also scale with the length of a single word.
        self.budget.charge(1 + len(token))
        return token

    def __iter__(self) -> Iterator[str]:
        for index in range(len(self)):
            yield self[index]


TokenTail = TypeVar("TokenTail", list[str], TokenView)


def _is_env_assignment(token: str) -> bool:
    if "=" not in token:
        return False
    key = token.split("=", 1)[0]
    return bool(key and (key[0].isalpha() or key[0] == "_") and all(char.isalnum() or char == "_" for char in key))


def _after_assignments(tokens: list[str] | TokenView, index: int) -> int:
    while index < len(tokens) and _is_env_assignment(tokens[index]):
        index += 1
    return index


@overload
def _strip_env(tokens: list[str]) -> list[str]: ...


@overload
def _strip_env(tokens: TokenView) -> TokenView: ...


def _strip_env(tokens: list[str] | TokenView) -> list[str] | TokenView:
    while True:
        index = _after_assignments(tokens, 0)
        if index == len(tokens) or Path(tokens[index]).name != "env":
            break

        index, split = _env_tail(tokens, index + 1)
        if split is None:
            index = _after_assignments(tokens, index)
            break
        if isinstance(tokens, TokenView):
            tokens = TokenView([*split, *tokens[index:]], tokens.budget)
        else:
            tokens = [*split, *tokens[index:]]

    if index < len(tokens) and Path(tokens[index]).name == "command":
        index += 1
        if index < len(tokens) and tokens[index] == "-p":
            index += 1
        if index < len(tokens) and tokens[index] == "--":
            index += 1
        elif index < len(tokens) and tokens[index].startswith("-"):
            index = len(tokens)
    return tokens[index:]


def _env_tail(tokens: list[str] | TokenView, index: int) -> tuple[int, list[str] | None]:
    while index < len(tokens):
        option = tokens[index]
        if option == "--":
            return index + 1, None
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
            index += 1
            continue
        if (
            len(option) > 1
            and option.startswith("-")
            and not option.startswith("--")
            and all(flag in {"i", "0", "v"} for flag in option[1:])
        ):
            index += 1
            continue
        if option in {"-u", "--unset", "-C", "--chdir", "-a", "--argv0"}:
            index += 2
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
            index += 1
            continue
        if option in {"-S", "--split-string"}:
            if index + 1 >= len(tokens):
                return len(tokens), None
            return _split_env(tokens[index + 1], index + 2, len(tokens))
        if option.startswith(("--split-string=", "-S")):
            value = option.split("=", 1)[1] if option.startswith("--split-string=") else option[2:]
            return _split_env(value, index + 1, len(tokens))
        return (len(tokens) if option.startswith("-") else index), None
    return len(tokens), None


def _split_env(value: str, index: int, end: int) -> tuple[int, list[str] | None]:
    try:
        return index, shlex.split(value, posix=os.name != "nt")
    except ValueError:
        return end, None


def _strip_npx_options(tokens: TokenTail) -> tuple[TokenTail, bool]:
    index = 0
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
    while index < len(tokens):
        option = tokens[index]
        if option == "--":
            index += 1
            break
        if option in no_value:
            index += 1
            continue
        if option in with_value:
            if index + 1 >= len(tokens):
                return tokens[len(tokens) :], True
            index += 2
            continue
        if option.startswith(("--package=", "--shell=", "--prefix=", "--node-options=")):
            index += 1
            continue
        if option.startswith("-"):
            return tokens[index:], True
        break
    return tokens[index:], False


def _package_runner_script(tokens: list[str] | TokenView) -> str | None:
    if not tokens:
        return None
    subcommand = Path(tokens[0]).name
    if subcommand == "test":
        return "test"
    if subcommand != "run":
        return None
    no_value = {"--", "-s", "--silent", "--if-present", "--ignore-scripts", "--foreground-scripts"}
    for index in range(1, len(tokens)):
        option = Path(tokens[index]).name
        if option not in no_value:
            return None if option.startswith("-") else option
    return None


def _strip_make_global_options(tokens: TokenTail) -> tuple[TokenTail, bool]:
    index = 0
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
    while index < len(tokens):
        option = tokens[index]
        if option == "--":
            index += 1
            break
        if option in no_value:
            index += 1
            continue
        if option in with_value:
            if index + 1 >= len(tokens):
                return tokens[len(tokens) :], True
            index += 2
            continue
        if any(option.startswith(f"{known}=") for known in with_value):
            index += 1
            continue
        if option.startswith("-j") and len(option) > 2 and option[2:].isdigit():
            index += 1
            continue
        if option.startswith(("-C", "-I", "-o", "-W")) and len(option) > 2:
            index += 1
            continue
        if option.startswith("-"):
            return tokens[index:], True
        break
    return tokens[index:], False


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
    index = 0
    command_no_value = no_value.get(command, set())
    command_with_value = with_value.get(command, set())
    while index < len(tokens):
        option = tokens[index]
        if option == "--":
            index += 1
            break
        if option in command_no_value:
            index += 1
            continue
        if option in command_with_value:
            if index + 1 >= len(tokens):
                return tokens[len(tokens) :], True
            index += 2
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
            index += 1
            continue
        if option.startswith("-"):
            return tokens[index:], True
        break
    return tokens[index:], False


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
    index = 0
    command_no_value = no_value.get(command, set())
    command_with_value = with_value.get(command, set())
    while index < len(tokens):
        option = tokens[index]
        if option == "--":
            index += 1
            break
        if option in command_no_value:
            index += 1
            continue
        if option in command_with_value:
            if index + 1 >= len(tokens):
                return tokens[len(tokens) :], True
            index += 2
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
            index += 1
            continue
        if option.startswith("-"):
            return tokens[index:], True
        break
    return tokens[index:], False
