#!/usr/bin/env bash
# Register the Keep-a-Changelog Unreleased merge driver for this clone.
# Safe to re-run. See CONTRIBUTING.md (Changelog).
set -euo pipefail
cd "$(dirname "$0")/.."

git config merge.changelog-unreleased.name \
  "Union-merge CHANGELOG [Unreleased]; conflict released sections"
git config merge.changelog-unreleased.driver \
  "python3 scripts/git_merge_changelog_unreleased.py %O %A %B"

echo "Configured merge.changelog-unreleased for $(pwd)"
