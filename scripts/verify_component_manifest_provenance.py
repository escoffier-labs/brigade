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
RENAMED_REPOSITORY = "escoffier-labs/brigadestack"
# The repository is renamed after 0.32.0 publishes. A release records the repository its
# workflow ran in, and its attestations are signed under that same name, so exactly these
# two names are accepted and never any other repository. Keep in step with
# src/brigade/update_cmd.py (tests/test_repository_rename_names.py checks the pair).
ACCEPTED_REPOSITORIES = (REPOSITORY, RENAMED_REPOSITORY)
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
# brigade-hub ships for Linux only (component_manifest.COMPONENT_PLATFORMS).
COMPONENT_PLATFORMS = {"brigade-hub": ("linux-amd64", "linux-arm64")}
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


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate manifest key {key!r}")
        result[key] = value
    return result


def _expected_asset_name(component: str, platform: str) -> str:
    return f"{component}-{platform}" + (".exe" if platform == "windows-amd64" else "")


def _release_url(repository: str, tag: str, filename: str) -> str:
    return f"https://github.com/{repository}/releases/download/{tag}/{filename}"


def read_manifest_repository(manifest_path: Path) -> tuple[str | None, list[str]]:
    """Offline structural checks, then the one accepted repository the manifest records.

    Used by --print-repository, whose output feeds an attestation verification, so it applies
    the same structural rules verify_manifest does before it trusts any component's source.
    """
    try:
        manifest = json.loads(manifest_path.read_text(), object_pairs_hook=_unique_json_object)
    except (OSError, ValueError) as exc:
        return None, [f"component manifest could not be loaded: {manifest_path} ({exc})"]
    components = manifest.get("components") if isinstance(manifest, dict) else None
    if not isinstance(components, dict) or not components:
        return None, ["component manifest field 'components' must be a non-empty object"]
    errors: list[str] = []
    if set(components) != set(COMPONENT_IDS):
        errors.append("component manifest must contain exactly " + ", ".join(COMPONENT_IDS))
    for component in COMPONENT_IDS:
        if component in components and not isinstance(components[component], dict):
            errors.append(f"component {component!r} must be an object")
    repository, repository_errors = recorded_repository(components)
    errors.extend(repository_errors)
    if repository is None and not errors:
        errors.append("component manifest records no repository")
    return (repository if not errors else None), errors


def _release_api_urls(tag: str, filename: str) -> set[str]:
    """The browser_download_url values GitHub's release API may report for one asset.

    After the rename, the API reports a pre-rename release's assets under the new name (and
    the reverse), so the API value is accepted under either of the two names. The manifest's
    own asset URLs stay strict to the repository it records.
    """
    return {_release_url(repository, tag, filename) for repository in ACCEPTED_REPOSITORIES}


def _tag_ref_url(repository: str, tag: str) -> str:
    return f"https://api.github.com/repos/{repository}/git/ref/tags/{tag}"


def _tag_object_url(repository: str, sha: str) -> str:
    return f"https://api.github.com/repos/{repository}/git/tags/{sha}"


def recorded_repository(components: dict[str, Any]) -> tuple[str | None, list[str]]:
    """Return the one accepted repository every component records, plus any errors.

    A manifest that names a third repository, or names the two accepted repositories on
    different components, has no trustworthy repository and yields None.
    """
    allowed = ", ".join(ACCEPTED_REPOSITORIES)
    repository: str | None = None
    errors: list[str] = []
    for component in COMPONENT_IDS:
        value = components.get(component)
        if not isinstance(value, dict):
            continue
        source = value.get("source")
        recorded = source.get("repository") if isinstance(source, dict) else None
        if recorded not in ACCEPTED_REPOSITORIES:
            errors.append(f"component {component!r} source.repository must be one of {allowed}")
        elif repository is None:
            repository = recorded
        elif recorded != repository:
            errors.append(f"component {component!r} source.repository {recorded!r} does not match {repository!r}")
    return (repository if not errors else None), errors


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


def resolve_tag_commit(tag: str, *, fetch: FetchFn, repository: str = REPOSITORY) -> str:
    ref = _fetch_json(fetch, _tag_ref_url(repository, tag), label="tag ref")
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
        annotated = _fetch_json(fetch, _tag_object_url(repository, sha), label="tag object")
        if annotated.get("tag") != tag:
            raise ValueError(f"GitHub tag object did not report exact tag {tag}")
        object_type, sha = _tag_target(annotated, label="tag object")
        depth += 1
    return sha


def _github_asset_digest(asset: dict[str, Any]) -> re.Match[str] | None:
    digest = asset.get("digest")
    return _DIGEST.fullmatch(digest) if isinstance(digest, str) else None


def verify_manifest(
    manifest_path: Path, *, fetch: FetchFn = default_fetch, expected_repository: str | None = None
) -> list[str]:
    try:
        raw_text = manifest_path.read_text()
        manifest = json.loads(raw_text, object_pairs_hook=_unique_json_object)
    except (OSError, ValueError) as exc:
        return [f"component manifest could not be loaded: {manifest_path} ({exc})"]
    if not isinstance(manifest, dict):
        return ["component manifest must be a JSON object"]

    errors: list[str] = []
    components = manifest.get("components")
    if not isinstance(components, dict):
        return ["component manifest field 'components' must be an object"]
    if set(components) != set(COMPONENT_IDS):
        errors.append("component manifest must contain exactly " + ", ".join(COMPONENT_IDS))

    repository, repository_errors = recorded_repository(components)
    if repository is not None and expected_repository is not None and repository != expected_repository:
        repository_errors.append(f"component manifest records {repository}, expected {expected_repository}")
    if expected_repository is not None and expected_repository not in ACCEPTED_REPOSITORIES:
        repository_errors.append(
            f"expected repository {expected_repository!r} is not one of {', '.join(ACCEPTED_REPOSITORIES)}"
        )
    if repository is None or repository_errors:
        # Do not look up releases, tags or assets under a repository the manifest cannot vouch for.
        return [
            *errors,
            *repository_errors,
            *([] if repository_errors else ["component manifest records no repository"]),
        ]

    tag: str | None = None
    expected_native: dict[str, dict[str, Any]] = {}
    for component in COMPONENT_IDS:
        value = components.get(component)
        if not isinstance(value, dict):
            errors.append(f"component {component!r} must be an object")
            continue
        source = value.get("source")
        release_tag = source.get("release_tag") if isinstance(source, dict) else None
        if not isinstance(release_tag, str) or not release_tag:
            errors.append(f"component {component!r} source.release_tag must be set")
            continue
        if tag is None:
            tag = release_tag
        elif tag != release_tag:
            errors.append(f"component {component!r} source.release_tag {release_tag!r} does not match {tag!r}")
        assets = value.get("assets")
        platforms = COMPONENT_PLATFORMS.get(component, SUPPORTED_PLATFORMS)
        if not isinstance(assets, dict) or set(assets) != set(platforms):
            errors.append(f"component {component!r} must publish the full platform matrix")
            continue
        for platform in platforms:
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
            if not isinstance(url, str) or url != _release_url(repository, release_tag, name):
                errors.append(f"component {component!r} asset {name!r} must use its immutable Brigade release URL")
            if name in expected_native:
                errors.append(f"component manifest lists duplicate native asset {name!r}")
            else:
                expected_native[name] = {"size": size, "sha256": digest, "url": url}

    if tag is None:
        return errors
    api_url = f"https://api.github.com/repos/{repository}/releases/tags/{tag}"
    status, body = fetch(api_url)
    if status != 200:
        return [*errors, f"GitHub release lookup failed for {repository}@{tag}: HTTP {status}"]
    try:
        release = json.loads(body)
    except json.JSONDecodeError:
        return [*errors, f"GitHub release lookup returned invalid JSON for {repository}@{tag}"]
    if not isinstance(release, dict) or release.get("tag_name") != tag:
        errors.append(f"GitHub release must report tag_name {tag!r}")
        return errors
    try:
        target_commit = resolve_tag_commit(tag, fetch=fetch, repository=repository)
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
    alias_names = {
        _expected_asset_name(component, platform) for component in LEGACY_ALIASES for platform in SUPPORTED_PLATFORMS
    }
    expected_aliases = alias_names
    for name in sorted(expected_aliases):
        asset = release_assets.get(name)
        if asset is None:
            continue
        size = asset.get("size")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            errors.append(f"release asset {name!r} size must be a positive integer")
        if asset.get("browser_download_url") not in _release_api_urls(tag, name):
            errors.append(f"release asset {name!r} must use the immutable Brigade release URL")
        if _github_asset_digest(asset) is None:
            errors.append(f"release asset {name!r} is missing a valid GitHub SHA-256 digest")
    expected_release_names = expected_aliases | set(expected_native) | {"component-manifest-v1.json", "checksums.txt"}
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
        # expected["url"] was already required to be the immutable URL under the recorded repository.
        if asset.get("browser_download_url") not in _release_api_urls(tag, name):
            errors.append(f"release asset {name!r} URL does not match manifest")
        match = _github_asset_digest(asset)
        if match is None or match.group(1) != expected["sha256"]:
            errors.append(f"release asset {name!r} digest does not match manifest")

    manifest_asset = release_assets.get("component-manifest-v1.json")
    checksums_asset = release_assets.get("checksums.txt")
    if manifest_asset is None or checksums_asset is None:
        return errors
    for name, asset in (("component-manifest-v1.json", manifest_asset), ("checksums.txt", checksums_asset)):
        if asset.get("browser_download_url") not in _release_api_urls(tag, name):
            errors.append(f"release asset {name!r} must use the immutable Brigade release URL")
        if _github_asset_digest(asset) is None:
            errors.append(f"release asset {name!r} is missing a valid GitHub SHA-256 digest")

    manifest_status, release_manifest = fetch(_release_url(repository, tag, "component-manifest-v1.json"))
    if manifest_status != 200:
        errors.append(f"release-page component-manifest-v1.json fetch failed: HTTP {manifest_status}")
    elif release_manifest != raw_text:
        errors.append("release-page component-manifest-v1.json does not match the local manifest")
    elif (manifest_digest := _github_asset_digest(manifest_asset)) is not None and (
        hashlib.sha256(release_manifest.encode()).hexdigest() != manifest_digest.group(1)
    ):
        errors.append("release-page component-manifest-v1.json digest does not match its release asset")

    checksums_status, checksums_text = fetch(_release_url(repository, tag, "checksums.txt"))
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
    if set(checksum_map) != expected_aliases | set(expected_native) | {"component-manifest-v1.json"}:
        errors.append("checksums.txt must contain exactly every native asset and component-manifest-v1.json")
    for name, expected in expected_native.items():
        if checksum_map.get(name) != expected["sha256"]:
            errors.append(f"checksums.txt digest for {name!r} does not match manifest")
    for name in sorted(expected_aliases):
        alias = release_assets.get(name)
        digest = _github_asset_digest(alias) if alias is not None else None
        if digest is not None and checksum_map.get(name) != digest.group(1):
            errors.append(f"checksums.txt digest for {name!r} does not match release")
    manifest_digest = _github_asset_digest(manifest_asset)
    if manifest_digest and checksum_map.get("component-manifest-v1.json") != manifest_digest.group(1):
        errors.append("checksums.txt digest for component-manifest-v1.json does not match release")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--expected-repository",
        help="require the manifest to record this repository (one of the two accepted names)",
    )
    parser.add_argument(
        "--print-repository",
        action="store_true",
        help="print the one accepted repository the manifest records and exit without any network lookup",
    )
    args = parser.parse_args(argv)
    if args.print_repository:
        repository, errors = read_manifest_repository(args.manifest)
        for error in errors:
            print(f"component manifest provenance: {error}", file=sys.stderr)
        if repository is None:
            return 1
        print(repository)
        return 0
    errors = verify_manifest(args.manifest.resolve(), expected_repository=args.expected_repository)
    for error in errors:
        print(f"component manifest provenance: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
