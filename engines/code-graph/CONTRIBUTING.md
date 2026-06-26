# Contributing to GraphTrail

GraphTrail is a local code-graph sidecar: it indexes a repository into a small SQLite graph of symbols, imports, and call edges, and answers structural questions over a CLI and a read-only MCP server. Patches are welcome. Before you start, please skim this file so we both spend our time on the right things.

## What kinds of changes land easily

- **Bug fixes** in the extractors, `store` (sync/schema), `query` (search/graph/context/stats), the CLI, or the MCP server.
- **Extractor improvements**: better symbol, import, or call-edge coverage for an already-supported language (Python, TypeScript/JavaScript, Rust, Go).
- **Resolution improvements**: sharper same-file-first / capped cross-file call-edge matching.
- **MCP server fixes** that keep it read-only and dependency-light.
- **Test coverage** for any of the above.

## What needs a conversation first

- **A new language.** Open an issue first describing the language and the `LangSpec` shape. New extractors are the main surface area and we want them consistent.
- **Breaking changes** to the SQLite schema, the JSON output shapes, or the MCP tool contracts. Agents and scripts depend on these being stable.
- **Anything that adds a default runtime dependency.** The default build is deliberately network-free and small. Network or cross-tool integrations belong behind an optional cargo feature (see `codesearch` and `miseledger`), never in the default binary.

## What does not land

- Personal details, hostnames, IPs, account IDs, or live auth profiles in code, tests, or fixtures. The whole point of a public repo is to keep that out. Run `content-guard` before you push.
- Anything that makes the default build call the network or start a daemon.
- Anything that makes the MCP server able to mutate a graph. Connections must stay `SQLITE_OPEN_READ_ONLY`.
- AI co-authorship trailers on commits (`Co-Authored-By: <model>`). Conventional commits only.

## Local dev

```bash
git clone https://github.com/escoffier-labs/graphtrail.git
cd graphtrail
cargo build
cargo test --all-features
```

CI runs the same gate it expects from you:

```bash
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test --all-features
cargo build --release
```

To smoke-test the indexer and the MCP server end-to-end:

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

## Adding extractor coverage

Each language is a provider under `src/extractors/<lang>.rs` behind the `LangSpec` trait, with shared traversal in `extractors/common.rs`. Symbols, imports, and call edges are all extracted from the tree-sitter AST in a single pass per file. When you add or change coverage:

1. Update the provider and its tree-sitter queries.
2. Add or extend tests under `tests/` (see `resolution.rs`, `incremental.rs`).
3. If the change affects JSON output, update `tests/json_schema.rs`.
4. If the change affects the MCP surface, update `tests/mcp.rs`.

## Filing issues

Please use the templates under `.github/ISSUE_TEMPLATE/`. The most useful report includes the language and a minimal source snippet that reproduces the wrong symbol, import, or edge, plus the `graphtrail --version` and the output of the failing query with `--json`.

Before posting output, remove tokens, private hostnames, private repo names, and unredacted absolute paths.

## License

By contributing you agree that your contribution is licensed under the MIT License, same as the rest of the repo.
