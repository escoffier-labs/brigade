# Brigade Announcement Posts

Channel-ready text, rewritten 2026-06-09 in the README's voice. The origin story is the hook; lead with it, not the feature list. Numbers below are real and public-safe. Update version numbers before posting.

## Relaunch post (v0.13 - lead with the recording)

> Refreshed 2026-06-26 for the v0.13 relaunch. Lead with the recording, not the feature list. The quickstart was re-verified on a clean machine the day this was written.

**The hook:** the 60-second quickstart, recorded on a clean machine. `brigade operator quickstart` wires a repo and `brigade operator doctor` reports `ready: yes`, nothing hidden. Embed `docs/assets/quickstart.svg` (or a GIF render of it) at the top of the post.

**One-sentence re-explanation (every post re-explains the project):** Brigade is a local CLI that gives your AI coding agents one shared memory and one MCP catalog across every tool, with a review gate and a receipt for every change. No daemon, no server.

**What's new in this wave (v0.13):** one MCP catalog synced into every tool's native config (Claude Code, Cursor, Codex, VS Code, OpenCode, Antigravity), dry-run by default, with a server-by-server diff before anything is written. Plus a verified 60-second quickstart.

**Why it exists (the proof):** a nightly auto-promotion job bloated the always-loaded memory index to 41KB past a 12KB budget, and 195 handoff notes sat unread across 35 repos behind a hardcoded allowlist. Every lint, warning, and receipt is scar tissue from something that once failed in silence. That system now runs ~500 memory cards across six months of daily multi-agent work.

**Install:** `pipx install brigade-cli` then `brigade operator quickstart --target ./my-repo --harnesses codex`
**Repo:** https://github.com/escoffier-labs/brigade  ·  **Site:** https://brigade.tools

**Social one-liner (X / Bluesky / Discord):** Brigade v0.13: one shared memory and MCP catalog for every AI coding agent you run, with a review gate and receipts. Local files, no daemon. Watch it wire a repo in 60s. `pipx install brigade-cli` 🦞 built on @openclaw

## Show HN

**Title:** Show HN: Brigade - local memory, handoffs, and guardrails for AI coding agents

**Body:**

I run an always-on agent (OpenClaw) next to daily Codex and Claude Code sessions, and I have since January. Every one of those tools wakes up empty. Whatever a session learned about my machine, my rules, or yesterday's dead ends scattered across tool-specific folders and died there.

So I hand-rolled fixes, one incident at a time: a slim memory index pointing at small markdown cards, a handoff note format every harness could write, an ingest cron that filed the good notes into durable memory every 30 minutes, staleness checks so old facts stopped being trusted forever.

Two incidents shaped the design. A nightly job that promoted raw session fragments straight into memory bloated my memory index to 41KB, past the 12KB bootstrap budget, so every session started with truncated memory and nobody noticed for weeks. Blind auto-promotion died that day; now nothing reaches memory unlinted, the safe notes file themselves and only the risky few wait for review. Later I found 195 handoff notes sitting unread across 35 repos because the ingester had a hardcoded three-repo allowlist and nothing warned about the coverage gap. Silence is the failure mode. Every part of Brigade that lints, warns, or writes a receipt exists because something once failed in silence.

That system now runs ~500 memory cards across six months of daily multi-agent work. Brigade is it packaged as one installable CLI: agents write handoff notes into local inboxes, Brigade lints and classifies them (including prompt-injection signals), safe notes file themselves into durable memory under one canonical owner, only ambiguous ones wait for review, and every consequential action lands a receipt in a plain file you can grep.

Deliberate non-features: no daemon, no server, no hosted anything, no auto-publish, no silent memory writes. Your memory is markdown in your repo, readable without Brigade.

It supports 18+ writer harnesses (Codex, Claude Code, OpenCode, Cursor, Aider, Goose, Copilot CLI, ...) with one shared note format, and it has an adoption path that inventories an existing homegrown setup read-only before changing anything. I cut my own production workspace over to it this week using that path.

Install: `pipx install brigade-cli` - then `brigade operator quickstart --target ./my-repo --harnesses codex`

Repo: https://github.com/escoffier-labs/brigade
Site: https://brigade.tools
The full production stack it came from: https://github.com/escoffier-labs/solos-cookbook

MIT, Python stdlib only, no runtime dependencies. Early-stage; I fix reported issues fast.

## Reddit (r/LocalLLaMA, r/ClaudeAI, Codex communities)

**Title:** I packaged six months of hand-rolled agent-memory infrastructure into one CLI (local-only, no daemon, markdown memory)

**Body:**

If you run more than one coding agent you know the problem: each tool learns a little, and the learning is scattered and dies. I ran OpenClaw + Codex + Claude Code daily since January and hand-rolled the fixes: slim memory index, atomic memory cards, a handoff note format every tool writes, a 30-minute ingest cron, staleness scanning.

The two incidents that taught me the rules: an auto-promotion job bloated my always-loaded memory index to 41KB (12KB budget) and every session silently started with truncated memory; and 195 handoff notes sat unread for weeks because the ingester's repo allowlist was hardcoded and nothing warned. Review gates and loud coverage checks are not features, they are scar tissue.

Brigade is that setup as one `pipx install brigade-cli`. Agents (Codex, Claude Code, OpenCode, Cursor, Aider, Goose, Copilot CLI, and more) write handoff notes to local inboxes; Brigade lints them, scans them for secrets and prompt-injection signals, routes safe ones to durable memory via one canonical owner, and queues the rest for your review. Everything is plain markdown and JSON receipts in your repo. No daemon, no server, no telemetry, nothing leaves your machine.

There is also an adoption path for people who already have a homegrown setup: it inventories your crons, scripts, and inboxes read-only and produces a migration plan instead of stomping on what works. I used exactly that path to cut my own production workspace over this week.

Repo: https://github.com/escoffier-labs/brigade - MIT, stdlib-only Python. Early-stage, feedback and issues very welcome.

## Short version (social / Discord)

Six months of hand-rolled agent-memory infrastructure (OpenClaw + Codex + Claude Code), packaged as one CLI. Agents write handoff notes, Brigade lints and guards them, the good ones become durable markdown memory, everything else waits for review. Local-only, no daemon, receipts for everything. `pipx install brigade-cli` - https://brigade.tools

## Posting checklist

- [ ] Update version references if newer than 0.13.0
- [x] Confirm `pipx install brigade-cli` works from a clean machine (re-verified 2026-06-26: isolated install, quickstart + doctor `ready: yes`)
- [ ] Attach `docs/assets/quickstart.svg` (or a GIF render) as the lead media
- [ ] Run `brigade scrub --policy public-content` over this file before posting; run grill before HN/Lobsters
- [ ] Post HN morning US time midweek; Reddit separately, not the same day
- [ ] Watch the repo issues; the README promises fast responses
