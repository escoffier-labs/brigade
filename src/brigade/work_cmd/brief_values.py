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
from typing import Any

from ..memory_care_instructions import TRUNCATED, VALUE_MAX, _SAFE_CHARS, _escape as _escape_char
from ..memory_care_instructions import code  # noqa: F401

__all__ = [
    "TRUNCATED",
    "VALUE_MAX",
    "WITHHELD",
    "BriefOutput",
    "code",
    "command_text",
    "is_plain",
    "line",
    "name",
    "record_command_text",
    "status_entry",
    "text",
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

# A record-supplied command string may hold these: plain characters, spaces and the
# quoting Brigade's own templates use. Anything else withholds it, including a shell
# metacharacter, a Windows expansion (`%`, `^`), a backslash and a control character.
_RECORD_COMMAND_CHARS = _SAFE_CHARS | frozenset(" ,=+:@'\"")


def _dangerous(char: str) -> bool:
    return unicodedata.category(char) in _DANGEROUS_CATEGORIES


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


def record_command_text(command: object) -> str:
    """A command string read from a record, or `WITHHELD` when it holds a metacharacter."""
    rendered = "" if command is None else str(command)
    if all(char in _RECORD_COMMAND_CHARS for char in rendered):
        return rendered
    return WITHHELD


def command_text(command: object, *untrusted: object) -> str:
    """A command Brigade composed, with each untrusted argument shell quoted.

    The command is withheld when an argument holds a character that can break a line
    or hide text. A space, a quote or a shell metacharacter is fine: `shlex.quote`
    puts the argument in single quotes, where none of them is interpreted. An
    argument the producer already quoted is left as it is.
    """
    rendered = "" if command is None else str(command)
    for raw in untrusted:
        token = str(raw if raw is not None else "")
        if not token:
            continue
        if not is_plain(token):
            return WITHHELD
        quoted = shlex.quote(token)
        if quoted == token or quoted in rendered:
            continue
        rendered = rendered.replace(token, quoted)
    return rendered
