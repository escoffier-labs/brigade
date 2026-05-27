# Brigade Roadmap

Brigade is being built as a practical daily workflow first, then a portable setup other people can adapt. The core direction is an organized version of real agent work: one command to start, predictable local artifacts, reviewable memory handoffs, and enough inspection to trust the loop during normal work.

## Current Phase: Daily Driver

Status: in progress.

- Local dogfood defaults live in gitignored `.brigade/dogfood.toml`.
- `brigade work bootstrap` prepares a repo for the daily loop.
- `brigade work brief` is the start-of-day entrypoint.
- `brigade work run` wraps a dogfood run in local work-session artifacts and handoffs.
- `brigade work tasks` plus `brigade work task add/show/done` provide a gitignored local task ledger.
- `brigade work run --queue-next` queues extracted follow-up work without duplicating equivalent pending tasks.
- `brigade work import add/list/show/promote` gives scanners and wrappers a stable local inbox for candidate work.

## Next Phase: Scanner-Ready Inbox

Status: active.

Goal: make Brigade a safe target for local automations that discover useful work.

- Keep raw scanner output private and gitignored under `.brigade/work/imports/`.
- Normalize imports into small records with `kind`, `source`, text, timestamps, and metadata.
- Document the scanner JSONL contract so external producers can target Brigade without importing Brigade internals.
- Validate and ingest scanner-authored JSONL files.
- Let wrappers import candidate tasks, findings, decisions, preferences, incidents, links, and commands without knowing Brigade internals.
- Convert memory-care refresh queues into local task imports.
- Promote selected imports into the work task ledger, with source metadata preserved.
- Dismiss noisy imports so scanners can be useful without leaving permanent queue clutter.
- Batch-promote reviewed imports by source and kind.
- Surface pending imports and grouped counts in `brigade work brief` so discovered work appears in the daily flow.

## Later Phase: Chat Surface Scanners

Goal: support the common places agent work happens without making any one chat product mandatory.

- Build adapters for Discord, Slack, Telegram, and export-based chat archives as separate scanner layers.
- Convert surface-specific events into the local import inbox instead of writing memory directly.
- Summarize private chat evidence, do not quote raw third-party messages into public docs or handoffs.
- Use promotion gates so only reviewed, durable, or actionable items become tasks or memory handoffs.
- Keep source metadata such as workspace, channel, thread, message range, and confidence local unless explicitly exported.

## Later Phase: Memory Card Decay And Refresh

Goal: prevent durable memory from silently rotting.

- Track freshness metadata, confidence, evidence, and review dates for memory cards.
- Run memory-care scanners that detect expired, stale, contradictory, or undersourced cards.
- Import refresh candidates into Brigade as local work imports.
- Promote refresh candidates into tasks or memory handoffs after review.
- Auto-fix only within safe gates where source evidence is current, low-risk, and locally reviewable.
- Treat bootstrap truncation as a hard failure. Bootstrap files stay slim, cards hold durable detail, and doctor checks enforce the boundary.

## Later Phase: Portable Operator Setup

Goal: keep the system usable by the original operator while making it adaptable by others.

- Keep Codex-first defaults, with Claude Code, OpenCode, Hermes, OpenClaw, and generic harness paths supported through writer-specific inboxes.
- Make local paths configurable and gitignored.
- Provide templates for fresh-start users without publishing private workspace state.
- Keep public repo docs focused on patterns, commands, and safety contracts.
- Leave release, tag, push-to-main, and production-impacting actions behind explicit approval gates.
