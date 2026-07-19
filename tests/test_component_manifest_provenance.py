"""Tests for scripts/verify_component_manifest_provenance.py."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

MISELEDGER_BASE = "https://github.com/escoffier-labs/miseledger/releases/download/v0.6.0/"
CHECKSUMS_URL = MISELEDGER_BASE + "checksums.txt"
RELEASE_API = "https://api.github.com/repos/escoffier-labs/miseledger/releases/tags/v0.6.0"

MISELEDGER_ASSETS = {
    "miseledger-darwin-amd64": {
        "byte_size": 16659744,
        "sha256": "fb952aefd763a624e2c0346e7cc22dde8af812649ece97bd6cb62f16bc9881df",
    },
    "miseledger-linux-amd64": {
        "byte_size": 16441315,
        "sha256": "246893c8c39318f774fc7a06338b5a8e87bf84661b1951251b2c0c971e9a7a6c",
    },
    "miseledger-windows-amd64.exe": {
        "byte_size": 16616960,
        "sha256": "033f9a492435068cd7e2bd1882422c1419189f60898071411656595a93640e39",
    },
    "sessionfind-linux-amd64": {
        "byte_size": 16445238,
        "sha256": "5b42f573d44f4301b0e1cfc2008842849c7704c0f96e390c2f5461f1f8059ac0",
    },
}


def _load_provenance_module():
    spec = importlib.util.spec_from_file_location(
        "verify_component_manifest_provenance_test",
        ROOT / "scripts/verify_component_manifest_provenance.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _asset(name: str, *, byte_size: int, sha256: str) -> dict:
    return {
        "asset_name": name,
        "byte_size": byte_size,
        "sha256": sha256,
        "download_url": MISELEDGER_BASE + name,
    }


def _minimal_manifest(*, miseledger_assets: dict | None = None) -> dict:
    if miseledger_assets is None:
        assets = {
            "linux-amd64": _asset(
                "miseledger-linux-amd64",
                byte_size=MISELEDGER_ASSETS["miseledger-linux-amd64"]["byte_size"],
                sha256=MISELEDGER_ASSETS["miseledger-linux-amd64"]["sha256"],
            ),
        }
    else:
        assets = miseledger_assets
    return {
        "schema_version": 1,
        "brigade_version": "test",
        "manifest_revision": "2026-07-18",
        "supported_platforms": [
            "linux-amd64",
            "linux-arm64",
            "darwin-amd64",
            "darwin-arm64",
            "windows-amd64",
        ],
        "components": {
            "graphtrail": {
                "component_revision": "a" * 40,
                "source": {"repository": "escoffier-labs/graphtrail"},
                "executable": "graphtrail",
                "assets": {},
            },
            "graphtrail-mcp": {
                "component_revision": "a" * 40,
                "source": {"repository": "escoffier-labs/graphtrail"},
                "executable": "graphtrail-mcp",
                "assets": {},
            },
            "miseledger": {
                "component_revision": "v0.6.0",
                "source": {"repository": "escoffier-labs/miseledger", "release_tag": "v0.6.0"},
                "executable": "miseledger",
                "assets": assets,
            },
            "sessionfind": {
                "component_revision": "v0.6.0",
                "source": {"repository": "escoffier-labs/miseledger"},
                "executable": "sessionfind",
                "assets": {},
            },
        },
    }


def _release_payload(extra_assets: dict[str, dict] | None = None) -> dict:
    assets = []
    merged = dict(MISELEDGER_ASSETS)
    if extra_assets:
        merged.update(extra_assets)
    for name, meta in merged.items():
        assets.append(
            {
                "name": name,
                "size": meta["byte_size"],
                "browser_download_url": MISELEDGER_BASE + name,
                "digest": f"sha256:{meta['sha256']}",
            }
        )
    assets.append(
        {
            "name": "checksums.txt",
            "size": 123,
            "browser_download_url": CHECKSUMS_URL,
        }
    )
    return {"tag_name": "v0.6.0", "assets": assets}


def _checksums_text(asset_map: dict[str, dict] | None = None) -> str:
    merged = dict(MISELEDGER_ASSETS)
    if asset_map:
        merged.update(asset_map)
    return "\n".join(f"{meta['sha256']}  {name}" for name, meta in sorted(merged.items())) + "\n"


def _mock_fetcher(
    *,
    release: dict | None = None,
    checksums: str | None = None,
    release_status: int = 200,
) -> object:
    release_payload = release if release is not None else _release_payload()
    checksums_body = checksums if checksums is not None else _checksums_text()

    def fetch(url: str) -> tuple[int, str]:
        if url == RELEASE_API:
            if release_status != 200:
                return release_status, "not found"
            return 200, json.dumps(release_payload)
        if url == CHECKSUMS_URL:
            return 200, checksums_body
        raise AssertionError(f"unexpected fetch url: {url}")

    return fetch


def test_verify_manifest_provenance_success(tmp_path):
    module = _load_provenance_module()
    manifest_path = tmp_path / "manifest-v1.json"
    manifest_path.write_text(json.dumps(_minimal_manifest()))

    errors = module.verify_manifest(manifest_path, fetch=_mock_fetcher())

    assert errors == []


@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("byte_size", 1, "byte_size"),
        ("sha256", "a" * 64, "sha256"),
    ],
)
def test_verify_manifest_provenance_reports_release_mismatches(tmp_path, field, value, match):
    module = _load_provenance_module()
    assets = _minimal_manifest()["components"]["miseledger"]["assets"]
    assets["linux-amd64"][field] = value
    manifest_path = tmp_path / "manifest-v1.json"
    manifest_path.write_text(json.dumps(_minimal_manifest(miseledger_assets=assets)))

    errors = module.verify_manifest(manifest_path, fetch=_mock_fetcher())

    assert any(match in error for error in errors)


def test_verify_manifest_provenance_reports_browser_download_url_mismatch(tmp_path):
    module = _load_provenance_module()
    release = _release_payload()
    for asset in release["assets"]:
        if asset["name"] == "miseledger-linux-amd64":
            asset["browser_download_url"] = MISELEDGER_BASE + "other-name"
            break
    manifest_path = tmp_path / "manifest-v1.json"
    manifest_path.write_text(json.dumps(_minimal_manifest()))

    errors = module.verify_manifest(manifest_path, fetch=_mock_fetcher(release=release))

    assert any("browser_download_url" in error for error in errors)


def test_verify_manifest_provenance_reports_missing_api_digest(tmp_path):
    module = _load_provenance_module()
    release = _release_payload()
    for asset in release["assets"]:
        if asset["name"] == "miseledger-linux-amd64":
            asset.pop("digest", None)
            break
    manifest_path = tmp_path / "manifest-v1.json"
    manifest_path.write_text(json.dumps(_minimal_manifest()))

    errors = module.verify_manifest(manifest_path, fetch=_mock_fetcher(release=release))

    assert any("digest" in error for error in errors)


def test_verify_manifest_provenance_reports_checksums_mismatch(tmp_path):
    module = _load_provenance_module()
    manifest_path = tmp_path / "manifest-v1.json"
    manifest_path.write_text(json.dumps(_minimal_manifest()))
    checksums = _checksums_text(
        {
            "miseledger-linux-amd64": {
                "byte_size": 16441315,
                "sha256": "b" * 64,
            }
        }
    )

    errors = module.verify_manifest(manifest_path, fetch=_mock_fetcher(checksums=checksums))

    assert any("checksums.txt" in error for error in errors)


def test_verify_manifest_provenance_reports_wrong_repo_or_tag(tmp_path):
    module = _load_provenance_module()
    assets = _minimal_manifest()["components"]["miseledger"]["assets"]
    assets["linux-amd64"]["download_url"] = (
        "https://github.com/other/repo/releases/download/v9.9.9/miseledger-linux-amd64"
    )
    manifest_path = tmp_path / "manifest-v1.json"
    manifest_path.write_text(json.dumps(_minimal_manifest(miseledger_assets=assets)))

    errors = module.verify_manifest(manifest_path, fetch=_mock_fetcher())

    assert any("repository" in error or "release_tag" in error for error in errors)


def test_verify_manifest_provenance_accepts_sha_revision_with_semantic_release_tag(tmp_path):
    module = _load_provenance_module()
    release_tag = "v0.1.0"
    graphtrail_base = f"https://github.com/escoffier-labs/graphtrail/releases/download/{release_tag}/"
    graphtrail_api = f"https://api.github.com/repos/escoffier-labs/graphtrail/releases/tags/{release_tag}"
    asset_name = "graphtrail-linux-amd64"
    sha256 = "a" * 64
    manifest = _minimal_manifest(miseledger_assets={})
    manifest["components"]["graphtrail"] = {
        "component_revision": "b" * 40,
        "source": {"repository": "escoffier-labs/graphtrail", "release_tag": release_tag},
        "executable": "graphtrail",
        "assets": {
            "linux-amd64": {
                "asset_name": asset_name,
                "byte_size": 100,
                "sha256": sha256,
                "download_url": graphtrail_base + asset_name,
            }
        },
    }
    release = {
        "tag_name": release_tag,
        "assets": [
            {
                "name": asset_name,
                "size": 100,
                "browser_download_url": graphtrail_base + asset_name,
                "digest": f"sha256:{sha256}",
            },
            {
                "name": "checksums.txt",
                "size": 10,
                "browser_download_url": graphtrail_base + "checksums.txt",
            },
        ],
    }
    checksums = f"{sha256}  {asset_name}\n"

    def fetch(url: str) -> tuple[int, str]:
        if url == graphtrail_api:
            return 200, json.dumps(release)
        if url == graphtrail_base + "checksums.txt":
            return 200, checksums
        raise AssertionError(f"unexpected fetch url: {url}")

    manifest_path = tmp_path / "manifest-v1.json"
    manifest_path.write_text(json.dumps(manifest))

    errors = module.verify_manifest(manifest_path, fetch=fetch)

    assert errors == []


def test_verify_manifest_provenance_reports_missing_release_tag(tmp_path):
    module = _load_provenance_module()
    manifest = _minimal_manifest()
    del manifest["components"]["miseledger"]["source"]["release_tag"]
    manifest_path = tmp_path / "manifest-v1.json"
    manifest_path.write_text(json.dumps(manifest))

    errors = module.verify_manifest(manifest_path, fetch=_mock_fetcher())

    assert any("source.release_tag must be set" in error for error in errors)


def test_verify_manifest_provenance_reports_mismatched_release_tag(tmp_path):
    module = _load_provenance_module()
    manifest = _minimal_manifest()
    manifest["components"]["miseledger"]["source"]["release_tag"] = "v9.9.9"
    manifest_path = tmp_path / "manifest-v1.json"
    manifest_path.write_text(json.dumps(manifest))

    errors = module.verify_manifest(manifest_path, fetch=_mock_fetcher())

    assert any("source.release_tag" in error and "download_url tag" in error for error in errors)


def test_verify_manifest_provenance_reports_duplicate_asset_names(tmp_path):
    module = _load_provenance_module()
    duplicate = _asset(
        "miseledger-linux-amd64",
        byte_size=MISELEDGER_ASSETS["miseledger-linux-amd64"]["byte_size"],
        sha256=MISELEDGER_ASSETS["miseledger-linux-amd64"]["sha256"],
    )
    manifest_path = tmp_path / "manifest-v1.json"
    manifest_path.write_text(
        json.dumps(
            _minimal_manifest(
                miseledger_assets={
                    "linux-amd64": duplicate,
                    "linux-arm64": duplicate,
                }
            )
        )
    )

    errors = module.verify_manifest(manifest_path, fetch=_mock_fetcher())

    assert any("duplicate" in error for error in errors)
