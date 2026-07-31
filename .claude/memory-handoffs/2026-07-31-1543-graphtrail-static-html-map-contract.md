# Memory Handoff

## Type
decision

## Title
GraphTrail static HTML maps use a dedicated bounded command

## Summary
GraphTrail uses a dedicated, bounded `graphtrail map` command for static Code Intelligence HTML. Its list lanes remain the accessible default; an inline SVG graph progressively enhances the same embedded JSON. SVG fits the hard caps while preserving keyboard-addressable nodes and safe text labels.

## Durable facts
- Contract: `graphtrail map <PATH> --out <FILE> [--direction callers|callees|neighbors] [--depth N] [--max-nodes N] [--max-edges N]`.
- Defaults: `neighbors`, depth `1`, `100` nodes, `250` edges. Hard maxima: depth `5`, `250` nodes, `500` edges.
- Selection expands breadth-first from focus-file symbols, sorts nodes and edges deterministically, and retains only edges whose endpoints survived bounding. Truncation reports exact rendered and omitted counts.
- Output is one static HTML file with inline CSS and JavaScript. Embedded JSON escapes `<`, `>`, and `&`; visible values use DOM text sinks only.
- SVG positions use stable node-ID hashes, explicit ordinal sorting, and exactly 160 repulsion-and-spring iterations without `Math.random()`. The first sorted focus node is centered before the settled graph is fitted to a fixed margin.
- Graph mode repeats truncation counts, supports keyboard pan and zoom, reuses the details panel, and POSIX-quotes paths in regeneration commands.

## Evidence
- implementation: `engines/code-graph/src/query/map.rs`, `engines/code-graph/src/query/map_html.rs`
- contracts: `engines/code-graph/tests/map.rs`, `engines/code-graph/tests/snapshots/map_shell.html`
- verification: engine format, clippy, all-feature tests, and release build passed through `brigade work verify run`

## Recommended memory action
create-card

## Target card
graphtrail-static-html-map-contract.md

## Suggested card content
---
topic: GraphTrail static HTML map contract
category: architecture
tags: [graphtrail, code-intelligence, html, cli, bounding, svg, determinism]
---

# GraphTrail static HTML map contract

Use `graphtrail map`, not `ExportFormat::Html`, for file-rooted static maps. Defaults are `neighbors`, depth `1`, `100` nodes, and `250` edges. Hard maxima are depth `5`, `250` nodes, and `500` edges. Traverse breadth-first, sort deterministically, keep edges between retained nodes, and report exact omissions.

Keep list lanes as the accessible default. Render the optional graph as inline SVG over the same bounded JSON. Seed positions from stable node-ID hashes, sort ordinally, and run exactly 160 repulsion-and-spring iterations without randomness. Center the first sorted focus node and fit the result to a fixed margin.

Escape embedded JSON, use DOM text sinks for untrusted values, POSIX-quote regeneration paths, make no network requests, and add no frontend toolchain or runtime service.
