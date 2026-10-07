# Journal process qualification: bounded #1565 slice

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

The new scenarios protect process lifetime and interprocess ordering that the
same-process short-write and fsync injections cannot observe. They require no
new production exports, dependencies, services, stores, or fixture changes.
The fail-closed policy remains the contract. A reproduced production defect
must return to the accountable parent before any production repair.

Parent verification is pending: wrap `./scripts/verify-focused` through
Brigade with `tests/test_run_journal_process_qualification.py`,
`tests/test_run_journal_qualification.py`, `tests/test_run_journal.py`, and
`tests/test_run_projector.py`. Repeat both independent actual-diff reviews on
the repaired candidate. PR readiness requires the exact-head full gate once,
required CI, and actual native Windows qualification artifacts. Earlier
focused passage and the canceled full gate do not qualify the repaired head.
