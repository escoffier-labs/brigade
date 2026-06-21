# GraphTrail implementation notes

Running log of decisions/deviations not captured in the spec. Newest first.

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
