---
topic: memory-scanner
category: foundation
tags: [memory, sweep, session-review, promotion, daily-logs]
---

# Memory Scanner

The memory scanner is the upstream half of the handoff flow. It is a session-review pass that distills durable knowledge from recent activity (sessions across all your harnesses, daily logs, chat archives) and persists it into canonical memory through the handoff path.

```text
sessions               daily session logs       chat archives
(Claude Code,            (memory/                (discrawl,
 Codex, OpenClaw,         YYYY-MM-DD.md)          slackcrawl, ...)
 ACP threads)
        \                     |                    /
         \                    |                   /
          v                   v                  v
                       ┌────────────────┐
                       │ memory scanner │  cron (typical: nightly)
                       │ (session-review│
                       │  agent)        │
                       └────────┬───────┘
                                |
                                v
              .claude/memory-handoffs/*.md   OR   direct card writes
                                |                       (only when high-confidence)
                                v
                         brigade ingest
                                |
                                v
              memory/cards/, TOOLS.md, USER.md, rules/, .learnings/
```

## What it does

A real implementation (typical nightly cadence):

1. **List recent sessions.** Last 12-24 hours, across all harnesses connected to canonical memory.
2. **Skip noise.** Cron-spawned sessions, heartbeat/reminder-only sessions, empty subagent shells, pure delivery mirror / announce-only sessions.
3. **Prioritize real human-facing sessions first.** Discord, ClickClack, WhatsApp, Telegram, Slack, manual ACP threads. These are where decisions, corrections, and preferences actually land.
4. **Start with summaries.** Only fetch deeper history for sessions that clearly contain durable decisions, corrections, preferences, project changes, new tooling facts, or new published outputs.
5. **Cap deep review** to the top N most promising sessions (typical: 8) unless there is an obvious reason to exceed it.
6. **Avoid duplication.** Do not re-promote facts that already exist as cards or in daily-log entries.

## What gets persisted

- **Update existing cards** when facts changed.
- **Create new cards** for durable workflows, infra facts, project state, or repeatable lessons.
- **Append concise timestamped notes** to the relevant daily log if the info is recent and session-specific.
- **Update `MEMORY.md`** only if the index itself needs to change (new card category, major architecture shift).

## What does not get persisted

- Banter, casual replies, "yeah" / "ok" exchanges.
- Anything already covered by an existing card.
- Speculation, reflections, or unverified findings.
- Raw transcripts. Transcripts stay in their archive; the scanner produces summary writes, not copies.

## Output format

The scanner reports back on what it did:

```text
Memory Sweep - YYYY-MM-DD

Sessions: N listed | N reviewed | N with durable content

Persisted:
- card-or-file.md - updated with concise durable change.

Issues:
- Short operational failure or delivery problem that needs review.

Skipped:
- N cron/agent sessions - routine, heartbeat-only, no-reply, or operational noise after signal pass.
- N chat/manual sessions - delivery-only, output artifacts, or no durable preference/tooling/project change.

Net result: Captured durable context and surfaced issues that should not be treated as noise.
```

This gives you an audit trail and a heartbeat for the scanner itself. If "persisted" is empty for a week, either nothing durable happened or the scanner stopped firing.

The scanner should also write a local machine-readable summary at:

```text
.brigade/chat-memory-sweeps/latest.json
```

Use `.brigade/chat-memory-sweep.example.json` as the contract. `brigade work import chat-sweep` imports only the `issues` array into the local work inbox. Raw transcripts, private message bodies, and channel exports stay in crawler archives.

## Scheduling

Common cadence (matches reference cookbook): nightly at quiet hours, after [pipeline-standups](pipeline-standups.md) have run and the day's activity has settled.

Avoid promoting during active sessions because card writes invalidate prefix caches.

Spread recurring jobs around update windows. Do not anchor memory ingest, chat sweep, crawler repair, and updater jobs on the same minute or the same five-minute window. Prefer staggered minute offsets such as `:15` and `:45` for frequent ingest jobs when an updater runs near the top of the hour.

## Implementation surface

`brigade` ships the contract; it does not ship the scanner agent itself. Wire it as:

- a cron job that spawns an isolated agent session with a "review last 12h and persist durable facts" prompt
- the prompt should embed the skip-rules and cost controls above
- output goes either directly to `memory/cards/*.md` (if your harness can write there) or through `.claude/memory-handoffs/` for the conservative ingester to route
- issue summaries also go to `.brigade/chat-memory-sweeps/latest.json` so the daily work loop can review operational failures

## Relationship to Memory Care

The memory scanner captures new durable knowledge. The memory-care staleness loop reviews old cards for drift. Run both: scanner for new facts, staleness checker for old facts that may no longer be true. See [memory-care-staleness](memory-care-staleness.md).

## Anti-patterns

- **Auto-promoting raw session fragments into `MEMORY.md`.** The index loads on every session; appending fragments nightly bloats it past the bootstrap budget and turns the on-load cache cost into a monthly tax. Write cards instead.
- **Persisting reflections as facts.** The scanner reads sessions and produces summaries of decisions that happened, not generated commentary on what might have. Promoted findings must be evidence-backed.
- **Reviewing its own output.** The scanner must skip cron-spawned sessions, heartbeats, announce-only noise, and prior scanner runs. Otherwise it spirals.
- **Skipping the handoff gates when uncertain.** When confidence is below the auto-promote bar, route through `.claude/memory-handoffs/` and let the ingester apply the same conservative rules everything else gets.
