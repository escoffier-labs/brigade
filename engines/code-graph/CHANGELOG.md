# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Schema v5 persists pending calls so an incremental sync can rebuild all derived edges when definitions change in another file. Databases from before v5 reindex once to populate the new table.
- Schema v6 stores confidence on call edges and rebuilds existing derived edges from persisted pending calls without re-parsing source files.
- `dead-code`, `cycles`, and `affected` analysis commands over the CLI, with matching `dead_code`, `cycles`, and `affected` MCP tools.
- Branch-drift detection in `doctor`, which marks a graph stale when its recorded sync branch differs from the checked-out branch.
- Feature-gated MCP `semantic_search` tool for `codesearch` builds. It uses the existing Code Search client, can return raw per-file hits with `blend: false`, and defaults to blended symbol rows ranked by embedding score plus graph centrality.
- Feature-gated MCP `context` argument `blend_code_search`, matching the CLI flag while leaving the default build's tool list and existing context calls unchanged.
- Shared Code Search index manifest support for `codesearch` builds. GraphTrail now discovers the manifest, matches the canonical repo root, falls back to `semantic_api_url` when `CODE_SEARCH_URL` is unset, scopes requests with `code_search_project`, and strips `code_search_file_prefix` from returned hits.

### Changed
- MCP tool names, schemas, refresh policy, validation, and dispatch now come from one registry. The public tool names and response shapes are unchanged.

### Fixed
- `sync` now refuses to index the filesystem root or the user's home directory instead of walking every cache, toolchain, and vendored source tree on the machine, which held the whole pending graph in memory and could exhaust system RAM. The CLI rejects the root before creating `.graphtrail/`, and the same guard covers the MCP `refresh: true` path. Set `GRAPHTRAIL_ALLOW_UNSAFE_ROOT=1` to override.
- `sync` disambiguates distinct same-named symbols that begin on one line, avoiding primary-key collisions in generated JavaScript bundles while preserving the first declaration's existing ID.
- Docker builds copy only the manifests and source tree they need, while `.dockerignore` excludes Brigade receipts, local agent state, memory handoffs, MCP configuration, environment files, and key material.
- Code Search responses above 8 MiB are rejected before JSON decoding, preventing an oversized response from being buffered without a bound.

## [0.3.0] - 2026-07-08

### Added
- Before/after code-graph diff: `graphtrail diff --before <db> --after <db> [--json]` compares two indexed databases into added/removed/changed symbols and added/removed call edges, opening both read-only. Nodes key on `(file_path, qualified_name, kind)`, so a symbol that only moves lines is not a spurious remove+add. The compact JSON output is built for attaching structural deltas to CI or agent-run receipts. (#10)
- MCP `diff` tool: the same before/after comparison over the MCP surface, taking explicit `before`/`after` database paths. (#12)
- Per-symbol body hashes (schema v3): a body edit that keeps the same signature and line span is now reported as changed instead of slipping through invisibly. Changed nodes carry a `previous` block with the before-side signature and start line, and the diff summary adds `added_edges_line_insensitive` / `removed_edges_line_insensitive` so a pure line shift reads as zero structural churn alongside the exact raw counts. Existing v2 databases upgrade in place with a one-pass reindex on the next sync. (#15)
- Extractor fingerprints (schema v4): each language extractor declares a version fingerprint recorded per file, and the incremental-sync skip check compares it alongside the content hash. Changing an extractor re-extracts exactly that language's files on the next sync, with no forced full-reindex migrations. (#17)
- MCP `refresh` parameter on the query tools (`search`, `callers`, `callees`, `impact`, `context`, `file_neighbors`, `stats`): opt-in incremental sync before answering, fail-open with a note if the sync cannot run. Queries themselves stay on read-only connections. (#17)
- `graphtrail doctor`: the freshness contract. Reports tool and schema versions, last-sync age, pending change counts (new / changed / deleted / fingerprint-stale files), and ignored-entry counts, then verdicts FRESH, STALE, or NEEDS-MIGRATION with exit codes 0/1/2 for scripting. Also exposed as an MCP tool, deliberately without `refresh`. (#18)

### Changed
- Sync respects `.gitignore` in git repositories (including nested and `.git/info/exclude`), and files that become ignored are removed from the index on the next sync, so polluted databases clean themselves. Non-git roots keep the hardcoded skip list, which now also covers bare `venv`. Hidden paths are still indexed. One real-world graph went from 2,955 files (2,803 of them site-packages noise) to 152. (#16)
- First index of a git repository ensures `.graphtrail/` is in `.gitignore`, idempotently. (#17)

### Fixed
- The diff JSON contract, edge-identity behavior, and read-only guarantees are locked by regression fixtures: a full golden test for the JSON shape, a line-shift fixture for edge churn, a body-only fixture, and CLI tests asserting missing-database errors and unmutated inputs. (#14)

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

[Unreleased]: https://github.com/escoffier-labs/graphtrail/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/escoffier-labs/graphtrail/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/escoffier-labs/graphtrail/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/escoffier-labs/graphtrail/releases/tag/v0.1.0
