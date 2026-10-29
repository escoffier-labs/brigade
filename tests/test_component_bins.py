"""Resolution order for managed engine executables."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from brigade import component_bins
from tests._home import set_home

_SHA = "0" * 64


def _write_executable(path: Path) -> Path:
    if os.name == "nt":
        path = path.with_suffix(".exe")
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
    return {
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "LOCALAPPDATA": str(tmp_path / "data"),
        **extra,
    }


def _assert_path_match(actual: str | None, expected: Path) -> None:
    # Windows lookup may preserve the PATHEXT spelling rather than file casing.
    assert actual is not None
    assert os.path.normcase(actual) == os.path.normcase(str(expected))


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
    _assert_path_match(component_bins.resolve("graphtrail", env=env), on_path)


def test_resolve_falls_back_to_legacy_location(tmp_path, monkeypatch):
    legacy = _write_executable(tmp_path / ".cargo" / "bin" / "graphtrail")
    set_home(monkeypatch, str(tmp_path / "host-home"))
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    env = _env(tmp_path, PATH=str(tmp_path / "empty"))
    assert component_bins.resolve("graphtrail", env=env) == str(legacy)


def test_resolve_unknown_name_uses_path_only(tmp_path, monkeypatch):
    on_path = _write_executable(tmp_path / "pathdir" / "custom-tool")
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    env = _env(tmp_path, PATH=str(on_path.parent))
    _assert_path_match(component_bins.resolve("custom-tool", env=env), on_path)


def test_resolve_override_tilde_uses_supplied_home(tmp_path, monkeypatch):
    override_bin = _write_executable(tmp_path / "supplied-home" / "tools" / "graphtrail")
    set_home(monkeypatch, str(tmp_path / "host-home"))
    env = _env(tmp_path, GRAPHTRAIL_BIN=f"~/tools/{override_bin.name}")
    env["HOME"] = str(tmp_path / "supplied-home")
    env["USERPROFILE"] = str(tmp_path / "supplied-home")
    assert component_bins.resolve("graphtrail", env=env) == str(override_bin)


def test_resolve_argv_rewrites_engine_head(tmp_path, monkeypatch):
    managed = _write_executable(tmp_path / "managed" / "miseledger")
    _write_installed_state(tmp_path / "data", {"miseledger": managed})
    set_home(monkeypatch, str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
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
    set_home(monkeypatch, str(tmp_path / "host-home"))
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    env = _env(tmp_path, PATH=str(tmp_path / "empty"))
    assert component_bins.resolve("agent-notify", env=env) == str(legacy)


def test_resolve_agent_notify_falls_back_to_supplied_path(tmp_path, monkeypatch):
    on_path = _write_executable(tmp_path / "pathdir" / "agent-notify")
    monkeypatch.setenv("PATH", str(tmp_path / "elsewhere"))
    env = _env(tmp_path, PATH=str(on_path.parent))
    _assert_path_match(component_bins.resolve("agent-notify", env=env), on_path)


def test_resolve_argv_rewrites_agent_notify_head(tmp_path, monkeypatch):
    managed = _write_executable(tmp_path / "managed" / "agent-notify")
    _write_installed_state(tmp_path / "data", {"agent-notify": managed})
    set_home(monkeypatch, str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    assert component_bins.resolve_argv(("agent-notify", "version", "--json")) == [
        str(managed),
        "version",
        "--json",
    ]


def test_family_resolution_and_explicit_override_conflicts(tmp_path):
    old = _write_executable(tmp_path / "managed" / "graphtrail")
    new = _write_executable(tmp_path / "path" / "brigade-code-graph")
    _write_installed_state(tmp_path / "data", {"graphtrail": old})
    env = _env(tmp_path, PATH=str(new.parent))
    assert component_bins.resolve("brigade-code-graph", env=env) == str(old)
    env.update(BRIGADE_CODE_GRAPH_BIN=str(new), GRAPHTRAIL_BIN=str(old))
    assert component_bins.resolve("graphtrail", env=env) is None
    env["GRAPHTRAIL_BIN"] = str(new)
    assert component_bins.resolve("graphtrail", env=env) == str(new)
    env["BRIGADE_CODE_GRAPH_BIN"] = ""
    assert component_bins.resolve("graphtrail", env=env) is None


def test_legacy_request_finds_canonical_managed_identity(tmp_path):
    new = _write_executable(tmp_path / "managed" / "brigade-evidence")
    _write_installed_state(tmp_path / "data", {"brigade-evidence": new})
    assert component_bins.resolve("miseledger", env=_env(tmp_path)) == str(new)


def test_argv_refuses_explicit_override_instead_of_executing_path(tmp_path, monkeypatch):
    import pytest

    on_path = _write_executable(tmp_path / "path" / "graphtrail")
    monkeypatch.setenv("PATH", str(on_path.parent))
    monkeypatch.setenv("BRIGADE_CODE_GRAPH_BIN", str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="override"):
        component_bins.resolve_argv(["graphtrail", "--version"])


def test_each_family_honors_canonical_override_and_path_for_legacy_request(tmp_path):
    families = [
        ("graphtrail", "brigade-code-graph", "BRIGADE_CODE_GRAPH_BIN"),
        ("graphtrail-mcp", "brigade-code-graph-mcp", "BRIGADE_CODE_GRAPH_MCP_BIN"),
        ("miseledger", "brigade-evidence", "BRIGADE_EVIDENCE_BIN"),
    ]
    for old, new, key in families:
        binary = _write_executable(tmp_path / "path" / new)
        env = _env(tmp_path, PATH=str(binary.parent))
        _assert_path_match(component_bins.resolve(old, env=env), binary)
        env[key] = str(tmp_path / "missing")
        assert component_bins.resolve(old, env=env) is None
        env[key] = str(binary)
        assert component_bins.resolve(old, env=env) == str(binary)


def test_family_resolution_preserves_path_directory_order_and_requested_name_ties(tmp_path):
    legacy = _write_executable(tmp_path / "earlier" / "miseledger")
    canonical = _write_executable(tmp_path / "later" / "brigade-evidence")
    env = _env(tmp_path, PATH=os.pathsep.join((str(legacy.parent), str(canonical.parent))))
    for requested in ("miseledger", "brigade-evidence"):
        _assert_path_match(component_bins.resolve(requested, env=env), legacy)

    colocated = _write_executable(legacy.parent / "brigade-evidence")
    _assert_path_match(component_bins.resolve("miseledger", env=env), legacy)
    _assert_path_match(component_bins.resolve("brigade-evidence", env=env), colocated)


def test_nonrenamed_components_keep_empty_overrides_unset(tmp_path, monkeypatch):
    set_home(monkeypatch, str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "data"))
    for name, key in (("agent-notify", "AGENT_NOTIFY_BIN"), ("sessionfind", "SESSIONFIND_BIN")):
        binary = _write_executable(tmp_path / name / name)
        env = _env(tmp_path, PATH=str(binary.parent), **{key: ""})
        _assert_path_match(component_bins.resolve(name, env=env), binary)
        monkeypatch.setenv("PATH", str(binary.parent))
        monkeypatch.setenv(key, "")
        _assert_path_match(component_bins.resolve_argv([name, "version"])[0], binary)
