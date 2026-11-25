# Release checklist

Pre-flight before tagging a release.

## 0. Assemble the changelog fragments

Pull requests record changelog entries as `changelog.d/*.md` fragments and never edit `CHANGELOG.md`. On the release branch, fold them into `## [Unreleased]` and delete them, then roll `Unreleased` under the new `## [X.Y.Z] - YYYY-MM-DD` heading in the same commit as the version bump:

```bash
python3 scripts/changelog_fragments.py assemble
```

The `changelog-fragments` CI job treats a diff that adds a version heading as the release commit, the only kind allowed to edit `CHANGELOG.md`, and requires it to leave no fragment behind. If a fragment lands on `main` after the branch was cut, run `assemble` again on the updated branch. Format and rules: [`changelog.d/README.md`](changelog.d/README.md).

## 1. Version bump

Set the version in `pyproject.toml`, then synchronize every stamped location:

```bash
.venv/bin/python scripts/version_sync.py --write
.venv/bin/python scripts/version_sync.py --check
```

Regenerate the shipped-file history, which records every released text of the bundled `SKILL.md` files and care runbooks so an upgrade can refresh an installed copy that has no local edits. Do it before the version-bump commit, with every `v*` tag fetched, and commit the result with the bump:

```bash
git fetch origin --tags
.venv/bin/python scripts/shipped_file_history.py --write
.venv/bin/python scripts/shipped_file_history.py --check --require-provenance
```

The file is append-only. The release cut leaves the new texts marked unreleased, and the first `--write` after the tag exists records the tag.

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

Start in a clean checkout of the merged release commit. Fetch `origin/main`,
confirm the checkout matches it, and run the shim expiry guard before tagging.
The stable publish workflow repeats the guard after validating tag identity.

```bash
(
  set -euo pipefail
  git fetch origin main --tags
  release_commit="$(git rev-parse origin/main)"
  test "$(git rev-parse HEAD)" = "$release_commit"
  test -z "$(git status --porcelain)"
  version="$(.venv/bin/python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
  .venv/bin/python scripts/retired_engine_names.py --release-version "$version"
  git tag -a "v$version" "$release_commit" -m "v$version"
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

The next publish workflow builds the five managed components (`agent-notify`, `graphtrail`,
`graphtrail-mcp`, `miseledger`, and `sessionfind`) for `linux-amd64`, `linux-arm64`,
`darwin-amd64`, `darwin-arm64`, and `windows-amd64`. It also builds the canonical aliases
`brigade-code-graph`, `brigade-code-graph-mcp`, and `brigade-evidence` as separate targets
for every platform. It creates one Brigade release with exactly 40 native assets
(25 legacy + 15 canonical) and exactly 42 total GitHub release files, counting the
native assets plus `component-manifest-v1.json` and `checksums.txt`. Checksums cover
41 entries: all native assets and the primary manifest. Attestations cover those same files.
The primary manifest retains the five legacy component IDs and executables for
stable 0.30.0 upgrade and offline rollback compatibility. Validators still accept
complete historical 25-asset releases (27 total files), but new production requires
the complete additive inventory. Partial alias sets and unrelated extras are rejected.
The checked-in historical manifest stays unchanged. Package assembly stages the
new tag's generated legacy-primary manifest. Default installation identities stay legacy.
The release body names `brigadestack==<version>` and mentions `brigade-cli==<version>` as the same code under the old name. The `build-python-dist` job builds both distributions from one tree: the root project as `brigadestack`, then a temporary copy stamped by `scripts/stamp_brigade_cli.py` as `brigade-cli` (same version, dependencies, extras and package files; only the name and console scripts differ). Before the PyPI upload, the separate `check-cli-upgrade` job (no OIDC token) runs `scripts/check_cli_upgrade.py`, which installs the newest older stable `brigade-cli` from PyPI in a fresh venv, upgrades it to the built wheel and requires `brigade --version` and `brigade-hook --help` to work, so an in-place `pipx upgrade brigade-cli` cannot ship broken. `build-and-publish` downloads the built artifact by ID and checks it against the build job's SHA-256 manifest before it uploads. Through 0.33 `brigade-cli` stays a full build. After 0.33, add the final stub release: a `brigade-cli` with no `brigadestack` dependency whose `brigade` command prints the migration command (`pipx uninstall brigade-cli` then `pipx install brigadestack`), and drop the stamped build and this gate in the same change. Rust builds run on the exact native runner
for each platform. Go builds are pure-Go cross builds with `CGO_ENABLED=0`.

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
for name in ("brigadestack", "brigade-cli"):
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=15) as response:
        published = json.load(response)["info"]["version"]
    if published != expected:
        raise SystemExit(f"{name} published != expected: {published} != {expected}")
    print(f"{name} published={published}")
PY
```

Create a Memory Handoff in `.claude/memory-handoffs/` for durable release workflow changes, root causes, or setup gotchas.

## 6b. Publish the MCP Registry manifests

Run this from a checkout of the release tag, only after step 6 confirms the version on PyPI. `scripts/version_sync.py` pins `mcp-registry/*/server.json` to the tagged version. The registry checks the `mcp-name` markers in the PyPI README, and clients launch the package as `uvx brigadestack`. The first release that can be published therefore has to contain both the README markers and the `brigadestack` console script. 0.30.0 has neither.

```bash
git checkout vX.Y.Z
grep -q '^brigadestack = ' pyproject.toml && grep -q 'mcp-name: io.github.escoffier-labs/brigade-memory' README.md
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

Wait for every Python published-artifact acceptance job and the published endpoint check. Pin the official PyPI wheel URL/SHA-256 and release component manifest/checksums in `npm/feasibility/runtime.json`. From 0.32.0 the pinned wheel is the `brigadestack` wheel: set `payloadWheel.distribution` to `brigadestack` with its URL and hash. The 0.31.0 pin still names `brigade-cli`. Bump `version` and `payloadVersion` there, in `npm/feasibility/package.json`, and in `distribution/escoffier-labs-brigade/npm/package.json` (its `version` and its exact `brigadestack` dependency). `scripts/version_sync.py` does not cover the npm compatibility package: npm publishes after section 5b has already bumped `pyproject.toml`, so its version follows `runtime.json` and `tests/test_brigadestack_distribution.py` fails the promotion PR if those versions drift. `publish-compat` repeats that check against both files before it writes. Also update the hard-coded `v0.31.0` tag, its commit, the concurrency group and the `--registry-spec brigadestack@<version>` value in `npm-publish.yml`, plus the version checks in `npm/feasibility/publish.cjs`. Keep the payload tag identity separate from the npm packaging commit. Merge the promotion only after review, security, focused/full verification, and all five native candidate jobs pass.

The npm package set is `brigadestack` (the real launcher), the compatibility package `@escoffier-labs/brigade` (source `distribution/escoffier-labs-brigade/npm`, published last by the `publish-compat` job of `npm-publish.yml`) and the five `platforms.*.package` companions in `npm/feasibility/runtime.json`. All seven trust `npm-publish.yml` with the `npm` environment. `@escoffier-labs/brigade` and the companions already exist. `brigadestack` exists from the 0.31.0 bridge, with its trust entry on the retired `brigadestack-npm-publish.yml`: replace it with `npm trust revoke brigadestack --id <id>` and the `npm trust github` command below before the 0.32.0 dispatch, since npm allows one trusted publisher per package.

Before configuring trust for a first release of a name, bootstrap that name: `@escoffier-labs/brigade` and the five `platforms.*.package` entries in `npm/feasibility/runtime.json` were the six original names. These names are absent at initial setup, and [npm trust requires an existing package](https://docs.npmjs.com/cli/v11/commands/npm-trust/). Use a signed-in `@escoffier-labs` organization-owner account with 2FA enabled, npm 11.15.0 or later and Node 22.14.0 or later.

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
npm trust github "$package_name" --repo escoffier-labs/brigadestack --file npm-publish.yml --env npm --allow-publish
npm trust list "$package_name"
```

Confirm the repository, workflow, environment and publish permission for all seven packages. The [npm trust `--file` flag](https://docs.npmjs.com/cli/v11/commands/npm-trust/) takes the workflow basename `npm-publish.yml`, not `.github/workflows/npm-publish.yml`.

Protect the GitHub environment `npm` to allow only `main`. After merging, require the candidate gate to pass on the exact merged SHA before stable dispatch. Dispatch `npm-publish.yml` on main with that reviewed `expected_sha` and `release_tag: v0.29.0`. It builds once with Node 24.22.0/npm 11.19.1, retains original archives for 14 days, requires native tarball acceptance, publishes companions before the launcher with explicit provenance, and requires native registry acceptance plus the final release gate.

After any package version is published, provenance requires retries at its original packaging commit, and a different commit requires a new coordinated version even when archive bytes match.

For a retry, choose **Re-run failed jobs** in the same workflow run within the 14-day artifact retention to reuse the successful build job's original archives. **Re-run all jobs** and a new dispatch rebuild archives and do not reuse the originals. Preserve the original archives and inspect any partial registry publication before rebuilding. Compare registry integrity before every publish. A 404 permits publication, equal integrity skips it, and a mismatch stops permanently for that version. An uncertain response requires bounded read-back reconciliation. Recover from different published bytes with a new coordinated version, never overwrite. See [npm preparation and runtime limits](npm/feasibility/README.md).
