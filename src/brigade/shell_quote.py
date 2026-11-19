"""Quote one command argument for the shell of the platform that will run it.

`shlex.quote` writes POSIX single quotes, and a single-quoted string quotes nothing in
cmd.exe or PowerShell. There is no one quoting that is safe for both Windows shells:
`subprocess.list2cmdline` only handles whitespace and quotes, so `ok&whoami` or
`$(whoami)` would stay live. So on Windows an argument is printed only when it is made
of plain path characters, with or without spaces, and anything else withholds the
whole command. A plain argument is returned unchanged, so a Windows path with no space
prints exactly as it did before this helper existed.
"""

from __future__ import annotations

import shlex
import string
import sys

__all__ = ["is_safe_argument", "is_windows", "quote_argument"]

# A character that needs no quoting in cmd.exe or PowerShell: the letters, the digits
# and the path characters, a drive colon and a Windows separator included.
_PLAIN = frozenset(string.ascii_letters + string.digits + "_./\\:-~+=@")


def is_windows() -> bool:
    """True on Windows. A test replaces this to check both renderings on one host."""
    return sys.platform == "win32"


def is_safe_argument(value: str) -> bool:
    """True when this platform's quoting can make the argument inert.

    On POSIX `shlex.quote` handles anything. On Windows only plain characters and
    spaces can be quoted safely: `&`, `|`, `<`, `>`, `^`, `%`, `$`, a backtick, a
    parenthesis, a semicolon, a comma, a quote, `!` and every non-ASCII character
    either break the command or stay interpreted inside double quotes.
    """
    if not is_windows():
        return True
    return all(char in _PLAIN or char == " " for char in value)


def quote_argument(value: str) -> str:
    """`value` quoted for this platform's shell.

    Check `is_safe_argument` first: an argument it rejects cannot be quoted safely and
    the caller withholds the command instead of printing a broken one.
    """
    if not value:
        return value
    if is_windows():
        if all(char in _PLAIN for char in value):
            return value
        return f'"{value}"'
    return shlex.quote(value)
