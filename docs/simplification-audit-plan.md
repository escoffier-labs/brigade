# Brigade Simplification Audit Plan

This is the next-phase scope for checking whether Brigade has overlapping tools, duplicated commands, or workflows that can be safely collapsed.

The first audit result is captured in [`docs/simplification-audit-report.md`](simplification-audit-report.md).

## Goal

Reduce command and implementation overlap without weakening Brigade's operator, memory, security, release, or harness contracts.

## Guardrails

- Run the audit before any automated code simplifier.
- Preserve public CLI behavior unless a deprecation path is documented.
- Keep security, handoff, release, and doctor evidence contracts stable.
- Prefer shared helpers for duplicated validation logic.
- Do not remove commands only because they are narrow; remove or merge them only when their user job is genuinely duplicated.

## Audit Pass

1. Build a command inventory from the parser and compare it to `docs/command-inventory.md`.
2. Group commands by user job: init, status, doctor, scan, review, report, import, closeout, archive, compare, schema, and repair.
3. Flag command pairs with overlapping inputs and outputs, especially across `daily`, `center`, `work`, `release`, `security`, `handoff`, and `operator`.
4. Search for duplicated constants, path maps, schema snippets, validation helpers, and output adapters.
5. Identify stale docs or commands whose behavior is now handled by a newer station.
6. Produce a reviewed candidate list with one of: keep, merge, extract helper, deprecate, or remove.

## First Targets

- `doctor` and `operator verify-harness` checks that validate the same harness contract.
- Security, release, work, and center health summaries that reformat the same evidence.
- Handoff inbox path maps and skip lists.
- Report, closeout, archive, and compare flows that share receipt structure.
- Docs command inventory drift.

## Verification

- Full test suite before and after each simplification slice.
- `brigade roadmap commands --check` if command docs are touched.
- `brigade security scan --policy strict --fail-on none --target .` after code movement.
- Manual diff review for public CLI output changes.
