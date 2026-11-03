# Engine internal rename plan

This plan retires the internal names of the two engines that ship inside
Brigade:

| Engine | Source | Retired names | Target names |
|---|---|---|---|
| Brigade Code Graph (Rust) | `engines/code-graph` | `graphtrail`, `graphtrail-mcp` | `brigade-code-graph`, `brigade-code-graph-mcp` |
| Brigade Evidence (Go) | `engines/evidence-ledger` | `miseledger` | `brigade-evidence` |

The `brigade` command, the `brigade` Python package, and `.brigade` workspaces
keep their names. Approved by the repository owner on 2026-11-03. Everything
below lands before v0.31.0.

Rules that apply to every slice:

- Old names live on as compatibility shims or legacy readers for at most two
  releases (v0.31 and v0.32, including their patch releases). They are removed
  in v0.33.0. The one proposed exception is the evidence data directory. See
  [Open questions](#open-questions).
- Historical receipts, memory cards, and evidence are never rewritten, and old
  evidence is never re-signed to change branding (ROADMAP, "Finish engine
  identity before moving existing data").
- No data migration in this plan. New code reads the old storage location in
  place. Real data-directory migration comes later, with recovery proof.
- Each slice is one pull request. It runs the focused gate through Brigade
  with its own selectors, and the full gate once per pull request, as
  AGENTS.md requires:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","<selectors>"]' --capture brigade-work
```

## Baseline

Measured on `a6ca15f72` with `git grep -n -i -E 'graphtrail|miseledger'`: 489
tracked files and 5,402 matching lines. The rest of this section lists what
earlier pull requests already shipped and what the source actually checks.

Already on main:

- #2121: `COMPONENT_FAMILIES` in `src/brigade/component_manifest.py` maps each
  legacy component id to its canonical family. `component_bins.ENV_OVERRIDES`
  reads `BRIGADE_CODE_GRAPH_BIN`, `BRIGADE_CODE_GRAPH_MCP_BIN` and
  `BRIGADE_EVIDENCE_BIN` beside the legacy `*_BIN` names, and refuses
  conflicting values.
- #2132: the `brigade-code-graph` and `brigade-code-graph-mcp` Cargo `[[bin]]`
  targets and `engines/evidence-ledger/cmd/brigade-evidence`. They delegate to
  the same entry points as the legacy binaries.
- #2148: `publish.yml` builds and attests 40 native assets (25 legacy-named,
  15 canonical aliases). `component-manifest-v1.json` still lists only the
  legacy component keys.

Couplings this plan has to respect:

- The canonical binaries still identify as the old names. `brigade-code-graph
  --version` prints `graphtrail 0.4.0` because clap uses `CARGO_PKG_NAME`.
  `brigade-evidence version` prints `miseledger 0.6.0` from `cmdVersion` in
  `internal/app/app.go`. `cmd/miseledger/entrypoints_test.go` asserts that
  output for both entry points.
- MCP `initialize` returns `serverInfo.name` `graphtrail`
  (`engines/code-graph/src/mcp.rs`) and `miseledger`
  (`internal/app/mcp.go`). The evidence `doctor --mcp` self-check requires
  `name == "miseledger"`.
- `brigade setup` post-install smoke (`component_install.py`,
  `_smoke_graphtrail`, `_smoke_graphtrail_mcp`, `_smoke_miseledger`) checks
  exit codes, non-empty output and the JSON-RPC envelope. It does not compare
  names, so changing the banner does not break setup.
- `evidence_runtime._normalize_miseledger_version` strips only a leading
  `miseledger ` prefix. A `brigade-evidence 0.6.0` banner would be unparseable
  and fail the version floor. `code_cmd._ENGINE_USAGE_LINE` and the
  `evidence_cmd` usage regex rewrite only the old names.
- `mcp_cmd.py` strips repo-local `--db` pins for the user-level editor
  projection only when the server name is `graphtrail` or the command
  basename contains `graphtrail`. A server named `brigade-code-graph` that
  runs `brigade-code-graph-mcp` keeps its repo pin. This is a live bug.
- Eleven Python modules build `target / ".graphtrail" / "graphtrail.db"`
  themselves (13 call sites in `aboyeur/briefs.py`,
  `claude_hooks/dependencies.py`, `code_export.py`, `context_cmd.py`,
  `graphtrail_delta.py`, `managed.py`, `operator_cmd/lifecycle.py`,
  `search_cmd.py`, `work_cmd/footprint.py`, `work_cmd/partition.py`,
  `work_cmd/verify_ranking.py`). The engines define their own defaults in
  `engines/code-graph/src/store/db.rs` and
  `engines/evidence-ledger/internal/app/paths.go`.
- The evidence bundle MAC key file lives in the evidence data directory
  (`evidenceBundleMACKeyPath`). The database and the key have to resolve
  together.
- `component_manifest.wire_id` raises on a manifest or installed state that
  holds both a legacy and a canonical key for one family.
  `component_manifest._parse_known_component` requires `executable` to equal the
  component id.
- v0.30.0's `component_state._load_components` returns `None` for any key
  outside its `KNOWN_COMPONENT_IDS`. A v0.30 client therefore treats
  canonically keyed `installed.json` as missing. It does not crash.
- New receipts write `code_graph_delta`. `graphtrail_delta` is read only as a
  legacy key (`work_cmd/footprint.py`, `runs_diff.py`). New receipts still
  write `miseledger_indexing` and `code_graph_delta.graphtrail_timeout_seconds`.
- No retired-name check exists. The content guard has no rule for these names,
  and `.content-guard-baseline.json` pins `home-path` fingerprints on a few
  lines that mention them. Editing those lines changes the fingerprints.

## Identifier classes

Treatment: **migrate** means the old name goes away with no window, because
nothing outside the repository can depend on it. **Dual-accept** means old and
new both work for the window, with a deprecation notice. **Frozen** means the
old spelling stays forever and is pinned by a test.

| Class | Current name | Target name | Treatment | Window | Removed in |
|---|---|---|---|---|---|
| Executables | `graphtrail`, `graphtrail-mcp`, `miseledger` | `brigade-code-graph`, `brigade-code-graph-mcp`, `brigade-evidence` | Dual-accept (legacy entry points and setup shims) | v0.31, v0.32 | v0.33 |
| Version banner, usage text | `graphtrail 0.4.0`, `miseledger 0.6.0`, `usage: miseledger ...` | Name of the invoked entry point | Dual-accept (legacy entry point prints the legacy name) | v0.31, v0.32 | v0.33 |
| MCP `serverInfo.name` | `graphtrail`, `miseledger` | `brigade-code-graph`, `brigade-evidence` | Dual-accept (follows entry point, checks accept both) | v0.31, v0.32 | v0.33 |
| MCP client server keys | `graphtrail`, `miseledger` | `brigade-code-graph`, `brigade-evidence` | Dual-accept (existing owned keys kept, new configs canonical) | v0.31, v0.32 | v0.33 (sync renames owned keys) |
| MCP tool names | `search`, `callers`, `search_evidence`, ... | unchanged | No change (no retired name) | none | none |
| MCP resource URIs | `miseledger://evidence/<id>` | unchanged | Frozen (persisted in memory refs, parsed by `memory_cmd.py`) | forever | never |
| Component manifest keys | `graphtrail`, `graphtrail-mcp`, `miseledger` | canonical family ids | Writer cuts over in v0.31, reader dual-accepts | v0.31, v0.32 | v0.33 |
| `installed.json` keys | legacy ids | canonical ids | Same as manifest keys | v0.31, v0.32 | v0.33 (legacy state treated as absent) |
| Release assets | `graphtrail-<platform>` and others | `brigade-code-graph-<platform>` and others | Dual-publish (already built) | v0.31, v0.32 | v0.33 stops building them |
| Published releases up to v0.32 | asset names, checksums, attestations | unchanged | Frozen (never re-published or re-signed) | forever | never |
| Binary env overrides | `GRAPHTRAIL_BIN`, `GRAPHTRAIL_MCP_BIN`, `MISELEDGER_BIN` | `BRIGADE_CODE_GRAPH_BIN`, `BRIGADE_CODE_GRAPH_MCP_BIN`, `BRIGADE_EVIDENCE_BIN` | Dual-read, doctor INFO on legacy use (stderr notice from R5) | v0.31, v0.32 | v0.33 |
| Engine env vars | `GRAPHTRAIL_DB`, `GRAPHTRAIL_ALLOW_UNSAFE_ROOT` | `BRIGADE_CODE_GRAPH_DB`, `BRIGADE_CODE_GRAPH_ALLOW_UNSAFE_ROOT` | Dual-read, doctor INFO on legacy use (stderr notice from R5) | v0.31, v0.32 | v0.33 |
| Legacy install fallbacks | `.cargo/bin/graphtrail*`, `.local/bin/miseledger` under the home directory | none | Dual-accept (resolver fallback) | v0.31, v0.32 | v0.33 |
| Code graph index | `<repo>/.graphtrail/graphtrail.db` | `<repo>/.brigade/code-graph/code-graph.db` | Legacy read in place | v0.31, v0.32 | v0.33 (index is rebuildable) |
| Evidence data, config, cache dirs | `<data>/miseledger/`, `<config>/miseledger/`, `<cache>/miseledger/` | `<data>/brigade/evidence/`, `<config>/brigade/evidence/`, `<cache>/brigade/evidence/` | Legacy read in place | v0.31, v0.32, then see open question 1 | gated on data migration |
| Verify-run snapshot files | `graphtrail-before.db`, `graphtrail-after.db` | `code-graph-before.db`, `code-graph-after.db` | Migrate (transient, deleted after each run) | none | v0.31 |
| Receipt export cursor | `.brigade/work/miseledger-export-cursor.json` | unchanged | Frozen (dedup state, renaming re-exports history) | forever | never |
| Markdown export manifests | `.miseledger-export-files`, `.miseledger-export-stage` | unchanged | Frozen (live in user export directories) | forever | never |
| HMAC and hash domains | `miseledger.evidence.bundle-mac.v1\x00`, `miseledger.evidence.stub.id.v1\x00`, `miseledger.markdown-export-filename.v1` | unchanged | Frozen | forever | never |
| Capability scope | `miseledger:item:` | unchanged | Frozen | forever | never |
| Persisted schemas and keys | `miseledger.adapter.v1`, `miseledger.internal.source_scan.v1`, `__miseledger_memory_backup__:`, `__miseledger_item_backup__:`, `miseledger://` locators, `SourceSystem: "miseledger"` | unchanged | Frozen | forever | never |
| Signed receipt fields | `miseledger_indexing`, `code_graph_delta.graphtrail_timeout_seconds`, legacy reader key `graphtrail_delta` | unchanged | Frozen | forever | never |
| Brigade output schema ids | `brigade.miseledger_export_result.v1`, `brigade.miseledger_fleet_export_result.v1`, `brigade.miseledger_index_result.v1` | unchanged for v1 | Frozen (a renamed id is a new schema version, out of scope) | forever | never |
| JSON output keys | `miseledger_binary`, `miseledger_version`, `tools["graphtrail"]`, ... | `evidence_binary`, `evidence_version`, `tools["brigade-code-graph"]`, ... | Dual-emit | v0.31, v0.32 | v0.33 (schema id bumps to v2 where a key is dropped) |
| Code graph output sentinels | `__graphtrail_limit_truncated__`, `__graphtrail_truncated_<dir>__` | unchanged | Frozen (machine-matched ids in query output) | forever | never |
| DOT export graph name | `digraph graphtrail` | `digraph code_graph` | Migrate (cosmetic label) | none | v0.31 |
| CLI flags and subcommands | `receipts export miseledger`, `work import --from-miseledger`, `work verify run --graphtrail-timeout` | `receipts export evidence`, `--from-evidence`, `--code-graph-timeout` | Dual-accept (hidden alias, warning) | v0.31, v0.32 | v0.33 |
| Workspace config key | `graphtrail_delta_timeout_seconds` | `code_graph_delta_timeout_seconds` | Dual-read, doctor INFO on legacy use (stderr notice from R5) | v0.31, v0.32 | v0.33 |
| Station ids and manifests | station `graphtrail`, `miseledger` in `station.json` and `managed-snapshot.json` | `brigade-code-graph`, `brigade-evidence` | Dual-accept (legacy id resolves as an alias) | v0.31, v0.32 | v0.33 |
| Rust package and crate | package and lib `graphtrail` | `brigade-code-graph` / `brigade_code_graph` | Migrate (in-repo only) | none | v0.31 |
| Go module path | `github.com/escoffier-labs/miseledger` | `github.com/escoffier-labs/brigade/engines/evidence-ledger` | Migrate (all packages are under `internal/`) | none | v0.31 |
| Deprecated Cargo feature | `miseledger` (direct evidence adapter, `MISELEDGER_DB`) | none | Frozen until its own deprecation policy in `build.rs` removes it | per `build.rs` | per `build.rs` |
| Python internals | `graphtrail_delta.py`, `resolve_miseledger`, `ITEM_REF_MISELEDGER`, ... | `code_graph_delta.py`, `resolve_evidence`, `ITEM_REF_EVIDENCE`, ... | Migrate (not public API, frozen values keep their spelling) | none | v0.31 |
| Docs, skills, templates | current presentation | canonical names | Migrate | none | v0.31 |
| History | `CHANGELOG.md` releases, `docs/proposals/**`, existing `docs/phase-*.md`, `docs/roadmap-archive.md` | unchanged | Frozen (history) | forever | never |

### Why the frozen identifiers stay

Each frozen string is either inside persisted data or inside a value that a
signature or MAC covers:

- The three domain strings are hashed into stored values: every
  `evidence_bundles.bundle_mac`, every `stub:v1:<hex>` id, and every
  incremental export filename. A new domain fails `evidence show`
  authentication on existing databases, changes stub ids, and orphans
  exported files.
- `miseledger:item:` is the `ItemRef` scope inside HMAC capability tokens
  (`brigade.trust-review-capability.v1`). Python (`trust_gate.py`,
  `authority_broker.py`) and Go both check it, and `trust_reviews` rows store
  it.
- `miseledger.adapter.v1` is a wire schema that external adapters and
  `brigade receipts export` emit. The importer must keep accepting it, and
  Brigade keeps emitting it so older engines can import new exports.
- Source-scan sentinels, backup collection prefixes, `miseledger://`
  locators and `SourceSystem` values sit in `items.provenance_json` and
  collection keys of existing databases. Provenance backfill checks hash
  them.
- Receipt fields are covered by Ed25519 or SSHSIG signatures. Readers keep
  every historical spelling. Writers keep the current spelling, so a receipt
  produced during the window verifies under every supported client.
- The export cursor and markdown manifests hold dedup state. A new name
  makes `--new-only` and incremental export treat all history as new.

### Pinning test

Slice R0 adds the pins before any rename work starts:

- `tests/test_frozen_engine_identifiers.py` holds a table of
  `(path, exact literal)` pairs for every frozen string above. It asserts that
  each literal is present byte-for-byte in its file. It also checks the Python
  constants by value (`trust_gate`, `receipts_cmd`, the cursor path) and checks
  that the receipt writer emits `miseledger_indexing` while the readers accept
  `graphtrail_delta`.
- `engines/evidence-ledger/internal/app/frozen_identifiers_test.go` holds
  golden vectors. It checks `computeEvidenceBundleMAC` for a fixed key and
  payload, `evidenceItemStubID` for a fixed server id, and the export filename
  digest for a fixed tuple, each against a recorded hex value. A changed domain
  string fails this test even if someone also edits the literal table.
- Each frozen literal gets a short comment, `frozen identifier: see
  docs/phase-engine-rename.md`, so a reader meets the reason at the definition.

## Storage locations

### Decision

- **Code graph index:** `<repo>/.brigade/code-graph/code-graph.db`, with its
  lock, WAL and SHM files beside it. The index is per repository, so it stays in
  the repository. Brigade already owns `.brigade/` there, and this removes a
  second top-level hidden directory. The engine keeps its existing ignore
  behavior: it appends `.brigade/code-graph/` to `.gitignore` when that path is
  not already ignored. The managed Brigade gitignore block in `install.py`
  gains the same entry. The index can be rebuilt from source, so the legacy
  read can expire on schedule.
- **Evidence:** `<data>/brigade/evidence/evidence.db` (with the MAC key file
  in the same directory), `<config>/brigade/evidence/config.toml` and
  `<cache>/brigade/evidence/`. The evidence archive belongs to the user and
  spans repositories, so `.brigade/` in one workspace is the wrong home. The
  roots stay the engine's current ones (`XDG_*_HOME` with the same fallbacks
  as `paths.go`). Only the leaf changes, so this rename adds no
  platform-specific root changes. Using `brigade/` matches the managed install
  root that `component_paths` already uses.

### Resolution rule

The Python helper and both engines implement one rule:

1. An explicit location wins: `--db`, then the canonical environment variable,
   then the legacy one. Canonical and legacy variables that are both set to
   different values are refused, which matches `component_bins` today.
2. The canonical location wins when it exists.
3. The legacy location is used in place when it exists and the canonical one
   does not. Writes go to the legacy location too. Nothing is copied or moved.
4. Otherwise, the canonical location is created.

When both exist, the canonical location wins. It is what v0.33 will read, so
choosing it now avoids a second silent switch at removal time. `brigade
doctor` reports WARN with both paths and their modification times. Nothing is
merged or deleted. For evidence, the database, MAC key and config resolve as
one directory, never one file from each. While the legacy directory exists,
writers never create the canonical directory, so both can exist only after a
manual copy or a downgrade and upgrade cycle (see
[Installer acceptance](#installer-acceptance)).

### Shared helper

`src/brigade/engine_paths.py` returns a small record:

```text
ResolvedLocation(path, source, legacy_present)
source: "explicit" | "canonical" | "legacy" | "new"
```

It exposes `code_graph_db(target, *, env)` and `evidence_dirs(*, env)`.
Python callers pass the resolved path to the engine with `--db`, so Python and
engine decisions cannot drift. `tests/fixtures/engine-paths-cases.json` lists
the resolution cases: neither location, legacy only, canonical only, both,
explicit, conflicting variables. The Python, Rust and Go tests all read that
one file.

## Entry point identity

Each binary takes its identity from the basename of `argv[0]`, with `.exe`
stripped. A legacy name selects the legacy identity. Any other name, including
a renamed copy, selects the canonical identity. Identity controls the version
banner, usage and help text, MCP `serverInfo.name` and the doctor self-check.
A legacy identity also prints one deprecation line on stderr that names the
replacement and the removal release. Stdout and the MCP stream stay clean.

So a v0.30 client that finds `miseledger` on its `PATH` keeps parsing
`miseledger 0.6.0`. A v0.31 client parses both. A hardlink shim named
`graphtrail` behaves like the legacy binary. Python always prefers the
canonical name when it resolves an executable, so Brigade's own runs do not
print the deprecation line.

That last guarantee holds only once R4 resolves canonical names first and R5
installs canonical executables. Before then, Brigade finds only the legacy
executable, and a stderr line would reach every relayed command and every
PowerShell 5.1 caller that runs with `$ErrorActionPreference = 'Stop'`, where
redirected native stderr becomes a terminating `NativeCommandError`. So the
deprecation line ships in R5, not in the engine identity slices. Until then
the legacy entry point's stderr stays byte-identical to v0.30.

The same rule covers every other legacy name. The legacy environment
variables in the class table (both engine variables and the `*_BIN`
overrides), the legacy config key and the hidden CLI aliases print nothing on stderr until
R5. R2 shipped a stderr warning for the two engine variables, and R4 removed
it. Conflicting old and new values are still refused. Until R5, `brigade
doctor` reports legacy use instead.

## Slices

Dependency order:

```text
R0, R1 (parallel)
  then R2, R3, R4 (parallel)
  then R5
  then R6
R2b, R3b: any time after R0
```

R0 and R1 touch different files and run in parallel. R2, R3 and R4 start
once both have merged, and run in parallel. R2b and R3b are mechanical and can run any
time after R0, but each has to rebase over R2 or R3 if those merge first. R5
waits for R2, R3 and R4. R6 is last.

### R0: frozen identifier pins and retired-name ratchet

- Files: `tests/test_frozen_engine_identifiers.py`,
  `engines/evidence-ledger/internal/app/frozen_identifiers_test.go`,
  `tests/test_retired_engine_names.py`,
  `tests/fixtures/retired-engine-names-baseline.json`, and comment-only
  edits on the frozen literals.
- Ratchet: the test counts case-insensitive retired-name matches per tracked
  file (`git ls-files`, contents and paths). It fails when a file's count
  rises above its baseline or when a file not in the baseline matches. Counts
  that fall pass without editing the baseline, so parallel slices do not
  collide on it. A script regenerates the baseline only in R6.
- Tests: the two pin tests and the ratchet. Run the ratchet against a
  temporary repository that adds a new retired-name line and confirm it fails.
- Acceptance: all three pass on main. Changing any frozen literal or domain
  string makes a test fail.
- Rollback: revert. No runtime effect.

### R1: `mcp sync` fix and shared path resolver

- Files: `src/brigade/mcp_cmd.py`, `src/brigade/engine_paths.py`,
  `tests/fixtures/engine-paths-cases.json`, `tests/test_mcp_adapters.py`,
  `tests/test_engine_paths.py`.
- Fix (first commit, bug first): recognize the code graph server by family.
  The server name or command basename, with `.exe` removed and lowercased,
  matches any of `component_manifest.family_names("graphtrail")` or
  `family_names("graphtrail-mcp")`. Rename `_without_repo_graphtrail_db_pin`
  to `_without_repo_code_graph_db_pin`. The pin check also treats
  `.brigade/code-graph/code-graph.db` as repo-local.
- Resolver: implements the rule above with no call sites switched yet. Read
  the environment through an injected mapping, as `component_bins` does.
- Tests:
  - First, a failing test: the projection for a `brigade-code-graph` server
    running `brigade-code-graph-mcp --db <repo>/.graphtrail/graphtrail.db`
    drops the pin.
  - The legacy-named projection is unchanged, and remote servers are left
    alone.
  - Every case in the shared fixture resolves as listed.
- Acceptance: the new test fails on main and passes on the branch, and the
  existing `mcp sync` tests pass unchanged.
- Rollback: revert. The resolver has no callers yet.

### R2: code graph engine identity, environment and storage

- Files: `engines/code-graph/src/cli.rs`, `src/main.rs`,
  `src/bin/brigade-code-graph.rs`, `src/bin/graphtrail-mcp.rs`,
  `src/bin/brigade-code-graph-mcp.rs`, `src/mcp.rs`, `src/store/db.rs`,
  `src/store/lock.rs`, `src/store/repo_policy.rs`, `src/store/walk.rs`,
  `src/watch.rs`, `src/query/export.rs`, and engine tests. Python side:
  `src/brigade/code_cmd.py` (usage regex accepts both names) and
  `src/brigade/component_install.py` (smoke messages, and the MCP smoke
  records the reported server name).
- Behavior: entry point identity sets clap's name at runtime, along with the
  banner and `serverInfo.name`. `BRIGADE_CODE_GRAPH_DB` and
  `BRIGADE_CODE_GRAPH_ALLOW_UNSAFE_ROOT` are read first. The legacy variables
  still work, and conflicting values are refused. R2 printed a stderr warning
  for them. R4 removed it, and it returns in R5 with the entry point notice.
  Storage defaults follow the resolution rule. The walker skips both
  `.graphtrail/` and `.brigade/code-graph/`. The ignore check covers the new
  directory. Tool descriptions name the new default path. The DOT graph name
  becomes `code_graph`.
- Tests:
  - The shared path cases, run in Rust.
  - `cli_read_only.rs` checks the banner for each entry point
    (`graphtrail 0.4.0` for legacy, `brigade-code-graph 0.4.0` for canonical)
    and that neither entry point writes a deprecation line yet (see
    [Entry point identity](#entry-point-identity)).
  - MCP `initialize` returns the name that matches the entry point.
  - A repository with only `.graphtrail/` keeps writing there. A fresh
    repository gets `.brigade/code-graph/` and its ignore entry.
  - `GRAPHTRAIL_DB` alone works and warns. Both variables set to different
    values fail.
  - Python: the `code_cmd` usage rewrite handles both names, and setup smoke
    passes with either banner.
- Acceptance: `cargo test` in `engines/code-graph` and the Python selectors
  for `test_issue362_code_evidence_facades.py` and
  `test_component_install.py` pass. A repository indexed by v0.30 still
  answers `brigade code callers` with no reindex.
- Rollback: revert. A repo first indexed by the reverted build has its index
  in `.brigade/code-graph/`, which older builds do not read. `brigade code
  sync` rebuilds it in `.graphtrail/`. No data is lost because the index is
  derived from source.

### R2b: Rust package rename

- Files: `engines/code-graph/Cargo.toml`, `Cargo.lock`, every
  `use graphtrail::` in `src/` and `tests/`, `Dockerfile`,
  `engines/code-graph/station.json` (command only. The station id changes in
  R4).
- Behavior: package and lib become `brigade-code-graph` and
  `brigade_code_graph`. Explicit `[[bin]]` targets keep `graphtrail` and
  `graphtrail-mcp` for the window, and `default-run` becomes
  `brigade-code-graph`. Before merging, check whether a `graphtrail` crate
  exists on the public registry. If it does, its published versions stay
  untouched and the renamed package sets `publish = false` until a separate
  publication decision. The golden fixture under
  `tests/fixtures/golden/mixed/` stays as is. It is test input, and the ratchet
  allows it.
- Tests: `cargo test`, plus a `cargo build` check that all four binaries are
  produced.
- Acceptance: same binaries and outputs as after R2.
- Rollback: revert.

### R3: evidence engine identity, environment and storage

- Files: `engines/evidence-ledger/cmd/brigade-evidence/main.go`,
  `cmd/miseledger/main.go`, `internal/app/app.go` (`cmdVersion` and the run
  entry take the identity), `internal/app/help.go` and the usage strings in
  `watch.go`, `crawl.go`, `migrate.go` and `crawlexport.go`,
  `internal/app/mcp.go`, `internal/app/paths.go`,
  `cmd/miseledger/entrypoints_test.go`. Python side:
  `src/brigade/evidence_runtime.py` (`_normalize_miseledger_version` strips
  either name) and `src/brigade/evidence_cmd.py` (usage regex accepts both).
- Behavior: identity drives the banner, usage, `serverInfo.name`, and the
  `doctor --mcp` check, which accepts either name. The legacy entry point
  writes no deprecation line yet (see [Entry point identity](#entry-point-identity)).
  `ResolvePaths` applies the
  resolution rule to the whole directory set, and the MAC key follows
  `DataDir`. The frozen strings stay untouched, and R0 fails if one of them moves.
- Tests:
  - `entrypoints_test.go` compares the two entry points after normalizing
    the program name and checks each banner.
  - Both entry points resolve the same legacy directory when it exists, and
    the canonical directory on a fresh home.
  - The new build opens an archive created by the v0.30 binary, and an
    existing bundle MAC still verifies.
  - Python: version normalization handles both banners, and `brigade
    evidence status` meets the version floor with either one.
- Acceptance: `go test ./...` in `engines/evidence-ledger` and the Python
  selectors for `test_evidence_cmd.py` and `test_evidence_runtime.py` pass.
- Rollback: revert. A fresh home that already created
  `<data>/brigade/evidence/` keeps that archive after a revert, but the
  reverted build reads only the legacy directory. Doctor on the next forward
  build reports the both-exist warning. No silent merge happens.

### R3b: Go module path rename

- Files: `engines/evidence-ledger/go.mod` and every import of
  `github.com/escoffier-labs/miseledger/...` (about 110 imports in 64 files),
  `Dockerfile`.
- Behavior: none. All packages are under `internal/` or `cmd/`, so no outside
  module can import them.
- Tests: `go build ./...` and `go test ./...`.
- Rollback: revert.

### R4: Python call sites, environment, CLI and JSON compatibility

- Files: the eleven modules listed in [Baseline](#baseline) switch to
  `engine_paths`. Also `component_bins.py` (legacy variable warning and
  canonical-first `PATH` order), `config.py` (`code_graph_delta_timeout_seconds`
  read first, legacy key warned), `cli/work/registration.py` and
  `cli/work/dispatching.py` (`--code-graph-timeout`, `--from-evidence`),
  `cli/receipts.py` (`receipts export evidence`), `evidence_cmd.py` and
  `search_cmd.py` and `operator_cmd/lifecycle.py` (dual-emit JSON keys),
  `managed.py` and `src/brigade/templates/stations/managed-snapshot.json`
  (canonical station ids, legacy ids as aliases), `cursor_user_cmd.py`
  (canonical keys for new entries, owned legacy keys kept), `install.py`
  (managed gitignore entry), and `graphtrail_delta.py` renamed to
  `code_graph_delta.py` with its importers (`aboyeur/__init__.py` facade list,
  `work_cmd/verification.py`, `runs_diff.py`). Snapshot files are renamed
  `code-graph-before.db` and `code-graph-after.db`, and the `cwd` derivation in
  the delta module comes from the resolver instead of `parent.parent`.
- MCP client migration: for owned entries named `graphtrail` or
  `miseledger`, `brigade mcp sync` and the user-level editor installer keep
  the key and point its command at the canonical executable. They print one
  notice that names the canonical key and the tool-permission patterns to
  update (for example `mcp__graphtrail__*` to `mcp__brigade-code-graph__*`).
  A new `--rename-legacy-servers` flag does the key rename now. Entries the user
  wrote themselves are never renamed.
- Warnings: no stderr line in R4, for the reason given under
  [Entry point identity](#entry-point-identity). The one stderr line per
  process per legacy name moves to R5. Doctor reports legacy use now: INFO for
  a legacy environment variable or config key in v0.31 (WARN in v0.32), WARN
  for conflicting old and new variables, and WARN with both paths and their
  modification times when both index or both evidence layouts exist.
- As built:
  - `brigade mcp sync` projects only the catalog in `.brigade/mcp.json`, which
    Brigade writes but does not seed with engine servers. A catalog entry
    under a legacy engine key keeps that key, and the sync output carries the
    rename note. With `--rename-legacy-servers --write`, the catalog key is
    renamed in the same projection transaction as the provider writes, and the
    pristine provider entries Brigade owns under the old key are removed.
    Edited or unowned provider entries are left alone. The flag refuses
    `--name`. The old entry is removed only in a harness where the canonical
    entry installs. If the canonical key conflicts in any planned harness, the
    catalog key is not renamed, nothing changes for that server, and sync
    reports the conflict and exits 1. Cleanup after the catalog rename comes
    from the catalog and ownership state. When the catalog has the canonical
    key and not the legacy one, a later `--rename-legacy-servers` run prunes the
    pristine legacy entry Brigade owns in each harness it covers. Each harness
    can therefore be renamed in its own `--harness` run. A plain sync points at
    such leftovers with a note. A renamed entry inherits the legacy entry's
    provider-native settings and ownership provenance, so adopted Codex or
    OpenCode auth survives the rename. The other adapters keep no native
    settings. A catalog that holds both the legacy and the canonical key gets
    a note on every sync, and a requested rename exits 1 for it. The catalog entry's command is not rewritten: the catalog is the
    operator's text, and before R5 the canonical executables are not installed.
  - The Cursor user installer writes canonical keys for new entries and keeps
    the legacy keys it already owns, with commands resolved canonical name
    first. Its pre-setup fallback stays the bare legacy name until R5. It
    prints the rename notice on stdout, never stderr. With
    `--rename-legacy-servers` it chooses the key per engine. It moves a
    pristine owned legacy entry to the canonical key when that key installs.
    An edited legacy entry, or a canonical key held by a foreign entry, keeps
    the legacy key as the desired key and reports a conflict.
  - Station ids in `managed.py`, both engine `station.json` files and the
    snapshot are canonical. The snapshot records for the two engines are
    copied from the in-tree `station.json` files with `kind: in-tree`
    provenance. `managed.resolve` accepts the legacy ids through
    `COMPONENT_FAMILIES`.
  - The legacy `receipts export` spelling is listed as deprecated in
    `docs/command-inventory.md`.
- Tests:
  - Every switched call site runs with legacy-only, canonical-only and both
    layouts, parametrized from the shared fixture.
  - Legacy CLI aliases work, stay out of `--help`, and print nothing on
    stderr until R5.
  - JSON outputs carry both keys with equal values.
  - Station lookup works by either id.
  - MCP sync keeps owned legacy keys, renames them with the flag, and leaves
    user-authored entries alone.
  - `test_evidence_branded_surface.py` still passes.
- Acceptance: the focused selectors for each touched module pass, and so do
  `scripts/managed_snapshot.py --check` and the command inventory check.
- Rollback: revert. Persisted config written with the canonical key is
  ignored by the reverted build, which falls back to the default timeout. That
  degrades a setting and loses no data.

### R5: release, manifest, installer and provenance

- Files: `src/brigade/component_manifest.py` (`KNOWN_COMPONENT_IDS` become
  canonical. Legacy keys stay readable through `COMPONENT_FAMILIES`),
  `src/brigade/component_state.py`, `src/brigade/component_install.py`
  (shim materialization and rollback), `scripts/generate_component_manifest.py`,
  `scripts/verify_component_manifest_provenance.py`,
  `docs/component-manifest-v1.schema.json`, `docs/component-manifest-policy.md`,
  `src/brigade/templates/components/manifest-v1.json`,
  `scripts/published-artifact-acceptance.py`,
  `scripts/windows-native-acceptance.ps1`, `.github/workflows/publish.yml`,
  `.github/workflows/ci.yml`, `engines/*/Dockerfile`.
- Manifest transition: the writer emits canonical keys and executables from
  v0.31. `wire_id` still rejects a manifest that carries both spellings, so
  no release ever carries both. Readers accept either spelling through v0.32.
  That keeps three cases working: v0.31 reading an older stable manifest
  under `--allow-compatible-stable-manifest`, v0.31 reading a v0.30
  `installed.json`, and v0.32 reading v0.31 state. Signed releases are not
  touched. Each release publishes and attests its own manifest. Earlier
  manifests and attestations stay as published, and the bundled template must
  still match the release manifest byte for byte, as the npm preparation
  check requires.
- Assets: v0.31 and v0.32 keep building and attesting both asset name sets,
  as #2148 does today. The manifest pins only the canonical assets. The
  legacy assets stay for direct downloads and scripts.
- Shims: after verifying and installing a canonical executable, `brigade
  setup` creates a hardlink under each legacy name in the managed bin
  directory. On Windows, or across volumes, it falls back to a verified copy.
  The shim list comes from `COMPONENT_FAMILIES`, so `installed.json` needs no
  new field. `--rollback` and the next setup remove shims they created.
  Entry point identity makes each shim behave like the legacy binary.
- Deprecation line: this slice turns on the legacy entry point's one stderr
  line, because it is the first slice where Brigade itself no longer invokes
  the legacy name. The Windows acceptance helper already treats stderr as
  diagnostics and fails only on the exit code. The stderr notices for legacy
  environment variables, the legacy config key and the legacy CLI aliases
  land in the same slice. Before turning them on, move the in-repo callers
  that still use legacy spellings, for example the legacy `receipts export`
  spelling in `scripts/windows-native-acceptance.ps1`, or the notices reach
  the same PowerShell 5.1 callers.
- Tests:
  - A manifest with canonical keys round-trips.
  - A legacy-keyed v0.30 manifest and state still load, and a mixed-key
    manifest is rejected.
  - Shims exist after setup, report the legacy banner, and go away on
    rollback.
  - Provenance verification passes against a manifest from the updated
    generator, and `test_published_artifact_acceptance.py` covers both asset
    sets.
- Acceptance: everything in [Installer acceptance](#installer-acceptance)
  passes on Linux in CI, and the Windows acceptance script passes on the
  Windows runner.
- Rollback: before a release, revert the PR. After a release, ship a patch
  release whose writer goes back to legacy keys. Every v0.31 reader accepts
  both spellings, so state written by either build still loads.

### R6: docs, skills, templates and the retired-name guard

- Files:
  - `docs/**` except history, `README.md`, and the engine READMEs and guides.
  - `docs/wiring-graphtrail-miseledger.md` renamed to
    `docs/wiring-code-graph-evidence.md`, with a one-line stub at the old
    path until v0.33.
  - `src/brigade/templates/skills/**` and `plugins/brigade/skills/**`, kept
    in sync by `scripts/sync_plugin_skills.py`.
  - `src/brigade/fixtures/paired-brigade-work/**`, the harness hook templates
    under `src/brigade/templates/*/hooks/`, and `registry/skills/**`.
  - `evals/memory-retrieval/**` prompts. Memory cards stay as written.
  - `.content-guard-baseline.json` entries for edited lines.
  - `CHANGELOG.md` (one Unreleased entry with the schedule) and the ROADMAP
    status line.
- Guard decision: CI blocks new uses of the retired names. The rule lives in
  a repository test (`tests/test_retired_engine_names.py`), not in the
  content guard. The content guard ships to users as a privacy and leak
  scanner, and a rule about this repository's retired names has no meaning in
  their repositories. R6 replaces the R0 count baseline with
  `tests/fixtures/retired-engine-names-allowlist.json`. Each entry gives a path
  glob, a reason (`frozen`, `shim`, `history`, `fixture`), and, for `frozen`
  and `shim` entries, the exact allowed literals. A match outside the
  allowlist fails. `shim` entries carry `"remove_in": "0.33.0"`. A version
  check fails once the package version reaches 0.33.0 while a `shim` entry
  remains, so removal cannot be forgotten.
- Tests: the guard against a temporary repository, plus the skill sync,
  managed snapshot and template profile checks.
- Acceptance: the guard passes, `brigade evidence --help` and `brigade code
  --help` name no retired binary, and the content-guard CI job passes with
  the updated baseline.
- Rollback: revert. Docs only, plus the test.

## Deprecation schedule

| Release | State |
|---|---|
| v0.31 | Canonical names everywhere new. Every legacy name in the class table works and prints a deprecation line. Doctor reports legacy use as INFO. |
| v0.32 | No new removals. Doctor reports legacy use as WARN and names v0.33. `mcp sync` notices repeat once per run. |
| v0.33 | Removal tasks below. The window can close earlier only with an owner decision, v0.33 is the latest removal release. |

Removal tasks for v0.33, one pull request per engine plus one for Python and
release:

1. Delete the `graphtrail` and `graphtrail-mcp` `[[bin]]` targets and
   `engines/evidence-ledger/cmd/miseledger`. Remove the legacy identity branch,
   the legacy `serverInfo.name` and the deprecation lines.
2. Stop building the 15 legacy-named release assets. Remove them from
   `publish.yml`, both acceptance scripts and the provenance script. Earlier
   releases keep theirs.
3. Remove the legacy entries from `COMPONENT_FAMILIES` reading paths,
   `ENV_OVERRIDES` and `_LEGACY_RELATIVE`. A legacy-keyed `installed.json`
   is then treated as absent, and doctor tells the user to run `brigade setup`.
   Setup stops creating shims and deletes the shims it created earlier.
4. Remove `GRAPHTRAIL_DB` and `GRAPHTRAIL_ALLOW_UNSAFE_ROOT` reads.
5. Stop reading `.graphtrail/graphtrail.db`. Doctor reports INFO "legacy
   index ignored. Run `brigade code sync`". The old directory is never
   deleted, and the walker keeps skipping it so a stale database is never
   indexed.
6. Evidence legacy directories: as decided under open question 1.
7. Remove the CLI aliases (`receipts export miseledger`, `--from-miseledger`,
   `--graphtrail-timeout`) and the `graphtrail_delta_timeout_seconds` read. A
   leftover key produces the existing unknown-key warning.
8. Drop the legacy JSON keys and bump each affected output schema id to `v2`.
9. Drop legacy station id aliases from `managed.py` and the snapshot.
10. `brigade mcp sync` renames owned `graphtrail` and `miseledger` keys by
    default and prints the tool-permission hint. User-authored entries are
    still left alone.
11. Remove the stub at `docs/wiring-graphtrail-miseledger.md`.
12. Delete the standalone engine release tooling that root CI never runs:
    `engines/evidence-ledger/.github/workflows/release.yml`, its validator and
    `testdata/workflows/release/*.yml`, `install.sh` and
    `scripts/bootstrap_local.sh`, plus `engines/code-graph/.github/workflows/*`.
    First confirm that no root workflow or test invokes them. Rename instead
    if one does.
13. Delete every `shim` entry from the retired-name allowlist. The version
    check from R6 enforces this.

## Installer acceptance

R5 adds these as integration tests that run against temporary homes and
repositories, never a real home directory. The fixture binaries are the
engines built from the same commit, plus v0.30.0 release assets fetched and
verified against their published checksums.

Fresh install (v0.31 CLI, empty home):

- `brigade setup` installs canonical executables and legacy hardlinks.
  `installed.json` has canonical keys.
- `brigade code sync` in a new repository creates
  `.brigade/code-graph/code-graph.db` and an ignore entry, and no
  `.graphtrail/`.
- The first evidence write creates `<data>/brigade/evidence/` with the
  database and MAC key, and no `miseledger/` directory.
- MCP `initialize` reports `brigade-code-graph` and `brigade-evidence`.
  Invoking a legacy shim reports the legacy name and writes one stderr line.
- `brigade doctor` reports no WARN or FAIL from these components.

Upgrade from v0.30 data:

- Seed a home with a v0.30 `installed.json`, a repository with
  `.graphtrail/graphtrail.db`, and a legacy evidence archive that holds
  bundles with MACs, trust reviews and stub ids. Add signed verify receipts
  (one with `graphtrail_delta`, one with `code_graph_delta`), an export cursor,
  and an editor MCP config with owned `graphtrail` and `miseledger` entries.
- After upgrading the CLI and running `brigade setup`, the canonical
  executables are installed and the state is rewritten with canonical keys.
- No canonical code graph or evidence directory is created. Code graph
  queries answer from the legacy index. `brigade evidence show` verifies the
  old bundle MACs, trust capability checks pass, and stub ids are unchanged.
- `brigade receipts verify` passes on every seeded receipt. `brigade receipts
  export evidence --new-only` exports nothing already in the cursor.
- `brigade mcp sync` keeps the owned keys, points them at canonical
  executables, and prints the rename hint. The seeded files that must not
  change (receipts, cursor, MAC key) are byte-identical after the run.

Downgrade from v0.31 to v0.30:

- The v0.30 client treats the canonically keyed `installed.json` as missing.
  It does not crash. Running v0.30 `brigade setup` reinstalls the legacy
  executables. This test pins that behavior so a later change to the state
  file cannot break it.
- Data that v0.31 used in legacy locations stays readable by v0.30.
- Data that a fresh v0.31 install created in canonical locations is not
  visible to v0.30. This is documented in the release notes. When the user
  upgrades again, doctor reports the both-exist warning, and nothing is merged.
- `brigade setup --rollback` within v0.31 restores the previous state and its
  shims.

## Risks

- **External scripts parse the banner.** A script that runs
  `brigade-code-graph --version` and expects `graphtrail` breaks in v0.31.
  Scripts that call the legacy names keep working until v0.33. The release
  notes call this out.
- **Tool-permission lists keyed by MCP server name.** Renaming a server key
  breaks tool allowlists in the user's clients. Brigade cannot edit those
  lists. For that reason the key rename is opt-in during the window, and
  v0.33 prints the patterns to change.
- **Both locations exist after a downgrade cycle.** Writes made under v0.30
  after a fresh v0.31 install are hidden behind the canonical location. Doctor
  makes this visible. Automatic merging is out of scope until data migration
  has recovery proof.
- **Hardlinks across volumes and on Windows.** The copy fallback doubles disk
  use for three binaries and needs its own checksum check after copying. The
  Windows acceptance script covers it.
- **Parallel slices and the baseline.** The R0 ratchet fails only on growth,
  so slices that lower counts do not conflict. Slices that add a needed legacy
  reader (R2, R3, R4) can hit growth in their own files. Each such slice
  raises only its own entries, and review checks that the added lines are
  compatibility code.
- **Content-guard fingerprints.** Editing a line recorded in
  `.content-guard-baseline.json` changes its fingerprint, and the pre-push hook
  blocks it. R6 updates the baseline in the same commit.
- **Registry coordinates.** If the Rust crate was ever published under the
  old name, renaming the package must not publish or yank anything. R2b checks
  this first.

## Open questions

1. **Evidence data directory after v0.33.** The code graph index can be
   rebuilt, so its legacy read can end on schedule. The evidence archive
   cannot be rebuilt: it holds imported evidence, trust reviews and the MAC
   key. Ending the legacy read in v0.33 with no migration would strand
   existing archives, which the ROADMAP rules out. Options:
   - (a) Keep reading the legacy evidence directory past v0.33 until a data
     migration with recovery proof ships, as the one exception to the
     two-release cap.
   - (b) Schedule that migration for v0.32 so the cap holds.
   - (c) Freeze the evidence directory name forever as a storage identifier,
     so there is no second location at all.

   Recommendation: (a). It keeps the no-migration rule for v2, it never
   strands data, and it limits the exception to one durable store. Option (c)
   is simpler but leaves the retired name in every user's data directory for
   good.
