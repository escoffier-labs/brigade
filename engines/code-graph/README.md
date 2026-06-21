# GraphTrail

GraphTrail is a local code graph sidecar for semantic search, Brigade context
packs, and MiseLedger evidence. It indexes source files into a small SQLite
graph so tools can ask structural questions before an agent edits code.

This repo is intentionally narrower than TraceDecay:

- Code Search keeps semantic chunks, summaries, and embeddings.
- GraphTrail owns symbols, imports, call edges, and graph context.
- MiseLedger owns session/evidence archives and JSON receipts.
- Brigade owns operator workflow, handoffs, context packs, and guardrails.

## Current MVP

GraphTrail currently supports Python, TypeScript/JavaScript, Rust, and Go. Symbols,
imports, and call edges are all extracted from the tree-sitter AST in a single pass per file
(`extractors/`), organized as per-language providers behind a `LangSpec` trait. Call
edges are resolved by name, preferring same-file targets before falling back to a
capped cross-file match.

- `files`
- `symbols`
- `edges`
- `imports`
- `symbols_fts`

The implementation is read-only after indexing. It installs no hooks, starts no
daemon, and makes no network calls.

The code is split into focused modules: `model` (shared types), `extractors`
(language providers + traversal), `store` (`db`/`schema`/`sync`), `query`
(`search`/`graph`/`context`/`stats`), and a thin `cli`.

## Commands

```bash
cargo build

cargo run -- init /path/to/repo
cargo run -- sync /path/to/repo          # incremental: a no-op when nothing changed
cargo run -- sync /path/to/repo --force  # rebuild every file

cargo run -- --db /path/to/repo/.graphtrail/graphtrail.db search "handoff lint"
cargo run -- --db /path/to/repo/.graphtrail/graphtrail.db callers lint
cargo run -- --db /path/to/repo/.graphtrail/graphtrail.db callees lint
cargo run -- --db /path/to/repo/.graphtrail/graphtrail.db impact lint
cargo run -- --db /path/to/repo/.graphtrail/graphtrail.db context "handoff lint" --json
cargo run -- --db /path/to/repo/.graphtrail/graphtrail.db stats --json
```

## MCP server

`graphtrail-mcp` is a read-only MCP server (newline-delimited JSON-RPC over stdio) that
exposes the query commands as tools: `search`, `callers`, `callees`, `impact`, `context`,
and `stats`. Connections are always opened `SQLITE_OPEN_READ_ONLY`, so it can never mutate
the graph. The default db comes from `--db`, `--db=<path>`, or the `GRAPHTRAIL_DB` env var.

It is multi-repo: every tool also accepts an optional `repo` (uses
`<repo>/.graphtrail/graphtrail.db`) or `db` (explicit path) argument, so a single running
server can answer for any indexed repository. The db is opened lazily per call, so the
server starts even before the default db exists.

```bash
cargo build --release   # produces target/release/graphtrail-mcp
```

Register it with an MCP client (e.g. Claude Code `.mcp.json` / `~/.claude.json`):

```json
{
  "mcpServers": {
    "graphtrail": {
      "command": "/abs/path/to/target/release/graphtrail-mcp",
      "args": ["--db", "/abs/path/to/repo/.graphtrail/graphtrail.db"]
    }
  }
}
```

## Near-Term Plan

Done:

1. ~~Move tree-sitter extraction into per-language provider modules.~~
2. ~~Replace regex call/import extraction with AST-based edge extraction.~~

Next:

3. ~~Add stable JSON schemas for graph context packs.~~
4. ~~Add read-only MCP tools after the CLI surface settles.~~

5. ~~Add a Code Search adapter that blends graph scores with embedding scores.~~
6. ~~Add a Brigade context-pack adapter.~~
7. ~~Add MiseLedger receipt links from symbols/files to prior sessions and diffs.~~

## Integrations

The Brigade adapter is built in: `context --markdown` renders a context pack as a
Brigade-friendly markdown document (droppable into a handoff's evidence section).

The Code Search and MiseLedger adapters are **optional cargo features** so the default
binary stays free of network and cross-tool dependencies:

```bash
# Blend Code Search embedding hits with graph centrality (POST localhost:5204/api/search).
# Honors CODE_SEARCH_URL and CODE_SEARCH_API_KEY.
cargo run --features codesearch -- --db <db> blend "rate limiting" --json

# Surface MiseLedger evidence items (read-only FTS) mentioning a symbol/term.
# Honors MISELEDGER_DB (defaults to ~/.local/share/miseledger/miseledger.db).
cargo run --features miseledger -- links "dispatch" --json
```

## Architecture Notes

TraceDecay's useful architectural lesson is the separation between language
providers, graph storage, and agent-facing query tools. GraphTrail is adopting
that shape without copying TraceDecay's implementation or product scope.

Near-term module boundaries should be:

- `extractors/` for per-language tree-sitter providers
- `store/` for SQLite schema and graph writes
- `query/` for search, callers, callees, impact, and context packs
- `mcp/` only after the CLI and JSON contracts are stable

GraphTrail should stay small enough to be a sidecar. It should not grow memory,
LCM, hooks, install automation, dashboards, or receipt ownership.

## Non-Goals

GraphTrail should not own memory, receipts, publishing, scheduling, or global
agent hooks. Those stay in Brigade and MiseLedger.
