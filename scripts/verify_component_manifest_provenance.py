#!/usr/bin/env python3
"""Verify a generated component manifest against its one immutable Brigade release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_MANIFEST = ROOT / "src/brigade/templates/components/manifest-v1.json"
REPOSITORY = "escoffier-labs/brigade"
COMPONENT_IDS = ("agent-notify", "graphtrail", "graphtrail-mcp", "miseledger", "sessionfind")
SUPPORTED_PLATFORMS = ("linux-amd64", "linux-arm64", "darwin-amd64", "darwin-arm64", "windows-amd64")
USER_AGENT = "brigade-component-manifest-provenance/1.0"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$")
MAX_TAG_DEREFERENCE_DEPTH = 5
FetchFn = Callable[[str], tuple[int, str]]


def parse_checksums(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2 or _SHA256.fullmatch(parts[0]) is None:
            raise ValueError(f"checksums.txt line {number} is malformed")
        if parts[1] in result:
            raise ValueError(f"checksums.txt lists duplicate asset name {parts[1]!r}")
        result[parts[1]] = parts[0]
    return result


def default_fetch(url: str) -> tuple[int, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    if token := os.environ.get("GITHUB_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def _expected_asset_name(component: str, platform: str) -> str:
    return f"{component}-{platform}" + (".exe" if platform == "windows-amd64" else "")


def _release_url(tag: str, filename: str) -> str:
    return f"https://github.com/{REPOSITORY}/releases/download/{tag}/{filename}"


def _tag_ref_url(tag: str) -> str:
    return f"https://api.github.com/repos/{REPOSITORY}/git/ref/tags/{tag}"


def _tag_object_url(sha: str) -> str:
    return f"https://api.github.com/repos/{REPOSITORY}/git/tags/{sha}"


def _fetch_json(fetch: FetchFn, url: str, *, label: str) -> dict[str, Any]:
    status, body = fetch(url)
    if status != 200:
        raise ValueError(f"GitHub {label} lookup failed: HTTP {status}")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"GitHub {label} lookup returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"GitHub {label} lookup must return an object")
    return payload


def _tag_target(payload: dict[str, Any], *, label: str) -> tuple[str, str]:
    target = payload.get("object")
    if not isinstance(target, dict):
        raise ValueError(f"GitHub {label} is missing its object")
    object_type = target.get("type")
    sha = target.get("sha")
    if object_type not in {"commit", "tag"} or not isinstance(sha, str) or _COMMIT.fullmatch(sha) is None:
        raise ValueError(f"GitHub {label} has an invalid object type or SHA")
    return object_type, sha


def resolve_tag_commit(tag: str, *, fetch: FetchFn) -> str:
    ref = _fetch_json(fetch, _tag_ref_url(tag), label="tag ref")
    if ref.get("ref") != f"refs/tags/{tag}":
        raise ValueError(f"GitHub tag ref did not report exact tag refs/tags/{tag}")
    object_type, sha = _tag_target(ref, label="tag ref")
    seen: set[str] = set()
    depth = 0
    while object_type == "tag":
        if sha in seen:
            raise ValueError("GitHub tag dereference detected a cycle")
        if depth >= MAX_TAG_DEREFERENCE_DEPTH:
            raise ValueError("GitHub tag dereference exceeded the maximum depth")
        seen.add(sha)
        annotated = _fetch_json(fetch, _tag_object_url(sha), label="tag object")
        if annotated.get("tag") != tag:
            raise ValueError(f"GitHub tag object did not report exact tag {tag}")
        object_type, sha = _tag_target(annotated, label="tag object")
        depth += 1
    return sha


def _github_asset_digest(asset: dict[str, Any]) -> re.Match[str] | None:
    digest = asset.get("digest")
    return _DIGEST.fullmatch(digest) if isinstance(digest, str) else None


def verify_manifest(manifest_path: Path, *, fetch: FetchFn = default_fetch) -> list[str]:
    try:
        raw_text = manifest_path.read_text()
        manifest = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        return [f"component manifest could not be loaded: {manifest_path} ({exc})"]
    if not isinstance(manifest, dict):
        return ["component manifest must be a JSON object"]

    errors: list[str] = []
    components = manifest.get("components")
    if not isinstance(components, dict):
        return ["component manifest field 'components' must be an object"]
    if set(components) != set(COMPONENT_IDS):
        errors.append("component manifest must contain exactly " + ", ".join(COMPONENT_IDS))

    tag: str | None = None
    expected_native: dict[str, dict[str, Any]] = {}
    for component in COMPONENT_IDS:
        value = components.get(component)
        if not isinstance(value, dict):
            errors.append(f"component {component!r} must be an object")
            continue
        source = value.get("source")
        if not isinstance(source, dict) or source.get("repository") != REPOSITORY:
            errors.append(f"component {component!r} source.repository must be {REPOSITORY}")
        release_tag = source.get("release_tag") if isinstance(source, dict) else None
        if not isinstance(release_tag, str) or not release_tag:
            errors.append(f"component {component!r} source.release_tag must be set")
            continue
        if tag is None:
            tag = release_tag
        elif tag != release_tag:
            errors.append(f"component {component!r} source.release_tag {release_tag!r} does not match {tag!r}")
        assets = value.get("assets")
        if not isinstance(assets, dict) or set(assets) != set(SUPPORTED_PLATFORMS):
            errors.append(f"component {component!r} must publish the full platform matrix")
            continue
        for platform in SUPPORTED_PLATFORMS:
            asset = assets.get(platform)
            if not isinstance(asset, dict):
                errors.append(f"component {component!r} platform {platform!r} asset must be an object")
                continue
            name = asset.get("asset_name")
            expected_name = _expected_asset_name(component, platform)
            digest = asset.get("sha256")
            size = asset.get("byte_size")
            url = asset.get("download_url")
            if name != expected_name:
                errors.append(f"component {component!r} platform {platform!r} asset_name must be {expected_name!r}")
                continue
            if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
                errors.append(f"component {component!r} asset {name!r} byte_size must be positive")
            if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
                errors.append(f"component {component!r} asset {name!r} sha256 must be lowercase SHA-256")
            if not isinstance(url, str) or url != _release_url(release_tag, name):
                errors.append(f"component {component!r} asset {name!r} must use its immutable Brigade release URL")
            if name in expected_native:
                errors.append(f"component manifest lists duplicate native asset {name!r}")
            else:
                expected_native[name] = {"size": size, "sha256": digest, "url": url}

    if tag is None:
        return errors
    api_url = f"https://api.github.com/repos/{REPOSITORY}/releases/tags/{tag}"
    status, body = fetch(api_url)
    if status != 200:
        return [*errors, f"GitHub release lookup failed for {REPOSITORY}@{tag}: HTTP {status}"]
    try:
        release = json.loads(body)
    except json.JSONDecodeError:
        return [*errors, f"GitHub release lookup returned invalid JSON for {REPOSITORY}@{tag}"]
    if not isinstance(release, dict) or release.get("tag_name") != tag:
        errors.append(f"GitHub release must report tag_name {tag!r}")
        return errors
    try:
        target_commit = resolve_tag_commit(tag, fetch=fetch)
    except ValueError as exc:
        errors.append(str(exc))
        target_commit = None
    for component in COMPONENT_IDS:
        value = components.get(component)
        revision = value.get("component_revision") if isinstance(value, dict) else None
        if not isinstance(revision, str) or _COMMIT.fullmatch(revision) is None:
            errors.append(f"component {component!r} component_revision must be a 40-character lowercase git SHA")
        elif target_commit is not None and revision != target_commit:
            errors.append(f"component {component!r} component_revision does not match the resolved tag commit")
    assets = release.get("assets")
    if not isinstance(assets, list):
        return [*errors, "GitHub release is missing an assets array"]
    release_assets: dict[str, dict[str, Any]] = {}
    for asset in assets:
        if not isinstance(asset, dict) or not isinstance(asset.get("name"), str):
            errors.append("GitHub release contains an invalid asset entry")
            continue
        name = asset["name"]
        if name in release_assets:
            errors.append(f"GitHub release lists duplicate asset {name!r}")
        release_assets[name] = asset
    expected_release_names = set(expected_native) | {"component-manifest-v1.json", "checksums.txt"}
    if set(release_assets) != expected_release_names:
        missing = sorted(expected_release_names - set(release_assets))
        extra = sorted(set(release_assets) - expected_release_names)
        if missing:
            errors.append("GitHub release is missing assets: " + ", ".join(missing))
        if extra:
            errors.append("GitHub release has unexpected assets: " + ", ".join(extra))

    for name, expected in expected_native.items():
        asset = release_assets.get(name)
        if asset is None:
            continue
        if asset.get("size") != expected["size"]:
            errors.append(f"release asset {name!r} size does not match manifest")
        if asset.get("browser_download_url") != expected["url"]:
            errors.append(f"release asset {name!r} URL does not match manifest")
        match = _github_asset_digest(asset)
        if match is None or match.group(1) != expected["sha256"]:
            errors.append(f"release asset {name!r} digest does not match manifest")

    manifest_asset = release_assets.get("component-manifest-v1.json")
    checksums_asset = release_assets.get("checksums.txt")
    if manifest_asset is None or checksums_asset is None:
        return errors
    for name, asset in (("component-manifest-v1.json", manifest_asset), ("checksums.txt", checksums_asset)):
        if asset.get("browser_download_url") != _release_url(tag, name):
            errors.append(f"release asset {name!r} must use the immutable Brigade release URL")
        if _github_asset_digest(asset) is None:
            errors.append(f"release asset {name!r} is missing a valid GitHub SHA-256 digest")

    manifest_status, release_manifest = fetch(_release_url(tag, "component-manifest-v1.json"))
    if manifest_status != 200:
        errors.append(f"release-page component-manifest-v1.json fetch failed: HTTP {manifest_status}")
    elif release_manifest != raw_text:
        errors.append("release-page component-manifest-v1.json does not match the local manifest")
    elif (manifest_digest := _github_asset_digest(manifest_asset)) is not None and (
        hashlib.sha256(release_manifest.encode()).hexdigest() != manifest_digest.group(1)
    ):
        errors.append("release-page component-manifest-v1.json digest does not match its release asset")

    checksums_status, checksums_text = fetch(_release_url(tag, "checksums.txt"))
    if checksums_status != 200:
        errors.append(f"checksums.txt fetch failed: HTTP {checksums_status}")
        return errors
    checksums_digest = _github_asset_digest(checksums_asset)
    if checksums_digest is not None and hashlib.sha256(checksums_text.encode()).hexdigest() != checksums_digest.group(
        1
    ):
        errors.append("release-page checksums.txt digest does not match its release asset")
    try:
        checksum_map = parse_checksums(checksums_text)
    except ValueError as exc:
        return [*errors, str(exc)]
    if set(checksum_map) != set(expected_native) | {"component-manifest-v1.json"}:
        errors.append("checksums.txt must contain exactly every native asset and component-manifest-v1.json")
    for name, expected in expected_native.items():
        if checksum_map.get(name) != expected["sha256"]:
            errors.append(f"checksums.txt digest for {name!r} does not match manifest")
    manifest_digest = _github_asset_digest(manifest_asset)
    if manifest_digest and checksum_map.get("component-manifest-v1.json") != manifest_digest.group(1):
        errors.append("checksums.txt digest for component-manifest-v1.json does not match release")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args(argv)
    errors = verify_manifest(args.manifest.resolve())
    for error in errors:
        print(f"component manifest provenance: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
