#!/usr/bin/env bash
set -euo pipefail

tag="${1:?usage: release-preflight.sh <tag> [source-dir]}"
source_dir="${2:-.}"

if [[ ! "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][0-9A-Za-z.-]+)?$ ]]; then
  echo "release tag must look like v1.2.3: $tag" >&2
  exit 1
fi

version="${tag#v}"
manifest_version="$({
  awk '
    /^\[package\]$/ { in_package = 1; next }
    /^\[/ && in_package { exit }
    in_package && /^version[[:space:]]*=/ {
      value = $0
      sub(/^[^=]*=[[:space:]]*/, "", value)
      gsub(/"/, "", value)
      print value
      exit
    }
  ' "$source_dir/Cargo.toml"
})"

if [[ -z "$manifest_version" ]]; then
  echo "Cargo.toml has no package version" >&2
  exit 1
fi
if [[ "$version" != "$manifest_version" ]]; then
  echo "tag and manifest version differ: tag=$version manifest=$manifest_version" >&2
  exit 1
fi
if ! grep -Fq "## [$version]" "$source_dir/CHANGELOG.md"; then
  echo "CHANGELOG.md has no release section for $version" >&2
  exit 1
fi

if ! tag_commit="$(
  git -C "$source_dir" rev-parse --verify "refs/tags/${tag}^{commit}" 2>/dev/null
)"; then
  echo "release tag does not exist as an immutable tag: $tag" >&2
  exit 1
fi
head_commit="$(git -C "$source_dir" rev-parse HEAD)"
if [[ "$tag_commit" != "$head_commit" ]]; then
  echo "release tag does not point to checked-out source: tag=$tag_commit head=$head_commit" >&2
  exit 1
fi
if ! git -C "$source_dir" merge-base --is-ancestor "$tag_commit" origin/master; then
  echo "release tag commit is not merged to origin/master: $tag_commit" >&2
  exit 1
fi

echo "release preflight passed: $tag at $head_commit"
