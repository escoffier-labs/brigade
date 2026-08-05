# Memory Handoff

## Type
decision

## Title
Compliant readability fixture

## Summary
Fixture handoff with explicit subjects and absolute dates only.

## Durable facts

- widget-parser.py gained a retry guard on 2026-08-05 to cap example-service payloads.
- example-service emits ACME-123 when widget-parser.py rejects oversized input.

## Evidence

- files changed: `tests/fixtures/handoff_lint/readability/compliant.md`
- commands run: `brigade handoff lint tests/fixtures/handoff_lint/readability/compliant.md`

## Recommended memory action
create-card

## Target card
example-widget.md

## Suggested card content
---
name: example-widget
description: Placeholder card documenting the 2026-08-05 guard rollout.
tags: [fixture, readability]
---

### Widget parser guard

On 2026-08-05, widget-parser.py started rejecting oversized example-service payloads before retrying.
