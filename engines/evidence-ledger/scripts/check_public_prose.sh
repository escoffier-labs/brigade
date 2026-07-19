#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
vale --config ~/.vale.ini README.md CHANGELOG.md docs/
