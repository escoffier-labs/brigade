# Grok Bot approved feed

## Status

Approved by the operator on 2026-08-25 as part of the first-day Grok Bot
reconciliation.

## Problem

The native Grok Bot adapter safely exposes a bounded queue, but it only
consumes jobs. Hourly Repository Scout and Implementation Worker routines ran
without producing work because no approved production envelope entered the
queue after commissioning.

## Decision

Add a local-only `brigade run cloud grokbot feed` command that reads an
explicit private manifest and enqueues a bounded number of approved envelopes.
It never invents tasks, reads chat, selects GitHub issues, or changes approval
state.

The manifest contains:

- schema `brigade.grokbot.feed.v1`
- `approved: true`
- a non-empty operator label
- an array of entries, each with an `idempotency_key` and the existing Grok Bot
  job `spec`

The command defaults to validation-only. `--apply` is required to enqueue.
`--limit` bounds newly created jobs from 1 through 10 and defaults to 1.
Existing idempotency records do not consume the limit. The feed stops on the
first invalid entry and performs no enqueue when validation fails.

## Boundaries

- Queue authority stays local. No feed tool is added to any MCP inventory.
- Existing job-envelope validation remains authoritative.
- The feed file must be a regular file owned by the current user and not group-
  or world-writable.
- Output includes only safe job projections and counts. It never prints
  instructions, verification commands, ownership paths, or credentials.
- The command does not schedule itself. systemd, cron, OpenClaw, or another
  operator may run an approved manifest later.
- Repeated runs are safe through the existing idempotency store.

## Failure behavior

- Malformed manifests, unsafe file permissions, duplicate feed keys, invalid
  limits, and invalid job specs fail before queue mutation.
- In validation mode, the command reports how many entries are valid and how
  many are already known.
- During apply, an unexpected queue error stops the feed and reports only the
  entry index and a generic error.

## File map

- Create `src/brigade/grokbot_feed.py` for feed schema, validation, and apply
  logic.
- Modify `src/brigade/cli/run_cloud.py` to add the `feed` parser and dispatch.
- Create `tests/test_grokbot_feed.py` for manifest, permission, validation,
  idempotency, limit, redaction, and CLI contracts.
- Modify `docs/grokbot-mcp.md` for the operator workflow and scheduling
  boundary.

## Implementation sequence

1. Write failing preflight tests for a valid manifest, wrong schema, missing
   approval, duplicate keys, unsafe permissions, invalid limits, and an invalid
   later job. Each error case must leave the queue untouched.
2. Implement `FeedError`, manifest loading, permission checks, and full
   preflight validation by reusing the existing job-envelope validator without
   writing queue state.
3. Write failing apply tests for bounded creation, idempotent continuation, and
   redacted results.
4. Implement apply by completing preflight first and then calling the existing
   queue enqueue operation in manifest order until the new-job limit is met.
5. Write CLI tests, expose
   `feed --manifest PATH --limit N [--apply] [--json]`, and document the
   one-shot workflow plus external scheduling boundary.

## Acceptance

1. A valid manifest can be checked without writing queue state.
2. `--apply --limit 1` creates only the first unknown job.
3. A repeated run skips the known entry and creates the next unknown job.
4. Invalid later entries prevent all writes.
5. CLI and JSON output remain free of private task context.
6. Existing Grok Bot queue, MCP, and operations tests stay green.

## Verification

Run these checks through the Brigade work loop:

```bash
pytest -q tests/test_grokbot_feed.py tests/test_grokbot_jobs.py tests/test_grokbot_mcp.py tests/test_grokbot_ops.py
ruff check src/brigade/grokbot_feed.py src/brigade/cli/run_cloud.py tests/test_grokbot_feed.py
git diff --check
```
