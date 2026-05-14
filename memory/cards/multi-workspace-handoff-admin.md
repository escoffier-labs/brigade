---
topic: multi-workspace-handoff-admin
category: workflow
tags: [memory, handoff, multi-workspace, admin]
---

# Multi-Workspace Handoff Admin

When one person administers multiple agent homes, only one workspace should own canonical durable memory. Other workspaces can run local sessions, keep local context, and write repo-local notes, but durable facts should flow back to the owner through Memory Handoffs.

## Shape

```text
managed workspace A      managed workspace B      repo-local sessions
  .claude/                 .claude/                 .claude/
    memory-handoffs/         memory-handoffs/         memory-handoffs/
          \                         |                         /
           \                        |                        /
            v                       v                       v
          staging inboxes on the canonical memory owner
                           |
                           v
                    solo-mise ingest
                           |
                           v
              memory/cards/, TOOLS.md, USER.md, rules/, .learnings/
```

## Why

Multiple active setups drift unless the agents can tell each other what happened. A secondary workspace should not silently become a second source of truth. Its job is to emit handoffs that say what changed, what evidence supports it, and what the canonical owner should remember.

## Pull Pattern

Run a small trusted sync from the canonical memory owner:

```bash
rsync -a --remove-source-files \
  --include='*.md' --exclude='processed/***' --exclude='*' \
  <remote>:<workspace>/.claude/memory-handoffs/ \
  <canonical-workspace>/pipeline/incoming-handoffs/<remote-label>/
```

Then include each staging directory in the ingest loop. Keep remote labels generic, such as `laptop`, `homelab`, `client-a`, or `research-vm`.

## Admin Rules

- Pull into staging first, then ingest. Do not ingest directly over SSH.
- Remove remote source files only after successful transfer to the canonical host.
- Exclude `processed/` so archives do not churn forever.
- Preserve the original handoff text for review. The canonical owner should see exactly what the remote harness wrote.
- Use per-source labels so failures can be traced to the workspace that produced them.
- Surface non-empty pulls to the agents that need to know. A handoff is both memory input and a coordination signal.

## Verification

```bash
find pipeline/incoming-handoffs -name '*.md' -not -path '*/processed/*'
solo-mise ingest --target . --dry-run
ls memory/handoff-inbox/ 2>/dev/null | wc -l
```

If no remote handoffs arrive for a week while remote work is happening, the remote workspace probably lacks the closeout instruction, the sync is broken, or the files are landing in the wrong repo.
