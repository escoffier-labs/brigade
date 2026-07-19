"""Tests for native component install engine."""

from __future__ import annotations

import hashlib

import pytest

import brigade
from brigade import component_manifest
from brigade.component_install import (
    ComponentInstallError,
    fetch_asset_to_cache,
    setup_native_components,
    verify_cached_asset,
)

from tests.component_install_helpers import (
    FakeOpener,
    fixture_payload,
    linux_env,
    test_manifest_asset as manifest_asset_fixture,
    write_test_manifest,
    write_verified_cache,
)


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
