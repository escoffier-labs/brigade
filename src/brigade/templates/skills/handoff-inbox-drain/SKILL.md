---
name: handoff-inbox-drain
description: Use when draining a Brigade handoff review inbox - triage drafts with allowlist-bound Brigade handoff commands, grounded edits that cite on-disk content, archive-only retention, and manual dedup until #724. Never delete, remove, or prune.
allowed-tools: Read, Grep, Bash(brigade handoff list:*), Bash(brigade handoff show:*), Bash(brigade handoff archive:*), Bash(brigade handoff closeout:*), Bash(brigade handoff lint:*), Bash(brigade handoff doctor:*)
compatibility: Brigade-wired workspaces with handoff inboxes; operator-scheduled headless harnesses.
---

# handoff-inbox-drain

Drain a handoff review inbox with judgment, without owning a scheduler. Brigade stays invoke-only; this skill is the allowlist-bound agent recipe for triage.

## When this applies

- A handoff draft inbox has pending or reviewed items to triage.
- An operator schedules a headless harness (`claude -p`, `codex exec`, or equivalent) against this skill.
- You are reinforcing existing memory, not inventing a parallel corpus.

## Allowlist

Work only through this allowlist of Brigade surfaces and local reads:

- `brigade handoff list`
- `brigade handoff show <id>`
- `brigade handoff archive <id>`
- `brigade handoff closeout` (when the workspace supports it)
- `brigade handoff lint` / `brigade handoff doctor` for health checks
- Read/Grep of on-disk handoff files, `memory/cards/`, and related indexes already present in the target

Bash is scoped to those `brigade handoff …` commands only. Do not expand into arbitrary filesystem writes, remote APIs, unscoped shell, or tools outside this allowlist. Stay inside the handoff inbox / `memory/cards/` roots; do not follow `..` or symlinks out of those trees.

## Grounded-edit rules

Every edit must be grounded. You must cite existing on-disk content (handoff body, card text, or index entry) before changing anything. Prefer reinforce-in-place: edit or refresh only what you can verify against a local source of truth. Date-bumps alone are not grounded edits.

## Retention (never delete)

- Never delete handoff drafts or memory cards.
- Never remove inbox items by destructive filesystem ops.
- Never prune archives, queues, or card trees.
- Archive only: uncertain or done items go through archive/closeout paths, or a needs-human file the operator already uses.

## Manual dedup (pending #724)

Until #724 lands fingerprint-based reinforcement, perform manual dedup before filing near-duplicates:

1. Search existing cards and recent handoffs for the same fact or decision.
2. If a close match exists, reinforce that card (cite the on-disk source) instead of creating a sibling.
3. If unsure, leave the draft for human review rather than splitting the corpus.

## Process

1. List pending drafts with the allowlist commands.
2. For each item, read the on-disk handoff and related cards.
3. Triage: archive noise, closeout completed review, reinforce an existing card when the fact matches, or park uncertain items for a human.
4. Capture outcomes against `handoff-inbox-drain` when verification is part of the run.
5. Stop when the inbox is drained or only needs-human items remain.

## Failure paths

- Missing inbox or handoff source config: stop. Report the missing path/config; do not invent an inbox or create one.
- Empty inbox / no pending drafts: exit cleanly with "nothing to drain"; do not invent work.
- Malformed handoff entry (unreadable frontmatter, missing id, broken body): skip that item, leave it for a human, continue with the rest. Never rewrite a malformed file just to make the loop continue.

## Out of scope

- `session-sweep` (blocked on #466)
- Scheduler or model-dispatch integration
- Automatic card creation without an on-disk citation
