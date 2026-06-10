# Memory Care

Brigade memory care is a local, read-only scanner for durable memory cards. It detects cards that need review, writes a refresh queue, and routes review tasks into the existing work inbox.

## Commands

```bash
brigade memory care init
brigade memory care scan
brigade memory care plan-fixes
brigade memory care backfill
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

## Safe Fix Planning

`brigade memory care backfill` handles the bulk version of the same repair for setups whose cards predate the freshness convention. It finds cards with frontmatter but no reviewed or freshness dates, derives `last_reviewed` from each card file's last git commit date (the last time anyone touched the fact; file mtime outside git, labeled as the lower-confidence source), and proposes `fresh_until` as the reviewed date plus the configured stale window. It is dry-run by default and prints the full plan; `--apply` writes the derived values into card frontmatter, never overwrites an existing value, and records a receipt under `.brigade/memory-care/backfills/`. Backfilled dates are honest rather than flattering: an old card lands as stale immediately, which is the point, since the scan then ranks the refresh queue by real age instead of reporting unknowable gaps.

`brigade memory care plan-fixes` reads the latest scan and builds a planning-only view for low-risk metadata repairs such as missing reviewed dates or missing freshness dates. The command never writes card files. Plan items include candidate metadata fields, source fingerprints, blockers, and the next review command. Reviewed-date plans are blocked until the operator checks current evidence. Freshness-date plans are blocked until the operator chooses an appropriate date or documents why the card should not expire.

Safe fix plans are copied into memory-care imports as metadata so the work inbox and daily brief can show that a local plan exists. The plan remains advisory. Follow-up work still goes through task promotion or Memory Handoff review.

## Refresh Queue

`brigade memory care scan` writes:

```text
memory/cards/decay/scan-latest.json
memory/cards/decay/refresh-queue.json
```

Queue entries include card identity, issue type, severity, priority, safe summary, evidence references, suggested refresh action, acceptance criteria, source item key, source fingerprint, and safe fix-plan metadata when available. `brigade memory care import-issues` imports those entries as source `memory-care` task imports with dedupe and dismissed-until-changed behavior.

## Boundary

Memory care never edits cards, runs a scheduler, mutates canonical memory, performs remote sync, or promotes imports automatically. Refreshes stay explicit through reviewed work tasks or the existing Memory Handoff flow.
