"""Tests for brigade.run_journal append-only lifecycle journal kernel."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from copy import deepcopy
from pathlib import Path

import pytest

from brigade import run_events, run_journal

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "run-lifecycle"
GOLDEN_LIFECYCLE_PATH = FIXTURES / "golden-lifecycle.jsonl"

RUN_ID = "20260727-153045-a1b2c3d4"
RECORDED_AT = "2026-07-27T15:30:45.123456Z"

_GOLDEN_APPEND_PLAN = (
    {
        "event_type": "run.created",
        "payload": {"status": "started"},
        "idempotency_key": "create-1",
        "recorded_at": "2026-07-27T15:30:45.123456Z",
    },
    {
        "event_type": "run.planning.started",
        "payload": {"detail": "planning"},
        "idempotency_key": "plan-start-1",
        "recorded_at": "2026-07-27T15:30:46.000000Z",
    },
    {
        "event_type": "run.dispatch.requested",
        "payload": {"seat": "coder", "attempt": 1},
        "idempotency_key": "dispatch-req-1",
        "recorded_at": "2026-07-27T15:30:47.000000Z",
    },
    {
        "event_type": "run.dispatch.completed",
        "payload": {"seat": "coder", "attempt": 1, "detail": "ok"},
        "idempotency_key": "dispatch-done-1",
        "recorded_at": "2026-07-27T15:30:48.000000Z",
    },
    {
        "event_type": "run.synthesis.completed",
        "payload": {"detail": "synthesized"},
        "idempotency_key": "synthesis-done-1",
        "recorded_at": "2026-07-27T15:30:49.000000Z",
    },
    {
        "event_type": "run.completed",
        "payload": {"status": "ok", "detail": "done"},
        "idempotency_key": "complete-1",
        "recorded_at": "2026-07-27T15:30:50.000000Z",
    },
)


def _run_dir(tmp_path: Path) -> Path:
    run_dir = tmp_path / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    return run_dir


def _journal_path(run_dir: Path) -> Path:
    return run_dir / "events" / "lifecycle.jsonl"


def _append(
    journal_path: Path,
    *,
    event_type: str,
    payload: dict,
    idempotency_key: str,
    expected_previous_sequence: int,
    recorded_at: str,
) -> run_journal.RunEvent:
    return run_journal.append_event(
        journal_path,
        run_id=RUN_ID,
        event_type=event_type,
        payload=payload,
        idempotency_key=idempotency_key,
        expected_previous_sequence=expected_previous_sequence,
        recorded_at=recorded_at,
    )


def _append_first_event(journal_path: Path) -> run_journal.RunEvent:
    return _append(
        journal_path,
        event_type="run.created",
        payload={"status": "started"},
        idempotency_key="create-1",
        expected_previous_sequence=0,
        recorded_at=RECORDED_AT,
    )


def test_ensure_journal_creates_private_directory_and_file(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    run_journal.ensure_journal(journal_path)

    assert journal_path.is_file()
    assert journal_path.parent.is_dir()
    assert stat.S_IMODE(journal_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal_path.parent.stat().st_mode) == 0o700


def test_append_event_writes_canonical_line_with_fsync(tmp_path, monkeypatch):
    journal_path = _journal_path(_run_dir(tmp_path))
    fsync_calls: list[int] = []

    def _track_fsync(fd):
        fsync_calls.append(fd)

    monkeypatch.setattr(os, "fsync", _track_fsync)

    event = _append_first_event(journal_path)

    lines = journal_path.read_bytes().splitlines(keepends=True)
    assert len(lines) == 1
    assert lines[0] == run_events.canonical_bytes(event.to_dict()) + b"\n"
    assert fsync_calls


def test_append_event_rejects_partial_os_write(tmp_path, monkeypatch):
    journal_path = _journal_path(_run_dir(tmp_path))
    original_write = os.write

    def _short_write(fd, data):
        return original_write(fd, data[: max(1, len(data) // 2)])

    monkeypatch.setattr(os, "write", _short_write)

    with pytest.raises(run_journal.PartialWriteError):
        _append_first_event(journal_path)


def test_idempotent_replay_returns_existing_event_without_second_append(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    first = _append_first_event(journal_path)
    before = journal_path.read_bytes()

    replay = _append(
        journal_path,
        event_type="run.created",
        payload={"status": "started"},
        idempotency_key="create-1",
        expected_previous_sequence=1,
        recorded_at=RECORDED_AT,
    )

    assert replay.event_id == first.event_id
    assert replay.request_digest == first.request_digest
    assert replay.event_digest == first.event_digest
    assert journal_path.read_bytes() == before


def test_same_idempotency_key_different_digest_raises_without_mutation(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    before = journal_path.read_bytes()

    with pytest.raises(run_journal.IdempotencyConflict) as excinfo:
        _append(
            journal_path,
            event_type="run.created",
            payload={"status": "changed"},
            idempotency_key="create-1",
            expected_previous_sequence=1,
            recorded_at=RECORDED_AT,
        )

    conflict = excinfo.value
    assert conflict.existing_event_id
    assert conflict.request_digest != conflict.existing_request_digest
    assert len(conflict.diagnostic) <= 240
    assert journal_path.read_bytes() == before


def test_stale_expected_previous_sequence_raises_without_append(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    before = journal_path.read_bytes()

    with pytest.raises(run_journal.StaleSequenceError) as excinfo:
        _append(
            journal_path,
            event_type="run.planning.started",
            payload={"detail": "planning"},
            idempotency_key="plan-start-1",
            expected_previous_sequence=0,
            recorded_at="2026-07-27T15:30:46.000000Z",
        )

    assert len(excinfo.value.diagnostic) <= 240
    assert journal_path.read_bytes() == before


def _huge_json_integer_line() -> bytes:
    return ("1" + "0" * 4999).encode() + b"\n"


def test_append_fails_closed_on_huge_json_integer_line(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    journal_path.write_bytes(journal_path.read_bytes() + _huge_json_integer_line())
    before = journal_path.read_bytes()

    with pytest.raises(run_journal.ChainIntegrityError) as excinfo:
        _append_second_event(journal_path)

    assert len(excinfo.value.diagnostic) <= 240
    assert "5000" not in excinfo.value.diagnostic
    assert journal_path.read_bytes() == before


def test_read_journal_stops_at_huge_json_integer_line(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    first = _append_first_event(journal_path)
    journal_path.write_bytes(journal_path.read_bytes() + _huge_json_integer_line())

    report = run_journal.read_journal(journal_path)

    assert len(report.events) == 1
    assert report.events[0].event_id == first.event_id
    assert len(report.chain_errors) == 1
    assert len(report.chain_errors[0]) <= 240
    assert "5000" not in report.chain_errors[0]


def test_read_journal_verified_prefix_stops_after_tampered_middle_line(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    first = _append_first_event(journal_path)
    _append_second_event(journal_path)
    _append(
        journal_path,
        event_type="run.dispatch.requested",
        payload={"seat": "coder", "attempt": 1},
        idempotency_key="dispatch-req-1",
        expected_previous_sequence=2,
        recorded_at="2026-07-27T15:30:47.000000Z",
    )

    lines = journal_path.read_text().splitlines()
    broken = json.loads(lines[1])
    broken["previous_digest"] = "0" * 64
    lines[1] = run_events.canonical_bytes(broken).decode("utf-8")
    journal_path.write_text("\n".join(lines) + "\n")

    report = run_journal.read_journal(journal_path)

    assert len(report.events) == 1
    assert report.events[0].event_id == first.event_id
    assert report.chain_errors
    assert any("previous_digest" in err or "digest" in err for err in report.chain_errors)


def test_recover_partial_tail_raises_on_missing_journal(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    quarantine_dir = tmp_path / "quarantine"

    with pytest.raises(run_journal.RunJournalError) as excinfo:
        run_journal.recover_partial_tail(journal_path, quarantine_dir)

    assert len(excinfo.value.diagnostic) <= 240
    assert journal_path.name in excinfo.value.diagnostic


def test_read_journal_detects_sequence_gap_without_mutation(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    first = _append_first_event(journal_path)
    second = _append(
        journal_path,
        event_type="run.planning.started",
        payload={"detail": "planning"},
        idempotency_key="plan-start-1",
        expected_previous_sequence=1,
        recorded_at="2026-07-27T15:30:46.000000Z",
    )

    lines = journal_path.read_text().splitlines()
    gap_line = deepcopy(json.loads(lines[1]))
    gap_line["sequence"] = 3
    gap_line["event_id"] = gap_line["event_id"].replace("-000002-", "-000003-")
    lines[1] = run_events.canonical_bytes(gap_line).decode("utf-8")
    journal_path.write_text("\n".join(lines) + "\n")
    before = journal_path.read_bytes()

    report = run_journal.read_journal(journal_path)

    assert report.partial_tail is None
    assert len(report.events) == 1
    assert report.events[0].event_id == first.event_id
    assert report.chain_errors
    assert any("sequence" in err for err in report.chain_errors)
    assert journal_path.read_bytes() == before
    assert first.event_id != second.event_id


def test_read_journal_detects_previous_digest_mismatch_without_mutation(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    _append(
        journal_path,
        event_type="run.planning.started",
        payload={"detail": "planning"},
        idempotency_key="plan-start-1",
        expected_previous_sequence=1,
        recorded_at="2026-07-27T15:30:46.000000Z",
    )

    lines = journal_path.read_text().splitlines()
    broken = json.loads(lines[1])
    broken["previous_digest"] = "0" * 64
    lines[1] = run_events.canonical_bytes(broken).decode("utf-8")
    journal_path.write_text("\n".join(lines) + "\n")
    before = journal_path.read_bytes()

    report = run_journal.read_journal(journal_path)

    assert len(report.events) == 1
    assert report.chain_errors
    assert any("previous_digest" in err or "digest" in err for err in report.chain_errors)
    assert journal_path.read_bytes() == before


def test_read_journal_reports_partial_final_line_without_mutation(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    partial_suffix = b'{"schema":"brigade.run_event.v1","event_type":"run.plan'
    journal_path.write_bytes(journal_path.read_bytes() + partial_suffix)
    before = journal_path.read_bytes()

    report = run_journal.read_journal(journal_path)

    assert report.partial_tail == partial_suffix
    assert len(report.events) == 1
    assert journal_path.read_bytes() == before


def test_append_refuses_to_write_over_partial_tail_without_mutation(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    partial_suffix = b'{"schema":"brigade.run_event.v1","event_type":"run.plan'
    journal_path.write_bytes(journal_path.read_bytes() + partial_suffix)
    before = journal_path.read_bytes()

    with pytest.raises(run_journal.PartialTailError) as excinfo:
        _append(
            journal_path,
            event_type="run.planning.started",
            payload={"detail": "planning"},
            idempotency_key="plan-start-1",
            expected_previous_sequence=1,
            recorded_at="2026-07-27T15:30:46.000000Z",
        )

    assert len(excinfo.value.diagnostic) <= 240
    assert journal_path.read_bytes() == before


def test_recover_partial_tail_quarantines_suffix_then_truncates(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    partial_suffix = b'{"schema":"brigade.run_event.v1","event_type":"run.plan'
    journal_path.write_bytes(journal_path.read_bytes() + partial_suffix)
    complete_bytes = journal_path.read_bytes()[: -len(partial_suffix)]
    quarantine_dir = tmp_path / "quarantine"

    report = run_journal.recover_partial_tail(journal_path, quarantine_dir)

    assert report.partial_bytes == partial_suffix
    assert report.quarantine_path.is_file()
    assert report.quarantine_path.read_bytes() == partial_suffix
    assert stat.S_IMODE(report.quarantine_path.stat().st_mode) == 0o600
    assert journal_path.read_bytes() == complete_bytes


def test_golden_lifecycle_journal_matches_fixture_bytes(tmp_path):
    if not GOLDEN_LIFECYCLE_PATH.is_file():
        pytest.fail(f"missing golden fixture: {GOLDEN_LIFECYCLE_PATH}")

    golden_bytes = GOLDEN_LIFECYCLE_PATH.read_bytes()
    journal_path = _journal_path(_run_dir(tmp_path))
    expected_previous_sequence = 0

    for step in _GOLDEN_APPEND_PLAN:
        _append(
            journal_path,
            event_type=step["event_type"],
            payload=step["payload"],
            idempotency_key=step["idempotency_key"],
            expected_previous_sequence=expected_previous_sequence,
            recorded_at=step["recorded_at"],
        )
        expected_previous_sequence += 1

    assert journal_path.read_bytes() == golden_bytes

    report = run_journal.read_journal(journal_path)
    assert report.partial_tail is None
    assert report.chain_errors == []
    assert len(report.events) == len(_GOLDEN_APPEND_PLAN)


def test_read_journal_rejects_unknown_envelope_fields_in_complete_lines(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    event = _append_first_event(journal_path)
    tampered = event.to_dict()
    tampered["unexpected"] = "field"
    journal_path.write_text(run_events.canonical_bytes(tampered).decode("utf-8") + "\n")

    report = run_journal.read_journal(journal_path)

    assert report.chain_errors
    assert len(report.chain_errors[0]) <= 240
    assert "unexpected" in report.chain_errors[0]


def _write_journal_lines(journal_path: Path, *envelopes: dict) -> None:
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_bytes(b"".join(run_events.canonical_bytes(env) + b"\n" for env in envelopes))


def _build_envelope(
    *, sequence: int, event_type: str, payload: dict, idempotency_key: str, recorded_at: str, previous_digest
) -> dict:
    return run_events.build_event(
        run_id=RUN_ID,
        sequence=sequence,
        event_type=event_type,
        payload=payload,
        idempotency_key=idempotency_key,
        recorded_at=recorded_at,
        previous_digest=previous_digest,
    )


def _append_second_event(journal_path: Path, *, expected_previous_sequence: int = 1) -> run_journal.RunEvent:
    return _append(
        journal_path,
        event_type="run.planning.started",
        payload={"detail": "planning"},
        idempotency_key="plan-start-1",
        expected_previous_sequence=expected_previous_sequence,
        recorded_at="2026-07-27T15:30:46.000000Z",
    )


def test_append_fails_closed_on_reordered_complete_lines(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    _append_second_event(journal_path)
    lines = journal_path.read_bytes().splitlines()
    journal_path.write_bytes(lines[1] + b"\n" + lines[0] + b"\n")
    before = journal_path.read_bytes()

    with pytest.raises(run_journal.ChainIntegrityError):
        _append(
            journal_path,
            event_type="run.dispatch.requested",
            payload={"seat": "coder", "attempt": 1},
            idempotency_key="dispatch-req-1",
            expected_previous_sequence=2,
            recorded_at="2026-07-27T15:30:47.000000Z",
        )
    assert journal_path.read_bytes() == before


def test_append_fails_closed_on_duplicated_complete_line(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    line = journal_path.read_bytes().splitlines()[0]
    journal_path.write_bytes(line + b"\n" + line + b"\n")
    before = journal_path.read_bytes()

    with pytest.raises(run_journal.ChainIntegrityError):
        _append_second_event(journal_path, expected_previous_sequence=2)
    assert journal_path.read_bytes() == before


def test_append_fails_closed_on_gapped_sequence(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    env1 = _build_envelope(
        sequence=1,
        event_type="run.created",
        payload={"status": "started"},
        idempotency_key="create-1",
        recorded_at=RECORDED_AT,
        previous_digest=None,
    )
    env3 = _build_envelope(
        sequence=3,
        event_type="run.planning.started",
        payload={"detail": "planning"},
        idempotency_key="plan-start-1",
        recorded_at="2026-07-27T15:30:46.000000Z",
        previous_digest=env1["event_digest"],
    )
    _write_journal_lines(journal_path, env1, env3)
    before = journal_path.read_bytes()

    with pytest.raises(run_journal.ChainIntegrityError):
        _append_second_event(journal_path, expected_previous_sequence=3)
    assert journal_path.read_bytes() == before


def test_append_fails_closed_on_broken_previous_digest_link(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    env1 = _build_envelope(
        sequence=1,
        event_type="run.created",
        payload={"status": "started"},
        idempotency_key="create-1",
        recorded_at=RECORDED_AT,
        previous_digest=None,
    )
    env2 = _build_envelope(
        sequence=2,
        event_type="run.planning.started",
        payload={"detail": "planning"},
        idempotency_key="plan-start-1",
        recorded_at="2026-07-27T15:30:46.000000Z",
        previous_digest="0" * 64,
    )
    _write_journal_lines(journal_path, env1, env2)

    with pytest.raises(run_journal.ChainIntegrityError):
        _append_second_event(journal_path, expected_previous_sequence=2)


def test_append_fails_closed_on_digest_invalid_line(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    line = journal_path.read_bytes().splitlines()[0]
    env = json.loads(line.decode("utf-8"))
    env["event_digest"] = "0" * 64
    journal_path.write_bytes(run_events.canonical_bytes(env) + b"\n")

    with pytest.raises(run_journal.ChainIntegrityError):
        _append_second_event(journal_path)


def test_append_fails_closed_on_repeated_idempotency_key(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    env1 = _build_envelope(
        sequence=1,
        event_type="run.created",
        payload={"status": "started"},
        idempotency_key="dup-1",
        recorded_at=RECORDED_AT,
        previous_digest=None,
    )
    env2 = _build_envelope(
        sequence=2,
        event_type="run.planning.started",
        payload={"detail": "planning"},
        idempotency_key="dup-1",
        recorded_at="2026-07-27T15:30:46.000000Z",
        previous_digest=env1["event_digest"],
    )
    _write_journal_lines(journal_path, env1, env2)

    with pytest.raises(run_journal.ChainIntegrityError):
        _append_second_event(journal_path, expected_previous_sequence=2)


@pytest.mark.parametrize(
    "missing_field",
    ["event_id", "event_digest", "idempotency_key", "request_digest", "sequence", "previous_digest"],
)
def test_append_fails_closed_with_typed_error_on_envelope_missing_field(tmp_path, missing_field):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    line = journal_path.read_bytes().splitlines()[0]
    env = json.loads(line.decode("utf-8"))
    del env[missing_field]
    journal_path.write_bytes(
        json.dumps(env, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    )
    before = journal_path.read_bytes()

    # RunJournalError (not KeyError) proves malformed fields become bounded
    # typed errors rather than trusted RunEvent materialization.
    with pytest.raises(run_journal.RunJournalError):
        _append_second_event(journal_path)
    assert journal_path.read_bytes() == before


def test_append_fails_closed_on_invalid_idempotency_entry(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    line = journal_path.read_bytes().splitlines()[0]
    env = json.loads(line.decode("utf-8"))
    env["idempotency_key"] = 42
    journal_path.write_bytes(
        json.dumps(env, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    )

    with pytest.raises(run_journal.RunJournalError):
        _append_second_event(journal_path)


def test_append_fails_closed_on_duplicate_json_keys(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_bytes(b'{"schema":"brigade.run_event.v1","schema":"brigade.run_event.v1","sequence":1}\n')

    with pytest.raises(run_journal.ChainIntegrityError) as excinfo:
        _append_first_event(journal_path)
    assert "schema" in excinfo.value.diagnostic


def test_append_fails_closed_on_whitespace_noncanonical_line(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    line = journal_path.read_bytes().splitlines()[0]
    spaced = json.dumps(json.loads(line.decode("utf-8")), separators=(", ", ": "), ensure_ascii=False).encode("utf-8")
    assert spaced != line
    journal_path.write_bytes(spaced + b"\n")

    with pytest.raises(run_journal.ChainIntegrityError):
        _append_second_event(journal_path)


def test_append_fails_closed_on_ascii_escaped_noncanonical_line(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append(
        journal_path,
        event_type="run.created",
        payload={"status": "café"},
        idempotency_key="create-1",
        expected_previous_sequence=0,
        recorded_at=RECORDED_AT,
    )
    line = journal_path.read_bytes().splitlines()[0]
    escaped = json.dumps(
        json.loads(line.decode("utf-8")), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")
    assert escaped != line
    journal_path.write_bytes(escaped + b"\n")

    with pytest.raises(run_journal.ChainIntegrityError):
        _append_second_event(journal_path)


def test_append_fails_closed_on_unsorted_key_order(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    line = journal_path.read_bytes().splitlines()[0]
    env = json.loads(line.decode("utf-8"))
    reversed_env = dict(reversed(list(env.items())))
    unsorted = json.dumps(reversed_env, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    assert unsorted != line
    journal_path.write_bytes(unsorted + b"\n")

    with pytest.raises(run_journal.ChainIntegrityError):
        _append_second_event(journal_path)


def test_append_fails_closed_on_oversized_integer_line(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    line = journal_path.read_bytes().splitlines()[0]
    env = json.loads(line.decode("utf-8"))
    env["sequence"] = 2**63
    journal_path.write_bytes(
        json.dumps(env, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    )

    with pytest.raises(run_journal.ChainIntegrityError):
        _append_second_event(journal_path)


def test_read_journal_reports_noncanonical_lines_as_chain_errors(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    line = journal_path.read_bytes().splitlines()[0]
    spaced = json.dumps(json.loads(line.decode("utf-8")), separators=(", ", ": "), ensure_ascii=False).encode("utf-8")
    journal_path.write_bytes(spaced + b"\n")

    report = run_journal.read_journal(journal_path)

    assert report.chain_errors
    assert all(len(err) <= 240 for err in report.chain_errors)
    assert report.events == []


def test_ensure_journal_modes_hold_under_permissive_umask(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    previous_umask = os.umask(0)
    try:
        run_journal.ensure_journal(journal_path)
    finally:
        os.umask(previous_umask)

    assert stat.S_IMODE(journal_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(journal_path.stat().st_mode) == 0o600


def test_ensure_journal_corrects_permissive_preexisting_modes(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    parent = journal_path.parent
    parent.mkdir(parents=True)
    os.chmod(parent, 0o777)
    fd = os.open(journal_path, os.O_CREAT | os.O_WRONLY, 0o644)
    os.close(fd)
    os.chmod(journal_path, 0o644)

    run_journal.ensure_journal(journal_path)

    assert stat.S_IMODE(parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(journal_path.stat().st_mode) == 0o600


def test_ensure_journal_rejects_symlinked_events_directory(tmp_path):
    run_dir = _run_dir(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (run_dir / "events").symlink_to(outside, target_is_directory=True)
    journal_path = run_dir / "events" / "lifecycle.jsonl"

    with pytest.raises(run_journal.RunJournalError):
        run_journal.ensure_journal(journal_path)
    assert not (outside / "lifecycle.jsonl").exists()


def test_append_rejects_symlinked_journal_file(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    run_journal.ensure_journal(journal_path)
    journal_path.unlink()
    target = tmp_path / "target.jsonl"
    target.write_bytes(b"")
    journal_path.symlink_to(target)

    with pytest.raises(run_journal.RunJournalError):
        _append_first_event(journal_path)
    assert target.read_bytes() == b""


def test_append_rejects_dangling_symlinked_journal_file(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    journal_path.parent.mkdir(parents=True)
    journal_path.symlink_to(tmp_path / "missing-target")

    with pytest.raises(run_journal.RunJournalError):
        _append_first_event(journal_path)


def test_recover_partial_tail_creates_private_quarantine_dir(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    journal_path.write_bytes(journal_path.read_bytes() + b"partial")
    quarantine_dir = tmp_path / "quarantine"

    report = run_journal.recover_partial_tail(journal_path, quarantine_dir)

    assert stat.S_IMODE(quarantine_dir.stat().st_mode) == 0o700
    assert report.quarantine_path is not None


def test_recover_partial_tail_retries_on_quarantine_name_collision(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    partial_suffix = b'{"schema":"brigade.run_event.v1","event_type":"run.plan'
    journal_path.write_bytes(journal_path.read_bytes() + partial_suffix)
    quarantine_dir = tmp_path / "quarantine"
    quarantine_dir.mkdir()
    digest = hashlib.sha256(partial_suffix).hexdigest()
    occupant = quarantine_dir / f"lifecycle-partial-000001-{digest}.bin"
    occupant.write_bytes(b"occupant")

    report = run_journal.recover_partial_tail(journal_path, quarantine_dir)

    assert report.quarantine_path is not None
    assert report.quarantine_path != occupant
    assert report.quarantine_path.name.startswith(f"lifecycle-partial-000001-{digest}-")
    assert report.quarantine_path.read_bytes() == partial_suffix
    # Write-once: the pre-existing occupant was never overwritten.
    assert occupant.read_bytes() == b"occupant"


def test_recover_partial_tail_returns_none_quarantine_path_without_partial(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    before = journal_path.read_bytes()

    report = run_journal.recover_partial_tail(journal_path, tmp_path / "quarantine")

    assert report.partial_bytes == b""
    assert report.quarantine_path is None
    assert journal_path.read_bytes() == before


def _disable_posix_open_guards(monkeypatch) -> None:
    """Simulate hosts without O_NOFOLLOW, O_DIRECTORY, or fchmod (e.g. Windows)."""
    monkeypatch.setattr(run_journal, "_HAS_O_NOFOLLOW", False)
    monkeypatch.setattr(run_journal, "_HAS_O_DIRECTORY", False)
    monkeypatch.setattr(run_journal, "_HAS_FCHMOD", False)
    monkeypatch.setattr(run_journal, "_OPEN_FLAGS", os.O_WRONLY | os.O_CREAT | os.O_APPEND)


def test_module_constants_use_getattr_for_import_safe_access():
    """Module-level flags are derived via getattr so import succeeds without POSIX APIs."""
    assert isinstance(run_journal._HAS_O_NOFOLLOW, bool)
    assert isinstance(run_journal._HAS_O_DIRECTORY, bool)
    assert isinstance(run_journal._HAS_FCHMOD, bool)
    assert isinstance(run_journal._O_NOFOLLOW, int)
    assert isinstance(run_journal._O_DIRECTORY, int)
    if run_journal._HAS_O_NOFOLLOW:
        assert run_journal._OPEN_FLAGS & run_journal._O_NOFOLLOW
    else:
        assert not (run_journal._OPEN_FLAGS & getattr(os, "O_NOFOLLOW", 0))


def test_fallback_ensure_journal_creates_private_directory_and_file(tmp_path, monkeypatch):
    _disable_posix_open_guards(monkeypatch)
    journal_path = _journal_path(_run_dir(tmp_path))

    run_journal.ensure_journal(journal_path)

    assert journal_path.is_file()
    assert journal_path.parent.is_dir()
    assert stat.S_IMODE(journal_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(journal_path.parent.stat().st_mode) == 0o700


def test_fallback_append_read_and_recover_partial_tail(tmp_path, monkeypatch):
    _disable_posix_open_guards(monkeypatch)
    journal_path = _journal_path(_run_dir(tmp_path))

    event = _append_first_event(journal_path)
    report = run_journal.read_journal(journal_path)
    assert len(report.events) == 1
    assert report.events[0].event_id == event.event_id

    partial_suffix = b'{"schema":"brigade.run_event.v1","event_type":"run.plan'
    journal_path.write_bytes(journal_path.read_bytes() + partial_suffix)
    complete_bytes = journal_path.read_bytes()[: -len(partial_suffix)]

    recovery = run_journal.recover_partial_tail(journal_path, tmp_path / "quarantine")
    assert recovery.partial_bytes == partial_suffix
    assert recovery.quarantine_path is not None
    assert recovery.quarantine_path.read_bytes() == partial_suffix
    assert journal_path.read_bytes() == complete_bytes


def test_fallback_rejects_symlinked_events_directory(tmp_path, monkeypatch):
    _disable_posix_open_guards(monkeypatch)
    run_dir = _run_dir(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (run_dir / "events").symlink_to(outside, target_is_directory=True)
    journal_path = run_dir / "events" / "lifecycle.jsonl"

    with pytest.raises(run_journal.RunJournalError) as excinfo:
        run_journal.ensure_journal(journal_path)

    assert len(excinfo.value.diagnostic) <= 240
    assert not (outside / "lifecycle.jsonl").exists()


def test_fallback_rejects_symlinked_journal_file(tmp_path, monkeypatch):
    _disable_posix_open_guards(monkeypatch)
    journal_path = _journal_path(_run_dir(tmp_path))
    run_journal.ensure_journal(journal_path)
    journal_path.unlink()
    target = tmp_path / "target.jsonl"
    target.write_bytes(b"")
    journal_path.symlink_to(target)

    with pytest.raises(run_journal.RunJournalError) as excinfo:
        _append_first_event(journal_path)

    assert len(excinfo.value.diagnostic) <= 240
    assert target.read_bytes() == b""


def test_fallback_open_nofollow_closes_fd_when_verify_identity_raises(tmp_path, monkeypatch):
    _disable_posix_open_guards(monkeypatch)
    journal_path = _journal_path(_run_dir(tmp_path))
    opened_fds: list[int] = []
    real_open = os.open

    def tracking_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd) if dir_fd is not None else real_open(path, flags, mode)
        opened_fds.append(fd)
        return fd

    monkeypatch.setattr(os, "open", tracking_open)

    def fail_verify(_path, _fd):
        raise run_journal.RunJournalError("verify failed")

    monkeypatch.setattr(run_journal, "_verify_fd_identity", fail_verify)

    with pytest.raises(run_journal.RunJournalError, match="verify failed"):
        run_journal.ensure_journal(journal_path)

    assert len(opened_fds) == 1
    with pytest.raises(OSError) as excinfo:
        os.fstat(opened_fds[0])
    assert excinfo.value.errno == errno.EBADF


def test_fallback_enforce_dir_mode_skips_open_without_o_directory(tmp_path, monkeypatch):
    _disable_posix_open_guards(monkeypatch)
    journal_path = _journal_path(_run_dir(tmp_path))
    events_dir = journal_path.parent
    real_open = os.open

    def guard_open(path, flags, mode=0o777, *, dir_fd=None):
        if Path(path) == events_dir:
            raise AssertionError("os.open must not be called for the events directory without O_DIRECTORY")
        if dir_fd is not None:
            return real_open(path, flags, mode, dir_fd=dir_fd)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", guard_open)
    events_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(events_dir, 0o755)

    run_journal.ensure_journal(journal_path)

    assert journal_path.is_file()
    assert stat.S_IMODE(events_dir.stat().st_mode) == 0o700
