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
brigade work verify run --target . --argv-json '["./scripts/verify"]' --timeout 3600 --capture brigade-work
brigade security template-audit --target .
BRIGADE_EXTRAS=1 brigade runbook run docs/runbooks/cold-start-gate.json --target . --approved
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

The publish workflow builds the five managed components (`agent-notify`, `graphtrail`,
`graphtrail-mcp`, `miseledger`, and `sessionfind`) for `linux-amd64`, `linux-arm64`,
`darwin-amd64`, `darwin-arm64`, and `windows-amd64`. It creates one Brigade release with
exactly 25 native assets (5 components × 5 platforms) and exactly 27 total GitHub release files,
counting the 25 native assets plus `component-manifest-v1.json` and `checksums.txt`.
The release body names `brigade-cli==<version>`. Rust builds run on the exact native runner
for each platform; Go builds are pure-Go cross builds with `CGO_ENABLED=0`.

The generated manifest and all component URLs must point to
`escoffier-labs/brigade` at the exact tag. The workflow fails before PyPI publication if inventory,
checksum, release-page manifest, or `gh attestation verify` checks fail. PyPI wheel and sdist builds
copy the final generated manifest into package data after that gate.

## 5b. Bump to the next minor

Set `version` in `pyproject.toml` to the next minor, run `.venv/bin/python scripts/version_sync.py --write`, set `BETA_PREVIEW_BASE` to the same value in `src/brigade/update_cmd.py`, update `docs/update-channels.md` and `docs/component-manifest-policy.md`, and open a PR. Note that `brigade update --channel beta` fails closed until the first dev wheel of the new base publishes at 12:00 UTC.

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

## 6b. Publish the MCP Registry manifests

Run this from a checkout of the release tag, only after step 6 confirms the version on PyPI. `scripts/version_sync.py` pins `mcp-registry/*/server.json` to the tagged version. The registry checks the `mcp-name` markers in the PyPI README, and clients launch the package as `uvx brigade-cli`. The first release that can be published therefore has to contain both the README markers and the `brigade-cli` console script. 0.30.0 has neither.

```bash
git checkout vX.Y.Z
grep -q '^brigade-cli = ' pyproject.toml && grep -q 'mcp-name: io.github.escoffier-labs/brigade-memory' README.md
mcp-publisher login github            # an escoffier-labs org member
mcp-publisher publish mcp-registry/memory/server.json
mcp-publisher publish mcp-registry/skills/server.json
```

## 7. Maintainer Hyper-V clean acceptance

Hosted Windows amd64 acceptance runs in GitHub Actions. For a clean Windows checkpoint run, follow
[`docs/runbooks/hyper-v-native-acceptance.md`](docs/runbooks/hyper-v-native-acceptance.md). It is a
maintainer-operated PowerShell Direct contract, never a self-hosted GitHub runner. Restore the
`clean` checkpoint before every run, record the exact release tag, allow Git, and require Go and
Cargo to be absent.

## 8. Coordinated stable npm promotion

Wait for every Python published-artifact acceptance job and the published endpoint check. Pin the official PyPI wheel URL/SHA-256 and release component manifest/checksums in `npm/feasibility/runtime.json`. Keep the payload tag identity separate from the npm packaging commit. Merge the promotion only after review, security, focused/full verification, and all five native candidate jobs pass.

Before configuring trust for the first release, bootstrap all six approved names: `@escoffier-labs/brigade` and the five `platforms.*.package` entries in `npm/feasibility/runtime.json`. These names are absent at initial setup, and [npm trust requires an existing package](https://docs.npmjs.com/cli/v11/commands/npm-trust/). Use a signed-in `@escoffier-labs` organization-owner account with 2FA enabled, npm 11.15.0 or later and Node 22.14.0 or later.

For each exact name, create a separate temporary folder containing only a minimal `package.json`. Use that name and version `0.0.0-bootstrap`, for example:

```json
{"name":"@escoffier-labs/brigade","version":"0.0.0-bootstrap"}
```

Stage that temporary folder, replacing the example path with its location:

```sh
npm stage publish /path/to/temporary-package --access public --tag bootstrap --ignore-scripts
```

For a new name, this creates a public `0.0.0-stage` placeholder. The staged version and its contents remain nonpublic until approval, as described in [staged publishing](https://docs.npmjs.com/staged-publishing/). Do not stage `0.29.0`: [staged and published versions share the same unique version index](https://docs.npmjs.com/cli/v11/commands/npm-stage/). Do not approve the bootstrap stage or publish real stable contents manually. Record each returned stage ID with its package name for later deliberate cleanup. Do not automatically delete or reject those stages.

Once all six placeholders exist, verify each package's organization ownership. Set `package_name` to each exact approved name in turn, configure its trusted publisher, complete the 2FA prompt and read back the configuration:

```sh
npm trust github "$package_name" --repo escoffier-labs/brigade --file npm-publish.yml --env npm --allow-publish
npm trust list "$package_name"
```

Confirm the repository, workflow, environment and publish permission for all six packages. The [npm trust `--file` flag](https://docs.npmjs.com/cli/v11/commands/npm-trust/) takes the workflow basename `npm-publish.yml`, not `.github/workflows/npm-publish.yml`.

Protect the GitHub environment `npm` to allow only `main`. After merging, require the candidate gate to pass on the exact merged SHA before stable dispatch. Dispatch `npm-publish.yml` on main with that reviewed `expected_sha` and `release_tag: v0.29.0`. It builds once with Node 24.22.0/npm 11.19.1, retains original archives for 14 days, requires native tarball acceptance, publishes companions before the launcher with explicit provenance, and requires native registry acceptance plus the final release gate.

After any package version is published, provenance requires retries at its original packaging commit, and a different commit requires a new coordinated version even when archive bytes match.

For a retry, choose **Re-run failed jobs** in the same workflow run within the 14-day artifact retention to reuse the successful build job's original archives. **Re-run all jobs** and a new dispatch rebuild archives and do not reuse the originals. Preserve the original archives and inspect any partial registry publication before rebuilding. Compare registry integrity before every publish. A 404 permits publication, equal integrity skips it, and a mismatch stops permanently for that version. An uncertain response requires bounded read-back reconciliation. Recover from different published bytes with a new coordinated version, never overwrite. See [npm preparation and runtime limits](npm/feasibility/README.md).
