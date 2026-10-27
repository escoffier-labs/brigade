"""Bounded, read-only discovery of the Codex user-base instruction cap.

This is deliberately not effective-config resolution. Only allowlisted values
and logical source labels leave this module, never paths or parser diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import stat

_CONFIG_READ_MAX_BYTES = 1024 * 1024
_DEFAULT_PROJECT_DOC_MAX_BYTES = 32768


@dataclass(frozen=True)
class UserBaseCap:
    value: int | None
    source: str
    config_label: str
    reason: str | None = None


def _fingerprint(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def read_user_base_cap() -> UserBaseCap:
    """Read a single top-level setting using the doctor's launch environment."""
    custom = os.environ.get("CODEX_HOME", "")
    label = "$CODEX_HOME/config.toml" if custom else "~/.codex/config.toml"

    def unknown(reason: str) -> UserBaseCap:
        return UserBaseCap(None, "unevaluated", label, reason)

    default = UserBaseCap(_DEFAULT_PROJECT_DOC_MAX_BYTES, "default-assumption", label)
    try:
        if custom:
            # resolve() uses process cwd, follows symlinks, and requires existence.
            home = Path(custom).resolve(strict=True)
            if not home.is_dir():
                return unknown("config_home_not_directory")
        else:
            home = Path.home() / ".codex"
        path = home / "config.toml"
        try:
            discovered = path.stat()
        except FileNotFoundError:
            # A dangling config symlink is a present, unevaluated input.
            try:
                path.lstat()
            except FileNotFoundError:
                return default
            return unknown("config_unreadable")
        try:
            import tomllib
        except ImportError:
            # Python 3.10 must not use the permissive compatibility parser here.
            return unknown("config_parser_unavailable")
        if not hasattr(os, "O_NONBLOCK"):
            return unknown("config_nonblocking_unavailable")
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_BINARY", 0)
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode):
                return unknown("config_not_regular")
            if _fingerprint(discovered) != _fingerprint(before):
                return unknown("config_changed_during_read")
            if before.st_size > _CONFIG_READ_MAX_BYTES:
                return unknown("config_read_resource_limit")
            data = bytearray()
            # One extra byte detects overflow even if the initial size was stale.
            while len(data) <= _CONFIG_READ_MAX_BYTES:
                chunk = os.read(fd, min(65536, _CONFIG_READ_MAX_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
            if len(data) > _CONFIG_READ_MAX_BYTES:
                return unknown("config_read_resource_limit")
            if (
                _fingerprint(before) != _fingerprint(os.fstat(fd))
                or _fingerprint(before) != _fingerprint(path.stat())
                or len(data) != before.st_size
            ):
                return unknown("config_changed_during_read")
        finally:
            os.close(fd)
    except (OSError, ValueError, RuntimeError):
        return unknown("config_unreadable")

    try:
        config = tomllib.loads(data.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        return unknown("config_invalid_toml")
    if "project_doc_max_bytes" not in config:
        return default
    value = config["project_doc_max_bytes"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return unknown("config_invalid_project_doc_max_bytes")
    return UserBaseCap(value, "user-base-config", label)
