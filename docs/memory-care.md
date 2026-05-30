# Memory Care

Brigade memory care is a local, read-only scanner for durable memory cards. It detects cards that need review, writes a refresh queue, and routes review tasks into the existing work inbox.

## Commands

```bash
brigade memory care init
brigade memory care scan
brigade memory care status
brigade memory care doctor
brigade memory care import-issues
```

`init` writes `.brigade/memory-care.toml`. The config is host-local and should stay gitignored.

## Config

The local config supports:

- `card_roots`: directories containing memory card Markdown files.
- `index_paths`: memory indexes such as `MEMORY.md`.
- `stale_after_days`: review age threshold.
- `expiry_warning_days`: days before `fresh_until` or `expires_at` counts as expired.
- `minimum_confidence`: `unknown`, `low`, `medium`, or `high`.
- `require_evidence`: whether cards without evidence metadata are flagged.
- `include_paths` and `exclude_paths`: relative path prefixes.
- `output_path`: where `scan-latest.json` and `refresh-queue.json` are written.
- `enabled_checks`: enabled issue types.
- `max_card_bytes`: oversized-card threshold.

## Issue Types

Memory care can emit:

- `stale`
- `expired`
- `undersourced`
- `contradictory`
- `missing-index-link`
- `orphaned-card`
- `oversized-card`
- `missing-frontmatter`
- `missing-reviewed`
- `missing-freshness`

Contradiction detection is deliberately conservative. Brigade only flags explicit duplicate card identities or metadata hints, not LLM-inferred factual conflicts.

`status` and JSON output summarize freshness metadata coverage: reviewed dates present, missing, and stale; freshness dates present, missing, and expired; confidence distribution; and evidence metadata present or missing. These checks only explain review needs. Brigade does not edit memory cards automatically.

## Refresh Queue

`brigade memory care scan` writes:

```text
memory/cards/decay/scan-latest.json
memory/cards/decay/refresh-queue.json
```

Queue entries include card identity, issue type, severity, priority, safe summary, evidence references, suggested refresh action, acceptance criteria, source item key, and source fingerprint. `brigade memory care import-issues` imports those entries as source `memory-care` task imports with dedupe and dismissed-until-changed behavior.

## Boundary

Memory care never edits cards, runs a scheduler, mutates canonical memory, performs remote sync, or promotes imports automatically. Refreshes stay explicit through reviewed work tasks or the existing Memory Handoff flow.
