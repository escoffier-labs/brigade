---
name: card-refresh
description: Use when refreshing stale Brigade memory cards from the care refresh queue - category allowlist, grounded edits that cite on-disk sources of truth, archive-only retention, and reinforce-in-place instead of near-duplicate siblings (#724). Never delete, remove, or prune.
allowed-tools: Read, Grep, Bash(brigade memory care status:*), Bash(brigade memory care plan-fixes:*), Bash(brigade memory care closeout:*)
compatibility: Brigade-wired workspaces with memory care scan output; operator-scheduled headless harnesses.
---

# card-refresh

Safely refresh stale memory cards that memory care already queued. Brigade does not run the agent; this skill is the allowlist-bound recipe an operator invokes on their own harness and schedule.

## When this applies

- `brigade memory care scan` (or import-issues) has produced a refresh queue.
- An operator schedules a headless harness against this skill for safe auto-refresh.
- Card categories in scope are on the operator's category allowlist.

## Allowlist

Work only through this allowlist:

- Read the care refresh queue (`.brigade/memory-care/decay/refresh-queue.json` or the workspace's configured legacy path)
- `brigade memory care status`
- `brigade memory care plan-fixes` (planning only; writes no cards)
- `brigade memory care closeout` when finishing reviewed queue items
- Read/Grep of the card file, its cited evidence paths, and related on-disk sources of truth
- Edit only cards whose category is on the operator allowlist for this run

Bash is scoped to those `brigade memory care …` commands only. Do not touch cards outside the category allowlist. Stay inside the allowlist above; do not follow `..` or symlinks out of the card / care-queue roots.

## Grounded-edit rules

Refreshes must be grounded. Edit or refresh a card only when you can cite existing on-disk content that verifies the change (the card body, linked evidence files, handoffs, or other local sources of truth). Never date-bump alone. If you cannot verify a claim against on-disk material, skip the card and leave it queued for a human.

## Retention (never delete)

- Never delete memory cards.
- Never remove cards from the corpus with destructive ops.
- Never prune the refresh queue or card trees by hand.
- Archive only when an operator-approved archive path already exists; otherwise leave stale-but-unverified cards for human review and use `care closeout` for items you actually refreshed.

## Reinforce over duplicate (#724)

Ingest already reinforces exact fingerprint matches and proposes near-match / opposite-polarity review. When refreshing:

1. Search for near-duplicate cards covering the same fact.
2. Reinforce the strongest existing card (cite the on-disk sources) instead of splitting.
3. If duplicates are ambiguous or look like opposite-polarity contradictions, stop and mark needs-human rather than merging blindly.

## Process

1. Read the refresh queue and filter to the category allowlist.
2. For each candidate, open the card and its evidence paths on disk.
3. Apply a grounded edit only when on-disk sources confirm the update; cite those sources in the edit notes or handoff.
4. Close out completed queue items with care closeout; leave the rest.
5. Capture outcomes against `card-refresh` when verification is part of the run.

## Failure paths

- Missing refresh queue or memory-care config: stop. Report the missing path/config; do not invent a queue.
- Empty queue / no category-allowlisted candidates: exit cleanly with "nothing to refresh"; do not invent cards.
- Malformed queue entry or card (bad JSON, missing path, unreadable frontmatter): skip that entry, leave it for a human, continue with the rest. Never delete or rewrite malformed files just to clear the queue.

## Out of scope

- `session-sweep` (blocked on #466)
- Scheduler or model-dispatch integration
- Refreshing categories outside the allowlist
