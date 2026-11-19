"""Quote one command argument for the shell of the platform that will run it.

`shlex.quote` writes POSIX single quotes, and a single-quoted string quotes nothing in
cmd.exe or PowerShell. A command the brief prints for a Windows operator therefore has
to use the native form. A plain argument is returned unchanged, so a Windows path with
no space prints exactly as it did before this helper existed.
"""

from __future__ import annotations

import shlex
import string
import subprocess
import sys

__all__ = ["is_windows", "quote_argument"]

# A character that needs no quoting in cmd.exe or PowerShell. An argument made only
# of these is returned as it is, a drive colon and a path separator included.
_PLAIN = frozenset(string.ascii_letters + string.digits + "_./\\:-~+=@")


def is_windows() -> bool:
    """True on Windows. A test replaces this to check both renderings on one host."""
    return sys.platform == "win32"


def quote_argument(value: str) -> str:
    """`value` quoted for this platform's shell, or unchanged when it needs no quoting."""
    if not value:
        return value
    if is_windows():
        if all(char in _PLAIN for char in value):
            return value
        return subprocess.list2cmdline([value])
    return shlex.quote(value)
