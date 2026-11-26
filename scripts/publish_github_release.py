#!/usr/bin/env python3
"""Upload and verify a draft GitHub release before publishing it."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import quote, urlencode

MANIFEST = "component-manifest-v1.json"
CHECKSUMS = "checksums.txt"


def local_inventory(directory):
    expected = {}
    for path in sorted(directory.iterdir()):
        with path.open("rb") as source:
            expected[path.name] = hashlib.file_digest(source, "sha256").hexdigest()
    if len(expected) != 29:
        raise ValueError("Expected exactly 29 local release assets")

    checksums = {}
    for line in (directory / CHECKSUMS).read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        if name in checksums:
            raise ValueError(f"Duplicate local checksum for {name!r}")
        checksums[name] = digest
    if checksums != {name: digest for name, digest in expected.items() if name != CHECKSUMS}:
        raise ValueError("Local assets do not match checksums.txt")

    manifest = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    manifest_assets = {}
    for component in manifest["components"].values():
        for asset in component["assets"].values():
            name = asset["asset_name"]
            if name in manifest_assets:
                raise ValueError(f"Duplicate manifest asset {name!r}")
            manifest_assets[name] = asset["sha256"]
    if manifest_assets != {name: digest for name, digest in checksums.items() if name != MANIFEST}:
        raise ValueError("Local native assets do not match the component manifest")
    return expected


def gh(*args, allow_not_found=False):
    result = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if result.returncode:
        if allow_not_found and re.match(r"HTTP/\d+(?:\.\d+)? 404(?:[ \n]|$)", result.stdout):
            return None
        raise ValueError(f"gh {args[0]} {args[1]} failed (exit {result.returncode}): {result.stderr.strip()}")
    return result.stdout


def api(endpoint, *args):
    return json.loads(gh("api", endpoint, *args))


def list_all(endpoint):
    pages = api(f"{endpoint}?per_page=100", "--paginate", "--slurp")
    if not isinstance(pages, list) or not pages or any(not isinstance(page, list) for page in pages):
        raise ValueError(f"Invalid paginated response for {endpoint}")
    return [item for page in pages for item in page]


def exact_tag_release(endpoint, tag):
    matches = [release for release in list_all(endpoint) if release["tag_name"] == tag]
    if len(matches) > 1:
        raise ValueError(f"Found multiple releases for {tag!r}")
    # The list includes authenticated drafts and can also see a release published since the tag lookup.
    return matches[0] if matches else None


def release_id(release, tag):
    identifier = release["id"]
    if (
        type(identifier) is not int
        or identifier <= 0
        or release["tag_name"] != tag
        or type(release["draft"]) is not bool
    ):
        raise ValueError(f"Invalid release identity for {tag!r}")
    return identifier


def missing_assets(endpoint, expected, *, draft):
    names = set()
    for asset in list_all(f"{endpoint}/assets"):
        name = asset["name"]
        if name in names:
            raise ValueError(f"Release has a duplicate asset {name!r}")
        if name not in expected:
            raise ValueError(f"Release has an unexpected asset {name!r}")
        if asset.get("digest") != f"sha256:{expected[name]}":
            raise ValueError(f"Release asset digest differs from local asset {name!r}")
        names.add(name)

    missing = sorted(expected.keys() - names)
    if missing and not draft:
        raise ValueError(f"Release is missing assets: {', '.join(missing)}")
    return missing


def publish(repository, tag, directory, notes):
    expected = local_inventory(directory)
    endpoint = f"repos/{repository}/releases"
    response = gh("api", f"{endpoint}/tags/{quote(tag, safe='')}", "--include", allow_not_found=True)
    if response is None:
        release = exact_tag_release(endpoint, tag)
        if release is None:
            gh(
                "release",
                "create",
                tag,
                "--repo",
                repository,
                "--verify-tag",
                "--draft",
                "--title",
                tag,
                "--notes",
                notes,
            )
            release = exact_tag_release(endpoint, tag)
            if release is None:
                raise ValueError(f"Created draft for {tag!r} was not found")
    else:
        _, separator, body = response.partition("\n\n")
        if not separator:
            raise ValueError("Missing release response body")
        release = json.loads(body)

    identifier = release_id(release, tag)
    draft = release["draft"]
    by_id = f"{endpoint}/{identifier}"
    missing = missing_assets(by_id, expected, draft=draft)
    if not draft:
        return

    for name in missing:
        url = f"https://uploads.github.com/{by_id}/assets?{urlencode({'name': name})}"
        gh(
            "api",
            url,
            "--method",
            "POST",
            "-H",
            "Content-Type: application/octet-stream",
            "--input",
            str(directory / name),
        )

    current = api(by_id)
    if release_id(current, tag) != identifier:
        raise ValueError(f"Release ID changed for {tag!r}")
    missing_assets(by_id, expected, draft=False)
    if current["draft"]:
        published = api(by_id, "--method", "PATCH", "-F", "draft=false", "-f", "make_latest=true")
        if release_id(published, tag) != identifier or published["draft"]:
            raise ValueError(f"Unexpected response when publishing {tag!r}")


def main():
    try:
        publish(os.environ["GITHUB_REPOSITORY"], os.environ["TAG"], Path("release-assets"), sys.argv[1])
    except (ValueError, KeyError, OSError, TypeError) as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
