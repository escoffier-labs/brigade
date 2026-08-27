# Issue #568 Slice 5: Recovery Checkpoints Implementation Plan

Implementation plan for the slice 5 boundary of GitHub issue #568
(https://github.com/escoffier-labs/brigade/issues/568), paired with the design spec
`docs/phase-568-slice-5-recovery-checkpoints.md`. Prior slice docs:
`docs/phase-568-slice-3-run-projector.md` and `docs/phase-568-slice-4-shadow-comparison.md`.

Every task is RED first: write the named failing tests that pin the behavior, implement the
full behavior so the focused suite goes green, then commit. No task lands a placeholder API
or a not-implemented function, and no commit knowingly leaves a prior test failing. Where a
prior test must change (the slice-2 implicit-activation tests, the slice-4 version-1
readiness tests, `test_unmapped_status_writes_run_json_without_journal_event`), the task
names it and updates it in the same commit.

Module ownership across the six tasks:

- Task 1: `src/brigade/run_checkpoint.py` (new), `src/brigade/run_events.py`, `src/brigade/run_journal.py`. `tests/test_run_checkpoint.py` (new), `tests/test_run_events.py`, `tests/test_run_journal.py`.
- Task 2: `src/brigade/run_lifecycle.py`, `src/brigade/run_checkpoint.py`, `src/brigade/aboyeur/`. `tests/test_run_lifecycle.py` (lifecycle and aboyeur hook coverage), `tests/test_run_checkpoint.py`.
- Task 3: `src/brigade/run_projector.py`, `src/brigade/run_shadow.py`. `tests/test_run_projector.py`, `tests/test_run_shadow.py`.
- Task 4: `src/brigade/runguard.py`. `tests/test_runguard.py`.
- Task 5: `src/brigade/runs_cmd.py`, `src/brigade/run_checkpoint.py`. `tests/test_runs_cmd.py`, `tests/test_run_checkpoint.py`.
- Task 6: `src/brigade/doctor.py`. `tests/test_doctor.py`.

Each task lists its focused verification. The closeout runs the full `./scripts/verify`
gate (ruff lint, ruff format check, version-sync check, mypy, full pytest with the coverage
floor). Per the workspace `AGENTS.md`, report the actual result and paste any failure
verbatim. never claim success you did not observe.

## Task 1: Checkpoint event type, payload validation, crash-safe publish, bounded journal reader

RED. New `tests/test_run_checkpoint.py`:

- `test_checkpoint_event_type_registered_with_closed_payload_keys` (mirrored in
  `tests/test_run_events.py`): `run_events.EVENT_TYPES` contains
  `run.snapshot.checkpointed` with exactly `path`, `sha256`, `media_type`, `byte_size`,
  `privacy_class`, `paired_event_type`.
- `test_validate_checkpoint_accepts_well_formed_payload`.
- `test_validate_checkpoint_rejects_malformed_payloads_without_opening_files`: extra key,
  missing key, wrong `media_type`, wrong `privacy_class`, uppercase or short `sha256`,
  boolean or out-of-range `byte_size`, absolute or backslash or `..` `path`, filename stem
  not equal to `sha256`, and a non-null `paired_event_type` that is not a mapped lifecycle
  event type each raise a bounded `CheckpointError` with a category and no file opened.
- `test_publish_checkpoint_file_writes_private_file_with_matching_digest`: 0o600 file at
  `events/recovery-checkpoints/<sha256>.json` under a 0o700 no-follow directory, bytes equal
  to the input.
- `test_publish_checkpoint_file_matching_collision_is_noop`: the existing final file is
  opened no-follow, verified as a regular single-link file, verified byte-equal, and left
  untouched.
- `test_publish_checkpoint_file_mismatched_collision_fails_closed`: category
  `collision-mismatch`, existing file untouched.
- `test_publish_checkpoint_file_unsafe_collision_fails_closed`: a symlink, non-regular
  inode, or file with link count above one fails with `collision-unsafe` and is untouched.
- `test_validate_checkpoint_open_fd_hardening`: symlink, non-regular file, and link count
  above one refused. size, digest, or writer-canonical-byte mismatch each fail with a
  bounded category.

New failing tests in `tests/test_run_journal.py`:

- `test_read_journal_bounded_matches_read_journal_on_complete_journal`.
- `test_read_journal_bounded_refuses_oversize_journal_before_allocation`: a journal larger
  than `MAX_JOURNAL_BYTES` (8 MiB) is refused via `fstat` before any whole-file allocation
  and reported as `bound exceeded`.
- `test_read_journal_bounded_refuses_event_sequence_513`: bounded chunk reads stop at the
  first complete event whose sequence is 513 (`MAX_JOURNAL_EVENTS` 512), reported as
  `bound exceeded`.

GREEN. Register `run.snapshot.checkpointed` in `run_events.EVENT_TYPES` with the closed
payload key set. Create `src/brigade/run_checkpoint.py`: constants
(`CHECKPOINT_EVENT_TYPE`, `CHECKPOINT_MEDIA_TYPE`, `CHECKPOINT_PRIVACY_CLASS`,
`CHECKPOINT_DIR_NAME`, `MAX_CHECKPOINT_BYTES` 16 MiB, `MAX_JOURNAL_BYTES` 8 MiB,
`MAX_JOURNAL_EVENTS` 512), `CheckpointError(RuntimeError)` with a bounded category,
`checkpoint_dir`, `checkpoint_path`, `validate_checkpoint` (full payload validation before
any path access, then exactly one no-follow fd with regular-file, link-count, size, digest,
and writer-canonical-byte equality checks), and the crash-safe publish helper (0o700
no-follow mkdir, private `tempfile.mkstemp` temp 0o600, write, flush, fsync, atomic
no-replace `os.link`, on `EEXIST` apply the same no-follow regular single-link fd hardening
before requiring exact byte and digest equality, else `collision-unsafe` or
`collision-mismatch`, POSIX-guarded directory fsync mirroring
`aboyeur._supports_directory_fsync`, unlink temp). Add `run_journal.read_journal_bounded`
(no-follow open, `fstat` before allocation, refuse above the byte bound before allocation,
bounded chunk reads, stop at sequence 513). existing `read_journal` stays compatible.

Focused: `pytest tests/test_run_checkpoint.py tests/test_run_events.py
tests/test_run_journal.py`. Commit: `feat(run-checkpoint): checkpoint event type, payload
validation, crash-safe publish, bounded journal reader`.

## Task 2: Explicit activation prepare step and fail-closed checkpoint hook

RED. Extend `tests/test_run_lifecycle.py`:

- `test_prepare_lifecycle_journal_creates_private_journal_under_held_lock`: with
  `lifecycle_journal_requested` pending and the journal absent,
  `run_lifecycle.prepare_lifecycle_journal` under the matching held `runguard.run_lock`
  creates 0o700 `events` and 0o600 `lifecycle.jsonl`.
- `test_prepare_lifecycle_journal_is_noop_when_journal_exists`.
- `test_prepare_lifecycle_journal_raises_bounded_error_on_io_failure`:
  `LifecycleJournalError` before any checkpoint or status event is appended.
- `test_record_lifecycle_transition_requires_existing_journal`: a mapped-status write whose
  journal is absent fails closed as `LifecycleJournalError`.
- `test_checkpoint_event_is_sequence_one_on_first_activated_write`: for the first activated
  write the checkpoint event is sequence 1 and the mapped status event (when the transition
  is real) is sequence 2. for an unmapped first status the checkpoint is sequence 1 and no
  status event follows.
- `test_checkpoint_failure_propagates_and_write_does_not_advance`: a checkpoint file or
  event failure propagates `CheckpointError` out of `aboyeur._write_json`. the lifecycle
  append and the `run.json` replace do not run and `run.json` keeps the prior state.
- Update the slice-2 tests that asserted implicit activation inside the first mapped-status
  write to assert the explicit prepare step instead.
- Update `test_unmapped_status_writes_run_json_without_journal_event` to account for the
  checkpoint event without weakening its no-status-event assertion.

Extend `tests/test_run_checkpoint.py`:

- `test_write_checkpoint_appends_event_and_replays_on_repeat`: first call appends
  `run.snapshot.checkpointed` with `paired_event_type` set to the exact adjacent mapped
  event type or null. a repeat of the same write returns the committed event with no new
  event and no new file.
- `test_write_checkpoint_same_bytes_different_pairing_uses_distinct_key`: equal checkpoint
  bytes paired with two different event contracts append distinct checkpoint events
  without `IdempotencyConflict`.

GREEN. Add `run_lifecycle.prepare_lifecycle_journal` (authority via
`runguard.is_active_run_owner`, idempotent, creates the empty journal through
`run_journal.ensure_journal` only when the request is pending under the held lock, raises
`LifecycleJournalError` on bounded failure). Remove implicit journal creation from
`record_lifecycle_transition`. it requires the journal to exist and keeps the same-status
and unmapped-status skip rules. Add `run_checkpoint.write_checkpoint` (ensure activation via
`prepare_lifecycle_journal`, publish the file through the task-1 helper, append the
checkpoint event with idempotency key
`checkpoint:<sha256>:<paired-event-type-or-none>`). Wire the hook in
`aboyeur._write_json` for `run.json` dict payloads in this order:
`prepare_lifecycle_journal`, `write_checkpoint`, the existing
`record_lifecycle_transition`, the existing `localio.write_text_atomic` replace, then the
existing `run_shadow.record_shadow_comparison`. A `CheckpointError` propagates before the
lifecycle append and the replace. `runguard._recover_run_artifact` repair writes stay
uncheckpointed.

Focused: `pytest tests/test_run_lifecycle.py tests/test_run_checkpoint.py`. Commit:
`feat(run-lifecycle): explicit activation prepare step and fail-closed checkpoint hook`.

## Task 3: Projector status-neutral rule, PROJECTOR_VERSION 2, shadow readiness staleness

RED. Extend `tests/test_run_projector.py`:

- `test_checkpoint_event_is_status_neutral_and_advances_chain_cursor`: a sequence whose tail
  is `run.snapshot.checkpointed` derives the same status as the prior event, advances
  `journal_last_sequence` and `journal_last_event_digest`, and passes the
  contiguous-sequence and digest-link checks.
- `test_checkpoint_first_event_derives_base_status`.
- `test_checkpoint_after_unmapped_status_preserves_last_mapped_status`.
- `test_projector_version_is_two`.
- Keep `test_empty_events_no_journal_preserves_base_status_and_deep_copies` green unchanged.

Extend `tests/test_run_shadow.py`:

- `test_version_one_shadow_artifact_is_stale`: `check_projection_readiness` treats a
  version-1 artifact as stale and the gate closes on `evidence-schema-mismatch`.
- `test_version_two_artifact_with_checkpoint_tail_reads_ready_when_bytes_match`.
- Update the slice-4 tests that asserted a version-1 artifact reads as ready to assert
  staleness under version 2.

GREEN. In `run_projector.project_run_snapshot`, add the status-neutral handling for
`run.snapshot.checkpointed`: no `EVENT_STATUS` row, skip status derivation for it, still
advance the chain cursor through it. Bump `PROJECTOR_VERSION` to 2. In
`run_shadow.check_projection_readiness`, require `projector_version` 2. a version-1 artifact
is stale with reason `evidence-schema-mismatch`.

Focused: `pytest tests/test_run_projector.py tests/test_run_shadow.py`. Commit:
`feat(run-projector): status-neutral checkpoint rule, PROJECTOR_VERSION 2, shadow readiness
staleness`.

## Task 4: runguard run_lock_state and the before_terminalize callback

RED. Extend `tests/test_runguard.py`:

- `test_run_lock_state_absent`, `test_run_lock_state_live`, `test_run_lock_state_stale`,
  `test_run_lock_state_foreign`, `test_run_lock_state_invalid`: the five states, where
  `live` covers both the current process and a live foreign pid.
- `test_run_lock_state_never_raises_and_never_mutates`.
- `test_recover_stale_run_invokes_before_terminalize_after_token_check`: the callback runs
  after `_claim_stale_lock` and the owner-token check, before `_finish_claimed_recovery`.
- `test_recover_stale_run_invokes_callback_for_existing_pending_claim`: the same callback
  runs when targeted recovery resumes an already-renamed `.stale` claim.
- `test_recover_stale_run_callback_error_restores_claimed_lock_and_raises`: any callback
  error triggers `_restore_claimed_lock` and a bounded `RunLockError` naming the retained
  lock path. the claim is not left dangling.
- `test_pending_activated_claim_without_callback_requires_explicit_recovery`: generic lock
  acquisition retains an activated-journal claim instead of terminalizing it without
  checkpoint validation.
- `test_recover_stale_run_without_callback_terminalizes_as_today`.
- `test_recover_stale_run_refuses_live_owner_current_and_foreign_pid`: every matching live
  owner is refused with `RunLockError` and no mutation.
- `test_recover_run_artifact_persists_lock_recovery_provenance`: when owner metadata
  contains `_lock_workspace` or `acquired_at`, the activated-journal callback path copies
  them to `failure.lock_workspace` and `failure.lock_acquired_at` before deleting the claim.
  absent owner values produce no keys.
- `test_legacy_recover_run_artifact_does_not_add_recovery_provenance`: no-callback
  no-journal terminalization remains byte-compatible with the current receipt.
- Confirm `is_active_run_owner` still recognizes only the current process and stays the
  normal-writer authority.

GREEN. Add `runguard.run_lock_state(workspace, run_dir)`: read-only, never raises, never
mutates, returns exactly `absent`, `live`, `stale`, `foreign`, or `invalid` per the spec
definitions. Add the optional keyword `before_terminalize` to `recover_stale_run` and pass
it into `_recover_pending_claims`: after either a new stale claim and token check or
takeover of a matching pending claim, invoke `before_terminalize(owner)`. on any callback
error call `_restore_claimed_lock` and raise a bounded `RunLockError` naming the retained
path. otherwise proceed to the existing `_finish_claimed_recovery`. When no callback is
supplied, `_recover_pending_claims` terminalizes legacy no-journal claims as today but
retains activated-journal claims and raises a bounded error requiring explicit
`brigade runs recover`, including from generic lock acquisition. Do not change
`is_active_run_owner`. Thread an internal `persist_recovery_provenance` flag through
`_finish_claimed_recovery` to `_recover_run_artifact`, enabled only after a supplied
`before_terminalize` callback succeeds. On that path, copy optional `_lock_workspace` and
`acquired_at` owner values into the private failure object as `lock_workspace` and
`lock_acquired_at`. The default false path keeps legacy no-journal receipts byte-compatible.
all existing `setdefault` and terminalization semantics stay unchanged.

Focused: `pytest tests/test_runguard.py`. Commit: `feat(runguard): read-only run_lock_state
and recover_stale_run before_terminalize callback`.

## Task 5: Recovery integration in runs_cmd.recover

RED. Extend `tests/test_runs_cmd.py`:

- `test_runs_recover_restores_missing_run_json_from_latest_checkpoint`: activated journal,
  dead owner, missing `run.json`. the callback writes byte-identical checkpoint bytes as
  the intermediate receipt, then exit 0 after existing stale-lock terminalization.
- `test_runs_recover_restores_corrupt_run_json_and_preserves_original`: corrupt `run.json`
  renamed to `run.json.corrupt-<uuid>`, checkpoint bytes restored as the intermediate
  receipt, then exit 0 after terminalization.
- `test_runs_recover_terminalization_preserves_receipt_fields`: after the checkpoint
  restore, the existing `_finish_claimed_recovery` terminalization keeps `task`, `roster`,
  `cwd`, `lock_workspace`, and the failure attribution from the restored bytes.
- `test_runs_recover_partial_tail_quarantined_then_verified`: a partial final line is
  quarantined via `run_journal.recover_partial_tail`, the journal re-read, and recovery
  proceeds only when the truncated chain verifies.
- `test_runs_recover_fail_closed_on_invalid_latest_checkpoint`: no earlier-checkpoint
  selection and no legacy fallback for activated-journal runs (exit 2).
- `test_runs_recover_fail_closed_on_uncovered_tail`, `..._on_chain_break`,
  `..._on_unknown_event_type`, `..._on_envelope_run_id_mismatch`,
  `..._on_corrupt_rename_oserror`: each exit 2 with no `run.json` mutation beyond preserving
  a corrupt file, and the claim restored via `_restore_claimed_lock`.
- `test_runs_recover_validates_parseable_run_json_before_terminalization`: a valid
  activated journal and checkpoint are checked, the parseable receipt is not restored over,
  and existing terminalization proceeds.
- `test_runs_recover_parseable_run_json_fails_closed_on_invalid_chain_or_coverage`: a
  parseable nonterminal receipt does not bypass chain, latest-checkpoint, or tail-coverage
  validation. exit 2, receipt unchanged, claim restored.
- Keep green, unchanged: `test_runs_recover_refuses_live_owner_without_changing_artifacts`,
  `test_runs_recover_is_idempotent_for_terminal_run`,
  `test_runs_recover_clears_matching_dead_lock_after_artifact_was_already_terminal`, the
  no-journal legacy tests
  `test_runs_recover_reconstructs_missing_run_json_from_matching_dead_lock` and
  `test_runs_recover_preserves_and_reconstructs_corrupt_run_json`, and
  `test_runs_show_surfaces_stale_lock_recovery_and_returns_nonzero` with
  `test_runs_watch_surfaces_stale_lock_recovery_and_returns_nonzero`.

Extend `tests/test_run_checkpoint.py`:

- `test_latest_checkpoint_event_selects_highest_sequence`.
- `test_recover_from_checkpoint_returns_repaired_and_already_terminal`.

GREEN. In `runs_cmd.recover`, add the entry gate: resolve the workspace via
`runguard.resolve_run_lock_workspace`, inspect the lock via `run_lock_state`, refuse a live
owner (current process or foreign pid) with exit 2 and no mutation, clear a terminal
`stale-lock-recovery` run as today, report a `foreign` or `invalid` lock without claiming,
and route no-journal runs to the legacy `runguard.recover_stale_run` path unchanged. For an
activated-journal run with a dead owner, call `recover_stale_run` with a
`before_terminalize` callback that restores checkpoint bytes only when `run.json` is missing
or unparseable, but always validates before terminalization: bounded
`read_journal_bounded` read, partial-tail quarantine and re-read, chain verification
(contiguous from sequence 1, linked `previous_digest`, one `run_id` equal to
`run_dir.name`), highest-sequence checkpoint selection, `validate_checkpoint` with
open-fd hardening, envelope `run_id` equality against the verified journal `run_id` and
`run_dir.name`, and coverage check. Only then, for a missing or unparseable receipt,
preserve corrupt `run.json` and restore via `localio.write_text_atomic`. Add
`run_checkpoint.latest_checkpoint_event` and `run_checkpoint.recover_from_checkpoint`,
returning `repaired` after an intermediate restore or `validated` for a parseable receipt.
The existing terminalization then writes the final stale-lock failure receipt while
preserving the restored fields. Map `CheckpointError` to exit 2.

Focused: `pytest tests/test_runs_cmd.py tests/test_run_checkpoint.py
tests/test_runguard.py`. Commit: `feat(runs-cmd): checkpoint recovery integration with
fail-closed validation`.

## Task 6: Doctor read-only recovery checkpoint check

RED. Extend `tests/test_doctor.py`:

- `test_doctor_includes_recovery_checkpoints_check`: `core_station_checks` emits one
  aggregate three-field `CheckResult` named `runs: recovery checkpoints`, preserving the
  existing station callback type.
- `test_doctor_scans_at_most_50_immediate_run_dirs_newest_first`: no recursive walk, omitted
  count reported inside the aggregate detail, not as a separate status line.
- `test_doctor_fail_on_bound_exceeded_without_parsing_further`: journal above 8 MiB or 512
  events, or checkpoint above 16 MiB, is `FAIL` with `bound exceeded`.
- `test_doctor_fail_on_partial_tail_without_quarantine`: a partial final journal line is
  `FAIL` and `recover_partial_tail` is never called.
- `test_doctor_per_run_verdicts`: `OK` for a matching checkpoint, `WARN` for a missing or
  unparseable `run.json` with a valid latest checkpoint, `FAIL` for a malformed chain,
  dangling or invalid latest checkpoint, digest or size or content mismatch, envelope
  `run_id` mismatch, uncovered tail, or bound excess, and `OK` for a live owner per
  `run_lock_state`.
- `test_doctor_accepts_valid_stale_recovery_terminal_receipt`: a parseable,
  recovered receipt is `OK` only when Doctor reconstructs the entire expected object from
  the checkpoint using `_recover_run_artifact`'s transformation and gets exact equality.
  It replays the conditional `cwd`, `lock_workspace`, and `started_at` `setdefault` calls
  from `failure.lock_workspace` and `failure.lock_acquired_at`, which Task 4 persists before
  the lock disappears.
  The reconstruction cross-checks `error`, `failure.detail`, integer `owner_pid`,
  `prior_status`, the three recovery timestamps, optional `seat` or `seats` attribution,
  the fixed phase and kind, the stamped schema, and every untouched checkpoint field.
  Another parseable checkpoint mismatch is `FAIL`.
- `test_doctor_checkpoint_check_never_mutates`: journal, checkpoint files, and `run.json`
  are byte-identical after the check. detail lists up to 8 failing run names and `--full`
  lists every run.

GREEN. Add the scoped check to `doctor.core_station_checks` using only the core `OK` /
`WARN` / `FAIL` / `MANUAL` vocabulary, `run_lock_state` for the live-owner verdict,
`read_journal_bounded` for journal reads, and `run_checkpoint.validate_checkpoint` for the
fd-first 16 MiB checkpoint bound plus `latest_checkpoint_event` for the per-run verdict.
Accept a checkpoint-byte mismatch only when a private Doctor helper reconstructs the full
expected stale-recovery object from the checkpoint and candidate recovery timestamp and
owner pid, replaying the persisted workspace and acquisition-time `setdefault` inputs as
pinned by the test above. all other parseable mismatches are `FAIL`. Return a
three-field `CheckResult`. do not change `station.py` or its station callback contract.
Legacy and no-journal runs are omitted and counted in the aggregate detail. Doctor never
quarantines, repairs, or mutates, and has no wall-clock timeout.

Focused: `pytest tests/test_doctor.py tests/test_run_checkpoint.py`. Commit:
`feat(doctor): read-only recovery checkpoint scoped check`.

## Closeout: full verification, outcome capture, MiseLedger export, memory handoff

1. Re-run every focused suite from tasks 1 through 6, then run the full `./scripts/verify`
   gate. Report the actual result and paste any failure verbatim. If the gate fails, fix the
   code, not the tests. never weaken, skip, or delete a test to make it pass, and say so
   explicitly before touching a test that is itself wrong.
2. Record the verification outcome through the `brigade work verify` flow so the slice
   receipt is captured and attributed.
3. Export the slice evidence through MiseLedger.
4. Write the memory handoff to `.claude/memory-handoffs/` per the workspace `AGENTS.md`
   template: the fail-closed checkpoint hook ordering, the crash-safe temp-plus-hard-link
   publish pattern, the runguard authority split (`is_active_run_owner` for the normal
   writer, read-only `run_lock_state` for inspection, `recover_stale_run` with
   `before_terminalize` for the claim), the `PROJECTOR_VERSION` 2 one-way door, the doctor
   read-only bounds, and any root causes or gotchas found during implementation, failures
   included.

Rollback constraints. Each task is a separate conventional commit on the slice branch. If a
task is found wrong after merge, revert that commit. Reverting task 2 leaves `run.json`
writes uncheckpointed but byte-identical to today. Reverting task 3 alone is unsafe (a
version-2 projector without the registered checkpoint event type fails closed on any
activated journal that already carries a checkpoint event). roll tasks 2 and 3 back together
and drop the activated journals. Reverting tasks 4 through 6 disables recovery and doctor
reporting without affecting the live writer. Never `git push --no-verify`. the content-guard
pre-push hook must pass.

Slice 6 boundary. Slice 5 lands the recovery substrate only. Slice 6 (issue #568 step 6)
cuts the journal over to the authoritative live writer for new runs, gated per run by the
slice-4 readiness gate plus the issue parity measurement criteria. No slice-6 work is
started in this plan.

Citations. GitHub issue #568 (https://github.com/escoffier-labs/brigade/issues/568). Spec:
`docs/phase-568-slice-5-recovery-checkpoints.md`. Prior slices:
`docs/phase-568-slice-3-run-projector.md`, `docs/phase-568-slice-4-shadow-comparison.md`.
No code is implemented in these documents.
