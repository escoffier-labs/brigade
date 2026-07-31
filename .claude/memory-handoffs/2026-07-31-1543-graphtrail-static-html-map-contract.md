# Memory Handoff

## Type
decision

## Title
GraphTrail static HTML maps use a dedicated bounded command

## Summary
GraphTrail's static Code Intelligence map is a dedicated `graphtrail map` command instead of another `ExportFormat`. The decision preserves `export_graph` as a whole-graph serializer while giving the HTML surface a required focus file, traversal direction, depth, and output limits.

## Durable facts
- Contract: `graphtrail map <PATH> --out <FILE> [--direction callers|callees|neighbors] [--depth N] [--max-nodes N] [--max-edges N]`.
- Defaults are `neighbors`, depth `1`, `100` nodes, and `250` edges. Hard maxima are depth `5`, `250` nodes, and `500` edges.
- Selection starts with focus-file symbols, expands breadth-first, and orders nodes and edges deterministically. Rendered edges must have both endpoints in the selected node set. Truncation reports exact rendered and omitted counts.
- Output is one static HTML file with inline CSS and JavaScript. Embedded JSON escapes `<`, `>`, and `&`; visible graph values use DOM text sinks only.

## Evidence
- files changed: `engines/code-graph/src/query/map.rs`, `engines/code-graph/src/query/map_html.rs`, `engines/code-graph/src/cli.rs`, `engines/code-graph/tests/map.rs`, `engines/code-graph/tests/cli_read_only.rs`
- commands run: `cargo fmt --manifest-path engines/code-graph/Cargo.toml -- --check`; `cargo clippy --manifest-path engines/code-graph/Cargo.toml --all-targets --all-features -- -D warnings`; `cargo test --manifest-path engines/code-graph/Cargo.toml --all-features`; `cargo build --release --manifest-path engines/code-graph/Cargo.toml`
- issue contract: `docs/issue-drafts/2026-07-31-code-intelligence-html-map.md`

## Recommended memory action
create-card

## Target card
graphtrail-static-html-map-contract.md

## Suggested card content
---
topic: GraphTrail static HTML map contract
category: architecture
tags: [graphtrail, code-intelligence, html, cli, bounding]
---

# GraphTrail static HTML map contract

Use `graphtrail map`, not `ExportFormat::Html`, for file-rooted static maps. Existing export formats serialize the whole file or symbol graph. The map contract requires a focus file plus direction, depth, node, and edge bounds.

Defaults: `neighbors`, depth `1`, `100` nodes, `250` edges. Hard maxima: depth `5`, `250` nodes, `500` edges. Traverse focus symbols breadth-first and sort deterministically. Keep only relationships whose endpoints are retained, and report exact rendered and omitted counts.

The output is one static file with inline CSS and JavaScript. Escape `<`, `>`, and `&` in embedded JSON, render untrusted values with `textContent`, make no network requests, and add no frontend toolchain or runtime service.
