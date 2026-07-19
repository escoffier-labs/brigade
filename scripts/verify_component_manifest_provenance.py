#!/usr/bin/env python3
"""Verify bundled component manifest release assets against GitHub provenance."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / "src/brigade/templates/components/manifest-v1.json"
USER_AGENT = "brigade-component-manifest-provenance/1.0"
GITHUB_RELEASE_URL = re.compile(
    r"^https://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+)/releases/download/(?P<tag>[^/]+)/(?P<filename>[^/?#]+)$"
)
GITHUB_RELEASE_TAG_API = "https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}"
_SHA256_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")


FetchFn = Callable[[str], tuple[int, str]]


def parse_github_release_url(url: str) -> tuple[str, str, str, str]:
    match = GITHUB_RELEASE_URL.fullmatch(url)
    if match is None:
        raise ValueError(f"download_url is not a direct GitHub release asset URL: {url}")
    return (
        match.group("owner"),
        match.group("repo"),
        match.group("tag"),
        match.group("filename"),
    )


def parse_checksums(text: str) -> dict[str, str]:
    digests: dict[str, str] = {}
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise ValueError(f"checksums.txt line {line_number} is malformed: {raw_line!r}")
        digest, name = parts
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError(f"checksums.txt line {line_number} has invalid sha256: {digest!r}")
        if name in digests:
            raise ValueError(f"checksums.txt lists duplicate asset name {name!r}")
        digests[name] = digest
    return digests


def default_fetch(url: str) -> tuple[int, str]:
    request = urllib.request.Request(url, headers=_request_headers())
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = response.read().decode("utf-8")
            return response.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _request_headers() -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def verify_manifest(manifest_path: Path, *, fetch: FetchFn = default_fetch) -> list[str]:
    try:
        raw = json.loads(manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        return [f"component manifest could not be loaded: {manifest_path} ({exc})"]
    if not isinstance(raw, dict):
        return [f"component manifest must be a JSON object: {manifest_path}"]

    errors: list[str] = []
    components = raw.get("components")
    if not isinstance(components, dict):
        return [f"component manifest field 'components' must be an object: {manifest_path}"]

    for component_id, component_raw in sorted(components.items()):
        if not isinstance(component_raw, dict):
            errors.append(f"component {component_id!r} must be an object")
            continue
        assets_raw = component_raw.get("assets")
        if not isinstance(assets_raw, dict) or not assets_raw:
            continue
        errors.extend(
            _verify_component(
                component_id,
                component_raw,
                assets_raw,
                fetch=fetch,
            )
        )
    return errors


def _verify_component(
    component_id: str,
    component_raw: dict[str, Any],
    assets_raw: dict[str, Any],
    *,
    fetch: FetchFn,
) -> list[str]:
    errors: list[str] = []
    source = component_raw.get("source")
    repository = ""
    if isinstance(source, dict) and isinstance(source.get("repository"), str):
        repository = source["repository"].strip()
    release_tag = ""
    if isinstance(source, dict) and isinstance(source.get("release_tag"), str):
        release_tag = source["release_tag"].strip()

    parsed_assets: list[dict[str, Any]] = []
    seen_names: dict[str, str] = {}
    release_coords: tuple[str, str, str] | None = None

    for platform, asset_raw in sorted(assets_raw.items()):
        if not isinstance(asset_raw, dict):
            errors.append(f"component {component_id!r} platform {platform!r} asset must be an object")
            continue
        asset_name = asset_raw.get("asset_name")
        byte_size = asset_raw.get("byte_size")
        digest = asset_raw.get("sha256")
        download_url = asset_raw.get("download_url")
        if not isinstance(asset_name, str) or not asset_name:
            errors.append(
                f"component {component_id!r} platform {platform!r} field 'asset_name' must be a non-empty string"
            )
            continue
        if asset_name in seen_names:
            errors.append(
                f"component {component_id!r} lists duplicate asset_name {asset_name!r} "
                f"on platforms {seen_names[asset_name]!r} and {platform!r}"
            )
        else:
            seen_names[asset_name] = platform
        if not isinstance(byte_size, int) or isinstance(byte_size, bool) or byte_size <= 0:
            errors.append(
                f"component {component_id!r} platform {platform!r} field 'byte_size' must be a positive integer"
            )
            continue
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            errors.append(
                f"component {component_id!r} platform {platform!r} field 'sha256' must be 64 lowercase hex characters"
            )
            continue
        if not isinstance(download_url, str) or not download_url:
            errors.append(
                f"component {component_id!r} platform {platform!r} field 'download_url' must be a non-empty string"
            )
            continue
        path_name = Path(urlparse(download_url).path).name
        if path_name != asset_name:
            errors.append(
                f"component {component_id!r} platform {platform!r} download_url final segment "
                f"must equal asset_name {asset_name!r} (found {path_name!r})"
            )
        try:
            owner, repo, tag, filename = parse_github_release_url(download_url)
        except ValueError as exc:
            errors.append(f"component {component_id!r} platform {platform!r} {exc}")
            continue
        if filename != asset_name:
            errors.append(
                f"component {component_id!r} platform {platform!r} download_url filename "
                f"must equal asset_name {asset_name!r} (found {filename!r})"
            )
        coords = (owner, repo, tag)
        if release_coords is None:
            release_coords = coords
        elif release_coords != coords:
            expected_owner, expected_repo, expected_tag = release_coords
            errors.append(
                f"component {component_id!r} platform {platform!r} download_url must use the "
                f"same repository and tag as other assets "
                f"({expected_owner}/{expected_repo}@{expected_tag}, found {owner}/{repo}@{tag})"
            )
        if repository and repository != f"{owner}/{repo}":
            errors.append(
                f"component {component_id!r} source.repository {repository!r} does not match "
                f"download_url repository {owner}/{repo}"
            )
        if not release_tag:
            errors.append(
                f"component {component_id!r} source.release_tag must be set when assets are published"
            )
        elif release_tag != tag:
            errors.append(
                f"component {component_id!r} source.release_tag {release_tag!r} does not match "
                f"download_url tag {tag!r}"
            )
        parsed_assets.append(
            {
                "platform": platform,
                "asset_name": asset_name,
                "byte_size": byte_size,
                "sha256": digest,
                "download_url": download_url,
            }
        )

    if errors or release_coords is None or not parsed_assets:
        return errors

    owner, repo, tag = release_coords
    release_url = GITHUB_RELEASE_TAG_API.format(owner=owner, repo=repo, tag=tag)
    status, body = fetch(release_url)
    if status != 200:
        errors.append(
            f"component {component_id!r} GitHub release lookup failed for {owner}/{repo}@{tag}: HTTP {status}"
        )
        return errors
    try:
        release = json.loads(body)
    except json.JSONDecodeError:
        errors.append(
            f"component {component_id!r} GitHub release lookup returned invalid JSON for {owner}/{repo}@{tag}"
        )
        return errors

    release_assets = release.get("assets")
    if not isinstance(release_assets, list):
        errors.append(f"component {component_id!r} GitHub release for {owner}/{repo}@{tag} is missing an assets array")
        return errors

    by_name: dict[str, dict[str, Any]] = {}
    checksums_url: str | None = None
    for asset in release_assets:
        if not isinstance(asset, dict):
            continue
        name = asset.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name == "checksums.txt":
            browser_download_url = asset.get("browser_download_url")
            if isinstance(browser_download_url, str) and browser_download_url:
                checksums_url = browser_download_url
            continue
        by_name[name] = asset

    expected_names = {asset["asset_name"] for asset in parsed_assets}
    release_names = set(by_name)
    missing = sorted(expected_names - release_names)
    if missing:
        errors.append(
            f"component {component_id!r} release {owner}/{repo}@{tag} is missing assets: " + ", ".join(missing)
        )

    for asset in parsed_assets:
        release_asset = by_name.get(asset["asset_name"])
        if release_asset is None:
            continue
        platform = asset["platform"]
        name = asset["asset_name"]
        release_size = release_asset.get("size")
        if release_size != asset["byte_size"]:
            errors.append(
                f"component {component_id!r} platform {platform!r} asset {name!r} byte_size "
                f"{asset['byte_size']} does not match GitHub release size {release_size!r}"
            )
        release_url_value = release_asset.get("browser_download_url")
        if release_url_value != asset["download_url"]:
            errors.append(
                f"component {component_id!r} platform {platform!r} asset {name!r} "
                "browser_download_url does not match the GitHub release asset URL"
            )
        digest_value = release_asset.get("digest")
        if not isinstance(digest_value, str):
            errors.append(
                f"component {component_id!r} platform {platform!r} asset {name!r} is missing a GitHub release digest"
            )
        else:
            digest_match = _SHA256_DIGEST.fullmatch(digest_value)
            if digest_match is None:
                errors.append(
                    f"component {component_id!r} platform {platform!r} asset {name!r} "
                    f"has malformed GitHub release digest {digest_value!r}"
                )
            elif digest_match.group(1) != asset["sha256"]:
                errors.append(
                    f"component {component_id!r} platform {platform!r} asset {name!r} sha256 "
                    f"{asset['sha256']} does not match GitHub release digest {digest_value}"
                )

    if checksums_url is None:
        errors.append(f"component {component_id!r} release {owner}/{repo}@{tag} is missing checksums.txt")
        return errors

    checksum_status, checksum_body = fetch(checksums_url)
    if checksum_status != 200:
        errors.append(
            f"component {component_id!r} checksums.txt fetch failed for {owner}/{repo}@{tag}: HTTP {checksum_status}"
        )
        return errors
    try:
        checksum_map = parse_checksums(checksum_body)
    except ValueError as exc:
        errors.append(f"component {component_id!r} checksums.txt is malformed: {exc}")
        return errors

    for asset in parsed_assets:
        name = asset["asset_name"]
        expected = asset["sha256"]
        found = checksum_map.get(name)
        if found is None:
            errors.append(f"component {component_id!r} asset {name!r} is missing from checksums.txt")
        elif found != expected:
            errors.append(
                f"component {component_id!r} asset {name!r} sha256 {expected} "
                f"does not match checksums.txt entry {found}"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="path to manifest-v1.json (default: bundled template manifest)",
    )
    args = parser.parse_args(argv)
    errors = verify_manifest(args.manifest.resolve())
    if errors:
        for error in errors:
            print(f"component manifest provenance: {error}", file=sys.stderr)
        return 1
    print(f"component manifest provenance: ok ({args.manifest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
