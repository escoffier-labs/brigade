# Memory Handoff

## Type
decision

## Title
Grok Bot Repository Scout work uses an explicit issue-label gate

## Summary
Brigade now has a bounded selector for converting explicitly approved GitHub issues into Grok Bot Repository Scout jobs. The selector is intentionally separate from Fleet Hub active-work reporting, which another workstream owns, and it spends Grok Bot quota only on read-only reports.

## Durable facts
- `brigade run cloud grokbot scout-feed` previews one issue selected from an owner-only private policy; `--apply` is required to enqueue.
- The operator gate is a configurable GitHub label. GitHub returns issue numbers only, and title, body, comments, policy paths, and verification commands do not enter command output.
- Each invocation creates at most one job. Active Scout work blocks selection, and `daily_limit` counts failed and expired attempts on the current UTC date.
- The stable idempotency key is a fixed 64-character SHA-256 digest of the repository and canonical positive issue-number bytes.
- Apply holds the Grok Bot queue lock across active-work, UTC daily-cap, idempotency, and job-creation checks.
- Scout envelopes use a fixed read-only instruction template and a report artifact. They cannot edit issues, repositories, pull requests, or remote settings.
- Brigade cannot read Grok Bot's remaining weekly quota percentage. The daily cap is an operator policy, not live quota telemetry.
- Fleet Hub active-work integration must remain a separate producer behind the `grokbot_jobs` queue contract to avoid competing ownership.

## Evidence
- files changed: `src/brigade/grokbot_scout_feed.py`, `src/brigade/cli/run_cloud.py`, `tests/test_grokbot_scout_feed.py`, `docs/grokbot-mcp.md`
- focused receipt: `20260826-190311-work-verify-2ca8a8`
- pre-review full repository receipt: `20260826-190339-work-verify-3f47cf`

## Recommended memory action
create-card

## Target card
grokbot-approved-issue-selector.md

## Suggested card content
---
title: Grok Bot approved issue selector
category: architecture
updated: 2026-08-26
---

Use Brigade's `scout-feed` command to turn explicitly labeled GitHub issues into bounded, read-only Grok Bot Repository Scout jobs. Keep the private policy owner-only, require `--apply`, cap daily attempts, and keep Fleet Hub active-work publishing in its separate workstream.
