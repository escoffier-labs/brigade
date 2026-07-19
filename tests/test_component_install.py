"""Tests for native component install engine."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

import brigade
from brigade import component_install, component_manifest, component_paths, component_state
from brigade.component_install import (
    ComponentInstallError,
    _restore_managed_executables,
    _snapshot_managed_executables,
    build_setup_plan,
    fetch_asset_to_cache,
    materialize_executable,
    resolve_roots,
    run_post_install_smoke,
    setup_native_components,
    verify_cached_asset,
)

from tests.component_install_helpers import (
    FakeOpener,
    all_fixture_payloads,
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


def _install_fixture_manifest(tmp_path, monkeypatch):
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    monkeypatch.setattr(component_manifest, "platform_key", lambda **_kwargs: "linux-amd64")
    return env, manifest_path


def _managed_paths(env: dict[str, str]) -> dict[str, Path]:
    roots = resolve_roots(env=env, system="linux")
    return {
        component_id: Path(component_paths.managed_executable_path(roots.data_root, component_id))
        for component_id in component_manifest.KNOWN_COMPONENT_IDS
    }


def _write_prior_managed(paths: dict[str, Path]) -> dict[str, bytes]:
    prior: dict[str, bytes] = {}
    for index, path in enumerate(paths.values()):
        if index % 2 == 0:
            payload = f"prior-{path.name}".encode()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            path.chmod(0o700)
            prior[path.name] = payload
    return prior


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


def test_resolve_roots_uses_xdg_paths(tmp_path):
    env = linux_env(tmp_path)
    roots = resolve_roots(env=env, system="linux")
    assert roots.data_root == env["XDG_DATA_HOME"]
    assert roots.cache_root == env["XDG_CACHE_HOME"]


def test_build_setup_plan_lists_all_four_components(tmp_path):
    manifest_path = tmp_path / "manifest-v1.json"
    manifest = write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    roots = resolve_roots(env=linux_env(tmp_path), system="linux")
    plan = build_setup_plan(manifest, platform="linux-amd64", roots=roots)
    assert {action.component_id for action in plan} == set(component_manifest.KNOWN_COMPONENT_IDS)


def test_build_setup_plan_emits_deterministic_actions(tmp_path):
    manifest_path = tmp_path / "manifest-v1.json"
    manifest = write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    roots = resolve_roots(env=linux_env(tmp_path), system="linux")
    plan = build_setup_plan(manifest, platform="linux-amd64", roots=roots)
    per_component = ("verify-cache", "download", "materialize", "smoke")
    assert [action.action for action in plan] == list(per_component) * len(component_manifest.KNOWN_COMPONENT_IDS)
    expected_ids: list[str] = []
    for component_id in component_manifest.KNOWN_COMPONENT_IDS:
        expected_ids.extend([component_id] * len(per_component))
    assert [action.component_id for action in plan] == expected_ids


def test_build_setup_plan_uses_exact_cache_and_managed_paths(tmp_path):
    manifest_path = tmp_path / "manifest-v1.json"
    manifest = write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    roots = resolve_roots(env=linux_env(tmp_path), system="linux")
    platform = "linux-amd64"
    plan = build_setup_plan(manifest, platform=platform, roots=roots)
    for component_id in component_manifest.KNOWN_COMPONENT_IDS:
        asset = manifest.components[component_id].assets[platform]
        cache_path = component_paths.cached_asset_path(roots.cache_root, asset.sha256, asset.asset_name)
        managed_path = component_paths.managed_executable_path(roots.data_root, component_id)
        component_actions = [action for action in plan if action.component_id == component_id]
        assert len(component_actions) == 4
        assert all(action.cache_path == cache_path for action in component_actions)
        assert all(action.managed_path == managed_path for action in component_actions)
        assert component_actions[0].asset_name == asset.asset_name
        assert component_actions[0].byte_size == asset.byte_size
        assert component_actions[0].sha256 == asset.sha256
        assert component_actions[0].download_url == asset.download_url
        assert component_actions[0].component_revision == manifest.components[component_id].component_revision


def test_setup_dry_run_writes_nothing(tmp_path, monkeypatch, capsys):
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    monkeypatch.setattr(component_manifest, "platform_key", lambda **_kwargs: "linux-amd64")
    rc = setup_native_components(dry_run=True, env=env)
    out = capsys.readouterr().out
    assert rc == 0
    assert "miseledger-linux-amd64" in out
    assert "download" in out
    assert not (Path(env["XDG_DATA_HOME"]) / "brigade" / "installed.json").exists()


def test_setup_dry_run_prints_required_metadata(tmp_path, monkeypatch, capsys):
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    monkeypatch.setattr(component_manifest, "platform_key", lambda **_kwargs: "linux-amd64")
    rc = setup_native_components(dry_run=True, env=env)
    out = capsys.readouterr().out
    assert rc == 0
    assert brigade.__version__ in out
    assert "fixture" in out
    assert "linux-amd64" in out
    for component_id in component_manifest.KNOWN_COMPONENT_IDS:
        assert component_id in out
    assert "verify-cache" in out
    assert "materialize" in out
    assert "smoke" in out


def test_setup_dry_run_creates_no_directories(tmp_path, monkeypatch):
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    monkeypatch.setattr(component_manifest, "platform_key", lambda **_kwargs: "linux-amd64")
    assert not Path(env["XDG_DATA_HOME"]).exists()
    assert not Path(env["XDG_CACHE_HOME"]).exists()
    rc = setup_native_components(dry_run=True, env=env)
    assert rc == 0
    assert not Path(env["XDG_DATA_HOME"]).exists()
    assert not Path(env["XDG_CACHE_HOME"]).exists()


def test_setup_dry_run_invokes_no_opener_or_runner(tmp_path, monkeypatch):
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    monkeypatch.setattr(component_manifest, "platform_key", lambda **_kwargs: "linux-amd64")
    opener = FakeOpener({})

    def boom_runner(*_args, **_kwargs):
        pytest.fail("runner should not be called during dry-run")

    rc = setup_native_components(dry_run=True, env=env, opener=opener, runner=boom_runner)
    assert rc == 0
    assert opener.calls == []


def test_setup_dry_run_rejects_unsupported_platform(tmp_path, monkeypatch, capsys):
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version=brigade.__version__)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)

    def bad_platform(**_kwargs):
        raise ValueError("unsupported platform foo-bar")

    monkeypatch.setattr(component_manifest, "platform_key", bad_platform)
    rc = setup_native_components(dry_run=True, env=env)
    err = capsys.readouterr().err
    assert rc == 1
    assert "unsupported platform" in err


def test_setup_rejects_brigade_version_mismatch(tmp_path, monkeypatch):
    env = linux_env(tmp_path)
    manifest_path = tmp_path / "manifest-v1.json"
    write_test_manifest(manifest_path, brigade_version="9.9.9")
    monkeypatch.setattr("brigade.__version__", "0.23.0", raising=False)
    monkeypatch.setattr(component_manifest, "manifest_path", lambda: manifest_path)
    rc = setup_native_components(env=env)
    assert rc == 1


def test_setup_install_writes_all_four_managed_files_and_state_after_smoke(tmp_path, monkeypatch):
    env, _manifest_path = _install_fixture_manifest(tmp_path, monkeypatch)
    opener = FakeOpener(all_fixture_payloads())

    assert setup_native_components(env=env, opener=opener) == 0

    roots = resolve_roots(env=env, system="linux")
    state_path = Path(component_paths.installed_state_path(roots.data_root))
    state = component_state.load_installed_state(state_path)
    assert state is not None
    assert set(state.components) == set(component_manifest.KNOWN_COMPONENT_IDS)
    assert len(opener.calls) == len(component_manifest.KNOWN_COMPONENT_IDS)
    for component_id, managed_path in _managed_paths(env).items():
        payload, byte_size, sha256 = fixture_payload(component_id)
        assert managed_path.read_bytes() == payload
        assert state.components[component_id].byte_size == byte_size
        assert state.components[component_id].sha256 == sha256
        assert state.components[component_id].executable == str(managed_path)


def test_setup_state_is_absent_while_smoke_runs(tmp_path, monkeypatch):
    env, _manifest_path = _install_fixture_manifest(tmp_path, monkeypatch)
    roots = resolve_roots(env=env, system="linux")
    state_path = Path(component_paths.installed_state_path(roots.data_root))
    original_smoke = component_install.run_post_install_smoke

    def assert_state_absent(managed_paths, **kwargs):
        assert not state_path.exists()
        original_smoke(managed_paths, **kwargs)

    monkeypatch.setattr(component_install, "run_post_install_smoke", assert_state_absent)

    assert setup_native_components(env=env, opener=FakeOpener(all_fixture_payloads())) == 0
    assert state_path.is_file()


def test_setup_fetches_every_asset_before_managed_bin_mutation(tmp_path, monkeypatch):
    env, _manifest_path = _install_fixture_manifest(tmp_path, monkeypatch)
    opener = FakeOpener(all_fixture_payloads())
    original_materialize = component_install.materialize_executable

    def assert_all_downloaded(*, cache_path, managed_path):
        assert len(opener.calls) == len(component_manifest.KNOWN_COMPONENT_IDS)
        original_materialize(cache_path=cache_path, managed_path=managed_path)

    monkeypatch.setattr(component_install, "materialize_executable", assert_all_downloaded)

    assert setup_native_components(env=env, opener=opener) == 0


def test_setup_last_download_failure_preserves_managed_bin(tmp_path, monkeypatch):
    env, _manifest_path = _install_fixture_manifest(tmp_path, monkeypatch)
    paths = _managed_paths(env)
    prior = _write_prior_managed(paths)
    payloads = all_fixture_payloads()
    payloads.pop(next(reversed(payloads)))

    assert setup_native_components(env=env, opener=FakeOpener(payloads)) == 1

    for component_id, path in paths.items():
        if component_id in prior:
            assert path.read_bytes() == prior[component_id]
            assert path.stat().st_mode & 0o777 == 0o700
        else:
            assert not path.exists()


def test_setup_smoke_failure_restores_old_files_and_removes_new_files(tmp_path, monkeypatch):
    env, _manifest_path = _install_fixture_manifest(tmp_path, monkeypatch)
    paths = _managed_paths(env)
    prior = _write_prior_managed(paths)

    def boom_runner(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    assert setup_native_components(env=env, opener=FakeOpener(all_fixture_payloads()), runner=boom_runner) == 1

    for component_id, path in paths.items():
        if component_id in prior:
            assert path.read_bytes() == prior[component_id]
            assert path.stat().st_mode & 0o777 == 0o700
        else:
            assert not path.exists()


def test_setup_materialize_failure_restores_managed_bin(tmp_path, monkeypatch):
    env, _manifest_path = _install_fixture_manifest(tmp_path, monkeypatch)
    paths = _managed_paths(env)
    prior = _write_prior_managed(paths)
    original_materialize = component_install.materialize_executable
    materialized = 0

    def fail_mid_batch(*, cache_path, managed_path):
        nonlocal materialized
        materialized += 1
        if materialized == 3:
            raise OSError("materialize failed")
        original_materialize(cache_path=cache_path, managed_path=managed_path)

    monkeypatch.setattr(component_install, "materialize_executable", fail_mid_batch)

    assert setup_native_components(env=env, opener=FakeOpener(all_fixture_payloads())) == 1

    for component_id, path in paths.items():
        if component_id in prior:
            assert path.read_bytes() == prior[component_id]
            assert path.stat().st_mode & 0o777 == 0o700
        else:
            assert not path.exists()


def test_setup_state_write_failure_restores_managed_bin(tmp_path, monkeypatch):
    env, _manifest_path = _install_fixture_manifest(tmp_path, monkeypatch)
    paths = _managed_paths(env)

    def fail_state_write(*_args, **_kwargs):
        raise OSError("state write failed")

    monkeypatch.setattr(component_install.component_state, "write_installed_state", fail_state_write)

    assert setup_native_components(env=env, opener=FakeOpener(all_fixture_payloads())) == 1
    assert all(not path.exists() for path in paths.values())


def test_setup_refuses_an_invalid_existing_current_state(tmp_path, monkeypatch, capsys):
    env, _manifest_path = _install_fixture_manifest(tmp_path, monkeypatch)
    roots = resolve_roots(env=env, system="linux")
    state_path = Path(component_paths.installed_state_path(roots.data_root))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{not valid json")
    opener = FakeOpener(all_fixture_payloads())

    assert setup_native_components(env=env, opener=opener) == 1
    assert "invalid installed state" in capsys.readouterr().err
    assert opener.calls == []


def test_repeat_setup_is_idempotent_without_network_or_managed_rewrites(tmp_path, monkeypatch):
    env, _manifest_path = _install_fixture_manifest(tmp_path, monkeypatch)
    first_opener = FakeOpener(all_fixture_payloads())
    assert setup_native_components(env=env, opener=first_opener) == 0
    paths = _managed_paths(env)
    mtimes_before = {component_id: path.stat().st_mtime_ns for component_id, path in paths.items()}
    roots = resolve_roots(env=env, system="linux")
    previous_path = Path(component_paths.installed_previous_state_path(roots.data_root))
    previous_before = previous_path.read_bytes() if previous_path.exists() else None

    second_opener = FakeOpener({})
    assert setup_native_components(env=env, opener=second_opener) == 0

    assert second_opener.calls == []
    assert {component_id: path.stat().st_mtime_ns for component_id, path in paths.items()} == mtimes_before
    assert (previous_path.read_bytes() if previous_path.exists() else None) == previous_before


def test_setup_rotates_previous_state_when_manifest_revision_changes(tmp_path, monkeypatch):
    env, manifest_path = _install_fixture_manifest(tmp_path, monkeypatch)
    assert setup_native_components(env=env, opener=FakeOpener(all_fixture_payloads())) == 0
    roots = resolve_roots(env=env, system="linux")
    current_path = Path(component_paths.installed_state_path(roots.data_root))
    current_before = current_path.read_bytes()
    payload = json.loads(manifest_path.read_text())
    payload["manifest_revision"] = "fixture-next"
    manifest_path.write_text(json.dumps(payload))

    assert setup_native_components(env=env, opener=FakeOpener({})) == 0

    previous_path = Path(component_paths.installed_previous_state_path(roots.data_root))
    assert previous_path.read_bytes() == current_before
    assert component_state.load_installed_state(current_path).manifest_revision == "fixture-next"


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


def test_run_post_install_smoke_rejects_graphtrail_mcp_nonzero_exit(tmp_path):
    managed = _write_managed_smoke_stubs(tmp_path)

    def nonzero_runner(argv, **kwargs):
        completed = subprocess.run(argv, **kwargs)
        if argv[0] == managed["graphtrail-mcp"]:
            return subprocess.CompletedProcess(argv, 1, completed.stdout, completed.stderr)
        return completed

    with pytest.raises(ComponentInstallError, match="graphtrail-mcp smoke failed.*exited 1"):
        run_post_install_smoke(managed, runner=nonzero_runner)


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
    sleep_script = "#!/usr/bin/env python3\nimport time\ntime.sleep(5)\n"

    def slow_runner(argv, **kwargs):
        kwargs["timeout"] = 0.2
        if argv[0] == managed["graphtrail"]:
            kwargs.pop("timeout", None)
            return subprocess.run(argv, **kwargs)
        return subprocess.run(argv, **kwargs)

    managed["graphtrail-mcp"] = _write_managed_stub(tmp_path, "graphtrail-mcp", script=sleep_script)
    with pytest.raises(ComponentInstallError, match="graphtrail-mcp smoke timed out"):
        run_post_install_smoke(managed, runner=slow_runner)
