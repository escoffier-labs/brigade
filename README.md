<p align="center">
  <img src="docs/assets/brigade-social-preview.jpg" alt="Brigade" width="900">
</p>

<h1 align="center">Brigade CLI</h1>

<p align="center">
  <strong>AI agent memory, handoffs, and local guardrails for Codex, Claude Code, OpenCode, and a dozen other harnesses.</strong>
</p>

<p align="center">
  <img src="https://img.shields.io/github/actions/workflow/status/escoffier-labs/brigade/ci.yml?branch=main&style=for-the-badge&label=ci" alt="CI status">
  <img src="https://img.shields.io/pypi/v/brigade-cli?style=for-the-badge&label=pypi" alt="PyPI version">
  <img src="https://img.shields.io/badge/python-3.10%2B-blue?style=for-the-badge&logo=python&logoColor=white" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/license-MIT-green?style=for-the-badge" alt="MIT license">
</p>

Your agents run loops. Brigade keeps the receipts.

## Why I built this

I run an always-on OpenClaw agent next to daily Codex and Claude Code sessions, and I have since January. Every one of those tools wakes up empty. Whatever a session learned about my machine, my rules, or yesterday's dead ends scattered across tool-specific folders and died there.

So I hand-rolled the fixes, one incident at a time: a slim `MEMORY.md` index pointing at small memory cards instead of one giant file, a handoff note format every harness could write, an ingest cron that filed the good notes into durable memory every 30 minutes, staleness checks so old cards stopped being trusted forever.

Two incidents shaped the design more than anything I planned. First, a nightly "dreaming" job that auto-promoted session fragments quietly bloated `MEMORY.md` to 41KB, way past the 12KB bootstrap budget, so every session started with truncated memory and nobody noticed for weeks. Auto-promotion died that day. Everything goes through review now. Second, I found 195 handoff notes sitting unread across 35 repos because the ingester had a hardcoded three-repo allowlist and nothing warned about the coverage gap. Silence is the failure mode. Every part of Brigade that lints, warns, or writes a receipt exists because something once failed quietly.

That system now runs 482 memory cards and survives daily multi-agent work. But explaining it to anyone meant: clone six repos, write these crons, keep your index slim, watch for staleness, and whatever you do, turn auto-promotion off. Brigade is that setup packaged as one installable CLI. The full production stack is documented in the [solos-cookbook](https://github.com/solomonneas/solos-cookbook) if you want to see where it came from.

## The loop

Writer harnesses leave handoff notes as they work. A memory owner (OpenClaw, Hermes, or just you) ingests the ones worth keeping. Brigade lints, guards, and routes everything in between, and every consequential action lands a receipt in a plain file you can grep, diff, and prune.

1. agents write handoff notes into their own local inboxes
2. Brigade lints and scans them before they can become memory
3. safe targeted notes get filed into durable memory by the owner
4. ambiguous or risky notes wait for your review
5. future sessions start with better context, and receipts show what happened

```mermaid
flowchart LR
    WRITERS["writer harnesses<br/>Codex · Claude Code · OpenCode · ..."]
    BRIGADE["Brigade<br/>lint · guard · route · receipts"]
    REVIEW["operator review<br/>safe · ambiguous · risky"]
    OWNER["memory owner<br/>OpenClaw / Hermes / you"]
    MEM["durable memory<br/>MEMORY.md index · memory cards"]

    WRITERS -- handoff notes --> BRIGADE --> REVIEW
    REVIEW -- safe targeted notes --> OWNER --> MEM
    MEM -. context .-> WRITERS

    classDef brigade fill:#2563eb,stroke:#1d4ed8,color:#fff;
    classDef memory fill:#ecfdf5,stroke:#059669,color:#064e3b;
    classDef gate fill:#fff7ed,stroke:#ea580c,color:#7c2d12;
    class BRIGADE brigade;
    class OWNER,MEM memory;
    class REVIEW gate;
```

Memory has two layers: knowledge cards under `memory/cards/` hold the detail, and `MEMORY.md` stays a slim one-line-per-card index that loads every session. `brigade memory care scan` flags stale, contradictory, or undersourced cards for review instead of letting them rot. Brigade never edits canonical memory itself; the owner does the writing.

It all runs on the machine you control: laptop, workstation, or VPS. Local by default, loud about the exceptions.

## Install

```bash
pipx install brigade-cli
brigade operator quickstart --target ./my-repo --harnesses codex
brigade operator doctor --target ./my-repo --profile local-operator
```

For an OpenClaw or Hermes workspace instead of a code repo:

```bash
brigade operator quickstart --target ~/agent-workspace --depth workspace --harnesses openclaw,hermes --owner openclaw
```

Use `--dry-run` first to preview the planned steps without writing anything; `brigade init --target ./my-repo --harnesses codex --dry-run` shows the full file-by-file list. Pass more harnesses as a comma-separated list. Quickstart only wires the harnesses you select and leaves the rest alone.

Write a handoff and check the wiring:

```bash
brigade handoff draft --target ./my-repo --inbox codex \
  --title "What changed" \
  --summary "Short note future agents should know." \
  --content "The durable note itself goes here."
brigade handoff lint --target ./my-repo
brigade handoff doctor --target ./my-repo
```

New here? Start with [docs/first-10-minutes.md](docs/first-10-minutes.md). Already have a homegrown setup with scripts, crons, and handoff folders? Brigade has an adoption path that inventories what you have before changing anything: start with `brigade operator adopt plan` and see the [technical guide](docs/technical-guide.md). Want an agent to set this up for you? Point it at this repo; [AGENTS.md](AGENTS.md) tells it exactly what to do and where to stop.

## Harness support

Each writer gets its own local inbox; one canonical owner ingests. Brigade keeps the note format consistent so different tools can contribute without inventing their own styles.

| Writer | Harness id | Inbox |
|---|---|---|
| Codex CLI | `codex` | `.codex/memory-handoffs/` |
| Claude Code | `claude` | `.claude/memory-handoffs/` |
| OpenCode | `opencode` | `.opencode/memory-handoffs/` |
| Antigravity | `antigravity` | `.antigravity/memory-handoffs/` |
| Pi | `pi` | `.pi/memory-handoffs/` |
| Cursor | `cursor` | `.cursor/memory-handoffs/` |
| Aider | `aider` | `.aider/memory-handoffs/` |
| Goose | `goose` | `.goose/memory-handoffs/` |
| Continue | `continue` | `.continue/memory-handoffs/` |
| GitHub Copilot CLI | `copilot` | `.copilot/memory-handoffs/` |
| Qwen Code | `qwen` | `.qwen/memory-handoffs/` |
| Kimi Code | `kimi` | `.kimi/memory-handoffs/` |
| AdaL | `adal` | `.adal/memory-handoffs/` |
| OpenHands | `openhands` | `.openhands/memory-handoffs/` |
| Hermes | `hermes` | `.hermes/memory-handoffs/` |
| OpenClaw | `openclaw` | usually the memory owner, not a writer |

All of them get handoff templates, ingest source coverage, and projected tools/skills. Per-harness details are in the [technical guide](docs/technical-guide.md).

## Beyond memory

The memory loop is the core. Around it, the same review-and-receipt pattern covers the rest of an operator's day, and you can ignore all of it until you need it:

- **Daily loop**: `brigade work brief` shows pending work, imports, and warnings; `brigade daily status` keeps it bounded and cheap.
- **Security**: `brigade security scan` is a local read-only scanner for agent workspaces (secrets, risky hooks, MCP configs, prompt-injection patterns); `brigade scrub` gates content before it leaves the machine.
- **Tools and skills**: one reviewed catalog projected into every harness's native format, with approval gates for anything that executes.
- **Research**: `brigade research run` turns a question into a cited local report and a reviewable memory handoff.
- **Fleet and release**: health evidence across your local repos and release-readiness receipts, with no publish step.

The full tour of every station lives in [docs/overview.md](docs/overview.md).

## Why not something else?

- **mem0, Letta, and friends** are memory layers for apps you are building, usually behind an API or a server. Brigade is for the agent CLIs you already run, and it is file-first: your memory is markdown in your repo, reviewable in git, readable without Brigade.
- **Native harness memory** (each tool's own auto-memory) is a per-tool silo. It does not cross harnesses, and it writes without review. Brigade gives every tool one shared format and one canonical owner, with a review gate in between.
- **A plain CLAUDE.md / AGENTS.md** works great until it bloats past the context budget and goes stale. Brigade exists because mine hit 41KB. It keeps bootstrap files slim, moves detail into indexed cards, and flags staleness instead of trusting last month's facts forever.
- **A daemon or hosted service** would be simpler to demo and worse to trust. Brigade writes local files when you run a command, and that is all it does.

## What Brigade is not

Brigade is not a hosted memory service, a daemon, or an automatic release bot.

It does not:

- run in the background or install schedulers
- push to GitHub or publish packages
- send notifications by default
- save every note automatically
- turn memory ingest into a silent background process
- skip review for ambiguous, risky, or failed notes

That pause is the point. Agent memory should be useful, not noisy.

## Docs

- [First 10 minutes](docs/first-10-minutes.md): shortest path from install to healthy setup.
- [Overview](docs/overview.md): the full tour of every station and diagram.
- [Technical guide](docs/technical-guide.md): the detailed command walkthrough.
- [Security and Content Guard](docs/security.md): scanner policies, handoff guards, import flow.
- [Handoff promotion](docs/handoff-promotion.md): how notes move toward memory.
- [Repo fleet](docs/repo-fleet.md) and [Tool catalog](docs/tool-catalog.md).
- [Command inventory](docs/command-inventory.md): every public CLI command.
- [Roadmap](ROADMAP.md) and [roadmap archive](docs/roadmap-archive.md).

Project identity: GitHub [`escoffier-labs/brigade`](https://github.com/escoffier-labs/brigade), website [brigade.tools](https://brigade.tools), PyPI [`brigade-cli`](https://pypi.org/project/brigade-cli/), command `brigade`. The name comes from the kitchen: a *brigade de cuisine* runs the line, and *mise en place* means the station is prepped before service. Set up the rules, memory, tools, and receipts before the session gets expensive.

It is early-stage and moving fast. If you hit a broken workflow, a confusing command, or a setup issue, [open an issue](https://github.com/escoffier-labs/brigade/issues) and I will get it fixed.
