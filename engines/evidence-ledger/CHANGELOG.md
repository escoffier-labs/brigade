# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Releases before this changelog was started are on the [releases page](https://github.com/escoffier-labs/miseledger/releases).

## [Unreleased]

### Added
- Added `miseledger migrate codex-arguments` command to migrate and truncate oversized or duplicated tool call arguments in the database for existing rows (#1414).

### Security
- Round 6 (#1201): the GOOS=darwin build is repaired without weakening linux. The descriptor-relative `openat`/`unlinkat` implementations are now linux-only, while darwin and the other non-linux unix platforms get a functional pathname-based equivalent: opens carry `O_NOFOLLOW|O_CLOEXEC` relative to the already-validated data-directory path (`O_CREAT|O_EXCL` where creation is required), the validated directory descriptor is `fstat`ed before and after every open/removal and must keep an identical dev/ino pair, and the opened file must be regular, singly linked (nlink 1), and exactly 0600. The residual darwin mutation window between those parent identity checks is pathname-based and exploitable only by a same-uid data-directory writer — a writer outside this engine's model (#1093). Windows keeps failing closed.
- Round 5 (#1201): execution of the verified stationtrail snapshot is now descriptor-based. Previously the snapshot was executed by its pathname after digest verification, so a same-uid process could rename another executable over the cached snapshot entry between verification and exec (the executed inode was no longer the verified one), and the cached `snapPath` let later commands skip every digest check. The verified snapshot is now kept open: its digest is re-read from that open descriptor immediately before each exec, and on linux every exec targets `/proc/self/fd/<fd>` so the kernel executes exactly the verified inode; platforms without descriptor exec fail closed with a typed error instead of degrading to pathname execution.
- Round 5 (#1201): executable-snapshot creation no longer follows a symlinked data directory and no longer falls back silently to the OS temp directory. The snapshot directory is created `mkdirat`-style relative to the validated data-directory descriptor (`O_DIRECTORY|O_NOFOLLOW`, current-uid checked), reopened `O_NOFOLLOW`, chmodded to exactly 0700, and re-verified; an uninitialized or redirected data directory now fails closed instead of hosting snapshots in a TMPDIR-chosen location.
- Round 5 (#1201): evidence bundle MAC key creation no longer returns to the absolute pathname after the safe directory-relative ENOENT — a window in which a swapped-in data directory received the fresh key material or an attacker-planted candidate file was adopted as the create-race winner. One validated data-directory descriptor is held across the entire miss-or-create sequence and creation uses `openat(O_CREAT|O_EXCL|O_NOFOLLOW, 0600)` with an umask-proof `fchmod`; the key file itself is created relative to that held descriptor.
- Round 5 (#1201): stationtrail handle cleanup is descriptor-backed (`unlinkat` against the held snapshot-directory and validated data-directory descriptors), preserves both the pinned-descriptor close error and the removal error instead of discarding one, keeps snapshot state when a removal fails so it can be retried, and surfaces rollback errors from failed materializations instead of suppressing them.
- Trust boundary: these protections model an attacker that can tamper with pathnames (scanner path, PATH entries, snapshot entries) but not one running as the same uid **with write access to the data directory** — such a process can chmod files it owns, wait out validation windows, or displace the data directory between operations, which remains outside this engine's model and is tracked in #1093.
- Cached evidence bundles are now HMAC-authenticated. Each saved bundle carries a MAC over its canonical reference — bundle ID, result item IDs, filters, and `generated_at` — keyed by a random 32-byte key stored 0600 in the private data directory. `evidence show` and bundle listing reject a cached bundle whose MAC is missing or does not match, so a same-UID process can no longer edit cached item IDs or include-flags and have the original bundle ID rematerialize different evidence. The MAC field never reaches clients (the outbound sanitizer drops it). Fixes #1201.
- The evidence bundle MAC key file is validated on every load: it must be a regular file (symlinks refused, opened with `O_NOFOLLOW` where available), owned by the current uid, mode 0600, and exactly 32 bytes (bounded 33-byte read). A file that fails any check is refused with a typed error (`EvidenceMACKeyError`) instead of sealing or verifying bundles with attacker-shaped key material. Residual: a same-UID process that can wait for these checks to pass can still read or replace a valid key — moving scanners to another uid is tracked in #1093. Fixes part of #1201.
- Concurrent first use of the evidence bundle MAC key can no longer generate competing keys. First creation claims the key path with `O_CREATE|O_EXCL`; racers that lose the create race load the winner's key (tolerating, with a bounded wait, only the transient window while the winner is still writing) instead of sealing bundles with a losing key that later fails verification. Fixes part of #1201.
- Search snippets are derived from the verified in-memory item text instead of the FTS row. A modified or stale `item_fts` body can no longer inject snippet wording under an eligible item; ineligible items keep empty snippets. Exact code-reference results keep their server-built snippets (they never came from the FTS row). Fixes #1202.
- Artifact bodies emitted with `--include-artifact-text` must carry a canonical SHA-256 content digest. A missing, unprefixed, malformed, or short `content_hash` is itself an integrity mismatch: the artifact is ineligible instead of shipping its text under a clean item. Well-formed digests are still compared exactly against the normalized artifact text. Fixes #1203.
- The stationtrail capability probe is fail-closed. Timeouts, nonzero exits without a legacy fingerprint, oversized output, malformed JSON, and unrecognizable capabilities documents now block the import; a positively identified legacy scanner (`capabilities` unknown-command fingerprint plus a parseable `stationtrail --version`) proceeds only when the operator has explicitly approved that executable's SHA-256 digest in the owner-protected `stationtrail-approved-digests` file under the data directory (one 64-hex digest per line, optional `sha256:` prefix). Round 2 (#1204): the same executable controls both the unknown-command text and the parseable `--version`, so implicit legacy tolerance was forgeable by the scanner itself; there is no longer any implicit tolerance — an unapproved digest (or an executable that cannot be hashed) blocks the import. Fixes #1204.
- Round 3 (#1204, #1201): the approved-digest allowlist moved from the `MISELEDGER_STATIONTRAIL_APPROVED_DIGESTS` environment variable — readable by any child process, including the scanner being gated — to `stationtrail-approved-digests` under the data directory, validated on every load exactly like the evidence bundle MAC key (regular file with symlinks refused, current-uid owner, mode 0600, bounded read); any malformed digest token refuses the whole list closed, and the environment-variable path is removed entirely. See the README "Legacy stationtrail scanner approval" section for the file's trust boundary.
- Round 3 (#1201): stationtrail resolution is now singular across probes, approval hashing, dry-runs, and imports: the executable is resolved once and opened, and hashing reads through that opened descriptor. Round 4 (#1201): hash-to-exec binding no longer relies on identity metadata, which an in-place rewrite preserves (device/inode on unix) or defeats entirely when both samples land after the rewrite (size/mtime elsewhere): the hashed descriptor bytes are copied once into an immutable private snapshot — fresh 0700 directory under the data directory, `O_EXCL`-created 0500 non-writable file, fsynced, digest verified against the approved digest before use — and every exec (probes, dry-runs, imports, watch) runs the snapshot's absolute path, with the snapshot removed when the handle closes. A swapped PATH entry, replaced binary, in-place rewrite, or a pathname swap between verification and process start can no longer change what executes.
- Round 4 (#1201): the stationtrail approved-digests file and the evidence bundle MAC key are loaded through a directory-relative, symlink-proof open: on unix the data directory itself is opened `O_DIRECTORY|O_NOFOLLOW`, refused unless it is a directory owned by the current uid, and the file is then opened openat-style relative to that handle with `O_NOFOLLOW`; other platforms keep failing closed. Previously `filepath.Join` plus a final-component-only symlink check followed symlinks in parent components, so a redirected data directory could feed both loaders attacker-chosen current-uid 0600 files, and the pathname stayed redirectable between open and read.
- Round 3 (#1201): on platforms where the portable Go stdlib provides neither a race-free no-follow open nor a file-ownership check (non-unix), evidence bundle MAC key load and creation are refused with a typed error instead of silently degrading to a symlink-following, ownership-blind loader; cached-bundle authentication therefore fails closed there until platform primitives exist.
- Evidence CLI, MCP, and HTTP exits now sanitize the complete serialized payload through one last allowlist walker. Ineligible items become `{id?, eligibility_status, reason_code}` — stub `id` is 24 lowercase hex or omitted. Eligible items always serialize `artifacts` as a list (`[]` when none); empty `related`, `results`, `warnings`, and `grouped_by_source` stay present rather than omitted. `grouped_by_source` counts only eligible closed-enum kinds. Request `query`/`filters` are not reflected; cached show regenerates live; a URL content hash cannot authorize swapped artifact text. Fixes #1030, #1031, #1032.
- `miseledger trust review` requires an operator-minted capability handed over stdin (`brigade.authority.capability-handoff.v1`). The MAC is bound to item id, current digest, requested transition, nonce, and expiry. Spent nonces are recorded in `used_capabilities`. `--operator-command` is audit metadata only. A missing capability is refused for every stdin kind, including piped empty input, `/dev/null`, and a PTY. stdin is not an authorization signal. Fixes #1029.

### Fixed
- Bounded reads for engine-side inputs: scanner dry-run stdout (8 MiB), the capabilities/version probe output (64 KiB), the scanner-written summary JSON (1 MiB), and cached evidence bundles (64 MiB) are read through size-checked helpers that reject oversized input before allocation. Oversized or unreadable summary files now fail the import instead of silently continuing without it. Fixes #1205. Round 2: scanner stderr captured for diagnostics is likewise capped at 1 MiB across the dry-run, capability-probe, and import runners (excess is dropped and a truncation marker is appended to the surfaced message), and the MAC key file read is bounded at 33 bytes.

- `import cursor` no longer aborts when `User/globalStorage/conversation-search.db`
  cannot be opened. Relative profile paths and Windows drive paths now use an
  absolute `file:` URI (the previous `file://<first-segment>/...` form made
  SQLite report `SQL logic error: out of memory`). A file that still cannot
  be read is skipped with a counted warning that names the path and the
  statement; prompt history and chat surfaces continue to import. Fixes #1052.
- `import discovered` lists per-source failures but exits 0 when at least one
  source imported; exit 1 is reserved for total failure (no source imported).
  Fixes #1052.
- `import sourceharvest` (and the crawl wrappers that call it) and provenance backfill retry the SQLITE_BUSY family (5, 261, 517) with a bounded total wait, so a concurrent writer no longer fails the crawl or hangs the suite. Exhausted contention names the holder-diagnosis instead of the raw SQLite string. Fixes #1067.
- `archive.Open` restores the pre-#1073 10s global `busy_timeout` so unwrapped command paths keep their contention tolerance. The two retry-wrapped paths bound their own wait via retry count, backoff, and `MaxTotalWait` rather than by shrinking the DSN timeout. `IsBusy` classifies by the SQLite result code (low 8 bits == 5) and cannot be tripped by parenthesized 5/261/517 in subprocess stderr. Fixes #1085.
- Content eligibility is centralized for every body-emitting read surface.
  Search snippets, MCP `search_evidence`, evidence bundles, Markdown export,
  default `show`, session preview, session search snippets, and session
  transcripts now hide bodies unless provenance parses and injection status
  is the validated typed value `clean` on a non-`unknown` / non-`quarantined`
  label. Imports keep stamping `quarantined`/`pending`. `miseledger trust
  review --mark-injection-clean` is the explicit operator path that records
  an injection-status transition to `clean`; moving the trust label alone
  does not make content eligible. Parse errors are blocking; stored digest
  representations are not lowercased to authorize a body. CLI
  `show --include-untrusted-body` / `--forensic-content` remain human-typed
  reveals. MCP and HTTP no longer accept a caller-settable reveal field.
  Routine `miseledger trust review` refuses a stored envelope that fails
  retainable validation instead of silently rewriting a parse-error-grade
  value to `clean`. (#1007, #1009)

### Changed

- Search integrity verification loads item text and provenance in one
  `IN (...)` query keyed by the result ids, instead of one select per
  hit (search limit is up to 200). (#964)
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

- `miseledger trust review --item ID --content-hash DIGEST` upgrades an item to `reviewed` or `verified` only when the supplied digest matches both the embedded envelope `hashes.content` and the recomputed item text. The transition appends one `provenance_events` row. (#587)
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
