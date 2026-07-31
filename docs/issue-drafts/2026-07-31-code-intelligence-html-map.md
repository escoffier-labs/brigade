# feat(code-graph): add a bounded file-scope static HTML map

## Summary

Add `graphtrail map <PATH> --out <FILE>` to export a self-contained HTML view of the indexed call graph around one file. The command stays inside GraphTrail, reads the existing SQLite graph, and writes one static file with inline CSS and JavaScript.

This is follow-on #1 from #631. It does not join the Run View release or add a Brigade-side command.

## Scope

### Command contract

```text
graphtrail map <PATH> --out <FILE> \
  [--direction callers|callees|neighbors] \
  [--depth <1..5>] \
  [--max-nodes <1..250>] \
  [--max-edges <1..500>]
```

- `<PATH>` is an indexed, repository-relative focus file.
- `--direction` defaults to `neighbors`.
- `--depth` defaults to `1` and cannot exceed `5`.
- `--max-nodes` defaults to `100` and cannot exceed `250`.
- `--max-edges` defaults to `250` and cannot exceed `500`.
- `--out` is required. The command writes exactly one HTML file and prints its path.
- Missing focus files and invalid bounds fail with a nonzero exit and a specific diagnostic.

Use a dedicated `map` command instead of adding `Html` to `ExportFormat`. The existing `export` command serializes the whole graph at file or symbol scope. The map has a required root, traversal direction, depth, and output bounds, so placing it behind `export --format html` would give that format a different argument contract from DOT, GraphML, and JSON Lines.

### Graph selection and bounds

- Seed traversal with symbols defined in the focus file.
- Follow incoming calls for `callers`, outgoing calls for `callees`, and both for `neighbors`.
- Expand breadth-first to the requested depth.
- Order candidates by hop, file path, start line, qualified name, and stable symbol id. Order edges by source id, target id, source line, and kind.
- Apply node and edge limits after deterministic selection so the same database and arguments produce identical bytes.
- Keep the focus file visible when it contains indexed symbols. If the focus file alone exceeds the node limit, retain its first symbols in the same deterministic order and report the omission.
- Include a visible truncation notice whenever symbols or relationships are omitted. The notice names the active depth and limits, and reports the rendered and omitted node and edge counts.
- An indexed file with no symbols produces a valid map with an explicit empty state.

### Static HTML contract

- Emit a complete HTML document with inline CSS, inline JavaScript, and no other files.
- Make no network requests. Do not use a CDN, remote font, telemetry, package manager, or frontend build step.
- Group the focus file, incoming callers, outgoing callees, and additional neighbors so direction remains readable without a force-directed layout.
- Show qualified symbol names, repository-relative paths, line numbers, call kinds, and call-site lines as text.
- Serialize embedded data with `<`, `>`, and `&` escaped before inserting it into a non-executable JSON script element.
- Create every visible untrusted value with DOM `textContent`. Do not use `innerHTML` for graph data.
- Support keyboard traversal between nodes, Enter or Space to open node details, visible focus, system light and dark themes, and `prefers-reduced-motion`.
- Expose the truncation notice as status text and keep direction labels available without relying on color.

## Acceptance criteria

- [ ] `graphtrail map <PATH> --out <FILE>` writes one self-contained static HTML file from the existing read-only graph database.
- [ ] The default command is rooted at one file, uses `neighbors`, depth `1`, at most `100` nodes, and at most `250` edges.
- [ ] Depth, node, and edge arguments reject values outside their documented hard limits.
- [ ] Caller, callee, and neighbor traversal stops at the requested depth.
- [ ] Identical graph contents and arguments produce byte-identical HTML, including when rows were inserted in a different order.
- [ ] Node and edge overflow produces visible rendered and omitted counts. No output exceeds the selected limits.
- [ ] Empty, small, and over-limit fixture graphs are covered by Rust tests.
- [ ] Hostile symbol names, paths, and edge labels containing markup or script-closing text remain inert.
- [ ] Snapshot coverage fixes the document landmarks, embedded-data boundary, accessibility hooks, and lack of external assets.
- [ ] CLI tests cover defaults, every option, invalid focus paths, invalid limits, output creation, and read-only database behavior.
- [ ] `cargo fmt --check`, `cargo clippy --all-targets --all-features -- -D warnings`, `cargo test --all-features`, and `cargo build --release` pass through Brigade verification receipts.
- [ ] Brigade's repository verification is run through `brigade work verify run --target . --command "./scripts/verify"`. Unrelated baseline failures outside this issue's write boundary are reported without modifying those files.

## Non-goals

- Brigade Python CLI changes or a `brigade code map` wrapper
- Run View integration or any run artifact reader
- Deep links between run, code, and evidence views
- A shared visual component or browser-surface framework
- Evidence Ledger or MiseLedger changes
- A local or remote HTTP server
- Symbol editing, run controls, or any other mutation
- Whole-repository interactive rendering
- A frontend dependency, build toolchain, remote asset, telemetry endpoint, or additional runtime service

## Future work

A Brigade-side convenience command can be reviewed separately after the GraphTrail contract is stable. Shared styling and deep links remain follow-ons #2 and #4 from #631.
