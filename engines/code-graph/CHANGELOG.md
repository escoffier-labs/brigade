# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
