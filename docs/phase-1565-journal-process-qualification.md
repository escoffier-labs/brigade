# Journal process qualification: bounded #1565 slice

## Historical test-only producer handoff

This slice adds independent process tests for the existing journal kernel at
base `da3d4db6d448d1ed82aa6153c38d01d3a1e2276b`. It changes no production
behavior. Issue #1565 remains open. The producer ran no tests or fixture
children, and supplies no runtime reproduction, native Windows result, or
scoreable evidence.

## Source contract

In [run_journal.py](../src/brigade/run_journal.py), `append_event` holds
`_append_critical_section` across tail verification, idempotency, sequence
checking, canonical write, and sync. `_read_tail_state` validates complete
records and refuses invalid history. Idempotency is checked before stale
sequence rejection. `_sync_existing_journal` supplies a fresh file sync and
the platform's directory sync before replay returns, without appending.
`read_journal_bounded` reports the verified prefix and exact partial suffix.
`recover_partial_tail` quarantines that suffix before truncating.
`journal_mutation` permits authorized reentry while retaining the outer lock.

The #651 serialization and merged #1605 sibling-lock corrections are present
in this base. `_open_journal_lock` uses `dirfd.open_child_lock_file` for bound
paths. In [nt_dirfd.py](../src/brigade/work_cmd/nt_dirfd.py), `open_lock_file`
uses dedicated descriptor-relative handles with rights and bidirectional
sharing compatible with CRT peers. This patch preserves those implementations.

[run_projector.py](../src/brigade/run_projector.py), `project_run_snapshot`,
validates event envelopes, contiguous sequence and digest links before deriving
status and the journal cursor. It rebuilds derived fields from accepted history.

## Coverage inventory

“Covered by existing tests” identifies authored coverage read at this base.
“Newly exercised” identifies scenarios authored in this slice. Neither label
claims test passage in this producer session.

| Invariant or limit | Classification | Source or test owner |
| --- | --- | --- |
| Canonical line, digest validity, sequence continuity and fail-closed append | Covered by existing tests | `tests/test_run_journal.py`: `test_append_event_writes_canonical_line_with_fsync`, `test_append_fails_closed_on_gapped_sequence`, `test_append_fails_closed_on_digest_invalid_line`, `test_append_fails_closed_on_repeated_idempotency_key` |
| Same key replays or conflicts without a second append | Covered by existing tests | `tests/test_run_journal.py`: `test_idempotent_replay_returns_existing_event_without_second_append`, `test_same_idempotency_key_different_digest_raises_without_mutation`. `tests/test_run_journal_qualification.py`: `test_same_key_process_race_has_one_record_and_consistent_result` covers POSIX flock with a Windows-incompatible pipe barrier |
| Same-head, same-key contenders serialize the actual tail/index transaction read through the msvcrt branch | Newly exercised | `tests/test_run_journal_process_qualification.py`: `test_same_key_process_race_replays_or_conflicts_without_second_record` with identical and conflicting payloads. First writer pauses after its real tail/index read. Contender proves OS byte-lock exclusion and signals its actual acquisition attempt before parent release. Its tail read requires release and completed acquisition, then observes the accepted index. Checks typed conflict identity/digests, one journal write, no contender write, valid history, and byte-preserving replay in another process |
| Distinct-key stale-head loser raises exactly one typed error and leaves the winner's bytes unchanged | Newly exercised | New module: `test_distinct_key_stale_head_process_loser_cannot_mutate_history`. Both children rendezvous before release. One-shot scheduling captures the winner's bytes before releasing the independent stale loser |
| Bound and unbound mutations share a sibling lock in either open order | Covered by existing tests | `tests/test_run_journal.py`: `test_windows_journal_lock_excludes_unbound_mutation_during_bound_tail_window`. `tests/test_run_journal_qualification.py`: `test_nt_journal_lock_shares_with_crt_peer_in_both_open_orders` |
| Deliberately terminated writer releases its actual OS byte lock and complete visible line replays after a fresh sync | Newly exercised | New module: `test_killed_writer_releases_os_lock_for_replay_or_partial_recovery[complete]`. Child pauses after the complete write at file-fsync entry, before calling the real OS fsync. A second process proves nonblocking byte-lock acquisition fails while the writer lives. Parent polls immediately before kill, rejects captured barrier-timeout diagnostics, and requires the platform's deliberate kill status before accepting successor replay |
| Deliberately terminated partial write blocks append without mutation, quarantines the exact suffix, and accepts the next sequence once | Newly exercised | Same new test, `[partial]`, with the same fresh pre-kill liveness, diagnostic and termination-status requirements. Child pauses after an actual deliberately shortened OS write. The successor verifies typed refusal and unchanged bytes before recovery, verifies restored prefix, retries and replays. Parent checks quarantine bytes and projection rebuilt from the two accepted events |
| Recovery preserves complete bytes and does not truncate when quarantine sync fails | Covered by existing tests | `tests/test_run_journal_qualification.py`: `test_crlf_recovery_preserves_complete_bytes_and_quarantines_exact_suffix`, `test_failed_quarantine_durability_preserves_partial_journal`, `test_short_write_recovery_then_retry_accepts_exactly_once` |
| Failed fsync can leave a complete visible line despite no successful append acknowledgment | Confirmed gap in a stronger no-visible-mutation promise, as documented by existing coverage | `append_event` writes before fsync. `tests/test_run_journal_qualification.py`: `test_failed_fsync_leaves_snapshot_unchanged_but_complete_journal_line_visible`, `test_fsync_error_retry_requires_new_sync_without_second_append`. These were read, not executed here |
| Projection rebuild, chain validation and deterministic encoding | Covered by existing tests and newly exercised after termination | `tests/test_run_projector.py`: `test_chain_errors_raise_event_chain_error`, `test_golden_replay_matches_expected_bytes`, `test_reprojection_is_byte_idempotent`. New termination test checks independent expected status, sequence and digest before comparing rebuilt bytes |
| Power-loss persistence, filesystem/device fault behavior, or crash inside the OS fsync call | Unobserved | These process tests do not simulate power loss, device failure, or an in-progress kernel sync. Complete visible bytes alone do not establish durable acceptance |
| Actual native Windows execution of the new module | Unobserved | Parent must supply native Windows artifacts with no new skips or allowlist changes |

## Process boundaries and pending verification

The new module has five cases across three test functions and no
authored skips. File barriers avoid `select.select` on Windows pipes. Temporary
scripts stay in short unique roots beneath the test suite's isolated
`Path.home()`. Child environments contain explicit platform and fixture keys.
Provider keys are never inherited. Fleet reporting is stubbed in the fixture
children to prevent unrelated identity discovery or network effects.

For same-key requests, both children first publish independently observed head
zero. The parent releases only the first writer and waits until its wrapper
has completed the real `_read_tail_state` call. That writer stays paused until
the parent releases a second file barrier. During the pause, the contender's
nonblocking OS byte-lock probe must fail with a lock-exclusion error. A first
tail read moved before lock acquisition therefore fails this proof regardless
of scheduling. The contender publishes an attempt marker immediately before
calling the retained `LK_LOCK` callable and publishes an acquisition marker
only after it returns. The parent requires the attempt and absence of
acquisition or tail entry before release. The contender's tail wrapper also
requires both release and completed lock acquisition before its real read,
then reports the accepted sequence, digest and idempotency key.

Windows children retain real `msvcrt`. POSIX children explicitly emulate that
branch with `fcntl.lockf` byte-region locks. POSIX results cannot qualify native
Windows sharing or NT handles. Termination uses `Popen.kill` and bounded reap,
with finally cleanup of all children before temporary directories are removed.
The parent polls the boundary writer immediately before killing it, captures
both output streams, and rejects child barrier timeouts. Required termination
status is negative `SIGKILL` on POSIX and `1` on Windows, where Python's
`Popen.kill` aliases `terminate` and calls `TerminateProcess` with exit code
`1`. The Windows branch never evaluates the POSIX signal constant.

The process scenarios protect process lifetime and interprocess ordering that
the same-process short-write and fsync injections cannot observe. That original
qualification slice required no production repair. Its failed-sync visibility
finding led to the approved residual repair below.

Focused verification now covers the process module through Brigade, as recorded
below. PR readiness requires the exact-head full gate once and an independent
implementation review. Landing requires current-head CI, including actual native
Windows qualification. Earlier focused passage and the canceled full gate do
not qualify the repaired head.

## Approved residual direction and integration boundary

The owner approved recovery-preserving indeterminate outcomes on 2026-10-11.
This supersedes the proposed requirement that every failed append leave visible
history unchanged. A complete canonical event may remain visible after a failed
file or directory sync. It may have committed. No rollback or additional
committed-prefix storage protocol is authorized.

The implemented bounded contract has three separate observations:

1. A failed append sync reports an indeterminate outcome with bounded diagnostics.
   It never reports that the event definitely failed to
   commit. Complete bytes remain available to identical-request replay.
2. Forensic reads, idempotency lookup and pure projection establish structural
   validity and hypothetical derived state. They perform no sync and provide no
   evidence that an earlier writer completed its durability barrier or that its
   caller received success.
3. Explicit retry or recovery validates an exact journal head under the existing
   mutation lock, completes fresh file and supported directory sync barriers,
   and only then publishes a confirmed recovered projection of that head. A
   failed recovery barrier leaves the journal bytes and prior projection intact.

Confirmation describes completion of the new barrier under the platform's sync
contract. It does not prove delivery of a past success response, survival of
arbitrary device failure, or the outcome of a writer killed before returning.
After restart, valid complete lines alone carry no historical acknowledgment
evidence. Recovery must establish its own barrier. An in-memory uncertainty flag
cannot replace that operation, and read APIs must not acquire durability by
performing hidden syncs.

At base `c3938be560b692443844a7fa444e51a0a5da37a3`, the chosen contract was
absent. Receipt `20261011-115816-work-verify-e7e166` ran:

```bash
brigade work verify run --target . --argv-json '["./scripts/verify-focused","tests/test_run_journal_qualification.py::test_failed_append_reports_indeterminate_outcome_without_discarding_history","tests/test_run_journal_qualification.py::test_recovery_cannot_publish_projection_when_fresh_journal_sync_fails","tests/test_run_journal_qualification.py::test_read_only_lookup_and_journal_read_do_not_sync","tests/test_run_journal_qualification.py::test_fsync_error_retry_requires_new_sync_without_second_append"]' --capture brigade-work
```

Lint, format, mypy, version sync and both snapshots passed. Pytest reported
`4 failed, 3 passed in 0.48s`. Both append cases failed with
`AssertionError: assert None == 'indeterminate'`. Both explicit recovery cases
failed with `Failed: DID NOT RAISE CheckpointError`: recovery restored a projection
despite continued failure of the journal barrier. Existing forensic no-sync and
byte-preserving identical-request replay cases passed. The receipt is audit-only.

The #1669 coordinator allocated `recover_from_checkpoint` in
`src/brigade/run_checkpoint.py` and `_bound_journal_failure` in
`src/brigade/run_lifecycle.py` before integration edits. Recovery now calls
`sync_journal_head` under the journal writer lock with the verified sequence and
digest before restoring or returning a projection. A changed head fails closed.
Lifecycle errors preserve the indeterminate outcome instead of flattening it.
Append and replay close failures also report uncertainty, and a close failure
cannot mask an earlier failed sync. The #1669 workspace reporting changes remain
owned by that lane and must survive integration.

The coordinator subsequently allocated `IndeterminateControlJournalError` and
the two control append catch blocks in `src/brigade/run_control_journal.py`.
They retain the stable `control_append_failed` code while exposing
`outcome="indeterminate"` through a typed subclass. Existing retry and CLI code
assertions remain unchanged. Receipt `20261011-121410-work-verify-4ec8e3` first
reproduced lost outcome information at both boundaries with `2 failed in 0.37s`.

Recovery restores a missing or corrupt snapshot. It does not replace an existing
parseable snapshot, and an authoritative status-changing retry can still fail
the prior-state gate before reaching identical-event replay. That case requires
explicit recovery handling rather than assuming retry repairs the snapshot.
The forensic projector has no new durability claim. Journal sync uncertainty is
separate from a requested side effect whose completion was never recorded.

On Windows, the existing directory-sync helper is a no-op. Local POSIX tests and
Windows emulation do not establish native Windows process qualification. The
fresh barrier promises only successful completion of the platform sync calls,
with no persisted proof of an earlier acknowledgment or stronger device claim.

Focused receipt `20261011-121428-work-verify-3a8d19` passed lint, format, mypy,
version sync and both snapshots, with `494 passed in 24.15s`. It selected
`tests/test_run_journal.py`, `tests/test_run_journal_qualification.py`,
`tests/test_run_projector.py`, `tests/test_run_journal_process_qualification.py`,
`tests/test_run_checkpoint.py`, `tests/test_run_lifecycle.py` and
`tests/test_run_events_cursor_control.py` through
`brigade work verify run` with `./scripts/verify-focused`. It includes unchanged
identical-event retry, pure forensic reads, killed-writer recovery and the new
sync/close uncertainty and recovery-publication regressions. At that receipt,
full verification, current-head independent implementation review and required
CI remained pending.
