# Issue #568 Slice 3: Run Snapshot Projector

This is the proposed implementation spec for the approved slice 3 boundary
of GitHub issue #568.
Slice 1 landed the append-only journal kernel (`run_journal`, `run_events`).
Slice 2 landed opt-in lifecycle journaling of run status transitions
(`run_lifecycle`, wired into `aboyeur._write_json`). Slice 3 adds a pure
projector that derives a `run.json` snapshot from a base snapshot plus a
verified lifecycle event sequence.

**This slice does not write `run.json` and does not change runtime behavior.**
It adds one pure module and its tests. No existing writer, reader, CLI
command, or recovery path calls the projector in this slice. Shadow
comparison, recovery integration, and live writer integration are later
slices, listed under Deferrals.

Source contracts this spec builds on:

- `src/brigade/run_events.py`: `brigade.run_event.v1` envelope, `EVENT_TYPES`
  registry, `validate_event`, canonical byte rules, `MAX_DIAGNOSTIC_LEN`.
- `src/brigade/run_journal.py`: `RunEvent`, `JournalReport`, `read_journal`,
  chain verification, typed bounded errors.
- `src/brigade/run_lifecycle.py`: `STATUS_EVENT_TYPE`, the slice-2 status to
  event mapping, and the allowlisted payload construction in
  `_allowlisted_payload`.
- `src/brigade/aboyeur/`: the current `run.json` field contract
  (`_run_payload` plus the merge-write recorders) and the snapshot encoding
  in `_write_json` (`json.dumps(payload, indent=2, sort_keys=True) + "\n"`,
  UTF-8, atomic replace).
- `src/brigade/run_resume.py`: writes the `resumed_at` and `recovery_history`
  run.json fields during resume.
- `src/brigade/runguard.py`: writes the `recovery_preserved_artifact`
  run.json field during guarded recovery.

Standard library only. Brigade is zero-runtime-dependency.

## Boundary

`project_run_snapshot(base_snapshot, events, *,
journal_present=...)` is a pure function with an explicit ownership map over
every current `run.json` field:

- It validates that `events` is a verified lifecycle sequence: every envelope
  passes `run_events.validate_event`, sequences are contiguous from 1,
  `previous_digest` links to the prior `event_digest`, and every event
  carries the same `run_id`. Any deviation fails closed. The expected caller
  obtains events from `run_journal.read_journal` after checking
  `partial_tail is None` and `chain_errors == []`. The projector re-verifies
  anyway as a second check.
- It derives exactly five fields: `status`, `projector_version`,
  `journal_present`, `journal_last_sequence`, `journal_last_event_digest`.
  Journal presence is an explicit input because an empty journal and a
  missing journal both produce an empty event sequence.
- It preserves every other current `run.json` field verbatim from
  `base_snapshot` through an explicit ownership map.
- It emits deterministic bytes using the existing sorted, indented
  `run.json` encoding, not the compact journal-line canonical encoding.
- It rejects unknown or unmapped fields in `base_snapshot` and unmapped
  event types in `events`. Nothing is skipped silently.

The projector performs no I/O, reads no clock, reads no environment, and
holds no mutable module state. The same inputs always produce byte-identical
output.

## API and types

New module: `src/brigade/run_projector.py`.

```python
PROJECTOR_VERSION: int = 1

# Field ownership over the run.json contract. Every current run.json key is
# in exactly one of these two sets. See the ownership inventory below.
DERIVED_FIELDS: frozenset[str]      # {"status", "projector_version", "journal_present",
                                    #  "journal_last_sequence", "journal_last_event_digest"}
PRESERVED_FIELDS: frozenset[str]    # the 44 remaining current run.json keys
OWNED_FIELDS: frozenset[str]        # DERIVED_FIELDS | PRESERVED_FIELDS

# Static event_type -> run.json status mappings (payload-independent rows of
# the event-to-status table below).
EVENT_STATUS: dict[str, str]

class ProjectionError(RuntimeError):
    """Base class for projection failures. Carries a bounded ``diagnostic``."""
    diagnostic: str

class UnknownSnapshotFieldError(ProjectionError): ...
class UnmappedEventTypeError(ProjectionError): ...
class EventChainError(ProjectionError): ...
class EventPayloadError(ProjectionError): ...
class ProjectionInputError(ProjectionError): ...
class SnapshotEncodingError(ProjectionError): ...

@dataclass(frozen=True)
class RunProjection:
    """Result of a successful projection."""
    snapshot: dict[str, Any]        # complete projected run.json object
    status: str                     # derived status (equals snapshot["status"])
    journal_present: bool
    last_sequence: int              # 0 when events is empty
    last_event_digest: str | None   # None when events is empty
    def to_bytes(self) -> bytes: ...

def project_run_snapshot(
    base_snapshot: Mapping[str, Any],
    events: Sequence[run_journal.RunEvent | Mapping[str, Any]],
    *,
    journal_present: bool,
) -> RunProjection: ...

def encode_snapshot_bytes(snapshot: Mapping[str, Any]) -> bytes: ...
```

Semantics:

- `events` items may be typed `run_journal.RunEvent` instances or raw
  envelope mappings. Every item is normalized to an envelope mapping and
  passed through `run_events.validate_event`. Typed events use
  `RunEvent.to_dict()`. Validation must produce an empty error list before
  projection continues. This keeps direct typed inputs under the same
  structural and digest checks as raw mappings.
- Chain verification inside the projector: the first event has `sequence ==
  1` and `previous_digest is None`. Each subsequent event has `sequence ==
  prior + 1` and `previous_digest == prior.event_digest`. All events share
  one `run_id`. Any break raises `EventChainError`. An empty sequence is
  valid and means "no committed journal facts".
- `base_snapshot` keys must be a subset of `OWNED_FIELDS`. Any other key
  raises `UnknownSnapshotFieldError`. Derived-field keys present in
  `base_snapshot` are ignored and recomputed, so re-projecting a projected
  snapshot is idempotent.
- `projector_version` is the constant `PROJECTOR_VERSION`, starting at 1. It
  bumps whenever the ownership map, the event-to-status mapping, or the
  encoding contract changes.
- `journal_present` must be a real boolean supplied by the caller from the
  journal path check. It is not inferred from `events`: a created empty
  journal projects `journal_present: True`, while a missing journal projects
  `journal_present: False`. Either case has `journal_last_sequence: 0`,
  `journal_last_event_digest: None`, and preserves `status` from
  `base_snapshot`. A non-boolean value raises `ProjectionInputError`.
- `encode_snapshot_bytes` is the single encoding path:
  `(json.dumps(snapshot, indent=2, sort_keys=True) + "\n").encode("utf-8")`,
  matching `aboyeur._write_json` byte for byte. `RunProjection.to_bytes()`
  delegates to it. JSON encoding failures are wrapped as
  `SnapshotEncodingError` with a bounded diagnostic that does not include
  snapshot values.

## run.json field ownership inventory

Derived (projector-owned, recomputed on every call):

| Field | Derivation |
| --- | --- |
| `status` | Event-to-status mapping of the final event, or preserved from base when the sequence is empty |
| `projector_version` | Constant `PROJECTOR_VERSION` (1) |
| `journal_present` | Explicit boolean input describing journal path presence |
| `journal_last_sequence` | Final event `sequence`, or 0 |
| `journal_last_event_digest` | Final event `event_digest`, or null |

Preserved (snapshot-owned and copied verbatim from `base_snapshot`, never
added, removed, retyped, or reformatted by the projector):

Identity and schema: `schema` (`"brigade.run.v1"`), `schema_version` (1),
`task`, `orchestrator`, `roster`, `worker`, `scheduler`.

Run configuration: `dry_run`, `read_only`, `cwd`, `lock_workspace`,
`codex_transport`, `lifecycle_journal_requested`.

Timing: `started_at`, `status_started_at`, `finished_at`,
`duration_seconds`, `resumed_at`.

Recovery (written by `run_resume.py` and `runguard.py`):
`recovery_history`, `recovery_preserved_artifact`.

Briefs and routing telemetry: `code_graph_brief`, `drift_impact_brief`,
`evidence_brief`, `brief_budget`, `route`, `skill_route_policy`,
`pre_run_snapshot`, `git`, `code_graph_delta`, `context_eval`,
`suspected_noop`.

Control transport: `control_transport`, `control_socket`.

Live progress (written by `record_dispatch_stage` and
`record_result_processing`): `active_stage`, `active_seats`, `phase_owner`.

Outcome and failure: `error`, `failure_phase`, `failure_kind`, `failure`,
`transport_warning`, `artifact_collection`.

Artifact references: `artifacts`, `handoff`.

That is 44 preserved fields plus 5 derived fields, 49 owned keys total.
This inventory mirrors `_run_payload` and the merge-write recorders in
`aboyeur.py` as of this spec, plus the recovery writers `run_resume.py`
(`resumed_at`, `recovery_history`) and `runguard.py`
(`recovery_preserved_artifact`). Any future writer that adds a `run.json`
field must extend the ownership map in the same change. The projector's
rejection of unmapped fields is the enforcement mechanism.

## Event-to-status mapping

Slice 2 (`run_lifecycle.STATUS_EVENT_TYPE`) journals these run.json status
transitions. The projector inverts that mapping. Where two run.json
statuses share one event type, the event payload `status` disambiguates,
because `_allowlisted_payload` always records the run.json status string in
the payload for these types.

| Event type | Derived status | Payload rule |
| --- | --- | --- |
| `run.created` | `started` | `payload.status` must be `"started"` |
| `run.planning.started` | `planning` | none |
| `run.dispatch.requested` | `dispatching` | none |
| `run.dispatch.completed` | `result-processing` | none |
| `run.synthesis.started` | `synthesizing` | none |
| `run.synthesis.completed` | `handoff` | none |
| `run.completed` | `ok` | `payload.status` must be `"ok"` |
| `run.failed` | value of `payload.status` | `payload.status` must be `"failed"` or `"timeout"` |
| `run.interrupted` | `canceled` | `payload.status` must be `"canceled"` |

Timeout vs failed: both run.json statuses journal as `run.failed`. The
projector reads `payload.status` and uses it directly as the derived status,
after checking it is `"failed"` or `"timeout"`. Any other value, or a
missing `status` key where the rule requires one, raises
`EventPayloadError`. The payload `detail` field is never read by the
projector.

Registered event types with no current status mapping
(`run.planning.completed`, `run.planning.failed`, `run.dispatch.observed`,
`run.dispatch.failed`, `run.synthesis.failed`, `run.paused`, `run.resumed`,
`approval.requested`, `approval.granted`, `approval.rejected`,
`approval.held`, `approval.consumed`, `run.recovery.started`,
`run.recovery.completed`) are never emitted by the slice-2 writer. If one
appears in the sequence, projection stops with `UnmappedEventTypeError`.
They are never skipped silently. Mapping them is deferred to the work listed
under Deferrals.

Status lag note: the run.json statuses `dry-run`, `incomplete`, and
`artifact-collection` have no event mapping in slice 2, so the journal
never records those transitions. When a run ends in one of those statuses,
the projected status (from the final journaled transition) legitimately
lags the live writer's `run.json` status. This is by design in slice 3.
Detecting and reconciling the gap belongs to shadow comparison (slice 4),
and journaling those statuses belongs to event enrichment. Both are
deferred.

## Invariants

1. Purity: no I/O, no clock, no environment, no randomness. Output depends
   only on `(base_snapshot, events, journal_present)`.
2. Determinism: identical inputs produce byte-identical `to_bytes()` output
   on every call and every host.
3. Closed ownership: output keys are a subset of `OWNED_FIELDS`. A base
   snapshot key outside `OWNED_FIELDS` stops projection. It is never
   dropped or passed through.
4. Preservation: for every preserved key present in `base_snapshot`, the
   output value deep-equals the base value. The projector adds no preserved
   key that base lacks and removes none that base has.
5. Derivation completeness: all five derived fields are always present in
   the output and always recomputed, even when present in the base.
6. Fail-closed chain: any sequence gap, duplicate, digest-link break,
   run_id mix, invalid envelope, unmapped event type, or payload rule
   violation raises a typed error. There is no partial projection.
7. Bounded errors: every failure is a `ProjectionError` subclass whose
   `diagnostic` is at most `run_events.MAX_DIAGNOSTIC_LEN` (240)
   characters, truncated with the same ellipsis convention as the journal
   layer.
8. Encoding parity: `to_bytes()` equals the `aboyeur._write_json` encoding
   of the same object: sorted keys, two-space indent, trailing newline,
   UTF-8.

## Bounded errors

| Error | Raised when | Diagnostic content |
| --- | --- | --- |
| `UnknownSnapshotFieldError` | base snapshot carries a key outside `OWNED_FIELDS` | category plus up to 8 sorted offending key names |
| `UnmappedEventTypeError` | a validated event's type has no status mapping | category plus the event type and its sequence |
| `EventChainError` | envelope invalid, sequence gap or duplicate, digest-link break, mixed run_id | category plus the failing sequence, never raw envelope bytes |
| `EventPayloadError` | a payload status rule fails | category plus event type and the expected value set, never the raw payload value |
| `ProjectionInputError` | `journal_present` is not a real boolean | category only |
| `SnapshotEncodingError` | projected values cannot be encoded by the existing `run.json` JSON contract | category only, never the raw value or serializer message |

Diagnostics carry categories and field names only. Raw payload values,
paths, and snapshot contents never appear in a diagnostic, matching the
slice-2 bounded-failure rule.

## Golden fixture and unit tests

Fixtures (under `tests/fixtures/run-lifecycle/`, alongside the existing
`golden-lifecycle.jsonl`):

- `golden-projection.base.json`: a base snapshot exercising the ownership
  map, including nested objects (`failure`, `code_graph_brief`,
  `control_transport`), lists (`active_seats`, `resumed_at`,
  `recovery_history`), and optional fields (`scheduler`, `handoff`,
  `artifact_collection`, `recovery_preserved_artifact`).
- `golden-projection.expected.json`: the exact expected projected bytes for
  that base plus the full `golden-lifecycle.jsonl` sequence.

Tests in `tests/test_run_projector.py`:

1. Golden replay: read `golden-lifecycle.jsonl` via
   `run_journal.read_journal`, assert no chain errors and no partial tail,
   project against the golden base, assert `to_bytes()` equals the golden
   expected bytes exactly.
2. Golden determinism: a second projection of the same inputs is
   byte-identical to the first.
3. Empty sequence with no journal: `journal_present` is False, last sequence
   0, digest None, status and all preserved fields match base.
4. Empty created journal: the same empty sequence with
   `journal_present=True` projects presence as True without inventing a last
   sequence or digest.
5. Full golden sequence derives `status == "ok"`,
   `journal_last_sequence == 6`, and the digest of the final golden event.
6. `run.failed` with `payload.status == "timeout"` derives `timeout`.
7. `run.failed` with `payload.status == "failed"` derives `failed`.
8. `run.failed` with any other `payload.status`, or a missing `status`,
   raises `EventPayloadError` with a diagnostic of at most 240 chars.
9. `run.interrupted` derives `canceled`. A wrong payload status raises.
10. Unknown base snapshot key raises `UnknownSnapshotFieldError`. The key
   name appears in the bounded diagnostic.
11. Registered-but-unmapped event type (for example `approval.requested`)
    in the sequence raises `UnmappedEventTypeError`.
12. Sequence gap, duplicate sequence, broken `previous_digest` link, and
    mixed run_id each raise `EventChainError` and produce no output.
13. Invalid envelope mapping (for example an unknown event type, which
    fails `run_events.validate_event`) raises `EventChainError`.
14. Invalid-envelope diagnostics do not include rejected envelope values.
15. A mutated typed `RunEvent` replacement whose envelope digest or ID does
    not validate raises `EventChainError`.
16. Preservation: every preserved field in a full-coverage base deep-equals
    the output, including nested objects, and no preserved key appears or
    disappears.
17. Re-projection idempotence: feeding a projected snapshot back as the
    base with the same events yields byte-identical output.
18. Encoding parity: `to_bytes()` equals
    `json.dumps(snapshot, indent=2, sort_keys=True) + "\n"` encoded UTF-8.
19. A non-JSON or circular preserved value raises `SnapshotEncodingError`
    with a bounded category-only diagnostic.
20. A non-boolean `journal_present` value raises `ProjectionInputError`.

The golden expected file is generated once by the implementation and
reviewed by hand before commit, the same convention as
`golden-lifecycle.jsonl`.

## Implementation steps

1. Add `src/brigade/run_projector.py` with the constants, ownership sets,
   error classes, `RunProjection`, `project_run_snapshot`, and
   `encode_snapshot_bytes` as specified above. Standard library only.
2. Implement event normalization: convert typed `RunEvent` instances with
   `to_dict()`, validate every normalized mapping through
   `run_events.validate_event`, then verify the chain (contiguous sequence
   from 1, digest linkage, single run_id).
3. Implement the ownership check and the preservation copy (deep copy of
   preserved values so the result shares no mutable state with the caller).
4. Implement status derivation per the event-to-status table, including the
   payload status rules and the empty-sequence preservation rule.
5. Implement `encode_snapshot_bytes` and `RunProjection.to_bytes()`.
6. Add the two golden fixtures under `tests/fixtures/run-lifecycle/`.
7. Add `tests/test_run_projector.py` with the 20 tests above.
8. No changes to `aboyeur.py`, `run_lifecycle.py`, `run_journal.py`,
   `run_events.py`, any CLI module, or any writer path.

## Compatibility

- Runtime behavior is unchanged. No code path invokes the projector in this
  slice, so `run.json` bytes, journal bytes, CLI output, and reader behavior
  (`runs watch`, `show`, `steer`, `interrupt`, `recover`, `resume`) are all
  exactly as before.
- The field names `projector_version`, `journal_present`,
  `journal_last_sequence`, and `journal_last_event_digest` are reserved by
  this spec for slice 4 and later. Nothing writes them yet. When a later
  slice starts writing them, existing readers see additive keys in a
  `brigade.run.v1` object, which the current reader set tolerates.
- `PROJECTOR_VERSION` starts at 1 and bumps on any change to the ownership
  map, the event-to-status mapping, or the encoding contract.
- Legacy run directories without `events/lifecycle.jsonl` are unaffected.
  The projector is never called for them at runtime.

## Non-goals

- Writing `run.json` or any other file from the projector.
- Comparing projected output against the live writer's output.
- Rebuilding `run.json` from the journal during recovery.
- Journaling or projecting the unmapped statuses (`dry-run`, `incomplete`,
  `artifact-collection`).
- Projecting approval, pause, resume, or recovery events.
- Journal compaction, redaction, retention, or quarantine flows.
- Any CLI surface.

## Deferrals

- Shadow comparison: running the projector alongside the live writer after
  each transition, recording mismatches, and gating automatic projection
  replacement is slice 4 (issue #568 step 4).
- Recovery: `brigade runs recover` journal verification and projection
  repair is slice 5 (issue #568 step 5). This slice's fail-closed validation
  is the contract recovery will reuse.
- Live writer integration: routing `aboyeur._write_json` through the
  projector, or making the journal authoritative for new runs, waits for
  shadow parity (issue #568 step 6).
- Journal-only reconstruction: projecting without a base snapshot (a
  deleted or corrupt `run.json`) requires deriving preserved fields from
  events, which the current event payloads do not carry. It needs event
  enrichment first and is out of scope here.
- Event enrichment: new payload keys or event types that would let the
  journal represent unmapped statuses, dispatch observation detail, or
  approval pause and resume projection are future slices under the issue's
  event contract, not this one.
