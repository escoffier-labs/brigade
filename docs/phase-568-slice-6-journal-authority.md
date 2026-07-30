# Issue #568 Slice 6: Journal Authority

Reviewed design specification for the approved slice 6 boundary of GitHub issue #568
(https://github.com/escoffier-labs/brigade/issues/568). Slices 1 through 5 landed the
append-only journal kernel (`run_journal`, `run_events`), opt-in lifecycle journaling
(`run_lifecycle`, wired into `aboyeur._write_json`), the pure run snapshot projector
(`run_projector`), shadow comparison plus the readiness gate (`run_shadow`), and the
recovery checkpoint substrate with `runs recover` and `doctor` integration
(`run_checkpoint`). See `docs/phase-568-slice-3-run-projector.md`,
`docs/phase-568-slice-4-shadow-comparison.md`, and
`docs/phase-568-slice-5-recovery-checkpoints.md`.

Slice 6 cuts the journal over to the authoritative writer for newly enrolled runs. `run.json`
becomes a compatibility projection: the journal owns lifecycle status and the journal cursor,
and the checkpoint base owns the documented preserved fields. The legacy writer no longer
decides `run.json` status for an authoritative run. The cutover is per-run, gated by the
slice-4 readiness gate plus a 1,000-run measurement that this slice ships but does not
interpret. Legacy and existing runs never migrate.

Source contracts, all standard-library only: `run_events` (`EVENT_TYPES`, `canonical_bytes`,
`request_digest`, `validate_event`, `_HEX64`, `MAX_DIAGNOSTIC_LEN` 240, `MAX_LINE_BYTES`
16384, `MAX_IDEMPOTENCY_KEY_LEN` 128, `MAX_PAYLOAD_STR_LEN` 512, `CanonicalizationError`).
`run_journal` (`RunEvent`, `JournalReport`, `read_journal`, `read_journal_bounded`,
`append_event`, `ensure_journal`, `recover_partial_tail`, `_open_nofollow`, `_mkdir_private`,
`_DIR_MODE` 0o700, `_FILE_MODE` 0o600). `run_lifecycle` (`STATUS_EVENT_TYPE`,
`record_lifecycle_transition`, `prepare_lifecycle_journal`, `LifecycleJournalError`, the
activation model, same-status and unmapped-status skip rules). `run_projector`
(`PROJECTOR_VERSION`, `EVENT_STATUS`, `_PAYLOAD_STATUS_RULES`, `project_run_snapshot`,
`encode_snapshot_bytes`, `RunProjection`, the `ProjectionError` subclasses, `DERIVED_FIELDS`,
`PRESERVED_FIELDS`, `OWNED_FIELDS`). `run_shadow` (`record_shadow_comparison`,
`check_projection_readiness`, `shadow_artifact_path`, `ReadinessReport`, the
fail-open-writer / fail-closed-gate split, the outcome and reason vocabularies).
`run_checkpoint` (`CHECKPOINT_EVENT_TYPE`, `CHECKPOINT_MEDIA_TYPE`,
`CHECKPOINT_PRIVACY_CLASS`, `CHECKPOINT_DIR_NAME`, `MAX_CHECKPOINT_BYTES`,
`MAX_JOURNAL_BYTES`, `MAX_JOURNAL_EVENTS`, `CheckpointError`, `checkpoint_dir`,
`checkpoint_path`, `publish_checkpoint_file`, `validate_checkpoint`, `write_checkpoint`,
`latest_checkpoint_event`, `recover_from_checkpoint`, `_parse_checkpoint_object`,
`_verify_coverage`, `_paired_event_derived_status`). `aboyeur._write_json`,
`aboyeur._supports_directory_fsync`. `localio` (`write_text_atomic`, `write_text_exclusive`,
`write_json`, `file_sha256`, `utc_now_iso`). `runguard` (`run_lock`, `recover_stale_run`,
`run_lock_state`, `is_active_run_owner`, `resolve_run_lock_workspace`, `RunLockError`).
`runs_cmd.recover` and `runs_cmd._recover_from_checkpoint`. `doctor.core_station_checks`
(`OK`, `WARN`, `FAIL`, `MANUAL`, `CheckResult`).

## Boundary

Slice 6 adds no runtime dependency, daemon, broker, or top-level harness. It changes the
existing lifecycle, checkpoint, projector, shadow, recovery, doctor, and run-writer modules
and adds one standard-library measurement script:

1. The `aboyeur._write_json` hook for `run.json` dict payloads. The hook already calls
   `prepare_lifecycle_journal`, `write_checkpoint`, `record_lifecycle_transition`,
   `localio.write_text_atomic`, then `run_shadow.record_shadow_comparison`. Slice 6
   reorders and extends it so an authoritative run writes the projected snapshot instead of
   the legacy body, and a not-yet-authoritative enrolled run keeps writing the legacy body
   while it builds parity evidence.
2. `run_checkpoint.write_checkpoint` and `run_checkpoint.validate_checkpoint`. For an
   authority-requested run, the checkpoint changes from the exact `run.json` bytes (slice 5)
   to a canonical projection base. The base retains `status` as the projector seed and strips
   only the four journal-metadata fields that would make the checkpoint self-referential:
   `projector_version`, `journal_present`, `journal_last_sequence`, and
   `journal_last_event_digest`. The checkpoint event payload gains an optional `body_kind`
   field distinguishing an existing slice-5 event with no `body_kind` (`legacy-full`) from a
   slice-6 `base-stripped` event. Coverage and recovery adjust to the projection base.
3. `run_projector` and `run_shadow`. `PROJECTOR_VERSION` bumps from 2 to 3. The event-to-status
   map gains `dry-run` through `run.completed`, `incomplete` through `run.failed`, and
   `artifact-collection` through a new `run.artifact_collection.started` event type.
   `record_shadow_comparison` preserves a stale v2 artifact by quarantine and starts a fresh
   v3 artifact. `check_projection_readiness` never resets current-version mismatches or
   errors.

A new opt-in flag `BRIGADE_RUN_JOURNAL_AUTHORITY` enrolls new runs only and persists
`run_journal_authority_requested: true` through `aboyeur._run_payload` and
`aboyeur.record_run_start`, alongside the
slice-2 `lifecycle_journal_requested` bootstrap. The enrollment controls are separate, but
the resulting states are not independent: authority requires lifecycle journaling.
`BRIGADE_RUN_JOURNAL_AUTHORITY=1` therefore sets both request fields on a new run. A
lifecycle-only run carries only `lifecycle_journal_requested`. No valid run carries only
`run_journal_authority_requested`. Legacy and existing runs never gain the authority request
and never migrate.

### Coverage boundary, decided

- The authoritative hook path fires for every `aboyeur._write_json` call whose target is
  named `run.json`, whose payload is a dict, and whose run carries
  `run_journal_authority_requested: true` with an activated journal. This covers the aboyeur
  writers and `run_resume._resume_locked`. `runguard._recover_run_artifact` repair writes
  through `localio.write_json` directly and are not projected. They remain the recovery plane.
- The not-yet-authoritative path (enrolled, gate not ready) writes the full legacy candidate
  with `status` to
  `run.json` exactly as slice 5 does, including the checkpoint and lifecycle append, so
  parity evidence accumulates.
- A true legacy run with neither request creates no checkpoint or projection and observes
  byte-identical behavior to slice 5. A run that carries `lifecycle_journal_requested` but not
  `run_journal_authority_requested` stays in slice-5 shadow mode forever: the legacy writer
  stays authoritative, the checkpoint stores the slice-5 full body, and the projector is never
  the live writer.

## Enrollment and authority state

A run has exactly one authority state at any time:

- `legacy`: never requested authority. The slice-5 path runs unchanged.
- `authority-requested`: `run_journal_authority_requested: true` is durable in `run.json`, the
  journal is activated, but no authoritative snapshot has committed yet. The writer is
  permitted to write the legacy body while the gate is not ready.
- `authoritative`: at least one projected snapshot has committed to `run.json` for this run.
  From that point the writer must project. A chain, evidence, readiness, or projection failure
  stops the write and cannot downgrade back to the legacy body.

The durable authority fact is the projected `run.json` itself plus the journal. Once a
projected snapshot has replaced `run.json`, the run is authoritative for life. The state is
not stored as a separate flag. It is derived from whether the current `run.json` carries the
four journal-metadata fields with `projector_version` equal to the current
`PROJECTOR_VERSION` and whether its saved sequence/digest matches that event in a verified
journal prefix. Later journal events do not demote an authoritative run. `status` exists on
legacy and projected snapshots, so it does not distinguish authority. The live writer
resolves this state before appending the next checkpoint or lifecycle event. Recovery and
doctor use the same prefix rule.

## Checkpoint body change

Slice 5 checkpointed the exact `run.json` bytes the legacy writer was about to commit. An
authoritative snapshot adds the digest and sequence of the journal tail. Checkpointing those
fields would be self-referential because the checkpoint event itself changes that tail.

Slice 6 therefore checkpoints a canonical projection base. It contains `status` plus every
present `run_projector.PRESERVED_FIELDS` key, including the new
`run_journal_authority_requested` field. It omits `projector_version`, `journal_present`,
`journal_last_sequence`, and `journal_last_event_digest`. The base uses the house canonical
form (`json.dumps(..., indent=2, sort_keys=True) + "\n"`, UTF-8). `status` remains in the
base because the pure projector accepts it as the compatibility seed and because a
not-yet-authoritative fallback must still write a complete legacy snapshot. For every
non-empty mapped journal, verified replay determines the output status. Removing the four
journal-metadata fields breaks the cycle while keeping the slice-5 status coverage check
available.

The checkpoint event payload keeps the closed slice-5 key set and adds one key:

| Key | Type | Value |
| --- | --- | --- |
| `path` | str | relative checkpoint path `events/recovery-checkpoints/<sha256>.json` |
| `sha256` | str | lowercase 64-char hex SHA-256 of the projection base |
| `media_type` | str | `application/vnd.brigade.run+json` |
| `byte_size` | int | length of the projection base in bytes |
| `privacy_class` | str | `private` |
| `paired_event_type` | str or null | exact mapped lifecycle event type expected immediately after the checkpoint, or null for same-status and unmapped writes |
| `body_kind` | str | optional. `base-stripped` for slice-6 authority checkpoints. Absence means slice-5 `legacy-full` |

`body_kind` is the only new allowlisted payload key. It is optional so the already-merged
slice-5 checkpoint events remain valid under `brigade.run_event.v1`. `validate_checkpoint`
interprets an absent `body_kind` as `legacy-full`. An explicit value must be
`base-stripped`. A `base-stripped` body must contain
`run_journal_authority_requested: true` and `lifecycle_journal_requested: true`. This prevents
a forged stripped body from entering the lifecycle-only recovery path. Slice 6 does not
rewrite existing journal events. A lifecycle-only run keeps writing slice-5 events without
`body_kind`. An authority-requested run appends a `base-stripped` checkpoint on its next
write, after which recovery uses that latest projection base.

The checkpoint idempotency key remains byte-for-byte slice-5 form for an absent
`body_kind`: `checkpoint:<sha256>:<paired-event-type-or-none>`. A `base-stripped` checkpoint
uses `checkpoint:base-stripped:<sha256>:<paired-event-type-or-none>`, with the paired-event
tail bounded to `MAX_IDEMPOTENCY_KEY_LEN`. A valid run cannot switch from `legacy-full` to
`base-stripped`: existing runs never migrate, and a stripped body requires both durable
request fields.

## Projector changes

`run_projector.PROJECTOR_VERSION` becomes 3. The event-to-status mapping widens so the
projector can derive every current `run.json` status, removing the slice-3 status-lag gap for
the three previously unmapped statuses.

`run_events.EVENT_TYPES` gains one new event type:

| Event type | Payload keys |
| --- | --- |
| `run.artifact_collection.started` | `detail` |

`run_lifecycle.STATUS_EVENT_TYPE` gains three rows:

| run.json status | Event type |
| --- | --- |
| `dry-run` | `run.completed` |
| `incomplete` | `run.failed` |
| `artifact-collection` | `run.artifact_collection.started` |

`run_projector.EVENT_STATUS` gains `run.artifact_collection.started` -> `artifact-collection`.
`run_projector._PAYLOAD_STATUS_RULES` widens two rows:

| Event type | Allowed payload `status` | Derived status |
| --- | --- | --- |
| `run.completed` | `{"ok", "dry-run"}` | payload status used directly |
| `run.failed` | `{"failed", "timeout", "incomplete"}` | payload status used directly |

`run.created`, `run.interrupted`, and the static rows are unchanged.
`run_journal_authority_requested` joins `PRESERVED_FIELDS`, so the ownership inventory grows
by one optional field. `DERIVED_FIELDS` remains unchanged. `encode_snapshot_bytes` is
unchanged. The projector remains pure: no I/O, no clock, no environment.

The slice-3 status-lag note is now resolved for `dry-run`, `incomplete`, and
`artifact-collection`: the journal records them and the projector derives them. A
`run.completed` event with `payload.status == "dry-run"` derives `dry-run`. A `run.failed`
event with `payload.status == "incomplete"` derives `incomplete`. A
`run.artifact_collection.started` event derives `artifact-collection`. Shadow comparison no
longer classifies these three as `lag`. They are `match` when parity holds.

## Shadow comparison and the readiness gate

`run_shadow.record_shadow_comparison` keeps its slice-4 contract for legacy and
lifecycle-journal-only runs. For an authority-requested run, the shadow candidate is built
the same way (the legacy candidate plus the four journal-metadata fields pinned from the
journal tail)
and the projector is run the same way. The comparison still produces `match`, `lag`,
`mismatch`, or `error` and still writes the cumulative artifact.

Comparison and readiness read the journal through `run_journal.read_journal_bounded` with
the slice-5 `MAX_JOURNAL_BYTES` and `MAX_JOURNAL_EVENTS` limits. A bound failure records
bounded error evidence and closes the gate as `journal-unreadable`. It never reaches the
projector.

The one behavioral change is projector-version staleness. `check_projection_readiness`
currently treats a `projector_version` mismatch as `evidence-schema-mismatch`, which closes
the gate permanently for the run. Slice 6 splits version mismatch into two cases:

- Stale-version mismatch (artifact `projector_version` is 2, current is 3):
  `record_shadow_comparison` atomically renames the complete v2 artifact to a private
  `.stale-projector-v2-<timestamp>` sibling before writing a fresh v3 artifact. The old
  mismatches and errors remain available in the preserved file but do not poison a different
  projector version. The verified v2 last sequence/digest is used only as the gap baseline
  for the first v3 comparison, so a normal checkpoint-plus-status advance does not create a
  false gap and a larger unexplained advance still records a fresh `comparison-gap` error.
  No v2 counter or recent record carries into v3. Until the fresh v3 comparison lands, the
  gate is not ready: it reports `evidence-projector-version-stale` before quarantine, or
  `no-evidence` if a crash lands after the rename and before the fresh artifact write.
- Current-version mismatch or error (artifact `projector_version` is 3 with nonzero
  `mismatches` or `errors`): the gate never resets those counters. It reports
  `mismatch-recorded` or `error-recorded` exactly as slice 4 does, and the run never becomes
  authoritative.

A new reason constant `REASON_EVIDENCE_PROJECTOR_VERSION_STALE` joins the closed reason set.
The existing `REASON_EVIDENCE_SCHEMA_MISMATCH` now fires only for schema, schema_version, or
run_id disagreement, not for a projector-version mismatch. A version-2 artifact read by the
version-3 gate therefore never permanently closes a run on version grounds alone. The
quarantined artifact preserves the old evidence. The active v3 artifact starts with v3
counts.

## Write ordering

For an `aboyeur._write_json` call whose target is `run.json` and whose payload is a dict, the
hook resolves the authority state from the pre-append `run.json` and verified journal,
computes the legacy candidate and projection base once, then follows the ordered path below.

1. Candidates: deep-copy the incoming payload and remove any stale
   `projector_version`, `journal_present`, `journal_last_sequence`, and
   `journal_last_event_digest` keys. Encode that complete legacy candidate, including
   `status`, with the house canonical form. It is the fallback `run.json` body and the shadow
   comparison input. The same object is the `base-stripped` checkpoint body.
2. Journal preparation: `run_lifecycle.prepare_lifecycle_journal` ensures the empty journal
   exists under the active run lock (no-op when already activated, establishes activation
   when the request is pending under the held lock). For an authority-requested run the
   request is `run_journal_authority_requested`. For a lifecycle-only run it is
   `lifecycle_journal_requested`.
3. Checkpoint fsync: `run_checkpoint.write_checkpoint` publishes the projection base to
   `events/recovery-checkpoints/<sha256>.json` via the crash-safe temp-and-hard-link pattern
   and appends one `run.snapshot.checkpointed` event with `body_kind: base-stripped`. The
   checkpoint file and event are durable before any lifecycle append or `run.json` replace.
   A `CheckpointError` here propagates out of `_write_json` and the write does not advance.
4. Mapped lifecycle append/fsync: `run_lifecycle.record_lifecycle_transition` appends the
   mapped status event under the active run lock, with the existing skip rules. Unmapped and
   same-status writes append no status event. With the slice-6 mapping, `dry-run`,
   `incomplete`, and `artifact-collection` are now mapped and append events.
5. Parity evidence: `run_shadow.record_shadow_comparison` runs after the lifecycle append. It
   builds the shadow candidate from the complete legacy candidate plus the four
   journal-metadata fields pinned from
   the journal tail, runs the projector, compares canonical bytes, and writes the cumulative
   artifact. A shadow failure is observational and fail-open for the writer, exactly as in
   slice 4.
6. Readiness veto: `run_shadow.check_projection_readiness` reads the artifact and the journal
   tail. If the run is not authority-requested, the veto is skipped and the writer writes the
   legacy body (slice 5). If the run is authority-requested and the gate is not ready (no
   comparisons yet, stale v2 evidence, a current `lag`, or any recorded mismatch or error),
   the writer writes the legacy body. This is the not-yet-authoritative fallback. If the run
   is already authoritative, the gate must be ready. A not-ready gate stops the write and
   cannot downgrade.
7. Projection: `run_projector.project_run_snapshot` runs against the projection base
   plus the journal events from a bounded, chain-valid read, with `journal_present=True`. The projected snapshot
   carries the present preserved fields and authority request from the base, derives status
   from the mapped journal, and writes the four journal-metadata fields from the tail.
   A `ProjectionError` here stops the write and cannot downgrade.
8. Atomic `run.json` replacement: `localio.write_text_atomic` replaces `run.json` with the
   projected bytes. After the replace, the run is authoritative.

The invariant: after step 3 the stripped base and its checkpoint event are durable. After
step 4 the journal owns the status and the cursor. After step 5 the parity evidence is
durable. Step 6 is the veto. After step 8 `run.json` is the projected compatibility snapshot.
A crash between 3 and 8 leaves a durable base plus a journal tail that may or may not cover
it. Recovery projects from both. A crash after 8 leaves `run.json` matching the projection.

A not-yet-authoritative run may write the legacy body when the gate is not ready. After any
authoritative snapshot commits, a chain, evidence, readiness, or projection failure stops
the write and cannot downgrade. The run then fails closed until recovery projects a fresh
snapshot from the verified base plus the covered journal tail.

The authority branch translates a readiness veto or `ProjectionError` into a bounded
`LifecycleJournalError`. Existing `record_run_start`, transition, artifact-collection, and
termination callers already translate that type to `RetainRunLockError`. Slice 6 adds the
same adapter around the two `run_resume._resume_locked` receipt writes. The lock therefore
remains available for stale recovery. Raw, unbounded exception text never enters the receipt
or terminal output.

### First-write parity

Before the first comparison, the readiness gate is not ready. Step 5 records the comparison
before step 6 consults the gate. If that first comparison is a current-version `match`, the
gate is ready in the same write and the writer may project the first committed `run.json`.
No special bypass of `check_projection_readiness` exists. A missing or unreadable evidence
artifact remains not ready.

A first-write `lag`, `mismatch`, or `error` does not authorize the first projection. The run
stays `authority-requested` and keeps writing legacy bodies. A later `match` may authorize the
next projection after a lag clears. A recorded current-version mismatch or error never
clears automatically, so a later match cannot authorize that run. First-write parity is a
one-time bridge for the empty-artifact case only. It never overrides recorded failure
evidence.

## Recovery

`runs_cmd.recover` is extended for authority-requested runs. The slice-5 activated-journal
recovery path is unchanged for lifecycle-journal-only runs. The entry gate, the stale-lock
claim, the `before_terminalize` callback, and the fail-closed cases from slice 5 are reused.
Slice 6 changes what the callback does for an authority-requested run: it projects from the
verified legacy-body checkpoint plus the covered journal tail instead of restoring the
checkpoint bytes verbatim.

`run_checkpoint.recover_from_checkpoint` gains an authority-aware branch. When the latest
checkpoint event is `body_kind: base-stripped` and its validated base carries
`run_journal_authority_requested: true`, recovery:

1. Reads the journal with `read_journal_bounded`, quarantines a partial tail via
   `recover_partial_tail`, re-reads, and verifies the chain is contiguous from sequence 1,
   every `previous_digest` links, and every event shares one `run_id` equal to the run
   directory name.
2. Selects the highest-sequence `run.snapshot.checkpointed` event. Any invalid latest
   checkpoint fails closed: no earlier-checkpoint selection and no legacy fallback for
   activated-journal runs. The tail must be covered.
3. Validates the checkpoint via `validate_checkpoint`. For a `base-stripped` checkpoint the
   validated bytes contain `status` plus the present preserved fields and omit the four
   journal-metadata fields. For a `legacy-full` checkpoint the slice-5 full-body validation
   runs unchanged.
4. For a `base-stripped` checkpoint, projects: `run_projector.project_run_snapshot` runs
   against the parsed base plus the verified journal events, with `journal_present=True`.
   The projected snapshot is the intermediate receipt. For a `legacy-full` checkpoint the
   slice-5 byte-identical restore runs unchanged.
5. Only when `run.json` is missing or unparseable, preserves a corrupt `run.json` by rename and
   writes the projected bytes (or the slice-5 checkpoint bytes for a `legacy-full` tail) via
   `localio.write_text_atomic`. A parseable `run.json` is never overwritten by the callback.
   The existing terminalization then writes the final stale-lock failure receipt.

`recover_from_checkpoint` feeds `runs_cmd._recover_from_checkpoint`, which feeds
`cli.runs.dispatch` through the existing `runs_cmd.recover` entry point. The CLI exit codes
are unchanged: 0 for a repaired or already-terminal run, 2 for any fail-closed case.
`recover_from_checkpoint` translates a `ProjectionError` into a bounded `CheckpointError`
with category `projection` before it crosses the existing command boundary.

## Doctor

`doctor.core_station_checks` keeps its slice-5 `runs: recovery checkpoints` check. Slice 6
adds one step before the byte comparison: for an authority-requested run whose latest
checkpoint is `base-stripped`, doctor projects from the verified base plus the covered journal
tail and compares the projected bytes to the current `run.json` instead of comparing the
checkpoint bytes directly. Doctor reprojects before comparison.

The per-run verdict flow becomes:

- Resolve the run's authority state from `run.json` and the journal.
- For a `legacy-full` latest checkpoint (lifecycle-journal-only or slice-5 tail), the slice-5
  verdict runs unchanged: `OK` when the checkpoint bytes equal the current `run.json` writer
  bytes, `WARN` when `run.json` is missing or unparseable with a valid checkpoint, `FAIL` for
  any validation, chain, coverage, or bound failure.
- For a `base-stripped` latest checkpoint on an authority-requested run, doctor projects from
  the parsed base plus the verified journal events and compares the projected bytes to the
  current `run.json`. `OK` when they match. `WARN` when `run.json` is missing or unparseable
  but the projection succeeds (repairable). `FAIL` when the projection raises a
  `ProjectionError`, the chain or coverage check fails, the latest checkpoint is invalid, or
  any bound is exceeded. A parseable recovered stale-lock-recovery receipt is `OK` only when
  doctor reconstructs it exactly from the base plus the journal plus the recovery provenance,
  as in slice 5.

Doctor is fully read-only. It never quarantines, repairs, or mutates the journal, checkpoint
files, or `run.json`. The 50-run scan limit, the newest-first ordering, the 8-name failure
preview, and the aggregate three-field `CheckResult` are unchanged.

## 1,000-run measurement

The cutover to journal authority stays opt-in pending a standard-library-only 1,000-run
measurement. Slice 6 ships the measurement harness but does not interpret it.

The harness is a stdlib-only Python script under `scripts/` with `--runs` and `--output`
arguments. Its default is 1,000. Unit tests call the same importable measurement function
with a small count, while the slice closeout drives exactly 1,000 real run directories
through the journal-authority path and records, per run and in aggregate:

- Journal bytes: total bytes written to `events/lifecycle.jsonl` per run.
- Append latency: wall-clock time of each `run_journal.append_event` call, measured with
  `time.perf_counter_ns`, reported as p50, p95, p99, and max across all appends in the run
  and across the 1,000 runs.
- Environment metadata: Python version, platform, filesystem type where the run directory
  lives, and whether directory fsync is supported.

The harness writes a single JSON report under `.brigade/measurements/` during closeout. Its
Brigade verification receipt and measured values are cited in the PR. It records no invented
pass threshold. The decision to remove the opt-in flag is a separate human judgment made
after reviewing the report, not an automated gate in this slice. The measurement is a
prerequisite for flipping the default. It does not flip the default itself.

## Crash matrix

| Crash window | State after crash | Recovery action |
| --- | --- | --- |
| Before checkpoint temp fsync | no new base file or event, `run.json` unchanged | Validate the previously referenced checkpoint and covered tail. If none exists on the first activated write, fail closed (exit 2). |
| After temp fsync, before hard-link publish | no new final file or event, orphan temp, `run.json` unchanged | Validate the previously referenced checkpoint and covered tail. The orphan temp is private and ignored. If no prior checkpoint exists, fail closed. |
| After hard-link publish, before checkpoint event fsync | new final file durable but unreferenced, `run.json` unchanged | Ignore the orphan checkpoint file and validate the previously referenced checkpoint and covered tail. If no prior checkpoint exists, fail closed. |
| After checkpoint event fsync, before lifecycle append | base and checkpoint event durable, no status event, `run.json` unchanged | Recovery selects the checkpoint (latest valid). Tail is the checkpoint event, covered. Project from the base plus the journal tail, then terminalize. |
| After lifecycle append fsync, before parity evidence | base, checkpoint event, and status event durable, no shadow artifact, `run.json` unchanged | Recovery projects from the base plus the covered journal tail. The next live write records a `comparison-gap` error if the artifact is stale, or rebuilds it under version 3. |
| After parity evidence, before readiness veto | base, events, and shadow artifact durable, `run.json` unchanged | Recovery projects from the base plus the covered tail. The gate is current for this sequence. |
| After readiness veto, before projection | gate ready recorded only in memory, `run.json` unchanged | Recovery projects from the base plus the covered tail. The next live write re-evaluates the gate. |
| During or after `run.json` replace | `run.json` is the old legacy body or the new projected bytes (atomic replace is all-or-nothing) | If `run.json` is the projected bytes, the run is authoritative. Recovery validates and may reproject. If `run.json` is the legacy body, recovery projects from the base plus the covered tail. |
| Projection failure after an authoritative commit | `run.json` holds the last projected bytes, the new write did not advance | The write stopped and cannot downgrade. Recovery projects from the latest verified base plus the covered tail. The run stays authoritative. |

## Compatibility

- Runtime behavior for readers is unchanged: `run.json` bytes for legacy and
  lifecycle-journal-only runs are exactly as in slice 5. For an authoritative run, `run.json`
  is the projected compatibility snapshot under `brigade.run.v1`. It adds the optional
  authority request and four journal-metadata fields to the existing receipt shape. A
  previous Brigade release ignores these additive keys and still parses the snapshot.
- A legacy run directory without `events/lifecycle.jsonl` is untouched. A lifecycle-journal-only
  run directory (slice 2 or 5) is untouched: its checkpoint stays `legacy-full`, its writer
  stays the legacy writer, and the projector is never the live writer.
- A new run directory enrolled with `BRIGADE_RUN_JOURNAL_AUTHORITY=1` still exposes a
  byte-compatible `run.json` to the previous Brigade release while it is not-yet-authoritative
  (it writes the legacy candidate). Once it commits a projected snapshot, the four
  journal-metadata fields appear. Prior releases tolerate them as additive keys.
- No new runtime dependency, daemon, network service, broker, or migration is introduced. The
  journal remains a per-run local file. The projector remains pure. The checkpoint substrate
  remains crash-safe and standard-library only.
- One-way projector-version door: `PROJECTOR_VERSION` is 3. A version-2 shadow artifact is
  stale, is quarantined intact, and is replaced by fresh version-3 evidence. Rolling an
  authority-requested run back to a release that predates slice 6 is unsupported, because that
  release does not recognize `run.artifact_collection.started`, the widened
  `run.completed` / `run.failed` payload status sets, or `base-stripped` checkpoints. Rolling
  forward again is fine because the journal is append-only and the old release never mutated
  it.

## Rollback

Rollback is per-commit and per-slice. The plan's commit boundaries let a wrong task be
reverted in isolation. Two rollback constraints apply:

- Reverting the projector-version bump (3 to 2) alone is unsafe: a version-3 projector
  without the registered `run.artifact_collection.started` event type and the widened payload
  rules fails closed on any authority-requested journal that already carries those events.
  Roll the projector bump and the event-registry and lifecycle-mapping changes back together,
  and drop the activated authority-requested journals.
- Reverting the checkpoint body change (full to stripped) alone is unsafe: a `base-stripped`
  checkpoint read by a slice-5 `validate_checkpoint` fails the writer-byte equality check.
  Roll the checkpoint body change and the recovery and doctor changes back together.

Reverting the enrollment flag and the `aboyeur._write_json` projection branch disables
authority without affecting legacy or lifecycle-journal-only runs. The content-guard pre-push
hook must pass. Never `git push --no-verify`.

## Invariants

1. Projection authority: for an authoritative run, `run.json` is the projected compatibility
   snapshot. The journal owns output `status` and the journal cursor. The checkpoint base owns
   the compatibility seed and every present preserved field.
2. Projection base: a slice-6 checkpoint body retains `status` and omits the four
   journal-metadata fields. The checkpoint is the base for projection and recovery.
3. Write-ahead: the checkpoint file and event are durable before the lifecycle append and
   before any `run.json` replace, for every activated-journal write.
4. Fail-closed hook: a checkpoint or lifecycle failure propagates and stops the write before
   the `run.json` replace. A shadow failure is fail-open for the writer and fail-closed at
   the gate.
5. No downgrade: after the first authoritative snapshot commits, a chain, evidence,
   readiness, or projection failure stops the write. The writer cannot fall back to the
   legacy body for that run.
6. Durable authority: the writer resolves authority before appending. A projected snapshot
   stays authoritative when the journal advances if its saved cursor matches a verified
   journal prefix.
7. First-write parity: step 5 records the first comparison, then the normal readiness gate
   authorizes the first committed projection only when that comparison is a current-version
   `match`. There is no gate bypass.
8. Version-door preservation: a stale v2 shadow artifact is quarantined intact before a fresh
   v3 artifact is written. A current-version mismatch or error is never reset.
9. Recovery authority: recovery projects from the verified base plus the covered journal
   tail for authority-requested runs. It never repairs over a parseable `run.json`.
10. Doctor reprojects: doctor projects from the verified base plus the covered tail before
   comparing to `run.json` for an authority-requested run.
11. Legacy preserved: legacy and lifecycle-journal-only runs are byte-identical to slice 5.
    No existing run migrates.
12. Standard library only: no new runtime dependency, daemon, broker, or migration.

## Blast radius

Exact blast-radius names from the code index. The `aboyeur._write_json` hook is the single
funnel for `run.json` writes. Its indexed production callers are `record_run_start`,
`record_run_termination`, `record_dispatch_stage`, `record_result_processing`,
`record_artifact_collection`, `run`, `write_sidecar_revision`, and
`run_resume._resume_locked`. Its callees are `runguard.resolve_run_lock_workspace`,
`run_lifecycle.prepare_lifecycle_journal`, `run_checkpoint.write_checkpoint`,
`run_lifecycle.record_lifecycle_transition`, `localio.write_text_atomic`, and
`run_shadow.record_shadow_comparison`. `run_checkpoint.recover_from_checkpoint` feeds
`runs_cmd._recover_from_checkpoint`, which feeds `cli.runs.dispatch` through the existing
`runs_cmd.recover` entry point. `run_projector.project_run_snapshot` feeds
`run_shadow.record_shadow_comparison` (as the projection step inside shadow comparison) and
the new authoritative projection step in `aboyeur._write_json`.
`run_shadow.check_projection_readiness` is the veto.

Affected tests (names this slice must keep green or extend): the slice-5 checkpoint tests in
`tests/test_run_checkpoint.py` (including the unchanged default-path
`test_write_checkpoint_append_then_replay` and
`test_first_activated_mapped_write_checkpoint_seq1_then_status_seq2`, plus explicit new
`base-stripped` and `body_kind` cases), the slice-4 shadow tests in
`tests/test_run_shadow.py` (updated for the version-2 staleness and the new reason), the
slice-3 projector tests in `tests/test_run_projector.py` (updated for `PROJECTOR_VERSION` 3
and the three new status mappings), the slice-2 lifecycle tests in
`tests/test_run_lifecycle.py` (updated for the three new `STATUS_EVENT_TYPE` rows), the
`tests/test_runs_cmd.py` recovery tests (extended for the authority-requested projection
recovery), and the `tests/test_doctor.py` recovery-checkpoint tests (extended for the
reproject-before-compare step). The `tests/test_run_events.py` registry test is extended for
`run.artifact_collection.started`.

## Non-goals and deferrals

- Approval, provider, or external-action execution. The journal records their facts. It does
  not execute them. Approval pause and resume projection is deferred. Until that later slice
  adds explicit projector rules, `approval.*`, `run.paused`, `run.resumed`, and
  `run.recovery.*` events must not be appended to an authoritative lifecycle journal. The
  projector correctly fails closed on any registered event without a status rule.
- Redaction, quarantine-and-rechain, or any operator redaction procedure. That is a separate
  operator-only flow outside this slice.
- Checkpoint garbage collection, retention, deduplication across runs, or a `brigade runs gc`
  command. Checkpoints follow whole-run retention.
- Issues #578 and #579 are adjacent routing and isolation work. Closed issue #566 owns outcome
  ledger serialization. None is a follow-up for the acceptance criteria deferred here, and
  all three scopes are explicitly excluded.
- Flipping the default for `BRIGADE_RUN_JOURNAL_AUTHORITY`. The 1,000-run measurement is
  shipped as a prerequisite. The default flip is a later human decision after the report is
  reviewed.
- Event enrichment beyond the three newly mapped statuses and the one new event type.

## Issue status

Issue #568 stays open after this slice. Slice 6 lands the authoritative writer, the stripped
checkpoint base, the projector-version-3 mapping, the readiness-gate version-door split, the
authority-aware recovery, and the doctor reproject step. The remaining acceptance criteria
from the issue (approval pause and resume projection, the crash-after-approval-consumption
reconciliation, the redaction procedure, and the final decision to remove the opt-in flag
after the 1,000-run measurement) are not implemented here. They must be implemented in later
slices or moved before closure. Issue #489 is the existing interrupted-run resume contract,
and issue #604 is the existing requested/observed external-control contract. The approval
integration and operator redaction procedure still need explicit ownership if they move out
of #568. The measurement review may remain a release decision recorded on #568.

Citations. GitHub issue #568 (https://github.com/escoffier-labs/brigade/issues/568). Prior
slices: `docs/phase-568-slice-3-run-projector.md`,
`docs/phase-568-slice-4-shadow-comparison.md`,
`docs/phase-568-slice-5-recovery-checkpoints.md`. No code is implemented in this document.
