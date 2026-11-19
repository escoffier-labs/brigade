"""Encoded values for the text lines of ``brigade work brief``.

The brief is read by a model at the start of a session. A file name, a branch, a
session title, a task text, an import text or a path that came from the filesystem,
from Git, from configuration or from ingested content can be written by an agent or
by content an agent ingested, so printable is not safe: such a value may read as an
instruction or forge a second line.

Every such value goes through the encoder #2550 added for the instruction-budget
lines, `memory_care_instructions.encode_value`: only ASCII letters, digits and
`. _ - / ~` pass, every other character is an escape (`\\x20`, `\\u202e`), the length
is capped with a visible `[truncated]` mark, and the line keeps the fixed prefix it
was written with. A plain path or identifier renders unchanged.

A value Brigade itself composed at render time (a fixed status, a health detail, a
command with no untrusted argument) is not encoded: it is fixed text from this
repository. A name such a value embeds is encoded where it is read.
"""

from __future__ import annotations

import re

from ..memory_care_instructions import TRUNCATED, VALUE_MAX, encode_value  # noqa: F401

__all__ = ["TRUNCATED", "VALUE_MAX", "encoded_command", "name", "status_entry", "text"]

# The two-character code `git status --short` writes. It is git's own alphabet,
# never text from the path, so it prints as it is and only the path is encoded.
_GIT_STATUS_CODE = re.compile(r"[ MADRCU?!]{2}")


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
    code, path = item[:2], item[2:]
    if not _GIT_STATUS_CODE.fullmatch(code):
        return name(item)
    return f"{code} {name(path.lstrip())}".rstrip()


def encoded_command(command: object, *untrusted: object) -> str:
    """A command Brigade composed, with each untrusted argument encoded where it appears.

    The command's own words stay readable and copyable; the argument is data, so a
    name that reads as a shell command or as an instruction never appears as one.
    An argument is appended after the command's own words, so its last occurrence
    is the one replaced: a task text of ``run`` never rewrites the verb.
    """
    rendered = "" if command is None else str(command)
    for raw in untrusted:
        token = str(raw if raw is not None else "").strip()
        if token:
            head, found, tail = rendered.rpartition(token)
            if found:
                rendered = f"{head}{name(token)}{tail}"
    return rendered
