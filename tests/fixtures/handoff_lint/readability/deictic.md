# Memory Handoff

## Type
decision

## Title
Deictic readability fixture

## Summary
Fixture handoff for standalone-readability deictic detection.

## Durable facts

- The parser guard belongs in widget-parser.py because that file owns retry scheduling.

## Evidence

- files changed: `tests/fixtures/handoff_lint/readability/deictic.md`
- commands run: `brigade handoff lint tests/fixtures/handoff_lint/readability/deictic.md`

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

Name widget-parser.py explicitly when describing retry ownership.
