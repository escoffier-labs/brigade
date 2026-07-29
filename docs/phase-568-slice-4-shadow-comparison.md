# Issue #568 Slice 4: Shadow Comparison and Readiness Gate

This is the proposed implementation spec for the approved slice 4 boundary
of GitHub issue #568.
Slice 1 landed the append-only journal kernel (`run_journal`, `run_events`).
Slice 2 landed opt-in lifecycle journaling of run status transitions
(`run_lifecycle`, wired into `aboyeur._write_json`). Slice 3 landed the pure
projector (`run_projector`) that derives a `run.json` snapshot from a base
snapshot plus a verified lifecycle event sequence.

Slice 4 wires the projector into the live write path in shadow mode: after
every committed legacy `run.json` write for a run with an activated journal,
it independently reconstructs a **shadow candidate** from the committed
legacy payload plus the five derived fields read off the verified journal
tail, encodes that candidate through `run_projector.encode_snapshot_bytes`,
runs the projector against the same legacy payload and journal, and
compares the two canonical byte strings. Parity is a full canonical byte
comparison, not a status-string comparison. The artifact persists only
SHA-256 digests of the two encodings plus a bounded list of differing field
names. A fail-closed readiness gate exposes the cumulative evidence so a
later slice can decide whether to make the journal authoritative.

**The legacy writer stays authoritative.** Shadow comparison never writes
`run.json`, never appends to the journal, never raises into the writer path,
and never changes what a reader of `run.json` observes. A shadow mismatch,
projection failure, or evidence failure is recorded as durable evidence and
surfaced only through the readiness gate. The run itself proceeds exactly as
before.

Source contracts this spec builds on:

- `src/brigade/run_events.py`: `brigade.run_event.v1` envelope, `EVENT_TYPES`
  registry, canonical byte rules, `_bound`, `MAX_DIAGNOSTIC_LEN` (240).
- `src/brigade/run_journal.py`: `RunEvent`, `JournalReport`, `read_journal`,
  chain verification, typed bounded errors, 0o700 `events` directory and
  0o600 journal file permissions.
- `src/brigade/run_lifecycle.py`: `STATUS_EVENT_TYPE`, the activation model
  (journal file existence is the durable activated fact), the bounded-failure
  wrapping convention (`LifecycleJournalError` categories, exception type
  names only).
- `src/brigade/run_projector.py`: `project_run_snapshot`, `RunProjection`,
  `encode_snapshot_bytes`, `PROJECTOR_VERSION`, the six `ProjectionError`
  subclasses with bounded `diagnostic` strings, the ownership map, and the
  slice-3 status-lag note.
- `src/brigade/aboyeur.py`: `_write_json`, the single funnel for `run.json`
  writes (lifecycle append before, `localio.write_text_atomic` after).
- `src/brigade/localio.py`: `write_text_atomic` (mkstemp 0o600 temp, fsync,
  `os.replace`), `utc_now_iso`.

Standard library only. Brigade is zero-runtime-dependency.

## Boundary

New module: `src/brigade/run_shadow.py`. One hook in `aboyeur._write_json`,
added immediately after the existing `localio.write_text_atomic` call so the
compared candidate is the *committed* legacy snapshot:

```python
localio.write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
if path.name == "run.json" and isinstance(payload, dict):
    run_shadow.record_shadow_comparison(path.parent, payload)
```

The hook runs after the atomic replace returns, so the compared candidate is
the committed legacy snapshot, not a candidate that might still fail to
write. If `write_text_atomic` raises, shadow comparison never runs and the
exception propagates exactly as before this slice.

Coverage boundary, decided:

- The hook fires for every `aboyeur._write_json` call whose target is named
  `run.json` and whose payload is a dict. That covers the aboyeur writers
  and `run_resume.py` (which calls `aboyeur._write_json`).
- `runguard.py`'s guarded-recovery rewrite of `run.json` goes through
  `localio.write_json` directly and is not shadowed. That write is not a
  lifecycle transition and is not journaled either, so there is no
  event-mapping parity signal to compare. Documented, not hidden.
- Inside the hook, `record_shadow_comparison` returns immediately unless the
  run's journal file exists (the slice-2 activation fact) and the payload
  carries a non-empty string `status`. Flag-off runs and legacy run
  directories create no artifact and observe byte-identical behavior.

## Comparison procedure

Per call, `record_shadow_comparison(run_dir, legacy_snapshot)`:

1. Resolve `run_dir`. Return unless `<run-dir>/events/lifecycle.jsonl` is a
   file. Return unless `legacy_snapshot` is a mapping with a non-empty string
   `status`.
2. Load the prior evidence artifact if present (see Evidence artifact). A
   present-but-unparseable artifact is quarantined by rename and treated as
   absent, with the corruption itself recorded as an `error` comparison with
   category `evidence-unreadable`.
3. Read the journal with `run_journal.read_journal`. A partial tail, chain
   errors, or an `OSError` classifies the comparison as `error` with category
   `journal-unreadable`. Through the normal write path this state is
   unreachable, because slice 2 fails the `run.json` write closed before the
   shadow hook runs. The category exists for direct calls and defense in
   depth.
4. Defense in depth: if the journal's first event `run_id` differs from the
   resolved run directory name, classify as `error` with category
   `journal-run-id-mismatch`. The slice-2 writer derives `run_id` from the
   directory name, so this cannot fire through the normal path.
5. **Build the shadow candidate.** Independently deep-copy the committed
   legacy payload (so the candidate shares no mutable state with the caller
   or the projector). Into that deep copy, write all five derived fields from
   the verified journal tail while **keeping the legacy status verbatim**:

   - `status` <- `legacy_snapshot["status"]` (unchanged)
   - `projector_version` <- `run_projector.PROJECTOR_VERSION`
   - `journal_present` <- `True`
   - `journal_last_sequence` <- tail event `sequence`
   - `journal_last_event_digest` <- tail event `event_digest`

   The shadow candidate is exactly "the committed legacy snapshot, plus the
   five projector-derived fields pinned from the journal tail, with the
   legacy status preserved." Encode it through
   `run_projector.encode_snapshot_bytes(shadow_candidate)` to get
   `shadow_bytes`. An encoding failure raises `SnapshotEncodingError`, which
   is classified as `error` with category
   `projection-error:SnapshotEncodingError` (it is a `ProjectionError`
   subclass and the projector's own bounded diagnostic is carried).
6. **Project.** Call
   `run_projector.project_run_snapshot(legacy_snapshot, report.events,
   journal_present=True)`. The legacy candidate is the base snapshot. Any
   `ProjectionError` subclass classifies as `error` with category
   `projection-error:<exception class name>` and carries the projector's
   already-bounded `diagnostic`. On success, `projected_bytes =
   projection.to_bytes()`.
7. **Compare canonical bytes.** This is the parity check.

   - If `shadow_bytes == projected_bytes`, the outcome is `match`.
   - If they differ, decode both byte strings back to mappings, take the
     sorted union of keys, and compute the bounded list of field names whose
     values differ (deep-unequal). Cap the list at `MAX_DIFFERING_FIELDS`
     (16). A longer list is truncated to the first 15 sorted names plus the
     literal token `...`. Then classify the divergence:

     - If the **only** differing field is `status` **and** the legacy status
       is not in `run_lifecycle.STATUS_EVENT_TYPE` (an unmapped status such
       as `dry-run`, `incomplete`, or `artifact-collection`), the outcome is
       `lag`. This is the documented slice-3 status lag: the projector
       derives the last mapped status from the journal tail, while the
       legacy writer advanced to an unmapped status that slice 2 never
       journals. It is a known deferred gap, not a mapping defect.
     - Otherwise the outcome is `mismatch`.

   For both `lag` and `mismatch`, the record persists the SHA-256 digest of
   `shadow_bytes`, the SHA-256 digest of `projected_bytes`, and the bounded
   differing-field list. No raw snapshot bytes, no raw status strings, no
   field values are persisted. For `match`, the two digests are equal by
   construction and the differing-field list is empty.
8. **Detect comparison gaps.** A comparison gap means a committed transition
   whose parity can no longer be checked because the intermediate legacy
   snapshot is gone. Only the journal fact remains. Two conditions, either
   of which records an additional `error` comparison with category
   `comparison-gap` in the same atomic artifact write as the main record:

   - **Absent prior evidence, journal already advanced**: the prior artifact
     is absent (or was just quarantined as unreadable) **and** the current
     journal tail `sequence > 1`. The first comparison on a freshly
     activated run sees `sequence == 1` and is not a gap. A first comparison
     that sees `sequence > 1` missed at least one earlier transition.
   - **Stale prior evidence, journal advanced by more than one**: the prior
     artifact's `last_compared_sequence` is an integer **and** the current
     journal tail sequence is greater than `last_compared_sequence + 1`.

   A gap is a cumulative error: fail-closed is the only honest posture. If
   the missed transition was the run's last write, no later comparison ever
   runs. The gate still closes via its journal-tail currency check (reason
   `journal-ahead-of-evidence`).
9. **Apply idempotency** (see Idempotency), update the cumulative artifact,
   and write it atomically.

The base-snapshot choice is deliberate: the projector preserves the 44
preserved fields verbatim from the base, so using the just-committed legacy
candidate makes preserved fields trivially equal between the shadow and
projected encodings and concentrates the byte-difference signal on the two
things shadow mode exists to prove: the journal replays clean through the
fail-closed projector, and the derived fields (status included) match what
the legacy writer committed. Full-snapshot parity (preserved fields derived
from events rather than copied from a base) requires event enrichment and is
deferred.

## Outcome taxonomy

Every comparison resolves to exactly one outcome:

| Outcome | Condition | Gate effect |
| --- | --- | --- |
| `match` | `shadow_bytes == projected_bytes` | none |
| `lag` | bytes differ, the only differing field is `status`, and the legacy status is not in `run_lifecycle.STATUS_EVENT_TYPE` | blocks the gate only while it is the latest outcome |
| `mismatch` | bytes differ and the divergence is not a `lag` | cumulative, closes the gate permanently for the run |
| `error` | journal unreadable, journal run_id mismatch, projection raised, shadow encoding raised, evidence unreadable, or comparison gap | cumulative, closes the gate permanently for the run |

Status-lag behavior for unmapped statuses, decided: an unmapped legacy
status produces a byte difference on `status` only (the four other derived
fields are pinned from the same journal tail on both sides), so it is
classified `lag`, never `match` and never `mismatch`. The projector derives
the last mapped status from the journal tail. The shadow candidate keeps the
unmapped legacy status. The divergence is fully explained by the documented
slice-3 status lag rather than by a mapping defect. A `lag` record carries
the two digests and `differing_fields == ["status"]` as evidence. Lags do
not count against readiness once a later mapped transition restores
`match` (for example `artifact-collection` -> `ok`), because the gate
evaluates the current replacement risk, and replacing the writer while the
projected status lags the legacy status would serve a stale status to
readers.

Mismatches and errors are cumulative and never forgiven for the life of the
run: once either counter is nonzero, the gate never reports ready for that
run again. This is the issue's "a mismatch is recorded and blocks automatic
projection replacement until the event mapping is corrected" rule. Resetting
evidence after a mapping fix is an operator action deferred to slice 6.

## Evidence artifact

Path: `<run-dir>/events/shadow-comparison.json`. It lives beside the journal
inside the already-private `events` directory (0o700) and follows the run
directory's retention and archive policy. `.brigade/runs/` is gitignored, so
the artifact is never tracked. Custom `--output-dir` runs treat it exactly
like the journal.

Format: one cumulative JSON document, schema `brigade.run_shadow.v1`, written
with the house encoding (`json.dumps(..., indent=2, sort_keys=True) + "\n"`,
UTF-8) through `localio.write_text_atomic`. A single atomic-replaced document
is chosen over an append-only JSONL log: one write per state-changing
comparison, one crash window with fail-closed stale state, no partial-tail
recovery burden, and a gate that reads one small file. The journal remains
the append-only fact log. The shadow artifact is derived evidence and may be
rebuilt or reset by an operator without touching facts.

Schema:

| Field | Content |
| --- | --- |
| `schema` | `brigade.run_shadow.v1` |
| `schema_version` | 1. Bumps on any schema change |
| `run_id` | run directory name |
| `projector_version` | `run_projector.PROJECTOR_VERSION` at last write |
| `comparisons` | count of recorded comparison state changes |
| `matches`, `mismatches`, `lags`, `errors` | per-outcome counters. Their sum always equals `comparisons` |
| `last_compared_sequence` | journal tail sequence at the last comparison that read the journal, else null |
| `last_compared_event_digest` | journal tail event digest at that comparison, else null |
| `last_shadow_digest` | SHA-256 of `shadow_bytes` at the most recent recorded comparison, else null |
| `last_projected_digest` | SHA-256 of `projected_bytes` at the most recent recorded comparison, else null (null on error when projection did not complete) |
| `last_differing_fields` | bounded differing-field list at the most recent recorded comparison, else null (empty on error) |
| `last_outcome` | outcome of the most recent recorded comparison, else null |
| `last_error_category` | error category of the most recent recorded comparison, else null |
| `last_recorded_at` | `localio.utc_now_iso()` of the last recorded comparison, else null |
| `recent_records` | FIFO list of the last 16 comparison records |

Each comparison record:

| Field | Content |
| --- | --- |
| `recorded_at` | `localio.utc_now_iso()` |
| `sequence` | journal tail sequence, or null when the journal was not read |
| `event_digest` | journal tail event digest, or null |
| `shadow_digest` | SHA-256 of `shadow_bytes`, or null when the shadow candidate could not be encoded |
| `projected_digest` | SHA-256 of `projected_bytes`, or null when projection did not complete |
| `differing_fields` | bounded differing-field list, empty on error |
| `outcome` | one of `match`, `lag`, `mismatch`, `error` |
| `category` | error category, present only for `error` records |
| `diagnostic` | bounded diagnostic, present only when a category carries one |

Error categories are a closed set: `journal-unreadable`,
`journal-run-id-mismatch`, `projection-error:<ProjectionError class name>`,
`comparison-gap`, `evidence-unreadable`. Diagnostics carry categories,
sequences, counts, and the projector's own bounded diagnostics only. Raw
payload values, snapshot contents, status strings, `run.json` error strings,
and paths never appear.

Permissions: the artifact file is 0o600. `write_text_atomic` creates its
temp file with `mkstemp` (mode 0o600) inside the 0o700 `events` directory
and renames it into place, so both the temp and the final file are private.
Orphan `.shadow-comparison.json.*.tmp` files left by a SIGKILL between
mkstemp and rename are ignored by all readers (only the exact artifact path
is read) and may be cleaned by future maintenance.

## Atomicity and crash windows

The artifact write is atomic (`mkstemp` + fsync + `os.replace`): a reader or
the gate sees either the previous complete artifact or the new one, never a
truncated file.

Crash windows, decided:

| Window | State after crash | Detection and effect |
| --- | --- | --- |
| After journal fsync, before `run.json` replace | journal holds the event, `run.json` unchanged | Slice-2 idempotency replays the committed event on retry. Shadow is not involved |
| After `run.json` replace, before the shadow comparison | event committed, snapshot committed, no comparison recorded | The next comparison sees the journal tail more than one sequence past `last_compared_sequence` (or, with no prior evidence, a tail sequence > 1) and records a `comparison-gap` error. The gate closes permanently for the run |
| During the artifact write | old artifact intact (atomic replace), possible orphan temp file | Same as above: the next comparison detects the stale `last_compared_sequence` as a gap, or, if the run ended, the gate's journal-tail currency check closes it |
| After the artifact write | fully recorded | none |

The `comparison-gap` error exists because parity for the skipped transition
can no longer be checked: the intermediate legacy snapshot is gone, only the
journal fact remains. Fail-closed is the only honest posture, so a gap is a
cumulative error. If the crashed transition was the run's last write, no
later comparison ever runs. The gate still closes, because the journal tail
no longer matches the artifact's `last_compared_sequence` /
`last_compared_event_digest` (reason `journal-ahead-of-evidence`).

## Idempotency and cumulative behavior

Decided rules:

- A comparison whose state is identical to the artifact's last recorded
  state (same journal tail sequence, same `last_shadow_digest`, same
  `last_projected_digest`, same outcome, same error category) is a complete
  no-op: no counters move, no record is appended, the artifact file is not
  rewritten. A byte-identical rewrite (same status, no field changes)
  produces no new journal event and leaves the shadow and projected
  encodings byte-identical, so the digests are unchanged and the write is a
  complete no-op. A same-status write that changes a preserved detail field
  also produces no new journal event, but the preserved field is copied from
  the base on both sides, so the shadow and projected encodings both change
  in lockstep. The digests differ from the prior record, the idempotency key
  does not match, and a fresh `match` is recorded at the same journal tail.
- Counters count distinct recorded comparison states. The invariant
  `comparisons == matches + mismatches + lags + errors` holds at all times,
  including when a `comparison-gap` or `evidence-unreadable` side error
  record lands in the same atomic write as the main record: each side error
  record increments `comparisons` and `errors` together.
- `recent_records` is capped at 16 entries FIFO. Counters are cumulative
  across the run's life regardless of the cap. Only per-record evidence is
  bounded.
- Gap and corruption errors are applied before the main record in the same
  single atomic artifact write, so one comparison never produces a
  half-updated artifact.

## Readiness gate

`check_projection_readiness(run_dir) -> ReadinessReport` is a read-only,
total function: it never raises and never writes. It is the fail-closed gate
a future slice consults before treating the journal as authoritative. Nothing
calls it at runtime in this slice. Tests exercise it directly.

Ready requires all of the following. Every failure contributes one reason
from this closed set:

| Reason | Condition |
| --- | --- |
| `no-journal` | `<run-dir>/events/lifecycle.jsonl` is absent |
| `journal-unreadable` | journal read fails, has a partial tail, or has chain errors |
| `no-evidence` | the artifact is absent or unreadable as bytes |
| `evidence-unreadable` | the artifact bytes do not parse as JSON |
| `evidence-schema-mismatch` | schema, schema_version, or run_id disagree, or `last_outcome` is outside the outcome set |
| `journal-ahead-of-evidence` | journal tail sequence or digest differs from the artifact's `last_compared_*` |
| `no-comparisons` | `comparisons` is not an integer >= 1 |
| `mismatch-recorded` | `mismatches` is nonzero |
| `error-recorded` | `errors` is nonzero |
| `status-lag-current` | `last_outcome` is `lag` |

The gate re-reads the journal and the artifact but does not re-project. It
is a fast veto, not a proof: the slice-6 replacement decision re-runs a full
replay before acting. A legacy run directory without a journal reports
`no-journal` and is therefore never ready, which is the correct fail-closed
answer for projection replacement.

## Error taxonomy

The module defines no exception classes. Its two public functions are total:

- `record_shadow_comparison` contains every internal failure. Known failure
  modes become `error` comparison records with the closed category set
  above. Anything else (including a failed evidence write) is swallowed by
  one outer `except Exception: return`, because the legacy writer is
  authoritative and shadow evidence is observational. A failed evidence
  write leaves the previous artifact in place. Staleness is then detected by
  gap detection or by the gate's journal-tail currency check, both of which
  fail closed.
- `check_projection_readiness` returns `ReadinessReport(ready=False,
  reasons=("evidence-unreadable",))` from one outer containment on any
  unexpected internal failure.

This mirrors the bounded-failure conventions of `run_lifecycle` (categories
plus exception type names, never raw exception text, which can carry paths
or private values) with one deliberate difference: slice 2 fails the
`run.json` write closed on journal errors, while slice 4 fails open for the
write and fails closed at the gate.

## Privacy

Evidence records carry only: timestamps, integer sequences, SHA-256 digest
hex strings, bounded differing-field-name lists, outcome and category
tokens, and the projector's bounded diagnostics. The allowlist is
structural: record fields are built one key at a time from the comparison
state, so no payload value, snapshot field, `run.json` error string, stack
trace, or path can enter the artifact. Raw status strings are deliberately
**not** persisted: a divergence on `status` is recorded as the field name
`"status"` in `differing_fields` plus the two encodings' digests, never as
the status value itself. A regression test drives a run whose `run.json`
carries a private marker in its `error` field and asserts the marker never
appears in the artifact bytes.

## Invariants

1. Authoritative legacy writer: the module never writes `run.json`, never
   appends to or truncates the journal, and never raises into the writer
   path.
2. Flag-off compatibility: no journal file, no artifact, no behavior change.
3. Atomic evidence: the artifact is only ever observed complete, at the old
   or new state.
4. Counter coherence: `comparisons` equals the sum of the four outcome
   counters at all times.
5. Idempotent observation: an unchanged comparison state (same tail
   sequence, same digests, same outcome, same category) produces no write.
6. Cumulative failure: `mismatches` and `errors` never decrease and are
   never reset by the module.
7. Fail-closed gate: any missing, unreadable, stale, or failed evidence
   closes the gate with a bounded reason code.
8. Bounded evidence: every diagnostic and differing-field list in the
   artifact is bounded. Differing-field lists cap at `MAX_DIFFERING_FIELDS`
   (16).
9. Private artifacts: the `events` directory stays 0o700 and the artifact
   file is 0o600. No raw status strings or snapshot values are persisted.

## API

```python
SHADOW_SCHEMA: str           # "brigade.run_shadow.v1"
SHADOW_SCHEMA_VERSION: int   # 1
SHADOW_ARTIFACT_NAME: str    # "shadow-comparison.json"
MAX_RECENT_RECORDS: int     # 16
MAX_DIFFERING_FIELDS: int   # 16

OUTCOME_MATCH / OUTCOME_LAG / OUTCOME_MISMATCH / OUTCOME_ERROR: str
REASON_NO_JOURNAL / REASON_JOURNAL_UNREADABLE / REASON_NO_EVIDENCE /
REASON_EVIDENCE_UNREADABLE / REASON_EVIDENCE_SCHEMA_MISMATCH /
REASON_JOURNAL_AHEAD / REASON_NO_COMPARISONS / REASON_MISMATCH_RECORDED /
REASON_ERROR_RECORDED / REASON_STATUS_LAG_CURRENT: str

@dataclass(frozen=True)
class ReadinessReport:
    ready: bool
    reasons: tuple[str, ...]

def shadow_artifact_path(run_dir: Path) -> Path: ...
def record_shadow_comparison(run_dir: Path, legacy_snapshot: Mapping[str, Any]) -> None: ...
def check_projection_readiness(run_dir: Path) -> ReadinessReport: ...
```

`SHADOW_SCHEMA_VERSION` bumps on any artifact schema change.
`projector_version` inside the artifact tracks
`run_projector.PROJECTOR_VERSION` so a projector bump is visible in evidence.

## Tests

New file `tests/test_run_shadow.py`, reusing the `tests/test_run_lifecycle.py`
scaffolding (real git repo fixture, real `runguard.run_lock` contexts, the
`enabled` env-flag fixture, and the `_write_run_json_locked` write helper):

1. Flag off: locked `started` write creates no `events` directory and no
   artifact. The gate reports `no-journal`.
2. Requested but not activated (pre-lock write only): no artifact.
3. First locked write on an activated run records a `match`: counters,
   `last_compared_sequence == 1`, `last_shadow_digest ==
   last_projected_digest`, gate ready.
4. Full mapped chain (`started` through `ok`, seven events): seven matches,
   gate ready at the end.
5. Byte-identical rewrite (same status, no field changes): artifact bytes
   are unchanged, counters do not move. A same-status write with a changed
   preserved detail field records a fresh `match` at the same journal tail
   (the preserved field is copied from the base on both sides, so both
   encodings change in lockstep and the digests differ from the prior
   record).
6. Unmapped status (`artifact-collection`) after `handoff`: outcome `lag`,
   `lags == 1`, `differing_fields == ["status"]`, journal sequence
   unchanged, gate reports `status-lag-current`.
7. Mapped `ok` after the lag: outcome `match`, gate ready again.
8. Forged mapped-status divergence (direct call with a legacy candidate
   whose mapped status disagrees with the journal tail): `mismatch`,
   `mismatches == 1`, `differing_fields == ["status"]`, gate reports
   `mismatch-recorded`, `run.json` bytes untouched.
9. Journal containing a valid registered-but-unmapped event type
   (`run.paused` appended directly): next locked write still advances
   `run.json` (fail open) and records `error` with category
   `projection-error:UnmappedEventTypeError`. Gate reports `error-recorded`.
10. Journal with a partial tail (direct call): `error` with category
    `journal-unreadable`. Gate reports `journal-unreadable`.
11. Simulated crash window (transition committed with the shadow step
    skipped, then a later transition): a `comparison-gap` error is recorded
    alongside the later `match`. Gate reports `error-recorded`.
12. Corrupt artifact: renamed to `shadow-comparison.json.corrupt-*`, fresh
    artifact records `evidence-unreadable` plus the new comparison. Gate
    reports `error-recorded`.
13. Journal advanced without a comparison (direct append): gate reports
    `journal-ahead-of-evidence`.
14. First comparison on a journal whose tail sequence is already > 1 with no
    prior artifact (direct call): a `comparison-gap` error is recorded
    alongside the main record.
15. Gate reason coverage: no journal, no evidence, wrong schema, wrong
    run_id, crafted zero-comparison artifact (`no-comparisons`).
16. Gate never raises on garbage artifact bytes: reports
    `evidence-unreadable`.
17. Privacy: a run whose `run.json` carries a private marker in `error`
    completes `failed` as a `match`, and the marker never appears in the
    artifact bytes. No raw status string appears in the artifact bytes.
18. Permissions and encoding: artifact mode 0o600, `events` directory
    0o700, sorted keys, two-space indent, exactly one trailing newline.
19. More than 16 distinct comparisons: `recent_records` caps at 16 while
    counters stay cumulative and coherent.
20. More than 16 differing fields (forged legacy candidate with many extra
    keys the projector rejects): the differing-field list caps at
    `MAX_DIFFERING_FIELDS` with the truncation token.
21. Containment: a journal read that raises `OSError` inside shadow still
    lets the locked write advance `run.json`, and records
    `journal-unreadable`. A failing evidence write is fully swallowed and
    the write proceeds.
22. Forged journal whose first event `run_id` differs from the directory
    name (direct call): `error` with category `journal-run-id-mismatch`.
23. A non-`run.json` write through `aboyeur._write_json` (for example
    `roster.json`) leaves the artifact untouched.

One existing test is updated: `tests/test_run_lifecycle.py`'s
`test_no_journal_file_until_lock_held` asserts the `events` directory
contains exactly `lifecycle.jsonl`. After this slice it also contains
`shadow-comparison.json` once the locked write runs.

## Implementation steps

1. Add `tests/test_run_shadow.py` (RED: import fails on the missing module).
2. Add `src/brigade/run_shadow.py` per the API and semantics above.
3. Wire the two-line hook into `aboyeur._write_json` and add the
   `run_shadow` import.
4. Update the one `events`-directory listing assertion in
   `tests/test_run_lifecycle.py`.
5. Run the focused suite, the neighboring journal/lifecycle/projector
   suites, then the full repository suite, ruff, and mypy.
6. Run the complete gate through Brigade:
   `brigade work verify run --target . --command "./scripts/verify" --capture brigade-work`.

The execution plan with exact code and commands is
`docs/phase-568-slice-4-shadow-comparison-plan.md`.

## Compatibility

- Runtime behavior for readers and writers is unchanged: `run.json` bytes,
  journal bytes, CLI output, and `runs watch`, `show`, `steer`, `interrupt`,
  `recover`, and `resume` behavior are exactly as before. The only new
  runtime effect is one extra `is_file()` check per `run.json` write on
  legacy runs, and one journal read plus one small atomic write per
  state-changing comparison on journaled runs.
- The four reserved fields (`projector_version`, `journal_present`,
  `journal_last_sequence`, `journal_last_event_digest`) are still not written
  to `run.json`. The only new on-disk artifact is
  `events/shadow-comparison.json`, additive and private.
- A legacy run directory without `events/lifecycle.jsonl` is untouched: no
  artifact is created and the gate reports `no-journal`.
- A new run directory still exposes a byte-compatible `run.json` to the
  previous Brigade release.

## Non-goals

- Recovery integration: `brigade runs recover` journal verification and
  projection repair is slice 5.
- Journal-authoritative writes or projection replacement: slice 6, gated by
  this slice's readiness gate plus the issue's parity measurement criteria.
- Approval, pause, resume, or recovery event enrichment and projection.
- Journaling or projecting the unmapped statuses (`dry-run`, `incomplete`,
  `artifact-collection`). Their lag is classified, not fixed.
- Retroactive comparison of transitions that crashed before their shadow
  step. Gaps close the gate instead.
- Evidence reset tooling after a mapping fix (an operator action, slice 6).
- Any CLI surface, doctor output, compaction, or redaction flow.

## Deferrals

- Slice 5 (issue #568 step 5): recovery verifies the chain and rebuilds a
  missing, corrupt, or stale snapshot. It should treat a closed readiness
  gate as a veto on journal-sourced rebuilds and surface the gate reasons.
- Slice 6 (issue #568 step 6): new runs treat the journal as authoritative
  only after parity passes. The gate is the per-run veto. The issue's
  1,000-run journal size and latency measurement remains a separate
  prerequisite. Evidence reset after a mapping correction is defined there.
- Event enrichment (unmapped statuses, dispatch observation detail, approval
  pause and resume projection) precedes any journal-only reconstruction
  without a base snapshot.
