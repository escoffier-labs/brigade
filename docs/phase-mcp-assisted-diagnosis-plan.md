# MCP assisted diagnosis Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for bounded implementation and review.

**Goal:** Give operators selected-project MCP drift diagnosis and explicit finding/report preparation.
**Architecture:** Factor existing comparison behind bounded in-memory inputs, then compose redacted diagnosis and existing report/import contracts.
**Tech Stack:** Python standard library and existing Brigade helpers. Zero new dependencies.
**Spec:** `docs/phase-mcp-assisted-diagnosis.md`.

## Global constraints

- Selected project adapter only. Three fixed files, 64 KiB each, 100 servers maximum.
- Default preview has no writes. Export/report preparation requires explicit flags.
- No live MCP/T3/model operations, automatic repair, import promotion or public posting.
- Preserve unrelated work and all existing assertion/proof/consent requirements.

## Review focus

- Ancestor links and path swaps must not redirect reads outside the project.
- Adapter parsers cannot turn malformed native config into an apparent missing server.
- Comparison must not fall back to ambient ownership or user-scope files.
- Config values, including exception text and auth keys, cannot cross output boundaries.
- A matching config cannot claim actual runtime availability or successful verification.

## Task 1: Complete diagnosis command

**Worker ownership:** `src/brigade/bug_diagnose.py`, `src/brigade/cli/bug.py`,
`src/brigade/mcp_cmd.py`, and `tests/test_bug_diagnose.py`.
**Parent ownership:** these two documents, changelog, handoff and the narrow
`src/brigade/cli/__init__.py` integration exemption for diagnosis. The generic
wrapper records failures and invokes update notifications after dispatch, so
diagnosis must bypass those hooks on success and safe refusal.
**Independent QA ownership:** `tests/test_bug_diagnose_security.py`.
**Report integration:** `src/brigade/bug_cmd.py` exposes a private preparation
helper over an explicit report projection. Diagnosis must use that helper
without invoking the ordinary collector.
**Interfaces:** existing `cli.main`, per-harness planner, CanonicalServer adapter
parsing/projection, Redactor and bug report v1. The worker chooses the smallest
private comparison-helper signature and records it. No new public Python API.

- [ ] Read the actual spec and source. Verify the three-file read footprint and
  absence of environment resolution. Review `/tmp/mcp-diagnosis-impact.json` and
  query exact new/changed paths before edits. Missing new-file coverage is explicit.
- [ ] Add focused tests for missing/current/conflicted/stale/excluded cases and
  selected-harness behavior. Show a meaningful failing behavior before implementation.
- [ ] Factor the existing planner's read boundary without duplicating its algorithm.
  Preserve existing callers' behavior. Add the bounded reader and allowlisted result.
- [ ] Register `bug diagnose` with the exact CLI options in the spec and compose
  safe preview, exclusive new-file JSONL export, and existing local bug preparation.
- [ ] Add adversarial tests for malformed/oversized/linked/escaping files, hostile
  values in keys and data, another harness never being read, no network/subprocess
  server launch, zero default writes and untrusted finding identity/verification.
- [ ] Exercise export through existing validate/preview/ingest on temp repos and
  report preparation through mocked transports and isolated global report readers.
- [ ] Run focused verification through Brigade for the new module plus affected
  MCP and bug-report selectors. Capture `brigade-work`, report observed results
  and failures verbatim. Never weaken existing tests or proof gates.
- [ ] Return diff, private helper interface, exact impact set, receipts and limits.
  Do not commit/push/open PR. No subagents or changes outside owned files.

## Task 2: Owner completion

- [ ] Review behavior and safety boundary, obtain independent code/security review.
- [ ] Update changelog and public command documentation with observed behavior.
- [ ] Run the full local gate once using the checkout-local environment and short
  interpreter alias if required by host crontab limits. Do not retry status 75.
- [ ] Run writing/content checks, commit/push, open and link the in-house PR.
- [ ] Verify current-head CI/reviews/conversations, own fixes and protected merge.
- [ ] Record a durable handoff and leave live/runtime/other owner work held.

The approved user workflow supplies design alignment and the existing native T3
worker exception supplies execution transport. Routine command and private helper
choices above are owner decisions. No further routine approval is required.
