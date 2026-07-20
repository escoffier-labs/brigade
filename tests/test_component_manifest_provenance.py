"""Tests for unified Brigade release manifest provenance."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "escoffier-labs/brigade"
TAG = "v1.2.3"
BASE = f"https://github.com/{REPOSITORY}/releases/download/{TAG}/"
API = f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{TAG}"
REF_API = f"https://api.github.com/repos/{REPOSITORY}/git/ref/tags/{TAG}"
TAG_API = f"https://api.github.com/repos/{REPOSITORY}/git/tags/"
COMPONENTS = ("graphtrail", "graphtrail-mcp", "miseledger", "sessionfind")
PLATFORMS = ("linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64", "windows-amd64")


def _module():
    spec = importlib.util.spec_from_file_location(
        "verify_component_manifest_provenance_test", ROOT / "scripts/verify_component_manifest_provenance.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _asset_name(component: str, platform: str) -> str:
    return f"{component}-{platform}" + (".exe" if platform == "windows-amd64" else "")


def _manifest() -> dict:
    components = {}
    for component in COMPONENTS:
        assets = {}
        for platform in PLATFORMS:
            name = _asset_name(component, platform)
            body = name.encode()
            assets[platform] = {
                "asset_name": name,
                "byte_size": len(body),
                "sha256": hashlib.sha256(body).hexdigest(),
                "download_url": BASE + name,
            }
        components[component] = {
            "component_revision": "a" * 40,
            "source": {"repository": REPOSITORY, "release_tag": TAG},
            "executable": component,
            "assets": assets,
        }
    return {
        "schema_version": 1,
        "brigade_version": "1.2.3",
        "manifest_revision": "v1.2.3+" + "a" * 40,
        "supported_platforms": list(PLATFORMS),
        "components": components,
    }


def _release(manifest: dict) -> tuple[dict, str, str]:
    native = {
        asset["asset_name"]: asset
        for component in manifest["components"].values()
        for asset in component["assets"].values()
    }
    manifest_body = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
    manifest_digest = hashlib.sha256(manifest_body.encode()).hexdigest()
    checksum_map = {name: asset["sha256"] for name, asset in native.items()}
    checksum_map["component-manifest-v1.json"] = manifest_digest
    checksums = "".join(f"{digest}  {name}\n" for name, digest in sorted(checksum_map.items()))
    assets = [
        {
            "name": name,
            "size": asset["byte_size"],
            "digest": f"sha256:{asset['sha256']}",
            "browser_download_url": BASE + name,
        }
        for name, asset in sorted(native.items())
    ]
    assets.extend(
        [
            {
                "name": "component-manifest-v1.json",
                "size": len(manifest_body.encode()),
                "digest": f"sha256:{manifest_digest}",
                "browser_download_url": BASE + "component-manifest-v1.json",
            },
            {
                "name": "checksums.txt",
                "size": len(checksums.encode()),
                "digest": f"sha256:{hashlib.sha256(checksums.encode()).hexdigest()}",
                "browser_download_url": BASE + "checksums.txt",
            },
        ]
    )
    return {"tag_name": TAG, "target_commitish": "main", "assets": assets}, checksums, manifest_body


def _fetcher(release: dict, checksums: str, manifest_body: str, *, tag_ref=None, tag_objects=None):
    tag_ref = tag_ref or {"ref": f"refs/tags/{TAG}", "object": {"type": "commit", "sha": "a" * 40}}
    tag_objects = tag_objects or {}

    def fetch(url: str) -> tuple[int, str]:
        if url == API:
            return 200, json.dumps(release)
        if url == REF_API:
            return 200, json.dumps(tag_ref)
        if url.startswith(TAG_API):
            return 200, json.dumps(tag_objects[url.removeprefix(TAG_API)])
        if url == BASE + "checksums.txt":
            return 200, checksums
        if url == BASE + "component-manifest-v1.json":
            return 200, manifest_body
        raise AssertionError(f"unexpected fetch URL: {url}")

    return fetch


def test_verifier_accepts_exact_one_release_inventory_and_release_page_manifest(tmp_path):
    module = _module()
    manifest = _manifest()
    release, checksums, manifest_body = _release(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(manifest_body)

    assert module.verify_manifest(path, fetch=_fetcher(release, checksums, manifest_body)) == []


def test_verifier_rejects_component_release_coordinate_drift(tmp_path):
    module = _module()
    manifest = _manifest()
    manifest["components"]["miseledger"]["source"]["repository"] = "escoffier-labs/miseledger"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    release, checksums, manifest_body = _release(_manifest())

    errors = module.verify_manifest(path, fetch=_fetcher(release, checksums, manifest_body))

    assert any("escoffier-labs/brigade" in error for error in errors)


def test_verifier_rejects_incomplete_matrix_extra_release_asset_and_checksum_gap(tmp_path):
    module = _module()
    manifest = _manifest()
    del manifest["components"]["sessionfind"]["assets"]["darwin-arm64"]
    release, checksums, manifest_body = _release(_manifest())
    release["assets"].append(
        {"name": "unexpected", "size": 1, "digest": "sha256:" + "a" * 64, "browser_download_url": BASE + "unexpected"}
    )
    checksums = checksums.replace("component-manifest-v1.json\n", "missing-manifest\n")
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))

    errors = module.verify_manifest(path, fetch=_fetcher(release, checksums, manifest_body))

    assert any("full platform matrix" in error for error in errors)
    assert any("unexpected assets" in error for error in errors)
    assert any("checksums.txt" in error for error in errors)


def test_verifier_requires_release_page_manifest_matching_the_local_manifest(tmp_path):
    module = _module()
    manifest = _manifest()
    release, checksums, _ = _release(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    errors = module.verify_manifest(path, fetch=_fetcher(release, checksums, "{}\n"))

    assert any("release-page component-manifest-v1.json" in error for error in errors)


def test_verifier_rejects_fetched_checksums_bytes_that_do_not_match_release_asset_digest(tmp_path):
    module = _module()
    manifest = _manifest()
    release, checksums, manifest_body = _release(manifest)
    checksum_asset = next(asset for asset in release["assets"] if asset["name"] == "checksums.txt")
    checksum_asset["digest"] = "sha256:" + "b" * 64
    path = tmp_path / "manifest.json"
    path.write_text(manifest_body)

    errors = module.verify_manifest(path, fetch=_fetcher(release, checksums, manifest_body))

    assert any("checksums.txt" in error and "digest" in error for error in errors)


def test_verifier_rejects_component_revision_mismatched_to_resolved_tag_commit(tmp_path):
    module = _module()
    manifest = _manifest()
    manifest["components"]["miseledger"]["component_revision"] = "b" * 40
    release, checksums, manifest_body = _release(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(manifest_body)

    errors = module.verify_manifest(path, fetch=_fetcher(release, checksums, manifest_body))

    assert any("component_revision" in error and "tag commit" in error for error in errors)


def test_verifier_dereferences_annotated_tag_and_ignores_release_target_commitish(tmp_path):
    module = _module()
    manifest = _manifest()
    release, checksums, manifest_body = _release(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(manifest_body)
    first_tag = "b" * 40
    second_tag = "c" * 40

    assert (
        module.verify_manifest(
            path,
            fetch=_fetcher(
                release,
                checksums,
                manifest_body,
                tag_ref={"ref": f"refs/tags/{TAG}", "object": {"type": "tag", "sha": first_tag}},
                tag_objects={
                    first_tag: {"tag": TAG, "object": {"type": "tag", "sha": second_tag}},
                    second_tag: {"tag": TAG, "object": {"type": "commit", "sha": "a" * 40}},
                },
            ),
        )
        == []
    )


@pytest.mark.parametrize(
    ("tag_ref", "tag_objects"),
    (
        ({"ref": "refs/tags/v9.9.9", "object": {"type": "commit", "sha": "a" * 40}}, {}),
        ({"ref": f"refs/tags/{TAG}", "object": {"type": "blob", "sha": "a" * 40}}, {}),
        (
            {"ref": f"refs/tags/{TAG}", "object": {"type": "tag", "sha": "b" * 40}},
            {"b" * 40: {"tag": "v9.9.9", "object": {"type": "commit", "sha": "a" * 40}}},
        ),
        (
            {"ref": f"refs/tags/{TAG}", "object": {"type": "tag", "sha": "b" * 40}},
            {"b" * 40: {"tag": TAG, "object": {"type": "tag", "sha": "b" * 40}}},
        ),
    ),
)
def test_verifier_fails_closed_for_malformed_or_cyclic_tag_objects(tmp_path, tag_ref, tag_objects):
    module = _module()
    manifest = _manifest()
    release, checksums, manifest_body = _release(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(manifest_body)

    errors = module.verify_manifest(
        path, fetch=_fetcher(release, checksums, manifest_body, tag_ref=tag_ref, tag_objects=tag_objects)
    )

    assert any("tag" in error for error in errors)


def test_verifier_fails_closed_for_excessively_nested_annotated_tags(tmp_path):
    module = _module()
    manifest = _manifest()
    release, checksums, manifest_body = _release(manifest)
    path = tmp_path / "manifest.json"
    path.write_text(manifest_body)
    shas = [f"{index:040x}" for index in range(1, module.MAX_TAG_DEREFERENCE_DEPTH + 2)]
    tag_objects = {
        sha: {"tag": TAG, "object": {"type": "tag", "sha": next_sha}}
        for sha, next_sha in zip(shas[:-1], shas[1:], strict=True)
    }

    errors = module.verify_manifest(
        path,
        fetch=_fetcher(
            release,
            checksums,
            manifest_body,
            tag_ref={"ref": f"refs/tags/{TAG}", "object": {"type": "tag", "sha": shas[0]}},
            tag_objects=tag_objects,
        ),
    )

    assert any("depth" in error for error in errors)


@pytest.mark.parametrize(
    "asset_name, digest",
    (
        ("component-manifest-v1.json", None),
        ("checksums.txt", None),
        ("graphtrail-linux-amd64", None),
        ("component-manifest-v1.json", {"sha256": "not-a-string"}),
    ),
)
def test_verifier_collects_errors_for_missing_or_malformed_github_release_digests(tmp_path, asset_name, digest):
    module = _module()
    manifest = _manifest()
    release, checksums, manifest_body = _release(manifest)
    asset = next(item for item in release["assets"] if item["name"] == asset_name)
    if digest is None:
        asset.pop("digest")
    else:
        asset["digest"] = digest
    path = tmp_path / "manifest.json"
    path.write_text(manifest_body)

    errors = module.verify_manifest(path, fetch=_fetcher(release, checksums, manifest_body))

    assert errors
    assert any(asset_name in error and "digest" in error for error in errors)
