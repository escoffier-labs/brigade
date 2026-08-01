# Issue #651 implementation plan: cross-process journal serialization and safe live control

Status: planned. This plan applies on top of #641, which introduced lifecycle-journal-backed control requests. It is intentionally ordered so the journal serialization primitive lands before callers depend on it.

## Scope and invariants

- Keep the existing event canonicalization and digest algorithms unchanged.
- Keep the existing event-type registry unchanged: this work does not add, remove, or rename event types.
- Do not edit golden journal fixtures. New tests must construct their own journals and assert their current canonical output through the existing APIs.
- Continue to fail closed on an invalid, partial, or forked journal. This change prevents new forks; it does not invent recovery for an already forked journal.

## Ordered implementation steps

1. **Add one cross-process mutation lock to `run_journal`.**

   - Derive the lock file as `journal_path.with_name(f"{journal_path.name}.lock")`, so a lifecycle journal at `events/lifecycle.jsonl` uses the private sibling `events/lifecycle.jsonl.lock`. Open or create that regular file with mode `0o600`, reject symlinks using the module's existing no-follow/identity safeguards, and leave the file in place after use.
   - Change `_append_critical_section` to accept the journal path. Inside that context manager, retain the current SIGTERM deferral and in-process reentry protection, then acquire an exclusive advisory `fcntl.flock(fd, LOCK_EX)` on the sibling lock descriptor before any tail read, idempotency inspection, canonical build, write, fsync, truncate, or replace. Release with `LOCK_UN` and close the descriptor in `finally`, including when the protected mutation raises. A lock-release failure must not mask the primary exception.
   - Provide one journal-owned mutation context for non-append mutations and make it enter `_append_critical_section(journal_path)` rather than creating another locking protocol. It must cover the full read/verify-to-replace window, not only `os.replace`.
   - Route all mutation paths through that section: `append_event` and idempotency lookup, `recover_partial_tail`, owner lifecycle and dispatch/checkpoint append calls, external `runs steer` and `runs interrupt` control appends, the redaction anchor append, and every redaction journal re-chain or replacement path. Redaction must hold the section from its verified read through replacement so an external control append cannot be lost; reorganize nested calls around the locked journal primitive rather than taking a second lock descriptor recursively.
   - Test: add `test_append_event_fcntl_lock_serializes_two_subprocess_writers` in `tests/test_run_journal.py`. Start two real Python subprocesses from a barrier, have each append after reading the same initial tail, and assert the resulting report has contiguous unique sequences, one digest-linked chain, and no `chain_errors`. Add `test_append_critical_section_releases_fcntl_lock_after_exception`, which injects a mutation failure and proves a later subprocess can acquire the lock and append. Extend `tests/test_run_redaction.py` with a barrier-backed redaction-versus-external-append test proving the final verified journal contains neither a dropped control line nor a fork.

2. **Make owner lifecycle writes retry a stale tail without changing their request identity.**

   - Add a single owner append helper in `run_lifecycle` and route `record_dispatch_fact`, `record_lifecycle_event`, and `record_lifecycle_transition` through it. Preserve the originally derived event payload and idempotency key for all attempts.
   - On `StaleSequenceError`, re-read and fully verify the journal's current chain head, set `expected_previous_sequence` from that fresh head, and retry the same append. Use three retries after the first attempt (four total attempts), with `time.sleep(0.01 * 2**retry_index)` between retries. Do not retry partial tails, chain errors, canonicalization errors, ownership failures, or I/O failures.
   - After all four attempts, surface the bounded `LifecycleJournalError` category `lifecycle_append_stale_exhausted`, chained from the last `StaleSequenceError`; do not advance `run.json` or convert it into a successful lifecycle transition. The error message remains bounded and must not include provider or journal body text.
   - Test: add `test_owner_lifecycle_append_retries_stale_tail_and_preserves_idempotency` and `test_owner_lifecycle_append_stale_retries_exhausted_blocks_run_json` in `tests/test_run_lifecycle.py`. The first injects one stale result, asserts a fresh tail read and one committed event; the second injects four stale results, asserts the bounded failure, backoff calls, and unchanged snapshot.

3. **Reject control against a run that is not both live and actively owned before it can journal.**

   - Add a control preflight used by `execute_control_request` before request-id inspection, shadow synchronization, `control.requested`, or any terminal control append. It reads `run.json`, requires a valid nonterminal status and no terminal completion marker, resolves the recorded lock workspace, and asks a new `runguard` predicate whether a live lock owner for this exact resolved run directory exists. This predicate checks the owner metadata and PID as an active owner, rather than requiring the external CLI process itself to own the lock.
   - Return stable bounded `ControlJournalError` codes: `control_run_not_live` for missing, malformed, or terminal run status and `control_owner_not_active` for a missing, stale, foreign, or unreadable owner lock. Do not call the transport, append a control event, update shadow evidence, or rewrite `run.json` after either refusal.
   - Test: add `test_control_request_refuses_completed_run_before_journal_or_transport` in `tests/test_run_events_cursor_control.py`, using a completed journal-backed run with persisted transport metadata. Assert the journal bytes and shadow bytes are identical before and after, no send callback runs, and the error code is `control_run_not_live`. Add a corresponding inactive-owner test for `control_owner_not_active`.

4. **Bound `control.failed` to a closed error class and a redacted detail reference.**

   - Define `ControlFailureClass` in `run_control_journal` with exactly these members and serialized values: `TRANSPORT_EXCEPTION = "transport_exception"`, `TRANSPORT_REJECTED = "transport_rejected"`, and `TRANSPORT_PROTOCOL_ERROR = "transport_protocol_error"`. Map a caught transport exception, an explicit `{"ok": false}` result, and a malformed or missing `ok` result to those classes respectively. Provider-returned codes and messages never select or extend the enum.
   - Replace the free-text `code` and `detail` fields in `control.failed` with `error_class` and `detail_digest`. `detail_digest` is the lowercase 64-hex SHA-256 digest of the complete UTF-8 detail string that would otherwise have been recorded, before truncation or normalization. It is a reference only: neither the detail string, provider error body, exception text, nor provider code is written to the journal. Replay responses use a fixed local message plus the stored class and digest reference.
   - Update the closed payload allowlist for the existing `control.failed` event to exactly `op`, `worker`, `turn_id`, `request_id`, `error_class`, and `detail_digest`; validate the enum before append. No event type or canonicalization rule changes.
   - Test: add `test_control_failed_payload_uses_closed_error_class_and_detail_digest` and `test_control_failed_rejects_unrecognized_error_class` in `tests/test_run_events_cursor_control.py`. Exercise all three enum members, assert a secret provider message and provider code are absent from raw journal bytes, and assert the digest is the SHA-256 reference of that detail.

5. **Close the same-request-id double-send window.**

   - Make the `inspect_control_request` result and the first `control.requested` append one journal-locked claim operation. When a caller finds or receives a committed requested event, compare its request digest with the submission fingerprint. A matching event is replay or indeterminate and must not invoke the transport; a mismatching one is the existing fingerprint conflict. Release the journal lock before calling the transport, preserving the current at-most-once request claim and crash semantics.
   - Test: add `test_concurrent_same_request_id_sends_once` in `tests/test_run_events_cursor_control.py`. Coordinate two callers with the same id and fingerprint, assert one transport call, exactly one requested event and one terminal event, and a replay or indeterminate result for the other caller.

6. **Let static `runs events` read past the first terminal lifecycle event.**

   - In `runs_cmd.events`, emit every verified event after the cursor through the current report's end. Record the first terminal event only to determine the return code after the complete currently available suffix is emitted; do not `break` at that terminal event. Preserve `--follow`'s existing exit-after-terminal behavior once that complete poll has been emitted.
   - Test: add `test_events_reads_post_terminal_redaction_and_control_records` in `tests/test_run_events_cursor_control.py`, seeding a terminal lifecycle event followed by valid `run.redaction.recorded` and `control.*` records. Assert all records and cursors are emitted in sequence and the terminal-derived exit code is retained.

7. **Make `runs events --follow` verify continuity across polls.**

   - Retain the last emitted sequence and digest, then before every later poll locate that sequence in the newly verified report and require the same digest. For a newly emitted suffix, also require its first event's `previous_digest` to match the retained digest. If a redaction rewrite or any other replacement changes the already-emitted prefix, stop before emitting a suffix with the stable cursor-continuity error rather than presenting discontinuous history.
   - Test: add `test_events_follow_refuses_suffix_after_redaction_rewrites_emitted_prefix` in `tests/test_run_events_cursor_control.py`. Run a follower past an initial cursor, perform a valid redaction rewrite between polls, append or expose the later suffix, and assert a continuity failure with no post-rewrite suffix written to stdout.

## Completion gate

Run the focused journal, lifecycle, control/cursor, and redaction test files while iterating, then run `./scripts/verify`. The final change must keep existing fixtures byte-identical and must pass the real subprocess race regression, completed-run refusal, bounded control-failure payload tests, and all three Minor regression tests.
