"""Shared helpers for component install engine tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import brigade
from brigade import component_manifest

GRAPHTRAIL_SHA = "64fcd2f9ec37f33e286708845a92e6cfa4abf3bb"
FIXTURE_REPOSITORY = "example/components"


def linux_env(root: Path) -> dict[str, str]:
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "XDG_DATA_HOME": str(root / "xdg-data"),
        "XDG_CACHE_HOME": str(root / "xdg-cache"),
    }


def smoke_stub_script(name: str) -> str:
    if name == "graphtrail":
        return (
            '#!/usr/bin/env python3\nimport sys\nif sys.argv[1:] == ["--version"]:\n'
            '    print("graphtrail test 0.4.0")\n    raise SystemExit(0)\nraise SystemExit(1)\n'
        )
    if name == "graphtrail-mcp":
        return (
            '#!/usr/bin/env python3\nimport json, sys\n'
            'req = json.load(sys.stdin)\n'
            'assert req.get("method") == "initialize"\n'
            'print(json.dumps({"jsonrpc": "2.0", "id": req.get("id"), "result": '
            '{"protocolVersion": "2024-11-05", "capabilities": {}, '
            '"serverInfo": {"name": "graphtrail-mcp", "version": "0.4.0"}}}))\n'
        )
    if name == "miseledger":
        return (
            '#!/usr/bin/env python3\nimport sys\nif sys.argv[1:] == ["version"]:\n'
            '    print("miseledger test 0.6.0")\n    raise SystemExit(0)\nraise SystemExit(1)\n'
        )
    if name == "sessionfind":
        return (
            '#!/usr/bin/env python3\nimport sys\nif sys.argv[1:] == ["--help"]:\n'
            '    print("usage: sessionfind [options]")\n    raise SystemExit(2)\nraise SystemExit(1)\n'
        )
    raise ValueError(name)


def fixture_payload(component_id: str, *, platform: str = "linux-amd64") -> tuple[bytes, int, str]:
    """Return (payload_bytes, byte_size, sha256) for runnable smoke-stub fixture bytes."""
    lines = smoke_stub_script(component_id).splitlines(keepends=True)
    if lines and lines[0].startswith("#!"):
        lines.insert(1, f"# platform: {platform}\n")
    else:
        lines.insert(0, f"# platform: {platform}\n")
    body = "".join(lines).encode("utf-8")
    digest = hashlib.sha256(body).hexdigest()
    return body, len(body), digest


def fixture_asset_name(component_id: str, *, platform: str) -> str:
    base = f"{component_id}-{platform}"
    if platform == "windows-amd64":
        return f"{base}.exe"
    return base


def test_component_revision(component_id: str) -> str:
    if component_id in ("graphtrail", "graphtrail-mcp"):
        return GRAPHTRAIL_SHA
    return "fixture-revision"


def test_manifest_asset(
    component_id: str, *, platform: str = "linux-amd64"
) -> component_manifest.ComponentAsset:
    _, byte_size, sha256 = fixture_payload(component_id, platform=platform)
    asset_name = fixture_asset_name(component_id, platform=platform)
    return component_manifest.ComponentAsset(
        asset_name=asset_name,
        byte_size=byte_size,
        sha256=sha256,
        download_url=f"https://example.invalid/components/{asset_name}",
    )


def write_test_manifest(path: Path, *, brigade_version: str) -> component_manifest.ComponentManifest:
    """Write a manifest whose digests match fixture_payload bytes for offline engine tests."""
    components: dict[str, object] = {}
    for component_id in component_manifest.KNOWN_COMPONENT_IDS:
        assets: dict[str, object] = {}
        for platform in component_manifest.SUPPORTED_PLATFORMS:
            _, byte_size, sha256 = fixture_payload(component_id, platform=platform)
            asset = test_manifest_asset(component_id, platform=platform)
            assert asset.byte_size == byte_size
            assert asset.sha256 == sha256
            assets[platform] = {
                "asset_name": asset.asset_name,
                "byte_size": byte_size,
                "sha256": sha256,
                "download_url": asset.download_url,
            }
        components[component_id] = {
            "component_revision": test_component_revision(component_id),
            "source": {"repository": FIXTURE_REPOSITORY, "release_tag": "fixture"},
            "executable": component_id,
            "assets": assets,
        }
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "brigade_version": brigade_version,
                "manifest_revision": "fixture",
                "supported_platforms": list(component_manifest.SUPPORTED_PLATFORMS),
                "components": components,
            }
        )
    )
    return component_manifest.load(path)
