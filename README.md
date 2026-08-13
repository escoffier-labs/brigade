<p align="center">
  <img src="docs/assets/brigade-wordmark.svg" alt="Brigade (by Escoffier Labs)" width="640">
</p>

<p align="center">
  Brigade keeps coding-agent work trackable across tools, repos, and sessions: tasks, receipts, shared memory, and synced tools.
</p>

<p align="center">
  <a href="https://brigade.tools/docs/getting-started/install">Install</a> ·
  <a href="https://brigade.tools/docs">Docs</a> ·
  <a href="docs/comparison.md">Compare</a> ·
  <a href="QUICKSTART.md">Quickstart</a>
</p>

Published **v0.26.1** is the stable install (`brigade-cli==0.26.1`). Current main / **0.27 beta** adds per-repo parallel-safe work waves, cross-repo ready-work campaigns, Memory Operations, activity and cloud status, code-graph views, bounded recall, and declared budgets. Cross-repo wave composition remains deferred. Cells below mark that split.

## What it does

- **Ready work and claim safety.** Brigade lists unblocked tasks and uses atomic claims so two agents cannot take the same item. Stable in v0.26.1.
- **Declared run limits and registered cloud activity.** Current main / 0.27 beta enforces declared wall-clock and worker-dispatch ceilings and reconciles registered cloud work against provider and GitHub state.
- **Verification receipts.** A check run through Brigade writes the command, the real exit code, and the Git state. Stable in v0.26.1. Not every Brigade command writes a receipt.
- **Owner-mediated handoffs and bounded recall.** Handoffs are linted and routed by the memory owner. Safe targeted notes may auto-file, while ambiguous or risky notes wait for review. Current main / 0.27 beta adds capped session-start recall.
- **Explicit dry-run-first harness projection.** MCP servers, tools, and skills are previewed before Brigade writes them into a harness. Stable in v0.26.1.

Learning in Brigade is outcome-based skill scoring, promotion, and rollback from captured verification receipts. It is not autonomous reflective memory learning.

## Install

Install with `pipx install brigade-cli` or `uv tool install brigade-cli`, then follow the [install guide](https://brigade.tools/docs/getting-started/install), [QUICKSTART.md](QUICKSTART.md), or give [first 10 minutes](docs/first-10-minutes.md) and [docs/agents-guide.md](docs/agents-guide.md) to your coding agent.

Stable channel: published v0.26.1. Preview channel: `brigade update --channel beta` for `0.27.0.devYYYYMMDD` wheels. Details: [update channels](docs/update-channels.md).

## How it compares

Brigade overlaps several categories. Most adjacent projects specialize in one layer. Brigade connects those layers around trackable coding-agent work. Where a neighbor is stronger, that cell says so. Dated sources and longer notes: [docs/comparison.md](docs/comparison.md) (2026-08-13).

### Work and orchestration

| Project | Ready work | Claim safety | Parallel and run control | Task-store boundary | Verification |
| --- | --- | --- | --- | --- | --- |
| [Brigade](https://github.com/escoffier-labs/brigade) | Stable ready set | Fail-closed CAS claims | 0.27: per-repo waves, cross-repo ready aggregation, declared limits | Local JSON files, no shared store | Command receipts |
| [Beads](https://github.com/gastownhall/beads) | Built in | Atomic claim | Agent workflow + Dolt sync | Dolt-backed distributed store | Task history |
| [Gas Town](https://github.com/gastownhall/gastown) | Beads-backed | Not documented | Multi-agent runtime | Beads ledger | Operational state |
| [nWave](https://github.com/nWave-ai/nWave) | Wave artifacts | Not documented | 7 reviewed phases + TDD gates | Git-tracked artifacts | Phase validation |
| [ActiveGraph](https://github.com/yoheinakajima/activegraph) | Reactive graph | Application-defined | Behaviors + runs | Append-only event log | Replay, fork, and diff |

### Memory

| Project | Durable memory | Capture and recall | Care and operations | Learning | Provenance |
| --- | --- | --- | --- | --- | --- |
| [Brigade](https://github.com/escoffier-labs/brigade) | Shared files + cards | Handoffs. 0.27: bounded recall | Owner policy. 0.27: Memory Operations | Outcome score + promote + rollback | Work receipts + sources |
| [Mem0 / OpenMemory](https://github.com/mem0ai/mem0) | User + agent + session | Automatic + hybrid search | History + metadata | Extraction + retrieval | Timestamps + entities |
| [Letta](https://github.com/letta-ai/letta) | Agent state + blocks | Agent-managed tools | Editable state | Persistent agent runtime | Persistent agent state |
| [agentmemory](https://github.com/rohitg00/agentmemory) | Shared coding-agent memory | Hooks + MCP + REST | Lifecycle + decay | Not documented | Searchable memory |
| [Hindsight](https://github.com/vectorize-io/hindsight) | Facts + experiences + models | Retain + recall | Reflect + feedback | Reflective memory learning | Temporal + causal links |
| [Built-in harness memory](https://code.claude.com/docs/en/memory) | Tool-specific | Rules + auto memories | Varies by harness | Harness-specific | Harness-specific |
| [OpenClaw](https://docs.openclaw.ai/concepts/memory) | Plain files + index | Keyword + optional vector search | File-owned memory | Not documented | Files + source paths |

### Tools and configuration

| Project | MCP | Reviewed rules and skills | Projection behavior | Source of truth | Audit and rollback |
| --- | --- | --- | --- | --- | --- |
| [Brigade](https://github.com/escoffier-labs/brigade) | MCP + tools catalog | Skills + adapters | Dry-run-first, then apply | Brigade catalog | Preserve + gate + rollback |
| [add-mcp](https://github.com/neon-solutions/add-mcp) | MCP servers | Not documented | Maps fields into native files | Registry + native files | Warnings for dropped fields |
| [Rulesync](https://github.com/dyoshikawa/rulesync) | Supported | Rules + skills + hooks | Per-target generation | Rulesync project | Generated outputs |
| [config-sync](https://pypi.org/project/aiconfigsync/) | Not documented | Rules for 12 tools | Generated rule files | Single config | Generated outputs |
| [chezmoi](https://www.chezmoi.io/) | As files | As files | Templates + apply | Git-backed source dir | Diff + templates + merge |
| [agentsync](https://agentsync.cc/) | Many agent targets | Skills + hooks + commands | Native/lossy/skipped report | Canonical config | Native/lossy/skipped report |

### Evidence and operations

| Project | Receipts | Code impact | Agent activity | Cloud state | Learning / replay |
| --- | --- | --- | --- | --- | --- |
| [Brigade](https://github.com/escoffier-labs/brigade) | Command + Git + optional impact | Stable impact. 0.27: code-graph views | 0.27: center activity | 0.27: registered cloud status | Score + promote + rollback |
| [ActiveGraph](https://github.com/yoheinakajima/activegraph) | Event trace | Not documented | Reactive behaviors | Not documented | Replay, fork, and diff |
| [CocoIndex](https://github.com/cocoindex-io/cocoindex) | Data lineage | Not documented | Not documented | Not documented | Incremental recompute |
| [Graphiti](https://github.com/getzep/graphiti) | Episode provenance | Not documented | Not documented | Not documented | Temporal fact updates |
| [Hindsight](https://github.com/vectorize-io/hindsight) | Not documented | Not documented | Not documented | Not documented | Reflective memory learning |
| [Neo4j Agent Memory](https://github.com/neo4j-labs/agent-memory) | Reasoning + tool traces | Not documented | Not documented | Not documented | Similar-task retrieval |

"Not documented" means the capability was not found in the project's official README or documentation on 2026-08-13. It is not proof that the capability is absent. An event trace, memory history, data lineage, and a command verification receipt answer different questions.

## Scope

Brigade is a local file-first control plane for coding agents. There is no Brigade daemon. Scheduling stays external, except optional operator-owned, target-scoped care registrations (`brigade care install`). The work ledger is machine-local JSON and is not a distributed task database. Ordinary runs have no hard run-budget unless one is declared. When declared, only wall-clock and worker-dispatch ceilings are enforced today. Model, tool, token, and cost dimensions stay observed unless an adapter owns an enforcement boundary. Campaigns aggregate ready work across repos, but do not yet compose parallel cross-repo waves. Center is read-only. External activity observations are best-effort. Cloud registry commands track work that another provider runs. Safe targeted handoffs may auto-file under owner policy. Ambiguous or risky notes wait for review.

Brigade is not a hosted memory service, automatic release bot, or full autonomous fleet runtime. It does not push to GitHub, publish packages, or send chat messages unless the operator uses an explicit send action. Name collisions: this is `brigade-cli` from [escoffier-labs/brigade](https://github.com/escoffier-labs/brigade), not the archived CNCF/Microsoft Kubernetes Brigade, Spinabot Brigade, or the 2017 Python package that became Nornir.

## Docs

- [First 10 minutes](docs/first-10-minutes.md) · [Overview](docs/overview.md) · [Technical guide](docs/technical-guide.md) · [Comparison](docs/comparison.md)
- [Execution model](docs/execution-model.md) · [Work closeout](docs/work-closeout.md) · [Outcome scoring](docs/outcome-scoring.md)
- [Memory care](docs/memory-care.md) · [Memory operations](docs/memory-operations.md) · [Code intelligence](docs/code-intelligence.md) · [MCP sync](docs/mcp-sync.md)
- [Agents guide](docs/agents-guide.md) · [Agent-assisted setup](docs/agent-assisted-setup.md) · [README coverage](docs/readme-coverage.md)
- [Security](docs/security.md) · [Command inventory](docs/command-inventory.md) · [Contributing](CONTRIBUTING.md) · [Roadmap](ROADMAP.md)

## License

MIT. See [LICENSE](LICENSE).

Project identity: GitHub [`escoffier-labs/brigade`](https://github.com/escoffier-labs/brigade), website [brigade.tools](https://brigade.tools), PyPI [`brigade-cli`](https://pypi.org/project/brigade-cli/), command `brigade`.
