"""Encoded values for the text lines of ``brigade work brief``.

The brief is read by a model at the start of a session. A file name, a branch, a
session title, a task text, an import text or a path that came from the filesystem,
from Git, from configuration or from ingested content can be written by an agent or
by content an agent ingested, so printable is not safe: such a value may read as an
instruction or forge a second line.

Two layers keep that true:

- Every such value goes through the encoder #2550 added for the instruction-budget
  lines, `memory_care_instructions.encode_value`: only ASCII letters, digits and
  `. _ - / ~` pass, every other character is an escape (`\\x20`, `\\u202e`), the length
  is capped with a visible `[truncated]` mark, and the line keeps the fixed prefix it
  was written with. A plain path or identifier renders unchanged.
- Every line the renderer prints passes through `sanitized_stdout` on its way out.
  That escapes a line break, a NEL, a line or paragraph separator, any other C0 or
  C1 control, an ANSI escape, a bidi override and a zero-width character wherever it
  appears, so a value a call site forgot to encode still cannot start a line or hide
  text.

A command is different from a value: it is meant to be run. `command_text` prints it
only while every untrusted argument is plain enough that `shlex.quote` leaves it
alone; otherwise it prints `WITHHELD` and the JSON payload still carries the real
command. That keeps a name that reads as an instruction out of a shell and out of a
paste.
"""

from __future__ import annotations

import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from ..memory_care_instructions import TRUNCATED, VALUE_MAX, _SAFE_CHARS, _escape as _escape_char
from ..memory_care_instructions import code, encode_value  # noqa: F401

__all__ = [
    "TRUNCATED",
    "VALUE_MAX",
    "WITHHELD",
    "code",
    "command_text",
    "is_command_plain",
    "is_command_shaped",
    "is_plain",
    "line",
    "name",
    "sanitized_stdout",
    "status_entry",
    "text",
]

# Printed in place of a command whose untrusted argument is not plain. The JSON
# payload still carries the real command, so an agent can read it as data.
WITHHELD = "withheld (unsafe characters; see --json)"

# The two-character code `git status --short` writes. It is git's own alphabet,
# never text from the path, so it prints as it is and only the path is encoded.
_GIT_STATUS_CODE = re.compile(r"[ MADRCU?!]{2}")

# Characters that can break a line, hide text or reorder it even though they are not
# a control code: zero width space/joiners, the bidi marks and overrides, the bidi
# isolates, the word joiner and the byte order mark.
_INVISIBLE = frozenset(
    {
        0x180E,
        0x200B,
        0x200C,
        0x200D,
        0x200E,
        0x200F,
        0x2028,
        0x2029,
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2060,
        0x2061,
        0x2062,
        0x2063,
        0x2064,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
        0xFEFF,
    }
)


def _dangerous(char: str) -> bool:
    point = ord(char)
    return point < 0x20 or 0x7F <= point <= 0x9F or point in _INVISIBLE


def line(value: str) -> str:
    """`value` with every character that can start a line, hide text or reorder it escaped."""
    if not value:
        return value
    return "".join(_escape_char(char) if _dangerous(char) else char for char in value)


class _LineSafeWriter:
    """Escape one printed line before it reaches the stream.

    `print` writes a whole line in one call, so a call's text is one line and at
    most one trailing newline ends it. Anything else inside it, including a newline
    a value smuggled in, is escaped: a line of the brief can only come from a print
    call, never from a value.
    """

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def write(self, text: str) -> int:
        terminated = text.endswith("\n")
        body = text[:-1] if terminated else text
        self._stream.write(line(body) + ("\n" if terminated else ""))
        return len(text)

    def flush(self) -> None:
        flush = getattr(self._stream, "flush", None)
        if flush is not None:
            flush()

    def __getattr__(self, item: str) -> Any:
        return getattr(self._stream, item)


@contextmanager
def sanitized_stdout() -> Iterator[None]:
    """Send every line printed inside the block through `line`."""
    original = sys.stdout
    sys.stdout = _LineSafeWriter(original)  # type: ignore[assignment]
    try:
        yield
    finally:
        sys.stdout = original


def name(raw: object, *, max_len: int = VALUE_MAX) -> str:
    """One untrusted name, id, path, status or code as a line-safe token.

    ``None`` keeps printing as ``None``, which is a fixed word and not a name.
    """
    return encode_value(str(raw), max_len=max_len)


def text(raw: object, *, max_len: int = VALUE_MAX) -> str:
    """Untrusted free text: collapsed onto one line, then encoded like a name."""
    return name(" ".join(str(raw if raw is not None else "").split()), max_len=max_len)


def status_entry(raw: object) -> str:
    """One ``git status --short`` line: the fixed status code, then the path encoded."""
    item = str(raw)
    code_text, path = item[:2], item[2:]
    if not _GIT_STATUS_CODE.fullmatch(code_text):
        return name(item)
    return f"{code_text} {name(path.lstrip())}".rstrip()


def is_plain(raw: object) -> bool:
    """True when the encoder would leave this value unchanged, so it holds no escape."""
    value = "" if raw is None else str(raw)
    return encode_value(value, max_len=max(len(value), 1)) == value


# A command token also tolerates the two characters a resolved path carries: the
# drive colon and the Windows separator. A command is printed, not decoded, so they
# carry no escape meaning there, and the encoder's own rule would withhold every
# command on Windows.
_COMMAND_SAFE = _SAFE_CHARS | frozenset("\\:")


def is_command_plain(raw: object) -> bool:
    """True when an untrusted token can sit inside a printed command unchanged."""
    return all(char in _COMMAND_SAFE for char in str(raw if raw is not None else ""))


# A printed command may hold these beyond a plain token: the quoting, separator and
# expansion-free punctuation Brigade's own command templates use. Anything else
# withholds the command, including a shell metacharacter such as a semicolon, a pipe,
# an ampersand, a dollar sign, a backtick, a parenthesis, an angle bracket, a brace,
# a bracket, a glob or a hash, and every control character.
_COMMAND_CHARS = _COMMAND_SAFE | frozenset(" ,=+@%^'\"")


def is_command_shaped(raw: object) -> bool:
    """True when a command string holds no shell metacharacter and no control character."""
    return all(char in _COMMAND_CHARS for char in str(raw if raw is not None else ""))


def command_text(command: object, *untrusted: object) -> str:
    """A command Brigade composed, or `WITHHELD` when it cannot be vouched for.

    `shlex.quote` can rewrite a token (`'` becomes `'"'"'`), so a quoted command
    cannot be repaired in place and a shell-quoted sentence is still a sentence to a
    model. A command with no untrusted argument prints as it is. A command string
    that came from a record is withheld when it holds a shell metacharacter, and a
    command with an untrusted argument is withheld when that argument is not plain.
    """
    rendered = "" if command is None else str(command)
    if not is_command_shaped(rendered):
        return WITHHELD
    if any(str(raw if raw is not None else "").strip() and not is_command_plain(raw) for raw in untrusted):
        return WITHHELD
    return rendered
