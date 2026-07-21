"""Tests for the evidence crawler runtime resolver and compatibility checks."""

from __future__ import annotations

import os
from pathlib import Path

from brigade import evidence_runtime


def _make_bin(tmp_path: Path) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    return bin_dir


def _write_script(path: Path, body: str) -> Path:
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def _path_with(tmp_path: Path, bin_dir: Path) -> str:
    return f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"


def _discrawl_script(version: str = "0.8.0", database: str = "ok", capabilities: str = "version doctor export") -> str:
    return (
        f'if [ "$1" = "version" ]; then echo "{version}"; exit 0; fi\n'
        f'if [ "$1" = "--help" ]; then echo "Commands: {capabilities}"; exit 0; fi\n'
        f'if [ "$1" = "doctor" ] && [ "$2" = "--json" ]; then '
        f'echo \'{{"config":"ok","config_path":"/tmp/.discrawl/config.toml","database":"{database}",'
        f'"default_guild_id":"ok","discord_token":"ok","embeddings":"ok","fts":"ok","vector":"ok"}}\'; '
        f"exit 0; fi\n"
        "exit 1\n"
    )


def test_resolve_crawler_prefers_source_override(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    _write_script(bin_dir / "discrawl", _discrawl_script())
    override_dir = tmp_path / "override"
    override_dir.mkdir()
    override = _write_script(override_dir / "discrawl", _discrawl_script(version="0.9.0"))
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))
    monkeypatch.setenv("DISCORD_CRAWLER_BIN", str(override))

    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    assert runtime.resolved_path == str(override)
    assert runtime.override == str(override)
    assert runtime.version == "0.9.0"


def test_resolve_crawler_falls_back_to_path(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    default = _write_script(bin_dir / "discrawl", _discrawl_script())
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))

    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    assert runtime.resolved_path == str(default)
    assert runtime.override is None
    assert runtime.version == "0.8.0"
    assert "export" in runtime.capabilities


def test_resolve_crawler_generic_discrawl_bin_override(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    _write_script(bin_dir / "discrawl", _discrawl_script())
    override_dir = tmp_path / "override"
    override_dir.mkdir()
    override = _write_script(override_dir / "mydiscrawl", _discrawl_script(version="0.9.0"))
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))
    monkeypatch.setenv("DISCRAWL_BIN", str(override))

    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    assert runtime.resolved_path == str(override)
    assert runtime.override == str(override)


def test_resolve_crawler_reports_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))

    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    assert runtime.resolved_path is None
    assert runtime.error is not None
    assert "no executable" in runtime.error


def test_resolve_crawler_unknown_source_returns_none(tmp_path):
    env = {"PATH": str(tmp_path / "empty")}
    assert evidence_runtime.resolve_crawler("unknown_source", env=env) is None


def test_resolve_crawler_probes_capabilities_from_help(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    _write_script(
        bin_dir / "discrawl",
        'if [ "$1" = "--help" ]; then echo "Commands: version doctor export"; exit 0; fi\n'
        'if [ "$1" = "version" ]; then echo "0.8.0"; exit 0; fi\n'
        "exit 1\n",
    )
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))

    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    assert set(runtime.capabilities) >= {"version", "doctor", "export"}


def test_check_compatibility_ok(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    _write_script(bin_dir / "discrawl", _discrawl_script())
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))
    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    compat = evidence_runtime.check_compatibility(runtime)

    assert compat.state == "ok"
    assert compat.database == "ok"
    assert compat.config_path == "/tmp/.discrawl/config.toml"


def test_check_compatibility_fails_on_unreadable_archive(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    _write_script(bin_dir / "discrawl", _discrawl_script(database="schema-too-new"))
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))
    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    compat = evidence_runtime.check_compatibility(runtime)

    assert compat.state == "fail"
    assert compat.database == "schema-too-new"
    assert "archive unreadable" in compat.detail
    assert "expected database='ok'" in compat.detail


def test_check_compatibility_fails_on_missing_capability(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    _write_script(bin_dir / "discrawl", _discrawl_script(capabilities="version doctor"))
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))
    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    compat = evidence_runtime.check_compatibility(runtime)

    assert compat.state == "fail"
    assert "export" in compat.missing_capabilities
    assert "missing required capabilities" in compat.detail


def test_check_compatibility_fails_on_version_below_floor(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    _write_script(bin_dir / "discrawl", _discrawl_script(version="0.7.0"))
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))
    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    compat = evidence_runtime.check_compatibility(runtime)

    assert compat.state == "fail"
    assert "version below floor" in compat.detail
    assert "expected >= 0.8.0" in compat.detail
    assert "observed 0.7.0" in compat.detail


def test_check_compatibility_warns_on_override_different_path(monkeypatch, tmp_path):
    bin_dir = _make_bin(tmp_path)
    _write_script(bin_dir / "discrawl", _discrawl_script())
    override_dir = tmp_path / "override"
    override_dir.mkdir()
    override = _write_script(override_dir / "discrawl", _discrawl_script(version="0.8.0"))
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))
    monkeypatch.setenv("DISCORD_CRAWLER_BIN", str(override))
    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    compat = evidence_runtime.check_compatibility(runtime)

    assert compat.state == "warn"
    assert "override binary" in compat.detail


def test_check_compatibility_override_drift_does_not_downgrade_version_fail(monkeypatch, tmp_path):
    # An override binary below the version floor must stay FAIL even though it
    # also drifts from the default path - override drift must never mask an
    # incompatible runtime, or _run_crawl would stop refusing it.
    bin_dir = _make_bin(tmp_path)
    _write_script(bin_dir / "discrawl", _discrawl_script(version="0.8.0"))
    override_dir = tmp_path / "override"
    override_dir.mkdir()
    override = _write_script(override_dir / "discrawl", _discrawl_script(version="0.7.0"))
    monkeypatch.setenv("PATH", _path_with(tmp_path, bin_dir))
    monkeypatch.setenv("DISCORD_CRAWLER_BIN", str(override))
    runtime = evidence_runtime.resolve_crawler("discord")

    assert runtime is not None
    compat = evidence_runtime.check_compatibility(runtime)

    assert compat.state == "fail"
    assert "version below floor" in compat.detail
