"""Tests for user-local component install and cache path invariants."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from brigade import component_paths

_VALID_SHA = "a" * 64


def test_unsupported_system_raises_actionable_error():
    env = {"HOME": "/home/alice"}
    with pytest.raises(
        ValueError,
        match="unsupported component path platform system 'freebsd'; supported systems: darwin, linux, windows",
    ):
        component_paths.data_root(env=env, system="freebsd")
    with pytest.raises(
        ValueError,
        match="unsupported component path platform system 'freebsd'; supported systems: darwin, linux, windows",
    ):
        component_paths.cache_root(env=env, system="freebsd")


def test_linux_defaults_use_xdg_paths():
    env = {"HOME": "/home/alice"}
    data = component_paths.data_root(env=env, system="linux")
    cache = component_paths.cache_root(env=env, system="linux")
    assert data == "/home/alice/.local/share"
    assert cache == "/home/alice/.cache"


def test_linux_honors_xdg_overrides():
    env = {"HOME": "/home/alice", "XDG_DATA_HOME": "/data", "XDG_CACHE_HOME": "/cache"}
    assert component_paths.data_root(env=env, system="linux") == "/data"
    assert component_paths.cache_root(env=env, system="linux") == "/cache"


def test_macos_defaults_use_library_paths():
    env = {"HOME": "/Users/alice"}
    data = component_paths.data_root(env=env, system="darwin")
    cache = component_paths.cache_root(env=env, system="darwin")
    assert data == "/Users/alice/Library/Application Support"
    assert cache == "/Users/alice/Library/Caches"


@patch("brigade.component_paths.host_platform.system", return_value="Darwin")
def test_default_detection_uses_platform_system_for_macos(_mock_system):
    env = {"HOME": "/Users/alice"}
    data = component_paths.data_root(env=env)
    cache = component_paths.cache_root(env=env)
    assert data == "/Users/alice/Library/Application Support"
    assert cache == "/Users/alice/Library/Caches"


def test_windows_defaults_use_localappdata():
    env = {"LOCALAPPDATA": r"C:\Users\alice\AppData\Local"}
    data = component_paths.data_root(env=env, system="windows")
    cache = component_paths.cache_root(env=env, system="windows")
    assert data == r"C:\Users\alice\AppData\Local"
    assert cache == r"C:\Users\alice\AppData\Local"


def test_windows_managed_executable_appends_exe_for_drive_path():
    root = r"C:\Users\alice\AppData\Local"
    path = component_paths.managed_executable_path(root, "miseledger")
    assert path == r"C:\Users\alice\AppData\Local\brigade\bin\miseledger.exe"


def test_windows_managed_executable_does_not_double_append_exe():
    root = r"C:\Users\alice\AppData\Local"
    path = component_paths.managed_executable_path(root, "miseledger.exe")
    assert path == r"C:\Users\alice\AppData\Local\brigade\bin\miseledger.exe"


def test_windows_managed_executable_appends_exe_for_unc_root():
    root = r"\\server\share\appdata"
    path = component_paths.managed_executable_path(root, "sessionfind")
    assert path == r"\\server\share\appdata\brigade\bin\sessionfind.exe"


def test_component_paths_never_use_repo_brigade(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    (repo / ".brigade").mkdir()
    env = {"HOME": str(tmp_path / "home"), "XDG_DATA_HOME": str(tmp_path / "xdg-data")}
    data = component_paths.data_root(env=env, system="linux")
    installed = component_paths.installed_state_path(data)
    executable = component_paths.managed_executable_path(data, "miseledger")
    cached = component_paths.cached_asset_path(
        component_paths.cache_root(env=env, system="linux"),
        _VALID_SHA,
        "miseledger-linux-amd64",
    )
    assert ".brigade" not in installed
    assert str(installed).endswith("brigade/installed.json")
    assert str(executable).endswith("brigade/bin/miseledger")
    assert str(cached).endswith(f"brigade/components/{_VALID_SHA}/miseledger-linux-amd64")


@pytest.mark.parametrize(
    ("sha256", "asset_name"),
    [
        ("A" * 64, "miseledger-linux-amd64"),
        ("abc", "miseledger-linux-amd64"),
        (_VALID_SHA, "../miseledger-linux-amd64"),
        (_VALID_SHA, "..\\miseledger-linux-amd64"),
        (_VALID_SHA, "nested/miseledger-linux-amd64"),
        (_VALID_SHA, "nested\\miseledger-linux-amd64"),
    ],
)
def test_cached_asset_path_rejects_unsafe_inputs(sha256, asset_name):
    with pytest.raises(ValueError):
        component_paths.cached_asset_path("/tmp/cache", sha256, asset_name)
