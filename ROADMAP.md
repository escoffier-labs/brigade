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

The base layers are built and shipped: portable setup (`operator quickstart`), memory handoffs with lint and source coverage, the local security scanner and Content Guard integration, the daily operator loop, the scanner-ready work inbox, repo fleet evidence, release readiness receipts, the tool catalog, the skill registry, deep research, runbook execution, chat surface sweeps, and backup health visibility. See the [archive](docs/roadmap-archive.md) for the complete record.

What follows is what is actually ahead.

## Now: respond to real usage

The proving-ground milestone is done: the maintainer's production workspace (live cron surfaces, a 30-minute ingest loop, ~500 memory cards) runs on Brigade end to end, including the adoption loop, metadata backfill, and the first runbook conversions. The archive holds the detail.

What "now" means after that shift:

- Treat the first external signals (issues, threads, install patterns) as the feature queue. Nothing speculative ships ahead of a reported need.
- Keep first contact honest: every release runs the cold-start gate (`docs/runbooks/cold-start-gate.json`), and significant releases re-run the agent cold-start scenarios in `docs/cold-start-testing.md`.
- Keep the adoption path smooth as real homegrown setups arrive: `operator adopt`, `handoff migrate`, and the memory-care backfill are the on-ramp, and friction found there outranks new stations.

## Next: the surfaces around the loop

- **Security plugin depth**: richer rule packs for agent workspaces (hooks, MCP configs, prompt-injection patterns), policy packs per audience, and optional offline threat-intel enrichment.
- **Memory care depth**: smarter staleness, contradiction, and evidence checks for cards, with safe gated metadata repairs.

Chat surface scanners (`brigade chat surfaces`, `brigade chat sweep`) and backup and recovery visibility (`brigade work backup`) shipped and moved to the archive; their remaining slices (scheduler spreading, outbound backup notifications) are tracked there and under Later notifications.

## Later: the workspace on top of the bones

The CLI is the skeleton that carries everything. Every future surface sits on top of an existing command plus its JSON contract, never a parallel implementation.

- A workspace UI that is a view over the CLI: model comparison, a document editor, a viewer for research reports and operator-center state.
- Optional local semantic memory retrieval (on-device embeddings over `memory/cards/`), staying file-first and optional.
- Owner-scoped tool gating so a publicly reachable instance refuses high-risk tools by default.
- Multi-channel operator notifications beyond the terminal, still opt-in.
- Personal-data surfaces such as calendar and email triage, behind the same privacy and approval gates.

## How items move

Roadmap items live here while they are direction. When a slice ships, its detail moves to the [roadmap archive](docs/roadmap-archive.md) with status and closing notes, checked by `brigade roadmap audit` and `brigade roadmap archive`. Command drift between docs and the CLI parser is checked by `brigade roadmap commands --check` in CI.
