# Issue #568 Slice 5: Recovery Checkpoints

Reviewed design specification for the approved slice 5 boundary of GitHub issue #568
(https://github.com/escoffier-labs/brigade/issues/568). Slices 1 through 4 landed the append-only journal kernel
(`run_journal`, `run_events`), opt-in lifecycle journaling (`run_lifecycle`, wired into `aboyeur._write_json`), the pure
run snapshot projector (`run_projector`), and shadow comparison plus the readiness gate (`run_shadow`. see
`docs/phase-568-slice-3-run-projector.md` and `docs/phase-568-slice-4-shadow-comparison.md`).

Slice 5 is a recovery-plane bridge. It durably checkpoints the exact `run.json` bytes the legacy writer is about to
commit, before the atomic replace, and teaches `brigade runs recover` and `brigade doctor` to verify the journal chain
and rebuild a missing or corrupt `run.json` from the latest checkpoint. The legacy writer stays authoritative for live
runs. Slice 6 is the authoritative writer cutover, gated by the slice-4 readiness gate plus the issue parity
measurement criteria. this slice does not cut over to the journal as the live writer.

Source contracts, all standard-library only: `run_events` (`EVENT_TYPES`, `canonical_bytes`, `request_digest`,
`validate_event`, `_HEX64`, `MAX_DIAGNOSTIC_LEN` 240, `MAX_LINE_BYTES` 16384, `CanonicalizationError`). `run_journal`
(`RunEvent`, `JournalReport`, `read_journal`, `read_journal_bounded`, `append_event`, `ensure_journal`,
`recover_partial_tail`, `_open_nofollow`, `_mkdir_private`, `_DIR_MODE` 0o700, `_FILE_MODE` 0o600). `run_lifecycle`
(`STATUS_EVENT_TYPE`, `record_lifecycle_transition`, `prepare_lifecycle_journal`, `LifecycleJournalError`, the
activation model, same-status and unmapped-status skip rules). `run_projector` (`PROJECTOR_VERSION`, `EVENT_STATUS`,
`project_run_snapshot`, `encode_snapshot_bytes`, `RunProjection`, the `ProjectionError` subclasses). `run_shadow`
(`record_shadow_comparison`, `check_projection_readiness`, `shadow_artifact_path`, the fail-open-writer /
fail-closed-gate split). `aboyeur._write_json`, `aboyeur._supports_directory_fsync`. `localio` (`write_text_atomic`,
`write_text_exclusive`, `write_json`, `file_sha256`). `runguard` (`run_lock`, `recover_stale_run`, `run_lock_state`,
`_recover_run_artifact`, `_finish_claimed_recovery`, `_claim_stale_lock`, `_restore_claimed_lock`, `_read_lock_owner`,
`_lock_is_stale`, `_pid_is_active`, `is_active_run_owner`, `resolve_run_lock_workspace`, `RunLockError`).
`runs_cmd.recover` (exit codes 0 and 2). `doctor` (`OK`, `WARN`, `FAIL`, `MANUAL`, `core_station_checks`,
`CheckResult`).

## Boundary

New module: `src/brigade/run_checkpoint.py`. Three integration points:

1. A checkpoint hook in `aboyeur._write_json`. The hook runs only after `run_lifecycle.prepare_lifecycle_journal`
   establishes activation for the current write. Activation is established when either (a) the run directory already
   carries an activated journal (`events/lifecycle.jsonl` exists), or (b) `lifecycle_journal_requested` is pending in
   `run.json` and the call holds the matching run lock. Pre-lock request bootstrap (`cli/run.py`) still only sets the
   request flag and never activates. The hook runs before the existing `record_lifecycle_transition` call. A
   checkpoint file or event failure is fail-closed: the bounded `CheckpointError` propagates out of `_write_json`, the
   lifecycle append and the `run.json` replace do not run, and the live write does not advance. The checkpoint is a
   recovery-plane gate on the live write, not a best-effort side effect.
2. A recovery integration in `runs_cmd.recover` that reads the journal and the latest checkpoint and performs exactly
   one repair mutation, restoring `run.json` from the checkpoint under the stale-lock claim.
3. A doctor check in `doctor.core_station_checks`, fully read-only.

The checkpoint never reads the live `run.json` after the replace, never appends to the journal during recovery, and
never mutates `run.json` outside the stale-lock claim. It is a write-ahead record of the exact bytes about to be
committed, content-addressed by their SHA-256.

### Coverage boundary, decided

- The checkpoint hook fires for every `aboyeur._write_json` call whose target is named `run.json`, whose payload is a
  dict, and for which `prepare_lifecycle_journal` has just established activation (the journal exists when the hook
  runs). This covers the aboyeur writers and `run_resume._resume_locked`. `runguard._recover_run_artifact` writes
  `run.json` through `localio.write_json` directly and is not checkpointed. that write is the recovery repair itself.
- The checkpoint is emitted for every activated-journal `run.json` write, including same-status detail refreshes and
  unmapped statuses (`dry-run`, `incomplete`, `artifact-collection`), wider than the lifecycle journal, which skips
  same-status and unmapped writes, so the checkpoint is the only durable record that covers them. Flag-off and legacy
  run directories create no checkpoint and observe byte-identical behavior. legacy no-journal recovery remains the only
  path for runs without `events/lifecycle.jsonl`.

## Activation model

Activation is the durable fact that `events/lifecycle.jsonl` exists for a run. Slice 2 activated implicitly inside the
first mapped-status write. Slice 5 replaces that with an explicit, separate prepare step so the first checkpoint can be
sequence 1.

- Pre-lock bootstrap (`cli/run.py` before `runguard.run_lock`): writes `lifecycle_journal_requested` set to true into
  `run.json` when the env flag is set. It never creates the journal and never appends. Unchanged from slice 2.
- Under the matching held run lock, `run_lifecycle.prepare_lifecycle_journal` validates authority with
  `runguard.is_active_run_owner(workspace, run_dir)`, and, when `lifecycle_journal_requested` is pending and the journal
  is absent, creates the empty private journal (0o700 `events`, 0o600 `lifecycle.jsonl`) via `run_journal.ensure_journal`.
  It is idempotent and raises `LifecycleJournalError` on any bounded I/O or canonicalization failure, before any
  checkpoint or status event is appended.
- The hook calls `prepare_lifecycle_journal` first, then writes the checkpoint. For the first activated write the
  checkpoint event is sequence 1, the optional mapped status event (for example `run.created` when the first status is
  `started`) is sequence 2, then the atomic `run.json` replace. For an unmapped first status the checkpoint is sequence
  1 and no status event follows. For later writes the checkpoint is sequence N and the mapped status event, when the
  transition is real, is sequence N+1.
- `record_lifecycle_transition` no longer creates the journal. It requires the journal to exist and appends the mapped
  status event under the active run lock, with the existing skip rules. A mapped-status write whose journal is absent
  fails closed as `LifecycleJournalError`, which the hook propagates.

## Event contract

A checkpoint is one event appended to the existing `events/lifecycle.jsonl` journal, under the existing
`brigade.run_event.v1` envelope. No new schema. The event type is `run.snapshot.checkpointed`, added to
`run_events.EVENT_TYPES` with a closed per-type payload key set.

Payload (flat, allowlisted):

| Key | Type | Value |
| --- | --- | --- |
| `path` | str | relative checkpoint path `events/recovery-checkpoints/<sha256>.json` |
| `sha256` | str | lowercase 64-char hex SHA-256 of the checkpointed `run.json` bytes |
| `media_type` | str | `application/vnd.brigade.run+json` |
| `byte_size` | int | length of the checkpointed bytes in bytes |
| `privacy_class` | str | `private` |
| `paired_event_type` | str or null | exact mapped lifecycle event type expected immediately after the checkpoint, or null for same-status and unmapped writes |

`media_type` and `privacy_class` are fixed exact values. `path` is relative to the run directory so the event stays
portable across `--output-dir` layouts and the projector never carries an absolute path. `paired_event_type` is the
`run_lifecycle.STATUS_EVENT_TYPE` mapping for the status in the checkpointed bytes when the write is a real mapped
transition, and null for a same-status refresh or an unmapped status. The checkpoint content is the exact `run.json`
bytes as encoded by `aboyeur._write_json` (sorted keys, two-space indent, one trailing newline, UTF-8), not the compact
`run_events.canonical_bytes` encoding, so the intermediate recovery restore is byte-identical before existing
stale-lock terminalization writes the final failure receipt. The enclosing checkpoint event's envelope
`run_id` must equal the single verified journal `run_id` and `run_dir.name`. no `run_id` key is carried in the payload.

## Payload validation

`validate_checkpoint` validates the full payload before any path access. A payload is valid only when all of the
following hold. any deviation raises `CheckpointError` with a bounded category diagnostic and no path is opened.

- The payload key set is exactly `path`, `sha256`, `media_type`, `byte_size`, `privacy_class`, `paired_event_type`.
  Extra or missing keys fail closed.
- `media_type` equals `application/vnd.brigade.run+json`.
- `privacy_class` equals `private`.
- `sha256` is a lowercase 64-character hex string (the `run_events._HEX64` shape).
- `byte_size` is an int, not a bool, in the closed range 0 through `MAX_CHECKPOINT_BYTES` (16 MiB).
- `path` is exactly `events/recovery-checkpoints/<sha256>.json` with the declared `sha256`, no leading slash, no `..`
  components, no backslashes, and the filename stem equals `sha256`.
- `paired_event_type` is null or a string in `run_events.EVENT_TYPES` that is a row in
  `run_lifecycle.STATUS_EVENT_TYPE` (a mapped lifecycle status event type).

Diagnostics are bounded to `run_events.MAX_DIAGNOSTIC_LEN` and carry a category only, never a raw path or payload value.

## Storage

Path: `<run-dir>/events/recovery-checkpoints/<sha256>.json`. The `recovery-checkpoints` directory is created 0o700 by
`run_journal`'s no-follow mkdir helper. Each checkpoint file is a 0o600 regular file published crash-safely from the
existing `localio.write_text_exclusive` pattern, not by a direct `O_EXCL` open on the final path:

1. Create the `recovery-checkpoints` directory 0o700 (no-follow mkdir).
2. `tempfile.mkstemp` a private temp file in the same directory (0o600), write the exact writer bytes, flush, fsync the
   temp fd.
3. Publish by `os.link(temp, final)` (atomic, no-replace hard link). On `EEXIST`, do not write the final path. Open the
   existing final file through the same no-follow, regular-file, single-link fd checks used by
   `validate_checkpoint`, then require exact byte and digest equality with the declared `sha256`: a safe matching
   collision is a no-op. an unsafe inode or mismatched collision raises `CheckpointError` with category
   `collision-unsafe` or `collision-mismatch`.
4. fsync the `recovery-checkpoints` directory on POSIX, guarded like `aboyeur._supports_directory_fsync`. on non-POSIX
   it is skipped.
5. unlink the temp file.

The atomic no-replace `os.link` publication is required and fails closed on platforms that do not support it. A crash
before step 3 leaves only the private temp. a crash between step 3 and step 5 leaves the final file durable and an
orphan temp (the next run unlinks either). The `events` directory stays 0o700 (slice 1), `recovery-checkpoints` is
0o700, and each checkpoint file is 0o600. `.brigade/runs/` is gitignored, so checkpoints are never tracked.

## Idempotency

The checkpoint event idempotency key is
`checkpoint:<sha256>:<paired-event-type-or-none>`. It stays below
`run_events.MAX_IDEMPOTENCY_KEY_LEN` for every mapped event type.
`run_journal.append_event` dedupes by idempotency key plus request digest:
same-key same-digest returns the committed event with no write. same-key different-digest raises
`IdempotencyConflict`. Binding the pairing to the key means replaying the same write is a complete no-op (no new
event, no new file, the existing final file verified byte-equal), while the same bytes with a different adjacent
event contract use a distinct key and append a distinct checkpoint event.

## Write ordering

For a `run.json` write where the hook is active, the hook reorders the existing steps so the checkpoint is durable
before the legacy `run.json` replace:

1. Prepare: `run_lifecycle.prepare_lifecycle_journal` ensures the empty journal exists under the active run lock
   (no-op when already activated, establishes activation when the request is pending under the held lock).
2. Checkpoint file: publish the exact writer bytes to `events/recovery-checkpoints/<sha256>.json` via the crash-safe
   temp-and-hard-link pattern above, fsync the file and (on POSIX) the checkpoint directory.
3. Checkpoint event: append `run.snapshot.checkpointed` to `events/lifecycle.jsonl` (fsync). On idempotency replay the
   final file already exists byte-equal and the event already exists digest-equal. both are no-ops.
4. Mapped status event: when the status is in `run_lifecycle.STATUS_EVENT_TYPE` and the transition is real,
   `record_lifecycle_transition` appends the mapped status event (fsync). Unmapped and same-status writes append no
   status event. This is the existing slice-2 behavior, unchanged except it now runs after the checkpoint event.
5. Atomic `run.json` replacement: `localio.write_text_atomic` replaces `run.json` with the same bytes the checkpoint
   holds. Then `run_shadow.record_shadow_comparison` runs after the replace, unchanged from slice 4.

The invariant: after step 3 fsyncs, the checkpoint file and event are durable. after step 5, `run.json` holds the same
bytes. A crash between 3 and 5 leaves a durable checkpoint whose bytes have not landed in `run.json`. recovery
restores them. A crash after 5 leaves `run.json` matching the checkpoint. The hook is fail-closed: a `CheckpointError`
from step 2 or 3 propagates out of `aboyeur._write_json` before step 4 and step 5, so a checkpoint subsystem failure
stops the live write.

## Projector rule

`run_projector` gains one status-neutral rule for `run.snapshot.checkpointed`: the event carries no status of its own,
so projection preserves the current derived status and advances only `journal_last_sequence` and
`journal_last_event_digest`. When the final event is `run.snapshot.checkpointed`, the derived `status` is the status
the sequence would derive at the prior event (or the base status when the checkpoint is the first event). `EVENT_STATUS`
gains no row for the checkpoint type. the projector skips status derivation for it and still advances the chain cursor
through it for the contiguous-sequence and digest-link checks.

`PROJECTOR_VERSION` bumps from 1 to 2. Recognized event semantics change (the projector now derives status through a
status-neutral checkpoint event), so version-1 shadow evidence is stale. The slice-4 shadow artifact carries
`projector_version`. a version-1 artifact is treated as stale by `check_projection_readiness`, and the readiness gate
closes on `evidence-schema-mismatch` until a fresh comparison lands under version 2. Regression coverage asserts that a
version-1 artifact no longer reads as ready and that a version-2 artifact with a checkpoint tail reads as ready when the
bytes match. The slice-3 invariant still holds when the tail is a checkpoint event: a checkpoint following an
unmapped-status write preserves the last mapped status, and shadow comparison is unaffected because the projected status
comes from the last mapped event.

## Recovery

`runs_cmd.recover` is extended for activated-journal runs. The legacy no-journal path
(`runguard.recover_stale_run` plus `_recover_run_artifact`) is unchanged and remains the only path for runs without
`events/lifecycle.jsonl`. `is_active_run_owner` is unchanged and remains only the normal-writer authority. it does not
gate foreign recovery. Slice 5 adds two narrow runguard APIs:

- `run_lock_state(workspace, run_dir)`: a read-only predicate returning a state for doctor/operator inspection:
  `absent` (no lock directory), `live` (lock exists and the owner pid is the current process or a live foreign pid),
  `stale` (lock exists, owner pid is dead, and `owner.json` records a matching `run_dir`), `foreign` (lock exists and
  `owner.json` records a non-matching `run_dir`), or `invalid` (lock directory present but `owner.json` missing or
  unparseable). These five states are the whole vocabulary. It never raises and never mutates.
- `recover_stale_run(cwd, run_dir, *, before_terminalize=None)`: runguard owns the stale claim. It claims the visible
  stale lock via `_claim_stale_lock`, verifies the owner token, and, when `before_terminalize` is provided, invokes
  `before_terminalize(owner)` before `_finish_claimed_recovery`. It also passes the callback through
  `_recover_pending_claims` when resuming an already-renamed `.stale` claim. If the callback raises for validation,
  partial-tail repair, corrupt preservation, bounded-read, or any other reason, either path calls
  `_restore_claimed_lock` and raises a bounded `RunLockError` naming the retained lock path. Only a successful callback
  proceeds to `_finish_claimed_recovery`. With no callback a legacy no-journal claim terminalizes exactly as today.
  `_recover_pending_claims` never auto-terminalizes a claim whose owner points to an activated journal when no callback
  is supplied. it retains the claim and raises a bounded error requiring explicit `brigade runs recover`. This prevents
  a later generic lock acquisition from bypassing checkpoint validation after a crash between claim and callback. It
  rejects every matching live owner (current process or foreign pid) with `RunLockError` and no mutation, and
  `runs_cmd.recover` relies on that refusal.

### Entry gate

Recovery resolves the run directory, reads `run.json` (tolerating a missing or unparseable file), resolves the
workspace from the run receipt (`runguard.resolve_run_lock_workspace`), and inspects the run lock via `run_lock_state`:
a live owner (state `live`), whether a foreign pid or the current process, returns exit 2 with the existing
`run owner process is still active` message and no mutation
(`test_runs_recover_refuses_live_owner_without_changing_artifacts`, plus a separate current-process test). a terminal
`run.json` whose `failure_phase` is `stale-lock-recovery` clears the matching dead lock as today (exit 0)
(`test_runs_recover_is_idempotent_for_terminal_run`,
`test_runs_recover_clears_matching_dead_lock_after_artifact_was_already_terminal`). a no-journal run routes to the
legacy `runguard.recover_stale_run` path unchanged. a foreign or invalid lock (state `foreign` or `invalid`) is
reported and not claimed.

### Stale-lock claim and checkpoint restore

When the owner is dead and the run has an activated journal, recovery calls `runguard.recover_stale_run` with a
`before_terminalize` callback. The callback always validates the activated journal and latest checkpoint, including
when `run.json` is parseable. only the restore mutation is conditional on a missing or unparseable `run.json`. It
reads `events/lifecycle.jsonl` with `read_journal_bounded` (no more than `MAX_JOURNAL_BYTES` or `MAX_JOURNAL_EVENTS`
complete events), quarantines a partial final tail via `run_journal.recover_partial_tail` (write-once quarantine,
truncate to the last complete line) and re-reads, then verifies the chain is contiguous from sequence 1, every
`previous_digest` links, and every event shares one `run_id` equal to the run directory name. A partial line that is
not the final line is a chain break. if after quarantine the journal still has a partial tail or chain errors, it fails
closed.

The callback then selects the highest-sequence `run.snapshot.checkpointed` event. It is authoritative and is
validated via `validate_checkpoint` (payload validation first, then open-fd hardening). Any invalid latest checkpoint
fails closed: no earlier-checkpoint selection and no legacy fallback for activated-journal runs. The tail must be
covered (see Coverage semantics). an uncovered tail fails closed. If `run.json` is missing or unparseable, the
callback renames a corrupt `run.json` to `run.json.corrupt-<uuid>` (best-effort. on `OSError`, fail closed) and writes
the checkpoint bytes to `run.json` via `localio.write_text_atomic`. This intermediate repair is byte-identical to the
checkpoint. The callback never repairs over a parseable `run.json`, which is left untouched. After successful
validation and any intermediate repair, `_finish_claimed_recovery` terminalizes the receipt as a stale-lock failure,
so the final `run.json` is expected to differ from the checkpoint in terminal fields while preserving `task`,
`roster`, `cwd`, `lock_workspace`, and the restored failure attribution inputs. On the activated-journal callback
path only, slice 5 copies the lock owner's non-secret recovery inputs into the private failure object before deleting
the claim:
`failure.lock_workspace` carries `owner["_lock_workspace"]` when present, and
`failure.lock_acquired_at` carries `owner["acquired_at"]` when present. These values let Doctor replay
`_recover_run_artifact`'s `setdefault` behavior after the lock directory is gone. Legacy no-journal terminalization
does not add these fields and keeps its existing byte contract. The enclosing
checkpoint event's envelope `run_id` must equal the single verified journal `run_id` and `run_dir.name`. a mismatch
fails closed.

### Open-fd hardening and fail-closed cases

`validate_checkpoint` opens exactly one file descriptor on the referenced path after payload validation. The fd must
not be a symlink (`O_NOFOLLOW` via `run_journal._open_nofollow`), must be a regular file, and must have a link count
of exactly one. The declared `byte_size` must match the actual file size. The file's SHA-256 must match the declared
`sha256` and the filename. The bytes must be valid UTF-8, parse as a JSON object, and equal the `aboyeur._write_json`
canonical encoding of that object (sorted keys, two-space indent, one trailing newline), the writer-byte equality
check, not `run_events.canonical_bytes`. Any failure raises `CheckpointError` with a bounded category.

Recovery fails closed (exit 2, no `run.json` mutation beyond preserving a corrupt file) when: the journal carries an
unknown schema or event type not in `run_events.EVENT_TYPES` (after partial-tail quarantine). the chain breaks
(sequence gap, duplicate, `previous_digest` mismatch, mixed `run_id`). the latest checkpoint event is missing,
dangling (file absent), or invalid. the enclosing checkpoint event's envelope `run_id` mismatches the journal `run_id`
or `run_dir.name`. the journal tail is not covered by the latest checkpoint. or the corrupt-`run.json` rename raises
`OSError`. There is no legacy fallback for activated-journal runs. the legacy `_recover_run_artifact` path is reached
only when the run has no `events/lifecycle.jsonl`.

## Coverage semantics

The latest checkpoint event at sequence N covers either itself as the tail or exactly one immediately following event
whose `event_type` equals the checkpoint's `paired_event_type` and whose derived status equals the status in the
checkpointed `run.json` bytes. When `paired_event_type` is null, the checkpoint covers only itself as the tail.
Anything else is uncovered-tail FAIL.

Through the normal write path, a mapped-transition write appends the checkpoint at N with `paired_event_type` set to
the mapped status event type, and the paired status event at N+1. An unmapped or same-status write appends only the
checkpoint at N with `paired_event_type` null. So the journal tail is always either a checkpoint event whose
`paired_event_type` is null (covered, tail is N), or a single status event immediately preceded by its covering
checkpoint whose `paired_event_type` names that status event type and whose derived status matches the checkpointed
bytes (covered, tail is N+1).

Uncovered-tail FAIL fires when the events after the latest valid checkpoint are not exactly the paired status event
of that checkpoint: a status event whose preceding checkpoint never landed, two status events after the last
checkpoint, a checkpoint at N followed by a status event at N+1 whose `event_type` is not the checkpoint's
`paired_event_type`, or a status event at N+1 whose derived status does not equal the status in the checkpointed
`run.json` bytes. Through the normal path this cannot fire. it is defense in depth against direct journal mutation.

## Crash matrix

| Crash window | State after crash | Recovery action |
| --- | --- | --- |
| Before checkpoint temp fsync | no new checkpoint file or event, `run.json` unchanged | Validate the previously referenced checkpoint and covered tail. If none exists on the first activated write, fail closed (exit 2), except the existing already-terminal stale-recovery cleanup. |
| After temp fsync, before hard-link publish | no new final file or event, orphan temp, `run.json` unchanged | Validate the previously referenced checkpoint and covered tail. The orphan temp is private and ignored. If no prior checkpoint exists, fail closed. |
| After hard-link publish, before checkpoint event fsync | new final file durable but unreferenced, `run.json` unchanged | Ignore the orphan checkpoint file and validate the previously referenced checkpoint and covered tail. If no prior checkpoint exists, fail closed. |
| After checkpoint event fsync, before status event fsync | checkpoint file and event durable, no status event, `run.json` unchanged | Recovery selects the checkpoint (latest valid). Tail is the checkpoint event, covered. Restore checkpoint bytes as the intermediate receipt, then existing stale-lock terminalization writes the final failure receipt. |
| After status event fsync, before `run.json` replace | checkpoint file, event, and status event durable. `run.json` unchanged | Recovery selects the checkpoint (N), tail is the paired status event at N+1, covered. Restore checkpoint bytes as the intermediate receipt, then terminalize. the status event and checkpoint agree (same write). |
| During or after `run.json` replace | `run.json` is the old or new bytes (atomic replace is all-or-nothing). after shadow comparison, fully recorded | If `run.json` is parseable, validate the journal and checkpoint without repair, then terminalize. If missing or unparseable, restore checkpoint bytes as the intermediate receipt, then terminalize. After shadow comparison the gate is current. before it, the slice-4 gate closes via `journal-ahead-of-evidence`, correct. |
| Checkpoint hard-link collision, mismatched bytes | `EEXIST` on `os.link`. existing final file has different bytes | Writer fails closed: `CheckpointError` propagates and the live write does not advance. Doctor reports the collision. Recovery treats the mismatched file as an invalid latest checkpoint and fails closed. |

## Doctor check

`doctor.core_station_checks` gains one scoped check, `runs: recovery checkpoints`, using the core `OK` / `WARN` /
`FAIL` / `MANUAL` vocabulary. Doctor is fully read-only: it never quarantines, repairs, or mutates the journal,
checkpoint files, or `run.json`. It scans at most 50 immediate non-symlink run directories under
`<target>/.brigade/runs/` in newest-first order with no recursive walk, and reads only `run.json`,
`events/lifecycle.jsonl`, and the latest referenced checkpoint file. The journal goes through
`read_journal_bounded` (no more than `MAX_JOURNAL_BYTES` 8 MiB or `MAX_JOURNAL_EVENTS` 512 complete events). The
checkpoint goes through `validate_checkpoint`, whose fd-first size check enforces `MAX_CHECKPOINT_BYTES` 16 MiB before
reading the body. A larger journal or checkpoint is `FAIL` with `bound exceeded` and is not parsed further. No
wall-clock timeout. A partial final journal line is `FAIL` without quarantine. Doctor never calls
`recover_partial_tail`. The omitted run count (runs beyond the 50 cap, plus legacy and no-journal runs out of scope) is
reported inside the aggregate check detail, not as a separate status line.

Per-run verdict for activated runs: `OK` when `run.json` parses, the chain verifies, and the latest checkpoint
validates with bytes equal to the current `run.json` writer bytes. A parseable recovered receipt is also `OK` only
when Doctor can reconstruct it exactly from the checkpoint object using `_recover_run_artifact`'s transformation.
It first replays `setdefault("cwd")` and `setdefault("lock_workspace")` from the candidate's optional
`failure.lock_workspace`, and `setdefault("started_at")` from its optional `failure.lock_acquired_at`. It then
derives `prior_status` and optional `seat` or `seats` attribution from the checkpoint, takes the candidate's integer
`failure.owner_pid` and valid ISO `failure.recovered_at`, requires that timestamp to equal `status_started_at` and
`finished_at`, and derives `error` and `failure.detail` as
`run owner process <owner_pid> is no longer active`, set the fixed stale-recovery phase and
`owner-process-exited` kind, stamp the run receipt schema, and require exact object equality. This cross-check covers
all terminal fields (`error`, `detail`, `owner_pid`, `prior_status`, timestamps, and attribution) and all untouched
checkpoint fields. The journal and latest checkpoint must still validate. Any other parseable checkpoint mismatch is
`FAIL`. `WARN` applies when `run.json` is
missing or unparseable but a valid latest checkpoint exists (repairable). `FAIL` also covers a malformed chain,
partial tail, dangling or invalid latest checkpoint, digest/size/content mismatch, envelope `run_id` mismatch,
uncovered tail, or any bound exceeded. `OK` (live owner) applies when `run_lock_state` returns `live` and the owner pid
is live, with no mutation. Legacy and no-journal runs are omitted (counted in the aggregate). The check emits one
aggregate three-field `CheckResult` whose detail summarizes counts (ok / warn / fail / omitted) and lists up to 8
failing run names. `--full` lists every run.

## CLI exit codes

`brigade runs recover` exit codes are unchanged: 0 for success (repaired from checkpoint, or already terminal, or
legacy repair succeeded for a no-journal run). 2 for invalid run, live owner, or unsafe (any fail-closed case above,
including a checkpoint collision, chain break, invalid latest checkpoint, envelope `run_id` mismatch, uncovered tail,
or a `before_terminalize` callback failure that retained the claim). `brigade runs watch` and `brigade runs show`
surface stale-lock recovery exactly as today. the checkpoint repair is transparent to them because the existing
terminalizer receives the same complete receipt it would have read from a healthy pre-crash write. This preserves the
`test_runs_show_surfaces_stale_lock_recovery_and_returns_nonzero` and
`test_runs_watch_surfaces_stale_lock_recovery_and_returns_nonzero` contracts.

## API

The new `run_checkpoint` module exposes constants (`CHECKPOINT_EVENT_TYPE`, `CHECKPOINT_MEDIA_TYPE`,
`CHECKPOINT_PRIVACY_CLASS`, `CHECKPOINT_DIR_NAME`, `MAX_CHECKPOINT_BYTES`, `MAX_JOURNAL_BYTES`,
`MAX_JOURNAL_EVENTS`). `CheckpointError(RuntimeError)` (bounded, carries a category). `checkpoint_dir(run_dir)`.
`checkpoint_path(run_dir, sha256)`. `write_checkpoint(run_dir, run_json_bytes, paired_event_type)` (ensure activation
via `prepare_lifecycle_journal`, publish the checkpoint file crash-safely, append the checkpoint event. returns the
appended or replayed event. raises `CheckpointError` on any failure). `validate_checkpoint(run_dir, event)`
(validate the payload, then open and verify the referenced file. returns the checkpoint bytes. raises `CheckpointError`
on any validation failure). `latest_checkpoint_event(report)` (highest-sequence `run.snapshot.checkpointed` event
from a `JournalReport`, or `None`). and `recover_from_checkpoint(run_dir)` (verify the journal, select and validate
the latest checkpoint for every activated run, and, only when `run.json` is missing or unparseable, preserve the
corrupt file and restore checkpoint bytes as the intermediate receipt. returns `repaired` or `validated`, or raises
`CheckpointError` mapped by the caller to exit 2). `run_events.EVENT_TYPES` gains the
`run.snapshot.checkpointed` row with the closed payload key set.
`run_projector.project_run_snapshot` gains the status-neutral handling for the checkpoint event type and
`PROJECTOR_VERSION` becomes 2. `run_lifecycle` gains `prepare_lifecycle_journal`. `run_journal` gains
`read_journal_bounded`, used by recovery and doctor: it opens the journal no-follow (`_open_nofollow`), `fstat`s the fd
before any whole-file allocation, refuses a size above `MAX_JOURNAL_BYTES` (8 MiB) before allocation, reads in bounded
chunks, and stops or fails at the first complete event whose sequence is 513. a journal over the byte or event bound is
reported as `bound exceeded` and not parsed further. Existing `read_journal` stays compatible. `runguard` gains
`run_lock_state` and the optional `before_terminalize` parameter on `recover_stale_run`. No other public API changes.

## Invariants

1. Write-ahead: the checkpoint file and event are durable before the atomic `run.json` replace for every activated-journal write.
2. Fail-closed hook: a checkpoint file or event failure propagates `CheckpointError` and stops the live write before the lifecycle append and the `run.json` replace.
3. Content addressing: the checkpoint filename and event `sha256` are the SHA-256 of the stored bytes. a collision requires byte equality or fails closed.
4. Crash-safe publish: the final checkpoint path is mutated only by an atomic `os.link` from a private fsynced temp. an existing final file is accepted only after exact byte and digest equality. No direct `O_EXCL` partial-write poison reaches the final path.
5. Platform-guarded directory fsync: the checkpoint directory is fsynced on POSIX and skipped on non-POSIX, mirroring `aboyeur._supports_directory_fsync`. The atomic no-replace `os.link` publication is required and fails closed where unsupported.
6. Writer authority: the legacy writer stays authoritative for live runs. the checkpoint stores the writer's own encoded bytes.
7. Recovery authority: `recover_stale_run` owns the stale claim and rejects every matching live owner (current process or foreign pid) with no mutation. only the claim winner repairs.
8. Callback-or-restore: a `before_terminalize` callback runs for both a visible stale lock and an already-renamed pending claim. if it raises for any reason, `_restore_claimed_lock` runs and a bounded `RunLockError` names the retained path. A pending activated-journal claim without a callback is retained for explicit recovery.
9. Exact intermediate restore: when `run.json` is missing or unparseable, recovery restores byte-identical checkpoint bytes before existing stale-lock terminalization writes the final failure receipt. it never repairs over a parseable `run.json`.
10. Recoverable provenance: activated-journal callback terminalization copies optional lock workspace and acquisition time into the private failure object before deleting the claim, so Doctor can reconstruct the final receipt exactly. legacy no-journal terminalization remains byte-compatible.
11. No repair journaling: recovery appends no journal events during repair.
12. Fail closed: unknown schema or type, chain break, missing or invalid latest checkpoint, envelope `run_id` mismatch, uncovered tail, and bound excess all fail closed with exit 2.
13. Legacy preserved: no-journal recovery is unchanged and is the only legacy fallback.
14. Privacy: checkpoint payloads carry only relative path, hex digest, fixed media type, byte size, fixed privacy class, and paired event type. No `run_id` and no `run.json` field values enter the event payload.
15. Bounded: doctor and recovery cap checkpoint bytes at 16 MiB and journal bytes at 8 MiB or 512 complete events.
16. Projector version door: `PROJECTOR_VERSION` is 2. version-1 shadow evidence is stale.

## Blast radius

Exact blast-radius names from the code index. The checkpoint hook sits inside `aboyeur._write_json`, whose indexed production
callers are `write_sidecar_revision`, `record_artifact_collection`, `record_run_termination`, `record_dispatch_stage`,
`record_result_processing`, `record_run_start`, `run`, and `run_resume._resume_locked`.
`record_lifecycle_transition`'s only indexed production caller is `aboyeur._write_json`. `_recover_run_artifact`'s is
`_finish_claimed_recovery`. `project_run_snapshot` and `core_station_checks` have no indexed production callers (they are
reached through CLI dispatch and, for the projector, through `run_shadow`). slice 5 adds the recovery and doctor callers,
both read-only. `run_lock_state` and the `before_terminalize` callback path on `recover_stale_run` are new runguard entry
points reached by `runs_cmd.recover` and `doctor`.

Affected tests (names this slice must keep green or extend): `test_runs_recover_*` in `tests/test_runs_cmd.py`
(including `test_runs_recover_reconstructs_missing_run_json_from_matching_dead_lock` and
`test_runs_recover_preserves_and_reconstructs_corrupt_run_json`, which exercise the legacy fallback that stays unchanged
for no-journal runs), `test_runs_show_surfaces_stale_lock_recovery_and_returns_nonzero`,
`test_runs_watch_surfaces_stale_lock_recovery_and_returns_nonzero`,
`test_unmapped_status_writes_run_json_without_journal_event` (updated to account for the checkpoint event without
weakening its no-status-event assertion), and `test_empty_events_no_journal_preserves_base_status_and_deep_copies` (the
status-neutral checkpoint rule must keep the empty-sequence projection unchanged). The slice-2 activation tests in
`tests/test_run_lifecycle.py` that assert implicit activation inside the first mapped-status write are updated to the
explicit prepare step. The slice-4 shadow tests that assert a version-1 artifact reads as ready are updated to assert
staleness under version 2.

## Compatibility

- Runtime behavior for readers is unchanged: `run.json` bytes, journal bytes for status transitions, CLI output, and
  `runs watch`, `show`, `steer`, `interrupt`, `recover`, and `resume` behavior are exactly as before for no-journal
  runs. For activated-journal runs, the only new on-disk artifacts are the `events/recovery-checkpoints/<sha256>.json`
  files and one extra `run.snapshot.checkpointed` event per `run.json` write, both additive and private.
- A legacy run directory without `events/lifecycle.jsonl` is untouched: no checkpoint is created, recovery uses the
  legacy path, and doctor omits it. The four reserved fields from slices 3 and 4 (`projector_version`,
  `journal_present`, `journal_last_sequence`, `journal_last_event_digest`) are still not written to `run.json`. the
  checkpoint stores `run.json` bytes, not derived fields.
- A new run directory still exposes a byte-compatible `run.json` to the previous Brigade release. The checkpoint event
  is a new event type in the existing `brigade.run_event.v1` registry.
- One-way journal-format door: old `run.json` readers remain compatible (the `run.json` byte contract is unchanged), but
  old Brigade releases that predate slice 5 do not recognize `run.snapshot.checkpointed`. their `run_journal` validation,
  projector, and recovery reject an activated journal carrying that event as an unknown type, so a slice-5 run read by an
  old release fails closed rather than silently misprojecting. Rolling an activated-journal run back to an old release is
  unsupported. rolling forward again is fine because the journal is append-only and the old release never mutated it.

## Non-goals and deferrals

- Authoritative writer cutover: treating the journal as the live writer for new runs is slice 6 (issue #568 step 6),
  gated per run by the slice-4 readiness gate plus the issue parity measurement criteria. The checkpoint plane landed
  here is the recovery substrate slice 6 builds on.
- GC, retention, or compaction of checkpoint files. checkpoint pruning, deduplication across runs. and a
  `brigade runs gc` command. Checkpoints follow whole-run retention in this slice. the rest is post-slice-6 operator
  tooling.
- Evidence reset tooling after a mapping fix (an operator action, slice 6).
- Journaling or projecting the unmapped statuses (`dry-run`, `incomplete`, `artifact-collection`) and event enrichment.
  the checkpoint covers their bytes and the projector still derives the last mapped status.
- Checkpointing `runguard._recover_run_artifact` repair writes (they are the recovery plane, not live transitions).
- Any new top-level harness, depth, or include manifest.
