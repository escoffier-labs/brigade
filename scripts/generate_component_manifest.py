#!/usr/bin/env python3
"""Generate a deterministic component-manifest-v1.json for one Brigade tag."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


COMPONENT_IDS = ("graphtrail", "graphtrail-mcp", "miseledger", "sessionfind")
SUPPORTED_PLATFORMS = ("linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64", "windows-amd64")
REPOSITORY = "escoffier-labs/brigade"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def asset_name(component: str, platform: str) -> str:
    suffix = ".exe" if platform == "windows-amd64" else ""
    return f"{component}-{platform}{suffix}"


def expected_asset_names() -> set[str]:
    return {asset_name(component, platform) for component in COMPONENT_IDS for platform in SUPPORTED_PLATFORMS}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _validate_inputs(tag: str, commit: str, assets_dir: Path) -> None:
    if not tag.startswith("v") or len(tag) == 1:
        raise ValueError("tag must be an immutable Brigade tag beginning with v")
    if _COMMIT.fullmatch(commit) is None:
        raise ValueError("commit must be a 40-character lowercase git SHA")
    if not assets_dir.is_dir():
        raise ValueError(f"assets directory does not exist: {assets_dir}")
    found = {path.name for path in assets_dir.iterdir() if path.is_file()}
    expected = expected_asset_names()
    missing = sorted(expected - found)
    extra = sorted(found - expected)
    if missing:
        raise ValueError("missing native assets: " + ", ".join(missing))
    if extra:
        raise ValueError("unexpected native assets: " + ", ".join(extra))


def generate_manifest(
    *, tag: str, commit: str, assets_dir: Path, output: Path, checksums_output: Path
) -> dict[str, Any]:
    """Write the manifest and its complete checksums map in deterministic order."""
    _validate_inputs(tag, commit, assets_dir)
    base_url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/"
    components: dict[str, Any] = {}
    native_digests: dict[str, str] = {}
    for component in COMPONENT_IDS:
        assets: dict[str, Any] = {}
        for platform in SUPPORTED_PLATFORMS:
            name = asset_name(component, platform)
            path = assets_dir / name
            digest = sha256(path)
            native_digests[name] = digest
            assets[platform] = {
                "asset_name": name,
                "byte_size": path.stat().st_size,
                "sha256": digest,
                "download_url": base_url + name,
            }
        components[component] = {
            "component_revision": commit,
            "source": {"repository": REPOSITORY, "release_tag": tag},
            "executable": component,
            "assets": assets,
        }
    manifest = {
        "schema_version": 1,
        "brigade_version": tag[1:],
        "manifest_revision": f"{tag}+{commit}",
        "supported_platforms": list(SUPPORTED_PLATFORMS),
        "components": components,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    manifest_digest = sha256(output)
    checksum_entries = {**native_digests, output.name: manifest_digest}
    checksums_output.parent.mkdir(parents=True, exist_ok=True)
    checksums_output.write_text("".join(f"{digest}  {name}\n" for name, digest in sorted(checksum_entries.items())))
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--assets-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checksums-output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        generate_manifest(
            tag=args.tag,
            commit=args.commit,
            assets_dir=args.assets_dir,
            output=args.output,
            checksums_output=args.checksums_output,
        )
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
