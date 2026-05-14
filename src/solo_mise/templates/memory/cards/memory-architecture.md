---
topic: memory-architecture
category: foundation
tags: [memory, bootstrap, handoff, canonical-owner]
---

# Memory Architecture

This workspace uses a single canonical memory owner. Side harnesses may keep local session context, but durable knowledge flows back through Memory Handoffs and is routed into the canonical store.

## Layout

```text
./
  AGENTS.md            # operating rules + memory contract
  MEMORY.md            # slim index pointing to cards
  TOOLS.md             # operational runbook (appendable target)
  USER.md              # stable user preferences (appendable target)
  IDENTITY.md
  SOUL.md
  HEARTBEAT.md
  SAFETY_RULES.md
  INSTALL_FOR_AGENTS.md
  memory/
    cards/             # durable knowledge cards (auto-promotion target)
      decay/           # optional staleness scan output + refresh queue
    handoff-inbox/     # ambiguous handoffs land here for review
  rules/               # workflow rules (appendable target)
  .learnings/          # concrete failures + lessons (appendable target)
  .claude/
    memory-handoffs/
      TEMPLATE.md
      processed/       # archive of ingested handoffs
```

## Why one owner

Two canonical memory systems is one too many. Either both have to be reconciled on every read, or one drifts silently and contradicts the other. Pick one owner. Everything else writes to it through handoffs.

## What goes where

| Kind of knowledge | Target |
|-------------------|--------|
| Architecture decision, durable concept, recurring pattern | `memory/cards/*.md` (frontmatter required) |
| Command, port, endpoint, script, runbook | `TOOLS.md` |
| Stable user preference | `USER.md` |
| Workflow rule, recurring correction | `rules/<name>.md` |
| Concrete failure | `.learnings/ERRORS.md` |
| Lesson or workaround | `.learnings/LEARNINGS.md` |
| Missing capability or enhancement request | `.learnings/FEATURE_REQUESTS.md` |

## Maintenance

- Keep `MEMORY.md` under ~200 lines so it stays in cache.
- Remove stale entries after verifying the source is obsolete.
- Consolidate duplicates. One topic, one card.
