#!/usr/bin/env bash
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"
exec python3 scripts/validate_release_workflow.py .github/workflows/release.yml
