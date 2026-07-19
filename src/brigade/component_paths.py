"""User-local install and cache path invariants for native components."""

from __future__ import annotations

import os
import platform as host_platform
import re
from collections.abc import Mapping
from pathlib import PurePosixPath, PureWindowsPath

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def data_root(*, env: Mapping[str, str] | None = None, system: str | None = None) -> str:
    """Return the OS-specific user data root without admin rights."""
    environment = env if env is not None else os.environ
    os_name = _normalize_system(system)
    if os_name == "windows":
        localappdata = environment.get("LOCALAPPDATA")
        if not localappdata:
            raise ValueError("windows component data root requires LOCALAPPDATA")
        return _normalize(localappdata, windows=True)
    home = environment.get("HOME")
    if not home:
        raise ValueError("component data root requires HOME")
    if os_name == "darwin":
        return _join(home, "Library", "Application Support", windows=False)
    data_home = environment.get("XDG_DATA_HOME")
    return _normalize(data_home, windows=False) if data_home else _join(home, ".local", "share", windows=False)


def cache_root(*, env: Mapping[str, str] | None = None, system: str | None = None) -> str:
    """Return the OS-specific user cache root without admin rights."""
    environment = env if env is not None else os.environ
    os_name = _normalize_system(system)
    if os_name == "windows":
        localappdata = environment.get("LOCALAPPDATA")
        if not localappdata:
            raise ValueError("windows component cache root requires LOCALAPPDATA")
        return _normalize(localappdata, windows=True)
    home = environment.get("HOME")
    if not home:
        raise ValueError("component cache root requires HOME")
    if os_name == "darwin":
        return _join(home, "Library", "Caches", windows=False)
    cache_home = environment.get("XDG_CACHE_HOME")
    return _normalize(cache_home, windows=False) if cache_home else _join(home, ".cache", windows=False)


def components_dir(data_root_path: str) -> str:
    return _join(data_root_path, "brigade", "components", windows=_is_windows_path(data_root_path))


def managed_executable_path(data_root_path: str, executable: str) -> str:
    windows = _is_windows_path(data_root_path)
    name = executable
    if windows and not executable.lower().endswith(".exe"):
        name = f"{executable}.exe"
    return _join(
        data_root_path,
        "brigade",
        "bin",
        name,
        windows=windows,
    )


def installed_state_path(data_root_path: str) -> str:
    return _join(data_root_path, "brigade", "installed.json", windows=_is_windows_path(data_root_path))


def installed_previous_state_path(data_root_path: str) -> str:
    return _join(data_root_path, "brigade", "installed.previous.json", windows=_is_windows_path(data_root_path))


def cached_asset_path(cache_root_path: str, sha256: str, asset_name: str) -> str:
    if not _SHA256.fullmatch(sha256):
        raise ValueError("sha256 must be 64 lowercase hex characters")
    if not _safe_basename(asset_name):
        raise ValueError("asset_name must be a safe basename")
    return _join(
        cache_root_path,
        "brigade",
        "components",
        sha256,
        asset_name,
        windows=_is_windows_path(cache_root_path),
    )


def _normalize_system(system: str | None) -> str:
    raw = (system or host_platform.system()).lower()
    if raw in {"darwin", "macos", "mac os x"}:
        return "darwin"
    if raw in {"windows", "nt"}:
        return "windows"
    if raw == "linux":
        return "linux"
    raise ValueError(f"unsupported component path platform system {raw!r}; supported systems: darwin, linux, windows")


def _is_windows_path(path: str) -> bool:
    return "\\" in path or (len(path) > 1 and path[1] == ":")


def _safe_basename(name: str) -> bool:
    if "/" in name or "\\" in name or name in {".", ".."}:
        return False
    return PurePosixPath(name).name == name and PureWindowsPath(name).name == name


def _join(*parts: str, windows: bool) -> str:
    if windows:
        return str(PureWindowsPath(*parts))
    return str(PurePosixPath(*parts))


def _normalize(path: str, *, windows: bool) -> str:
    if windows:
        return str(PureWindowsPath(path))
    return str(PurePosixPath(path))
