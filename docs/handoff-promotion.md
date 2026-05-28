# Reviewed Handoff Promotion

`brigade work import promote-handoff` turns a reviewed durable scanner import into a local Memory Handoff draft. It is for non-task imports that should become durable knowledge after review, not for work execution.

## Commands

```bash
brigade work import plan-handoff <import-id>
brigade work import plan-handoff <import-id> --json
brigade work import promote-handoff <import-id>
brigade work import promote-handoff <import-id> --json
```

`plan-handoff` previews the handoff target, type, local inbox, provenance, and blockers. `promote-handoff` writes the draft, runs handoff lint, and only then marks the import promoted.

## Supported Imports

Handoff promotion is for durable non-task import kinds:

- `decision`
- `preference`
- `link`
- `command`
- `finding`
- `incident`

Task imports still use `brigade work import promote` and `brigade work import promote --run`.

## Target Documents

Brigade picks a no-card handoff target from kind and metadata:

- preferences go to `USER.md`
- commands and operational notes go to `TOOLS.md`
- incidents and failures go to `.learnings/ERRORS.md`
- feature requests go to `.learnings/FEATURE_REQUESTS.md`
- workflow rules go to `rules/scanner-imports.md`
- other durable lessons go to `.learnings/LEARNINGS.md`

Scanner metadata can set `handoff_target_document` to a valid document target when the default is too broad.

## Privacy Boundary

Handoff promotion writes only a local draft under the configured handoff inbox, usually `.codex/memory-handoffs/` or `.claude/memory-handoffs/`. It does not edit `MEMORY.md`, memory cards, or canonical memory, and it does not run the ingestor.

Raw private chat fields are rejected by default, including `raw_text`, `raw_messages`, `messages`, `message_text`, `quotes`, and `transcript`. Unsafe URLs, tokens, host-private paths, user ids, channel ids, hostnames, and secret-looking values are redacted before the draft is written.

Promoted imports preserve the local handoff path, target document, promotion timestamp, and source fingerprint for review.
