# Brigade comparison notes

Date: 2026-10-18

This page backs the four README matrices. The README shows the closest neighbors for each layer. Rows marked *(docs only)* stay here for reference. Cells use "Not documented" when the capability was not found in that project's official README or docs on the dated source pass. That is not proof of absence.

Brigade stable claims refer to published **v0.30.0**. Anything newer on main is labeled current main / **0.31 beta**.

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
| Brigade | Ready set plus parallel-safe waves | Fail-closed compare-and-set claims, optional hub-arbitrated repo claims | Per-repo waves, campaign-composed waves, declared wall-clock and worker-dispatch limits, named retry/reroute, fleet board | Machine-local JSON under `.brigade/`. Optional hub for events and claims, not a task DB | Verify receipts (command, exit code, Git state) | [README](https://github.com/escoffier-labs/brigade) |
| Beads | Built-in ready / dependency graph | Atomic claim | Agent workflow plus Dolt sync | Dolt-backed distributed store (`bd dolt push` / `pull`) | Task history | [README](https://github.com/gastownhall/beads) |
| Gas Town | Beads-backed work | Not documented | Multi-agent runtime (Mayor, rigs, polecats, convoys) | Beads ledger | Refinery merge-queue verification gates | [README](https://github.com/gastownhall/gastown) |
| nWave *(docs only)* | Artifacts across 7 waves | Not documented | Human approval between waves plus TDD gates | Git-tracked artifacts | Phase validation and audit logs | [README](https://github.com/nWave-ai/nWave) |
| ActiveGraph *(docs only in this table)* | Reactive graph work | Application-defined | Behaviors and runs | Append-only event log | Replay, fork, and diff | [README](https://github.com/yoheinakajima/activegraph) |
| Claude Code agent teams | Shared task list with dependency gating | File-locked self-claim | Lead plus teammates. Experimental, off by default | Local to one Claude Code team session | `TaskCompleted` hook can block completion | [Agent teams](https://code.claude.com/docs/en/agent-teams) |
| Backlog.md | Board with task dependencies | Not documented | Not documented | Markdown files in Git | Acceptance criteria and definition of done | [README](https://github.com/MrLesk/Backlog.md) |

Honest neighbor strengths: Beads for the distributed Dolt store. Gas Town for the multi-agent runtime. nWave for phased delivery with audit artifacts. ActiveGraph for replay / fork / diff. Brigade's optional fleet hub coordinates events and claims. It does not replace those neighbors.

## Memory

| Project | Durable memory | Capture and recall | Care and operations | Learning | Provenance | Official source |
| --- | --- | --- | --- | --- | --- | --- |
| Brigade | Shared markdown cards plus index | Linted handoffs, bounded session-start recall | Owner policy, Memory Operations (topology, inventory), vault project/search/propose | Outcome score, promote, rollback from receipts | Work receipts and source paths | [README](https://github.com/escoffier-labs/brigade) |
| Mem0 / OpenMemory | User, agent, session, application memory | Extraction and hybrid search | History and metadata APIs | Extraction plus retrieval | Timestamps and entities | [README](https://github.com/mem0ai/mem0) |
| Letta Code *(docs only)* | Git-tracked memory filesystem plus blocks | Agent-managed memory | Editable memory | Dreaming plus skill learning | Git history of memory files | [README](https://github.com/letta-ai/letta-code) |
| agentmemory (`rohitg00/agentmemory`) | Shared coding-agent memory | Hooks, MCP, REST, host plugins | Lifecycle, decay, audit, governed delete | Lessons and reflection (`/lesson`, slot reflection) | Write-time origin channel and citation provenance | [README](https://github.com/rohitg00/agentmemory) |
| Hindsight | Facts, experiences, mental models | Retain and recall | Reflect and feedback | Reflective memory learning | Temporal and causal links | [README](https://github.com/vectorize-io/hindsight) |
| Built-in harness memory | Per-tool stores | Rules, auto memory, AGENTS.md (varies) | Varies by harness | Harness-specific | Harness-specific | [Claude](https://code.claude.com/docs/en/memory), [Codex](https://developers.openai.com/codex/memories), [Cursor](https://docs.cursor.com/context/rules) |
| OpenClaw *(docs only)* | Plain Markdown plus index | Keyword search, optional vector search | File-owned memory | Dreaming distills daily notes | Provenance class and source paths | [Memory docs](https://docs.openclaw.ai/concepts/memory) |

The name `agentmemory` is ambiguous. This table links `rohitg00/agentmemory` only. Other similarly named projects exist and are out of scope here.

`letta-ai/letta` is now a pointer repository. Source moved to `letta-ai/letta-code`, a coding-agent harness, so it moved to docs only. Hindsight now also ships a coding-agent installer and MCP server.

Beads now documents persistent project memory (`bd remember`, `bd prime`) as of 2026-08-26. That is still not owner-mediated handoff plus care, so Beads stays in the Work table.

## Tools and configuration

| Project | MCP | Reviewed rules and skills | Projection behavior | Source of truth | Audit and rollback | Official source |
| --- | --- | --- | --- | --- | --- | --- |
| Brigade | MCP and tools catalog | Skills and harness adapters | Dry-run-first (`mcp sync`, `tools plan`), then apply | Brigade catalog | Preserve unmanaged entries, gate writes, rollback via files | [README](https://github.com/escoffier-labs/brigade) |
| add-mcp | MCP servers across native configs | Not documented | Maps supported fields into native files | Native files plus `sync` / `unify` commands | Warns when a field is dropped | [README](https://github.com/neon-solutions/add-mcp) |
| Rulesync | Supported | Rules, skills, hooks, commands, subagents | Per-target generation | Rulesync project | Generated outputs | [README](https://github.com/dyoshikawa/rulesync) |
| chezmoi *(docs only)* | As ordinary files | As ordinary files | Templates and apply | Git-backed source directory | Diff, templates, merge | [Docs](https://www.chezmoi.io/) |
| agentsync | Many agent targets | Skills, hooks, commands, subagents, memory, plugins | Native / lossy / skipped report | Canonical config | Native / lossy / skipped report | [Docs](https://agentsync.cc/), [repo](https://github.com/spxrogers/agentsync) |
| Ruler | Supported | Rules, skills, subagents | Per-agent generated files | `.ruler/` directory | Gitignore automation. Rollback not documented | [README](https://github.com/intellectronica/ruler) |

`aiconfigsync` (config-sync) left this page on 2026-10-18. Its metadata repository still returned 404 and PyPI had no release since 2026-04-19.

## Evidence and operations

| Project | Receipts | Code impact | Agent activity | Cloud state | Learning / replay | Official source |
| --- | --- | --- | --- | --- | --- | --- |
| Brigade | Command, exit code, Git state, optional graph delta, causal lineage receipts | `brigade code` impact, Center code-graph views and blast radius | Center activity, hub-served fleet board, external harness sessions, Command Deck fleet overview | Registered Codex Cloud, Cursor cloud agent, and Jules task status (best-effort). Claude cloud sessions register only | Score, promote, rollback. Run child, diff, resume | [README](https://github.com/escoffier-labs/brigade) |
| ActiveGraph | Event trace | Not documented | Reactive behaviors | Not documented | Replay, fork, and diff | [README](https://github.com/yoheinakajima/activegraph) |
| Graphiti | Episode provenance and validity windows | Not documented | Not documented | Not documented | Temporal fact updates | [README](https://github.com/getzep/graphiti) |
| Codex Cloud | Changed files and test results for review | Not documented | Task review and follow-ups | Hosted task workspaces that keep running while you are away | Not documented | [Docs](https://developers.openai.com/codex/cloud) |

## Removed on 2026-10-18

- CocoIndex and Neo4j Agent Memory: data-pipeline and graph-memory projects with almost no overlap on these columns.
- Hindsight in the Evidence table: every cell was Not documented. It stays in Memory.
- `aiconfigsync`: unresolvable source repository, no release since April 2026.
- Considered and not added: Vibe Kanban (README says it is sunsetting), Task Master (no push since 2026-04-28), Claude Squad (thin feature surface).

## Brigade scope (honest limits)

- Local file-first control plane. No required daemon. Optional foreground serve commands cover Center, Run View, and the fleet hub.
- Scheduling is external, except optional operator-owned, target-scoped care registrations.
- Work ledger is machine-local JSON, not a distributed Beads/Dolt store.
- An optional fleet hub aggregates run events and arbitrates repo claims. The fleet board can show external harness sessions. Local journals stay authoritative. An optional Dolt sink exports hub history. It is not the work ledger.
- An approved Grok Bot scout feed enqueues labeled GitHub issues only after `--apply`. It does not invent tasks or read issue prose.
- Ordinary runs have no hard run-budget unless declared. Declared enforcement today covers wall-clock and worker-dispatch ceilings only.
- Model, tool, token, and cost dimensions stay observed unless an adapter owns an enforcement boundary.
- Cross-repo campaigns aggregate ready work and compose parallel-safe waves at query time from each member's per-repo partition. Waves are not persisted; member ledgers stay authoritative.
- Center views and the fleet board are read-only. Cloud registry commands track provider work. They do not execute that work.
- External activity observations are best-effort.
- Safe targeted handoffs may auto-file. Ambiguous or risky notes wait for review.
- Outcome learning is scored verification, not reflective memory learning.

## Source checklist (2026-10-18)

All rows were re-checked on 2026-10-18 against official READMEs and docs. Brigade cells were checked against v0.29.0 and have not been re-read for v0.30.0. chezmoi docs were not re-read, so its row rests on the earlier pass.

- https://github.com/escoffier-labs/brigade
- https://github.com/gastownhall/beads
- https://beads.gascity.com/
- https://github.com/gastownhall/gastown
- https://github.com/nWave-ai/nWave
- https://github.com/mem0ai/mem0
- https://github.com/letta-ai/letta-code
- https://code.claude.com/docs/en/agent-teams
- https://github.com/MrLesk/Backlog.md
- https://github.com/intellectronica/ruler
- https://developers.openai.com/codex/cloud
- https://github.com/rohitg00/agentmemory
- https://code.claude.com/docs/en/memory
- https://developers.openai.com/codex/memories
- https://docs.cursor.com/context/rules
- https://docs.openclaw.ai/concepts/memory
- https://github.com/neon-solutions/add-mcp
- https://github.com/dyoshikawa/rulesync
- https://www.chezmoi.io/
- https://agentsync.cc/
- https://github.com/yoheinakajima/activegraph
- https://github.com/getzep/graphiti
- https://github.com/vectorize-io/hindsight
