# Brigade comparison notes

Date: 2026-08-13

This page backs the four README matrices. Cells use "Not documented" when the capability was not found in that project's official README or docs on this date. That is not proof of absence.

Brigade stable claims refer to published **v0.26.1**. Current-main / **0.27 beta** cells are labeled as such.

## How to read the tables

Brigade overlaps work orchestration, memory, tool sync, and evidence products. Most neighbors specialize in one layer. Keep these record types separate:

- task audit trail
- command verification receipt
- event trace / replay log
- memory history
- data lineage

Learning columns also differ. Brigade scores captured verification receipts, then promotes or rolls back skills. Hindsight's documented loop is retain, recall, and reflect over facts, experiences, and mental models. Those are different jobs.

## Work and orchestration

| Project | Ready work | Claim safety | Parallel and run control | Task-store boundary | Verification | Official source |
| --- | --- | --- | --- | --- | --- | --- |
| Brigade | Stable ready set | Fail-closed compare-and-set claims | 0.27: waves, campaigns, declared wall-clock and worker-dispatch limits | Machine-local JSON under `.brigade/`, not distributed | Verify receipts (command, exit code, Git state) | [README](https://github.com/escoffier-labs/brigade) |
| Beads | Built-in ready / dependency graph | Atomic claim | Agent workflow plus Dolt sync | Dolt-backed distributed store (`bd dolt push` / `pull`) | Task history | [README](https://github.com/gastownhall/beads) |
| Gas Town | Beads-backed work | Not documented | Multi-agent runtime (Mayor, rigs, polecats, convoys) | Beads ledger | Operational state | [README](https://github.com/gastownhall/gastown) |
| nWave | Artifacts across 7 waves | Not documented | Human approval between waves plus TDD gates | Git-tracked artifacts | Phase validation and audit logs | [README](https://github.com/nWave-ai/nWave) |
| ActiveGraph | Reactive graph work | Application-defined | Behaviors and runs | Append-only event log | Replay, fork, and diff | [README](https://github.com/yoheinakajima/activegraph) |

Honest neighbor strengths: Beads for the distributed Dolt store. Gas Town for the multi-agent runtime. nWave for phased delivery with audit artifacts. ActiveGraph for replay / fork / diff.

## Memory

| Project | Durable memory | Capture and recall | Care and operations | Learning | Provenance | Official source |
| --- | --- | --- | --- | --- | --- | --- |
| Brigade | Shared markdown cards plus index | Linted handoffs. 0.27: bounded session-start recall | Owner policy. 0.27: Memory Operations (topology, inventory) | Outcome score, promote, rollback from receipts | Work receipts and source paths | [README](https://github.com/escoffier-labs/brigade) |
| Mem0 / OpenMemory | User, agent, session, application memory | Extraction and hybrid search | History and metadata APIs | Extraction plus retrieval | Timestamps and entities | [README](https://github.com/mem0ai/mem0) |
| Letta | Agent state and memory blocks | Agent-managed memory tools | Editable memory | Persistent agent runtime state | Persistent agent state | [Memory blocks](https://docs.letta.com/guides/core-concepts/memory/memory-blocks) |
| agentmemory (`rohitg00/agentmemory`) | Shared coding-agent memory | Hooks, MCP, REST, host plugins | Lifecycle and decay | Not documented | Searchable memory | [README](https://github.com/rohitg00/agentmemory) |
| Hindsight | Facts, experiences, mental models | Retain and recall | Reflect and feedback | Reflective memory learning | Temporal and causal links | [README](https://github.com/vectorize-io/hindsight) |
| Built-in harness memory | Per-tool stores | Rules and auto memories (varies) | Varies by harness | Harness-specific | Harness-specific | [Claude](https://code.claude.com/docs/en/memory), [Codex](https://developers.openai.com/codex/memories), [Cursor](https://docs.cursor.com/context/rules) |
| OpenClaw | Plain Markdown plus index | Keyword search, optional vector search | File-owned memory | Not documented | Files and source paths | [Memory docs](https://docs.openclaw.ai/concepts/memory) |

The name `agentmemory` is ambiguous. This table links `rohitg00/agentmemory` only. Other similarly named projects exist and are out of scope here.

## Tools and configuration

| Project | MCP | Reviewed rules and skills | Projection behavior | Source of truth | Audit and rollback | Official source |
| --- | --- | --- | --- | --- | --- | --- |
| Brigade | MCP and tools catalog | Skills and harness adapters | Dry-run-first (`mcp sync`, `tools plan`), then apply | Brigade catalog | Preserve unmanaged entries, gate writes, rollback via files | [README](https://github.com/escoffier-labs/brigade) |
| add-mcp | MCP servers across native configs | Not documented | Maps supported fields into native files | Registry plus native files | Warns when a field is dropped | [README](https://github.com/neon-solutions/add-mcp) |
| Rulesync | Supported | Rules, skills, hooks, commands, subagents | Per-target generation | Rulesync project | Generated outputs | [README](https://github.com/dyoshikawa/rulesync) |
| config-sync (`aiconfigsync`) | Not documented | Rules for 12 assistants | Generated rule files | Single config | Generated outputs | https://pypi.org/project/aiconfigsync/ |
| chezmoi | As ordinary files | As ordinary files | Templates and apply | Git-backed source directory | Diff, templates, merge | [Docs](https://www.chezmoi.io/) |
| agentsync | Many agent targets | Skills, hooks, commands, subagents | Native / lossy / skipped report | Canonical config | Native / lossy / skipped report | [Docs](https://agentsync.cc/) |

On 2026-08-13 the GitHub repository named in the `aiconfigsync` package metadata did not resolve. The PyPI package page remains the cited source.

## Evidence and operations

| Project | Receipts | Code impact | Agent activity | Cloud state | Learning / replay | Official source |
| --- | --- | --- | --- | --- | --- | --- |
| Brigade | Command, exit code, Git state, optional graph delta | Stable `brigade code` impact. 0.27: Center code-graph views | 0.27: Center activity | 0.27: registered cloud status (best-effort) | Score, promote, rollback | [README](https://github.com/escoffier-labs/brigade) |
| ActiveGraph | Event trace | Not documented | Reactive behaviors | Not documented | Replay, fork, and diff | [README](https://github.com/yoheinakajima/activegraph) |
| CocoIndex | Source-to-target data lineage | Not documented | Not documented | Not documented | Incremental recompute | [README](https://github.com/cocoindex-io/cocoindex) |
| Graphiti | Episode provenance and validity windows | Not documented | Not documented | Not documented | Temporal fact updates | [README](https://github.com/getzep/graphiti) |
| Hindsight | Not documented | Not documented | Not documented | Not documented | Reflective memory learning | [README](https://github.com/vectorize-io/hindsight) |
| Neo4j Agent Memory | Reasoning and tool traces | Not documented | Not documented | Not documented | Similar-task retrieval | [README](https://github.com/neo4j-labs/agent-memory) |

## Brigade scope (honest limits)

- Local file-first control plane. No Brigade daemon.
- Scheduling is external, except optional operator-owned, target-scoped care registrations.
- Work ledger is machine-local JSON, not a distributed Beads/Dolt store.
- Ordinary runs have no hard run-budget unless declared. Declared enforcement today covers wall-clock and worker-dispatch ceilings only.
- Model, tool, token, and cost dimensions stay observed unless an adapter owns an enforcement boundary.
- Cross-repo campaigns aggregate ready work. Campaign-aware parallel wave composition remains deferred.
- Center views are read-only. Cloud registry commands track provider work. They do not execute that work.
- External activity observations are best-effort.
- Safe targeted handoffs may auto-file. Ambiguous or risky notes wait for review.
- Outcome learning is scored verification, not reflective memory learning.

## Source checklist (2026-08-13)

- https://github.com/escoffier-labs/brigade
- https://github.com/gastownhall/beads
- https://github.com/gastownhall/gastown
- https://github.com/nWave-ai/nWave
- https://github.com/mem0ai/mem0
- https://github.com/letta-ai/letta
- https://github.com/rohitg00/agentmemory
- https://code.claude.com/docs/en/memory
- https://developers.openai.com/codex/memories
- https://docs.cursor.com/context/rules
- https://docs.openclaw.ai/concepts/memory
- https://github.com/neon-solutions/add-mcp
- https://github.com/dyoshikawa/rulesync
- https://pypi.org/project/aiconfigsync/
- https://www.chezmoi.io/
- https://agentsync.cc/
- https://github.com/yoheinakajima/activegraph
- https://github.com/cocoindex-io/cocoindex
- https://github.com/getzep/graphiti
- https://github.com/vectorize-io/hindsight
- https://github.com/neo4j-labs/agent-memory
