# Memory Handoff

## Type
workflow

## Title
Relative-date readability fixture

## Summary
Fixture handoff for standalone-readability relative-date detection.

## Durable facts

- The guard shipped last week before the example-service rollout finished.

## Evidence

- files changed: `tests/fixtures/handoff_lint/readability/relative-date.md`
- commands run: `brigade handoff lint tests/fixtures/handoff_lint/readability/relative-date.md`

## Recommended memory action
create-card

## Target card
example-widget.md

## Suggested card content
---
name: example-widget
description: Placeholder card for readability fixture validation.
tags: [fixture, readability]
---

### Widget parser guard

Replace relative timing with absolute dates in durable facts.
