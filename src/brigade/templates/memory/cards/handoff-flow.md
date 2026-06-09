---
topic: handoff-flow
category: foundation
tags: [memory, handoff, ingester, claude-code, codex]
---

# Memory Handoff Flow

Side harnesses write Memory Handoffs to their own writer inbox ({{handoff_inboxes}}). A conservative ingester parses them and routes durable knowledge into canonical memory.

## End-to-end

1. Side harness finishes a substantial task.
2. Closeout rule fires: "did this session produce durable knowledge?"
3. If yes, the harness writes `{{handoff_inbox}}<YYYY-MM-DD-HHMM>-<slug>.md` using `TEMPLATE.md`.
4. The ingester (run by the memory owner) parses each handoff.
5. Handoffs route to: a memory card, an appendable document, or the review inbox.
6. Processed handoffs move to the inbox's `processed/` folder.

## Multiple Workspaces

If you administer more than one agent setup, keep this flow hub-and-spoke. Secondary workspaces write local handoffs, then the canonical owner pulls them into staging directories and runs the same ingester. This lets agents on separate machines or repos inform each other about what changed without creating competing memory stores.

See [multi-workspace-handoff-admin](multi-workspace-handoff-admin.md) for the full pattern.

## Auto-promotion rules

Only three handoff shapes can silently mutate canonical memory. Everything else lands in `memory/handoff-inbox/` for manual review.

**Card auto-promotion:**
- `Recommended memory action` is `create-card` or `update-card`.
- `Target card` matches `^[A-Za-z0-9._-]+\.md$` (no path traversal).
- `Suggested card content` starts with YAML frontmatter.
- `Target document` and `Suggested document content` sections are omitted entirely.

**Document routing:**
- `Recommended memory action` is `no-card`.
- `Target document` is one of: `TOOLS.md`, `USER.md`, `rules/*.md`, `.learnings/*.md`.
- `Suggested document content` has no `##` headings (would parse as new sections).
- `Target card` and `Suggested card content` sections are omitted entirely.

## Closeout instruction

The harness must be told to write handoffs without prompting. Put this in the harness's instruction file (e.g. `~/.claude/CLAUDE.md` or equivalent):

```text
At the end of any substantial task, check whether the session produced durable
knowledge. If yes, create a Memory Handoff in `{{handoff_inbox}}`
using the standard format. Do this without waiting to be reminded.
```

## Verification

```bash
# Handoffs being produced
find . -path "*/memory-handoffs/*.md" -not -path "*/processed/*" -mtime -7

# Ingest run
brigade ingest --target . --dry-run

# Pre-ingest lint
brigade handoff lint --target .

# Cards landed via promotion
find memory/cards -mtime -7 -name "*.md"

# Review inbox depth
ls memory/handoff-inbox/ 2>/dev/null | wc -l
```

## Gotchas

- `##` inside `Suggested document content` parses as a new handoff section. Use `###` or deeper.
- Mixed card and document sections can trigger route skips. Run `brigade handoff lint` and delete the unused branch before ingest.
- Auto-promotion writes to the filesystem immediately. Ingest during quiet hours if you care about cache continuity.
- The ingester is intentionally conservative. If your inbox grows, refine your handoff quality; do not loosen the rules.
