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

GraphTrail currently supports Python and TypeScript/JavaScript with conservative
heuristic extraction:

- `files`
- `symbols`
- `edges`
- `imports`
- `symbols_fts`

The first implementation is read-only after indexing. It installs no hooks,
starts no daemon, and makes no network calls.

## Commands

```bash
cargo build

cargo run -- init /path/to/repo
cargo run -- sync /path/to/repo

cargo run -- --db /path/to/repo/.graphtrail/graphtrail.db search "handoff lint"
cargo run -- --db /path/to/repo/.graphtrail/graphtrail.db callers lint
cargo run -- --db /path/to/repo/.graphtrail/graphtrail.db callees lint
cargo run -- --db /path/to/repo/.graphtrail/graphtrail.db impact lint
cargo run -- --db /path/to/repo/.graphtrail/graphtrail.db context "handoff lint" --json
cargo run -- --db /path/to/repo/.graphtrail/graphtrail.db stats --json
```

## Near-Term Plan

1. Replace heuristic extraction internals with tree-sitter providers.
2. Add stable JSON schemas for graph context packs.
3. Add read-only MCP tools after the CLI surface settles.
4. Add a Code Search adapter that blends graph scores with embedding scores.
5. Add a Brigade context-pack adapter.
6. Add MiseLedger receipt links from symbols/files to prior sessions and diffs.

## Non-Goals

GraphTrail should not own memory, receipts, publishing, scheduling, or global
agent hooks. Those stay in Brigade and MiseLedger.
