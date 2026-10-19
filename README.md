<p align="center">
  <img src="docs/assets/brigade-wordmark.svg" alt="Brigade (by Escoffier Labs)" width="640">
</p>

<p align="center">
  One shared record for every coding agent you run: ready work, safe claims, proof that checks passed, and memory that carries over between tools.
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

Brigade is a local CLI for people who point Claude Code, Codex, Cursor, and other agents at the same repos. Each harness keeps its own notes, its own MCP config, and its own idea of what "tests pass" means. Brigade gives them one ledger in the repo instead: what is ready, who claimed it, which commands actually ran and how they exited, and what the last session handed off.

**Stable:** v0.29.0 (`brigade-cli==0.29.0` on PyPI, `@escoffier-labs/brigade@0.29.0` on npm). **Beta:** main is the 0.30 preview line. See the [changelog](CHANGELOG.md) for what each release changed.

## What it does

- **Ready work and safe claims.** `brigade work ready` lists unblocked tasks. Claims are atomic, so two agents cannot take the same item.
- **Verification receipts.** A check run through Brigade records the command, the real exit code, and the Git state. An agent's "tests pass" becomes something you can open and read.
- **Shared memory with an owner.** Agents write handoffs. The memory owner lints and files them. Safe notes can auto-file, and risky ones wait for review. Session-start recall is capped.
- **One tool catalog, previewed first.** MCP servers, tools, and skills are defined once and shown as a dry run before Brigade writes them into each harness.
- **Cloud agents tracked next to local ones.** Tasks you hand to Codex Cloud, Cursor cloud agents, or Jules are registered with their provider task ID and reconciled against provider and GitHub state, so they show up beside local runs instead of disappearing into another tab.
- **Optional fleet view.** A self-hosted hub can arbitrate repo claims across machines and show runs, harness sessions, and cloud tasks on one board.

Learning in Brigade means scoring skills against captured receipts, then promoting or rolling them back. It is not autonomous reflective memory.

<details>
<summary><b>Examples</b></summary>

**Two agents, one backlog.** Each agent asks for the next ready task. The second claim on the same item fails with exit 13 and names the holder.

```bash
brigade work ready --parallel-safe          # ready tasks, grouped into waves that do not touch the same files
brigade work claim --next --actor codex     # take the highest-priority ready task
```

**"Tests pass" with a receipt.** The agent runs the check through Brigade instead of reporting it from memory.

```bash
brigade work verify run --target . --command "pytest -q"
```

The receipt keeps the command, exit code, Git commit, and dirty state, so a reviewer can check the claim without rerunning it.

**Switching harnesses mid-task.** Claude Code ends a session with a handoff. The next Codex session starts from the brief instead of a blank context.

```bash
brigade handoff lint                        # check the handoff before it is filed
brigade work brief --target .               # what the next agent sees at session start
```

**One MCP config for every tool.**

```bash
brigade mcp plan                            # read-only preview
brigade mcp sync --write                    # merge into each harness's native config
```

</details>

## Install

If Node.js 22 or newer and npm are installed on glibc Linux x64/ARM64, macOS x64/ARM64, or Windows x64, install the [npm package](https://www.npmjs.com/package/@escoffier-labs/brigade). This route does not need Python or pipx first:

```bash
npm install --global @escoffier-labs/brigade@0.29.0
brigade setup
```

To try it without a global install, run each command through npx instead, for example `npx --yes @escoffier-labs/brigade@0.29.0 --version`. The package brings its own uv, through platform packages that npm installs as optional dependencies, and the published 0.29.0 wheel. Leave optional dependencies enabled. The first launch downloads a managed Python, so it needs network access. Install Git yourself for repository features. npm targets are Linux (glibc) x64 and ARM64, macOS x64 and ARM64, and Windows x64. Upgrade with npm, because `brigade update` refuses npm-managed installs. Platform details and known limits: [npm package notes](npm/feasibility/README.md).

On other platforms, or to use Python 3.10 or newer directly:

```bash
pipx install brigade-cli      # or: uv tool install brigade-cli
brigade setup
```

Then follow the [install guide](https://brigade.tools/docs/getting-started/install) or [QUICKSTART.md](QUICKSTART.md). To let your coding agent do the setup, hand it [first 10 minutes](docs/first-10-minutes.md) and the [agents guide](docs/agents-guide.md).

For preview builds on a pipx install, run `brigade update --channel beta`. Details: [update channels](docs/update-channels.md).

## How it compares

Most neighbors go deeper on one layer. Brigade connects work, memory, tool config, and evidence around the same coding-agent task. Where a neighbor is stronger, its cell says so. Sources, dated notes, and projects that left these tables: [docs/comparison.md](docs/comparison.md) (checked 2026-10-18).

Key: <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Supported · <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Partial · <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented

### Work and orchestration

| Project | Ready work | Claim safety | Parallel and run control | Task store | Verification |
| --- | --- | --- | --- | --- | --- |
| [Brigade](https://github.com/escoffier-labs/brigade) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Ready set + parallel-safe waves | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Fail-closed claims, optional hub | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Waves, campaigns, declared limits | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Local JSON ledger, not a task DB | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Command receipts |
| [Beads](https://github.com/gastownhall/beads) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Built in | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Atomic claim | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Agent workflow + Dolt sync | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Dolt-backed distributed store | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Task history |
| [Gas Town](https://github.com/gastownhall/gastown) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Beads-backed | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Multi-agent runtime | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Beads ledger | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Refinery merge-queue gates |
| [Claude Code agent teams](https://code.claude.com/docs/en/agent-teams) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Shared list with dependencies | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> File-locked self-claim | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Lead + teammates, experimental | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> One Claude Code session | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> TaskCompleted hook can block |
| [Backlog.md](https://github.com/MrLesk/Backlog.md) | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Board + dependencies | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Markdown in Git | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Acceptance criteria |

### Memory

| Project | Durable memory | Capture and recall | Care and operations | Learning | Provenance |
| --- | --- | --- | --- | --- | --- |
| [Brigade](https://github.com/escoffier-labs/brigade) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Shared files + cards | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Linted handoffs + bounded recall | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Owner policy + Memory Operations | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Outcome score, promote, rollback | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Work receipts + sources |
| [agentmemory](https://github.com/rohitg00/agentmemory) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Shared coding-agent memory | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Hooks + MCP + REST | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Decay, audit, governed delete | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Lessons + reflect | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Write-time origin + citations |
| [Hindsight](https://github.com/vectorize-io/hindsight) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Facts + experiences + models | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Retain + recall | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Reflect + feedback | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Reflective memory learning | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Temporal + causal links |
| [Mem0](https://github.com/mem0ai/mem0) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> User + agent + session | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Extraction + hybrid search | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> History + metadata | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Extraction + retrieval | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Timestamps + entities |
| [Built-in harness memory](https://code.claude.com/docs/en/memory) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Per tool | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Rules + auto memory | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Varies by harness | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Harness-specific | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Harness-specific |

### Tools and configuration

| Project | MCP | Rules and skills | Projection | Source of truth | Audit and rollback |
| --- | --- | --- | --- | --- | --- |
| [Brigade](https://github.com/escoffier-labs/brigade) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> MCP + tools catalog | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Skills + harness adapters | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Dry run first, then write | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Brigade catalog | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Preserve, gate, roll back |
| [Rulesync](https://github.com/dyoshikawa/rulesync) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Supported | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Rules, skills, hooks, subagents | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Per-target generation | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Rulesync project | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Generated outputs |
| [Ruler](https://github.com/intellectronica/ruler) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Supported | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Rules, skills, subagents | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Per-agent files | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> `.ruler/` directory | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Gitignore automation, no rollback |
| [add-mcp](https://github.com/neon-solutions/add-mcp) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> MCP servers | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Maps fields into native files | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Native files + sync | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Warns on dropped fields |
| [agentsync](https://agentsync.cc/) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Many agent targets | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Skills, hooks, memory, plugins | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Native/lossy/skipped report | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Canonical config | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Native/lossy/skipped report |

### Evidence and operations

| Project | Receipts | Code impact | Agent activity | Cloud state | Replay and learning |
| --- | --- | --- | --- | --- | --- |
| [Brigade](https://github.com/escoffier-labs/brigade) | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Command + exit code + Git state | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Impact queries + blast radius | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Center, fleet board, harness sessions | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Codex, Cursor, Jules task status | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Score + rollback, run diff/resume |
| [ActiveGraph](https://github.com/yoheinakajima/activegraph) | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Event trace | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Reactive behaviors | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Replay, fork, and diff |
| [Graphiti](https://github.com/getzep/graphiti) | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Episode provenance | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Temporal fact updates |
| [Codex Cloud](https://developers.openai.com/codex/cloud) | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Changed files + test results | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented | <img src="docs/assets/status-partial.svg" width="10" height="10" alt="Partial"> Task review | <img src="docs/assets/status-supported.svg" width="10" height="10" alt="Supported"> Hosted task workspaces | <img src="docs/assets/status-undocumented.svg" width="10" height="10" alt="Not documented"> Not documented |

"Not documented" means the capability was not found in that project's official README or docs on the dated pass. It is not proof of absence.

## Scope

Brigade is a local, file-first control plane. Nothing runs until you run a command or your own scheduler does. The work ledger is machine-local JSON, not a distributed task database, and the optional fleet hub coordinates claims and events without replacing local records. Run budgets apply only when declared. Brigade does not push to GitHub, publish packages, or send messages unless you use an explicit send action. Full boundaries: [execution model](docs/execution-model.md).

Name collisions: this is `brigade-cli` from [escoffier-labs/brigade](https://github.com/escoffier-labs/brigade), not the archived CNCF Kubernetes Brigade, Spinabot Brigade, or the 2017 Python package that became Nornir.

## Docs

- [First 10 minutes](docs/first-10-minutes.md) · [Overview](docs/overview.md) · [Technical guide](docs/technical-guide.md) · [Comparison](docs/comparison.md)
- [Execution model](docs/execution-model.md) · [Work closeout](docs/work-closeout.md) · [Outcome scoring](docs/outcome-scoring.md)
- [Memory care](docs/memory-care.md) · [Memory operations](docs/memory-operations.md) · [Code intelligence](docs/code-intelligence.md) · [MCP sync](docs/mcp-sync.md)
- [Agents guide](docs/agents-guide.md) · [Agent-assisted setup](docs/agent-assisted-setup.md) · [README coverage](docs/readme-coverage.md)
- [Runbooks](docs/runbooks/README.md) · [Fleet hub on a Proxmox LXC](docs/runbooks/fleet-hub-proxmox.md)
- [Security](docs/security.md) · [Command inventory](docs/command-inventory.md) · [Contributing](CONTRIBUTING.md) · [Roadmap](ROADMAP.md)

## License

MIT. See [LICENSE](LICENSE).

Project identity: GitHub [`escoffier-labs/brigade`](https://github.com/escoffier-labs/brigade), website [brigade.tools](https://brigade.tools), PyPI [`brigade-cli`](https://pypi.org/project/brigade-cli/), npm [`@escoffier-labs/brigade`](https://www.npmjs.com/package/@escoffier-labs/brigade), command `brigade`.
