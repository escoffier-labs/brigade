# Release checklist

Pre-flight before tagging a release.

## 1. Version bump

Set the version in `pyproject.toml`, then synchronize every stamped location:

```bash
.venv/bin/python scripts/version_sync.py --write
.venv/bin/python scripts/version_sync.py --check
```

## 2. Full verification

```bash
./scripts/verify
brigade security template-audit --target .
brigade runbook run docs/runbooks/cold-start-gate.json --target . --approved
```

## 3. Local install smoke

```bash
(
  set -euo pipefail
  smoke_root="$(mktemp -d)"
  cleanup() { rm -rf "$smoke_root"; }
  trap cleanup EXIT
  export PIPX_HOME="$smoke_root/pipx-home"
  export PIPX_BIN_DIR="$smoke_root/bin"
  target="$smoke_root/target"
  mkdir -p "$PIPX_HOME" "$PIPX_BIN_DIR" "$target"
  pipx install "$PWD"
  "$PIPX_BIN_DIR/brigade" --version
  "$PIPX_BIN_DIR/brigade" init --target "$target" --depth workspace --harnesses claude,codex,openclaw
  "$PIPX_BIN_DIR/brigade" doctor --target "$target"
)
```

## 4. Merge the release commit

Commit on a feature branch, push it, open a PR, wait for required checks, and merge it. Never tag an unmerged feature-branch commit.

## 5. Tag the merged commit

Fetch `origin/main`, confirm its declared version, create an annotated tag on that exact commit, push only the tag, and wait for the publish workflow:

```bash
(
  set -euo pipefail
  git fetch origin main --tags
  version="$(.venv/bin/python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
  git show origin/main:pyproject.toml | grep -F "version = \"$version\""
  git tag -a "v$version" origin/main -m "v$version"
  git push origin "v$version"

  run_id=""
  for attempt in $(seq 1 12); do
    run_id="$(gh run list --workflow publish.yml --branch "v$version" --limit 1 --json databaseId --jq '.[0].databaseId')"
    [ -n "$run_id" ] && break
    sleep 5
  done
  test -n "$run_id"
  gh run watch "$run_id" --exit-status
)
```

The publish workflow builds `graphtrail`, `graphtrail-mcp`, `miseledger`, and `sessionfind` for
`linux-amd64`, `linux-arm64`, `darwin-amd64`, `darwin-arm64`, and `windows-amd64`. It creates one
Brigade release with exactly 20 native assets, `component-manifest-v1.json`, and `checksums.txt`.
The release body names `brigade-cli==<version>`. Rust builds run on the exact native runner for
each platform; Go builds are pure-Go cross builds with `CGO_ENABLED=0`.

The generated manifest and all component URLs must point to
`escoffier-labs/brigade` at the exact tag. The workflow fails before PyPI publication if inventory,
checksum, release-page manifest, or `gh attestation verify` checks fail. PyPI wheel and sdist builds
copy the final generated manifest into package data after that gate.

## 6. Verify the published package

```bash
python3 - <<'PY'
import json
import tomllib
import urllib.request
from pathlib import Path

expected = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
with urllib.request.urlopen("https://pypi.org/pypi/brigade-cli/json", timeout=15) as response:
    published = json.load(response)["info"]["version"]
if published != expected:
    raise SystemExit(f"published != expected: {published} != {expected}")
print(f"published={published}")
PY
```

Create a Memory Handoff in `.claude/memory-handoffs/` for durable release workflow changes, root causes, or setup gotchas.

## 7. Maintainer Hyper-V clean acceptance

Hosted Windows amd64 acceptance runs in GitHub Actions. For a clean Windows checkpoint run, follow
[`docs/runbooks/hyper-v-native-acceptance.md`](docs/runbooks/hyper-v-native-acceptance.md). It is a
maintainer-operated PowerShell Direct contract, never a self-hosted GitHub runner. Restore the
`clean` checkpoint before every run, record the exact release tag, allow Git, and require Go and
Cargo to be absent.
