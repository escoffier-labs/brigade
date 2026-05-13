---
topic: handoff-flow
category: foundation
tags: [memory, handoff, ingester, claude-code, codex]
---

# Memory Handoff Flow

Claude Code, Codex, and other side harnesses write Memory Handoffs to `.claude/memory-handoffs/`. A conservative ingester parses them and routes durable knowledge into canonical memory.

## End-to-end

1. Side harness finishes a substantial task.
2. Closeout rule fires: "did this session produce durable knowledge?"
3. If yes, the harness writes `.claude/memory-handoffs/<YYYY-MM-DD-HHMM>-<slug>.md` using `TEMPLATE.md`.
4. The ingester (run by the memory owner) parses each handoff.
5. Handoffs route to: a memory card, an appendable document, or the review inbox.
6. Processed handoffs move to `.claude/memory-handoffs/processed/`.

## Auto-promotion rules

Only three handoff shapes can silently mutate canonical memory. Everything else lands in `memory/handoff-inbox/` for manual review.

**Card auto-promotion:**
- `Recommended memory action` is `create-card` or `update-card`.
- `Target card` matches `^[A-Za-z0-9._-]+\.md$` (no path traversal).
- `Suggested card content` starts with YAML frontmatter.

**Document routing:**
- `Recommended memory action` is `no-card`.
- `Target document` is one of: `TOOLS.md`, `USER.md`, `rules/*.md`, `.learnings/*.md`.
- `Suggested document content` has no `##` headings (would parse as new sections).

## Closeout instruction

The harness must be told to write handoffs without prompting. Put this in the harness's instruction file (e.g. `~/.claude/CLAUDE.md` or equivalent):

```text
At the end of any substantial task, check whether the session produced durable
knowledge. If yes, create a Memory Handoff in `.claude/memory-handoffs/`
using the standard format. Do this without waiting to be reminded.
```

## Verification

```bash
# Handoffs being produced
find . -path "*/.claude/memory-handoffs/*.md" -not -path "*/processed/*" -mtime -7

# Ingest run
solo-mise ingest --target . --dry-run

# Cards landed via promotion
find memory/cards -mtime -7 -name "*.md"

# Review inbox depth
ls memory/handoff-inbox/ 2>/dev/null | wc -l
```

## Gotchas

- `##` inside `Suggested document content` parses as a new handoff section. Use `###` or deeper.
- Auto-promotion writes to the filesystem immediately. Ingest during quiet hours if you care about cache continuity.
- The ingester is intentionally conservative. If your inbox grows, refine your handoff quality; do not loosen the rules.
