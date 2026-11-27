"""Render push gate diagnostics and copyable Bash arguments."""

import os
import shlex
import sys


def terminal_text(value: object) -> str:
    return "".join(char if char.isprintable() else ascii(char)[1:-1] for char in str(value))


def shell_arg(value: str) -> str:
    if value == terminal_text(value):
        return shlex.quote(value)
    escaped = "".join(
        chr(byte) if 32 <= byte <= 126 and byte not in (39, 92) else f"\\x{byte:02x}" for byte in os.fsencode(value)
    )
    return "$'" + escaped + "'"


def diagnostic(*lines: object, remedy: str | None = None) -> None:
    for line in lines:
        print(terminal_text(line), file=sys.stderr)
    if remedy:
        for line in remedy.split("\n"):
            print(terminal_text(line), file=sys.stderr)
