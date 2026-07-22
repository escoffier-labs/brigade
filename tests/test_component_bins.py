"""Resolution order for managed engine executables."""

from __future__ import annotations

import json
import stat
from pathlib import Path

from brigade import component_bins

_SHA = "0" * 64


def _write_executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _write_installed_state(data_home: Path, components: dict[str, Path]) -> None:
    state_dir = data_home / "brigade"
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "brigade_version": "0.25.0",
        "manifest_revision": "v0.25.0+test",
        "platform": "linux-amd64",
        "installed_at": "2026-07-21T00:00:00+00:00",
        "components": {
            name: {
                "component_revision": "test",
                "asset_name": f"{name}-linux-amd64",
                "byte_size": 1,
                "sha256": _SHA,
                "download_url": f"https://example.invalid/{name}",
                "executable": str(path),
            }
            for name, path in components.items()
        },
    }
    (state_dir / "installed.json").write_text(json.dumps(payload))


def _env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"HOME": str(tmp_path), "XDG_DATA_HOME": str(tmp_path / "data"), **extra}


def test_managed_path_returns_installed_executable(tmp_path):
    binary = _write_executable(tmp_path / "managed" / "graphtrail")
    _write_installed_state(tmp_path / "data", {"graphtrail": binary})
    assert component_bins.managed_path("graphtrail", env=_env(tmp_path)) == str(binary)


def test_managed_path_requires_binary_on_disk(tmp_path):
    _write_installed_state(tmp_path / "data", {"graphtrail": tmp_path / "missing" / "graphtrail"})
    assert component_bins.managed_path("graphtrail", env=_env(tmp_path)) is None


def test_managed_path_without_state_file(tmp_path):
    assert component_bins.managed_path("graphtrail", env=_env(tmp_path)) is None


def test_resolve_prefers_env_override(tmp_path, monkeypatch):
    override = _write_executable(tmp_path / "override" / "graphtrail")
    managed = _write_executable(tmp_path / "managed" / "graphtrail")
    _write_installed_state(tmp_path / "data", {"graphtrail": managed})
    env = _env(tmp_path, GRAPHTRAIL_BIN=str(override))
    assert component_bins.resolve("graphtrail", env=env) == str(override)


def test_resolve_broken_override_does_not_fall_through(tmp_path, monkeypatch):
    managed = _write_executable(tmp_path / "managed" / "graphtrail")
    _write_installed_state(tmp_path / "data", {"graphtrail": managed})
    monkeypatch.setenv("PATH", str(managed.parent))
    env = _env(tmp_path, GRAPHTRAIL_BIN=str(tmp_path / "nope"))
    assert component_bins.resolve("graphtrail", env=env) is None


def test_resolve_prefers_managed_over_path(tmp_path, monkeypatch):
    managed = _write_executable(tmp_path / "managed" / "graphtrail")
    on_path = _write_executable(tmp_path / "pathdir" / "graphtrail")
    _write_installed_state(tmp_path / "data", {"graphtrail": managed})
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    env = _env(tmp_path, PATH=str(on_path.parent))
    assert component_bins.resolve("graphtrail", env=env) == str(managed)


def test_resolve_falls_back_to_supplied_path(tmp_path, monkeypatch):
    on_path = _write_executable(tmp_path / "pathdir" / "graphtrail")
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    env = _env(tmp_path, PATH=str(on_path.parent))
    assert component_bins.resolve("graphtrail", env=env) == str(on_path)


def test_resolve_falls_back_to_legacy_location(tmp_path, monkeypatch):
    legacy = _write_executable(tmp_path / ".cargo" / "bin" / "graphtrail")
    monkeypatch.setenv("HOME", str(tmp_path / "host-home"))
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    env = _env(tmp_path, PATH=str(tmp_path / "empty"))
    assert component_bins.resolve("graphtrail", env=env) == str(legacy)


def test_resolve_unknown_name_uses_path_only(tmp_path, monkeypatch):
    on_path = _write_executable(tmp_path / "pathdir" / "custom-tool")
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    env = _env(tmp_path, PATH=str(on_path.parent))
    assert component_bins.resolve("custom-tool", env=env) == str(on_path)


def test_resolve_override_tilde_uses_supplied_home(tmp_path, monkeypatch):
    override_bin = _write_executable(tmp_path / "supplied-home" / "tools" / "graphtrail")
    monkeypatch.setenv("HOME", str(tmp_path / "host-home"))
    env = _env(tmp_path, GRAPHTRAIL_BIN="~/tools/graphtrail")
    env["HOME"] = str(tmp_path / "supplied-home")
    assert component_bins.resolve("graphtrail", env=env) == str(override_bin)


def test_resolve_argv_rewrites_engine_head(tmp_path, monkeypatch):
    managed = _write_executable(tmp_path / "managed" / "miseledger")
    _write_installed_state(tmp_path / "data", {"miseledger": managed})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert component_bins.resolve_argv(("miseledger", "doctor", "--json")) == [
        str(managed),
        "doctor",
        "--json",
    ]


def test_resolve_argv_passes_through_absolute_and_unknown(tmp_path):
    absolute = ("/usr/bin/true", "--flag")
    assert component_bins.resolve_argv(absolute) == list(absolute)
    unknown = ("some-tool", "run")
    assert component_bins.resolve_argv(unknown) == list(unknown)


def test_resolve_agent_notify_prefers_env_override(tmp_path, monkeypatch):
    override = _write_executable(tmp_path / "override" / "agent-notify")
    managed = _write_executable(tmp_path / "managed" / "agent-notify")
    _write_installed_state(tmp_path / "data", {"agent-notify": managed})
    env = _env(tmp_path, AGENT_NOTIFY_BIN=str(override))
    assert component_bins.resolve("agent-notify", env=env) == str(override)


def test_resolve_agent_notify_broken_override_does_not_fall_through(tmp_path, monkeypatch):
    managed = _write_executable(tmp_path / "managed" / "agent-notify")
    _write_installed_state(tmp_path / "data", {"agent-notify": managed})
    monkeypatch.setenv("PATH", str(managed.parent))
    env = _env(tmp_path, AGENT_NOTIFY_BIN=str(tmp_path / "nope"))
    assert component_bins.resolve("agent-notify", env=env) is None


def test_resolve_agent_notify_prefers_managed_over_legacy_go_bin(tmp_path, monkeypatch):
    managed = _write_executable(tmp_path / "managed" / "agent-notify")
    _write_executable(tmp_path / "go" / "bin" / "agent-notify")
    _write_installed_state(tmp_path / "data", {"agent-notify": managed})
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    env = _env(tmp_path, PATH=str(tmp_path / "empty"))
    assert component_bins.resolve("agent-notify", env=env) == str(managed)


def test_resolve_agent_notify_falls_back_to_legacy_go_bin(tmp_path, monkeypatch):
    legacy = _write_executable(tmp_path / "go" / "bin" / "agent-notify")
    monkeypatch.setenv("HOME", str(tmp_path / "host-home"))
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    env = _env(tmp_path, PATH=str(tmp_path / "empty"))
    assert component_bins.resolve("agent-notify", env=env) == str(legacy)


def test_resolve_agent_notify_falls_back_to_supplied_path(tmp_path, monkeypatch):
    on_path = _write_executable(tmp_path / "pathdir" / "agent-notify")
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    env = _env(tmp_path, PATH=str(on_path.parent))
    assert component_bins.resolve("agent-notify", env=env) == str(on_path)


def test_resolve_argv_rewrites_agent_notify_head(tmp_path, monkeypatch):
    managed = _write_executable(tmp_path / "managed" / "agent-notify")
    _write_installed_state(tmp_path / "data", {"agent-notify": managed})
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert component_bins.resolve_argv(("agent-notify", "version", "--json")) == [
        str(managed),
        "version",
        "--json",
    ]
