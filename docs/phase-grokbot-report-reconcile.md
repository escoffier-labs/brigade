# Grok Bot report reconciliation for the canonical owner

## Status

Implemented as a local CLI slice. Preview is default. Apply writes Memory
Handoff drafts for canonical-owner review and never edits canonical memory.

## Problem

Repository Scout completions can store a private report snapshot, but those
bytes stay under queue authority. There is no bounded, idempotent path that
turns a verified snapshot into a reviewable Memory Handoff without printing
report text or auto-editing canonical memory.

## Decision

Add `brigade run cloud grokbot reconcile-reports --target QUEUE --owner OWNER`.
Preview counts verified completed scout report jobs and writes nothing.
`--apply` creates at most `--limit` (1-50, default 1) deterministic drafts in
the owner workspace. The default inbox is the canonical owner's direct review inbox,
`memory/handoff-inbox`; it does not depend on handoff-ingest promotion flags.

Each draft:

- identifies `job_id`, repository, task hash, report SHA-256, and completion time
- marks the report as untrusted Grok Bot automation output
- quotes every report line so `##` headings cannot split handoff sections
- lands directly in the canonical owner's review inbox instead of auto-editing memory

Private idempotency markers live at
`.brigade/cloud/grokbot/reconcile/<job-id>.json`. They store the digest and
job handle, never report text. Repeated apply does not duplicate. Concurrent
applies serialize on the existing queue lock. Corrupt snapshots and conflicting
markers fail closed. Legacy jobs with no stored snapshot are counted as
`unavailable` and left unmarked so later retrievable reports still reconcile.

## Boundaries

- No Fleet Hub, receipt, or job-JSON copy of report text.
- No new daemon, dependency, or MCP tool.
- Queue authority stays on `--target`. Drafts land in `--owner`.
- Existing `grokbot_jobs.read_report` remains the snapshot verifier.
- Existing handoff rendering and linting remain the draft writer.
- Custom inboxes stay owner-relative and cannot escape through traversal or symlinks.
- Apply requires POSIX descriptor primitives and fails closed on Windows; preview remains read-only there.

## File map

- Create `src/brigade/grokbot_reconcile.py` for preview, apply, quoting, and markers.
- Modify `src/brigade/cli/run_cloud.py` for the public command.
- Create `tests/test_grokbot_reconcile.py` for preview, apply, idempotency,
  fail-closed, redaction, and CLI contracts.
- Modify `docs/grokbot-mcp.md` and `docs/technical-guide.md`.
