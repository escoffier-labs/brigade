---
topic: pipeline-standups
category: foundation
tags: [standups, recap, cron, cross-harness, daily-rhythm]
---

# Pipeline Standups (Night + Morning)

Two short cron-driven sessions that produce a cross-harness daily rhythm: a nightshift standup that summarizes the day before bed, and a morning report that frames the day ahead. They are not memory writes; they are operational summaries delivered to a chat channel so the user can review state at a glance.

## Why two

A single end-of-day or start-of-day digest works for a single-harness setup. With sessions running across multiple harnesses (Claude Code, Codex, OpenClaw, ACP threads), the user needs a recap that spans them all - and a morning brief that picks up where last night left off.

- **Night (typical: 21:00 local).** What got done today. What is uncommitted. What is queued.
- **Morning (typical: 08:00 local).** Dev servers up/down. Yesterday's highlights. Today's priorities. Any overnight alerts.

## Nightshift standup

Prompt shape:

```text
Nightshift standup. Read these files and produce a brief status report:

1. Read MEMORY.md (current state section)
2. Read the latest memory/YYYY-MM-DD.md daily log
3. Check git status across active repos under your work tree

Produce a concise report:
- What was done today
- Any uncommitted/unpushed work
- Active backlog items
- Suggested priorities for tonight

Keep it to 10-15 lines. No fluff.
```

Delivered to a single chat channel (the user's main work channel). Skip the headers, lead with bullets.

## Morning report

Prompt shape:

```text
Morning report. Read these and produce a brief daily briefing:

1. Read MEMORY.md (key sections)
2. Read the latest memory/YYYY-MM-DD.md
3. Check for any failed cron jobs or errors overnight
4. Probe configured dev servers / services for up/down state

Produce:
- Dev server status (X/Y online)
- Yesterday's highlights
- Today's priorities
- Any alerts

Keep it brief.
```

The dev-server probe is the differentiator: it surfaces silent process death between sessions without requiring the user to ask.

## Cadence and delivery

| Job | Typical schedule | Channel | Purpose |
|-----|------------------|---------|---------|
| `pipeline-standup` | `0 21 * * *` | main work channel | Nightshift recap |
| `pipeline-morning-report` | `0 8 * * *` | main work channel | Morning briefing |

Both run in **isolated sessions** so they do not pollute the main agent's running context. Wake mode for the standup is typically `next-heartbeat` (lets the report land when the user is next active); the morning report fires immediately so it is waiting at 08:00 sharp.

## Cross-harness scope

The standups read durable state (MEMORY.md, daily logs, git status) rather than session transcripts directly. This means they work even when sessions span Claude Code on a laptop, Codex in a tmux pane, and OpenClaw on the workspace host - whatever wrote durable findings into the canonical memory store gets included.

If a harness produced no durable writes during the day, it will not appear in the standup. That is the point. The standup reports on persisted state, not chatter.

## Relationship to memory scanner

Standups summarize what is already in memory. The [memory-scanner](memory-scanner.md) (typically scheduled later, e.g. 22:00) is what promotes durable findings *into* memory from the day's sessions.

Run order: standup -> memory scanner -> overnight ingester sweeps. Morning report next day reads what all three left behind.

## Tuning

- **If standups are noisy:** the daily log is being written too granularly. Move minor updates to cards or `.learnings/`.
- **If standups are empty:** session work is not landing in durable memory. Check the memory scanner's "Persisted" count over the last week.
- **If the dev-server probe times out:** widen the per-port timeout in the morning prompt, or drop ports that are intentionally on-demand.
