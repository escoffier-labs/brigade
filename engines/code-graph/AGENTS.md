# AGENTS.md

Orientation for coding agents working on GraphTrail.

GraphTrail is a local code-graph sidecar. It parses a repository with tree-sitter in a single pass per file, extracts symbols, imports, and call edges into a small SQLite graph under `.graphtrail/`, and answers structural questions (search, callers, callees, impact, context, stats) plus freshness checks (`doctor`), a dry-run `evaluate`, edge lineage (`explain`), graph `export`, and an opt-in foreground `watch` over two surfaces: a CLI (`graphtrail`) and an MCP server (`graphtrail-mcp`). The default build makes no network calls and starts no daemon. Languages supported: Python, TypeScript/JavaScript, Rust, Go.

MCP query connections always use `SQLITE_OPEN_READ_ONLY`. `refresh: true` starts an incremental graph-index write and waits up to 10 seconds before opening the query read-only. If the refresh fails or times out, the query proceeds and appends a `refresh_error` note to its text result. A timed-out worker may finish concurrently with that read-only query. Without `refresh`, query tools do not write the graph.

## Build and test

```bash
cargo build --release        # binaries land in target/release/
cargo test --all-features
```

## CI gate

CI runs the same checks it expects from you. Run them locally before pushing:

```bash
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test --all-features
cargo build --release
```

## Brigade work loop

This repository is Brigade-wired. Read the work brief before editing:

```bash
brigade work brief --target .
```

Run checks through Brigade so the exit code is recorded, then capture the outcome against the skill or card that guided the change:

```bash
brigade work verify run --target . --command "cargo test --all-features"
brigade outcome capture taste --run-id latest --kind skill
```

Replace `taste` with the skill or card used for that verification. After substantial work, write durable findings in the standard Memory Handoff format under `.claude/memory-handoffs/`, then run `brigade handoff lint` before finishing.

## Module map

The code is split into focused modules:

- `model` (`src/model.rs`): shared types.
- `evaluate` (`src/evaluate.rs`): dry-run extraction with zero database writes.
- `watch` (`src/watch.rs`, feature `watch`, on by default): foreground debounced sync-on-change.
- `extractors` (`src/extractors/`): per-language tree-sitter providers plus shared traversal in `common.rs`. Each language is a provider behind the `LangSpec` trait.
- `store` (`src/store/`): database access, locking, metadata, schema upgrades, repository policy, incremental sync, persisted pending calls, edge resolution, and edge lineage (`explain`).
- `query` (`src/query/`): symbol search, graph traversal, context packs, stats, freshness checks, graph diffs, structural health, and affected-test attribution.
- `mcp` (`src/mcp.rs`): JSON-RPC handling plus the MCP tool registry, argument policy, and dispatch.
- `adapters` (`src/adapters/`): optional Code Search and MiseLedger integrations behind cargo features.
- `cli` (`src/cli.rs`): a thin command-line interface.
- Binaries: `src/main.rs` (the `graphtrail` CLI) and `src/bin/graphtrail-mcp.rs` (the `graphtrail-mcp` MCP server).

## MCP smoke test

Build, then pipe newline-delimited JSON-RPC into the server over stdio. It speaks JSON-RPC 2.0 and exposes fourteen tools (`search`, `callers`, `callees`, `impact`, `context`, `stats`, `doctor`, `file_neighbors`, `dead_code`, `cycles`, `affected`, `explain`, `repos`, `diff`).

```bash
cargo run -- init .
cargo run -- sync .
cargo run -- --db .graphtrail/graphtrail.db stats --json
cargo build --release
printf '%s\n%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | ./target/release/graphtrail-mcp --db .graphtrail/graphtrail.db
```

## Conventions

- Keep the default build network-free and small. Network or cross-tool integrations go behind an optional cargo feature (see `codesearch` and `miseledger`), never in the default binary.
- MCP query connections must stay read-only. The single sanctioned graph write starts when a supported query receives `refresh: true`. Preserve the 10-second wait, fail-open `refresh_error` note, and possible overlap between a timed-out worker and its query.
- Schema, JSON output shapes, and MCP tool contracts are stable contracts; breaking changes need a conversation first.
- Each language extractor owns an `EXTRACTOR_FINGERPRINT` constant. Bump that language's fingerprint whenever the extractor can produce different symbols, imports, calls, symbol ids, signatures, containers, body hashes, language labels, or filtering behavior for the same file content. Do not bump unrelated language fingerprints. One exception: a change to the SHARED symbol-id derivation in `extractors/common.rs` affects every language at once and must ship as a schema migration that rewrites ids in place (see the v7 rewrite in `store/schema.rs`), not as four fingerprint bumps.
- No personal details, hostnames, IPs, account IDs, or live auth profiles in code, tests, or fixtures.
- Conventional commits only. No AI co-authorship trailers.

See `CONTRIBUTING.md` for what lands easily and `README.md` for the full design rationale.
