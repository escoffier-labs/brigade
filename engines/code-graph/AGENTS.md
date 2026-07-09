# AGENTS.md

Orientation for coding agents working on GraphTrail.

GraphTrail is a local code-graph sidecar. It parses a repository with tree-sitter in a single pass per file, extracts symbols, imports, and call edges into a small SQLite graph under `.graphtrail/`, and answers structural questions (search, callers, callees, impact, context, stats) plus freshness checks (`doctor`) over two surfaces: a CLI (`graphtrail`) and an MCP server (`graphtrail-mcp`). MCP queries always run on `SQLITE_OPEN_READ_ONLY` connections; the one deliberate exception is the opt-in `refresh: true` parameter, which runs the same incremental sync as the CLI on a write connection before answering, then serves the query read-only. The default build makes no network calls and starts no daemon. Languages supported: Python, TypeScript/JavaScript, Rust, Go.

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

## Module map

The code is split into focused modules:

- `model` (`src/model.rs`): shared types.
- `extractors` (`src/extractors/`): per-language tree-sitter providers plus shared traversal in `common.rs`. Each language is a provider behind the `LangSpec` trait.
- `store` (`src/store/`): `db`, `lock`, `schema`, `sync`.
- `query` (`src/query/`): `search`, `graph`, `context`, `stats`, `doctor`.
- `cli` (`src/cli.rs`): a thin command-line interface.
- Binaries: `src/main.rs` (the `graphtrail` CLI) and `src/bin/graphtrail-mcp.rs` (the `graphtrail-mcp` MCP server).

## MCP smoke test

Build, then pipe newline-delimited JSON-RPC into the server over stdio. It speaks JSON-RPC 2.0 and exposes ten tools (`search`, `callers`, `callees`, `impact`, `context`, `stats`, `doctor`, `file_neighbors`, `repos`, `diff`).

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
- MCP queries must stay read-only: query connections are `SQLITE_OPEN_READ_ONLY`. The single sanctioned write path is the opt-in `refresh: true` sync step, which must remain fail-open and default-off.
- Schema, JSON output shapes, and MCP tool contracts are stable contracts; breaking changes need a conversation first.
- Each language extractor owns an `EXTRACTOR_FINGERPRINT` constant. Bump that language's fingerprint whenever the extractor can produce different symbols, imports, calls, symbol ids, signatures, containers, body hashes, language labels, or filtering behavior for the same file content. Do not bump unrelated language fingerprints.
- No personal details, hostnames, IPs, account IDs, or live auth profiles in code, tests, or fixtures.
- Conventional commits only. No AI co-authorship trailers.

See `CONTRIBUTING.md` for what lands easily and `README.md` for the full design rationale.
