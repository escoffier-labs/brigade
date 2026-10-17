# Code intelligence

Brigade's built-in code map (`brigade code`, historically GraphTrail) indexes a repository for structural queries and attaches a graph delta to verification receipts when the engine is present. Install it with `brigade setup`. No separate GraphTrail product install is required.

## Sync

```bash
brigade setup
brigade code sync --target .
brigade code doctor --target .
```

`sync` builds or refreshes the local index. Status reports whether the index is present and usable. Verification attempts to sync or build the index when the engine is available. If capture fails, verification still runs and the receipt records the graph-delta failure status.

## Structural queries

```bash
brigade code callers <symbol>
brigade code callees <symbol>
brigade code impact <symbol>
brigade code context "<task>"
brigade code outline <path>
```

Use these before edits that rename symbols or change call shape. `impact` and `context` are the usual entry points for agent briefs.

## Receipt deltas

When a check runs through `brigade work verify run` and the code map is available, the verify receipt (`schema_version: 2`) may include a `code_graph_delta` summary of symbols touched by the change. That delta is evidence for the run, not a substitute for the exit code.

Not every Brigade command writes a receipt. Ad hoc shell commands outside `work verify run` do not automatically produce one.

## Export and Center

The code map exposes a bounded JSON export contract and a Center view:

```bash
brigade code export --target . --json
brigade code export --target . --json --symbol <symbol> --overlay
```

The export caps the module map at 15 modules and 24 edges. `--overlay` reads the local Git change set. The Center dashboard consumes this command contract instead of opening the graph database itself.

## MCP access

`brigade setup` installs `graphtrail-mcp`. Register it in the harness configuration directly or through `brigade mcp sync` so agents can query callers, impact, and context. Historical MCP entry labels may still say `graphtrail`.

## Limitations

- The index is local to the machine and target. It is not a hosted code-intelligence service.
- Queries depend on a successful sync. Stale checkouts need another sync pass.
- Graph ranking inside `work verify plan` is advisory. The worker still chooses the command.
- Static analysis has language and dynamic-dispatch blind spots. Treat impact results as evidence for choosing checks, not proof that no other code is affected.
- Binary and path names under the engines tree may still use the GraphTrail label. The operator surface is `brigade code`.

Related: [wiring guide](wiring-graphtrail-miseledger.md), [work closeout](work-closeout.md), [receipt schemas](receipt-schemas.md), [operator center](operator-center.md).
