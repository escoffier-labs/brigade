"""Tokenization shared by verify execution and trial command identities."""

from __future__ import annotations

import os
import shlex


def _strip_outer_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {'"', "'"}:
        return token[1:-1]
    return token


def split_verify_command(command: str, *, windows: bool | None = None) -> list[str]:
    """Split a raw check command using the execution platform's rules."""
    if windows is None:
        windows = os.name == "nt"
    parts = shlex.split(command, posix=not windows)
    if windows:
        return [_strip_outer_quotes(token) for token in parts]
    return parts
