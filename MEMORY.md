# MEMORY.md - Master Index

## How Memory Works

- **This file:** Slim index. Loaded every session. Keep it under ~7KB so it stays in cache.
- **Knowledge cards:** `memory/cards/*.md`. Atomic durable facts, ~300-500 tokens each. Searched semantically by the configured memory store.
- **Daily logs:** `memory/YYYY-MM-DD.md`. Raw session notes.
- **One canonical owner:** **OpenClaw**.
- **Do not** dump everything here. Write knowledge cards instead.
- **Do not** auto-promote raw session fragments into this file. That bloats the index, blows through bootstrap-truncation, and turns the on-load cache cost into a monthly tax.

## Identity

Replace this line with one sentence: your name, your runtime, your model, your host. Example shape:

```text
<agent-name> <emoji> | <provider/model> | <host or platform>
Owner: <user name> (<user contact>)
```

## Quick Context

A handful of stable facts the agent should always have at hand. Replace these with the user's real anchors:

- **Role / day job:** <one line>
- **Active focus:** <one line - the thing they actually care about this month>
- **Writing rules:** <e.g. no em dashes, no AI-attribution trailers, citation standard>
- **Hard publish gate:** <e.g. content-guard scan required before any public push>

## Agent Architecture

Replace this with your actual agent roster. Example shape:

| Agent | Model | Role |
|-------|-------|------|
| `main` (you) | <provider/model> | Orchestration, planning, content, code |
| `coder` | <provider/model> | Bulk code work, structured output |
| `researcher` | <provider/model> | Deep research, long context |
| `escalation` | <provider/model> | Hard reasoning, polish, review |
| Embeddings | <provider/model> | Memory search, local |

## Session Workflow

1. Read this file (slim index).
2. Read `SOUL.md` + `USER.md`.
3. Search the memory store for task-relevant cards.
4. Skim today + yesterday `memory/YYYY-MM-DD.md`.
5. Start working.

## Daily Rhythm

| When | What | Card |
|------|------|------|
| Night (~21:00) | Pipeline standup | [pipeline-standups](memory/cards/pipeline-standups.md) |
| Night (~22:00) | Memory sweep / session review | [memory-scanner](memory/cards/memory-scanner.md) |
| Continuous | Handoff ingester | [handoff-flow](memory/cards/handoff-flow.md) |
| Morning (~08:00) | Morning report | [pipeline-standups](memory/cards/pipeline-standups.md) |

## Card Categories

Build out this table as you learn the shape of your durable knowledge. Starter shape:

| Category | Topics |
|----------|--------|
| foundation | memory architecture, handoff flow, content safety, memory scanner, chat-surface crawlers, pipeline standups |
| system | identity, memory-search system, sub-agent patterns, agent-wrapper patterns |
| user | personal context, communication style, preferences |
| infrastructure | hosts, ports, deploys, mounts, local services |
| models | subscriptions, assignment rules, benchmarks |
| workflow | pipeline rules, content strategy, publishing checklist |
| tools | local APIs, browser stacks, MCPs, skills |
| security | hardening, audits, runbooks |
| lessons | hard-won gotchas, corrections, prior-incident learnings |

Add categories as the workspace grows. One topic per card; one card per topic.

## Starter Cards

- [memory-architecture](memory/cards/memory-architecture.md) - how this workspace stores durable knowledge
- [handoff-flow](memory/cards/handoff-flow.md) - how Memory Handoffs flow into canonical memory
- [memory-scanner](memory/cards/memory-scanner.md) - session-review pass that promotes durable findings
- [pipeline-standups](memory/cards/pipeline-standups.md) - nightshift + morning cross-harness recaps
- [chat-surface-crawlers](memory/cards/chat-surface-crawlers.md) - discrawl-shaped local archives for Discord, Slack, WhatsApp, etc.
- [content-safety](memory/cards/content-safety.md) - publish gates and what they block

## Current Priorities

Replace this with short pointers to the sprint or working card. Example:

- See card `current-priorities` for the live sprint log.

## Maintenance

- Consolidate duplicate entries.
- Remove stale pointers after verifying the source is obsolete.
- Keep this file under ~200 lines so it stays in cache.
- If the file grows past the bootstrap budget, move detail into cards and link.
