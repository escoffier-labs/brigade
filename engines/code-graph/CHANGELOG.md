# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-07-03

### Added
- Transitive impact: `impact <symbol> --depth N` (and a `depth` argument on the MCP tool) walks callers and callees breadth-first up to 5 hops. Edge rows carry a `hops` field, traversal is cycle-safe, and each direction caps at 500 edges with a self-describing `kind: "truncated"` marker row instead of silent loss. (#8)
- Agent navigation tools: `neighbors <path>` / MCP `file_neighbors` return a file's structural neighborhood (symbols, import links, call-connected files with edge counts); `search` accepts a path filter (`--path` / MCP `path`) to scope results to a directory; a `repos` MCP tool reports the default database's freshness and discovers indexed repositories under caller-supplied roots. (#6)
- MCP `context` tool accepts `format: "json" | "markdown"`, so agents can pull the Brigade-ready markdown pack directly over MCP. (#4)
- `stats` now reports `synced_at`, `tool_version`, and per-language file counts. (#6)
- `Dockerfile` and `.dockerignore` for a containerized `graphtrail-mcp`, an `AGENTS.md` for coding agents, and a `rust-version = "1.85"` MSRV pin. (#7)

### Changed
- Call resolution is qualifier- and import-aware (schema v2): member and scoped calls (`a.save()`, `Type::new()`, `module.func()`) resolve through the enclosing container or the importing file's imports, including relative Python modules and Rust crate paths, instead of fanning out to same-named symbols. Calls into stdlib or third-party modules no longer produce edges. On a ~4.6k-symbol repo this removed two-thirds of the edges (all false positives) while leaving files and symbols identical. Rebuild existing indexes with `graphtrail sync <repo> --force`. (#5)
- Ambiguous cross-file fallback resolution is deterministically ordered, so repeated syncs produce identical edge sets. (#4)
- A no-op incremental sync now updates the `synced_at` meta key, making index freshness distinguishable from staleness. (#4)
- Query commands (`search`, `callers`, `callees`, `impact`, `context`, `stats`) open the database read-only; only `init` and `sync` open it writable. (#4)
- Context packs render one location contract everywhere: symbols as `file:start-end`, edges as `source file:line -> target file`. (#4)

### Fixed
- The MCP server returns proper JSON-RPC errors (`-32700` parse, `-32600` invalid request, `-32602` invalid params, including non-string `repo`/`db` selectors that previously fell back to the default database silently) and clamps limits before SQLite integer binding. (#4)

## [0.1.0] - 2026-06-27

### Added
- Maintainer-health files: `SECURITY.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, GitHub issue and pull-request templates.
- Code-graph indexer: parses Python, TypeScript/JavaScript, Rust, and Go with tree-sitter, extracting symbols, imports, and call edges into a local SQLite graph under `.graphtrail/`.
- Per-language extractor providers behind a `LangSpec` trait, with call edges resolved same-file-first before a capped cross-file fallback.
- CLI commands: `init`, `sync` (incremental, with `--force`), `search`, `callers`, `callees`, `impact`, `context` (with `--json` and `--markdown`), and `stats`.
- Read-only MCP server (`graphtrail-mcp`): newline-delimited JSON-RPC 2.0 over stdio exposing `search`, `callers`, `callees`, `impact`, `context`, and `stats`. Connections are always opened `SQLITE_OPEN_READ_ONLY`; multi-repo via an optional `repo`/`db` argument per tool.
- Built-in Brigade adapter: `context --markdown` renders a context pack as Brigade-friendly markdown.
- Optional cargo features `codesearch` (blend embedding hits with graph centrality) and `miseledger` (surface evidence items), kept out of the default network-free build.

### Changed
- README rewritten to lead with what GraphTrail is, why it exists, and how it differs from grep, embedding search, and an LSP. Adds a verified MCP tool table, a copy-paste quickstart, a real `stats` proof block, and a recorded terminal demo (`docs/assets/graphtrail-context.svg`, reproducible from the `.cast`) of init, sync, callers, and context.

[Unreleased]: https://github.com/escoffier-labs/graphtrail/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/escoffier-labs/graphtrail/releases/tag/v0.1.0
