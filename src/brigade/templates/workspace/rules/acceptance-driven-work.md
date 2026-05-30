# Acceptance Driven Work

Use this repo-shareable rule to keep local work reviewable across manual tasks, issue-backed tasks, and scanner-promoted tasks.

## Rule

Every work item should have acceptance criteria that answer these questions:

1. What observable behavior, artifact, or evidence should exist when the item is done?
2. Which focused test, command, or review step proves the item?
3. Which documentation, changelog, roadmap, release, or handoff evidence must be updated?
4. Which privacy or safety boundary must be preserved?

## Criteria Shape

Prefer short checklist items that can be verified locally:

- A focused regression test covers the changed behavior.
- The relevant CLI command returns stable text and JSON output.
- The local receipt links to verification, review, or closeout evidence.
- Public docs avoid private names, raw logs, secrets, and private infrastructure values.

## Boundaries

- Keep acceptance criteria local and explicit.
- Do not require automatic promotion, dismissal, publishing, memory mutation, or remote mutation.
- Do not embed personal workflow preferences in this file.
