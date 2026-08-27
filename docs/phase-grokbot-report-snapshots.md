# Grok Bot report snapshots and stable HTTP replay

## Status

Approved by the operator on 2026-08-26 after the first approved-issue selector
canary returned only a report path and declared digest.

## Problem

Repository Scout completion records contain a report path and SHA-256 supplied
by Grok Bot, but report bytes remain on Grok Bot's cloud computer. Brigade can
reconcile terminal state but cannot inspect the report or recompute its digest.

The HTTP admission gate also consumes the request body before invoking the MCP
SDK. Its replay channel returns a synthetic disconnect immediately after the
buffered request. The SDK's disconnect watcher can observe that disconnect
before it finishes the JSON response, producing `ASGI callable returned without
completing response` even when the tool mutation succeeds.

## Decision

Keep the existing completion tool and add an optional `report_text` argument.
For Repository Scout report completions, Brigade accepts up to 12,000 UTF-8
bytes, verifies the declared SHA-256 against those bytes, and stores a private
snapshot under queue authority before committing the terminal job record.
Existing callers that submit metadata only remain valid, so deployment can be
rolled out before every Grok Bot routine is updated.

Add operator-only `grokbot_queue_report`. It accepts only a job id and returns
the stored text, byte count, and recomputed SHA-256 for a completed report job.
It does not expose job instructions, verification commands, ownership paths,
leases, tokens, or reports from other artifact kinds. Missing legacy snapshots,
digest mismatches, oversized files, symlinks, and malformed UTF-8 return the
same public validation error as other rejected tool inputs.

The request gate will replay buffered request messages first, then delegate
subsequent receives to the original ASGI receive callable. It will no longer
invent a disconnect. Authentication, host checks, origin checks, and raw
tool-schema checks stay unchanged. The overall request ceiling increases from
16,384 to 80,000 bytes so a 12,000-byte UTF-8 report still fits when a JSON
serializer escapes C0 control characters as six-byte `\u00XX` sequences.

## Storage boundary

- Create a private `artifacts` directory beside `jobs` and `idempotency` with
  mode 0700.
- Store one canonical snapshot named `<job-id>.md` with mode 0600.
- Write the snapshot atomically while holding the existing queue lock.
- Write snapshot bytes before the completed job record. A crash can leave an
  invisible orphan, but cannot produce a completed record that points to a
  missing snapshot.
- Read through a no-follow descriptor on POSIX (`O_NOFOLLOW` relative to the
  artifacts directory fd). On Windows, refuse a symlink or non-regular path
  with an `lstat` pre-check before opening. Enforce the 12,000-byte ceiling,
  decode strict UTF-8, recompute SHA-256, and compare it with the completed
  result artifact.
- Do not add report content to job JSON, status projections, tracker rows,
  Fleet Hub events, receipts, or logs.

## Compatibility

- Existing `grokbot_queue_complete` calls without `report_text` keep their
  current behavior.
- `report_text` is accepted only when the job role and expected artifact kind
  are `repository-scout` and `report`.
- Draft PR and branch completions reject `report_text`.
- Operator inventory gains one read-only tool. Worker inventories keep their
  current names and gain only the optional completion argument.
- The request ceiling is 80,000 bytes. The smaller report ceiling leaves room
  for the JSON-RPC envelope, artifact metadata, and escaped C0 controls.

## Alternatives considered

### Separate upload and completion tools

A dedicated upload tool would keep completion arguments smaller. It introduces
an orphan lifecycle and lets uploads exist before job completion. Inline capture
keeps one lease-checked terminal action.

### Store only the cloud path and digest

This is the current behavior. It cannot support inspection, independent digest
verification, memory ingestion, or evidence export after the cloud session
ends.

### Post every report to GitHub

GitHub comments are easy to retrieve, but they turn a local queue result into an
external mutation and can expose content before operator review. GitHub posting
can be a later approved action, not the transport for queue artifacts.

## File map

- Modify `src/brigade/grokbot_jobs.py` for private snapshot storage, bounded
  completion capture, and verified reads.
- Modify `src/brigade/grokbot_mcp.py` for the optional completion argument, the
  operator-only report tool, and ASGI receive delegation.
- Modify `tests/test_grokbot_jobs.py` for storage, digest, size, role, symlink,
  corruption, and legacy compatibility cases.
- Modify `tests/test_grokbot_mcp.py` for inventories, typed tool schemas,
  bounded report retrieval, and disconnect replay behavior.
- Modify `docs/grokbot-mcp.md` for the report snapshot contract and rollout
  instructions.

## Acceptance

1. A Repository Scout can complete with `report_text` and matching metadata.
2. The queue stores a private canonical snapshot and operator retrieval returns
   its text, byte count, and independently recomputed digest.
3. Digest mismatch, oversize content, wrong role, wrong artifact kind, unsafe
   storage, invalid UTF-8, and missing legacy snapshots fail without exposing
   private content.
4. Existing metadata-only completion remains valid.
5. Status, tracker, Fleet Hub, and error projections never include report text.
6. The ASGI replay path does not signal disconnect until the original receive
   callable does.
7. Existing Grok Bot queue, MCP, operations, feed, selector, and tracker tests
   stay green.

## Verification

Run through Brigade:

```bash
pytest -q tests/test_grokbot_jobs.py tests/test_grokbot_mcp.py tests/test_grokbot_ops.py tests/test_grokbot_feed.py tests/test_grokbot_scout_feed.py tests/test_cloud_tracker.py
ruff check src/brigade/grokbot_jobs.py src/brigade/grokbot_mcp.py tests/test_grokbot_jobs.py tests/test_grokbot_mcp.py
ruff format --check src/brigade/grokbot_jobs.py src/brigade/grokbot_mcp.py tests/test_grokbot_jobs.py tests/test_grokbot_mcp.py
git diff --check
```
