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

<p align="center">
  <img src="https://shieldcn.dev/github/ci/escoffier-labs/brigade.svg?workflow=ci.yml&branch=main&label=ci&size=xs" alt="CI status">
  <img src="https://shieldcn.dev/pypi/v/brigade-cli.svg?label=pypi&size=xs" alt="PyPI version">
  <img src="https://shieldcn.dev/pypi/dm/brigade-cli.svg?size=xs" alt="PyPI downloads per month">
  <img src="https://shieldcn.dev/badge/python-3.10+-blue.svg?logo=python&logoColor=white&size=xs" alt="Python 3.10+">
  <img src="https://shieldcn.dev/badge/rust-code_graph-b7410e.svg?logo=rust&logoColor=white&size=xs" alt="Rust: code graph engine">
  <img src="https://shieldcn.dev/badge/go-evidence_log-00add8.svg?logo=go&logoColor=white&size=xs" alt="Go: evidence log engine">
  <img src="https://shieldcn.dev/badge/license-MIT-4e7247.svg?size=xs" alt="MIT license">
</p>

Published **v0.26.1** is the stable install (`brigade-cli==0.26.1`). Current main / **0.27 beta** adds per-repo parallel-safe work waves, cross-repo ready-work campaigns with query-time wave composition, Memory Operations, an optional fleet hub, vault projection, run lineage, activity and cloud status, code-graph views, bounded recall, and declared budgets. Cells below mark that split.

## What it does

- **Ready work and claim safety.** Brigade lists unblocked tasks and uses atomic claims so two agents cannot take the same item. Stable in v0.26.1.
- **Declared run limits, optional fleet hub, and registered cloud activity.** Current main / 0.27 beta enforces declared wall-clock and worker-dispatch ceilings, can report runs and repo claims through an operator-owned fleet hub, and reconciles registered cloud work against provider and GitHub state.
- **Verification receipts.** A check run through Brigade writes the command, the real exit code, and the Git state. Stable in v0.26.1. Not every Brigade command writes a receipt.
- **Owner-mediated handoffs and bounded recall.** Handoffs are linted and routed by the memory owner. Safe targeted notes may auto-file, while ambiguous or risky notes wait for review. Current main / 0.27 beta adds capped session-start recall and vault project, search, and propose.
- **Explicit dry-run-first harness projection.** MCP servers, tools, and skills are previewed before Brigade writes them into a harness. Stable in v0.26.1.

Learning in Brigade is outcome-based skill scoring, promotion, and rollback from captured verification receipts. It is not autonomous reflective memory learning.

## Install

Install with `pipx install brigade-cli` or `uv tool install brigade-cli`, then follow the [install guide](https://brigade.tools/docs/getting-started/install), [QUICKSTART.md](QUICKSTART.md), or give [first 10 minutes](docs/first-10-minutes.md) and [docs/agents-guide.md](docs/agents-guide.md) to your coding agent.

Stable channel: published v0.26.1. Preview channel: `brigade update --channel beta` for `0.27.0.devYYYYMMDD` wheels. Details: [update channels](docs/update-channels.md).

## How it compares

Brigade overlaps several categories. Most adjacent projects specialize in one layer. Brigade connects those layers around trackable coding-agent work. Where a neighbor is stronger, that cell says so. Dated sources and longer notes: [docs/comparison.md](docs/comparison.md) (2026-08-26).

Comparison key: <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Supported · <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Partial · <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented

### Work and orchestration

| Project | Ready work | Claim safety | Parallel and run control | Task-store boundary | Verification |
| --- | --- | --- | --- | --- | --- |
| [Brigade](https://github.com/escoffier-labs/brigade) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Stable ready set | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Fail-closed local CAS. 0.27: optional hub claims | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> 0.27: per-repo waves, campaign-composed waves, declared limits, fleet board | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Local JSON ledger. Optional hub for events/claims, not a task DB | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Command receipts |
| [Beads](https://github.com/gastownhall/beads) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Built in | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Atomic claim | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Agent workflow + Dolt sync | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Dolt-backed distributed store | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Task history |
| [Gas Town](https://github.com/gastownhall/gastown) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Beads-backed | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Multi-agent runtime | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Beads ledger | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Operational state |
| [nWave](https://github.com/nWave-ai/nWave) | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Wave artifacts | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> 7 reviewed phases + TDD gates | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Git-tracked artifacts | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Phase validation |
| [ActiveGraph](https://github.com/yoheinakajima/activegraph) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Reactive graph | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Application-defined | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Behaviors + runs | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Append-only event log | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Replay, fork, and diff |

### Memory

| Project | Durable memory | Capture and recall | Care and operations | Learning | Provenance |
| --- | --- | --- | --- | --- | --- |
| [Brigade](https://github.com/escoffier-labs/brigade) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Shared files + cards | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Handoffs. 0.27: bounded recall | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Owner policy. 0.27: Memory Operations, vault project/search/propose | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Outcome score + promote + rollback | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Work receipts + sources |
| [Mem0 / OpenMemory](https://github.com/mem0ai/mem0) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> User + agent + session | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Automatic + hybrid search | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> History + metadata | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Extraction + retrieval | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Timestamps + entities |
| [Letta](https://github.com/letta-ai/letta) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Agent state + blocks | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Agent-managed tools | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Editable state | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Persistent agent runtime | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Persistent agent state |
| [agentmemory](https://github.com/rohitg00/agentmemory) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Shared coding-agent memory | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Hooks + MCP + REST | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Lifecycle + decay | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Searchable memory |
| [Hindsight](https://github.com/vectorize-io/hindsight) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Facts + experiences + models | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Retain + recall | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Reflect + feedback | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Reflective memory learning | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Temporal + causal links |
| [Built-in harness memory](https://code.claude.com/docs/en/memory) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Tool-specific | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Rules + auto memories | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Varies by harness | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Harness-specific | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Harness-specific |
| [OpenClaw](https://docs.openclaw.ai/concepts/memory) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Plain files + index | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Keyword + optional vector search | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> File-owned memory | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Files + source paths |

### Tools and configuration

| Project | MCP | Reviewed rules and skills | Projection behavior | Source of truth | Audit and rollback |
| --- | --- | --- | --- | --- | --- |
| [Brigade](https://github.com/escoffier-labs/brigade) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> MCP + tools catalog | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Skills + adapters | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Dry-run-first, then apply | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Brigade catalog | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Preserve + gate + rollback |
| [add-mcp](https://github.com/neon-solutions/add-mcp) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> MCP servers | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Maps fields into native files | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Registry + native files | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Warnings for dropped fields |
| [Rulesync](https://github.com/dyoshikawa/rulesync) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Supported | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Rules + skills + hooks | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Per-target generation | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Rulesync project | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Generated outputs |
| [config-sync](https://pypi.org/project/aiconfigsync/) | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Rules for 12 tools | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Generated rule files | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Single config | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Generated outputs |
| [chezmoi](https://www.chezmoi.io/) | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> As files | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> As files | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Templates + apply | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Git-backed source dir | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Diff + templates + merge |
| [agentsync](https://agentsync.cc/) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Many agent targets | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Skills + hooks + commands | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Native/lossy/skipped report | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Canonical config | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Native/lossy/skipped report |

### Evidence and operations

| Project | Receipts | Code impact | Agent activity | Cloud state | Learning / replay |
| --- | --- | --- | --- | --- | --- |
| [Brigade](https://github.com/escoffier-labs/brigade) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Command + Git + optional impact | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Stable impact. 0.27: code-graph views + blast radius | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> 0.27: center activity + fleet board | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> 0.27: registered cloud status | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Score + promote + rollback. 0.27: run child/diff/resume |
| [ActiveGraph](https://github.com/yoheinakajima/activegraph) | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Event trace | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Reactive behaviors | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Replay, fork, and diff |
| [CocoIndex](https://github.com/cocoindex-io/cocoindex) | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Data lineage | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Incremental recompute |
| [Graphiti](https://github.com/getzep/graphiti) | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Episode provenance | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Temporal fact updates |
| [Hindsight](https://github.com/vectorize-io/hindsight) | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Reflective memory learning |
| [Neo4j Agent Memory](https://github.com/neo4j-labs/agent-memory) | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Reasoning + tool traces | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Similar-task retrieval |

"Not documented" means the capability was not found in the project's official README or documentation on the dated source pass. Brigade, Beads, and Gas Town were re-read on 2026-08-26. Other neighbor cells stay on the 2026-08-13 pass. An event trace, memory history, data lineage, and a command verification receipt answer different questions.

## Scope

Brigade is a local file-first control plane for coding agents. Nothing runs until a command or an operator-owned scheduler starts it. Optional serve commands (`brigade center serve`, `brigade runs serve`, `brigade fleet serve`) stay in the foreground until you stop them. Scheduling stays external, except optional operator-owned, target-scoped care registrations (`brigade care install`). The work ledger is machine-local JSON and is not a distributed task database. An optional fleet hub aggregates run events and arbitrates repo claims. Local journals stay authoritative. Ordinary runs have no hard run-budget unless one is declared. When declared, only wall-clock and worker-dispatch ceilings are enforced today. Model, tool, token, and cost dimensions stay observed unless an adapter owns an enforcement boundary. Campaigns aggregate ready work across repos and compose parallel-safe waves at query time from each member's per-repo partition. Center and the fleet board are read-only. External activity observations are best-effort. Cloud registry commands track work that another provider runs. Safe targeted handoffs may auto-file under owner policy. Ambiguous or risky notes wait for review.

Brigade is not a hosted memory service, automatic release bot, or full autonomous fleet runtime. It does not push to GitHub, publish packages, or send chat messages unless the operator uses an explicit send action. Name collisions: this is `brigade-cli` from [escoffier-labs/brigade](https://github.com/escoffier-labs/brigade), not the archived CNCF/Microsoft Kubernetes Brigade, Spinabot Brigade, or the 2017 Python package that became Nornir.

## Docs

- [First 10 minutes](docs/first-10-minutes.md) · [Overview](docs/overview.md) · [Technical guide](docs/technical-guide.md) · [Comparison](docs/comparison.md)
- [Execution model](docs/execution-model.md) · [Work closeout](docs/work-closeout.md) · [Outcome scoring](docs/outcome-scoring.md)
- [Memory care](docs/memory-care.md) · [Memory operations](docs/memory-operations.md) · [Code intelligence](docs/code-intelligence.md) · [MCP sync](docs/mcp-sync.md)
- [Agents guide](docs/agents-guide.md) · [Agent-assisted setup](docs/agent-assisted-setup.md) · [README coverage](docs/readme-coverage.md)
- [Runbooks](docs/runbooks/README.md) · [Fleet hub on a Proxmox LXC](docs/runbooks/fleet-hub-hogwarts.md)
- [Security](docs/security.md) · [Command inventory](docs/command-inventory.md) · [Contributing](CONTRIBUTING.md) · [Roadmap](ROADMAP.md)

## License

MIT. See [LICENSE](LICENSE).

Project identity: GitHub [`escoffier-labs/brigade`](https://github.com/escoffier-labs/brigade), website [brigade.tools](https://brigade.tools), PyPI [`brigade-cli`](https://pypi.org/project/brigade-cli/), command `brigade`.
