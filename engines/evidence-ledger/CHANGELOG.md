# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases before this changelog was started are on the [releases page](https://github.com/escoffier-labs/miseledger/releases).

## [Unreleased]

### Changed

- Memory projection E2 read-path hardening (#843): soft-tombstone superseded
  content-hash versions (and drop their FTS rows) so each
  `(source, collection, external_id)` has one default-live version; generic
  search/explain/evidence and session search exclude tombstones and non-latest
  rows; latest-live filtering runs inside the bounded FTS candidate CTE so
  pre-E2 superseded rows cannot fill the 1,000-row pool and hide a live hit;
  Markdown export, session list/metadata/preview/transcript/stats, and
  evidence related-item expansion use the same latest-live predicate (including
  pre-E2 duplicate live rows); Markdown export stages under
  `.miseledger-export-stage`, records managed basenames in
  `.miseledger-export-files`, and reconciles obsolete managed `*.md` when
  reusing `--out` after the last live item is tombstoned; `doctor --archive`
  ignores intentional tombstones in `archive_items_missing_fts`; generic
  `show --json` exposes qualified relation targets (`target_source_kind`,
  `target_collection_external_id`, `target_external_id`, nullable
  `target_item_id`) plus live/tombstone state and omits quarantined
  `injection=pending` body/raw unless `--include-untrusted-body` /
  `include_untrusted_body` is set.

### Added

- Provenance read verification Slice 5: search, show, evidence bundles, HTTP, and
  MCP recompute envelope content and materialized raw/artifact hashes, surface
  `integrity_mismatch` plus envelope fields, suppress mismatched snippets and
  bodies, and append one idempotent downgrade event per item/hash/mismatch
  without deleting the row. Direct `miseledger show --forensic-content` can
  reveal a mismatched or synthesized-legacy body with a warning; it never
  changes trust and only reveals a body when injection status is the explicit
  known-safe value `clean` (empty, unknown, and parse-lost statuses block).
  MCP and HTTP have no forensic reveal path. Bundle cache and Markdown keep
  envelope fields and `integrity_omitted`.
- Provenance persistence Slice 2: ingest stamps indexed `provenance.*` projections,
  append-only `provenance_events`, resumable `miseledger doctor provenance backfill`,
  on-read legacy show synthesis, and search/SQL filters for origin, modality, and
  trust label without nested JSON parsing.
- Engine Slice 1a memory projection: native `miseledger crawl memory <workspace>`
  source (`brigade-memory`), qualified cross-source relation targets on
  `miseledger.adapter.v1`, completed-scan soft tombstones scoped to memory,
  source-local `--rebuild`, scan receipts, and doctor/status memory health.

### Changed

- Memory projection E1 hardening (#843): operator-declared RFC 4122
  `memory-<uuid4>` (version 4 + variant bits) from `memory/NAMESPACE`,
  namespace-scoped collection/scan/rebuild, detach/remap failure-preserving
  rebuild that restores prior live IDs/hashes/relations/metadata/events/
  artifacts, post-resolution `unresolved_relations`, #724 content fingerprint
  for duplicate detection only, duplicate explicit-id fail-closed, empty-
  namespace status/doctor dual-read across all `brigade-memory` collections,
  legacy `memory:cards` scoped-rebuild (never tombstoned by namespaced
  crawl), and latest-version selection via database-monotonic
  `items.ingest_seq` for relation resolution plus live health after
  content-addressed card edits (including equal/backward wall-clock
  `updated_at` and ignoring stale outbound unresolved on prior versions).
  v2→v3 migration deterministically backfills `ingest_seq` from prior
  `updated_at` order (stable `id` fallback) so existing multi-version cards
  keep the previously live winner before any crawl. Re-ingesting known
  Text+Summary identity advances `ingest_seq` and reconciles
  metadata/tags/provenance/artifacts/relations so same-text frontmatter
  edits (including receipt-A → receipt-B retargets) project correctly
  without minting a duplicate item or event; direct adapter AlreadyKnown
  re-imports also re-resolve inbound relations onto that restored version.

## [0.6.0] - 2026-07-18

### Added

- `miseledger schedule run|daemon` for repeatable local crawler schedules from a
  small TOML config, plus a schedule smoke script.
- Native Grok session discovery, adapter generation, import, watch, and crawl
  support for `summary.json` and `chat_history.jsonl` under `~/.grok/sessions`.
- Current Cursor conversation ingestion from the read-only
  `User/globalStorage/conversation-search.db` search database, including WAL
  scan tracking and body search. Legacy Cursor Agent JSON remains supported.
- Native Pi agent session JSONL ingestion for `~/.pi/agent/sessions`, including
  `import pi`, `crawl sessions`, watch, and sources discover coverage.
- `sessions list` and `sessions search` (and the `sessionfind` wrapper) accept
  `--project` and `--model` filters backed by stored session metadata.
- Contract tests for all six source-owned adapter exporters: Discrawl,
  Gitcrawl, Slacrawl, Graincrawl, Notcrawl, and Mailcrawl.

### Fixed

- `crawl github` now supports current Gitcrawl releases that expose
  `sync` and `threads --json` but not `export adapter`.
- `crawl telegram` now converts Telecrawl's public `--json messages` output to
  adapter records. This supports installed Telecrawl 0.1.0 builds that do not
  provide an `export adapter` command.
- The Pi adapter now indexes thinking blocks and tool-call arguments so extended
  reasoning and tool use are searchable.

## [0.5.0] - 2026-07-11

### Added

- Missing wrapper tools now fail with a one-line diagnostic naming the binary
  and where to get it, before the archive is opened or touched. Covers crawl
  exporters, `import sourceharvest`, `import stationtrail`, watch dry-runs, and
  OpenCode session export (#19, #23).
- `doctor` reports availability of all external wrapper tools (stationtrail,
  sourceharvest, opencode, and the seven crawler exporters) and supports
  `--json` with structured `wrapper_tools` entries (#20, #21, #23).
- Added a Brigade `station.json` contract for archive doctor checks, bounded
  evidence Markdown, and version conformance. `doctor --help` and
  `evidence --help` now return without opening the archive or creating cache
  state (#24).
- Release assets now carry GitHub build provenance. Verify a download with
  `gh attestation verify <asset> --repo escoffier-labs/miseledger` (#30).
- A redacted Cursor fixture under `testdata/harnesses` exercises the cursor
  adapter the same way the other harness fixtures do (#26).
- A docs-drift CI check runs on docs-only pushes (which the main CI job
  deliberately skips) and fails when the MCP tool docs fall out of sync with
  the registered tools (#31).

### Changed

- Session listing and preview queries use a new collection-leading items index.
  Existing archives pick it up automatically on next open (#25).
- Relation backfill resolves targets through a dedicated
  `items(source_id, external_id)` index (#18).
- Install docs lead with a pinned, checksum-verified path. The mutable-HEAD
  one-liner is a labeled alternative (#28).
- The CLI dispatch and top-level help are generated from one command table, and
  flag parsing is consolidated into shared helpers. Help output and command
  behavior are unchanged (#29).

### Fixed

- `serve` binds its listener before reporting startup. Bind failures no longer
  print an `ok: true` line, `--addr 127.0.0.1:0` reports the actual bound
  address, and shutdown exits cleanly (#27, #32).
- The release workflow no longer fails when the GitHub release for the tag
  already exists. It uploads assets to it instead. The v0.4.0 release shipped
  with no assets for five days because of this failure mode. Assets were
  rebuilt from the tag and re-uploaded (#30).

## [0.4.0] - 2026-07-06

### Added

- `fork` and `diff` let local archives branch into standalone SQLite copies and
  compare added, changed, and removed evidence across archive states (#14).
- `crawl github` and `crawl telegram` wrap `gitcrawl` and `telecrawl` adapter
  exports, so those crawler outputs can stream straight into MiseLedger (#13).
- `prune policy` and `prune --policy` now provide item-level retention for large
  archives. The default policy dry-runs old operational-noise items first, and
  destructive runs require `--apply --export <path>` so matched records are
  written to compressed adapter JSONL before deletion (#12).
- Adapter imports now read `.jsonl.gz` files, including retention prune exports.

### Changed

- `import adapter --source` keeps the override behavior but now warns when the
  override disagrees with the embedded `source.kind` (#13).
- Quickstart and asset docs now cover existing OpenClaw and crawler installs
  plus the StationTrail and SourceHarvest fold-in path.

## [0.3.1] - 2026-07-02

### Fixed

- Multi-term search no longer pegs a CPU for minutes on large archives: FTS
  ranking runs first in a bounded, materialized candidate pool, the relations
  boost applies only to that pool, and relations gained source/target-item
  indexes. A query that previously never returned on a 1.4M-item archive now
  answers in seconds (#9).
- Native imports and `crawl sessions` skip files whose size and mtime match the
  scan manifest (content-hash fallback when only mtime differs), report
  `files_parsed`/`files_skipped`, and persist each file's scan row as soon as
  its records are committed, so interrupted catch-up runs on large archives
  make durable progress instead of restarting from zero. `--since` and `--full`
  bypass the fast path. Dry runs record nothing (#10).
- The OpenCode adapter skips `session_diff` JSON arrays instead of emitting a
  parse warning for every file (#11).

## [0.3.0] - 2026-07-02

MiseLedger absorbs its StationTrail and SourceHarvest exporter siblings: session
logs, files, notes, git history, and crawler exports all flow in through one
binary, with the `miseledger.adapter.v1` JSONL contract unchanged as the
integration surface for external exporters.

### Added

- `miseledger crawl` front door: `sessions`, `docs`, `files`, `repo`, `markdown`,
  `html`, `gitlog`, `json`, `jsonl`, and `adapter` cover what SourceHarvest
  exported, and `discord`, `slack`, `granola`, `notion`, and `gmail` wrap the
  adapter-emitting crawler binaries (discrawl, slacrawl, graincrawl, notcrawl,
  mailcrawl) so their archives stream straight into the ledger.
- Native OpenCode session adapter (`import opencode`, `crawl sessions`, and
  `sources discover` coverage), closing the last session-source gap StationTrail
  covered.
- Cursor adapter, provider exports (`chatgpt-export`, `claude-export`), session
  previews/transcript view, and a browser session finder.
- Redaction classes `paths`, `secrets`, `emails`, `urls`, `hostnames` (plus
  `safe`/`none`/`all` shorthands) on import and crawl, applied to every
  text-bearing adapter field: item text and summaries, tags, collection and
  actor names, artifact text/paths/URLs, links, relation metadata, and raw
  paths.
- `import stationtrail` and `import sourceharvest` accept the retired
  exporters' JSONL output unchanged.
- `Dockerfile` and `.dockerignore` that build the static, CGO-free binary and run
  `miseledger mcp` over stdio, so the MCP server can be containerized for registries.
- Project governance: `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, and
  issue / pull-request templates.

### Performance

- Import fast-paths already-known items, prints progress, and runs SQLite with
  `synchronous=NORMAL`, keeping daily incremental refreshes cheap on multi-GB
  archives.

### Changed

- README now leads with a recorded terminal demo (`docs/assets/miseledger-ledger.svg`,
  reproducible from `miseledger-ledger.cast`): `init`, `import adapter`, `search`,
  and `stats` against a synthetic session.
- README opening now states what / why / how-it-differs in the first three sentences,
  adds a top-of-page Website link, a keyword-rich `What it does` section, a real-output
  proof block, and `Why not something else?` and `What MiseLedger is not` sections.

### Fixed

- MCP stdio server now accepts newline-delimited JSON-RPC (the ratified MCP stdio
  transport used by Claude Desktop, the MCP Inspector, and Glama) in addition to the
  LSP-style `Content-Length` framing. A spec-compliant client previously got a server
  that silently produced no output. The framing is detected from the first message and
  responses match it.
- Commit the synthetic `testdata/exports/*.json` fixtures that an over-broad
  `exports/` `.gitignore` rule had excluded, so `go test ./...` passes on a clean
  checkout. CI and fresh clones were failing `TestCrawlProviderExports` and
  `TestSessionsListAndSearch` on the missing files.
