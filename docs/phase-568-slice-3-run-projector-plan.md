# Issue #568 Slice 3: Run Snapshot Projector - Execution Plan

Goal: implement the approved spec at `docs/phase-568-slice-3-run-projector.md` by adding one pure projector module (`src/brigade/run_projector.py`), its 19 tests, and two golden fixtures, with zero changes to any existing writer, reader, or CLI path.

Architecture: `project_run_snapshot(base_snapshot, events, *, journal_present)` re-validates every envelope through `run_events.validate_event`, re-verifies the chain (contiguous sequence from 1, digest linkage, single `run_id`), then derives exactly five fields (`status`, `projector_version`, `journal_present`, `journal_last_sequence`, `journal_last_event_digest`) while deep-copying the 41 preserved fields verbatim from the base. Output bytes come from the single encoding path `(json.dumps(snapshot, indent=2, sort_keys=True) + "\n").encode("utf-8")`, matching `aboyeur._write_json` byte for byte. The module performs no I/O, reads no clock or environment, and nothing in the runtime calls it in this slice.

The spec is the authority. This plan quotes the spec's API and the real source contracts in `src/brigade/run_events.py`, `src/brigade/run_journal.py`, `src/brigade/run_lifecycle.py`, and `src/brigade/aboyeur.py` as of the base commit. If any quoted contract disagrees with the checked-out code, stop and re-read the code. Do not improvise.

## File map

| Path | Action |
| --- | --- |
| `tests/test_run_projector.py` | Create first (RED). 19 test functions, listed below |
| `src/brigade/run_projector.py` | Create. The entire production surface of this slice |
| `tests/fixtures/run-lifecycle/golden-projection.base.json` | Create. Hand-authored base snapshot carrying all 41 preserved fields |
| `tests/fixtures/run-lifecycle/golden-projection.expected.json` | Create. Generated once by the implementation, reviewed by hand before commit |

Do not edit `src/brigade/aboyeur.py`, `src/brigade/run_lifecycle.py`, `src/brigade/run_journal.py`, `src/brigade/run_events.py`, any CLI module, `pyproject.toml`, or any other existing file. The existing fixture `tests/fixtures/run-lifecycle/golden-lifecycle.jsonl` is read, never modified.

## Repository conventions used below

- Tests run with `python3 -m pytest` from the repo root. `tests/conftest.py` inserts `src/` on `sys.path`, so no install step is needed. Ad-hoc scripts outside pytest need `PYTHONPATH=src`.
- Focused runs name the test file: `python3 -m pytest tests/test_run_projector.py -q`.
- Lint and type gates: `python3 -m ruff check <paths>` (line length 120, rules B, E4, E7, E9, F) and `python3 -m mypy src/brigade/run_projector.py` (new modules are checked from day one. Only legacy modules carry overrides in `pyproject.toml`).
- Fixtures for this work live under `tests/fixtures/run-lifecycle/`, beside `golden-lifecycle.jsonl` (6 events, run_id `20260727-153045-a1b2c3d4`, final event `run.completed` with `payload.status == "ok"`, final `event_digest` `176259cd00408b637f1c13496fcdfafea7dd96753fb8a9e76dbea58817442fc7`).

## Task 1: RED tests

- [x] Create `tests/test_run_projector.py` with the approved RED coverage:

```python
"""Tests for brigade.run_projector pure run snapshot projector (issue #568 slice 3).

Covers the slice-3 contract: golden replay against the committed lifecycle
journal, determinism, empty-sequence journal presence semantics, the
event-to-status mapping with its payload status rules, closed field ownership
over run.json, fail-closed chain verification, preservation deep-equality,
re-projection idempotence, encoding parity with aboyeur._write_json, and
bounded typed errors for every failure mode.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from brigade import run_events, run_journal, run_projector

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "run-lifecycle"
GOLDEN_LIFECYCLE_PATH = FIXTURES / "golden-lifecycle.jsonl"
GOLDEN_BASE_PATH = FIXTURES / "golden-projection.base.json"
GOLDEN_EXPECTED_PATH = FIXTURES / "golden-projection.expected.json"

RUN_ID = "20260727-153045-a1b2c3d4"
RECORDED_AT = "2026-07-27T15:30:45.123456Z"
GOLDEN_FINAL_DIGEST = "176259cd00408b637f1c13496fcdfafea7dd96753fb8a9e76dbea58817442fc7"


def _load_golden_base() -> dict:
    if not GOLDEN_BASE_PATH.is_file():
        pytest.fail(f"missing golden fixture: {GOLDEN_BASE_PATH}")
    return json.loads(GOLDEN_BASE_PATH.read_text(encoding="utf-8"))


def _read_golden_events() -> list[run_journal.RunEvent]:
    if not GOLDEN_LIFECYCLE_PATH.is_file():
        pytest.fail(f"missing golden fixture: {GOLDEN_LIFECYCLE_PATH}")
    report = run_journal.read_journal(GOLDEN_LIFECYCLE_PATH)
    assert report.partial_tail is None
    assert report.chain_errors == []
    return report.events


def _minimal_base() -> dict:
    return {
        "schema": "brigade.run.v1",
        "schema_version": 1,
        "task": "project the lifecycle journal",
        "orchestrator": "chef",
        "dry_run": False,
        "read_only": False,
        "status": "started",
        "started_at": "2026-07-27T15:30:45.123456+00:00",
        "status_started_at": "2026-07-27T15:30:45.123456+00:00",
        "suspected_noop": False,
        "code_graph_brief": {"attached": False, "bytes": 0},
        "drift_impact_brief": {"attached": False, "bytes": 0, "pending_count": 0},
        "evidence_brief": {"attached": False, "bytes": 0},
        "brief_budget": {"bytes": 8192, "attached": []},
    }


def _build_chain(
    steps: list[tuple[str, dict]],
    *,
    run_id: str = RUN_ID,
    start_sequence: int = 1,
    previous_digest: str | None = None,
) -> list[dict]:
    envelopes = []
    sequence = start_sequence
    for event_type, payload in steps:
        env = run_events.build_event(
            run_id=run_id,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            idempotency_key=f"test-{run_id}-{sequence}",
            recorded_at=RECORDED_AT,
            previous_digest=previous_digest,
        )
        envelopes.append(env)
        previous_digest = env["event_digest"]
        sequence += 1
    return envelopes


def _failed_chain(status_payload: dict) -> list[dict]:
    return _build_chain(
        [
            ("run.created", {"status": "started"}),
            ("run.failed", status_payload),
        ]
    )


def test_golden_replay_matches_expected_bytes():
    if not GOLDEN_EXPECTED_PATH.is_file():
        pytest.fail(f"missing golden fixture: {GOLDEN_EXPECTED_PATH}")
    projection = run_projector.project_run_snapshot(
        _load_golden_base(), _read_golden_events(), journal_present=True
    )
    assert projection.to_bytes() == GOLDEN_EXPECTED_PATH.read_bytes()


def test_golden_projection_is_byte_deterministic():
    base = _load_golden_base()
    events = _read_golden_events()
    first = run_projector.project_run_snapshot(base, events, journal_present=True)
    second = run_projector.project_run_snapshot(base, events, journal_present=True)
    assert first.to_bytes() == second.to_bytes()


def test_empty_sequence_without_journal_preserves_base():
    base = _minimal_base()
    projection = run_projector.project_run_snapshot(base, [], journal_present=False)
    assert projection.status == "started"
    assert projection.journal_present is False
    assert projection.last_sequence == 0
    assert projection.last_event_digest is None
    snapshot = projection.snapshot
    assert snapshot["status"] == "started"
    assert snapshot["projector_version"] == 1
    assert snapshot["journal_present"] is False
    assert snapshot["journal_last_sequence"] == 0
    assert snapshot["journal_last_event_digest"] is None
    for key, value in base.items():
        assert snapshot[key] == value
    assert snapshot["code_graph_brief"] is not base["code_graph_brief"]


def test_empty_created_journal_projects_presence_without_facts():
    projection = run_projector.project_run_snapshot(_minimal_base(), [], journal_present=True)
    assert projection.journal_present is True
    assert projection.last_sequence == 0
    assert projection.last_event_digest is None
    assert projection.status == "started"
    assert projection.snapshot["journal_present"] is True
    assert projection.snapshot["journal_last_sequence"] == 0
    assert projection.snapshot["journal_last_event_digest"] is None


def test_full_golden_sequence_derives_terminal_facts():
    projection = run_projector.project_run_snapshot(
        _load_golden_base(), _read_golden_events(), journal_present=True
    )
    assert projection.status == "ok"
    assert projection.last_sequence == 6
    assert projection.last_event_digest == GOLDEN_FINAL_DIGEST


def test_run_failed_timeout_payload_derives_timeout():
    chain = _failed_chain({"status": "timeout", "detail": "bounded"})
    projection = run_projector.project_run_snapshot(_minimal_base(), chain, journal_present=True)
    assert projection.status == "timeout"
    assert projection.last_sequence == 2


def test_run_failed_failed_payload_derives_failed():
    chain = _failed_chain({"status": "failed", "detail": "bounded"})
    projection = run_projector.project_run_snapshot(_minimal_base(), chain, journal_present=True)
    assert projection.status == "failed"
    assert projection.last_sequence == 2


@pytest.mark.parametrize("payload", [{"status": "ok"}, {"detail": "bounded"}])
def test_run_failed_bad_or_missing_payload_status_raises(payload):
    chain = _failed_chain(payload)
    with pytest.raises(run_projector.EventPayloadError) as excinfo:
        run_projector.project_run_snapshot(_minimal_base(), chain, journal_present=True)
    assert len(excinfo.value.diagnostic) <= run_events.MAX_DIAGNOSTIC_LEN
    assert "run.failed" in excinfo.value.diagnostic


def test_run_interrupted_derives_canceled_and_wrong_status_raises():
    chain = _build_chain(
        [
            ("run.created", {"status": "started"}),
            ("run.interrupted", {"status": "canceled"}),
        ]
    )
    projection = run_projector.project_run_snapshot(_minimal_base(), chain, journal_present=True)
    assert projection.status == "canceled"

    bad = _build_chain(
        [
            ("run.created", {"status": "started"}),
            ("run.interrupted", {"status": "failed"}),
        ]
    )
    with pytest.raises(run_projector.EventPayloadError) as excinfo:
        run_projector.project_run_snapshot(_minimal_base(), bad, journal_present=True)
    assert len(excinfo.value.diagnostic) <= run_events.MAX_DIAGNOSTIC_LEN


def test_unknown_base_key_raises_unknown_snapshot_field():
    base = _minimal_base()
    base["mystery_key"] = 1
    with pytest.raises(run_projector.UnknownSnapshotFieldError) as excinfo:
        run_projector.project_run_snapshot(base, [], journal_present=False)
    assert "mystery_key" in excinfo.value.diagnostic
    assert len(excinfo.value.diagnostic) <= run_events.MAX_DIAGNOSTIC_LEN


def test_registered_unmapped_event_type_raises():
    chain = _build_chain(
        [
            ("run.created", {"status": "started"}),
            ("run.paused", {"approval_id": "ap-1", "reason": "review"}),
        ]
    )
    with pytest.raises(run_projector.UnmappedEventTypeError) as excinfo:
        run_projector.project_run_snapshot(_minimal_base(), chain, journal_present=True)
    assert "run.paused" in excinfo.value.diagnostic
    assert len(excinfo.value.diagnostic) <= run_events.MAX_DIAGNOSTIC_LEN


def test_chain_breaks_raise_event_chain_error():
    base = _minimal_base()
    first = _build_chain([("run.created", {"status": "started"})])

    gap = first + _build_chain(
        [("run.planning.started", {"detail": "planning"})],
        start_sequence=3,
        previous_digest=first[0]["event_digest"],
    )
    with pytest.raises(run_projector.EventChainError):
        run_projector.project_run_snapshot(base, gap, journal_present=True)

    duplicate = first + first
    with pytest.raises(run_projector.EventChainError):
        run_projector.project_run_snapshot(base, duplicate, journal_present=True)

    broken = first + _build_chain(
        [("run.planning.started", {"detail": "planning"})],
        start_sequence=2,
        previous_digest="0" * 64,
    )
    with pytest.raises(run_projector.EventChainError):
        run_projector.project_run_snapshot(base, broken, journal_present=True)

    mixed = first + _build_chain(
        [("run.planning.started", {"detail": "planning"})],
        run_id="20260727-153045-b2c3d4e5",
        start_sequence=2,
        previous_digest=first[0]["event_digest"],
    )
    with pytest.raises(run_projector.EventChainError):
        run_projector.project_run_snapshot(base, mixed, journal_present=True)


def test_invalid_envelope_mapping_raises_event_chain_error():
    env = dict(_build_chain([("run.created", {"status": "started"})])[0])
    env["event_type"] = "run.unknown"
    with pytest.raises(run_projector.EventChainError):
        run_projector.project_run_snapshot(_minimal_base(), [env], journal_present=True)


def test_mutated_typed_run_event_raises_event_chain_error():
    env = _build_chain([("run.created", {"status": "started"})])[0]
    event = run_journal.RunEvent(**env)
    mutated = dataclasses.replace(event, payload={"status": "ok"})
    with pytest.raises(run_projector.EventChainError):
        run_projector.project_run_snapshot(_minimal_base(), [mutated], journal_present=True)


def test_full_coverage_base_preservation_deep_equals():
    base = _load_golden_base()
    assert run_projector.PRESERVED_FIELDS <= set(base.keys())
    projection = run_projector.project_run_snapshot(base, [], journal_present=False)
    snapshot = projection.snapshot
    for key in run_projector.PRESERVED_FIELDS:
        assert snapshot[key] == base[key]
    preserved_in_output = set(snapshot.keys()) & run_projector.PRESERVED_FIELDS
    assert preserved_in_output == set(base.keys()) & run_projector.PRESERVED_FIELDS
    assert snapshot["failure"] is not base["failure"]
    assert snapshot["active_seats"] is not base["active_seats"]


def test_reprojection_is_idempotent():
    events = _read_golden_events()
    first = run_projector.project_run_snapshot(_load_golden_base(), events, journal_present=True)
    second = run_projector.project_run_snapshot(first.snapshot, events, journal_present=True)
    assert first.to_bytes() == second.to_bytes()


def test_to_bytes_matches_run_json_encoding():
    projection = run_projector.project_run_snapshot(_minimal_base(), [], journal_present=False)
    expected = (json.dumps(projection.snapshot, indent=2, sort_keys=True) + "\n").encode("utf-8")
    assert projection.to_bytes() == expected
    assert run_projector.encode_snapshot_bytes(projection.snapshot) == expected


def test_unencodable_preserved_value_raises_snapshot_encoding_error():
    base = _minimal_base()
    base["failure"] = {"phase": "dispatch", "detail": object()}
    with pytest.raises(run_projector.SnapshotEncodingError) as excinfo:
        run_projector.project_run_snapshot(base, [], journal_present=False)
    assert len(excinfo.value.diagnostic) <= run_events.MAX_DIAGNOSTIC_LEN

    circular = _minimal_base()
    loop: dict = {}
    loop["self"] = loop
    circular["failure"] = loop
    with pytest.raises(run_projector.SnapshotEncodingError) as excinfo:
        run_projector.project_run_snapshot(circular, [], journal_present=False)
    assert len(excinfo.value.diagnostic) <= run_events.MAX_DIAGNOSTIC_LEN
    assert "Circular" not in excinfo.value.diagnostic


@pytest.mark.parametrize("value", ["yes", 1, None])
def test_non_boolean_journal_present_raises_projection_input_error(value):
    with pytest.raises(run_projector.ProjectionInputError):
        run_projector.project_run_snapshot(_minimal_base(), [], journal_present=value)
```

- [x] Run the RED command and confirm the expected failure:

```bash
python3 -m pytest tests/test_run_projector.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'brigade.run_projector'`. Any other failure means the test file has a syntax or import error. Fix the test file before continuing.

## Task 2: production module

- [ ] Create `src/brigade/run_projector.py` with exactly this content:

```python
"""Pure run snapshot projector (issue #568, slice 3).

Derives a complete ``run.json`` snapshot from a base snapshot plus a verified
lifecycle event sequence. The projector owns exactly five derived fields
(``status``, ``projector_version``, ``journal_present``,
``journal_last_sequence``, ``journal_last_event_digest``) and deep-copies the
41 preserved fields of the current ``run.json`` contract verbatim from the
base. Every envelope is re-validated through ``run_events.validate_event``
and the chain is re-verified (contiguous sequence from 1, previous-digest
linkage, single run_id) even when the caller already read the events through
``run_journal.read_journal``. Any deviation fails closed with a bounded typed
error; there is no partial projection.

The projector performs no I/O, reads no clock, reads no environment, and holds
no mutable module state. Nothing in the runtime calls it in this slice.
Standard library only. Brigade is zero-runtime-dependency.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from brigade import run_events, run_journal

PROJECTOR_VERSION: int = 1

# Field ownership over the run.json contract. Every current run.json key is
# in exactly one of these two sets; any other base key stops projection.
DERIVED_FIELDS: frozenset[str] = frozenset(
    {
        "status",
        "projector_version",
        "journal_present",
        "journal_last_sequence",
        "journal_last_event_digest",
    }
)

PRESERVED_FIELDS: frozenset[str] = frozenset(
    {
        # Identity and schema
        "schema",
        "schema_version",
        "task",
        "orchestrator",
        "roster",
        "worker",
        "scheduler",
        # Run configuration
        "dry_run",
        "read_only",
        "cwd",
        "lock_workspace",
        "codex_transport",
        "lifecycle_journal_requested",
        # Timing
        "started_at",
        "status_started_at",
        "finished_at",
        "duration_seconds",
        # Briefs and routing telemetry
        "code_graph_brief",
        "drift_impact_brief",
        "evidence_brief",
        "brief_budget",
        "route",
        "skill_route_policy",
        "pre_run_snapshot",
        "git",
        "code_graph_delta",
        "context_eval",
        "suspected_noop",
        # Control transport
        "control_transport",
        "control_socket",
        # Live progress (record_dispatch_stage, record_result_processing)
        "active_stage",
        "active_seats",
        "phase_owner",
        # Outcome and failure
        "error",
        "failure_phase",
        "failure_kind",
        "failure",
        "transport_warning",
        "artifact_collection",
        # Artifact references
        "artifacts",
        "handoff",
    }
)

OWNED_FIELDS: frozenset[str] = DERIVED_FIELDS | PRESERVED_FIELDS

# Payload-independent rows of the event-to-status mapping. The remaining four
# mapped event types carry their run.json status in the payload and are
# handled by _EVENT_PAYLOAD_STATUS.
EVENT_STATUS: dict[str, str] = {
    "run.planning.started": "planning",
    "run.dispatch.requested": "dispatching",
    "run.dispatch.completed": "result-processing",
    "run.synthesis.started": "synthesizing",
    "run.synthesis.completed": "handoff",
}

# Payload-dependent rows: event_type -> allowed payload status values. The
# derived status is the payload value itself after the membership check.
_EVENT_PAYLOAD_STATUS: dict[str, frozenset[str]] = {
    "run.created": frozenset({"started"}),
    "run.completed": frozenset({"ok"}),
    "run.failed": frozenset({"failed", "timeout"}),
    "run.interrupted": frozenset({"canceled"}),
}

_ENCODING_CATEGORY = "snapshot cannot be encoded under the run.json JSON contract"


class ProjectionError(RuntimeError):
    """Base class for projection failures. Carries a bounded ``diagnostic``."""

    def __init__(self, diagnostic: str) -> None:
        super().__init__(diagnostic)
        self.diagnostic = diagnostic


class UnknownSnapshotFieldError(ProjectionError):
    """The base snapshot carried a key outside ``OWNED_FIELDS``."""


class UnmappedEventTypeError(ProjectionError):
    """A validated event's type has no status mapping."""


class EventChainError(ProjectionError):
    """Envelope validation or chain verification failed."""


class EventPayloadError(ProjectionError):
    """A payload status rule failed for a mapped event type."""


class ProjectionInputError(ProjectionError):
    """An input argument violated the projector contract."""


class SnapshotEncodingError(ProjectionError):
    """Projected values cannot be encoded by the run.json JSON contract."""


@dataclass(frozen=True)
class RunProjection:
    """Result of a successful projection."""

    snapshot: dict[str, Any]
    status: str
    journal_present: bool
    last_sequence: int
    last_event_digest: str | None

    def to_bytes(self) -> bytes:
        return encode_snapshot_bytes(self.snapshot)


def encode_snapshot_bytes(snapshot: Mapping[str, Any]) -> bytes:
    """Encode a snapshot with the existing run.json byte contract.

    Sorted keys, two-space indent, trailing newline, UTF-8, matching
    ``aboyeur._write_json`` byte for byte. Encoding failures (non-JSON values,
    circular references, recursion or overflow) surface as a bounded
    category-only SnapshotEncodingError, never as a raw serializer exception.
    """
    try:
        return (json.dumps(snapshot, indent=2, sort_keys=True) + "\n").encode("utf-8")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise SnapshotEncodingError(_ENCODING_CATEGORY) from exc


def _validated_envelopes(
    events: Sequence[run_journal.RunEvent | Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Normalize every item to an envelope mapping and re-validate it."""
    if isinstance(events, (str, bytes)) or not isinstance(events, Sequence):
        raise ProjectionInputError("events must be a sequence of envelopes")
    envelopes: list[Mapping[str, Any]] = []
    for index, item in enumerate(events, start=1):
        env = item.to_dict() if isinstance(item, run_journal.RunEvent) else item
        errors = run_events.validate_event(env)
        if errors:
            raise EventChainError(run_events._bound(f"event envelope invalid at sequence {index}"))
        envelopes.append(env)
    return envelopes


def _event_status(env: Mapping[str, Any]) -> str:
    """Map one validated envelope to its run.json status, enforcing payload rules."""
    event_type = env["event_type"]
    static = EVENT_STATUS.get(event_type)
    if static is not None:
        return static
    expected = _EVENT_PAYLOAD_STATUS.get(event_type)
    if expected is None:
        raise UnmappedEventTypeError(
            run_events._bound(f"event type {event_type} has no status mapping at sequence {env['sequence']}")
        )
    payload = env["payload"]
    value = payload.get("status")
    if not isinstance(value, str) or value not in expected:
        wanted = ", ".join(sorted(expected))
        raise EventPayloadError(
            run_events._bound(f"event type {event_type} requires payload status in {{{wanted}}}")
        )
    return value


def project_run_snapshot(
    base_snapshot: Mapping[str, Any],
    events: Sequence[run_journal.RunEvent | Mapping[str, Any]],
    *,
    journal_present: bool,
) -> RunProjection:
    """Project a complete run.json snapshot from a base plus a verified event sequence.

    Fail closed: unknown base keys, invalid envelopes, chain breaks, mixed
    run_id values, unmapped event types, payload rule violations, and
    unencodable preserved values each raise a typed ProjectionError subclass
    with a bounded diagnostic. An empty event sequence is valid and preserves
    status from the base.
    """
    if not isinstance(journal_present, bool):
        raise ProjectionInputError("journal_present must be a real boolean")
    if not isinstance(base_snapshot, Mapping):
        raise ProjectionInputError("base snapshot must be a mapping")

    unknown = sorted(set(base_snapshot.keys()) - OWNED_FIELDS)
    if unknown:
        raise UnknownSnapshotFieldError(
            run_events._bound("base snapshot carries unmapped fields: " + ", ".join(unknown[:8]))
        )

    envelopes = _validated_envelopes(events)

    last_sequence = 0
    last_event_digest: str | None = None
    run_id: str | None = None
    status: str | None = None
    for env in envelopes:
        sequence = env["sequence"]
        if sequence != last_sequence + 1:
            raise EventChainError(run_events._bound(f"event chain sequence break at sequence {sequence}"))
        previous_digest = env["previous_digest"]
        if sequence == 1:
            if previous_digest is not None:
                raise EventChainError("event chain sequence 1 previous_digest must be null")
        elif previous_digest != last_event_digest:
            raise EventChainError(run_events._bound(f"event chain digest link break at sequence {sequence}"))
        if run_id is None:
            run_id = env["run_id"]
        elif env["run_id"] != run_id:
            raise EventChainError(run_events._bound(f"event chain mixes run_id at sequence {sequence}"))
        status = _event_status(env)
        last_sequence = sequence
        last_event_digest = env["event_digest"]

    if status is None:
        base_status = base_snapshot.get("status")
        if not isinstance(base_status, str):
            raise ProjectionInputError("base snapshot must carry a string status when events is empty")
        status = base_status

    snapshot: dict[str, Any] = {}
    for key in PRESERVED_FIELDS:
        if key in base_snapshot:
            snapshot[key] = deepcopy(base_snapshot[key])
    snapshot["status"] = status
    snapshot["projector_version"] = PROJECTOR_VERSION
    snapshot["journal_present"] = journal_present
    snapshot["journal_last_sequence"] = last_sequence
    snapshot["journal_last_event_digest"] = last_event_digest

    # Fail closed at projection time on preserved values the run.json JSON
    # contract cannot encode; to_bytes() re-encodes deterministically.
    encode_snapshot_bytes(snapshot)

    return RunProjection(
        snapshot=snapshot,
        status=status,
        journal_present=journal_present,
        last_sequence=last_sequence,
        last_event_digest=last_event_digest,
    )


__all__ = [
    "DERIVED_FIELDS",
    "EVENT_STATUS",
    "OWNED_FIELDS",
    "PRESERVED_FIELDS",
    "PROJECTOR_VERSION",
    "EventChainError",
    "EventPayloadError",
    "ProjectionError",
    "ProjectionInputError",
    "RunProjection",
    "SnapshotEncodingError",
    "UnknownSnapshotFieldError",
    "UnmappedEventTypeError",
    "encode_snapshot_bytes",
    "project_run_snapshot",
]
```

Implementation notes that resolve the spec's implicit corners (these are decisions, not open questions):

- The empty-sequence rule preserves `status` from the base. Because the invariants require all five derived fields in every output and every failure to be a `ProjectionError` subclass, a base without a string `status` on an empty sequence raises `ProjectionInputError` (category-only diagnostic). Every real base from `aboyeur._run_payload` carries a string status, so this only fires on synthetic input.
- Every event in the sequence must map to a status. The mapping walk computes each event's status (raising `UnmappedEventTypeError` or `EventPayloadError` on the offending event) and keeps the final one, matching the spec's "nothing is skipped silently" rule.
- Diagnostics follow the spec's bounded-error table: categories plus field names, event types, sequences, and expected value sets only. Raw payload values, envelope bytes, paths, and snapshot contents never appear. Bounding reuses `run_events._bound` (240 chars, ellipsis), the same cross-module reuse `run_lifecycle.py` already makes.

- [ ] Run the focused suite and confirm the expected intermediate state:

```bash
python3 -m pytest tests/test_run_projector.py -q
```

Expected: 17 passed, 5 failed, and every failure is `Failed: missing golden fixture: .../golden-projection.base.json` from tests 1, 2, 5, 15, and 16. (Two tests are parametrized, so the 19 test functions collect as 22 items.) Any other failure is a real bug in the module. Fix it before continuing.

## Task 3: golden base fixture

- [ ] Create `tests/fixtures/run-lifecycle/golden-projection.base.json` with exactly this content (all 41 preserved fields, nested objects, lists, and optional fields, plus the derived `status` key the live writer always carries):

```json
{
  "schema": "brigade.run.v1",
  "schema_version": 1,
  "task": "Project the lifecycle journal",
  "orchestrator": "chef",
  "roster": {
    "path": ".brigade/roster.toml",
    "source": "project",
    "shadowed": []
  },
  "worker": "coder",
  "scheduler": {
    "requested": "auto",
    "used": "serial"
  },
  "dry_run": false,
  "read_only": false,
  "cwd": "/work/repo",
  "lock_workspace": "/work/repo",
  "codex_transport": "app-server",
  "lifecycle_journal_requested": true,
  "started_at": "2026-07-27T15:30:45.123456+00:00",
  "status_started_at": "2026-07-27T15:30:49.000000+00:00",
  "finished_at": "2026-07-27T15:30:50.000000+00:00",
  "duration_seconds": 4.877,
  "code_graph_brief": {
    "attached": true,
    "bytes": 2048
  },
  "drift_impact_brief": {
    "attached": false,
    "bytes": 0,
    "pending_count": 0
  },
  "evidence_brief": {
    "attached": true,
    "bytes": 512
  },
  "brief_budget": {
    "bytes": 8192,
    "attached": [
      "code-graph"
    ]
  },
  "route": {
    "skill": "brigade-work",
    "matched": [
      "run"
    ],
    "score": 3
  },
  "skill_route_policy": {
    "policy_applied": true,
    "extensions": [
      "force-verify"
    ]
  },
  "pre_run_snapshot": {
    "head": "abc123",
    "dirty": false
  },
  "git": {
    "head": "abc123",
    "branch": "main",
    "dirty": false
  },
  "code_graph_delta": {
    "summary": "no drift",
    "changed": 0
  },
  "context_eval": {
    "verdict": "pass",
    "score": 0.9
  },
  "suspected_noop": false,
  "control_transport": {
    "kind": "unix",
    "path": "/run/brigade/ctl.sock"
  },
  "control_socket": "/run/brigade/ctl.sock",
  "active_stage": 2,
  "active_seats": [
    "coder",
    "reviewer"
  ],
  "phase_owner": "chef",
  "error": null,
  "failure_phase": null,
  "failure_kind": null,
  "failure": {
    "phase": "dispatch",
    "kind": "worker-error",
    "detail": "bounded detail",
    "seat": "coder"
  },
  "transport_warning": {
    "kind": "retry",
    "attempts": 1
  },
  "artifact_collection": {
    "status": "ok"
  },
  "artifacts": "/work/repo/.brigade/runs/20260727-153045-a1b2c3d4",
  "handoff": "/work/repo/.brigade/runs/20260727-153045-a1b2c3d4/handoff.md",
  "status": "handoff"
}
```

- [ ] Confirm the fixture carries every preserved field and nothing outside the ownership map:

```bash
PYTHONPATH=src python3 - <<'PY'
import json
from pathlib import Path
from brigade import run_projector

base = json.loads(Path("tests/fixtures/run-lifecycle/golden-projection.base.json").read_text(encoding="utf-8"))
missing = run_projector.PRESERVED_FIELDS - set(base.keys())
extra = set(base.keys()) - run_projector.OWNED_FIELDS
assert not missing, f"fixture missing preserved fields: {sorted(missing)}"
assert not extra, f"fixture carries unmapped fields: {sorted(extra)}"
print(f"base fixture covers all {len(run_projector.PRESERVED_FIELDS)} preserved fields")
PY
```

Expected output: `base fixture covers all 41 preserved fields`.

- [ ] Run the focused suite and confirm the expected intermediate state:

```bash
python3 -m pytest tests/test_run_projector.py -q
```

Expected: 21 passed, 1 failed, and the one failure is `Failed: missing golden fixture: .../golden-projection.expected.json` from `test_golden_replay_matches_expected_bytes`.

## Task 4: generate and hand-review the golden expected fixture

The convention from `golden-lifecycle.jsonl` applies: the implementation generates the expected bytes once, a human reviews them, and only then are they committed.

- [ ] Generate `tests/fixtures/run-lifecycle/golden-projection.expected.json`:

```bash
PYTHONPATH=src python3 - <<'PY'
import json
from pathlib import Path
from brigade import run_journal, run_projector

fixtures = Path("tests/fixtures/run-lifecycle")
base = json.loads((fixtures / "golden-projection.base.json").read_text(encoding="utf-8"))
report = run_journal.read_journal(fixtures / "golden-lifecycle.jsonl")
assert report.partial_tail is None and report.chain_errors == []
projection = run_projector.project_run_snapshot(base, report.events, journal_present=True)
(fixtures / "golden-projection.expected.json").write_bytes(projection.to_bytes())
print("wrote", fixtures / "golden-projection.expected.json")
PY
```

- [ ] Hand-review the generated bytes against this checklist. The review is the point of the golden convention, so do not skip it:

```bash
PYTHONPATH=src python3 - <<'PY'
import json
from pathlib import Path

raw = Path("tests/fixtures/run-lifecycle/golden-projection.expected.json").read_bytes()
assert raw.endswith(b"\n") and not raw.endswith(b"\n\n"), "exactly one trailing newline"
data = json.loads(raw)
assert len(data) == 46, f"expected 46 owned keys, got {len(data)}"
assert list(data) == sorted(data), "keys must be sorted"
assert data["status"] == "ok"
assert data["projector_version"] == 1
assert data["journal_present"] is True
assert data["journal_last_sequence"] == 6
assert data["journal_last_event_digest"] == "176259cd00408b637f1c13496fcdfafea7dd96753fb8a9e76dbea58817442fc7"
base = json.loads(Path("tests/fixtures/run-lifecycle/golden-projection.base.json").read_text(encoding="utf-8"))
for key, value in base.items():
    if key != "status":
        assert data[key] == value, f"preserved field drifted: {key}"
print("golden expected fixture reviewed ok")
PY
```

Also read the file top to bottom and confirm by eye: two-space indent, preserved nested values (`failure`, `code_graph_brief`, `control_transport`, `active_seats`) match the base verbatim, and no key outside the 46 owned fields appears. Expected output: `golden expected fixture reviewed ok`.

- [ ] Run the focused suite to green:

```bash
python3 -m pytest tests/test_run_projector.py -q
```

Expected: `22 passed` (19 test functions, two of them parametrized).

## Task 5: full verification

- [ ] Focused suite plus the neighboring journal and lifecycle suites:

```bash
python3 -m pytest tests/test_run_projector.py tests/test_run_events.py tests/test_run_journal.py tests/test_run_lifecycle.py -q
```

Expected: all passed, no regressions in the slice-1 and slice-2 suites.

- [ ] Full repository suite:

```bash
python3 -m pytest -q
```

Expected: all passed.

- [ ] Lint and type gates:

```bash
python3 -m ruff check src/brigade/run_projector.py tests/test_run_projector.py
python3 -m mypy src/brigade/run_projector.py
```

Expected: clean (no findings, `Success: no issues found`).

- [ ] Diff review: confirm only the four new files exist in the change set and no existing file is modified:

```bash
git status --porcelain
```

Expected: exactly four new paths (`src/brigade/run_projector.py`, `tests/test_run_projector.py`, `tests/fixtures/run-lifecycle/golden-projection.base.json`, `tests/fixtures/run-lifecycle/golden-projection.expected.json`) plus any docs from this planning step. Zero `M` lines.

## Task 6: commit

- [ ] Stage and commit with a conventional message:

```bash
git add src/brigade/run_projector.py \
  tests/test_run_projector.py \
  tests/fixtures/run-lifecycle/golden-projection.base.json \
  tests/fixtures/run-lifecycle/golden-projection.expected.json
git commit -m "feat(runs): add pure run snapshot projector (#568)"
```

This is an in-house repo, so append the `Co-authored-by` trailer for the implementing seat per the trailer policy (for example `Co-authored-by: Codex <codex@openai.com>`), matching the #607 and #608 slice commits. Add a trailer only for a tool that did substantial work on the commit.

## Task 7: Brigade verification

Per the mandatory Brigade work loop and Definition of Done in `AGENTS.md`,
the final result must run the complete repository gate through Brigade:

```bash
brigade work verify run --target . --command "./scripts/verify" --capture brigade-work
```

Expected: the receipt records ruff lint, ruff format check, version sync,
mypy, and the full pytest suite with its coverage floor. Atomic
`--capture brigade-work` records the outcome once. Do not run a second
`brigade outcome capture` for the same receipt.
