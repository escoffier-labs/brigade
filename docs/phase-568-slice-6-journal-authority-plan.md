# Issue #568 Slice 6: Journal Authority Implementation Plan

Goal in one sentence: cut `run.json` over to a journal-authoritative projected
snapshot for newly enrolled runs while leaving legacy and lifecycle-only runs
byte-identical to slice 5.

Architecture: the append-only lifecycle journal owns output `status` and the
journal cursor, a canonical projection base (the legacy candidate minus the
four self-referential journal-metadata fields) owns the preserved fields, and
the pure `run_projector` derives `run.json` from base plus verified journal
tail. The `aboyeur._write_json` hook is the single funnel: it writes the
stripped checkpoint and lifecycle event first, records shadow parity, consults
the readiness gate, then either writes the legacy body (not-yet-authoritative
fallback) or replaces `run.json` with the projected bytes (authoritative).
Recovery and doctor reproject from the verified base plus the covered tail
before comparing or restoring.

Technology constraints: standard library only, zero runtime dependencies, no
daemon, no broker, no migration. Python 3.10+ (matches `pyproject.toml`
`requires-python = ">=3.10"`).
`run_projector` stays pure (no I/O, no clock, no environment). All journal and
checkpoint reads go through `run_journal.read_journal_bounded` with the slice-5
`MAX_JOURNAL_BYTES` (8 MiB) and `MAX_JOURNAL_EVENTS` (512) limits. The
`BRIGADE_RUN_JOURNAL_AUTHORITY` opt-in flag is per-run and durable, enrolls new
runs only, and requires `BRIGADE_LIFECYCLE_JOURNAL` semantics (it sets both
request fields). The default flip is a later human decision after the 1,000-run
measurement is reviewed.

Execute task-by-task, tracking every checkbox. Each task is RED first: write
the named failing tests, run them and watch them fail, implement the minimal
change, run to green, then commit. No task lands a placeholder API or a
not-implemented function. No commit knowingly leaves a prior focused suite red.
Where a prior test must change, the task names it and updates it in the same
commit. The closeout runs the full `./scripts/verify` gate through Brigade.

## File and responsibility map

Every file the slice creates or modifies, with the one responsibility each
holds. Later tasks may return to a file owned by an earlier stage only after
the earlier commit and focused verification are complete. No parallel workers
share write ownership.

| File | Responsibility | Owning task |
| --- | --- | --- |
| `src/brigade/run_events.py` | Register `run.artifact_collection.started` and add optional `body_kind` to the `run.snapshot.checkpointed` payload allowlist | 1 |
| `src/brigade/run_projector.py` | Bump `PROJECTOR_VERSION` to 3, widen `run.completed`/`run.failed` payload rules, add the `artifact-collection` static row, add `run_journal_authority_requested` to `PRESERVED_FIELDS` | 1 |
| `tests/test_run_events.py` | Registry and payload-allowlist assertions for the new event type and key | 1 |
| `tests/test_run_projector.py` | Version, three new status mappings, and unknown-payload-status error assertions | 1 |
| `src/brigade/run_lifecycle.py` | Add the three `STATUS_EVENT_TYPE` rows for `dry-run`, `incomplete`, `artifact-collection` | 2 |
| `tests/test_run_lifecycle.py` | Mapping and append assertions for the three previously unmapped statuses | 2 |
| `src/brigade/run_checkpoint.py` | `body_kind` on the payload, `write_checkpoint` `body_kind` parameter, stripped-base publish path, `validate_checkpoint` body-kind branch, `recover_from_checkpoint` authority-aware projection branch | 3, 5 |
| `tests/test_run_checkpoint.py` | `body_kind`, stripped-base, idempotency-key, coverage, and recovery-projection assertions, plus the closed checkpoint payload-allowlist assertion widened in task 1 | 1, 3, 5 |
| `src/brigade/run_shadow.py` | `REASON_EVIDENCE_PROJECTOR_VERSION_STALE`, version-door split in `check_projection_readiness`, stale-v2 quarantine in `record_shadow_comparison`, gap-baseline carry, bounded journal reads | 4 |
| `tests/test_run_shadow.py` | Stale-v2, quarantine, gap-baseline, current-v3 non-reset, schema-mismatch split, bounded-read assertions, plus the `test_unmapped_status_after_handoff_is_lag` expectation updated in task 2 and the `test_gate_reason_coverage` zero-comparisons projector-version bump plus the renamed current-version checkpoint-tail readiness test updated in task 1 | 1, 2, 4 |
| `tests/test_runs_cmd.py` | Authority-requested recovery projection, corrupt-preserve, terminalization field preservation, fail-closed projection error, legacy-full verbatim restore | 5 |
| `tests/test_runguard.py` | Kept green unchanged for the recovery plane | 5 |
| `src/brigade/doctor.py` | Reproject-before-compare branch in `_recovery_checkpoint_run_verdict` for `base-stripped` authority-requested runs | 6 |
| `tests/test_doctor.py` | Reproject OK, missing-run.json WARN, projection-error FAIL, legacy-full verbatim, no-mutation assertions | 6 |
| `src/brigade/aboyeur.py` | `BRIGADE_RUN_JOURNAL_AUTHORITY` enrollment, `run_journal_authority_requested` field, authority-state resolution, authoritative write path in `_write_json` | 7 |
| `src/brigade/run_resume.py` | Wrap the two `_resume_locked` receipt writes so a bounded `LifecycleJournalError` becomes `RetainRunLockError` | 7 |
| `tests/test_run_lifecycle.py`, `tests/test_aboyeur.py`, `tests/test_run_resume.py`, `tests/test_run_shadow.py` | Enrollment, first-write parity, no-downgrade, fail-closed, lock-retention, write-order, legacy-unchanged assertions | 7 |
| `scripts/measure_run_journal.py` | Stdlib-only 1,000-run measurement harness exposing `measure_runs(runs, root)` and `write_report(report, output)` plus a `--runs`/`--output` CLI | 8 |
| `tests/test_run_journal_measurement.py` | Harness schema, p50/p95/p99/max, environment metadata, no-threshold, `write_report`, and CLI assertions | 8 |

`src/brigade/runs_cmd.py` is intentionally absent from this table: slice 6
makes no source change to it. `runs_cmd._recover_from_checkpoint` already maps
a `CheckpointError` to exit 2, and the new `projection` category rides that
existing branch unchanged.

Module ownership across the eight sequential tasks. Each task has exclusive
write ownership for its stage.

- Task 1: `src/brigade/run_events.py`, `src/brigade/run_projector.py`. `tests/test_run_events.py`, `tests/test_run_projector.py`, `tests/test_run_checkpoint.py` (the closed checkpoint payload-allowlist assertion in `test_checkpoint_event_type_registered_with_closed_payload_keys` only), `tests/test_run_shadow.py` (the `test_gate_reason_coverage` zero-comparisons projector-version bump and the renamed `test_current_projector_version_artifact_with_checkpoint_tail_reads_ready_when_bytes_match` only, since both currently assert the slice-5 projector version 2 that this task bumps to 3).
- Task 2: `src/brigade/run_lifecycle.py`. `tests/test_run_lifecycle.py`, `tests/test_run_shadow.py` (the `test_unmapped_status_after_handoff_is_lag` shadow expectation only, since `dry-run`, `incomplete`, and `artifact-collection` become mapped).
- Task 3: `src/brigade/run_checkpoint.py`. `tests/test_run_checkpoint.py`.
- Task 4: `src/brigade/run_shadow.py`. `tests/test_run_shadow.py`.
- Task 5: `src/brigade/run_checkpoint.py` (recovery branch only). `tests/test_runs_cmd.py`, `tests/test_run_checkpoint.py`, `tests/test_runguard.py`.
- Task 6: `src/brigade/doctor.py`. `tests/test_doctor.py`.
- Task 7: `src/brigade/aboyeur.py`, `src/brigade/run_resume.py`, `src/brigade/run_shadow.py` (live-writer contract documentation only). `tests/test_run_lifecycle.py`, `tests/test_aboyeur.py`, `tests/test_run_resume.py`, `tests/test_run_shadow.py`.
- Task 8: `scripts/measure_run_journal.py`, `tests/test_run_journal_measurement.py`.

Each task lists its focused verification, wrapped as a `brigade work verify run`
invocation so the receipt is captured and attributed. The closeout runs the
full `./scripts/verify` gate (ruff lint, ruff format check, version-sync check,
mypy, full pytest with the coverage floor) through Brigade. Per the workspace
`AGENTS.md`, report the actual result and paste any failure verbatim. Never
claim success you did not observe.

Named exceptions and categories this slice introduces or reuses. Projector
errors: `ProjectionError` and its subclasses `UnknownSnapshotFieldError`,
`UnmappedEventTypeError`, `EventChainError`, `EventPayloadError`,
`ProjectionInputError`, `SnapshotEncodingError` (all unchanged, defined in
`run_projector`). Checkpoint errors: `CheckpointError` with categories
`payload-shape`, `payload-keys`, `media-type`, `privacy-class`, `sha256`,
`byte-size`, `path`, `paired-event-type`, `open-fd`, `fstat`, `not-regular`,
`link-count`, `size-mismatch`, `read`, `close`, `digest-mismatch`, `utf8`,
`json-object`, `writer-bytes`, `mkdir`, `temp-create`, `chmod`, `io`, `link`,
`dir-fsync`, `unlink`, `collision-unsafe`, `collision-mismatch` (existing),
plus the new `body-kind` and `base-stripped-requests` categories added in task
3 when `validate_checkpoint` and the stripping helper reject an unknown or
under-requested base-stripped body, plus the new `projection` category added
in task 5 when `recover_from_checkpoint` wraps a `ProjectionError` before it
crosses the `runs_cmd` boundary, plus `no-journal`, `journal-read`, `partial-tail`, `chain`, `empty-journal`,
`no-checkpoint`, `run-id-mismatch`, `uncovered-tail`, `preserve-corrupt`,
`restore-write`, `path-resolve` (existing recovery categories). Lifecycle
errors: `LifecycleJournalError` (existing, bounded). Journal errors:
`RunJournalError` and `ChainIntegrityError` (existing). Readiness reasons:
`REASON_NO_JOURNAL`, `REASON_JOURNAL_UNREADABLE`, `REASON_NO_EVIDENCE`,
`REASON_EVIDENCE_UNREADABLE`, `REASON_EVIDENCE_SCHEMA_MISMATCH`,
`REASON_JOURNAL_AHEAD`, `REASON_NO_COMPARISONS`, `REASON_MISMATCH_RECORDED`,
`REASON_ERROR_RECORDED`, `REASON_STATUS_LAG_CURRENT` (existing), plus the new
`REASON_EVIDENCE_PROJECTOR_VERSION_STALE` added in task 4.

## Task 1: New event type, widened payload rules, PROJECTOR_VERSION 3

**Files:**
- Modify: `src/brigade/run_events.py:71-98` (EVENT_TYPES), `src/brigade/run_projector.py:29` (PROJECTOR_VERSION), `src/brigade/run_projector.py:44-100` (PRESERVED_FIELDS), `src/brigade/run_projector.py:106-122` (EVENT_STATUS, _PAYLOAD_STATUS_RULES)
- Test: `tests/test_run_events.py`, `tests/test_run_projector.py`, `tests/test_run_checkpoint.py`

Task 3 depends on this task's registry update: `run_checkpoint.write_checkpoint` builds the `run.snapshot.checkpointed` payload through `run_events.build_event`, which enforces the closed `EVENT_TYPES` allowlist. The `body_kind` key this task adds to the allowlist is the same key task 3 sets when it calls `write_checkpoint(..., body_kind="base-stripped")`. Task 3 cannot land its stripped-base write path until this task widens the allowlist, or every `base-stripped` checkpoint append raises `CanonicalizationError` on the unknown `body_kind` key.

- [x] Write the failing tests. Append to `tests/test_run_events.py`:

```python
def test_event_types_registry_includes_run_artifact_collection_started():
    assert "run.artifact_collection.started" in run_events.EVENT_TYPES
    assert run_events.EVENT_TYPES["run.artifact_collection.started"] == frozenset({"detail"})


def test_checkpoint_event_payload_allowlist_includes_optional_body_kind():
    assert run_events.EVENT_TYPES["run.snapshot.checkpointed"] == frozenset(
        {"path", "sha256", "media_type", "byte_size", "privacy_class", "paired_event_type", "body_kind"}
    )


def test_build_checkpoint_event_accepts_base_stripped_payload_with_body_kind():
    event = run_events.build_event(
        run_id=RUN_ID,
        sequence=1,
        event_type="run.snapshot.checkpointed",
        payload={
            "path": "events/recovery-checkpoints/aaa.json",
            "sha256": "a" * 64,
            "media_type": "application/json",
            "byte_size": 1,
            "privacy_class": "internal",
            "paired_event_type": "run.planning.started",
            "body_kind": "base-stripped",
        },
        idempotency_key="checkpoint-1",
        recorded_at=RECORDED_AT,
        previous_digest=None,
    )
    assert run_events.validate_event(event) == []
    assert event["payload"]["body_kind"] == "base-stripped"


def test_build_checkpoint_event_accepts_legacy_payload_without_body_kind():
    event = run_events.build_event(
        run_id=RUN_ID,
        sequence=1,
        event_type="run.snapshot.checkpointed",
        payload={
            "path": "events/recovery-checkpoints/aaa.json",
            "sha256": "a" * 64,
            "media_type": "application/json",
            "byte_size": 1,
            "privacy_class": "internal",
            "paired_event_type": "run.planning.started",
        },
        idempotency_key="checkpoint-1",
        recorded_at=RECORDED_AT,
        previous_digest=None,
    )
    assert run_events.validate_event(event) == []
    assert "body_kind" not in event["payload"]
```

Update the existing `test_checkpoint_event_type_registered_with_closed_payload_keys` in `tests/test_run_checkpoint.py` (at `tests/test_run_checkpoint.py:80`) to assert the widened allowlist. The exact replacement body is:

```python
def test_checkpoint_event_type_registered_with_closed_payload_keys():
    assert "run.snapshot.checkpointed" in run_events.EVENT_TYPES
    assert run_events.EVENT_TYPES["run.snapshot.checkpointed"] == frozenset(
        {"path", "sha256", "media_type", "byte_size", "privacy_class", "paired_event_type", "body_kind"}
    )
```

Update the existing `test_full_field_fixture_preserves_deep_equality_and_copies_nested_values` in `tests/test_run_projector.py` (at `tests/test_run_projector.py:423`) for the `PRESERVED_FIELDS` count increase (44 to 45, since `run_journal_authority_requested` joins the set) and the exact `EVENT_STATUS` map change (the new `run.artifact_collection.started` row). The exact replacement body is:

```python
def test_full_field_fixture_preserves_deep_equality_and_copies_nested_values():
    base = _full_base_snapshot()
    assert len(PRESERVED_FIELDS) == 45
    assert DERIVED_FIELDS == {
        "status",
        "projector_version",
        "journal_present",
        "journal_last_sequence",
        "journal_last_event_digest",
    }
    assert EVENT_STATUS == {
        "run.planning.started": "planning",
        "run.dispatch.requested": "dispatching",
        "run.dispatch.completed": "result-processing",
        "run.synthesis.started": "synthesizing",
        "run.synthesis.completed": "handoff",
        "run.artifact_collection.started": "artifact-collection",
    }
    projection = project_run_snapshot(base, [], journal_present=False)
    for field in PRESERVED_FIELDS:
        assert field in projection.snapshot
        assert projection.snapshot[field] == base[field]
    assert projection.snapshot["failure"] is not base["failure"]
    assert projection.snapshot["recovery_history"] is not base["recovery_history"]
    assert projection.snapshot["active_seats"] is not base["active_seats"]
    assert projection.snapshot["code_graph_brief"] is not base["code_graph_brief"]
    assert set(projection.snapshot.keys()) <= OWNED_FIELDS
```

Append to `tests/test_run_projector.py`:

```python
def test_run_completed_with_dry_run_status_derives_dry_run():
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    completed = _build_event(
        2, "run.completed", {"status": "dry-run", "detail": "dry-run"},
        "complete-dry", "2026-07-27T15:30:46.000000Z", created["event_digest"],
    )
    projection = project_run_snapshot(_minimal_base_snapshot(), [created, completed], journal_present=True)
    assert projection.status == "dry-run"


def test_run_completed_with_ok_status_still_derives_ok():
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    completed = _build_event(
        2, "run.completed", {"status": "ok", "detail": "ok"},
        "complete-ok", "2026-07-27T15:30:46.000000Z", created["event_digest"],
    )
    projection = project_run_snapshot(_minimal_base_snapshot(), [created, completed], journal_present=True)
    assert projection.status == "ok"


def test_run_failed_with_incomplete_status_derives_incomplete():
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    failed = _build_event(
        2, "run.failed", {"status": "incomplete", "detail": "incomplete"},
        "failed-inc", "2026-07-27T15:30:46.000000Z", created["event_digest"],
    )
    projection = project_run_snapshot(_minimal_base_snapshot(), [created, failed], journal_present=True)
    assert projection.status == "incomplete"


def test_run_failed_with_failed_and_timeout_still_derive():
    for status in ("failed", "timeout"):
        created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
        failed = _build_event(
            2, "run.failed", {"status": status, "detail": status},
            f"failed-{status}", "2026-07-27T15:30:46.000000Z", created["event_digest"],
        )
        projection = project_run_snapshot(_minimal_base_snapshot(), [created, failed], journal_present=True)
        assert projection.status == status


def test_run_artifact_collection_started_derives_artifact_collection():
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    artifact = _build_event(
        2, "run.artifact_collection.started", {"detail": "artifact-collection"},
        "artifact-1", "2026-07-27T15:30:46.000000Z", created["event_digest"],
    )
    projection = project_run_snapshot(_minimal_base_snapshot(), [created, artifact], journal_present=True)
    assert projection.status == "artifact-collection"


def test_run_completed_with_unknown_payload_status_raises_event_payload_error():
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    completed = _build_event(
        2, "run.completed", {"status": "bogus", "detail": "bogus"},
        "complete-bogus", "2026-07-27T15:30:46.000000Z", created["event_digest"],
    )
    with pytest.raises(EventPayloadError):
        project_run_snapshot(_minimal_base_snapshot(), [created, completed], journal_present=True)


def test_run_failed_with_unknown_payload_status_raises_event_payload_error():
    created = _build_event(1, "run.created", {"status": "started"}, "create-1", RECORDED_AT, None)
    failed = _build_event(
        2, "run.failed", {"status": "bogus", "detail": "bogus"},
        "failed-bogus", "2026-07-27T15:30:46.000000Z", created["event_digest"],
    )
    with pytest.raises(EventPayloadError):
        project_run_snapshot(_minimal_base_snapshot(), [created, failed], journal_present=True)
```

Rename the existing `test_projector_version_is_two` (at `tests/test_run_projector.py:541`) to `test_projector_version_is_three` and update its body to assert `PROJECTOR_VERSION == 3` and `projection.snapshot["projector_version"] == 3`. The renamed body is:

```python
def test_projector_version_is_three():
    projection = project_run_snapshot(_minimal_base_snapshot(), [], journal_present=False)
    assert PROJECTOR_VERSION == 3
    assert projection.snapshot["projector_version"] == 3
```

No second `test_projector_version_is_three` definition is appended. The rename above is the only definition.

Update the existing `test_gate_reason_coverage` in `tests/test_run_shadow.py` (at `tests/test_run_shadow.py:565`) so the zero-comparisons artifact reaches `REASON_NO_COMPARISONS` instead of short-circuiting on the projector-version check. The slice-5 readiness gate folds a stale `projector_version` into `REASON_EVIDENCE_SCHEMA_MISMATCH`, so once this task bumps `PROJECTOR_VERSION` to 3 the zero-comparisons branch below never runs and the assertion fails (observed in Brigade failure receipt `20260730-074819-work-verify-e0e0f2`). The exact replacement body is:

```python
def test_gate_reason_coverage(tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    # No journal at all.
    assert REASON_NO_JOURNAL in run_shadow.check_projection_readiness(run_dir).reasons

    # Journal present, no artifact.
    run_dir.joinpath("events").mkdir()
    run_lifecycle._journal_path(run_dir).write_text("")  # empty journal file
    report = run_shadow.check_projection_readiness(run_dir)
    assert REASON_NO_EVIDENCE in report.reasons

    # Wrong schema.
    localio.write_json(
        run_shadow.shadow_artifact_path(run_dir),
        {
            "schema": "brigade.other.v1",
            "schema_version": 1,
            "run_id": _RUN_ID,
            "projector_version": run_projector.PROJECTOR_VERSION,
            "comparisons": 1,
            "matches": 1,
            "mismatches": 0,
            "lags": 0,
            "errors": 0,
            "last_outcome": "match",
            "last_compared_sequence": None,
            "last_compared_event_digest": None,
            "last_shadow_digest": None,
            "last_projected_digest": None,
            "last_differing_fields": None,
            "last_error_category": None,
            "last_recorded_at": None,
            "recent_records": [],
        },
    )
    assert REASON_EVIDENCE_SCHEMA_MISMATCH in run_shadow.check_projection_readiness(run_dir).reasons

    # Wrong run_id.
    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    data["schema"] = "brigade.run_shadow.v1"
    data["run_id"] = "other"
    data["projector_version"] = run_projector.PROJECTOR_VERSION
    run_shadow.shadow_artifact_path(run_dir).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    assert REASON_EVIDENCE_SCHEMA_MISMATCH in run_shadow.check_projection_readiness(run_dir).reasons

    # Zero comparisons. Set projector_version to the current PROJECTOR_VERSION
    # so the artifact passes the version door and reaches REASON_NO_COMPARISONS.
    # Task 4 later owns genuinely stale v2 evidence and its own quarantine test.
    data["run_id"] = _RUN_ID
    data["schema"] = "brigade.run_shadow.v1"
    data["comparisons"] = 0
    data["projector_version"] = run_projector.PROJECTOR_VERSION
    run_shadow.shadow_artifact_path(run_dir).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    assert REASON_NO_COMPARISONS in run_shadow.check_projection_readiness(run_dir).reasons
```

Rename the existing `test_version_two_artifact_with_checkpoint_tail_reads_ready_when_bytes_match` (at `tests/test_run_shadow.py:1110`) to `test_current_projector_version_artifact_with_checkpoint_tail_reads_ready_when_bytes_match` and update its body to assert the current projector version. The renamed body is:

```python
def test_current_projector_version_artifact_with_checkpoint_tail_reads_ready_when_bytes_match(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")

    events = _events(run_dir)
    assert events[-1].event_type == "run.created"
    assert events[0].event_type == "run.snapshot.checkpointed"

    artifact = run_shadow.shadow_artifact_path(run_dir)
    data = json.loads(artifact.read_text())
    assert data["projector_version"] == run_projector.PROJECTOR_VERSION
    assert data["last_compared_sequence"] == 2
    assert data["last_outcome"] == "match"

    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is True
    assert report.reasons == ()
```

Genuinely stale v2 evidence is owned by task 4 (`test_version_two_artifact_is_stale_not_permanently_closed` and the quarantine tests). Task 1 only retargets these two current-version expectations to the new `PROJECTOR_VERSION` 3 so the slice-5 readiness gate keeps reaching the named reason branches after the bump.

- [x] Run it, watch it fail: `brigade work verify run --target . --command ".venv/bin/python -m pytest -q tests/test_run_events.py tests/test_run_projector.py tests/test_run_checkpoint.py tests/test_run_shadow.py::test_gate_reason_coverage tests/test_run_shadow.py::test_version_two_artifact_with_checkpoint_tail_reads_ready_when_bytes_match" --capture brigade-work` - expect FAIL, `AssertionError` on the new registry and version assertions, `CanonicalizationError` on `test_build_checkpoint_event_accepts_base_stripped_payload_with_body_kind` (the `body_kind` key is not yet in the allowlist), `AssertionError` on the widened allowlist in `test_checkpoint_event_type_registered_with_closed_payload_keys`, `AssertionError` on `len(PRESERVED_FIELDS) == 45` and the six-entry `EVENT_STATUS` map in `test_full_field_fixture_preserves_deep_equality_and_copies_nested_values`, plus the renamed `test_projector_version_is_three` failing on `assert PROJECTOR_VERSION == 3`, plus `test_gate_reason_coverage` failing on the zero-comparisons assertion because the stale `projector_version: 2` artifact short-circuits to `REASON_EVIDENCE_SCHEMA_MISMATCH` before reaching `REASON_NO_COMPARISONS`, plus `test_version_two_artifact_with_checkpoint_tail_reads_ready_when_bytes_match` failing on `assert data["projector_version"] == 2` because the live writer now records 3. The two `test_run_shadow.py` failures are observed verbatim in Brigade failure receipt `20260730-074819-work-verify-e0e0f2`.
- [x] Implement the minimal change. In `src/brigade/run_events.py` add the row to `EVENT_TYPES`:

```python
    "run.artifact_collection.started": frozenset({"detail"}),
```

and widen the `run.snapshot.checkpointed` allowlist to include `body_kind`:

```python
    "run.snapshot.checkpointed": frozenset(
        {"path", "sha256", "media_type", "byte_size", "privacy_class", "paired_event_type", "body_kind"}
    ),
```

In `src/brigade/run_projector.py` set `PROJECTOR_VERSION: int = 3`. Add `"run_journal_authority_requested",` to the `PRESERVED_FIELDS` set (it joins `lifecycle_journal_requested`). Add the static row `"run.artifact_collection.started": "artifact-collection",` to `EVENT_STATUS`. Widen the two payload rules:

```python
    "run.completed": (frozenset({"ok", "dry-run"}), None),
    "run.failed": (frozenset({"failed", "timeout", "incomplete"}), None),
```

`DERIVED_FIELDS`, `OWNED_FIELDS`, and `encode_snapshot_bytes` do not change. `run_journal_authority_requested` joins `PRESERVED_FIELDS` and therefore `OWNED_FIELDS`.

- [x] Run to green: `brigade work verify run --target . --command ".venv/bin/python -m pytest -q tests/test_run_events.py tests/test_run_projector.py tests/test_run_checkpoint.py tests/test_run_shadow.py::test_gate_reason_coverage tests/test_run_shadow.py::test_current_projector_version_artifact_with_checkpoint_tail_reads_ready_when_bytes_match" --capture brigade-work` - expect PASS, all collected tests pass. Keep `test_empty_events_no_journal_preserves_base_status_and_deep_copies` green unchanged.
- [x] Commit: `git add src/brigade/run_events.py src/brigade/run_projector.py tests/test_run_events.py tests/test_run_projector.py tests/test_run_checkpoint.py tests/test_run_shadow.py && git commit -m "feat(run-projector): map dry-run incomplete artifact-collection and bump PROJECTOR_VERSION to 3"`

## Task 2: Lifecycle mapping for the three previously unmapped statuses

**Files:**
- Modify: `src/brigade/run_lifecycle.py:98-109` (STATUS_EVENT_TYPE)
- Test: `tests/test_run_lifecycle.py`, `tests/test_run_shadow.py`

The three statuses this task maps (`dry-run`, `incomplete`, `artifact-collection`) were previously unmapped, so four existing tests assert the unmapped behavior and must change in this same commit: `test_unmapped_status_after_handoff_is_lag` in `tests/test_run_shadow.py`, and `test_unmapped_intermediate_status_still_appends_the_second_a`, `test_unmapped_status_writes_run_json_without_status_event`, and `test_unmapped_activated_write_checkpoint_seq1_no_status_event` in `tests/test_run_lifecycle.py`. Each is renamed and rewritten below so no test keeps asserting unmapped behavior for a status this task maps.

- [ ] Write the failing tests. Append to `tests/test_run_lifecycle.py`:

```python
def test_status_event_type_maps_dry_run_to_run_completed():
    assert run_lifecycle.STATUS_EVENT_TYPE["dry-run"] == "run.completed"


def test_status_event_type_maps_incomplete_to_run_failed():
    assert run_lifecycle.STATUS_EVENT_TYPE["incomplete"] == "run.failed"


def test_status_event_type_maps_artifact_collection_to_run_artifact_collection_started():
    assert run_lifecycle.STATUS_EVENT_TYPE["artifact-collection"] == "run.artifact_collection.started"


def test_record_lifecycle_transition_appends_dry_run_event(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "dry-run")
    status_events = _status_events(run_dir)
    assert [e.event_type for e in status_events] == ["run.created", "run.completed"]
    assert status_events[-1].payload["status"] == "dry-run"


def test_record_lifecycle_transition_appends_incomplete_event(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "incomplete")
    status_events = _status_events(run_dir)
    assert [e.event_type for e in status_events] == ["run.created", "run.failed"]
    assert status_events[-1].payload["status"] == "incomplete"


def test_record_lifecycle_transition_appends_artifact_collection_event(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "artifact-collection")
    status_events = _status_events(run_dir)
    assert [e.event_type for e in status_events] == ["run.created", "run.artifact_collection.started"]


def test_dry_run_incomplete_artifact_collection_no_longer_skip(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    for status in ("dry-run", "incomplete", "artifact-collection"):
        _write_run_json_locked(repo, run_dir, status)
    status_events = _status_events(run_dir)
    assert [e.event_type for e in status_events] == [
        "run.created",
        "run.completed",
        "run.failed",
        "run.artifact_collection.started",
    ]
```

Rename and rewrite the four existing tests that assert unmapped behavior for statuses this task maps. In `tests/test_run_shadow.py`, replace `test_unmapped_status_after_handoff_is_lag` (at `tests/test_run_shadow.py:259`) with `test_artifact_collection_after_handoff_is_mapped_no_lag`. The exact replacement body is:

```python
def test_artifact_collection_after_handoff_is_mapped_no_lag(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    chain = [
        "started",
        "planning",
        "dispatching",
        "result-processing",
        "synthesizing",
        "handoff",
        "artifact-collection",
    ]

    _write_run_json(run_dir, "started")
    for status in chain:
        _write_run_json_locked(repo, run_dir, status)

    data = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    # artifact-collection is now mapped to run.artifact_collection.started, so
    # the final comparison matches and no lag is recorded.
    assert data["lags"] == 0
    assert data["mismatches"] == 0
    assert data["errors"] == 0
    assert data["last_outcome"] == "match"
    # The mapped artifact-collection write appends a checkpoint event and a
    # run.artifact_collection.started status event, so the last compared
    # sequence is the status event (seq 14), not the checkpoint (seq 13).
    assert data["last_compared_sequence"] == 14
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is True
    assert REASON_STATUS_LAG_CURRENT not in report.reasons
```

In `tests/test_run_lifecycle.py`, replace `test_unmapped_intermediate_status_still_appends_the_second_a` (at `tests/test_run_lifecycle.py:451`) with `test_mapped_intermediate_artifact_collection_appends_status_event`. The exact replacement body is:

```python
def test_mapped_intermediate_artifact_collection_appends_status_event(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "dispatching")
    # artifact-collection is now mapped: it appends both a checkpoint event
    # and a run.artifact_collection.started status event.
    _write_run_json_locked(repo, run_dir, "artifact-collection")
    # A-mapped-B-A: the second dispatching is a real transition from the
    # artifact-collection snapshot and must append even though the journal
    # tail status payload matches.
    _write_run_json_locked(repo, run_dir, "dispatching")

    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["status"] == "dispatching"
    assert [e.event_type for e in _status_events(run_dir)] == [
        "run.created",
        "run.dispatch.requested",
        "run.artifact_collection.started",
        "run.dispatch.requested",
    ]
    events = _events(run_dir)
    # The second "dispatching" produces identical run.json bytes to the first,
    # so its checkpoint replays and only the status event appends. The
    # artifact-collection checkpoint is now paired with run.artifact_collection.started.
    assert [e.event_type for e in events] == [
        "run.snapshot.checkpointed",
        "run.created",
        "run.snapshot.checkpointed",
        "run.dispatch.requested",
        "run.snapshot.checkpointed",
        "run.artifact_collection.started",
        "run.dispatch.requested",
    ]
    assert [e.sequence for e in events] == [1, 2, 3, 4, 5, 6, 7]
    # The second run.dispatch.requested links to the artifact-collection
    # status event immediately before it, not to the checkpoint.
    assert events[6].previous_digest == events[5].event_digest
    # The artifact-collection checkpoint is paired with its mapped status event.
    assert events[4].payload["paired_event_type"] == "run.artifact_collection.started"
```

In `tests/test_run_lifecycle.py`, replace `test_unmapped_status_writes_run_json_without_status_event` (at `tests/test_run_lifecycle.py:574`) with `test_mapped_dry_run_and_artifact_collection_write_status_events`. The exact replacement body is:

```python
def test_mapped_dry_run_and_artifact_collection_write_status_events(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)

    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    _write_run_json_locked(repo, run_dir, "dry-run")
    _write_run_json_locked(repo, run_dir, "artifact-collection")

    assert (run_dir / "run.json").is_file()
    # dry-run maps to run.completed and artifact-collection maps to
    # run.artifact_collection.started, so each appends a status event in
    # addition to its checkpoint event.
    assert [e.event_type for e in _status_events(run_dir)] == [
        "run.created",
        "run.completed",
        "run.artifact_collection.started",
    ]
    assert len(_checkpoint_events(run_dir)) == 3
```

In `tests/test_run_lifecycle.py`, replace `test_unmapped_activated_write_checkpoint_seq1_no_status_event` (at `tests/test_run_lifecycle.py:753`) with `test_mapped_activated_artifact_collection_writes_status_event`. The exact replacement body is:

```python
def test_mapped_activated_artifact_collection_writes_status_event(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")  # activate + run.created
    # artifact-collection is now mapped: checkpoint event appends paired
    # with run.artifact_collection.started, and that status event appends too.
    _write_run_json_locked(repo, run_dir, "artifact-collection")

    events = _events(run_dir)
    assert [e.event_type for e in events] == [
        "run.snapshot.checkpointed",
        "run.created",
        "run.snapshot.checkpointed",
        "run.artifact_collection.started",
    ]
    assert [e.sequence for e in events] == [1, 2, 3, 4]
    assert events[2].payload["paired_event_type"] == "run.artifact_collection.started"
    assert [e.event_type for e in _status_events(run_dir)] == [
        "run.created",
        "run.artifact_collection.started",
    ]
```

- [ ] Run it, watch it fail: `brigade work verify run --target . --command ".venv/bin/python -m pytest -q tests/test_run_lifecycle.py tests/test_run_shadow.py" --capture brigade-work` - expect FAIL, `AssertionError` on the `status_events` assertions because `STATUS_EVENT_TYPE.get(status)` returns `None` for `dry-run`, `incomplete`, and `artifact-collection` so no status event appends and the appended-event list does not match, plus the four renamed tests fail because the unmapped behavior they no longer assert is gone and the mapped behavior they now assert is absent. No `KeyError` is raised: the slice-5 lookup uses `.get(status)` and returns `None`, so the failures are assertion failures from missing status events, never `KeyError`.
- [ ] Implement the minimal change. In `src/brigade/run_lifecycle.py` add the three rows to `STATUS_EVENT_TYPE`:

```python
    "dry-run": "run.completed",
    "incomplete": "run.failed",
    "artifact-collection": "run.artifact_collection.started",
```

`_allowlisted_payload` already builds the payload from the closed per-type allowlist, so `run.completed` and `run.failed` carry `payload.status` and `run.artifact_collection.started` carries `detail`. No change to the activation model, the skip rules for same-status writes, or the bounded-failure wrapping.

- [ ] Run to green: `brigade work verify run --target . --command ".venv/bin/python -m pytest -q tests/test_run_lifecycle.py tests/test_run_shadow.py" --capture brigade-work` - expect PASS. Keep `test_first_activated_mapped_write_checkpoint_seq1_then_status_seq2` green unchanged.
- [ ] Commit: `git add src/brigade/run_lifecycle.py tests/test_run_lifecycle.py tests/test_run_shadow.py && git commit -m "feat(run-lifecycle): map dry-run incomplete and artifact-collection status transitions"`

## Task 3: Stripped checkpoint base and body_kind payload key

**Files:**
- Modify: `src/brigade/run_checkpoint.py:37-39` (`_CHECKPOINT_PAYLOAD_KEYS`), `src/brigade/run_checkpoint.py:70-131` (`_validate_payload`), `src/brigade/run_checkpoint.py:503-528` (`_checkpoint_payload`, `_checkpoint_idempotency_key`), `src/brigade/run_checkpoint.py:531-601` (`write_checkpoint`), `src/brigade/run_checkpoint.py:191-266` (`validate_checkpoint`)
- Test: `tests/test_run_checkpoint.py`

- [ ] Write the failing tests. Append to `tests/test_run_checkpoint.py`:

```python
def _stripped_base_obj(status: str = "planning") -> dict:
    return {
        "schema": "brigade.run.v1",
        "schema_version": 1,
        "status": status,
        "task": "demo",
        "run_journal_authority_requested": True,
        "lifecycle_journal_requested": True,
    }


def test_validate_checkpoint_treats_missing_body_kind_as_legacy_full(tmp_path):
    run_dir = _run_dir(tmp_path)
    base = _stripped_base_obj()
    base_bytes = _writer_bytes(base)
    _place_checkpoint_file(run_dir, base_bytes)
    payload = _checkpoint_payload(base_bytes, paired_event_type="run.planning.started")
    assert run_checkpoint.validate_checkpoint(run_dir, payload) == base_bytes


def test_validate_checkpoint_rejects_unknown_body_kind(tmp_path):
    run_dir = _run_dir(tmp_path)
    base = _stripped_base_obj()
    base_bytes = _writer_bytes(base)
    _place_checkpoint_file(run_dir, base_bytes)
    payload = _checkpoint_payload(base_bytes, paired_event_type="run.planning.started")
    payload["body_kind"] = "bogus"
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "body-kind"


def test_validate_base_stripped_requires_authority_and_lifecycle_requests(tmp_path):
    run_dir = _run_dir(tmp_path)
    base = _stripped_base_obj()
    del base["run_journal_authority_requested"]
    base_bytes = _writer_bytes(base)
    _place_checkpoint_file(run_dir, base_bytes)
    payload = _checkpoint_payload(base_bytes, paired_event_type="run.planning.started")
    payload["body_kind"] = "base-stripped"
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.validate_checkpoint(run_dir, payload)
    assert excinfo.value.category == "base-stripped-requests"


def test_checkpoint_sha256_is_over_stripped_body(tmp_path):
    run_dir = _run_dir(tmp_path)
    full = {"schema": "brigade.run.v1", "status": "planning", "task": "demo",
            "projector_version": 3, "journal_present": True,
            "journal_last_sequence": 2, "journal_last_event_digest": "a" * 64,
            "run_journal_authority_requested": True,
            "lifecycle_journal_requested": True}
    full_bytes = _writer_bytes(full)
    stripped = {k: v for k, v in full.items() if k not in {
        "projector_version", "journal_present", "journal_last_sequence", "journal_last_event_digest"}}
    stripped_bytes = _writer_bytes(stripped)
    assert run_checkpoint._strip_journal_metadata_from_base(full_bytes) == stripped_bytes
    assert hashlib.sha256(stripped_bytes).hexdigest() != hashlib.sha256(full_bytes).hexdigest()
    _place_checkpoint_file(run_dir, stripped_bytes)
    payload = _checkpoint_payload(stripped_bytes, paired_event_type="run.planning.started")
    payload["body_kind"] = "base-stripped"
    assert run_checkpoint.validate_checkpoint(run_dir, payload) == stripped_bytes


def test_strip_journal_metadata_helper_is_bounded_on_non_object(tmp_path):
    already_stripped = _writer_bytes({"schema": "brigade.run.v1"})  # valid JSON object, but test non-object below
    arr_bytes = b"[1, 2, 3]\n"
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint._strip_journal_metadata_from_base(arr_bytes)
    assert excinfo.value.category == "json-object"
    bad_utf8 = b"\xff\xfe\n"
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint._strip_journal_metadata_from_base(bad_utf8)
    assert excinfo.value.category == "utf8"
    # idempotent: stripping an already-stripped body is a no-op
    assert run_checkpoint._strip_journal_metadata_from_base(already_stripped) == already_stripped


def test_legacy_full_checkpoint_keeps_slice5_idempotency_key():
    sha = "a" * 64
    assert run_checkpoint._checkpoint_idempotency_key(sha, paired_event_type="run.planning.started") == f"checkpoint:{sha}:run.planning.started"
    assert run_checkpoint._checkpoint_idempotency_key(sha, paired_event_type=None) == f"checkpoint:{sha}:none"


def test_base_stripped_checkpoint_key_includes_body_kind_and_is_bounded():
    sha = "a" * 64
    key = run_checkpoint._checkpoint_idempotency_key(sha, paired_event_type="run.planning.started", body_kind="base-stripped")
    assert key == f"checkpoint:base-stripped:{sha}:run.planning.started"
    assert len(key) <= run_events.MAX_IDEMPOTENCY_KEY_LEN


def test_write_checkpoint_strips_journal_metadata_from_base(enabled, tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        full = {"schema": "brigade.run.v1", "status": "planning", "task": "demo",
                "projector_version": 3, "journal_present": True,
                "journal_last_sequence": 2, "journal_last_event_digest": "a" * 64,
                "run_journal_authority_requested": True,
                "lifecycle_journal_requested": True}
        full_bytes = _writer_bytes(full)
        event = run_checkpoint.write_checkpoint(
            run_dir, full_bytes, workspace=workspace,
            paired_event_type="run.planning.started", body_kind="base-stripped",
        )
    assert event is not None
    assert event.payload["body_kind"] == "base-stripped"
    sha = event.payload["sha256"]
    stored = run_checkpoint.checkpoint_path(run_dir, sha).read_bytes()
    stored_obj = json.loads(stored.decode("utf-8"))
    for absent in ("projector_version", "journal_present", "journal_last_sequence", "journal_last_event_digest"):
        assert absent not in stored_obj
    assert stored_obj["status"] == "planning"
    assert stored_obj["run_journal_authority_requested"] is True


def test_write_checkpoint_legacy_full_omits_body_kind_from_payload(enabled, tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        base = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
        event = run_checkpoint.write_checkpoint(
            run_dir, _writer_bytes(base), workspace=workspace, paired_event_type="run.planning.started",
        )
    assert event is not None
    assert "body_kind" not in event.payload
```

- [ ] Run it, watch it fail: `brigade work verify run --target . --command ".venv/bin/python -m pytest -q tests/test_run_checkpoint.py" --capture brigade-work` - expect FAIL, `TypeError: write_checkpoint() got an unexpected keyword argument 'body_kind'` on the stripped-base write, plus `AssertionError` on the key-shape and idempotency assertions.
- [ ] Implement the minimal change. In `src/brigade/run_checkpoint.py` add `"body_kind"` to `_CHECKPOINT_PAYLOAD_KEYS`. Define the exact stripping constant and helper before any SHA, payload, or publish work:

```python
_JOURNAL_METADATA_FIELDS = (
    "projector_version",
    "journal_present",
    "journal_last_sequence",
    "journal_last_event_digest",
)


def _strip_journal_metadata_from_base(base_bytes: bytes) -> bytes:
    """Remove exactly the four self-referential journal-metadata fields from a
    canonical base body, retaining ``status`` and every present preserved
    field. Called by ``write_checkpoint`` BEFORE the sha256, payload, or
    publish step for a ``base-stripped`` body. Bounded: a non-UTF-8 body raises
    ``CheckpointError(category="utf8")``, a non-JSON body raises
    ``CheckpointError(category="json-object")``, and a non-object JSON value
    raises ``CheckpointError(category="json-object")``. Fields absent from the
    input are skipped (the strip is idempotent). The returned bytes use the
    house canonical form.
    """
    try:
        text = base_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CheckpointError(_bound("base-stripped body must be UTF-8"), category="utf8") from exc
    try:
        obj = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CheckpointError(_bound("base-stripped body must be JSON"), category="json-object") from exc
    if not isinstance(obj, dict):
        raise CheckpointError(_bound("base-stripped body must be a JSON object"), category="json-object")
    stripped = {k: v for k, v in obj.items() if k not in _JOURNAL_METADATA_FIELDS}
    return (json.dumps(stripped, indent=2, sort_keys=True) + "\n").encode("utf-8")
```

In `_validate_payload`, accept the payload when `keys == _CHECKPOINT_PAYLOAD_KEYS` (with `body_kind`) or `keys == _CHECKPOINT_PAYLOAD_KEYS - {"body_kind"}` (legacy slice-5 set). When `body_kind` is present it must equal `"base-stripped"` or raise `CheckpointError(_bound("body_kind must be base-stripped"), category="body-kind")`. For a `base-stripped` body, after the existing writer-canonical check, parse the object and require `run_journal_authority_requested is True` and `lifecycle_journal_requested is True`, and require that none of `projector_version`, `journal_present`, `journal_last_sequence`, `journal_last_event_digest` are present, or raise `CheckpointError(_bound("base-stripped body must carry both durable requests and no journal metadata"), category="base-stripped-requests")`.

In `write_checkpoint`, the `base-stripped` order is exact:

1. Verify that incoming `base_bytes` use the house canonical form. Raise `writer-bytes` on mismatch.
2. Call `_strip_journal_metadata_from_base(base_bytes)` to produce `stripped_bytes`.
3. Re-verify that `stripped_bytes` use the house canonical form.
4. Parse `stripped_bytes` and run the `base-stripped-requests` request and absence checks from `_validate_payload`.
5. Compute `sha256` over `stripped_bytes`.
6. Build `_checkpoint_payload(...)` from `stripped_bytes`, `sha256`, `paired_event_type`, and `body_kind="base-stripped"`.
7. Build the idempotency key.
8. Publish the checkpoint file and append the event.

For a `legacy-full` body (absent `body_kind`), the slice-5 path runs unchanged. The SHA, payload, and publish all use the incoming bytes verbatim, and no stripping helper runs.

Update `_checkpoint_payload` to accept `body_kind: str | None = None` and include the key only when it is not `None`. Update `_checkpoint_idempotency_key` to accept `body_kind: str | None = None` and, when it is `"base-stripped"`, build `f"{_CHECKPOINT_IDEMPOTENCY_PREFIX}:base-stripped:{sha}:{paired}"` with the same paired-tail budget logic bounded to `MAX_IDEMPOTENCY_KEY_LEN`. Update `write_checkpoint` to accept `body_kind: str | None = None` and pass it through to `_checkpoint_payload` and `_checkpoint_idempotency_key`. The default call (no `body_kind`) keeps the exact slice-5 idempotency key `checkpoint:<sha256>:<paired-event-type-or-none>` and omits the key from the payload.

- [ ] Run to green: `brigade work verify run --target . --command ".venv/bin/python -m pytest -q tests/test_run_checkpoint.py" --capture brigade-work` - expect PASS. Keep `test_write_checkpoint_append_then_replay` and `test_first_activated_mapped_write_checkpoint_seq1_then_status_seq2` green unchanged for the default `legacy-full` path.
- [ ] Commit: `git add src/brigade/run_checkpoint.py tests/test_run_checkpoint.py && git commit -m "feat(run-checkpoint): strip journal metadata from checkpoint base and add body_kind"`

## Task 4: Readiness gate version-door split

**Files:**
- Modify: `src/brigade/run_shadow.py:44-53` (reason constants), `src/brigade/run_shadow.py:364-470` (`record_shadow_comparison`), `src/brigade/run_shadow.py:473-531` (`check_projection_readiness`)
- Test: `tests/test_run_shadow.py`

- [ ] Write the failing tests. Add `REASON_EVIDENCE_PROJECTOR_VERSION_STALE` to the import block at `tests/test_run_shadow.py:15-26`, then append:

```python
def test_version_two_artifact_is_stale_not_permanently_closed(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    artifact = run_shadow.shadow_artifact_path(run_dir)
    data = json.loads(artifact.read_text())
    data["projector_version"] = 2
    artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_EVIDENCE_PROJECTOR_VERSION_STALE in report.reasons
    assert REASON_EVIDENCE_SCHEMA_MISMATCH not in report.reasons


def test_stale_v2_artifact_is_quarantined_before_fresh_v3_evidence(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    artifact = run_shadow.shadow_artifact_path(run_dir)
    data = json.loads(artifact.read_text())
    data["projector_version"] = 2
    data["mismatches"] = 1
    artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    v2_bytes = artifact.read_bytes()
    _write_run_json_locked(repo, run_dir, "planning")
    quarantined = list(run_dir.glob("events/.stale-projector-v2-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == v2_bytes
    fresh = json.loads(artifact.read_text())
    assert fresh["projector_version"] == 3
    assert fresh["mismatches"] == 0
    assert fresh["comparisons"] >= 1


def test_crash_after_stale_quarantine_before_v3_write_is_not_ready(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    artifact = run_shadow.shadow_artifact_path(run_dir)
    data = json.loads(artifact.read_text())
    data["projector_version"] = 2
    artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    import shutil
    target = artifact.with_name(".stale-projector-v2-quarantined")
    shutil.move(str(artifact), str(target))
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_NO_EVIDENCE in report.reasons


def test_stale_v2_cursor_is_gap_baseline_for_first_v3_comparison(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    artifact = run_shadow.shadow_artifact_path(run_dir)
    data = json.loads(artifact.read_text())
    data["projector_version"] = 2
    artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    _write_run_json_locked(repo, run_dir, "planning")
    fresh = json.loads(artifact.read_text())
    assert fresh["projector_version"] == 3
    assert fresh["last_error_category"] != "comparison-gap"


def test_current_v3_mismatch_is_never_reset(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    artifact = run_shadow.shadow_artifact_path(run_dir)
    data = json.loads(artifact.read_text())
    data["mismatches"] = 1
    data["last_outcome"] = "mismatch"
    artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    _write_run_json_locked(repo, run_dir, "planning")
    fresh = json.loads(artifact.read_text())
    assert fresh["mismatches"] >= 1
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_MISMATCH_RECORDED in report.reasons


def test_current_v3_error_is_never_reset(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    artifact = run_shadow.shadow_artifact_path(run_dir)
    data = json.loads(artifact.read_text())
    data["errors"] = 1
    data["last_outcome"] = "error"
    data["last_error_category"] = "journal-unreadable"
    artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    _write_run_json_locked(repo, run_dir, "planning")
    fresh = json.loads(artifact.read_text())
    assert fresh["errors"] >= 1
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_ERROR_RECORDED in report.reasons


def test_evidence_schema_mismatch_now_only_for_schema_or_run_id(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    artifact = run_shadow.shadow_artifact_path(run_dir)
    for field, value in (("schema", "brigade.other.v1"), ("schema_version", 9), ("run_id", "other")):
        data = json.loads(artifact.read_text())
        data[field] = value
        artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        report = run_shadow.check_projection_readiness(run_dir)
        assert report.ready is False
        assert REASON_EVIDENCE_SCHEMA_MISMATCH in report.reasons
        assert REASON_EVIDENCE_PROJECTOR_VERSION_STALE not in report.reasons


def test_shadow_and_readiness_use_checkpoint_journal_bounds(enabled, tmp_path):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _write_run_json(run_dir, "started")
    _write_run_json_locked(repo, run_dir, "started")
    journal = _journal_path(run_dir)
    from brigade import run_checkpoint
    # Parse the legacy snapshot from run.json BEFORE oversizing the journal,
    # matching the actual two-argument record_shadow_comparison signature.
    legacy_snapshot = json.loads((run_dir / "run.json").read_text())
    journal.write_bytes(journal.read_bytes() + b"x" * (run_checkpoint.MAX_JOURNAL_BYTES + 1))
    run_shadow.record_shadow_comparison(run_dir, legacy_snapshot)
    fresh = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    assert fresh["last_error_category"] == "journal-unreadable"
    report = run_shadow.check_projection_readiness(run_dir)
    assert report.ready is False
    assert REASON_JOURNAL_UNREADABLE in report.reasons
```

Update the existing `test_version_one_shadow_artifact_is_stale` (at `tests/test_run_shadow.py:1093`) to assert the version-door split: a version-1 artifact is also stale, so `REASON_EVIDENCE_PROJECTOR_VERSION_STALE` (not `REASON_EVIDENCE_SCHEMA_MISMATCH`) is the reason when the artifact is present, and the next comparison quarantines it before writing fresh v3 evidence.

- [ ] Run it, watch it fail: `brigade work verify run --target . --command ".venv/bin/python -m pytest -q tests/test_run_shadow.py" --capture brigade-work` - expect FAIL, `ImportError` on `REASON_EVIDENCE_PROJECTOR_VERSION_STALE`, plus `AssertionError` on the version-door and quarantine assertions.
- [ ] Implement the minimal change. In `src/brigade/run_shadow.py` add `REASON_EVIDENCE_PROJECTOR_VERSION_STALE = "evidence-projector-version-stale"` to the reason constants. In `check_projection_readiness`, split the `projector_version` check from the schema/schema_version/run_id check: when `data.get("projector_version") != run_projector.PROJECTOR_VERSION`, return `ReadinessReport(ready=False, reasons=(REASON_EVIDENCE_PROJECTOR_VERSION_STALE,))` before the schema check. The schema check now fires only for `schema`, `schema_version`, or `run_id` disagreement. In `record_shadow_comparison`, when the prior artifact has a different `projector_version`, atomically rename the complete prior artifact to a private `.stale-projector-v2-<timestamp>` sibling under `events/` before writing a fresh current-version artifact. Use the quarantined artifact's verified `last_compared_sequence` and `last_compared_event_digest` only as the comparison-gap baseline for the first v3 record, so a normal checkpoint-plus-status advance does not create a false gap and a larger unexplained advance records a fresh `comparison-gap` error. Do not carry any v2 counter or recent record into v3. Never reset a current-version mismatch or error: when the active artifact is at the current version with nonzero `mismatches` or `errors`, the existing slice-4 cumulative behavior is unchanged. Use `run_journal.read_journal_bounded` with the slice-5 `MAX_JOURNAL_BYTES` and `MAX_JOURNAL_EVENTS` limits in both comparison and readiness paths, classifying a bound failure as `journal-unreadable`.

- [ ] Run to green: `brigade work verify run --target . --command ".venv/bin/python -m pytest -q tests/test_run_shadow.py" --capture brigade-work` - expect PASS.
- [ ] Commit: `git add src/brigade/run_shadow.py tests/test_run_shadow.py && git commit -m "feat(run-shadow): quarantine stale projector evidence before fresh comparisons"`

## Task 5: Authority-aware recovery projection

**Files:**
- Modify: `src/brigade/run_checkpoint.py:752-853` (`recover_from_checkpoint`)
- Test: `tests/test_runs_cmd.py`, `tests/test_run_checkpoint.py`, `tests/test_runguard.py`

- [ ] Write the failing tests. Append to `tests/test_run_checkpoint.py`:

```python
def test_recover_from_checkpoint_projects_for_base_stripped_authority_requested(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    from brigade import run_projector
    base = {
        "schema": "brigade.run.v1", "schema_version": 1, "status": "planning",
        "task": "demo", "cwd": str(workspace),
        "run_journal_authority_requested": True,
        "lifecycle_journal_requested": True,
    }
    base_bytes = _writer_bytes(base)
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_checkpoint.write_checkpoint(
            run_dir, base_bytes, workspace=workspace,
            paired_event_type="run.planning.started", body_kind="base-stripped",
        )
        run_journal.append_event(
            _journal_path(run_dir), run_id=RUN_ID, event_type="run.planning.started",
            payload={"detail": "planning"}, idempotency_key="plan-1",
            expected_previous_sequence=1, recorded_at="2026-07-27T15:30:46.000000Z",
        )
    (run_dir / "run.json").unlink()
    repaired = run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert repaired["status"] == "planning"
    assert repaired["task"] == "demo"
    assert repaired["projector_version"] == run_projector.PROJECTOR_VERSION
    assert repaired["journal_present"] is True
    assert "run_journal_authority_requested" in repaired
    restored = (run_dir / "run.json").read_bytes()
    assert restored == _writer_bytes(repaired)


def test_recover_from_checkpoint_uses_checkpoint_request_when_run_json_missing(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    base = {
        "schema": "brigade.run.v1", "schema_version": 1, "status": "planning",
        "task": "demo", "run_journal_authority_requested": True,
        "lifecycle_journal_requested": True,
    }
    base_bytes = _writer_bytes(base)
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_checkpoint.write_checkpoint(
            run_dir, base_bytes, workspace=workspace,
            paired_event_type=None, body_kind="base-stripped",
        )
    (run_dir / "run.json").unlink()
    repaired = run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert repaired["run_journal_authority_requested"] is True


def test_recover_from_checkpoint_restores_verbatim_for_legacy_full(tmp_path):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo"}
    _activated_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").unlink()
    repaired = run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert repaired == run_json_obj
    assert (run_dir / "run.json").read_bytes() == _writer_bytes(run_json_obj)


def test_recover_from_checkpoint_wraps_projection_error_as_checkpoint_error(tmp_path, monkeypatch):
    workspace = _workspace(tmp_path)
    run_dir = _run_dir(tmp_path)
    base = {
        "schema": "brigade.run.v1", "schema_version": 1, "status": "planning",
        "task": "demo", "run_journal_authority_requested": True,
        "lifecycle_journal_requested": True,
    }
    base_bytes = _writer_bytes(base)
    _bootstrap_request(run_dir)
    with runguard.run_lock(workspace, run_dir=run_dir):
        run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace)
        run_checkpoint.write_checkpoint(
            run_dir, base_bytes, workspace=workspace,
            paired_event_type="run.planning.started", body_kind="base-stripped",
        )
    (run_dir / "run.json").unlink()
    from brigade import run_projector
    def raise_projection(*_a, **_kw):
        raise run_projector.EventPayloadError("forged base")
    monkeypatch.setattr(run_projector, "project_run_snapshot", raise_projection)
    with pytest.raises(run_checkpoint.CheckpointError) as excinfo:
        run_checkpoint.recover_from_checkpoint(run_dir, None)
    assert excinfo.value.category == "projection"
```

Append to `tests/test_runs_cmd.py`:

```python
def test_runs_recover_projects_authority_requested_run_from_base_stripped_checkpoint(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    base = {
        "status": "planning", "task": "demo task", "cwd": str(workspace),
        "orchestrator": "chef", "run_journal_authority_requested": True,
        "lifecycle_journal_requested": True,
    }
    _activate_authority_journal_with_checkpoint(workspace, run_dir, base)
    (run_dir / "run.json").unlink()
    rc = runs_cmd.recover(str(run_dir), cwd=workspace)
    assert rc == 0
    recovered = json.loads((run_dir / "run.json").read_text())
    assert recovered["status"] == "failed"
    assert recovered["failure_phase"] == "stale-lock-recovery"
    assert recovered["task"] == "demo task"
    assert recovered["projector_version"] == 3


def test_runs_recover_fail_closed_on_projection_error_for_authority_requested(tmp_path, capsys, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    base = {
        "status": "planning", "task": "demo task", "cwd": str(workspace),
        "run_journal_authority_requested": True,
        "lifecycle_journal_requested": True,
    }
    _activate_authority_journal_with_checkpoint(workspace, run_dir, base)
    (run_dir / "run.json").unlink()
    from brigade import run_projector
    monkeypatch.setattr(run_projector, "project_run_snapshot", lambda *_a, **_kw: (_ for _ in ()).throw(run_projector.EventPayloadError("forged")))
    rc = runs_cmd.recover(str(run_dir), cwd=workspace)
    assert rc == 2
    assert not (run_dir / "run.json").exists()
```

Add the helper `_activate_authority_journal_with_checkpoint` to `tests/test_runs_cmd.py`, mirroring `_activate_journal_with_checkpoint` but bootstrapping `run.json` with both request fields true and passing `body_kind="base-stripped"` to `write_checkpoint`.

- [ ] Run it, watch it fail: `brigade work verify run --target . --command ".venv/bin/python -m pytest -q tests/test_runs_cmd.py tests/test_run_checkpoint.py tests/test_runguard.py" --capture brigade-work` - expect FAIL, `TypeError: write_checkpoint() got an unexpected keyword argument 'body_kind'` from the helper until task 3 lands, then `AttributeError` on the projection branch in `recover_from_checkpoint`.
- [ ] Implement the minimal change. In `run_checkpoint.recover_from_checkpoint`, after `validate_checkpoint` and `_verify_coverage`, branch on the latest checkpoint's `body_kind`. For `base-stripped` whose validated base carries `run_journal_authority_requested: True`, parse the base, run `run_projector.project_run_snapshot(parsed_base, report.events, journal_present=True)`, and use the projected bytes as the intermediate receipt. Wrap a `ProjectionError` as a bounded `CheckpointError(_bound("projection failed"), category="projection")` before it crosses the `runs_cmd` boundary. For `legacy-full` (absent `body_kind`), the slice-5 byte-identical restore runs unchanged. The restore mutation still fires only when `run.json` is missing or unparseable. `runs_cmd._recover_from_checkpoint` is unchanged in shape: it still calls `recover_from_checkpoint` inside the `before_terminalize` callback and maps `CheckpointError` to exit 2. The `cli.runs.dispatch` path through `runs_cmd.recover` is unchanged.

- [ ] Run to green: `brigade work verify run --target . --command ".venv/bin/python -m pytest -q tests/test_runs_cmd.py tests/test_run_checkpoint.py tests/test_runguard.py" --capture brigade-work` - expect PASS. Keep the slice-5 `test_runs_recover_*` tests for the no-journal legacy path, the live-owner refusal, the terminal idempotency, and the `runs show` / `runs watch` stale-lock-recovery surfacing green unchanged.
- [ ] Commit: `git add src/brigade/run_checkpoint.py tests/test_run_checkpoint.py tests/test_runs_cmd.py && git commit -m "feat(runs-cmd): project authority-requested recovery from stripped base"`

## Task 6: Doctor reproject before compare

**Files:**
- Modify: `src/brigade/doctor.py:184-274` (`_recovery_checkpoint_run_verdict`)
- Test: `tests/test_doctor.py`

- [ ] Write the failing tests. Append to `tests/test_doctor.py`:

```python
def test_doctor_reprojects_authority_requested_run_before_compare(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    base = {
        "schema": "brigade.run.v1", "schema_version": 1, "status": "planning",
        "task": "demo", "cwd": str(workspace),
        "run_journal_authority_requested": True,
        "lifecycle_journal_requested": True,
    }
    checkpoint_bytes = _activate_authority_recovery_journal_with_checkpoint(
        workspace, run_dir, base, paired_event_type="run.planning.started",
    )
    # The checkpoint stored the stripped base. Project it to the expected run.json.
    from brigade import run_projector, run_journal, run_lifecycle
    events = run_journal.read_journal(run_lifecycle._journal_path(run_dir)).events
    parsed = json.loads(checkpoint_bytes.decode("utf-8"))
    projected = run_projector.project_run_snapshot(parsed, events, journal_present=True)
    (run_dir / "run.json").write_bytes(projected.to_bytes())
    status, _name, detail = _recovery_check(workspace)
    assert status == doctor_mod.OK


def test_doctor_warn_when_run_json_missing_with_projectable_base(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    base = {
        "schema": "brigade.run.v1", "schema_version": 1, "status": "planning",
        "task": "demo", "cwd": str(workspace),
        "run_journal_authority_requested": True,
        "lifecycle_journal_requested": True,
    }
    _activate_authority_recovery_journal_with_checkpoint(
        workspace, run_dir, base, paired_event_type="run.planning.started",
    )
    (run_dir / "run.json").unlink()
    status, _name, _detail = _recovery_check(workspace)
    assert status == doctor_mod.WARN


def test_doctor_fail_on_projection_error_for_authority_requested(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    base = {
        "schema": "brigade.run.v1", "schema_version": 1, "status": "planning",
        "task": "demo", "cwd": str(workspace),
        "run_journal_authority_requested": True,
        "lifecycle_journal_requested": True,
    }
    _activate_authority_recovery_journal_with_checkpoint(
        workspace, run_dir, base, paired_event_type="run.planning.started",
    )
    from brigade import run_projector
    monkeypatch.setattr(run_projector, "project_run_snapshot", lambda *_a, **_kw: (_ for _ in ()).throw(run_projector.EventPayloadError("forged")))
    status, _name, _detail = _recovery_check(workspace)
    assert status == doctor_mod.FAIL


def test_doctor_legacy_full_checkpoint_still_compares_verbatim(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    run_json_obj = {"schema": "brigade.run.v1", "status": "planning", "task": "demo", "cwd": str(workspace)}
    checkpoint_bytes = _activate_recovery_journal_with_checkpoint(workspace, run_dir, run_json_obj)
    (run_dir / "run.json").write_bytes(checkpoint_bytes)
    status, _name, _detail = _recovery_check(workspace)
    assert status == doctor_mod.OK


def test_doctor_reproject_check_never_mutates(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    run_dir = workspace / ".brigade" / "runs" / _RUN_ID
    base = {
        "schema": "brigade.run.v1", "schema_version": 1, "status": "planning",
        "task": "demo", "cwd": str(workspace),
        "run_journal_authority_requested": True,
        "lifecycle_journal_requested": True,
    }
    checkpoint_bytes = _activate_authority_recovery_journal_with_checkpoint(
        workspace, run_dir, base, paired_event_type="run.planning.started",
    )
    from brigade import run_projector, run_journal, run_lifecycle
    events = run_journal.read_journal(run_lifecycle._journal_path(run_dir)).events
    parsed = json.loads(checkpoint_bytes.decode("utf-8"))
    projected = run_projector.project_run_snapshot(parsed, events, journal_present=True)
    (run_dir / "run.json").write_bytes(projected.to_bytes())
    journal_before = run_lifecycle._journal_path(run_dir).read_bytes()
    cp_before = list((run_dir / "events" / "recovery-checkpoints").iterdir())
    run_json_before = (run_dir / "run.json").read_bytes()
    _recovery_check(workspace)
    assert run_lifecycle._journal_path(run_dir).read_bytes() == journal_before
    assert list((run_dir / "events" / "recovery-checkpoints").iterdir()) == cp_before
    assert (run_dir / "run.json").read_bytes() == run_json_before
```

Add a helper `_activate_authority_recovery_journal_with_checkpoint` to `tests/test_doctor.py` mirroring `_activate_recovery_journal_with_checkpoint` but bootstrapping `run.json` with both request fields true and passing `body_kind="base-stripped"` to `write_checkpoint`. Use it in the authority-requested tests above.

- [ ] Run it, watch it fail: `brigade work verify run --target . --command ".venv/bin/python -m pytest -q tests/test_doctor.py tests/test_run_checkpoint.py" --capture brigade-work` - expect FAIL, `AttributeError` on the reproject branch in `_recovery_checkpoint_run_verdict`.
- [ ] Implement the minimal change. In `doctor._recovery_checkpoint_run_verdict`, after validating the latest checkpoint, branch on `body_kind`. For `base-stripped` on an authority-requested run, parse the base, project with `run_projector.project_run_snapshot(parsed_base, report.events, journal_present=True)`, and compare the projected bytes to the current `run.json` bytes instead of comparing the checkpoint bytes directly. `OK` when they match. `WARN` when `run.json` is missing or unparseable but the projection succeeds. `FAIL` when the projection raises a `ProjectionError`, the chain or coverage check fails, the latest checkpoint is invalid, or any bound is exceeded. The stale-lock-recovery receipt reconstruction for a `base-stripped` run projects the base plus the journal plus the recovery provenance and requires exact equality. The 50-run scan limit, the newest-first ordering, the 8-name failure preview, the aggregate three-field `CheckResult`, and the read-only contract are unchanged. Doctor never quarantines, repairs, or mutates the journal, checkpoint files, or `run.json`.

- [ ] Run to green: `brigade work verify run --target . --command ".venv/bin/python -m pytest -q tests/test_doctor.py tests/test_run_checkpoint.py" --capture brigade-work` - expect PASS.
- [ ] Commit: `git add src/brigade/doctor.py tests/test_doctor.py && git commit -m "feat(doctor): reproject authority-requested runs before checkpoint comparison"`

## Task 7: Enrollment flag and authoritative write path in aboyeur._write_json

**Files:**
- Modify: `src/brigade/aboyeur.py:491-530` (`_write_json`), `src/brigade/aboyeur.py:2155-2225` (`_run_payload`), `src/brigade/aboyeur.py:2299-2372` (`record_run_start`), `src/brigade/run_resume.py:244,258` (the two `_resume_locked` receipt writes), `src/brigade/run_shadow.py` (live-writer contract documentation only)
- Test: `tests/test_run_lifecycle.py`, `tests/test_aboyeur.py`, `tests/test_run_resume.py`, `tests/test_run_shadow.py`

- [ ] Write the failing tests. Append to `tests/test_run_lifecycle.py` (these tests need `run_shadow`, `run_checkpoint`, `run_journal`, and `localio` imported at the top of the appended block):

```python
from brigade import run_shadow, run_checkpoint, run_journal, localio

_AUTHORITY_FIELD = "run_journal_authority_requested"


def _apply_authority_request(run_dir: Path, payload: dict[str, object]) -> dict[str, object]:
    run_json = run_dir / "run.json"
    if run_json.is_file():
        existing = json.loads(run_json.read_text())
        if existing.get(_AUTHORITY_FIELD) is True:
            payload[_AUTHORITY_FIELD] = True
        if existing.get(_REQUEST_FIELD) is True:
            payload[_REQUEST_FIELD] = True
    elif run_lifecycle.is_lifecycle_journaling_enabled():
        payload[_REQUEST_FIELD] = True
    return payload


def _write_run_json_authority(run_dir: Path, status: str, **kwargs) -> None:
    payload = _apply_authority_request(run_dir, _run_payload(status, **kwargs))
    aboyeur._write_json(run_dir / "run.json", payload)


def test_authority_enrollment_persists_run_journal_authority_requested(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    monkeypatch.setenv("BRIGADE_RUN_JOURNAL_AUTHORITY", "1")
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    aboyeur.record_run_start(
        run_dir, task="authority run", cwd=repo, roster=_minimal_roster(),
        read_only=False, lock_workspace=repo,
    )
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["run_journal_authority_requested"] is True
    assert meta["lifecycle_journal_requested"] is True
    assert not _journal_path(run_dir).exists()


def test_not_yet_authoritative_run_writes_legacy_body_when_gate_not_ready(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    monkeypatch.setenv("BRIGADE_RUN_JOURNAL_AUTHORITY", "1")
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    aboyeur.record_run_start(run_dir, task="authority run", cwd=repo, roster=_minimal_roster(), read_only=False, lock_workspace=repo)
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json_authority(run_dir, "planning")
    meta = json.loads((run_dir / "run.json").read_text())
    assert "projector_version" not in meta
    assert meta["status"] == "planning"


def test_first_write_match_authorizes_first_projected_snapshot(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    monkeypatch.setenv("BRIGADE_RUN_JOURNAL_AUTHORITY", "1")
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    aboyeur.record_run_start(run_dir, task="authority run", cwd=repo, roster=_minimal_roster(), read_only=False, lock_workspace=repo)
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json_authority(run_dir, "started")
        _write_run_json_authority(run_dir, "planning")
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["projector_version"] == 3
    assert meta["journal_present"] is True
    assert meta["status"] == "planning"


def test_authoritative_run_fail_closed_on_projection_error(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    monkeypatch.setenv("BRIGADE_RUN_JOURNAL_AUTHORITY", "1")
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    aboyeur.record_run_start(run_dir, task="authority run", cwd=repo, roster=_minimal_roster(), read_only=False, lock_workspace=repo)
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json_authority(run_dir, "started")
        _write_run_json_authority(run_dir, "planning")
    meta_before = (run_dir / "run.json").read_bytes()
    from brigade import run_projector
    monkeypatch.setattr(run_projector, "project_run_snapshot", lambda *_a, **_kw: (_ for _ in ()).throw(run_projector.EventPayloadError("forged")))
    with runguard.run_lock(repo, run_dir=run_dir):
        # Direct aboyeur._write_json authority failure surfaces as a bounded
        # LifecycleJournalError. Only high-level caller adapters (run_resume)
        # translate it to RetainRunLockError.
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    assert (run_dir / "run.json").read_bytes() == meta_before


def test_legacy_run_unchanged_under_authority_flag_off(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    monkeypatch.delenv("BRIGADE_RUN_JOURNAL_AUTHORITY", raising=False)
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    aboyeur.record_run_start(run_dir, task="lifecycle only", cwd=repo, roster=_minimal_roster(), read_only=False, lock_workspace=repo)
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json(run_dir, "planning")
    meta = json.loads((run_dir / "run.json").read_text())
    assert "run_journal_authority_requested" not in meta
    assert "projector_version" not in meta


def _enroll_and_authorize(repo, run_dir, monkeypatch):
    monkeypatch.setenv("BRIGADE_RUN_JOURNAL_AUTHORITY", "1")
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    aboyeur.record_run_start(run_dir, task="authority run", cwd=repo, roster=_minimal_roster(), read_only=False, lock_workspace=repo)
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json_authority(run_dir, "started")
        _write_run_json_authority(run_dir, "planning")
    return json.loads((run_dir / "run.json").read_text())


def test_journal_ahead_remains_authoritative(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    with runguard.run_lock(repo, run_dir=run_dir):
        run_journal.append_event(
            _journal_path(run_dir), run_id=run_dir.name,
            event_type="run.completed", payload={"status": "ok", "detail": "ok"},
            idempotency_key="complete-1",
            expected_previous_sequence=None,
            recorded_at="2026-07-27T15:31:46.000000Z",
        )
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json_authority(run_dir, "ok")
    meta = json.loads((run_dir / "run.json").read_text())
    assert meta["projector_version"] == 3
    assert meta["status"] == "ok"
    assert meta["journal_present"] is True


def test_current_version_mismatch_fail_closed(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    artifact = run_shadow.shadow_artifact_path(run_dir)
    data = json.loads(artifact.read_text())
    data["mismatches"] = 1
    data["last_outcome"] = "mismatch"
    artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    meta_before = (run_dir / "run.json").read_bytes()
    with runguard.run_lock(repo, run_dir=run_dir):
        # Direct aboyeur._write_json authority failure surfaces as a bounded
        # LifecycleJournalError. Only high-level caller adapters (run_resume)
        # translate it to RetainRunLockError.
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    assert (run_dir / "run.json").read_bytes() == meta_before


def test_current_version_error_fail_closed(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    artifact = run_shadow.shadow_artifact_path(run_dir)
    data = json.loads(artifact.read_text())
    data["errors"] = 1
    data["last_outcome"] = "error"
    data["last_error_category"] = "journal-unreadable"
    artifact.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    meta_before = (run_dir / "run.json").read_bytes()
    with runguard.run_lock(repo, run_dir=run_dir):
        # Direct aboyeur._write_json authority failure surfaces as a bounded
        # LifecycleJournalError. Only high-level caller adapters (run_resume)
        # translate it to RetainRunLockError.
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    assert (run_dir / "run.json").read_bytes() == meta_before


def test_bounded_journal_failure_after_authority_fail_closed(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    journal = _journal_path(run_dir)
    journal.write_bytes(journal.read_bytes() + b"x" * (run_checkpoint.MAX_JOURNAL_BYTES + 1))
    meta_before = (run_dir / "run.json").read_bytes()
    journal_before = journal.read_bytes()
    with runguard.run_lock(repo, run_dir=run_dir):
        # Direct aboyeur._write_json authority failure surfaces as a bounded
        # LifecycleJournalError. Only high-level caller adapters (run_resume)
        # translate it to RetainRunLockError.
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    assert (run_dir / "run.json").read_bytes() == meta_before
    # The bounded journal read fails before any append, so the journal bytes
    # are unchanged and the failure is proven pre-append.
    assert journal.read_bytes() == journal_before


def test_authority_fail_closed_on_incomplete_projection_metadata(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    # An authoritative run carries all four projection metadata fields. Drop
    # exactly one (journal_last_event_digest) so the saved metadata is
    # incomplete: the run is past the not-yet-authoritative fallback but
    # cannot verify, so the authority writer must fail closed.
    run_json = run_dir / "run.json"
    meta = json.loads(run_json.read_text())
    del meta["journal_last_event_digest"]
    run_json.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    meta_before = run_json.read_bytes()
    journal_before = _journal_path(run_dir).read_bytes()
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    assert run_json.read_bytes() == meta_before
    assert _journal_path(run_dir).read_bytes() == journal_before


def test_authority_fail_closed_on_saved_sequence_not_matching_verified_prefix(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    # The saved journal_last_sequence must match the event at that sequence in
    # a bounded, chain-valid journal prefix. Corrupt only the saved cursor
    # sequence on run.json (point it past the real tail) so the digest check
    # cannot verify and the authority writer fails closed before any append.
    run_json = run_dir / "run.json"
    meta = json.loads(run_json.read_text())
    meta["journal_last_sequence"] = meta["journal_last_sequence"] + 99
    run_json.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    meta_before = run_json.read_bytes()
    journal_before = _journal_path(run_dir).read_bytes()
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    assert run_json.read_bytes() == meta_before
    assert _journal_path(run_dir).read_bytes() == journal_before


def test_authority_fail_closed_on_saved_digest_not_matching_verified_prefix(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    # Corrupt only the saved journal_last_event_digest on run.json so the
    # cursor sequence still resolves but the digest verification fails and
    # the authority writer fails closed before any append. Splitting cursor
    # and digest mismatches keeps each failure mode unambiguous.
    run_json = run_dir / "run.json"
    meta = json.loads(run_json.read_text())
    meta["journal_last_event_digest"] = "b" * 64
    run_json.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")
    meta_before = run_json.read_bytes()
    journal_before = _journal_path(run_dir).read_bytes()
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            _write_run_json_authority(run_dir, "dispatching")
    assert run_json.read_bytes() == meta_before
    assert _journal_path(run_dir).read_bytes() == journal_before


def test_authoritative_write_order_checkpoint_lifecycle_parity_readiness_replace(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    monkeypatch.setenv("BRIGADE_RUN_JOURNAL_AUTHORITY", "1")
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    aboyeur.record_run_start(run_dir, task="authority run", cwd=repo, roster=_minimal_roster(), read_only=False, lock_workspace=repo)
    # Perform the first started write BEFORE installing spies so the run is
    # authoritative and base construction is not an observable call. Base
    # construction (the stripped candidate) is internal to the writer and is
    # not an observable call, so the order assertion starts at the checkpoint.
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json_authority(run_dir, "started")
    calls: list[str] = []
    real_checkpoint = run_checkpoint.write_checkpoint
    real_transition = run_lifecycle.record_lifecycle_transition
    real_shadow = run_shadow.record_shadow_comparison
    real_readiness = run_shadow.check_projection_readiness
    real_atomic = localio.write_text_atomic
    def checkpoint_spy(*a, **kw):
        calls.append("checkpoint")
        return real_checkpoint(*a, **kw)
    def transition_spy(*a, **kw):
        calls.append("lifecycle")
        return real_transition(*a, **kw)
    def shadow_spy(*a, **kw):
        calls.append("parity")
        return real_shadow(*a, **kw)
    def readiness_spy(*a, **kw):
        calls.append("readiness")
        return real_readiness(*a, **kw)
    def atomic_spy(path, payload, **kw):
        calls.append("replace")
        return real_atomic(path, payload, **kw)
    monkeypatch.setattr(run_checkpoint, "write_checkpoint", checkpoint_spy)
    monkeypatch.setattr(run_lifecycle, "record_lifecycle_transition", transition_spy)
    monkeypatch.setattr(run_shadow, "record_shadow_comparison", shadow_spy)
    monkeypatch.setattr(run_shadow, "check_projection_readiness", readiness_spy)
    monkeypatch.setattr(localio, "write_text_atomic", atomic_spy)
    # Spy on exactly one subsequent planning write and assert the five
    # observable calls fire once each in this order. Base construction is
    # not an observable call, so it does not appear in the order list.
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json_authority(run_dir, "planning")
    assert calls == ["checkpoint", "lifecycle", "parity", "readiness", "replace"]


def test_first_authority_write_checkpoint_seq1_then_status_seq2(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    monkeypatch.setenv("BRIGADE_RUN_JOURNAL_AUTHORITY", "1")
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    aboyeur.record_run_start(run_dir, task="authority run", cwd=repo, roster=_minimal_roster(), read_only=False, lock_workspace=repo)
    # Perform exactly one started write after enrollment. The approved design
    # permits the current-version first comparison to authorize that same
    # write, so the first authority write produces checkpoint seq 1 and the
    # run.created status event seq 2 in a single write.
    with runguard.run_lock(repo, run_dir=run_dir):
        _write_run_json_authority(run_dir, "started")
    events = run_journal.read_journal(_journal_path(run_dir)).events
    assert events[0].event_type == "run.snapshot.checkpointed"
    assert events[0].sequence == 1
    assert events[0].payload.get("body_kind") == "base-stripped"
    assert events[1].event_type == "run.created"
    assert events[1].sequence == 2


def test_direct_write_json_failure_raises_lifecycle_journal_error(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    run_dir = _run_dir(repo)
    _enroll_and_authorize(repo, run_dir, monkeypatch)
    from brigade import run_projector
    monkeypatch.setattr(run_projector, "project_run_snapshot", lambda *_a, **_kw: (_ for _ in ()).throw(run_projector.EventPayloadError("forged")))
    payload = _apply_authority_request(run_dir, _run_payload("dispatching"))
    with runguard.run_lock(repo, run_dir=run_dir):
        with pytest.raises(run_lifecycle.LifecycleJournalError):
            aboyeur._write_json(run_dir / "run.json", payload)
```

Append to `tests/test_run_resume.py`. The two tests are self-contained and use the existing `_write_run_dir` fixture, a successful `_StubServer`, and an `agents.run_agent` stub whose `ok` value selects the receipt-write site under test. Each monkeypatches `aboyeur._write_json` to raise `run_lifecycle.LifecycleJournalError` only at the `run.json` receipt write (the sidecar revision writes for `worker-results.json` and `synthesis.json` must still succeed) and then exercises `run_resume.resume(run_dir)`. Neither calls `aboyeur._write_json` directly. The two tests cover the two distinct `run_resume._resume_locked` receipt writes at `src/brigade/run_resume.py:244` (failed-synthesis path, `final.ok` is false) and `src/brigade/run_resume.py:258` (successful-synthesis path, `final.ok` is true). Both must translate the bounded `LifecycleJournalError` to `runguard.RetainRunLockError` while retaining the workspace `run.lock`.

```python
def test_authority_resume_failed_synthesis_receipt_write_retains_lock(tmp_path, monkeypatch):
    from brigade import aboyeur, run_events, run_lifecycle, runguard

    run_dir = _write_run_dir(
        tmp_path,
        results=[
            {
                "worker": "cook",
                "task": "write code",
                "ok": False,
                "detail": "timeout",
                "text": "part",
                "thread_id": "t-1",
                "status": "interrupted",
            },
        ],
    )
    # Mark the run as authority-requested so the failed-synthesis receipt
    # write at src/brigade/run_resume.py:244 goes through the authoritative
    # projection path in aboyeur._write_json.
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["run_journal_authority_requested"] = True
    run_meta["lifecycle_journal_requested"] = True
    run_meta["cwd"] = str(tmp_path)
    run_meta["lock_workspace"] = str(tmp_path)
    (run_dir / "run.json").write_text(json.dumps(run_meta))
    monkeypatch.setenv("BRIGADE_RUN_JOURNAL_AUTHORITY", "1")
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    monkeypatch.setattr(run_resume.codex_appserver, "AppServer", _StubServer)
    # Failed synthesis: final.ok is False, so _resume_locked takes the
    # status="failed" branch and writes the receipt at line 244.
    monkeypatch.setattr(
        run_resume.agents,
        "run_agent",
        lambda *a, **k: agents.AgentResult(text="synthesis failed", ok=False, detail="timeout"),
    )
    real_write_json = aboyeur._write_json

    def failing_write_json(path, payload, **kwargs):
        if Path(path).name == "run.json":
            raise run_lifecycle.LifecycleJournalError(run_events._bound("projection failed"))
        return real_write_json(path, payload, **kwargs)

    monkeypatch.setattr(aboyeur, "_write_json", failing_write_json)
    with pytest.raises(runguard.RetainRunLockError):
        run_resume.resume(run_dir)
    # The workspace run.lock must remain so stale-lock recovery can rerun.
    lock_path = tmp_path / ".brigade" / "run.lock"
    assert lock_path.exists()


def test_authority_resume_successful_synthesis_receipt_write_retains_lock(tmp_path, monkeypatch):
    from brigade import aboyeur, run_events, run_lifecycle, runguard

    run_dir = _write_run_dir(
        tmp_path,
        results=[
            {
                "worker": "cook",
                "task": "write code",
                "ok": False,
                "detail": "timeout",
                "text": "part",
                "thread_id": "t-1",
                "status": "interrupted",
            },
        ],
    )
    # Mark the run as authority-requested so the successful-synthesis
    # receipt write at src/brigade/run_resume.py:258 goes through the
    # authoritative projection path in aboyeur._write_json.
    run_meta = json.loads((run_dir / "run.json").read_text())
    run_meta["run_journal_authority_requested"] = True
    run_meta["lifecycle_journal_requested"] = True
    run_meta["cwd"] = str(tmp_path)
    run_meta["lock_workspace"] = str(tmp_path)
    (run_dir / "run.json").write_text(json.dumps(run_meta))
    monkeypatch.setenv("BRIGADE_RUN_JOURNAL_AUTHORITY", "1")
    monkeypatch.setenv("BRIGADE_LIFECYCLE_JOURNAL", "1")
    monkeypatch.setattr(run_resume.codex_appserver, "AppServer", _StubServer)
    # Successful synthesis: final.ok is True, so _resume_locked takes the
    # status="ok" branch and writes the receipt at line 258.
    monkeypatch.setattr(
        run_resume.agents,
        "run_agent",
        lambda *a, **k: agents.AgentResult(text="final synthesis", ok=True),
    )
    real_write_json = aboyeur._write_json

    def failing_write_json(path, payload, **kwargs):
        if Path(path).name == "run.json":
            raise run_lifecycle.LifecycleJournalError(run_events._bound("projection failed"))
        return real_write_json(path, payload, **kwargs)

    monkeypatch.setattr(aboyeur, "_write_json", failing_write_json)
    with pytest.raises(runguard.RetainRunLockError):
        run_resume.resume(run_dir)
    # The workspace run.lock must remain so stale-lock recovery can rerun.
    lock_path = tmp_path / ".brigade" / "run.lock"
    assert lock_path.exists()
```

- [ ] Run it, watch it fail: `brigade work verify run --target . --command ".venv/bin/python -m pytest -q tests/test_run_lifecycle.py tests/test_aboyeur.py tests/test_run_resume.py tests/test_run_shadow.py tests/test_run_checkpoint.py" --capture brigade-work` - expect FAIL, `AttributeError` on `is_run_journal_authority_enabled` and the authority branch in `_write_json`, plus `DID NOT RAISE` on `test_authority_fail_closed_on_incomplete_projection_metadata`, `test_authority_fail_closed_on_saved_sequence_not_matching_verified_prefix`, and `test_authority_fail_closed_on_saved_digest_not_matching_verified_prefix` because the slice-5 writer has no authority-state resolver and writes the legacy body instead of failing closed, plus `test_bounded_journal_failure_after_authority_fail_closed` failing its new `journal.read_bytes() == journal_before` assertion for the same reason.

- [ ] Implement the minimal change. In `src/brigade/aboyeur.py` add the constants `_AUTHORITY_FLAG_ENV = "BRIGADE_RUN_JOURNAL_AUTHORITY"` and `_AUTHORITY_REQUEST_FIELD = "run_journal_authority_requested"`, and the helper `is_run_journal_authority_enabled()` mirroring `run_lifecycle.is_lifecycle_journaling_enabled`. Add the parameter `run_journal_authority_requested: bool | None = None` to `_run_payload` and, when it is true, set `payload["run_journal_authority_requested"] = True` alongside the existing `lifecycle_journal_requested` block. In `record_run_start`, read an existing `run_journal_authority_requested: true` from `run.json` like the existing `lifecycle_journal_requested` read, and enroll a new run (no existing `run.json`) with `BRIGADE_RUN_JOURNAL_AUTHORITY` set by setting both request fields true. A later environment change never enrolls an existing run. The bootstrap never creates the journal and never appends.

Add the authority-state resolver helper to `aboyeur.py`:

```python
def _resolve_authority_state(run_dir: Path, workspace: Path, incoming: Mapping[str, object]) -> str:
    """Return one of 'legacy', 'authority-requested', 'authoritative'.

    'legacy' only when no durable authority request is present
    (run_journal_authority_requested is not true on run.json).
    'authority-requested' only when the durable authority request is true
    and NONE of the four projection metadata fields is present on run.json
    (projector_version, journal_present, journal_last_sequence,
    journal_last_event_digest). Once ANY of the four projection metadata
    fields is present, the run is no longer in the not-yet-authoritative
    fallback: incomplete fields, a stale or wrong projector version, a
    bounded read failure, a chain failure, a cursor failure, or a digest
    failure all raise a bounded LifecycleJournalError. There is no
    metadata-shape downgrade. 'authoritative' only when run.json carries
    all four journal-metadata fields with projector_version ==
    run_projector.PROJECTOR_VERSION and its saved
    journal_last_sequence/journal_last_event_digest verifies against the
    event at that sequence in a bounded, chain-valid journal prefix. Reads
    use run_journal.read_journal_bounded with the slice-5 limits. A bound
    failure, chain error, cursor/digest mismatch, or projector-version
    mismatch on a run with ANY projection metadata field present raises a
    bounded LifecycleJournalError and never returns 'legacy' or
    'authority-requested'. A bound failure or chain error on a run with no
    projection metadata fields returns 'authority-requested' (the
    not-yet-authoritative fallback). It never authorizes projection.
    """
```

The helper reads `run.json` (parseable dict or None). When the durable authority request is not true, it returns `'legacy'` without touching the journal. When the request is true and none of the four projection metadata fields is present on `run.json`, it returns `'authority-requested'` without touching the journal. Once any of the four projection metadata fields is present, the run is past the not-yet-authoritative fallback. Incomplete fields, a stale or wrong `projector_version`, a bounded read failure, a chain failure, a cursor failure, or a digest failure all raise a bounded `LifecycleJournalError` and never downgrade to `'legacy'` or `'authority-requested'`. When all four fields are present with `projector_version == run_projector.PROJECTOR_VERSION`, it calls `run_journal.read_journal_bounded` and verifies the saved `journal_last_sequence`/`journal_last_event_digest` against the event at that sequence in the verified prefix. On success it returns `'authoritative'`. On any bound failure, chain error, cursor/digest mismatch, or projector-version mismatch it raises a bounded `LifecycleJournalError` and never returns `'legacy'`.

Rewrite the `run.json` branch of `_write_json` to resolve the authority state before any new checkpoint or lifecycle append (the resolver itself raises a bounded `LifecycleJournalError` when projected metadata exists and validation fails, so the writer never observes an authoritative run downgrade), then:

1. Build the complete legacy candidate: deep-copy the incoming payload, drop any stale `projector_version`, `journal_present`, `journal_last_sequence`, `journal_last_event_digest` keys, and encode with the house canonical form. The same object is the `base-stripped` checkpoint body whenever the resolved state is `authority-requested` or `authoritative`.
2. `run_lifecycle.prepare_lifecycle_journal(run_dir, workspace=workspace, incoming_snapshot=payload)` (unchanged activation).
3. `run_checkpoint.write_checkpoint(run_dir, base_bytes, workspace=workspace, paired_event_type=run_lifecycle.STATUS_EVENT_TYPE.get(status), body_kind=("base-stripped" if authority_state in {"authority-requested", "authoritative"} else None))`. A `CheckpointError` propagates and stops the write.
4. `run_lifecycle.record_lifecycle_transition(...)` (unchanged, with the slice-6 mapping).
5. `run_shadow.record_shadow_comparison(run_dir, legacy_snapshot)` (unchanged, fail-open for the writer).
6. Readiness veto: `run_shadow.check_projection_readiness(run_dir)`. If `authority_state == "legacy"`, the veto is skipped and the writer writes the legacy body (slice 5). If `authority_state == "authority-requested"` and the gate is not ready (no comparisons yet, stale v2 evidence, a current lag, or any recorded mismatch or error), write the legacy body. If `authority_state == "authoritative"` and the gate is not ready (`REASON_MISMATCH_RECORDED`, `REASON_ERROR_RECORDED`, `REASON_STATUS_LAG_CURRENT`, `REASON_JOURNAL_UNREADABLE`, `REASON_NO_EVIDENCE`, or `REASON_EVIDENCE_UNREADABLE`), stop the write with no downgrade.
7. For an `authority-requested` or `authoritative` run with a ready gate, call `run_journal.read_journal_bounded` with the slice-5 byte and event limits, then call `run_projector.project_run_snapshot(parsed_base, report.events, journal_present=True)`. A bounded-read, chain, or `ProjectionError` failure stops the write with no downgrade.
8. `localio.write_text_atomic(path, projected_bytes)`. After the replace, the run is authoritative.

Translate an authority readiness or projection failure into a bounded `run_lifecycle.LifecycleJournalError` so the existing `RetainRunLockError` caller paths retain the lock. The readiness veto routes `REASON_EVIDENCE_PROJECTOR_VERSION_STALE` or `REASON_NO_COMPARISONS` on an authority-requested run to the legacy-body fallback, and `REASON_MISMATCH_RECORDED`, `REASON_ERROR_RECORDED`, `REASON_STATUS_LAG_CURRENT`, `REASON_JOURNAL_UNREADABLE`, `REASON_NO_EVIDENCE`, or `REASON_EVIDENCE_UNREADABLE` on an authoritative run stops the write. `runguard._recover_run_artifact` repair writes stay unprojected. The hook computes the complete legacy candidate and projection base exactly once per write. Update the `run_shadow` module and function documentation to state that authority-mode comparison occurs before the candidate is committed. Its comparison input and fail-open behavior remain unchanged. Wrap the two `run_resume._resume_locked` receipt writes (at `src/brigade/run_resume.py:244` and `:258`) so a bounded `LifecycleJournalError` becomes `RetainRunLockError` before it leaves the held-lock scope. Do not change resume eligibility, transport, synthesis, or terminal-state behavior.

- [ ] Run to green: `brigade work verify run --target . --command ".venv/bin/python -m pytest -q tests/test_run_lifecycle.py tests/test_aboyeur.py tests/test_run_resume.py tests/test_run_shadow.py tests/test_run_checkpoint.py" --capture brigade-work` - expect PASS. The write-order test `test_authoritative_write_order_checkpoint_lifecycle_parity_readiness_replace` and `test_first_authority_write_checkpoint_seq1_then_status_seq2` pass once the implementation lands.
- [ ] Commit: `git add src/brigade/aboyeur.py src/brigade/run_resume.py src/brigade/run_shadow.py tests/test_run_lifecycle.py tests/test_aboyeur.py tests/test_run_resume.py tests/test_run_shadow.py && git commit -m "feat(aboyeur): enroll new runs with BRIGADE_RUN_JOURNAL_AUTHORITY and project authoritative snapshots"`

## Task 8: 1,000-run measurement harness

**Files:**
- Create: `scripts/measure_run_journal.py`, `tests/test_run_journal_measurement.py`
- Test: `tests/test_run_journal_measurement.py`

- [ ] Write the failing tests. Create `tests/test_run_journal_measurement.py` (loads the script via `importlib.util.spec_from_file_location`, matching the `tests/test_version_sync_script.py` pattern, since `scripts/` is not a package):

```python
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "measure_run_journal.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("measure_run_journal_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_measure_runs_returns_report_with_required_fields(tmp_path):
    module = _load_module()
    report = module.measure_runs(runs=3, root=tmp_path)
    assert isinstance(report, dict)
    for key in ("runs", "per_run", "aggregate", "environment"):
        assert key in report
    assert report["runs"] == 3
    assert len(report["per_run"]) == 3
    for entry in report["per_run"]:
        assert "journal_bytes" in entry
        assert "append_latencies_ns" in entry
        assert isinstance(entry["append_latencies_ns"], list)


def test_measure_runs_reports_p50_p95_p99_max(tmp_path):
    module = _load_module()
    report = module.measure_runs(runs=2, root=tmp_path)
    agg = report["aggregate"]
    for key in ("p50_ns", "p95_ns", "p99_ns", "max_ns"):
        assert key in agg
        assert isinstance(agg[key], int)


def test_measure_runs_records_environment_metadata(tmp_path):
    module = _load_module()
    report = module.measure_runs(runs=1, root=tmp_path)
    env = report["environment"]
    assert env["python_version"] == sys.version
    assert "platform" in env
    assert "filesystem_type" in env
    assert "directory_fsync_supported" in env


def test_measure_runs_introduces_no_pass_threshold(tmp_path):
    module = _load_module()
    report = module.measure_runs(runs=1, root=tmp_path)
    flat = json.dumps(report)
    assert "threshold" not in flat
    assert "pass" not in flat


def test_write_report_writes_canonical_json_to_exact_output_path(tmp_path):
    module = _load_module()
    report = module.measure_runs(runs=1, root=tmp_path)
    output = tmp_path / "report.json"
    module.write_report(report, output)
    assert output.is_file()
    written = json.loads(output.read_text())
    assert written == report
    assert output.read_bytes().endswith(b"\n")


def test_cli_writes_report_to_exact_output_path(tmp_path):
    output = tmp_path / "cli-report.json"
    rc = subprocess.run(
        [sys.executable, str(SCRIPT), "--runs", "1", "--output", str(output)],
        cwd=ROOT,
        capture_output=True,
    )
    assert rc.returncode == 0
    assert output.is_file()
    written = json.loads(output.read_text())
    assert written["runs"] == 1
    assert "threshold" not in json.dumps(written)
```

- [ ] Run it, watch it fail: `brigade work verify run --target . --command ".venv/bin/python -m pytest -q tests/test_run_journal_measurement.py" --capture brigade-work` - expect FAIL, `FileNotFoundError` for `scripts/measure_run_journal.py`.
- [ ] Implement the minimal change. Create `scripts/measure_run_journal.py` as a stdlib-only module exposing two functions and a CLI:

```python
def measure_runs(runs: int, root: Path) -> dict:
    """Drive ``runs`` run directories under ``root`` through the
    journal-authority path (bootstrap with both request fields, activate the
    journal, append a checkpoint and a mapped status event under a real
    ``runguard.run_lock``), and return a report dict with ``runs``,
    ``per_run`` (each entry carrying ``journal_bytes`` and
    ``append_latencies_ns`` measured with ``time.perf_counter_ns``),
    ``aggregate`` (``p50_ns``, ``p95_ns``, ``p99_ns``, ``max_ns`` across all
    appends), and ``environment``. Records no invented pass threshold.
    """


def write_report(report: dict, output: Path) -> None:
    """Write ``report`` as canonical JSON (``indent=2, sort_keys=True`` plus a
    trailing newline) to the exact file path ``output``. ``output`` is a file
    path, not a directory. Parent directories are created with ``mkdir(parents=True)``.
    """
```

The `__main__` entry point uses argparse with `--runs` (default `1000`) and `--output` (required, an exact file path). It calls `measure_runs(runs, root=Path.cwd())` then `write_report(report, Path(args.output))`. `_environment_metadata()` returns Python version, platform, filesystem type, and directory-fsync support. Filesystem type is resolved by a stdlib `/proc/self/mountinfo` deepest-mount parser. Parse each mountinfo line, find the mount whose mountpoint is the longest path-prefix of the run directory, and return its `fstype` field, which is the column after the separator ` - `. Fall back to the string `"unknown"` when `/proc/self/mountinfo` is absent or no entry matches. Directory-fsync support is determined by generating a temporary directory under `root`, opening it with `os.open(..., os.O_RDONLY|os.O_DIRECTORY)`, calling `os.fsync(fd)` inside a `try/except OSError`, and recording `True` when `fsync` returns or `False` when it raises. The harness records no invented pass threshold.

- [ ] Run to green: `brigade work verify run --target . --command ".venv/bin/python -m pytest -q tests/test_run_journal_measurement.py" --capture brigade-work` - expect PASS. The default suite invokes the harness with a small run count so its schema and mechanics are always tested.
- [ ] Commit: `git add scripts/measure_run_journal.py tests/test_run_journal_measurement.py && git commit -m "feat(run-journal): add stdlib-only 1,000-run measurement harness without a pass threshold"`

## Closeout: full verification, measurement, independent review, MiseLedger export, memory handoff

1. Re-run every focused suite from tasks 1 through 8, then run the full `./scripts/verify` gate through Brigade:
   `brigade work verify run --target . --command "./scripts/verify" --capture brigade-work`.
   Report the actual result and paste any failure verbatim. If the gate fails, fix the code, not the tests. Never weaken, skip, or delete a test to make it pass, and say so explicitly before touching a test that is itself wrong.
2. Run the real measurement through Brigade:
   `brigade work verify run --target . --command ".venv/bin/python scripts/measure_run_journal.py --runs 1000 --output .brigade/measurements/issue-568-slice6.json" --capture brigade-work`.
   Review the JSON report, cite its receipt and measured values in the PR, and do not infer a pass threshold from those values.
3. Record the verification outcome through the `brigade work verify` flow so the slice receipt is captured and attributed. Capture failures too.
4. Export the slice evidence through MiseLedger:
   `brigade receipts export miseledger --target . --new-only --import` so the evidence returns to the next Brigade run as an untrusted evidence brief.
5. Write the memory handoff to `.claude/memory-handoffs/` per the workspace `AGENTS.md` template: the projection checkpoint base and the optional `body_kind` payload key, the `PROJECTOR_VERSION` 3 one-way door and the three new status mappings, the readiness-gate version-door split with quarantined prior-version evidence, the ordinary-gate first-write authorization rule, the no-downgrade invariant after the first authoritative commit, the authority-aware recovery projection, the doctor reproject-before-compare step, the enrollment flag and its lifecycle-journaling prerequisite, the 1,000-run measurement harness with no invented pass threshold, and any root causes or gotchas found during implementation, failures included.

## Integration ordering and independent review

Tasks 1 through 6 are independently testable and land in order. Task 7 (the `aboyeur._write_json` integration) depends on tasks 1 through 4 and is the only task that touches the live write path. It lands after task 6 so doctor and recovery already understand the stripped base before any run commits one. Task 8 (the measurement harness) depends on task 7 because its real 1,000-run command exercises the authority path. It lands last, and the measured report is required before the slice PR is ready for review.

Each task is a separate conventional commit on the slice branch. No commit knowingly leaves a prior focused suite red. The integration task (task 7) is the only task that may update prior tests across modules. It does so in its own commit. An independent review pass runs before merge: the reviewer re-runs the focused suites and the full gate, checks the blast-radius names in the spec against the actual call graph, and verifies that no task edited a file outside its exclusive ownership set.

The exact GraphTrail blast-radius names already recorded in the spec: the `aboyeur._write_json` callers `record_run_start`, `record_run_termination`, `record_dispatch_stage`, `record_result_processing`, `record_artifact_collection`, `run`, `write_sidecar_revision`, and `run_resume._resume_locked`. Its callees `runguard.resolve_run_lock_workspace`, `run_lifecycle.prepare_lifecycle_journal`, `run_checkpoint.write_checkpoint`, `run_lifecycle.record_lifecycle_transition`, `localio.write_text_atomic`, and `run_shadow.record_shadow_comparison`. `run_checkpoint.recover_from_checkpoint` feeds `runs_cmd._recover_from_checkpoint`, which feeds `cli.runs.dispatch` through `runs_cmd.recover`. `run_projector.project_run_snapshot` feeds `run_shadow.record_shadow_comparison` and the new authoritative projection step in `aboyeur._write_json`. `run_shadow.check_projection_readiness` is the veto. The affected tests are `tests/test_run_checkpoint.py`, `tests/test_run_shadow.py`, `tests/test_run_projector.py`, `tests/test_run_lifecycle.py`, `tests/test_runs_cmd.py`, `tests/test_doctor.py`, and `tests/test_run_events.py`.

## Rollback constraints

Each task is a separate conventional commit on the slice branch. If a task is found wrong after merge, revert that commit. Two rollback constraints apply:

- Reverting the projector-version bump (task 1, version 3 to 2) alone is unsafe: a version-3 projector without the registered `run.artifact_collection.started` event type and the widened `run.completed` / `run.failed` payload rules fails closed on any authority-requested journal that already carries those events. Roll tasks 1 and 2 back together, and drop the activated authority-requested journals.
- Reverting the checkpoint body change (task 3, full to stripped) alone is unsafe: a `base-stripped` checkpoint read by a slice-5 `validate_checkpoint` fails the writer-byte equality check. Roll tasks 3, 5, and 6 back together.

Reverting task 7 (the enrollment flag and the authoritative write path) disables authority without affecting legacy or lifecycle-journal-only runs. Reverting task 4 (the version-door split) re-introduces permanent `evidence-schema-mismatch` on version mismatch. Roll it back only together with task 1. Reverting task 8 removes the measurement harness with no effect on runtime behavior. Never `git push --no-verify`. The content-guard pre-push hook must pass.

## Push, PR, and CI steps

1. Push the slice branch: `git push -u origin codex/568-6-journal-authority` (only when explicitly asked). The content-guard pre-push hook must pass.
2. Open the PR: `gh pr create --title "feat(run-journal): journal-authoritative run.json for newly enrolled runs (issue #568 slice 6)" --body "$(cat <<'EOF'\n## Summary\n- Projector version 3 maps dry-run, incomplete, and artifact-collection. New run.artifact_collection.started event type.\n- Stripped checkpoint base plus optional body_kind. Legacy-full idempotency key preserved.\n- Readiness-gate version-door split with stale-v2 quarantine and gap-baseline carry.\n- Authority-aware recovery projection and doctor reproject-before-compare.\n- BRIGADE_RUN_JOURNAL_AUTHORITY enrollment and authoritative write path in aboyeur._write_json with no-downgrade.\n- Stdlib-only 1,000-run measurement harness, no pass threshold.\n\n## Test plan\n- [ ] ./scripts/verify green\n- [ ] 1,000-run measurement report cited below\n- [ ] Independent review re-ran focused suites and full gate\n- [ ] Blast-radius names match the spec\nEOF\n)"`.
3. Cite the Brigade verification receipt and the measured JSON values (journal bytes, p50/p95/p99/max append latency, environment metadata) in the PR body. Do not infer a pass threshold.
4. Wait for CI: `content-guard`, `repo-metadata`, `install-from-source`, `quickstart-smoke`, and `windows-native-acceptance`. The local `./scripts/verify` gate is the fast completion gate. CI-only jobs cover the slower release and public-repo checks.

## Slice 6 boundary and issue #568 remaining-open criteria

Slice 6 lands the authoritative writer for newly enrolled runs, the stripped checkpoint base, the projector-version-3 mapping, the readiness-gate version-door split, the authority-aware recovery projection, the doctor reproject step, and the 1,000-run measurement harness. The opt-in flag `BRIGADE_RUN_JOURNAL_AUTHORITY` stays opt-in. The default flip is a later human decision after the measurement report is reviewed. Issue #568 stays open after this slice: the remaining acceptance criteria (approval pause and resume projection, the crash-after-approval-consumption reconciliation, the redaction procedure, and the final default-flip decision) must be implemented in later slices or moved to linked follow-up issues before #568 can close. Issue #489 owns interrupted-run resume work and issue #604 owns requested/observed external-control facts. Approval integration and the operator redaction procedure need explicit ownership if they move out of #568. Issues #578 and #579 remain separate routing and isolation work. Closed issue #566 remains separate outcome-ledger work.

Citations. GitHub issue #568 (https://github.com/escoffier-labs/brigade/issues/568). Spec: `docs/phase-568-slice-6-journal-authority.md`. Prior slices: `docs/phase-568-slice-3-run-projector.md`, `docs/phase-568-slice-4-shadow-comparison.md`, `docs/phase-568-slice-5-recovery-checkpoints.md`. No code is implemented in this document.
