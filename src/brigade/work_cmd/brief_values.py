"""Encoded values and printed lines for the text lines of ``brigade work brief``.

The brief is read by a model at the start of a session. A file name, a branch, a
session title, a task text, an import text or a path that came from the filesystem,
from Git, from configuration or from ingested content can be written by an agent or
by content an agent ingested, so a raw value can forge a line or hide text.

Two layers keep that true:

- `name()` and `text()` escape only the dangerous set: a line break, a NEL, a line
  or paragraph separator, any other C0 or C1 control, an ANSI escape, a bidi
  control, a zero-width or other invisible format character, and a backslash, so a
  reader cannot mistake a real backslash for an escape. Letters, digits, spaces and
  printable punctuation pass, so `next: review the auth module` stays readable. The
  length is capped with a visible `[truncated]` mark.
- `BriefOutput` collects the lines the renderer prints and sanitizes each one on the
  way in, so a value a call site forgot to escape still cannot start a line or hide
  text. It renders into a buffer instead of swapping `sys.stdout`, so two briefs can
  render at once and no wrapper can outlive a render.

A command is different from a value: it is meant to be run. `command_text` shell
quotes each untrusted argument and withholds the command when an argument holds a
dangerous character. `record_command_text` handles a whole command string that came
from a record: it withholds one that holds a shell metacharacter, a Windows
expansion or a control character.
"""

from __future__ import annotations

import re
import shlex
import sys
import unicodedata
from collections.abc import Sequence
from functools import lru_cache
from typing import Any

from ..memory_care_instructions import TRUNCATED, VALUE_MAX, _escape as _escape_char
from ..memory_care_instructions import code  # noqa: F401
from .. import shell_quote

__all__ = [
    "TRUNCATED",
    "VALUE_MAX",
    "WITHHELD",
    "BriefOutput",
    "Trusted",
    "brigade_commands",
    "code",
    "command_text",
    "is_plain",
    "line",
    "name",
    "quote_argument",
    "record_command_text",
    "render_argv",
    "status_entry",
    "text",
    "trusted_words",
]

# Printed in place of a command that cannot be vouched for. The JSON payload still
# carries the real command, so an agent can read it as data.
WITHHELD = "withheld (unsafe characters; see --json)"

# The two-character code `git status --short` writes. It is git's own alphabet,
# never text from the path, so it prints as it is and only the path is escaped.
_GIT_STATUS_CODE = re.compile(r"[ MADRCU?!]{2}")

# A character that can start a line, hide text or reorder it: every control
# (Cc, which covers C0, DEL and C1 including the NEL), every invisible format
# character (Cf: bidi controls, zero width space and joiners, the word joiner, the
# Arabic letter mark, a byte order mark), a surrogate, and the line and paragraph
# separators (Zl, Zp).
_DANGEROUS_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})

# Unicode Default_Ignorable_Code_Point characters that are not Cf: the combining
# grapheme joiner, the Hangul fillers, the Khmer and Mongolian selectors, the Hangul
# filler, the variation selectors, the musical control characters and the variation
# selectors supplement. They are invisible, so they can hide text.
_IGNORABLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFFA0, 0xFFA0),
    (0x1D173, 0x1D17A),
    (0xFFF0, 0xFFF8),
    (0xE0000, 0xE0FFF),
)

# A switch a record command may carry: Brigade's own flags. Anything else that starts
# with `-` is treated as a value and withholds the command.
_RECORD_FLAG = re.compile(r"-{1,2}[A-Za-z0-9][A-Za-z0-9-]*")


def _dangerous(char: str) -> bool:
    if unicodedata.category(char) in _DANGEROUS_CATEGORIES:
        return True
    point = ord(char)
    return any(low <= point <= high for low, high in _IGNORABLE_RANGES)


def _unsafe(char: str) -> bool:
    return char == "\\" or _dangerous(char)


def line(value: str) -> str:
    """`value` with every character that can start a line, hide text or reorder it escaped."""
    if not value:
        return value
    return "".join(_escape_char(char) if _dangerous(char) else char for char in value)


def _sanitize_printed(text: str) -> str:
    """One printed line: at most one trailing newline, and the rest escaped."""
    terminated = text.endswith("\n")
    body = text[:-1] if terminated else text
    return line(body) + ("\n" if terminated else "")


class BriefOutput:
    """Collect the brief's printed lines, each sanitized as it is printed.

    `emit` behaves like `print`, including `file=`, so the renderer can rebind
    `print` to it. Rendering into this buffer leaves `sys.stdout` untouched.
    """

    def __init__(self) -> None:
        self._lines: list[str] = []

    def __call__(self, *args: object, sep: str = " ", end: str = "\n", file: Any = None, flush: bool = False) -> None:
        text = sep.join(str(arg) for arg in args) + end
        if file is not None and file is not sys.stdout:
            file.write(_sanitize_printed(text))
            return
        self._lines.append(_sanitize_printed(text))

    def render(self) -> str:
        return "".join(self._lines)


def _readable(value: str, *, max_len: int = VALUE_MAX) -> str:
    """`value` with only a dangerous character escaped, capped with a visible mark."""
    pieces = ["\\\\" if char == "\\" else _escape_char(char) if _dangerous(char) else char for char in value]
    if sum(len(piece) for piece in pieces) <= max_len:
        return "".join(pieces)
    out: list[str] = []
    used = 0
    for piece in pieces:
        if used + len(piece) > max_len - len(TRUNCATED):
            break
        out.append(piece)
        used += len(piece)
    return "".join(out) + TRUNCATED


def name(raw: object, *, max_len: int = VALUE_MAX) -> str:
    """One untrusted name, id, path, status or code, readable unless it holds a dangerous character."""
    return _readable(str(raw), max_len=max_len)


def text(raw: object, *, max_len: int = VALUE_MAX) -> str:
    """Untrusted free text: the same readable rule, so a sentence stays a sentence."""
    return _readable(str(raw if raw is not None else ""), max_len=max_len)


def status_entry(raw: object) -> str:
    """One ``git status --short`` line: the fixed status code, then the path escaped."""
    item = str(raw)
    code_text, path = item[:2], item[2:]
    if not _GIT_STATUS_CODE.fullmatch(code_text):
        return name(item)
    return f"{code_text} {name(path.lstrip())}".rstrip()


def is_plain(raw: object) -> bool:
    """True when the value holds no dangerous character and no backslash."""
    return not any(_unsafe(char) for char in str(raw if raw is not None else ""))


def _has_dangerous(raw: object) -> bool:
    """True when the value holds a character that can break a line or hide text.

    A command argument is shell quoted, so a backslash is literal there and only the
    dangerous set matters. In display text a backslash is escaped so an escape cannot
    be faked, which is why `is_plain` is stricter.
    """
    return any(_dangerous(char) for char in str(raw if raw is not None else ""))


class Trusted(str):
    """One argv word Brigade wrote itself. It is printed exactly as it is."""


def trusted_words(text: str) -> tuple[Trusted, ...]:
    """A Brigade literal as argv words: `trusted_words("brigade work run")`."""
    return tuple(Trusted(word) for word in text.split())


def _option_like(token: str) -> bool:
    """True when a shell would read the argument as a switch, a splat or an expansion."""
    if token.startswith("-"):
        return True
    if shell_quote.is_windows():
        if token.startswith(("@", "~", "+")):
            return True
        # A cmd switch is one segment (`/h`, `/help`); a path holds more separators.
        if len(token) > 1 and token[0] == "/" and token[1].isalpha() and "/" not in token[1:]:
            return True
    return False


def quote_argument(value: str) -> str:
    """`shell_quote.quote_argument`, re-exported for the brief's callers."""
    return shell_quote.quote_argument(value)


def render_argv(items: Sequence[object]) -> str:
    """Join argv items, quoting each untrusted one for this shell."""
    return " ".join(str(item) if isinstance(item, Trusted) else shell_quote.quote_argument(str(item)) for item in items)


def command_text(*items: object) -> str:
    """A command Brigade composed, as argv: `Trusted` words print as they are.

    Each untrusted item is quoted for this platform's shell, where a space, a quote
    or a shell metacharacter is inert. The whole command is withheld when an item
    holds a character that can break a line or hide text, when this platform cannot
    quote it safely (a cmd or PowerShell metacharacter on Windows), or when a shell
    would read it as a switch, a splat or an expansion.
    """
    for item in items:
        if isinstance(item, Trusted):
            continue
        token = "" if item is None else str(item)
        if not token or _has_dangerous(token) or not shell_quote.is_safe_argument(token) or _option_like(token):
            return WITHHELD
    return render_argv(items)


@lru_cache(maxsize=1)
def brigade_commands() -> frozenset[str]:
    """The top-level command names Brigade registers, with their aliases."""
    from ..cli._index import COMMANDS

    names: set[str] = set()
    for entry in COMMANDS:
        names.add(entry.name)
        names.update(entry.aliases)
    return frozenset(names)


def record_command_text(command: object, *, executables: frozenset[str] = frozenset({"brigade"})) -> str:
    """A command string read from a record, rendered as argv when it is Brigade's own.

    The string is split into argv, and the command is withheld unless it runs an
    allowed executable and, for `brigade`, a top-level command Brigade registers.
    Every remaining item is an untrusted argument, except a switch Brigade's own
    flags match. A Windows string with a quote is withheld: POSIX splitting would
    mangle a quoted Windows path.
    """
    rendered = "" if command is None else str(command)
    if shell_quote.is_windows() and '"' in rendered:
        return WITHHELD
    try:
        argv = shlex.split(rendered)
    except ValueError:
        return WITHHELD
    if not argv or argv[0] not in executables:
        return WITHHELD
    items: list[object] = [Trusted(argv[0])]
    rest = argv[1:]
    if argv[0] == "brigade":
        if not rest or rest[0] not in brigade_commands():
            return WITHHELD
        items.append(Trusted(rest[0]))
        rest = rest[1:]
    for token in rest:
        if _RECORD_FLAG.fullmatch(token):
            items.append(Trusted(token))
            continue
        if not token or _has_dangerous(token) or not shell_quote.is_safe_argument(token) or _option_like(token):
            return WITHHELD
        items.append(token)
    return render_argv(items)
