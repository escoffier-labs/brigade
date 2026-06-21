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
