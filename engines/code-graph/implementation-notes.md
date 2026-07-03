# GraphTrail implementation notes

Running log of decisions/deviations not captured in the spec. Newest first.

## Audit slice 8 MCP navigation (2026-07-03)

Implemented the agent-navigation slice on `feat/mcp-agent-navigation`.

Decisions:
- `search` now accepts an optional path filter in both CLI and MCP. The filter stays in SQLite over `symbols.file_path`, matching exact file paths, descendants under a directory prefix, or a path fragment.
- Added `neighbors <path>` to the CLI and `file_neighbors` to MCP. The query returns one row per adjacent file with incoming and outgoing cross-file call-edge counts.
- Added MCP `repos`. It reports metadata for the resolved default DB and, when `roots` is supplied, scans each root and its immediate child directories for `.graphtrail/graphtrail.db`.
- `stats` now returns a typed JSON object with `synced_at`, `tool_version`, and `language_files` in addition to the existing table counts and schema version.

Verification during implementation:
- `brigade work verify run --target . --command "cargo test query::"`
- `brigade work verify run --target . --command "cargo test --test mcp"`

## Phase 2 import-aware resolver review fixes (2026-07-02)

Addressed reviewer findings from the Slice A/B implementation pass.

Decisions:
- Import resolution now has three states: no matching import, resolved import, and matched-but-unresolved import. Matched-but-unresolved imports return no edge and do not fall through to global bare-name fanout.
- Relative TypeScript/JavaScript/Python module paths are normalized component-by-component, so `../util` from `src/app/caller.ts` maps to `src/util.*`, not `src/app/util.*`.
- Python source files also resolve bare local modules such as `from b import helper` to `b.py`; this is limited to `.py` callers to avoid treating package imports in other languages as local files.
- Rust grouped `use` imports are expanded through the same single-import parser, covering `use crate::factory::{build as make};`.
- `Cargo.toml` now sets `default-run = "graphtrail"` so `cargo run --release -- sync ...` selects the CLI binary even though `graphtrail-mcp` also exists.

Final acceptance numbers:
- Before review fixes, immutable read of `~/repos/brigade/.graphtrail/graphtrail.db`: `files=262 symbols=4581 edges=29141 imports=1583`.
- After stage 2 review fixes, forced release sync of `~/repos/brigade` into `/tmp/p2-stage2-brigade-20260702-175348.db`: `files=262 symbols=4581 calls=9580 imports=2481 deleted=0`, with DB rows `edges=9534` and `cross_file_edges=3767`.
- The Python repro row is present in the Brigade sync DB: `src/brigade/cli/handoff.py|dispatch|268|src/brigade/handoff_cmd.py|lint`.
- The Rust MCP repro row lives in GraphTrail, not Brigade. A forced release self-sync into `/tmp/p2-stage2-graphtrail-20260702-175348.db` produced `src/bin/graphtrail-mcp.rs|main|19|src/mcp.rs|serve`.
- The exact default DB sync command reached the CLI but failed under this sandbox with SQLite error 14 because writes outside `~/repos/graphtrail` and `/tmp` are blocked. The `/tmp` DB run used the same source root and release binary.

Concrete edge changes from the Brigade smoke diff:
- Before wrong: `tests/test_work_cmd_backup.py:test_work_backup_init_status_doctor_and_json` line 33 fanned out to many `_write_json` symbols, including `src/brigade/aboyeur.py:_write_json` and `src/brigade/research/registry.py:_write_json`. After right: line 33 resolves only to `tests/work_cmd_test_helpers.py:_write_json`.
- Before wrong: `src/brigade/cli/work.py:dispatch` line 698 fanned out to unrelated `status` functions across `center_cmd.py`, `daily_cmd.py`, `dogfood_cmd.py`, and others. After right: no edge is emitted for that qualified call unless the import/module target can be proven.
- Before wrong: `src/brigade/work_cmd/session.py:brief` emitted repeated edges to unrelated `src/brigade/context_cmd.py:_short` at many call lines. After right: those `_short` fanout edges are gone; the remaining precise edge is `brief -> src/brigade/work_cmd/session.py:_brief_payload` at line 1168.

## Audit slices integration (2026-07-02)

Integrated the stage 1 sync, read-only query, context rendering, and MCP protocol slices together.
No git merge conflicts were present on `fix/audit-slices-sync-mcp`; the integration point was making
the combined behavior pass the full Rust checks.

Decisions:
- Incremental sync keeps unchanged repositories cheap: it skips parsing and file row rewrites, but
  still commits a meta-only transaction so `synced_at` reflects the latest successful sync attempt.
- Cross-file fallback call resolution sorts candidates by `(file_path, symbol_id)` before applying
  the existing cap, so ambiguous fallback edges are stable across filesystem traversal order.
- Query-style CLI commands and the MCP server use read-only SQLite opens. An earlier draft used an
  `immutable=1` URI for clean DB files to avoid sidecar creation, but immutable connections skip
  locking entirely and these DBs are rewritten by a 15-minute background sync (and the no-op meta
  write above means EVERY sync now writes), so a concurrent write could serve torn reads. Reverted
  to plain `SQLITE_OPEN_READ_ONLY` everywhere; the read-only test ignores SQLite's own `-wal`/`-shm`
  sidecars, which a read-only WAL connection may legitimately (re)create.
- Post-review fix: MCP `db`/`repo` selector args are now type-checked (`-32602` on non-string)
  instead of being silently ignored and falling back to the default DB.
- Context output has one location contract across CLI markdown, CLI plain text, and MCP markdown:
  symbols render as `file:start-end`, and edges render as `source_file:line -> target_file`.
- MCP keeps protocol errors separate from tool execution failures: JSON parse, invalid request, and
  invalid params return JSON-RPC errors; valid tool calls that fail at execution still return MCP
  tool results with `isError: true`.
- Search and context limits clamp before SQLite integer binding, avoiding lossy `usize` to `i64`
  casts on wide platforms.

## Phase 1: module refactor + AST edges (2026-06-20)

Baseline before any change (brigade smoke, `target/release/graphtrail`):
`files=240 symbols=4313 edges=32164 imports=1669`. (Memory's older 4294/31879 numbers
predate brigade's own evolution; compare against this captured baseline.)

Approach: two verifiable chunks so the smoke delta is attributable.
- **Chunk 1 (pure split):** move `src/main.rs` verbatim into `model.rs`, `cli.rs`,
  `extractors/{mod,common,python,typescript}.rs`, `store/{mod,db,schema,sync}.rs`,
  `query/{mod,search,graph,context,stats}.rs`, `lib.rs`, thin `main.rs`. Behavior identical
  (still regex edges). Smoke numbers MUST equal baseline exactly.
- **Chunk 2 (AST edges):** replace regex `collect_calls` + regex import scanners with a single
  tree-sitter AST pass per file (symbols + imports + calls from one traversal). Same-file-first
  call resolution. Add `PendingCall.source_file`. Drop `regex` dep. Numbers shift (expected).

Decisions:
- Renamed `SymbolLanguage` enum -> `Lang` in `model.rs`, with `db_label()` returning the
  on-disk language string ("python"/"typescript") to keep the DB byte-compatible (.js/.jsx/.ts/.tsx
  all still map to "typescript").
- Added `lib.rs` so tests + a future MCP binary can use the crate API; `main.rs` is a 3-line entry.
- `SCHEMA_VERSION` const added in `store/schema.rs` (no on-disk change yet; the `meta` table lands in Phase 2).
- Tests relocated beside their code: python/typescript extractor tests -> `extractors/*.rs`;
  `fts_query` test -> `query/search.rs`.

### Chunk 1 result (commit 5a3fef3)
Pure split compiled first try; 3 tests pass; brigade smoke IDENTICAL to baseline
(`files=240 symbols=4313 edges=32164 imports=1669`) confirming behavior preserved.

### Chunk 2 result (AST edges)
Replaced regex `collect_calls` + regex import scanners with one `LangSpec`-driven AST pass
(`extractors/common.rs::extract_with`). `regex` dropped from `[dependencies]`.
- New brigade smoke: `files=240 symbols=4313 edges=26646 imports=1471`.
  - `files`/`symbols` UNCHANGED (symbol layer untouched) = no regression.
  - `edges` 32164 -> 26646: AST excludes regex false positives (keyword-paren, names inside
    strings/comments) and same-file-first resolution trims homonym fan-out. Higher precision.
  - `imports` 1669 -> 1471: AST no longer matches `import`/`from` text inside docstrings/strings;
    multi-name `import a, b` and `require()` are now counted. Net more correct.
- **Behavior change worth noting:** the old regex `collect_calls` attributed a call to the FIRST
  symbol whose line-range contained it = the OUTERMOST enclosing symbol (parent class), a quirk of
  pre-order `Vec::find`. The AST pass attributes to the INNERMOST enclosing symbol (the actual
  method/function) via the frame stack. This is more correct and shifts some `source` ids.
- Recursion produces a self-call PendingCall but is dropped at resolution (`target == source_id`),
  same net effect as the old `target == source.name` skip.
- 11 unit tests + `tests/resolution.rs` (same-file-first) pass; clippy clean; fmt clean.

## Phase 2: versioned JSON + meta (commit 28712fb)
- `meta(key,value)` table; `store::meta` upsert/read + `write_sync_meta` (schema_version,
  tool_version, synced_at) inside the sync transaction.
- `ContextPack` and `stats` JSON now carry `schema_version`. `tests/json_schema.rs` is the contract.
- Kept array-shaped outputs (search/callers/...) unchanged to avoid breaking the CLI contract;
  versioning rides on the pack + stats + the meta table.

## Phase 3: read-only MCP server
- Decision: NO `rmcp`/`tokio`. Hand-rolled newline-delimited JSON-RPC 2.0 in `src/mcp.rs`
  (zero new deps, no async runtime) keeps the sidecar small and dependency-light, matching the
  project's non-goals. `handle_request` is pure and unit-testable; `serve` is the stdio loop.
- `src/bin/graphtrail-mcp.rs` resolves the db from `--db`/`--db=`/`GRAPHTRAIL_DB`/default and opens
  it with `open_read_only` (`SQLITE_OPEN_READ_ONLY`) so the server cannot mutate the graph.
- Tools: search/callers/callees/impact/context/stats. `tests/mcp.rs` covers initialize echo,
  notification-no-response, tools/list, tools/call (success + unknown), unknown-method error.
- Verified end-to-end by piping a real JSON-RPC session into the binary against a brigade db.
- NOT auto-registered in ~/.claude.json: registration is repo-specific (needs a --db) and editing
  the live Claude config mid-session is risky. README documents the mcpServers snippet instead.

## Phases 4-6: integration adapters
Contracts mapped from the live systems first (code-search-api FastAPI, brigade handoff markdown,
miseledger Go SQLite). Design choices:
- **Phase 5 (Brigade)** is built in, no feature gate: `query::context::render_markdown` + a
  `context --markdown` flag emit a Brigade-friendly markdown context pack. Self-contained; Brigade
  consumes markdown handoffs, so a clean markdown pack drops straight into an evidence section.
- **Phase 4 (Code Search)**: pure `query::blend::blend` (always built + unit-tested) combines
  embedding hits (per file) with each symbol's normalized call-edge degree. The HTTP client lives in
  `adapters::codesearch` behind the `codesearch` feature (adds optional `ureq`). `blend` CLI command
  is feature-gated. Response parsing/dedup is unit-tested against the documented payload; a live call
  needs CODE_SEARCH_API_KEY (the running service is key-protected; key not in workspace .env).
- **Phase 6 (MiseLedger)**: `adapters::miseledger` behind the `miseledger` feature opens the Go
  tool's SQLite db `SQLITE_OPEN_READ_ONLY` and FTS-searches `item_fts` for a term, returning
  evidence items with snippets. `links` CLI command is feature-gated. Smoke-tested live against the
  7.4GB db (returned highlighted snippets for "dispatch"). Note: `items.raw_path` points at session
  transcripts, not project source, so linking is by FTS body match (the symbol name), not raw_path.
- Guardrail honored: default build has NO ureq and NO network (verified via `cargo tree`). 22 tests
  pass across default + all-features; clippy clean both ways.
