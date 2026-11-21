#!/usr/bin/env python3
"""Generate a deterministic component-manifest-v1.json for one Brigade tag."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


COMPONENT_IDS = (
    "agent-notify",
    "brigade-code-graph",
    "brigade-code-graph-mcp",
    "brigade-evidence",
    "brigade-hub",
    "sessionfind",
)
LEGACY_ALIASES = ("graphtrail", "graphtrail-mcp", "miseledger")
SUPPORTED_PLATFORMS = ("linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64", "windows-amd64")
# Mirrors component_manifest.COMPONENT_PLATFORMS: brigade-hub ships for Linux only.
COMPONENT_PLATFORMS = {"brigade-hub": ("linux-amd64", "linux-arm64")}
# The repository is renamed after 0.32.0 publishes. A manifest records the repository its
# workflow runs in (GITHUB_REPOSITORY), never a fixed slug, because the release attestations
# are signed under that name. Exactly these two names are accepted, matching
# scripts/verify_component_manifest_provenance.py.
ACCEPTED_REPOSITORIES = ("escoffier-labs/brigade", "escoffier-labs/brigadestack")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def asset_name(component: str, platform: str) -> str:
    suffix = ".exe" if platform == "windows-amd64" else ""
    return f"{component}-{platform}{suffix}"


def platforms(component: str) -> tuple[str, ...]:
    return COMPONENT_PLATFORMS.get(component, SUPPORTED_PLATFORMS)


def expected_asset_names() -> set[str]:
    return {asset_name(component, platform) for component in COMPONENT_IDS for platform in platforms(component)}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve_repository(repository: str | None) -> str:
    repository = repository if repository is not None else os.environ.get("GITHUB_REPOSITORY", "")
    if repository not in ACCEPTED_REPOSITORIES:
        raise ValueError(
            f"repository {repository!r} must be one of {', '.join(ACCEPTED_REPOSITORIES)} "
            "(pass --repository or run inside GitHub Actions)"
        )
    return repository


def _validate_inputs(tag: str, commit: str, assets_dir: Path) -> set[str]:
    if not tag.startswith("v") or len(tag) == 1:
        raise ValueError("tag must be an immutable Brigade tag beginning with v")
    if _COMMIT.fullmatch(commit) is None:
        raise ValueError("commit must be a 40-character lowercase git SHA")
    if not assets_dir.is_dir():
        raise ValueError(f"assets directory does not exist: {assets_dir}")
    found = {path.name for path in assets_dir.iterdir() if path.is_file()}
    expected = expected_asset_names()
    aliases = {asset_name(component, platform) for component in LEGACY_ALIASES for platform in SUPPORTED_PLATFORMS}
    expected |= aliases
    missing = sorted(expected - found)
    extra = sorted(found - expected)
    if missing:
        raise ValueError("missing native assets: " + ", ".join(missing))
    if extra:
        raise ValueError("unexpected native assets: " + ", ".join(extra))
    for name in sorted(found):
        if (assets_dir / name).stat().st_size <= 0:
            raise ValueError(f"canonical native asset {name!r} must have positive byte size")
    return found


def generate_manifest(
    *,
    tag: str,
    commit: str,
    assets_dir: Path,
    output: Path,
    checksums_output: Path,
    repository: str | None = None,
) -> dict[str, Any]:
    """Write the manifest and its complete checksums map in deterministic order.

    The manifest records ``repository``, defaulting to ``GITHUB_REPOSITORY``.
    """
    repository = _resolve_repository(repository)
    native_names = _validate_inputs(tag, commit, assets_dir)
    base_url = f"https://github.com/{repository}/releases/download/{tag}/"
    components: dict[str, Any] = {}
    native_digests = {name: sha256(assets_dir / name) for name in sorted(native_names)}
    for component in COMPONENT_IDS:
        assets: dict[str, Any] = {}
        for platform in platforms(component):
            name = asset_name(component, platform)
            path = assets_dir / name
            digest = native_digests[name]
            assets[platform] = {
                "asset_name": name,
                "byte_size": path.stat().st_size,
                "sha256": digest,
                "download_url": base_url + name,
            }
        components[component] = {
            "component_revision": commit,
            "source": {"repository": repository, "release_tag": tag},
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
    output.write_bytes((json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    manifest_digest = sha256(output)
    checksum_entries = {**native_digests, output.name: manifest_digest}
    checksums_output.parent.mkdir(parents=True, exist_ok=True)
    checksums_output.write_bytes(
        "".join(f"{digest}  {name}\n" for name, digest in sorted(checksum_entries.items())).encode("utf-8")
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--assets-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--checksums-output", type=Path, required=True)
    parser.add_argument("--repository", help="repository to record; defaults to GITHUB_REPOSITORY")
    args = parser.parse_args(argv)
    try:
        generate_manifest(
            tag=args.tag,
            commit=args.commit,
            assets_dir=args.assets_dir,
            output=args.output,
            checksums_output=args.checksums_output,
            repository=args.repository,
        )
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
