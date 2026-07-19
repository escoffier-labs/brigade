"""Tests for native component install engine."""

from __future__ import annotations

import hashlib
import os
import subprocess

import pytest

import brigade
from brigade import component_manifest
from brigade.component_install import (
    ComponentInstallError,
    _restore_managed_executables,
    _snapshot_managed_executables,
    fetch_asset_to_cache,
    materialize_executable,
    run_post_install_smoke,
    setup_native_components,
    verify_cached_asset,
)

from tests.component_install_helpers import (
    FakeOpener,
    fixture_payload,
    linux_env,
    smoke_stub_script,
    test_manifest_asset as manifest_asset_fixture,
    write_test_manifest,
    write_verified_cache,
)

_SMOKE_COMPONENTS = ("graphtrail", "graphtrail-mcp", "miseledger", "sessionfind")


def _write_managed_stub(tmp_path, component_id: str, *, script: str | None = None) -> str:
    payload = script.encode("utf-8") if script is not None else fixture_payload(component_id)[0]
    path = tmp_path / component_id
    path.write_bytes(payload)
    path.chmod(0o755)
    return str(path)


def _write_managed_smoke_stubs(tmp_path) -> dict[str, str]:
    managed: dict[str, str] = {}
    for component_id in _SMOKE_COMPONENTS:
        managed[component_id] = _write_managed_stub(tmp_path, component_id)
    return managed


def _recording_runner(calls: list[tuple[list[str], dict[str, object]]]):
    def runner(argv, **kwargs):
        calls.append((list(argv), dict(kwargs)))
        return subprocess.run(argv, **kwargs)

    return runner


def test_verify_cached_asset_rejects_invalid_byte_size(tmp_path):
    path = tmp_path / "asset"
    path.write_bytes(b"data")
    with pytest.raises(ComponentInstallError, match="byte_size"):
        verify_cached_asset(path, byte_size=0, sha256="a" * 64)
    with pytest.raises(ComponentInstallError, match="byte_size"):
        verify_cached_asset(path, byte_size=-1, sha256="a" * 64)


def test_verify_cached_asset_rejects_malformed_sha256(tmp_path):
    path = tmp_path / "asset"
    with pytest.raises(ComponentInstallError, match="sha256"):
        verify_cached_asset(path, byte_size=10, sha256="not-a-valid-digest")
    with pytest.raises(ComponentInstallError, match="sha256"):
        verify_cached_asset(path, byte_size=10, sha256="G" * 64)


def test_verify_cached_asset_rejects_size_mismatch(tmp_path):
    path = tmp_path / "asset"
    path.write_bytes(b"short")
    with pytest.raises(ComponentInstallError, match="byte_size"):
        verify_cached_asset(path, byte_size=10, sha256="a" * 64)


def test_verify_cached_asset_rejects_digest_mismatch(tmp_path):
    path = tmp_path / "asset"
    path.write_bytes(b"0123456789")
    with pytest.raises(ComponentInstallError, match="sha256"):
        verify_cached_asset(path, byte_size=10, sha256="b" * 64)


def test_verify_cached_asset_rejects_missing_file(tmp_path):
    path = tmp_path / "asset"
    with pytest.raises(ComponentInstallError, match="missing"):
        verify_cached_asset(path, byte_size=10, sha256="a" * 64)


def test_verify_cached_asset_accepts_valid_cache(tmp_path):
    path = tmp_path / "asset"
    payload = b"0123456789"
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    verify_cached_asset(path, byte_size=len(payload), sha256=digest)


def test_fetch_asset_to_cache_writes_verified_bytes(tmp_path):
    asset = manifest_asset_fixture("miseledger", platform="linux-amd64")
    payload, byte_size, sha256 = fixture_payload("miseledger", platform="linux-amd64")
    assert asset.byte_size == byte_size
    assert asset.sha256 == sha256
    cache_path = tmp_path / "cache" / sha256 / asset.asset_name
    opener = FakeOpener({asset.download_url: payload})
    result = fetch_asset_to_cache(asset, cache_path=cache_path, offline=False, opener=opener)
    assert result == cache_path
    verify_cached_asset(cache_path, byte_size=byte_size, sha256=sha256)


def test_fetch_asset_to_cache_reuses_valid_cache_without_network(tmp_path):
    asset = manifest_asset_fixture("miseledger", platform="linux-amd64")
    payload, byte_size, sha256 = fixture_payload("miseledger", platform="linux-amd64")
    cache_path = tmp_path / "cache" / sha256 / asset.asset_name
    write_verified_cache(cache_path, payload=payload)
    opener = FakeOpener({})
    result = fetch_asset_to_cache(asset, cache_path=cache_path, offline=False, opener=opener)
    assert result == cache_path
    assert opener.calls == []


def test_fetch_asset_offline_fails_when_cache_missing(tmp_path):
    asset = manifest_asset_fixture("miseledger", platform="linux-amd64")
    cache_path = tmp_path / "cache" / asset.sha256 / asset.asset_name
    with pytest.raises(ComponentInstallError, match="offline"):
        fetch_asset_to_cache(asset, cache_path=cache_path, offline=True)


def test_fetch_asset_offline_fails_when_cache_corrupt(tmp_path):
    asset = manifest_asset_fixture("miseledger", platform="linux-amd64")
    cache_path = tmp_path / "cache" / asset.sha256 / asset.asset_name
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"bad")
    with pytest.raises(ComponentInstallError, match="offline"):
        fetch_asset_to_cache(asset, cache_path=cache_path, offline=True)


def test_fetch_asset_online_replaces_bad_cache_only_after_verified_download(tmp_path):
    asset = manifest_asset_fixture("miseledger", platform="linux-amd64")
    payload, byte_size, sha256 = fixture_payload("miseledger", platform="linux-amd64")
    cache_path = tmp_path / "cache" / sha256 / asset.asset_name
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(b"stale")
    opener = FakeOpener({asset.download_url: payload})
    fetch_asset_to_cache(asset, cache_path=cache_path, offline=False, opener=opener)
    verify_cached_asset(cache_path, byte_size=byte_size, sha256=sha256)


def test_fetch_asset_invalid_download_preserves_stale_cache(tmp_path):
    asset = manifest_asset_fixture("miseledger", platform="linux-amd64")
    cache_path = tmp_path / "cache" / asset.sha256 / asset.asset_name
    stale = b"stale"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_bytes(stale)
    bad_payload = b"wrong-bytes"
    opener = FakeOpener({asset.download_url: bad_payload})
    with pytest.raises(ComponentInstallError):
        fetch_asset_to_cache(asset, cache_path=cache_path, offline=False, opener=opener)
    assert cache_path.read_bytes() == stale


def test_fetch_asset_cleans_up_temp_on_failure(tmp_path):
    asset = manifest_asset_fixture("miseledger", platform="linux-amd64")
    cache_path = tmp_path / "cache" / asset.sha256 / asset.asset_name
    bad_payload = b"wrong-bytes"
    opener = FakeOpener({asset.download_url: bad_payload})
    with pytest.raises(ComponentInstallError):
        fetch_asset_to_cache(asset, cache_path=cache_path, offline=False, opener=opener)
    leftovers = list(cache_path.parent.glob(f".{cache_path.name}.*"))
    assert leftovers == []


def test_setup_rejects_brigade_version_mismatch(tmp_path, monkeypatch):
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version="9.9.9")
    monkeypatch.setattr("brigade.__version__", "0.23.0", raising=False)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    rc = setup_native_components(env=env)
    assert rc == 1


def test_materialize_executable_sets_mode_and_replaces(tmp_path):
    cache_path = tmp_path / "cache.bin"
    managed_path = tmp_path / "bin" / "tool"
    cache_path.write_bytes(b"#!/bin/sh\n")
    materialize_executable(cache_path=cache_path, managed_path=managed_path)
    assert managed_path.read_bytes() == cache_path.read_bytes()
    assert oct(managed_path.stat().st_mode & 0o777) == oct(0o755)


def test_materialize_executable_creates_parent_dirs(tmp_path):
    cache_path = tmp_path / "cache.bin"
    managed_path = tmp_path / "deep" / "nested" / "bin" / "tool"
    payload = b"#!/bin/sh\necho ok\n"
    cache_path.write_bytes(payload)
    materialize_executable(cache_path=cache_path, managed_path=managed_path)
    assert managed_path.read_bytes() == payload
    assert managed_path.parent.is_dir()


def test_materialize_executable_rejects_missing_cache(tmp_path):
    cache_path = tmp_path / "cache.bin"
    managed_path = tmp_path / "bin" / "tool"
    with pytest.raises(ComponentInstallError, match="cache asset missing"):
        materialize_executable(cache_path=cache_path, managed_path=managed_path)


def test_materialize_executable_failure_preserves_prior_bytes(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.bin"
    managed_path = tmp_path / "bin" / "tool"
    prior = b"OLD"
    managed_path.parent.mkdir(parents=True)
    managed_path.write_bytes(prior)
    managed_path.chmod(0o755)
    cache_path.write_bytes(b"NEW")

    def fail_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        materialize_executable(cache_path=cache_path, managed_path=managed_path)
    assert managed_path.read_bytes() == prior


def test_materialize_executable_cleans_up_temp_on_failure(tmp_path, monkeypatch):
    cache_path = tmp_path / "cache.bin"
    managed_path = tmp_path / "bin" / "tool"
    cache_path.write_bytes(b"NEW")

    def fail_replace(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError):
        materialize_executable(cache_path=cache_path, managed_path=managed_path)
    leftovers = list(managed_path.parent.glob(f".{managed_path.name}.*"))
    assert leftovers == []


def test_restore_managed_executables_restores_bytes_and_mode(tmp_path):
    path_a = tmp_path / "bin" / "a"
    path_b = tmp_path / "bin" / "b"
    path_a.parent.mkdir(parents=True)
    path_a.write_bytes(b"original-a")
    path_a.chmod(0o754)
    path_b.write_bytes(b"original-b")
    path_b.chmod(0o700)

    snapshot = _snapshot_managed_executables([path_a, path_b])

    path_a.write_bytes(b"mutated-a")
    path_a.chmod(0o644)
    path_b.write_bytes(b"mutated-b")
    path_b.chmod(0o644)

    _restore_managed_executables(snapshot)

    assert path_a.read_bytes() == b"original-a"
    assert path_b.read_bytes() == b"original-b"
    assert oct(path_a.stat().st_mode & 0o777) == oct(0o754)
    assert oct(path_b.stat().st_mode & 0o777) == oct(0o700)


def test_restore_managed_executables_removes_newly_created_paths(tmp_path):
    existing = tmp_path / "bin" / "existing"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"keep-me")
    existing.chmod(0o755)

    new_path = tmp_path / "bin" / "new"
    snapshot = _snapshot_managed_executables([existing, new_path])

    existing.write_bytes(b"changed")
    new_path.parent.mkdir(parents=True, exist_ok=True)
    new_path.write_bytes(b"brand-new")
    new_path.chmod(0o755)

    _restore_managed_executables(snapshot)

    assert existing.read_bytes() == b"keep-me"
    assert not new_path.exists()


def test_run_post_install_smoke_invokes_absolute_paths_only(tmp_path):
    managed = _write_managed_smoke_stubs(tmp_path)
    calls: list[tuple[list[str], dict[str, object]]] = []

    run_post_install_smoke(managed, runner=_recording_runner(calls))

    assert {cmd[0] for cmd, _kwargs in calls} == set(managed.values())
    assert calls[0][0] == [managed["graphtrail"], "--version"]
    assert calls[1][0] == [managed["graphtrail-mcp"]]
    assert "input" in calls[1][1]
    assert calls[2][0] == [managed["miseledger"], "version"]
    assert calls[3][0] == [managed["sessionfind"], "--help"]


def test_run_post_install_smoke_rejects_relative_paths(tmp_path):
    managed = _write_managed_smoke_stubs(tmp_path)
    managed["graphtrail"] = "graphtrail"
    with pytest.raises(ComponentInstallError, match="graphtrail.*absolute managed path"):
        run_post_install_smoke(managed)


def test_run_post_install_smoke_rejects_wrong_component_set(tmp_path):
    managed = {"graphtrail": _write_managed_stub(tmp_path, "graphtrail")}
    with pytest.raises(ComponentInstallError, match="exactly 4 managed paths"):
        run_post_install_smoke(managed)


def test_run_post_install_smoke_rejects_missing_executable(tmp_path):
    managed = _write_managed_smoke_stubs(tmp_path)
    missing = tmp_path / "graphtrail"
    missing.unlink()
    with pytest.raises(ComponentInstallError, match="graphtrail.*managed executable missing"):
        run_post_install_smoke(managed)


def test_run_post_install_smoke_rejects_graphtrail_nonzero_exit(tmp_path):
    managed = _write_managed_smoke_stubs(tmp_path)
    script = smoke_stub_script("graphtrail").replace("raise SystemExit(0)", "raise SystemExit(1)")
    managed["graphtrail"] = _write_managed_stub(tmp_path, "graphtrail", script=script)
    with pytest.raises(ComponentInstallError, match="graphtrail smoke failed.*exited 1"):
        run_post_install_smoke(managed)


def test_run_post_install_smoke_rejects_graphtrail_empty_stdout(tmp_path):
    script = (
        '#!/usr/bin/env python3\nimport sys\nif sys.argv[1:] == ["--version"]:\n'
        "    raise SystemExit(0)\nraise SystemExit(1)\n"
    )
    managed = _write_managed_smoke_stubs(tmp_path)
    managed["graphtrail"] = _write_managed_stub(tmp_path, "graphtrail", script=script)
    with pytest.raises(ComponentInstallError, match="graphtrail smoke failed.*empty stdout"):
        run_post_install_smoke(managed)


def test_run_post_install_smoke_rejects_graphtrail_mcp_malformed_json(tmp_path):
    script = '#!/usr/bin/env python3\nprint("not-json")\n'
    managed = _write_managed_smoke_stubs(tmp_path)
    managed["graphtrail-mcp"] = _write_managed_stub(tmp_path, "graphtrail-mcp", script=script)
    with pytest.raises(ComponentInstallError, match="graphtrail-mcp smoke failed.*malformed JSON-RPC"):
        run_post_install_smoke(managed)


def test_run_post_install_smoke_rejects_miseledger_nonzero_exit(tmp_path):
    managed = _write_managed_smoke_stubs(tmp_path)
    script = smoke_stub_script("miseledger").replace("raise SystemExit(0)", "raise SystemExit(1)")
    managed["miseledger"] = _write_managed_stub(tmp_path, "miseledger", script=script)
    with pytest.raises(ComponentInstallError, match="miseledger smoke failed.*exited 1"):
        run_post_install_smoke(managed)


def test_run_post_install_smoke_rejects_sessionfind_wrong_exit(tmp_path):
    managed = _write_managed_smoke_stubs(tmp_path)
    script = smoke_stub_script("sessionfind").replace("raise SystemExit(2)", "raise SystemExit(0)")
    managed["sessionfind"] = _write_managed_stub(tmp_path, "sessionfind", script=script)
    with pytest.raises(ComponentInstallError, match="sessionfind smoke failed.*expected 2"):
        run_post_install_smoke(managed)


def test_run_post_install_smoke_rejects_sessionfind_missing_usage(tmp_path):
    script = (
        '#!/usr/bin/env python3\nimport sys\nif sys.argv[1:] == ["--help"]:\n'
        '    print("options only")\n    raise SystemExit(2)\nraise SystemExit(1)\n'
    )
    managed = _write_managed_smoke_stubs(tmp_path)
    managed["sessionfind"] = _write_managed_stub(tmp_path, "sessionfind", script=script)
    with pytest.raises(ComponentInstallError, match="sessionfind smoke failed.*no usage text"):
        run_post_install_smoke(managed)


def test_run_post_install_smoke_rejects_timeout(tmp_path):
    managed = _write_managed_smoke_stubs(tmp_path)
    sleep_script = '#!/usr/bin/env python3\nimport time\ntime.sleep(5)\n'

    def slow_runner(argv, **kwargs):
        kwargs["timeout"] = 0.2
        if argv[0] == managed["graphtrail"]:
            kwargs.pop("timeout", None)
            return subprocess.run(argv, **kwargs)
        return subprocess.run(argv, **kwargs)

    managed["graphtrail-mcp"] = _write_managed_stub(tmp_path, "graphtrail-mcp", script=sleep_script)
    with pytest.raises(ComponentInstallError, match="graphtrail-mcp smoke timed out"):
        run_post_install_smoke(managed, runner=slow_runner)
