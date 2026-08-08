# Memory Handoff

## Type
bugfix

## Title
Bare-pronoun readability fixture

## Summary
Fixture handoff for standalone-readability bare-pronoun detection.

## Durable facts

- It was fixed by tightening the retry guard in widget-parser.py.

## Evidence

- files changed: `tests/fixtures/handoff_lint/readability/bare-pronoun.md`
- commands run: `brigade handoff lint tests/fixtures/handoff_lint/readability/bare-pronoun.md`

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

Document the retry guard change with explicit module names.
