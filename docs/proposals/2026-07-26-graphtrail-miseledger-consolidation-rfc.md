# RFC: GraphTrail and MiseLedger product and release ownership

Status: draft, awaiting operator decision

Decision owner: operator

Issue: [#352](https://github.com/escoffier-labs/brigade/issues/352)

Date: 2026-07-26

## Decision in one sentence

Choose Option A, full consolidation, keep both archived repositories as
read-only history anchors, and make the existing executable names, MCP server
names, MCP tool names, and data paths stable Brigade contracts.

This is a recommendation, not an approval. The operator checklist at the end
is the decision gate.

## Why this RFC is still a decision

The repository is ahead of the unresolved issue:

- GraphTrail and MiseLedger source histories are already under
  `engines/code-graph/` and `engines/evidence-ledger/`.
- Brigade v0.25.0 already published both engines as one native asset set from
  one Brigade commit.
- The standalone repositories were archived on 2026-07-21.
- The checked-in [Phase 4 policy](../phase-4a-compatibility-and-archive.md)
  marks its archive checklist complete, while sibling issue
  [#365](https://github.com/escoffier-labs/brigade/issues/365) is still open
  with its original unchecked acceptance list.

Those actions create path dependence, but they do not remove the operator's
choice. Options B and C remain possible. They now require an intentional
reversal of part of the shipped migration rather than a choice among three
greenfield layouts.

No matching MiseLedger evidence was available for the historical claims in
this RFC. The evidence used here is the current source tree, release artifacts,
archived repository records, and the linked issue and policy history. The
requested `docs/proposals/2026-07-26-brigadeclaw-phase-1.md` was not present in
the local repository, fetched remote branches, GitHub default-branch code
search, or the visible proposal directory.

## Scope and constraints

This RFC decides source ownership, release ownership, and compatibility
policy. It does not authorize source moves, repository unarchival, repository
archival, command removal, database migration, new issues, or implementation.

All options preserve these invariants:

- GraphTrail remains Rust and MiseLedger remains Go.
- `.graphtrail/graphtrail.db` stays in each indexed repository.
- MiseLedger keeps its XDG data, config, cache, and database locations.
- The two SQLite databases remain separate.
- Existing database schemas are not destructively rewritten for this choice.
- Existing executable and MCP names remain available until a separately
  approved compatibility decision says otherwise.

## Current engine inventory

### GraphTrail

| Property | Current state |
| --- | --- |
| Active source | [`engines/code-graph/`](../../engines/code-graph/) in this repository |
| Historical repository | [`escoffier-labs/graphtrail`](https://github.com/escoffier-labs/graphtrail), archived 2026-07-21 |
| Language and build | Rust 2024 edition, Rust 1.85 minimum; default build includes `watch`, while `codesearch` and the direct `miseledger` adapter are optional features |
| Executables | `graphtrail` and `graphtrail-mcp` |
| Public shipping today | Brigade v0.25.0 builds both executables from the tagged Brigade commit for Linux amd64/arm64, macOS amd64/arm64, and Windows amd64, then pins size and SHA-256 in `component-manifest-v1.json` |
| Old shipping path | Standalone v0.1.0 through v0.4.0 releases and crates.io. Four releases shipped from 2026-06-27 through 2026-07-17. The crate remains unyanked but is maintenance-frozen under the Phase 4 policy |
| Index storage | One SQLite database per repository at `.graphtrail/graphtrail.db` by default |
| Database schema | Schema 7, with files, symbols, edges, imports, pending calls, metadata, and symbol FTS. Some upgrades rebuild edges or rewrite symbol ids |
| Multi-repository selection | Every MCP query accepts `repo` or `db`; `repos` can scan configured roots for repository indexes |
| Write boundary | Query connections are read-only. `refresh: true` may start an incremental sync before a read. The CLI owns explicit `init`, `sync`, and `watch` writes |

The default managed MCP build exposes these 14 tool names:

`search`, `callers`, `callees`, `impact`, `context`, `stats`, `doctor`,
`file_neighbors`, `dead_code`, `cycles`, `affected`, `explain`, `diff`, and
`repos`.

An optional build with the `codesearch` feature adds `semantic_search`.
Brigade's managed release does not enable that feature today. The tool registry
is defined in
[`engines/code-graph/src/mcp.rs`](../../engines/code-graph/src/mcp.rs), and the
storage and selector contract is documented in
[`engines/code-graph/README.md`](../../engines/code-graph/README.md).

GraphTrail also still contains a deprecated, read-only MiseLedger SQLite
adapter. The `miseledger` Cargo feature, `graphtrail context --evidence`, and
`graphtrail links` read MiseLedger's FTS tables directly. They warn and point
operators to Brigade's code and evidence commands. This is the remaining
engine-to-engine schema dependency.

### MiseLedger

| Property | Current state |
| --- | --- |
| Active source | [`engines/evidence-ledger/`](../../engines/evidence-ledger/) in this repository |
| Historical repository | [`escoffier-labs/miseledger`](https://github.com/escoffier-labs/miseledger), archived 2026-07-21 |
| Language and build | Go, with pure-Go SQLite through `modernc.org/sqlite`; no cgo |
| Executables | `miseledger` and the `sessionfind` compatibility executable |
| Public shipping today | Brigade v0.25.0 builds both executables from the tagged Brigade commit for the same five platforms and pins them in the release manifest |
| Old shipping path | Twelve standalone releases from v0.1.0 on 2026-06-03 through v0.6.0 on 2026-07-18, including several patch releases days or hours apart |
| Archive storage | One operator-level SQLite archive at the XDG data path, normally `~/.local/share/miseledger/miseledger.db` |
| Database schema | Schema 1, opened with WAL and foreign keys |
| Other state | Config at the XDG config path, evidence bundles under the XDG cache path, source discovery and scan state in the archive/config, and project or workspace metadata on imported items |
| Per-repository state | MiseLedger does not create one database per repository. Brigade keeps export cursors and temporary adapter exports under each repository's `.brigade/work/` directory |
| Write boundary | MCP search and show paths call `openMigrated()`, so they can run a database migration before reading. `create_evidence_bundle` also writes a cached bundle |

The MCP server exposes these five tool names:

`search_evidence`, `show_item`, `create_evidence_bundle`,
`show_evidence_bundle`, and `list_sources`.

The CLI also owns imports, `search`, `show`, `explain`, evidence bundle
creation, `export markdown`, read-only SQL, loopback HTTP serving, session
list/search, source scans, and retention operations. There is no MCP tool named
`export`; MCP creates and reads cached evidence bundles, while Markdown export
is a CLI surface. The MCP registry is defined in
[`engines/evidence-ledger/internal/app/mcp.go`](../../engines/evidence-ledger/internal/app/mcp.go),
and paths are defined in
[`engines/evidence-ledger/internal/app/paths.go`](../../engines/evidence-ledger/internal/app/paths.go).

The archived v0.6.0 source and the in-tree engine can report the same
MiseLedger semantic version while advertising different MCP argument schemas.
The in-tree `search_evidence` and `create_evidence_bundle` accept
`code_reference`; the archived v0.6.0 source does not. Version comparison alone
cannot prove compatibility.

## Brigade touchpoints and seams

The engines are separate processes and databases, but Brigade already owns
most of their composition and distribution.

| Seam | Brigade ownership today | Duplication or skew risk |
| --- | --- | --- |
| Binary distribution | `component_manifest.py`, `component_install.py`, `component_state.py`, `component_paths.py`, and `component_bins.py` install, verify, record, and resolve the four engine executables | A generated release manifest is unified, while the source tree also retains a standalone fallback manifest. A missing `installed.json` can make diagnostics describe old standalone revisions |
| Command language | `code_cmd.py` and the `search` compatibility group execute GraphTrail; `evidence_cmd.py` and `evidence_runtime.py` execute MiseLedger | Brigade and the engines each expose related command words. Aliases and error behavior can drift even when the process call still succeeds |
| Run context | `aboyeur.py` asks GraphTrail for a task context pack and `evidence_brief.py` asks MiseLedger for an evidence bundle, then budgets and injects both into plan, worker, and synthesis prompts | GraphTrail ranks graph context, MiseLedger ranks evidence, and Brigade ranks the resulting briefs again |
| Code graph receipts | `graphtrail_delta.py` captures before/after graphs; run and verify receipts, `outcome.py`, and `outcome_cmd.py` retain a bounded graph delta | Graph schema changes can break receipt extraction without breaking the MCP tool list |
| Receipt indexing | `receipts_cmd.py` converts Brigade receipts into `miseledger.adapter.v1`, writes `miseledger-export-*` files, keeps `.brigade/work/miseledger-export-cursor.json`, and imports through the MiseLedger CLI | Brigade receipts and MiseLedger evidence items are distinct ledgers connected by a conversion contract. Failed import, cursor advancement, and retry behavior must stay synchronized |
| Automatic evidence flow | `work_cmd/verification.py` captures the outcome and asks the receipt exporter to index new receipts after `--capture` | A verification can pass while evidence indexing is missing or stale. #552 fixed one form of that split |
| Structured code evidence | `schemas/code-reference.v1.schema.json` is produced from GraphTrail delta nodes by Python, placed in adapter records, and matched by the Go evidence engine | One schema crosses Rust output, Python normalization, JSON, SQLite metadata, and Go matching. Tool names can remain stable while arguments or canonicalization skew |
| Direct engine coupling | GraphTrail's optional MiseLedger adapter reads MiseLedger FTS tables without Brigade | It creates a second compositor and binds GraphTrail to an evidence-ledger storage schema |
| MCP and harness config | `cursor_user_cmd.py`, `mcp_cmd.py`, managed station metadata, and component resolution generate or inspect engine commands | Generated absolute paths do not fix hand-written configs, wrappers, hooks, or long-running MCP processes |
| Health and operator views | `doctor.py`, operator lifecycle checks, work briefs, managed snapshots, and station manifests report engine state | Health may report a path binary as usable while the pinned managed component is absent or at another revision |
| CI and release | Path-filtered Rust and Go jobs run engine checks; `publish.yml` builds native assets, creates the manifest and checksums, attests them, publishes the Python package, and runs published-artifact acceptance | One failing platform or language gate can block an unrelated engine or CLI release |

The two apparent evidence ledgers are not the same object:

- Brigade's verify receipts and outcome records answer what ran, what exit code
  it returned, what patch was checked, and how a skill or card scored.
- MiseLedger stores normalized, searchable evidence items and bundles from
  several harnesses and crawlers.

They are converging at the adapter and schema boundary, not by merging
databases. Changes #548 and #552 show the present coupling. The Go engine's
filtered search plan changed on the same day that the Python verification path
began indexing captured receipts automatically. Structured code references
couple them further through shared vectors and exact-match behavior.

GraphTrail is coupled differently. Brigade consumes its CLI and JSON output in
run briefs, context packs, before/after deltas, verification receipts, outcome
records, operator reports, and code commands, but GraphTrail still owns a
small, per-repository database and a stable process boundary. That difference
drives Option C.

## Consumers beyond Brigade

The public dependency count was recorded as zero forks and zero known reverse
dependencies when the Phase 4 window was compressed. Operator migration cost
still exists:

- Codex, Cursor, OpenClaw, and Claude MCP configurations use server names such
  as `graphtrail` and `miseledger`, executable names such as
  `graphtrail-mcp`, and sometimes absolute paths or wrappers.
- Rules files name `affected`, `impact`, `callers`, `search_evidence`, and
  `show_item`. Renaming those tools breaks prescribed workflows even if an
  equivalent tool exists.
- Hooks and runbooks invoke GraphTrail sync, Brigade receipt export, MiseLedger
  refresh/import, and `sessionfind`.
- Existing scripts may call `graphtrail`, `miseledger`, or `sessionfind`
  directly and parse JSON.
- Old installs may still use Cargo, standalone GitHub release URLs, local-bin
  copies, or Go-era install instructions.
- Existing GraphTrail and MiseLedger databases outlive whichever executable
  path opens them.
- An apparently read-only MiseLedger MCP search may migrate the archive before
  reading it, so a binary rollback can still encounter a newer database
  schema.

A 2026-07-26 reference-host audit found all 19 current MCP tool names working,
but the active MCP paths still reached legacy GraphTrail 0.3.0 and MiseLedger
0.5.0 binaries. `brigade version --components` reported the managed engine set
missing, and `sessionfind` was absent. This conflicts with the completed
operator-migration statement in the Phase 4 policy. It proves that keeping tool
names is necessary but insufficient: release ownership also needs a check that
every harness resolves the intended bytes.

## Release train today

Brigade has one public semantic version. `pyproject.toml`,
`src/brigade/__init__.py`, template `_brigade_version` fields, and the bundled
component manifest are checked for version alignment.

The v0.25.0 release workflow:

1. builds Python distribution artifacts;
2. builds `graphtrail` and `graphtrail-mcp` from `engines/code-graph/`;
3. builds `miseledger` and `sessionfind` from
   `engines/evidence-ledger/`;
4. builds each native executable for five platform keys;
5. writes one manifest whose component revisions and source tags identify the
   same immutable Brigade commit;
6. publishes sizes, SHA-256 digests, checksums, attestations, and release
   assets;
7. installs and executes published artifacts by managed absolute path in
   acceptance jobs.

CI keeps feedback partly independent through path filters, but publication is
one gate. The latest public unified release is v0.25.0. The source tree is
0.25.1, so it also contains unreleased engine work.

CLI and native updates are not one filesystem transaction. The updater can
replace the Python package before native component setup completes. Native
installation snapshots and restores component files and state on failure, but
a rerun is still required to converge the CLI and engine set.

`stations/` and engine `station.json` files describe process contracts and
safe probes. Agent Pantry supplies a useful federation pattern:
`pantry_compat.py` probes the separate binary's released semantic version,
enforces a minimum version that contains every Brigade-invoked surface, and
refuses incompatible builds before calling them. Federation would need this
pattern plus immutable component pins and cross-repository artifact contract
tests. A version floor alone would not prevent schema skew.

The sibling Phase 4 policy already prescribes:

- keep `graphtrail`, `graphtrail-mcp`, `miseledger`, and `sessionfind`;
- keep `brigade search` aliases for the `brigade code` commands;
- preserve data paths and schemas;
- use managed absolute paths;
- leave old GraphTrail crate versions unyanked;
- never rewrite the archived repositories' `master` branches;
- unarchive a mirror if a repair requires it.

This RFC does not reopen those protections. It asks which source and release
owner should carry them.

## Option A: full consolidation

Both engines remain in this repository and on Brigade's release train. The
standalone repositories remain archived history anchors.

### Migration from today's state

1. Record the operator's approval and name Brigade as canonical for source,
   issues, binaries, schemas, and releases.
2. Treat the 19 default MCP tool names, four engine executable names, current
   server names, CLI JSON contracts, and data paths as stable contracts. Add
   aliases before any future rename.
3. Audit every configured harness, wrapper, hook, and runbook against the
   managed paths recorded by `brigade setup`. Require
   `brigade version --components` to pass on the reference operator set.
4. Publish the next unified release from the in-tree engine revisions and test
   both direct executable calls and MCP `tools/list`, including argument
   schemas, through the installed absolute paths. Record capabilities as well
   as versions.
5. Remove current standalone-install guidance while retaining read-only
   migration pages, unyanked crate versions, and the one-release manifest
   fallback already documented.
6. Close the state mismatch between #365 and the executed Phase 4 policy only
   after the operator approves this RFC.

### Existing installs and the #365 shim

Managed installs keep the same executable names. Existing MCP server and tool
names do not change. Hand-written paths and stale wrappers must be repointed,
but their command and tool vocabulary survives. Cargo and standalone release
URLs stop receiving fixes. Existing databases open in place.

The #365 shim is the managed binary set itself, plus the `brigade search`
aliases and `sessionfind` compatibility executable. This option should not
start a new rename window. It should keep the MCP tool names indefinitely
unless another RFC establishes aliases and a measured migration.

### Release impact, effort, and fatal risk

- Release impact: one polyglot release, one manifest, one issue queue, and one
  published-artifact gate.
- Effort from today's state: **S**. The source import, release build, manifest,
  and archive work already shipped. The remaining work is decision recording,
  operator-path verification, contract locking, and release cleanup.
- Risk that kills the option: an urgent engine-only fix cannot ship because an
  unrelated Python, Go, Rust, or platform gate blocks the whole Brigade
  release.

## Option B: federation

GraphTrail and MiseLedger return to independent canonical repositories and
release cadences. Brigade pins exact engine releases in its component manifest
and tests their contracts. Brigade remains the public compositor and installer,
but not the engines' source owner.

### Migration from today's state

1. Obtain operator approval to unarchive both public repositories.
2. Reconcile every post-import engine commit from the Brigade monorepo into
   each standalone history without rewriting either `master` branch.
3. Declare the standalone repositories canonical again and stop engine feature
   changes in the monorepo copies.
4. Restore independent Rust and Go CI, security-fix paths, release assets,
   checksums, and immutable tags.
5. Change Brigade's release manifest to pin exact external tags, commits,
   assets, and digests. Do not use `latest`.
6. Add a Pantry-style released-version floor plus artifact contract tests for
   MCP tool names and arguments, CLI JSON, station probes, GraphTrail database
   compatibility, `miseledger.adapter.v1`, structured code-reference vectors,
   and receipt import retry behavior.
7. Keep the monorepo engines as read-only transition copies until the external
   artifacts pass Brigade's published-artifact matrix, then remove duplicate
   source ownership in a separately reviewed change.

### Existing installs and the #365 shim

The installed filenames, MCP server names, tool names, and data paths can
remain unchanged, so a careful federation cutover need not break runtime
calls. Release URLs, provenance, source links, contributor workflows, and
security reporting move back to two repositories. Archived links become live
again. Operators that expect one Brigade commit for all bytes lose that
identity and must read component revisions instead.

The #365 shims remain required and become cross-repository contracts. Brigade
must test them against every pinned engine release before accepting the pin.

### Release impact, effort, and fatal risk

- Release impact: three release authorities. Brigade can ship against existing
  engine pins, while an engine can ship independently and wait for a Brigade
  pin update.
- Effort from today's state: **L**. Both histories must be reconciled, both
  public projects reactivated, release automation restored, and the monorepo
  copies retired without losing post-import work.
- Risk that kills the option: source-of-truth ambiguity returns and a
  cross-repository fix lands in one engine or Brigade without its matching
  contract update.

## Option C: absorb MiseLedger, federate GraphTrail

MiseLedger stays in this repository and on Brigade's release train. GraphTrail
returns to an independent canonical repository and is pinned as a versioned
component.

This is the asymmetric choice supported by the seam inventory. MiseLedger
co-changes with receipt export, automatic verify indexing, evidence briefs, and
the shared code-reference schema. GraphTrail has many Brigade consumers, but
its per-repository database, read-only MCP protocol, and process boundary are
cleaner. Its direct MiseLedger adapter is already deprecated.

### Migration from today's state

1. Obtain operator approval to keep MiseLedger consolidated and unarchive only
   GraphTrail.
2. Reconcile GraphTrail's post-import commits onto the standalone history
   without rewriting `master`, then restore Rust CI and releases.
3. Keep GraphTrail's default MCP and JSON contracts unchanged. Move no
   GraphTrail database.
4. Pin an exact GraphTrail tag, commit, assets, and digests in Brigade. Apply a
   Pantry-style version floor and artifact contract tests.
5. Keep MiseLedger, `sessionfind`, adapter conversion, code-reference vectors,
   and evidence search tests in the Brigade release.
6. Run the published-artifact matrix with external GraphTrail assets and
   in-tree evidence assets before retiring the duplicate GraphTrail source.

### Existing installs and the #365 shim

Executable names, MCP server names, tool names, and data paths can remain
unchanged. GraphTrail source, issue, release, and security links become active
again. MiseLedger links continue to point to Brigade. Operators must understand
that `brigade version --components` reports one external engine revision and
one in-tree engine revision.

The #365 shims remain in Brigade's installer and tests. GraphTrail must also
test the same names before publishing because it owns the external artifact.

### Release impact, effort, and fatal risk

- Release impact: two release authorities. Evidence changes remain atomic with
  Brigade receipt and schema changes; GraphTrail fixes can ship independently.
- Effort from today's state: **M**. One repository and release pipeline return,
  and one external component contract replaces the all-in-tree graph build.
- Risk that kills the option: GraphTrail's run-brief, graph-delta, and
  code-reference consumers evolve often enough that the split creates repeated
  two-release coordination without delivering useful independence.

## Decision matrix

Scores use 1 for poor and 5 for strong. "Maintenance load after" scores lower
ongoing load higher. Criteria are equally weighted.

| Option | Operator disruption | Maintenance load after | Release coherence | Reversibility | Total |
| --- | ---: | ---: | ---: | ---: | ---: |
| A. Full consolidation | 4 | 5 | 5 | 2 | **16** |
| B. Federation | 4 | 2 | 2 | 5 | **13** |
| C. Absorb MiseLedger, federate GraphTrail | 4 | 3 | 3 | 4 | **14** |

Option A loses points on reversibility because new engine work now lands after
the shared import point and public standalone release machinery is frozen.
All three options can preserve runtime names and data paths, so their direct
operator disruption is similar. Option B is reversible in organizational
shape, but reversing into it today causes the largest source and release
migration for maintainers. Option C preserves an escape hatch for the engine
with the cleaner process boundary.

## Recommendation

Choose **Option A, full consolidation**.

The recommendation does not rest only on work already performed. Brigade owns
the contracts that change most often: component resolution, run brief
composition, graph-delta receipts, adapter export and retry, exact code
references, operator health, and published-artifact verification. One source
and release owner keeps those changes reviewable in one patch and testable
against one immutable release manifest.

The two strongest counterarguments are:

1. A single polyglot release gate can delay an urgent GraphTrail or MiseLedger
   fix for reasons unrelated to that engine. Federation gives each engine a
   smaller security and patch-release path.
2. GraphTrail and MiseLedger remain useful outside Brigade. Their stable MCP
   and database boundaries are credible independent products, and consolidation
   commits Brigade to maintaining branded compatibility surfaces for
   non-Brigade consumers indefinitely.

The runner-up is Option C. Flip from A to C if a GraphTrail-only production or
security fix misses a 72-hour release target because an unrelated Brigade or
MiseLedger gate blocks publication. That is evidence that the shared release
train is imposing more delay than its atomic contracts save.

## Operator decision checklist

1. **Are you willing to keep both standalone repositories archived and make
   Brigade the only source, issue, binary, and public release authority?**
   Yes gates Option A; no requires Option B or C.
2. **Must `graphtrail`, `graphtrail-mcp`, `miseledger`, `sessionfind`, and all
   19 current default MCP tool names remain supported without a scheduled
   removal date?** Yes gates the compatibility contract recommended for A and
   C; no requires a separate rename and alias decision.
3. **Can a GraphTrail-only urgent fix wait for Brigade's Python, Go, Rust, and
   platform release gates for up to 72 hours?** Yes supports A; no gates C, or
   B if MiseLedger also needs independent releases.
4. **Will you require every supported operator host to pass
   `brigade version --components` and resolve managed absolute paths before the
   next unified release is called complete?** Yes gates a safe A or C rollout;
   no accepts the current legacy-path skew.
5. **If federation wins, are you willing to reactivate public issue queues,
   security workflows, release automation, and maintainer obligations for the
   unarchived repositories?** Yes permits B or C; no rules them out.
