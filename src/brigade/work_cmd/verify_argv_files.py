"""Bind verification reuse to files named by argv, including ignored/external files.

Hash all existing file arguments, even when Git also covers them. This avoids
assuming that containment means coverage (ignored paths and symlink targets do
not necessarily contribute bytes to the Git tree). Reads are capped at 16 MiB
per file. Uncovered directories, other non-regular files, unreadable files and
larger inputs disable reuse. Non-ignored in-tree directories use the tree
fingerprint. Bare program names use PATH and are not file bindings.
This covers direct argv paths, not files loaded transitively by a script.
"""

from __future__ import annotations

import hashlib
import re
import stat
from pathlib import Path
from typing import Any

from ..localio import check_git_ignored

MAX_ARGV_FILE_BYTES = 16 * 1024 * 1024


class _ArgvFileTooLarge(Exception):
    """An argv file exceeds the reuse hashing budget."""


def argv_file_hashes(
    target: Path, commands: list[str | list[str]], *, report_oversized: bool = False
) -> dict[str, str] | None:
    """Return resolved-path SHA-256 bindings, or None when reuse is unsafe."""
    from . import verification

    hashes: dict[str, str] = {}
    try:
        for command in commands:
            # Execution validation belongs to verification's preflight. Binding
            # inputs must not depend on the current PATH or program version.
            argv = list(command) if isinstance(command, list) else verification._verification_split_command(command)
            if isinstance(command, str):
                while argv and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[0]):
                    argv.pop(0)
            if not argv:
                return None
            for index, arg in enumerate(argv):
                if index == 0 and not verification._verify_token_is_path(arg):
                    continue
                path = Path(arg).expanduser()
                if not path.is_absolute():
                    path = target / path
                try:
                    info = path.stat()
                except FileNotFoundError:
                    if index == 0:
                        return None
                    continue
                if stat.S_ISDIR(info.st_mode):
                    if check_git_ignored(target.resolve(), path.resolve()) != "no":
                        return None
                    continue
                if not stat.S_ISREG(info.st_mode):
                    return None
                if info.st_size > MAX_ARGV_FILE_BYTES:
                    raise _ArgvFileTooLarge
                path = path.resolve()
                key = str(path)
                if key not in hashes:
                    with path.open("rb") as stream:
                        data = stream.read(MAX_ARGV_FILE_BYTES + 1)
                    if len(data) > MAX_ARGV_FILE_BYTES:
                        raise _ArgvFileTooLarge
                    hashes[key] = hashlib.sha256(data).hexdigest()
    except _ArgvFileTooLarge:
        if report_oversized:
            from . import verify_runtime

            verify_runtime._print_verify_console(
                f"verification reuse skipped (argv_file_too_large): an argv file exceeds the {MAX_ARGV_FILE_BYTES}-byte hash limit"
            )
        return None
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
        or latest.get("tree_stable") is False
        or latest.get("tree_fingerprint") != fingerprint
        or latest.get("planned_commands") != planned_display
    ):
        return False
    records = latest.get("commands")
    if (
        not isinstance(records, list)
        or not records
        or len(records) != len(planned_display)
        or any(
            not isinstance(record, dict) or record.get("status") != "completed" or record.get("exit_code") != 0
            for record in records
        )
    ):
        return False
    hashes = argv_file_hashes(target, commands, report_oversized=True)
    # Legacy receipts remain eligible only when no file arguments need binding.
    return hashes is not None and latest.get("argv_file_hashes", {}) == hashes
