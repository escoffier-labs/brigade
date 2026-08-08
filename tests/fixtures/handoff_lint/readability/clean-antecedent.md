# Memory Handoff

## Type
bugfix

## Title
Clean antecedent readability fixture

## Summary
Fixture handoff where bullets name subjects before any pronoun appears.

## Durable facts

- widget-parser.py drops the final retry because its loop bound is off by one.
- example-service returns ACME-123 when the guard rejects oversized payloads.

## Evidence

- files changed: `tests/fixtures/handoff_lint/readability/clean-antecedent.md`
- commands run: `brigade handoff lint tests/fixtures/handoff_lint/readability/clean-antecedent.md`

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

widget-parser.py keeps an explicit subject in every bullet so later pronouns stay clear.
