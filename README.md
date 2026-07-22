<p align="center">
  <img src="docs/assets/brigade-kitchen-scene.jpg" alt="Brigade - the brigade at the pass" width="900">
</p>

<h1 align="center">Brigade</h1>

<p align="center">
  <strong>Your agents run loops. Brigade keeps the receipts.</strong>
</p>

<p align="center">
  Coding agents do real work unsupervised now, and at the end of a session all you have is their word for what happened. Brigade makes the fleet auditable: every check leaves a receipt with the real exit code and the exact symbols the change touched, backed by a built-in code-intelligence graph and evidence ledger. It also keeps the plumbing shared: one MCP and tool catalog synced into every harness, and one reviewed memory that improves from real outcomes, never from a model grading itself. A CLI that writes plain files on your machine. No daemon, no lock-in.
</p>

<p align="center">
  <a href="https://brigade.tools">Website</a> · <a href="https://brigade.tools/docs">Docs</a> · <a href="#install">Install</a> · <a href="https://escoffierlabs.dev/cookbook/">Cookbook</a>
</p>

<p align="center">
  <img src="https://shieldcn.dev/github/ci/escoffier-labs/brigade.svg?workflow=ci.yml&branch=main&label=ci&size=xs" alt="CI status">
  <img src="https://shieldcn.dev/pypi/v/brigade-cli.svg?label=pypi&size=xs" alt="PyPI version">
  <img src="https://shieldcn.dev/pypi/dm/brigade-cli.svg?size=xs" alt="PyPI downloads per month">
  <img src="https://shieldcn.dev/badge/python-3.10+-blue.svg?logo=python&logoColor=white&size=xs" alt="Python 3.10+">
  <img src="https://shieldcn.dev/badge/rust-Code_engine-b7410e.svg?logo=rust&logoColor=white&size=xs" alt="Rust: the Code engine">
  <img src="https://shieldcn.dev/badge/go-Evidence_engine-00add8.svg?logo=go&logoColor=white&size=xs" alt="Go: the Evidence engine">
  <img src="https://shieldcn.dev/badge/license-MIT-4e7247.svg?size=xs" alt="MIT license">
</p>

<p align="center">
  <img src="docs/assets/brigade-demo.svg" alt="Recording: an agent claims tests pass; a verify run writes a receipt with the real exit code, code impact shows what the change touched, evidence search finds the run in the ledger, and outcome rank scores the skill that did the work" width="800">
</p>

<p align="center"><em>An agent said "tests pass." This is the claim becoming a record: receipt, graph, ledger, rank.</em></p>

## The loop

Every piece of work Brigade touches runs the same circuit:

1. Intent and acceptance criteria go in.
2. Prior evidence and code impact attach to the brief.
3. Replaceable workers execute, bounded.
4. Verification runs with a real exit code.
5. The receipt, graph delta, and outcome land where the next run starts.

Work enters as intent and leaves as evidence.

## Install

```bash
pipx install brigade-cli
pipx ensurepath           # then open a new shell so `brigade` is on PATH
brigade setup             # install the verified native engines
brigade operator quickstart --target ./my-repo --harnesses codex
```

brigade prints a one-line notice when a new release is out (checked at most
once a day via an anonymous request; set `BRIGADE_NO_UPDATE_CHECK=1` to
disable - details in [docs/update-channels.md](docs/update-channels.md)).

Stable pinners may deliberately install an exact release with
`pipx install brigade-cli==X.Y.Z` or refresh through
`brigade update --channel stable`. Channel ownership, beta rules, and when to
use `brigade update` are in [docs/update-channels.md](docs/update-channels.md).

`brigade operator doctor --target ./my-repo` prints `ready: yes` when the wiring is healthy. The default footprint is small: `AGENTS.md`, `SAFETY_RULES.md`, a handoff template, and `.brigade/` state. Add `--dry-run` to preview anything before it writes. Nothing leaves your machine.

Per-OS setup (apt, Homebrew, Scoop, PowerShell), workspace depth, and multi-harness installs: [install guide](https://brigade.tools/docs/getting-started/install), [QUICKSTART.md](QUICKSTART.md), [first 10 minutes](docs/first-10-minutes.md). Homegrown setup already? `brigade operator adopt plan`.

## Auditable agents: every action leaves a receipt

An agent that reports "tests pass, nothing else changed" is making a claim. Brigade turns the claim into a record. Run any check through Brigade and it writes a receipt: the command, the real exit code, the graph delta, the git state.

```bash
brigade work verify run --target . --command "pytest -q" --capture brigade-work
```

```jsonc
// .brigade/work/verify-runs/20260722-033355-work-verify-298ff5/receipt.json (abridged)
{
  "run_id": "20260722-033355-work-verify-298ff5",
  "status": "completed",
  "duration_seconds": 15.18,
  "commands": { "check": { "argv": ["pytest", "-q"], "returncode": 0 } },
  "code_graph_delta": { "changed_symbol_count": 0 },
  "git": { "branch": "main", "dirty_files": 0 }
}
```

Receipts land in the Evidence ledger, a Go engine installed by `brigade setup`, and every consequential action elsewhere in Brigade (a memory write, a skill promotion, a sync) is logged the same way. `brigade evidence search` answers "what ran, when, and what did it change" from files, weeks later. Cross-model dispatches through `brigade run` carry the same paper trail. When someone asks what your agents did this week, the answer comes from receipts you can grep, not from scrollback. [Capability page](https://brigade.tools/evidence-memory).

## Code intelligence, built in

The `code_graph_delta` line in that receipt comes from a Rust code-graph engine, also installed by `brigade setup` (digest-verified, no toolchain required). It indexes your repo once and keeps up incrementally. On this repository a sync pass over 652 files and 10,405 symbols reports in well under a second. A receipt names the exact symbols a change touched, and your agents stop grepping and start asking structural questions:

```
$ brigade code impact _write_receipt
test_runbook_closeout_imports_failed_steps  --calls@31--> _write_receipt
test_runbook_closeout_without_import_flag   --calls@45--> _write_receipt

$ brigade code context "verify receipts"
## Entry Points
- function `_verify_receipts` at src/brigade/work_cmd/verification.py:185-192
- function `_iter_verify_receipts` at src/brigade/workflow_cmd.py:142-167
```

The graph feeds the rest of Brigade instead of sitting idle: `brigade run` prepends a context pack when a graph exists, so a dispatched model starts from callers and blast radius instead of a cold grep, and an MCP server ships alongside so any harness can query the graph directly. [Capability page](https://brigade.tools/code-intelligence).

## One MCP and tool catalog, synced into every tool

Every agent tool reads its MCP servers from a different file in a different shape. The same servers wired across Claude Code, Cursor, Codex, VS Code, OpenCode, and Antigravity means hand-editing six configs and keeping them in sync forever. Brigade keeps one canonical catalog and merges it into each tool's native config for you.

```bash
brigade mcp init                  # scaffold .brigade/mcp.json
brigade mcp add --name github --command npx \
  --args "-y @modelcontextprotocol/server-github" \
  --env GITHUB_AUTH_ENV=ref:BRIGADE_GITHUB_AUTH_ENV
brigade mcp sync                  # dry-run: show the diff for every tool
brigade mcp sync --write          # merge into each tool's config
brigade mcp verify                # check initialize + tools/list at runtime
```

Run `brigade mcp sync` and you get the per-tool plan, server by server, before a single file changes:

```
brigade mcp sync (dry-run): ~/my-repo
claude       github               missing        -> create
claude       sentry               missing        -> create
cursor       github               missing        -> create
cursor       sentry               missing        -> create
codex        github               missing        -> create
codex        sentry               missing        -> create
vscode       github               missing        -> create
vscode       sentry               missing        -> create
opencode     github               missing        -> create
opencode     sentry               missing        -> create
```

| Tool | File it writes |
|---|---|
| Claude Code | `.mcp.json` |
| Cursor | `.cursor/mcp.json` |
| Codex CLI | `.codex/config.toml` (merged surgically, other tables preserved) |
| Grok CLI | `.grok/config.toml` (same TOML shape as Codex) |
| VS Code | `.vscode/mcp.json` (secrets become `inputs[]`) |
| OpenCode | `opencode.json` |
| Antigravity | `~/.gemini/config/mcp_config.json` (user-scoped) |

Dry-run by default. Merges by server key, so servers you added by hand are never touched. Secrets are written as `${VAR}` references, never inlined. Ownership lives in a gitignored sidecar, so a fresh clone re-syncs without conflict. Tools and skills get the same treatment via `brigade tools sync`: one reviewed catalog, projected into each harness's native format. Full merge rules: [docs/mcp-sync.md](docs/mcp-sync.md). Evaluating options first? [The comparison page](https://brigade.tools/compare/sync-mcp-servers-across-coding-agents).

## Shared memory and verified learning

Writer harnesses leave handoff notes as they work. Brigade lints, guards, and classifies each one. Safe, targeted notes file themselves into durable memory. The ambiguous few wait for your review. Every consequential action is logged to a plain file you can grep, diff, and prune.

<p align="center">
  <img src="docs/assets/memory-workflow.svg" alt="Brigade memory workflow: writer handoffs pass through linting, guards, and classification before reaching durable memory or review" width="820">
</p>

Memory stays two layers deep: knowledge cards hold the detail, `MEMORY.md` stays a slim one-line-per-card index that loads every session. `brigade memory care scan` flags stale or contradictory cards instead of letting them rot, and `brigade evidence search` plus exported briefs mean the next session starts where this one stopped.

Filing notes is the first loop. The second loop earns trust: Brigade promotes a learned skill only when a real signal proves it helped, and rolls it back the moment a signal says it broke. The model never grades its own work.

```
$ brigade outcome rank
- brigade-work      score=0.692  helped=675  hurt=260
- ultra-work-scout  score=0.563  helped=50   hurt=24
- memory-handoff    score=0.490  helped=8    hurt=2
```

- `brigade outcome capture` records a verify run's real exit code against the skill that produced it.
- `brigade outcome score` ranks by a Wilson lower bound, so two lucky passes never outrank twenty vetted runs.
- `brigade outcome reconcile` is the gate: dry-run by default, `--apply` installs a skill that earned it or rolls a regressed one back.
- `brigade outcome explain` prints the full signal trail behind any decision, so every promotion is as auditable as the runs that earned it.

The ledger is plain JSON and markdown under `memory/outcome/`, tracked in git, readable without Brigade. `brigade init` wires a `brigade-work` skill into each harness so agents run this loop without being told. With Claude Code it also installs project-scoped hooks that redirect raw test commands through verify runs.

## Optional stations

Code intelligence, Evidence, and Content Guard (`brigade scrub`, a secrets and PII scan before anything goes public) are built in and installed by `brigade setup`. Everything else is an optional station in its own repo. Core works with none installed, and `brigade status` health-checks whatever is present.

| Station | Install | Role |
|---|---|---|
| [Agent Pantry](https://github.com/escoffier-labs/agentpantry) | `brigade add pantry` | Encrypted browser-session and secret sync across machines |
| [Token Glace](https://github.com/escoffier-labs/token-glace) | `brigade add tokens` | Compact noisy tool output before it burns context |
| [Skillet](https://github.com/escoffier-labs/skillet) | optional roster | Portable skills that reconcile can promote or roll back |
| Notifications | `brigade add notifications` | Optional `agent-notify` binary for Discord, Telegram, or Signal; status and setup planning only until you wire hooks or pass an explicit `--send` |

Upgrading from the standalone GraphTrail or MiseLedger installs? `brigade setup` replaces both. The old `brigade add graphtrail` / `add evidence` paths remain as compatibility shims. Details: [wiring guide](docs/wiring-graphtrail-miseledger.md), [station contract](docs/station-contract.md).

Beyond the daily loop, the same review-and-receipt pattern covers cross-model runs (`brigade run` dispatches one bounded task across your roster), security scans, friction mining, research reports, and fleet health. All of it stays behind `brigade extras on` until you ask. The full tour: [docs/overview.md](docs/overview.md).

## Why not something else?

- **mem0, Letta, agentmemory, and friends** are memory layers for apps you are building, usually behind an API or a server. Brigade is for the agent CLIs you already run, and it is file-first: your memory is markdown in your repo, reviewable in git, readable without Brigade.
- **add-mcp, chezmoi, and config-sync scripts** move MCP or dotfiles around, but they sync one thing with no review gate and no receipt. Brigade keeps MCP servers, tools, skills, and memory in one canonical source, shows the per-tool diff before any write, and leaves a receipt you can roll back.
- **Native harness memory** is a per-tool silo. It does not cross harnesses, and it writes without review. Brigade gives every tool one shared format and one canonical owner, with a review gate in between.
- **Already running Hermes, or any self-improving agent?** Keep it. Brigade is the verification layer on top: it promotes a skill only when a real signal confirms it, keeps every learned skill as portable markdown in your git, and runs one loop across your whole fleet.
- **A plain CLAUDE.md / AGENTS.md** works great until it bloats past the context budget and goes stale. Brigade keeps bootstrap files slim, moves detail into indexed cards, and flags staleness instead of trusting last month's facts forever.
- **A daemon or hosted service** would be simpler to demo and worse to trust. Brigade writes local files when you run a command, and that is all it does.

| | Across harnesses | MCP, tools, and memory in one source | Review gate + receipts | Local files, no daemon |
|---|:---:|:---:|:---:|:---:|
| **Brigade** | yes | yes | yes | yes |
| mem0 / Letta / agentmemory | per-SDK | memory only | no | usually hosted or a server |
| add-mcp / chezmoi / config-sync | partial | MCP or dotfiles only | no | yes |
| Native harness memory | no | memory only | no | yes |

## What Brigade is not

Brigade is not a hosted memory service, a daemon, or an automatic release bot. It does not run in the background or install schedulers (one scoped exception: `brigade tools runtime start` launches a local runtime process, only when you start it, until you stop it). It does not push to GitHub, publish packages, save every note automatically, or skip review for ambiguous, risky, or failed notes. `brigade work brief` and related status surfaces may report notification readiness or suggest installing the notifications station, but Brigade never sends a message unless the operator uses an explicit send action such as `brigade pantry expiry-alert --send`. That pause is the point: agent memory should be useful, not noisy.

And it is not the other projects that share the name. This Brigade is the AI-agent operator CLI from [`escoffier-labs/brigade`](https://github.com/escoffier-labs/brigade), installed with `pipx install brigade-cli`. It is not the CNCF/Microsoft Brigade for Kubernetes event scripting (archived 2022), the Spinabot Brigade agent crew, or the 2017 `brigade` Python package that became Nornir.

## Why I built this

<p align="center">
  <img src="docs/assets/brigade-social-preview.jpg" alt="Brigade - le chef de cuisine" width="900">
</p>

I run an always-on OpenClaw agent next to daily Codex and Claude Code sessions. Every one of those tools wakes up empty, and whatever a session learned scattered across tool-specific folders and died there. Two incidents shaped the design: a "dreaming" job that promoted raw session fragments straight into memory bloated `MEMORY.md` past the bootstrap budget, so every session started truncated and nobody noticed for weeks. And 195 handoff notes sat unread across 35 repos because an ingester had a hardcoded allowlist and nothing warned about the gap. Silence is the failure mode. Every part of Brigade that lints, warns, or writes a receipt exists because something once failed in silence. The full production stack, now 482 cards across daily multi-agent work, is documented in the [Cookbook](https://escoffierlabs.dev/cookbook/).

## Harnesses

Nineteen harnesses get handoff inboxes and ingest coverage, from Codex, Claude Code, and Cursor to Goose, Aider, and OpenHands. Most also get projected tools and skills in their native format. The per-harness matrix is in the [technical guide](docs/technical-guide.md).

## Docs

- [First 10 minutes](docs/first-10-minutes.md) · [Overview](docs/overview.md) · [Technical guide](docs/technical-guide.md)
- [MCP sync](docs/mcp-sync.md) · [Security and Content Guard](docs/security.md) · [Handoff promotion](docs/handoff-promotion.md)
- [Command inventory](docs/command-inventory.md) · [Station contract](docs/station-contract.md)
- [Maintainers](MAINTAINERS.md) · [Governance](GOVERNANCE.md) · [Security](SECURITY.md) · [Contributing](CONTRIBUTING.md) · [Roadmap](ROADMAP.md)

## License

MIT. See [LICENSE](LICENSE).

Project identity: GitHub [`escoffier-labs/brigade`](https://github.com/escoffier-labs/brigade), website [brigade.tools](https://brigade.tools), PyPI [`brigade-cli`](https://pypi.org/project/brigade-cli/), command `brigade`. The name comes from the kitchen: a *brigade de cuisine* runs the line, and *mise en place* means the station is prepped before service. Set up the rules, memory, tools, and receipts before the session gets expensive.

It is early-stage and moving fast. If you hit a broken workflow, a confusing command, or a setup issue, [open an issue](https://github.com/escoffier-labs/brigade/issues) and I will get it fixed.
