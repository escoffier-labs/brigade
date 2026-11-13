"""Shared preview/apply filesystem guards for connector registries."""

from __future__ import annotations
import json
import os
import stat
import re
from pathlib import Path
from brigade import grokbot_ops, grokbot_mcp

VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class PackError(ValueError):
    """A rejected pack registry or lifecycle request."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _owned_unit_dir(unit_dir: Path, instance: str) -> bool:
    if unit_dir.is_symlink() or not unit_dir.is_dir():
        return False
    unit = unit_dir / grokbot_ops.unit_name(instance)
    return unit.is_file() and not unit.is_symlink()


def _assert_removable(path: Path, *, config_file: bool) -> None:
    if grokbot_ops._path_is_symlink(path) or path.is_symlink():
        raise PackError("symlink")
    try:
        info = path.lstat()
    except OSError as exc:
        raise PackError("unsafe-path") from exc
    if not stat.S_ISREG(info.st_mode):
        raise PackError("unsafe-path")
    if config_file and stat.S_IMODE(info.st_mode) & 0o077:
        raise PackError("unsafe-permissions")
    parent = -1
    try:
        parent = grokbot_ops._open_parent_nofollow(path, create=False)
    except OSError as exc:
        raise PackError("unsafe-path") from exc
    finally:
        if parent != -1:
            os.close(parent)


def _exists_or_link(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _assert_config_destination_safe(path: Path) -> None:
    if grokbot_ops._path_is_symlink(path) or path.is_symlink():
        raise PackError("unsafe-path")
    parent = -1
    try:
        parent = grokbot_ops._open_parent_nofollow(path, create=False)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PackError("unsafe-path") from exc
    finally:
        if parent != -1:
            os.close(parent)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise PackError("unsafe-path") from exc
    if not stat.S_ISREG(info.st_mode):
        raise PackError("unsafe-path")


def _snapshot_regular_file(path: Path) -> tuple[str, int] | None:
    if grokbot_ops._path_is_symlink(path) or path.is_symlink():
        raise PackError("unsafe-path")
    try:
        text = grokbot_ops._read_regular_text(path)
        mode = stat.S_IMODE(path.lstat().st_mode)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PackError("unsafe-path") from exc
    return text, mode


def _restore_regular_file(path: Path, snapshot: tuple[str, int] | None) -> None:
    if snapshot is None:
        if grokbot_ops._path_is_symlink(path) or path.is_symlink():
            raise PackError("unsafe-path")
        try:
            grokbot_ops.remove_regular_file(path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise PackError("unsafe-path") from exc
        return
    if _matches_regular_file_snapshot(path, snapshot):
        return
    text, mode = snapshot
    try:
        grokbot_ops._write_text_nofollow_atomic(path, text, mode=mode)
    except OSError as exc:
        raise PackError("unsafe-path") from exc


def _matches_regular_file_snapshot(path: Path, snapshot: tuple[str, int] | None) -> bool:
    """Return True only when existence, exact contents, and mode match the snapshot."""
    try:
        current = _snapshot_regular_file(path)
    except PackError:
        return False
    if current is None or snapshot is None:
        return current is None and snapshot is None
    current_text, current_mode = current
    expected_text, expected_mode = snapshot
    return current_text.encode("utf-8") == expected_text.encode("utf-8") and current_mode == expected_mode


def _bind_port_from_existing_config(path: Path) -> int | None:
    """Read one bind port from a first-party config without following unsafe paths."""
    if grokbot_ops._path_is_symlink(path) or path.is_symlink():
        raise PackError("unsafe-path")
    try:
        raw = grokbot_ops._read_regular_text(path)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise PackError("unsafe-path") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PackError("unsafe-path") from exc
    if not isinstance(payload, dict):
        raise PackError("unsafe-path")
    bind = payload.get("bind")
    if not isinstance(bind, str):
        raise PackError("unsafe-path")
    _host, port = _parse_default_bind(bind)
    return port


def _parse_version(value: str) -> tuple[int, int, int]:
    if not VERSION_RE.fullmatch(value):
        raise PackError("invalid-version")
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _parse_default_bind(value: str) -> tuple[str, int]:
    try:
        return grokbot_mcp.parse_bind(value)
    except grokbot_mcp.ConfigurationError:
        raise PackError("unsafe-bind") from None
