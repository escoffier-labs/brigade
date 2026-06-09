# Brigade Roadmap

Brigade is built as a practical daily workflow first, then a portable setup other people can adapt. The core direction: one command to start, predictable local artifacts, reviewable memory handoffs, and enough inspection to trust the loop during normal work.

This file is direction-only. The full per-feature completion detail through v0.8.x (every implemented, strengthened, and started bullet) lives in the [roadmap archive](docs/roadmap-archive.md). The generated list of every public command is in [docs/command-inventory.md](docs/command-inventory.md).

## Vocabulary

- **harness**: an AI agent program (Claude Code, Codex, OpenClaw, Hermes).
- **operator**: you, the human running the agents.
- **handoff**: a memory note an agent writes to be saved long-term.
- **ingest**: reading those notes and filing them into permanent memory.
- **scanner**: an automation that goes looking for useful work.
- **import / inbox**: a holding queue where found work waits for your review.
- **receipt**: a local file logging that something happened, kept for audit and proof.
- **gate**: a manual approval checkpoint; nothing risky happens without your yes.
- **dogfood**: Brigade being used on itself or the maintainer's real setup.

The one rule behind all of it: Brigade writes local files and queues, but it never publishes, edits canonical memory, runs background daemons, or touches remote servers on its own. Everything waits for an explicit command.

## Where things stand

The base layers are built and shipped: portable setup (`operator quickstart`), memory handoffs with lint and source coverage, the local security scanner and Content Guard integration, the daily operator loop, the scanner-ready work inbox, repo fleet evidence, release readiness receipts, the tool catalog, the skill registry, deep research, and runbook execution. See the [archive](docs/roadmap-archive.md) for the complete record.

What follows is what is actually ahead.

## Now: prove it on a real homegrown setup

The highest-priority work is making Brigade adapt and operate a real, organically grown operator workspace, not just fresh repos. If Brigade cannot take over the maintainer's own stack (OpenClaw cron, shell crontab, PM2 services, a live 30-minute ingest loop), it will not work for anyone else's homegrown system either.

- Drive the adoption loop (`brigade operator adopt`, `operator surfaces`, `operator migration`) to an actual completed cutover on a production workspace, and fix every rough edge found on the way.
- Convert reviewed shell-cron jobs into explicit `brigade runbook` definitions, starting with the memory-ingest wrapper.
- Keep first contact clean: quickstart writes only what the selected harnesses need, doctors stay quiet on healthy setups, and new-user issues get fixed fast.

## Next: the surfaces around the loop

- **Chat surface scanners**: pull reviewable work items out of chat exports (Discord, Slack, Telegram, and friends) through the same import inbox, summarized rather than quoted, never written to memory directly.
- **Backup and recovery visibility**: snapshot age, check results, and restore-rehearsal dates as part of the same daily loop, with stale or failed backups becoming inbox incidents.
- **Security plugin depth**: richer rule packs for agent workspaces (hooks, MCP configs, prompt-injection patterns), policy packs per audience, and optional offline threat-intel enrichment.
- **Memory care depth**: smarter staleness, contradiction, and evidence checks for cards, with safe gated metadata repairs.

## Later: the workspace on top of the bones

The CLI is the load-bearing skeleton. Every future surface sits on top of an existing command plus its JSON contract, never a parallel implementation.

- A workspace UI that is a view over the CLI: model comparison, a document editor, a viewer for research reports and operator-center state.
- Optional local semantic memory retrieval (on-device embeddings over `memory/cards/`), staying file-first and optional.
- Owner-scoped tool gating so a publicly reachable instance refuses high-risk tools by default.
- Multi-channel operator notifications beyond the terminal, still opt-in.
- Personal-data surfaces such as calendar and email triage, behind the same privacy and approval gates.

## How items move

Roadmap items live here while they are direction. When a slice ships, its detail moves to the [roadmap archive](docs/roadmap-archive.md) with status and closing notes, checked by `brigade roadmap audit` and `brigade roadmap archive`. Command drift between docs and the CLI parser is checked by `brigade roadmap commands --check` in CI.
