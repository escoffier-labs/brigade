"""Bind verification reuse to files named by argv, including ignored/external files.

Hash all existing file arguments, even when Git also covers them. This avoids
assuming that containment means coverage (ignored paths and symlink targets do
not necessarily contribute bytes to the Git tree). Reads are capped at 16 MiB
per file. Non-regular, unreadable or larger inputs disable reuse for that run.
This covers direct argv paths, not files loaded transitively by a script.
"""

from __future__ import annotations

import hashlib
import shutil
import stat
from pathlib import Path
from typing import Any

MAX_ARGV_FILE_BYTES = 16 * 1024 * 1024


def argv_file_hashes(target: Path, commands: list[str | list[str]]) -> dict[str, str] | None:
    """Return resolved-path SHA-256 bindings, or None when reuse is unsafe."""
    from . import verification

    hashes: dict[str, str] = {}
    try:
        for command in commands:
            argv, env, error = (
                verification._verify_parse_argv(command, target)
                if isinstance(command, list)
                else verification._verify_parse_command(command, target)
            )
            if error or argv is None:
                return None
            for index, arg in enumerate(argv):
                if index == 0 and not verification._verify_token_is_path(arg):
                    resolved = shutil.which(arg, path=env.get("PATH"))
                    if resolved is None:
                        return None
                    arg = resolved
                path = Path(arg).expanduser()
                if not path.is_absolute():
                    path = target / path
                try:
                    info = path.stat()
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARGV_FILE_BYTES:
                    return None
                path = path.resolve()
                key = str(path)
                if key not in hashes:
                    with path.open("rb") as stream:
                        data = stream.read(MAX_ARGV_FILE_BYTES + 1)
                    if len(data) > MAX_ARGV_FILE_BYTES:
                        return None
                    hashes[key] = hashlib.sha256(data).hexdigest()
    except (OSError, ValueError, RuntimeError):
        return None
    return hashes


def reusable_receipt(
    target: Path,
    commands: list[str | list[str]],
    planned_display: list[str],
    fingerprint: str | None,
    latest: dict[str, Any] | None,
) -> bool:
    if (
        fingerprint is None
        or latest is None
        or latest.get("status") != "completed"
        or latest.get("tree_fingerprint") != fingerprint
        or latest.get("planned_commands") != planned_display
    ):
        return False
    hashes = argv_file_hashes(target, commands)
    # Legacy receipts remain eligible only when no file arguments need binding.
    return hashes is not None and latest.get("argv_file_hashes", {}) == hashes
