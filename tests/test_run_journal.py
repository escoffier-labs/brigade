"""Tests for brigade.run_journal append-only lifecycle journal kernel."""

from __future__ import annotations

import errno
import ast
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import threading
import time
from copy import deepcopy
from pathlib import Path

import pytest

from brigade import run_events, run_journal
from brigade import run_checkpoint
from tests.support import PRIVATE_DIRECTORY_MODE, PRIVATE_FILE_MODE, assert_private_mode

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
    assert_private_mode(journal_path, PRIVATE_FILE_MODE)
    assert_private_mode(journal_path.parent, PRIVATE_DIRECTORY_MODE)


def test_append_event_writes_canonical_line_with_fsync(tmp_path, monkeypatch):
    journal_path = _journal_path(_run_dir(tmp_path))
    fsync_calls: list[int] = []

    def _track_fsync(fd):
        fsync_calls.append(fd)

    monkeypatch.setattr(os, "fsync", _track_fsync)

    event = _append_first_event(journal_path)

    lines = journal_path.read_bytes().splitlines(keepends=True)
    assert len(lines) == 1
    assert lines[0] == run_events.canonical_bytes(event.to_dict()) + os.linesep.encode("ascii")
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
    assert_private_mode(report.quarantine_path, PRIVATE_FILE_MODE)
    if os.name == "nt":
        # Text-mode descriptors translate line endings on Windows.
        assert journal_path.read_bytes().splitlines() == complete_bytes.splitlines()
    else:
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

    assert_private_mode(journal_path.parent, PRIVATE_DIRECTORY_MODE)
    assert_private_mode(journal_path, PRIVATE_FILE_MODE)


def test_ensure_journal_corrects_permissive_preexisting_modes(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    parent = journal_path.parent
    parent.mkdir(parents=True)
    os.chmod(parent, 0o777)
    fd = os.open(journal_path, os.O_CREAT | os.O_WRONLY, 0o644)
    os.close(fd)
    os.chmod(journal_path, 0o644)

    run_journal.ensure_journal(journal_path)

    assert_private_mode(parent, PRIVATE_DIRECTORY_MODE)
    assert_private_mode(journal_path, PRIVATE_FILE_MODE)


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

    assert_private_mode(quarantine_dir, PRIVATE_DIRECTORY_MODE)
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
    assert_private_mode(journal_path, PRIVATE_FILE_MODE)
    assert_private_mode(journal_path.parent, PRIVATE_DIRECTORY_MODE)


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
    if os.name == "nt":
        # Text-mode descriptors translate line endings on Windows.
        assert journal_path.read_bytes().splitlines() == complete_bytes.splitlines()
    else:
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


def test_fallback_enforce_dir_mode_corrects_via_lstat_without_o_directory(tmp_path, monkeypatch):
    _disable_posix_open_guards(monkeypatch)
    events_dir = _journal_path(_run_dir(tmp_path)).parent
    real_open = os.open

    def guard_open(path, flags, mode=0o777, *, dir_fd=None):
        if Path(path) == events_dir:
            raise AssertionError("directory mode fallback must not open the events directory")
        if dir_fd is not None:
            return real_open(path, flags, mode, dir_fd=dir_fd)
        return real_open(path, flags, mode)

    monkeypatch.setattr(os, "open", guard_open)
    events_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(events_dir, 0o755)

    run_journal._enforce_dir_mode(events_dir)

    assert_private_mode(events_dir, PRIVATE_DIRECTORY_MODE)


# -- read_journal_bounded (issue #568 slice 5, Task 1) ------------------------


def test_read_journal_bounded_matches_read_journal_on_complete_journal(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    _append_second_event(journal_path)

    bounded = run_journal.read_journal_bounded(journal_path)
    plain = run_journal.read_journal(journal_path)

    assert bounded.partial_tail == plain.partial_tail
    assert bounded.chain_errors == plain.chain_errors
    assert [e.event_id for e in bounded.events] == [e.event_id for e in plain.events]


def test_read_journal_bounded_refuses_oversize_journal_before_allocation(tmp_path, monkeypatch):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)

    real_open = os.open
    opened: list[tuple] = []

    def tracking_open(path, flags, mode=0o777, *, dir_fd=None):
        fd = real_open(path, flags, mode, dir_fd=dir_fd) if dir_fd is not None else real_open(path, flags, mode)
        opened.append((Path(path), flags))
        return fd

    monkeypatch.setattr(os, "open", tracking_open)

    # Pretend fstat reports a journal larger than MAX_JOURNAL_BYTES.
    real_fstat = os.fstat

    def big_fstat(fd):
        st = real_fstat(fd)
        st = os.stat_result(
            (
                st.st_mode,
                st.st_ino,
                st.st_dev,
                st.st_nlink,
                st.st_uid,
                st.st_gid,
                run_checkpoint.MAX_JOURNAL_BYTES + 1,  # st_size
                st.st_atime,
                st.st_mtime,
                st.st_ctime,
            )
        )
        return st

    monkeypatch.setattr(os, "fstat", big_fstat)

    with pytest.raises(run_journal.RunJournalError) as excinfo:
        run_journal.read_journal_bounded(journal_path)

    assert "bound exceeded" in excinfo.value.diagnostic
    # No whole-file allocation: the only open is the no-follow read fd. The
    # ``opened`` list must be non-empty so the ``all(...)`` guard is non-vacuous
    # (a regression that short-circuited before opening would pass vacuously
    # otherwise).
    assert opened, "read_journal_bounded must open the journal fd before the bound check"
    assert all(p.name == "lifecycle.jsonl" for p, _ in opened)


def test_read_journal_bounded_refuses_event_sequence_above_ceiling(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    previous_digest: str | None = None
    for sequence in range(1, run_checkpoint.MAX_JOURNAL_EVENTS + 2):
        event_type = "run.planning.started" if sequence % 2 == 0 else "run.dispatch.observed"
        payload = (
            {"detail": "n"} if event_type == "run.planning.started" else {"seat": "c", "attempt": 1, "detail": "n"}
        )
        env = run_events.build_event(
            run_id=RUN_ID,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            idempotency_key=f"ev-{sequence}",
            recorded_at="2026-07-27T15:30:45.000000Z",
            previous_digest=previous_digest,
        )
        with journal_path.open("ab") as handle:
            handle.write(run_events.canonical_bytes(env) + b"\n")
        previous_digest = env["event_digest"]

    with pytest.raises(run_journal.RunJournalError) as excinfo:
        run_journal.read_journal_bounded(journal_path)

    assert "bound exceeded" in excinfo.value.diagnostic


def test_read_journal_is_forensic_api_after_bounded_reader(tmp_path):
    """The forensic reader may inspect an over-limit journal without becoming a runtime path.

    Builds a journal one event past the ceiling with efficient direct canonical construction
    (``run_events.build_event`` + a single appending ``write`` per line, no
    ``append_event`` fsyncs). ``read_journal_bounded`` refuses its first event
    past ``MAX_JOURNAL_EVENTS``. ``read_journal`` intentionally remains an
    unbounded forensic API, while runtime control and mutation paths are
    required to call ``read_journal_bounded`` instead.
    """
    journal_path = _journal_path(_run_dir(tmp_path))
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    previous_digest: str | None = None
    with journal_path.open("ab") as handle:
        for sequence in range(1, run_checkpoint.MAX_JOURNAL_EVENTS + 2):
            event_type = "run.planning.started" if sequence % 2 == 0 else "run.dispatch.observed"
            payload = (
                {"detail": "n"} if event_type == "run.planning.started" else {"seat": "c", "attempt": 1, "detail": "n"}
            )
            env = run_events.build_event(
                run_id=RUN_ID,
                sequence=sequence,
                event_type=event_type,
                payload=payload,
                idempotency_key=f"ev-{sequence}",
                recorded_at="2026-07-27T15:30:45.000000Z",
                previous_digest=previous_digest,
            )
            handle.write(run_events.canonical_bytes(env) + b"\n")
            previous_digest = env["event_digest"]

    # read_journal_bounded refuses the journal on bounds.
    with pytest.raises(run_journal.RunJournalError) as excinfo:
        run_journal.read_journal_bounded(journal_path)
    assert "bound exceeded" in excinfo.value.diagnostic

    # read_journal stays compatible: it has no event-count bound and reads the
    # full gap-free, digest-linked chain without raising or reporting errors.
    plain = run_journal.read_journal(journal_path)
    assert len(plain.events) == run_checkpoint.MAX_JOURNAL_EVENTS + 1
    assert plain.partial_tail is None
    assert plain.chain_errors == []
    assert plain.events[0].sequence == 1
    assert plain.events[-1].sequence == run_checkpoint.MAX_JOURNAL_EVENTS + 1


def test_runtime_journal_paths_do_not_call_the_unbounded_forensic_reader():
    """Runtime mutation and recovery control paths must enforce shared reader bounds."""
    runtime_modules = (
        "doctor.py",
        "run_lifecycle.py",
        "run_checkpoint.py",
        "run_shadow.py",
        "runs_cmd.py",
    )
    source_root = Path(run_journal.__file__).parent
    offenders: list[str] = []
    for module_name in runtime_modules:
        tree = ast.parse((source_root / module_name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "run_journal"
                and node.func.attr == "read_journal"
            ):
                offenders.append(f"{module_name}:{node.lineno}")
    assert offenders == []


def test_near_event_ceiling_idempotency_index_and_duplicate_replay_stay_within_budget(tmp_path, record_property):
    """A 2,047-event journal keeps index construction and exact replay bounded."""
    journal_path = _journal_path(_run_dir(tmp_path))
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    previous_digest: str | None = None
    event_count = run_checkpoint.MAX_JOURNAL_EVENTS - 1
    with journal_path.open("ab") as handle:
        for sequence in range(1, event_count + 1):
            env = run_events.build_event(
                run_id=RUN_ID,
                sequence=sequence,
                event_type="run.planning.started",
                payload={"detail": "performance"},
                idempotency_key=f"performance-{sequence}",
                recorded_at="2026-07-27T15:30:45.000000Z",
                previous_digest=previous_digest,
            )
            handle.write(run_events.canonical_bytes(env) + b"\n")
            previous_digest = env["event_digest"]

    budget_ns = 5_000_000_000
    started_ns = time.perf_counter_ns()
    _sequence, _digest, index, partial_tail, _journal_bytes = run_journal._read_tail_state(journal_path)
    index_build_ns = time.perf_counter_ns() - started_ns

    started_ns = time.perf_counter_ns()
    replay = run_journal.lookup_idempotent_event(
        journal_path,
        event_type="run.planning.started",
        payload={"detail": "performance"},
        idempotency_key=f"performance-{event_count}",
    )
    duplicate_replay_ns = time.perf_counter_ns() - started_ns

    record_property("idempotency_index_build_ns", index_build_ns)
    record_property("idempotency_duplicate_replay_ns", duplicate_replay_ns)
    record_property("idempotency_performance_budget_ns", budget_ns)
    assert partial_tail is None
    assert len(index) == event_count
    assert replay is not None and replay.sequence == event_count
    assert index_build_ns <= budget_ns, f"idempotency index build took {index_build_ns}ns (budget {budget_ns}ns)"
    assert duplicate_replay_ns <= budget_ns, f"duplicate replay took {duplicate_replay_ns}ns (budget {budget_ns}ns)"


# -- Finding 6: read_journal_bounded must count complete records, not trust sequence --


def test_read_journal_bounded_rejects_complete_record_above_ceiling_even_with_repeated_sequence(tmp_path):
    journal_path = _journal_path(_run_dir(tmp_path))
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    previous_digest: str | None = None
    for sequence in range(1, run_checkpoint.MAX_JOURNAL_EVENTS + 1):
        event_type = "run.planning.started" if sequence % 2 == 0 else "run.dispatch.observed"
        payload = (
            {"detail": "n"} if event_type == "run.planning.started" else {"seat": "c", "attempt": 1, "detail": "n"}
        )
        env = run_events.build_event(
            run_id=RUN_ID,
            sequence=sequence,
            event_type=event_type,
            payload=payload,
            idempotency_key=f"ev-{sequence}",
            recorded_at="2026-07-27T15:30:45.000000Z",
            previous_digest=previous_digest,
        )
        with journal_path.open("ab") as handle:
            handle.write(run_events.canonical_bytes(env) + b"\n")
        previous_digest = env["event_digest"]

    # The first complete record above the shared ceiling repeats sequence 1
    # with a null previous digest. A sequence-trusting reader would treat it as
    # a duplicate or chain break instead of a bound excess.
    env_above_ceiling = run_events.build_event(
        run_id=RUN_ID,
        sequence=1,
        event_type="run.created",
        payload={"status": "started"},
        idempotency_key="ev-above-ceiling-repeated",
        recorded_at="2026-07-27T15:30:45.000000Z",
        previous_digest=None,
    )
    with journal_path.open("ab") as handle:
        handle.write(run_events.canonical_bytes(env_above_ceiling) + b"\n")

    with pytest.raises(run_journal.RunJournalError) as excinfo:
        run_journal.read_journal_bounded(journal_path)

    assert "bound exceeded" in excinfo.value.diagnostic


# -- Slice 7 assignment 5: append-side bounds --------------------------------


def _write_journal_events(journal_path: Path, count: int) -> dict:
    """Write ``count`` canonical events directly (no append_event fsyncs)."""
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    previous_digest: str | None = None
    last_env: dict | None = None
    with journal_path.open("ab") as handle:
        for sequence in range(1, count + 1):
            event_type = "run.planning.started" if sequence % 2 == 0 else "run.dispatch.observed"
            payload = (
                {"detail": "n"} if event_type == "run.planning.started" else {"seat": "c", "attempt": 1, "detail": "n"}
            )
            env = run_events.build_event(
                run_id=RUN_ID,
                sequence=sequence,
                event_type=event_type,
                payload=payload,
                idempotency_key=f"ev-{sequence}",
                recorded_at="2026-07-27T15:30:45.000000Z",
                previous_digest=previous_digest,
            )
            handle.write(run_events.canonical_bytes(env) + b"\n")
            previous_digest = env["event_digest"]
            last_env = env
    assert last_env is not None
    return last_env


def _tracking_write(monkeypatch) -> list[bytes]:
    """Monkeypatch os.write and return a list that collects write payloads."""
    original_write = os.write
    write_calls: list[bytes] = []

    def _record_write(fd, data):
        write_calls.append(data)
        return original_write(fd, data)

    monkeypatch.setattr(os, "write", _record_write)
    return write_calls


def test_append_event_refuses_distinct_event_above_ceiling_before_write(tmp_path, monkeypatch):
    journal_path = _journal_path(_run_dir(tmp_path))
    _write_journal_events(journal_path, run_checkpoint.MAX_JOURNAL_EVENTS)
    before = journal_path.read_bytes()
    write_calls = _tracking_write(monkeypatch)

    with pytest.raises(run_journal.RunJournalError) as excinfo:
        run_journal.append_event(
            journal_path,
            run_id=RUN_ID,
            event_type="run.created",
            payload={"status": "started"},
            idempotency_key="ev-above-ceiling",
            expected_previous_sequence=run_checkpoint.MAX_JOURNAL_EVENTS,
            recorded_at="2026-07-27T15:30:50.000000Z",
        )

    assert "bound exceeded" in excinfo.value.diagnostic
    assert not write_calls
    assert journal_path.read_bytes() == before


def test_old_sequence_512_journal_recovers_and_appends_513_without_renumbering(tmp_path):
    """A chain stopped at the pre-upgrade ceiling remains appendable after the bump."""
    assert run_checkpoint.MAX_JOURNAL_EVENTS == 2048
    journal_path = _journal_path(_run_dir(tmp_path))
    _write_journal_events(journal_path, 512)

    appended = run_journal.append_event(
        journal_path,
        run_id=RUN_ID,
        event_type="run.created",
        payload={"status": "started"},
        idempotency_key="ev-513-after-upgrade",
        expected_previous_sequence=512,
        recorded_at="2026-07-27T15:30:50.000000Z",
    )

    report = run_journal.read_journal_bounded(journal_path)
    assert appended.sequence == 513
    assert [event.sequence for event in report.events] == list(range(1, 514))
    assert report.events[511].idempotency_key == "ev-512"
    assert report.events[512].idempotency_key == "ev-513-after-upgrade"


def test_append_event_refuses_growth_past_max_journal_bytes_before_write(tmp_path, monkeypatch):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    before = journal_path.read_bytes()
    monkeypatch.setattr(run_checkpoint, "MAX_JOURNAL_BYTES", len(before) + 64)
    write_calls = _tracking_write(monkeypatch)

    with pytest.raises(run_journal.RunJournalError) as excinfo:
        _append_second_event(journal_path)

    assert excinfo.value.diagnostic == "bound exceeded: journal above MAX_JOURNAL_BYTES"
    assert not write_calls
    assert journal_path.read_bytes() == before


def test_append_event_allows_idempotent_replay_of_512th_event_when_full(tmp_path, monkeypatch):
    journal_path = _journal_path(_run_dir(tmp_path))
    last_env = _write_journal_events(journal_path, run_checkpoint.MAX_JOURNAL_EVENTS)
    before = journal_path.read_bytes()
    write_calls = _tracking_write(monkeypatch)

    replay = run_journal.append_event(
        journal_path,
        run_id=RUN_ID,
        event_type=last_env["event_type"],
        payload=dict(last_env["payload"]),
        idempotency_key=last_env["idempotency_key"],
        expected_previous_sequence=run_checkpoint.MAX_JOURNAL_EVENTS,
        recorded_at=last_env["recorded_at"],
    )

    assert replay.sequence == run_checkpoint.MAX_JOURNAL_EVENTS
    assert replay.event_id == last_env["event_id"]
    assert not write_calls
    assert journal_path.read_bytes() == before


def test_append_event_allows_idempotent_replay_at_exact_byte_bound(tmp_path, monkeypatch):
    journal_path = _journal_path(_run_dir(tmp_path))
    existing = _append_first_event(journal_path)
    before = journal_path.read_bytes()
    monkeypatch.setattr(run_checkpoint, "MAX_JOURNAL_BYTES", len(before))
    write_calls = _tracking_write(monkeypatch)

    replay = _append_first_event(journal_path)

    assert replay == existing
    assert replay.request_digest == existing.request_digest
    assert not write_calls
    assert journal_path.read_bytes() == before


def test_append_event_refuses_oversize_journal_consistent_with_read_journal_bounded(tmp_path, monkeypatch):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    padding = b"\n" * (run_checkpoint.MAX_JOURNAL_BYTES + 1024)
    journal_path.write_bytes(journal_path.read_bytes() + padding)
    before = journal_path.read_bytes()
    write_calls = _tracking_write(monkeypatch)

    with pytest.raises(run_journal.RunJournalError) as append_exc:
        _append_second_event(journal_path)

    with pytest.raises(run_journal.RunJournalError) as read_exc:
        run_journal.read_journal_bounded(journal_path)

    assert append_exc.value.diagnostic == read_exc.value.diagnostic
    assert "bound exceeded" in append_exc.value.diagnostic
    assert not write_calls
    assert journal_path.read_bytes() == before


def test_append_event_refuses_journal_with_records_above_ceiling_before_write(tmp_path, monkeypatch):
    journal_path = _journal_path(_run_dir(tmp_path))
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    previous_digest: str | None = None
    with journal_path.open("ab") as handle:
        for sequence in range(1, run_checkpoint.MAX_JOURNAL_EVENTS + 2):
            event_type = "run.planning.started" if sequence % 2 == 0 else "run.dispatch.observed"
            payload = (
                {"detail": "n"} if event_type == "run.planning.started" else {"seat": "c", "attempt": 1, "detail": "n"}
            )
            env = run_events.build_event(
                run_id=RUN_ID,
                sequence=sequence,
                event_type=event_type,
                payload=payload,
                idempotency_key=f"ev-{sequence}",
                recorded_at="2026-07-27T15:30:45.000000Z",
                previous_digest=previous_digest,
            )
            handle.write(run_events.canonical_bytes(env) + b"\n")
            previous_digest = env["event_digest"]
    before = journal_path.read_bytes()
    write_calls = _tracking_write(monkeypatch)

    with pytest.raises(run_journal.RunJournalError) as append_exc:
        run_journal.append_event(
            journal_path,
            run_id=RUN_ID,
            event_type="run.created",
            payload={"status": "started"},
            idempotency_key="ev-new",
            expected_previous_sequence=run_checkpoint.MAX_JOURNAL_EVENTS + 1,
            recorded_at="2026-07-27T15:30:50.000000Z",
        )

    with pytest.raises(run_journal.RunJournalError) as read_exc:
        run_journal.read_journal_bounded(journal_path)

    assert append_exc.value.diagnostic == read_exc.value.diagnostic
    assert "bound exceeded" in append_exc.value.diagnostic
    assert not write_calls
    assert journal_path.read_bytes() == before


# -- Slice 7 assignment 1: signal atomicity ----------------------------------


def _run_isolated_child(
    script: str,
    *args: str,
    timeout: float = 5.0,
) -> tuple[int, str, str]:
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    proc = subprocess.Popen(
        [sys.executable, "-c", script, *args],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate(timeout=2.0)
        pytest.fail(
            f"isolated signal/lock child timed out and was killed and reaped\nstdout:\n{stdout}\nstderr:\n{stderr}"
        )
    return proc.returncode, stdout, stderr


def _install_fake_pthread_sigmask_api(monkeypatch) -> tuple[object, object]:
    block_mode = object()
    restore_mode = object()
    monkeypatch.setattr(signal, "SIG_BLOCK", block_mode, raising=False)
    monkeypatch.setattr(signal, "SIG_SETMASK", restore_mode, raising=False)
    monkeypatch.setattr(run_journal, "_HAS_PTHREAD_SIGMASK", True)
    monkeypatch.setattr(run_journal, "_DEFERRED_SIGNALS", frozenset({signal.SIGTERM}))
    return block_mode, restore_mode


def test_defer_sigterm_translates_mask_block_oserror(monkeypatch):
    block_mode, _ = _install_fake_pthread_sigmask_api(monkeypatch)

    def fail_block(how, _mask):
        assert how is block_mode
        raise OSError("block failed")

    monkeypatch.setattr(signal, "pthread_sigmask", fail_block, raising=False)

    with pytest.raises(run_journal.RunJournalError) as excinfo:
        with run_journal._defer_sigterm():
            pytest.fail("body must not run after mask block failure")

    assert excinfo.value.diagnostic == "SIGTERM mask block failed"
    assert isinstance(excinfo.value.__cause__, OSError)


def test_defer_sigterm_translates_mask_restore_oserror(monkeypatch):
    block_mode, restore_mode = _install_fake_pthread_sigmask_api(monkeypatch)
    prior = {signal.SIGINT}

    def fail_restore(how, _mask):
        if how is block_mode:
            return prior
        assert how is restore_mode
        raise OSError("restore failed")

    monkeypatch.setattr(signal, "pthread_sigmask", fail_restore, raising=False)

    with pytest.raises(run_journal.RunJournalError) as excinfo:
        with run_journal._defer_sigterm():
            pass

    assert excinfo.value.diagnostic == "SIGTERM mask restoration failed"
    assert isinstance(excinfo.value.__cause__, OSError)


def test_defer_sigterm_restore_oserror_does_not_mask_primary_base_exception(monkeypatch):
    block_mode, restore_mode = _install_fake_pthread_sigmask_api(monkeypatch)
    primary = KeyboardInterrupt("primary body failure")

    def fail_restore(how, _mask):
        if how is block_mode:
            return {signal.SIGINT}
        assert how is restore_mode
        raise OSError("restore failed")

    monkeypatch.setattr(signal, "pthread_sigmask", fail_restore, raising=False)

    with pytest.raises(KeyboardInterrupt) as excinfo:
        with run_journal._defer_sigterm():
            raise primary

    assert excinfo.value is primary


@pytest.mark.parametrize("body_raises", [False, True], ids=["success", "body-exception"])
def test_append_critical_section_restores_exact_prior_signal_mask_in_subprocess(body_raises):
    if not run_journal._HAS_PTHREAD_SIGMASK:
        pytest.skip("signal.pthread_sigmask unavailable on this platform")

    script = r"""
import json
import signal
import sys
from brigade import run_journal

body_raises = sys.argv[1] == "true"
seed_signal = signal.SIGUSR1 if hasattr(signal, "SIGUSR1") else signal.SIGINT
original = signal.pthread_sigmask(signal.SIG_BLOCK, set())
seeded = set(original)
seeded.add(seed_signal)
signal.pthread_sigmask(signal.SIG_SETMASK, seeded)

class BodyFailure(BaseException):
    pass

caught = False
try:
    try:
        with run_journal._append_critical_section():
            inside = set(signal.pthread_sigmask(signal.SIG_BLOCK, set()))
            if signal.SIGTERM not in inside:
                raise RuntimeError("SIGTERM was not blocked inside critical section")
            if not seeded.issubset(inside):
                raise RuntimeError("seeded prior mask was not preserved inside critical section")
            if body_raises:
                raise BodyFailure()
    except BodyFailure:
        caught = True
    restored = set(signal.pthread_sigmask(signal.SIG_BLOCK, set()))
    print(json.dumps({
        "caught": caught,
        "restored_exactly": restored == seeded,
        "seeded_nonempty": bool(seeded),
    }))
finally:
    signal.pthread_sigmask(signal.SIG_SETMASK, original)
"""
    rc, stdout, stderr = _run_isolated_child(script, str(body_raises).lower())

    assert rc == 0, f"child exited {rc}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    result = json.loads(stdout)
    assert result == {
        "caught": body_raises,
        "restored_exactly": True,
        "seeded_nonempty": True,
    }


def test_append_event_signal_deferral_prevents_duplicate_sequence_in_subprocess(tmp_path):
    """A regression fails by child timeout instead of hanging the pytest process."""
    if not run_journal._HAS_PTHREAD_SIGMASK:
        pytest.skip("signal.pthread_sigmask unavailable on this platform")

    script = r"""
import json
import os
from pathlib import Path
import signal
import sys
from brigade import run_journal

journal_path = Path(sys.argv[1])
run_id = "20260727-153045-a1b2c3d4"
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
run_journal.append_event(
    journal_path,
    run_id=run_id,
    event_type="run.created",
    payload={"status": "started"},
    idempotency_key="create-1",
    expected_previous_sequence=0,
    recorded_at="2026-07-27T15:30:45.123456Z",
)

handler_results = []
def handler(_signum, _frame):
    try:
        run_journal.append_event(
            journal_path,
            run_id=run_id,
            event_type="run.interrupted",
            payload={"status": "interrupted", "detail": "sigterm"},
            idempotency_key="term-1",
            expected_previous_sequence=1,
            recorded_at="2026-07-27T15:30:50.000000Z",
        )
        handler_results.append(["appended"])
    except run_journal.StaleSequenceError as exc:
        handler_results.append(["stale", exc.diagnostic])
    except BaseException as exc:
        handler_results.append(["error", type(exc).__name__, str(exc)])

old_handler = signal.signal(signal.SIGTERM, handler)
fired = False
real_read_tail = run_journal._read_tail_state
def tracking_read_tail(path):
    global fired
    result = real_read_tail(path)
    if not fired:
        fired = True
        os.kill(os.getpid(), signal.SIGTERM)
    return result
run_journal._read_tail_state = tracking_read_tail

try:
    run_journal.append_event(
        journal_path,
        run_id=run_id,
        event_type="run.planning.started",
        payload={"detail": "planning"},
        idempotency_key="plan-1",
        expected_previous_sequence=1,
        recorded_at="2026-07-27T15:30:46.000000Z",
    )
finally:
    signal.signal(signal.SIGTERM, old_handler)

report = run_journal.read_journal(journal_path)
print(json.dumps({
    "chain_errors": report.chain_errors,
    "sequences": [event.sequence for event in report.events],
    "handler_results": handler_results,
}))
"""
    journal_path = _journal_path(_run_dir(tmp_path))
    rc, stdout, stderr = _run_isolated_child(script, str(journal_path))

    assert rc == 0, f"child exited {rc}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    result = json.loads(stdout)
    assert result["chain_errors"] == []
    assert result["sequences"] == [1, 2]
    assert result["handler_results"]
    assert result["handler_results"][0][0] == "stale"
    assert result["handler_results"][0][1] == "stale sequence: expected previous 1, actual 2"


def test_append_critical_section_rejects_same_thread_nested_entry_without_pthread_sigmask():
    """Without pthread_sigmask, nested same-thread entry must fail fast, not deadlock."""
    script = """
from contextlib import contextmanager
from brigade import run_journal

run_journal._HAS_PTHREAD_SIGMASK = False

@contextmanager
def nested():
    with run_journal._append_critical_section():
        with run_journal._append_critical_section():
            yield

try:
    with nested():
        pass
except run_journal.RunJournalError as exc:
    if len(exc.diagnostic) > 240:
        raise SystemExit(2)
    if "recursive append" not in exc.diagnostic:
        raise SystemExit(3)
    raise SystemExit(0)
raise SystemExit(1)
"""
    rc, stdout, stderr = _run_isolated_child(script, timeout=2.0)
    assert rc == 0, f"child exited {rc}\nstdout:\n{stdout}\nstderr:\n{stderr}"


def test_recover_partial_tail_reentrancy_matches_append_event(tmp_path):
    """Recovery must fail fast when signal delivery recursively enters it."""
    script = """
from pathlib import Path
from brigade import run_journal

journal_path = Path(__import__("sys").argv[1])
quarantine_dir = Path(__import__("sys").argv[2])
run_journal._HAS_PTHREAD_SIGMASK = False
original = run_journal._read_bytes_nofollow
entered = False
def recurse(path):
    global entered
    data = original(path)
    if not entered:
        entered = True
        run_journal.recover_partial_tail(journal_path, quarantine_dir)
    return data
run_journal._read_bytes_nofollow = recurse
try:
    run_journal.recover_partial_tail(journal_path, quarantine_dir)
except run_journal.RunJournalError as exc:
    if "recursive append" not in exc.diagnostic:
        raise SystemExit(2)
    raise SystemExit(0)
raise SystemExit(1)
"""
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    journal_path.write_bytes(journal_path.read_bytes() + b"partial")

    rc, stdout, stderr = _run_isolated_child(script, str(journal_path), str(tmp_path / "quarantine"), timeout=2.0)

    assert rc == 0, f"child exited {rc}\nstdout:\n{stdout}\nstderr:\n{stderr}"


def test_append_event_process_lock_prevents_worker_main_sigterm_duplicate_in_subprocess(tmp_path):
    """Worker-thread append holds the process lock while SIGTERM runs on the main
    thread: the handler blocks until the worker finishes, then sees a fresh tail
    and raises StaleSequenceError instead of duplicating sequence 2.
    """
    if not run_journal._HAS_PTHREAD_SIGMASK:
        pytest.skip("signal.pthread_sigmask unavailable on this platform")

    script = r"""
import json
import os
from pathlib import Path
import signal
import sys
import threading
from brigade import run_journal

journal_path = Path(sys.argv[1])
run_id = "20260727-153045-a1b2c3d4"
signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM})
run_journal.append_event(
    journal_path,
    run_id=run_id,
    event_type="run.created",
    payload={"status": "started"},
    idempotency_key="create-1",
    expected_previous_sequence=0,
    recorded_at="2026-07-27T15:30:45.123456Z",
)

handler_results = []
handler_started = threading.Event()
worker_past_tail = threading.Event()
worker_done = threading.Event()
worker_errors = []
def handler(_signum, _frame):
    handler_started.set()
    try:
        run_journal.append_event(
            journal_path,
            run_id=run_id,
            event_type="run.interrupted",
            payload={"status": "interrupted", "detail": "sigterm"},
            idempotency_key="term-worker-1",
            expected_previous_sequence=1,
            recorded_at="2026-07-27T15:30:50.000000Z",
        )
        handler_results.append(["appended"])
    except run_journal.StaleSequenceError as exc:
        handler_results.append(["stale", exc.diagnostic])
    except BaseException as exc:
        handler_results.append(["error", type(exc).__name__, str(exc)])

old_handler = signal.signal(signal.SIGTERM, handler)
real_read_tail = run_journal._read_tail_state
fired = False
def tracking_read_tail(path):
    global fired
    result = real_read_tail(path)
    if threading.current_thread().name == "append-worker" and not fired:
        fired = True
        worker_past_tail.set()
        os.kill(os.getpid(), signal.SIGTERM)
    return result
run_journal._read_tail_state = tracking_read_tail

def worker_append():
    try:
        run_journal.append_event(
            journal_path,
            run_id=run_id,
            event_type="run.planning.started",
            payload={"detail": "planning"},
            idempotency_key="plan-1",
            expected_previous_sequence=1,
            recorded_at="2026-07-27T15:30:46.000000Z",
        )
    except BaseException as exc:
        worker_errors.append([type(exc).__name__, str(exc)])
    finally:
        worker_done.set()

worker = threading.Thread(target=worker_append, name="append-worker")
worker.start()
if not worker_past_tail.wait(timeout=2.0):
    raise RuntimeError("worker did not reach tail read")
worker.join(timeout=2.0)
if worker.is_alive() or not worker_done.is_set():
    raise RuntimeError("worker append did not finish")
if not handler_started.wait(timeout=0.2):
    raise RuntimeError("SIGTERM handler did not run")
signal.signal(signal.SIGTERM, old_handler)

report = run_journal.read_journal(journal_path)
print(json.dumps({
    "chain_errors": report.chain_errors,
    "sequences": [event.sequence for event in report.events],
    "handler_results": handler_results,
    "worker_errors": worker_errors,
}))
"""
    journal_path = _journal_path(_run_dir(tmp_path))
    rc, stdout, stderr = _run_isolated_child(script, str(journal_path))

    assert rc == 0, f"child exited {rc}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    result = json.loads(stdout)
    assert result["worker_errors"] == []
    assert result["chain_errors"] == []
    assert result["sequences"] == [1, 2]
    assert result["handler_results"]
    assert result["handler_results"][0][0] == "stale"
    assert result["handler_results"][0][1] == "stale sequence: expected previous 1, actual 2"


def test_append_event_process_lock_serializes_workers_without_pthread_sigmask(tmp_path):
    script = r"""
import json
from pathlib import Path
import sys
import threading
from brigade import run_journal

journal_path = Path(sys.argv[1])
run_id = "20260727-153045-a1b2c3d4"
run_journal.append_event(
    journal_path,
    run_id=run_id,
    event_type="run.created",
    payload={"status": "started"},
    idempotency_key="create-1",
    expected_previous_sequence=0,
    recorded_at="2026-07-27T15:30:45.123456Z",
)
run_journal._HAS_PTHREAD_SIGMASK = False

first_reached_tail = threading.Event()
release_first = threading.Event()
second_started = threading.Event()
second_reached_tail = threading.Event()
results = {}
real_read_tail = run_journal._read_tail_state
def tracking_read_tail(path):
    result = real_read_tail(path)
    name = threading.current_thread().name
    if name == "first-worker":
        first_reached_tail.set()
        if not release_first.wait(timeout=2.0):
            raise RuntimeError("first worker release timed out")
    elif name == "second-worker":
        second_reached_tail.set()
    return result
run_journal._read_tail_state = tracking_read_tail

def append_from(name, key):
    if name == "second":
        second_started.set()
    try:
        event = run_journal.append_event(
            journal_path,
            run_id=run_id,
            event_type="run.planning.started",
            payload={"detail": name},
            idempotency_key=key,
            expected_previous_sequence=1,
            recorded_at="2026-07-27T15:30:46.000000Z",
        )
        results[name] = ["appended", event.sequence]
    except run_journal.StaleSequenceError as exc:
        results[name] = ["stale", exc.diagnostic]
    except BaseException as exc:
        results[name] = ["error", type(exc).__name__, str(exc)]

first = threading.Thread(target=append_from, args=("first", "plan-1"), name="first-worker")
second = threading.Thread(target=append_from, args=("second", "plan-2"), name="second-worker")
first.start()
if not first_reached_tail.wait(timeout=1.0):
    raise RuntimeError("first worker did not reach tail read")
second.start()
if not second_started.wait(timeout=1.0):
    raise RuntimeError("second worker did not start")
serialized = not second_reached_tail.wait(timeout=0.2)
release_first.set()
first.join(timeout=2.0)
second.join(timeout=2.0)
if first.is_alive() or second.is_alive():
    raise RuntimeError("worker did not finish")

report = run_journal.read_journal(journal_path)
print(json.dumps({
    "serialized": serialized,
    "results": results,
    "chain_errors": report.chain_errors,
    "sequences": [event.sequence for event in report.events],
}))
"""
    journal_path = _journal_path(_run_dir(tmp_path))
    rc, stdout, stderr = _run_isolated_child(script, str(journal_path))

    assert rc == 0, f"child exited {rc}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    result = json.loads(stdout)
    assert result["serialized"] is True
    assert result["results"]["first"] == ["appended", 2]
    assert result["results"]["second"] == [
        "stale",
        "stale sequence: expected previous 1, actual 2",
    ]
    assert result["chain_errors"] == []
    assert result["sequences"] == [1, 2]


# -- Issue #651: cross-process journal serialization -------------------------

_POSIX_FCNTL_LOCK_ONLY = pytest.mark.skipif(
    os.name != "posix",
    reason="the cross-process journal mutation lock is an fcntl.flock sibling file",
)

_FCNTL_RACE_CHILD = r"""
import json
import os
import sys
import time
from pathlib import Path

from brigade import run_journal

journal_path = Path(sys.argv[1])
name = sys.argv[2]
rounds = int(sys.argv[3])
barrier = Path(sys.argv[4])
run_id = sys.argv[5]
micros = sys.argv[6]
outcomes = []


def tail_sequence():
    report = run_journal.read_journal(journal_path)
    if report.chain_errors:
        raise RuntimeError("chain errors: " + "; ".join(report.chain_errors))
    return report.events[-1].sequence if report.events else 0


broken = False
for r in range(rounds):
    if not broken:
        try:
            tail = tail_sequence()
        except Exception as exc:
            outcomes.append({"round": r, "broken": str(exc)})
            broken = True
    if broken:
        # Keep the parent's per-round barrier protocol satisfied without
        # touching the (possibly forked) journal again.
        (barrier / f"{name}.ready.{r}").write_text("broken")
        (barrier / f"{name}.done.{r}").write_text("done")
        continue
    (barrier / f"{name}.ready.{r}").write_text(str(tail))
    go = barrier / f"go.{r}"
    deadline = time.monotonic() + 30.0
    while not os.path.lexists(go):
        if time.monotonic() > deadline:
            raise SystemExit(f"{name}: go file for round {r} never appeared")
    appended = False
    attempts = 0
    stale = 0
    while not appended and attempts < 8:
        attempts += 1
        try:
            run_journal.append_event(
                journal_path,
                run_id=run_id,
                event_type="run.planning.started",
                payload={"detail": f"race round {r} child {name}"},
                idempotency_key=f"race-r{r:02d}-{name}",
                expected_previous_sequence=tail,
                recorded_at=f"2026-07-27T17:{r:02d}:00.{micros}Z",
            )
            appended = True
        except run_journal.StaleSequenceError:
            stale += 1
            try:
                tail = tail_sequence()
            except Exception as exc:
                outcomes.append({"round": r, "broken": str(exc)})
                broken = True
                break
        except run_journal.RunJournalError as exc:
            outcomes.append({"round": r, "error": type(exc).__name__, "detail": exc.diagnostic})
            broken = True
            break
    outcomes.append({"round": r, "appended": appended, "attempts": attempts, "stale": stale})
    (barrier / f"{name}.done.{r}").write_text("done")

(barrier / f"{name}.result.json").write_text(json.dumps(outcomes))
"""


def _spawn_barrier_child(script_path: Path, *args: str) -> subprocess.Popen:
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    return subprocess.Popen(
        [sys.executable, str(script_path), *args],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_files(paths: list[Path], deadline_s: float, what: str) -> None:
    deadline = time.monotonic() + deadline_s
    while True:
        missing = [path for path in paths if not path.exists()]
        if not missing:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for {what}: {[str(p) for p in missing]}")
        time.sleep(0.01)


@_POSIX_FCNTL_LOCK_ONLY
def test_append_event_fcntl_lock_serializes_two_subprocess_writers(tmp_path):
    """Two real OS processes racing appends from the same tail must serialize.

    Each round, both children read the same tail, rendezvous, and are released
    simultaneously to append the next sequence. Without a cross-process lock
    both children pass the stale check for the same sequence N+1 and both
    O_APPEND, forking the digest chain.
    """
    journal_path = _journal_path(_run_dir(tmp_path))
    seed_count = 200
    rounds = 10
    pad = "x" * 440
    for i in range(seed_count):
        _append(
            journal_path,
            event_type="run.planning.started",
            payload={"detail": f"seed {i:03d} {pad}"},
            idempotency_key=f"seed-{i:03d}",
            expected_previous_sequence=i,
            recorded_at=f"2026-07-27T15:{i // 60:02d}:{i % 60:02d}.000000Z",
        )

    barrier = tmp_path / "barrier"
    barrier.mkdir()
    child_script = tmp_path / "race_child.py"
    child_script.write_text(_FCNTL_RACE_CHILD)

    children = {
        name: _spawn_barrier_child(
            child_script,
            str(journal_path),
            name,
            str(rounds),
            str(barrier),
            RUN_ID,
            micros,
        )
        for name, micros in (("alpha", "000001"), ("beta", "000002"))
    }
    try:
        for r in range(rounds):
            ready = [barrier / f"{name}.ready.{r}" for name in children]
            _wait_for_files(ready, 30.0, f"round {r} ready rendezvous")
            (barrier / f"go.{r}").write_text("go")
            done = [barrier / f"{name}.done.{r}" for name in children]
            _wait_for_files(done, 30.0, f"round {r} done rendezvous")
    except BaseException:
        for proc in children.values():
            proc.kill()
        for name, proc in children.items():
            stdout, stderr = proc.communicate(timeout=5.0)
            raise AssertionError(
                f"race barrier failed; child {name} rc={proc.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
            ) from None

    child_results = {}
    for name, proc in children.items():
        stdout, stderr = proc.communicate(timeout=30.0)
        assert proc.returncode == 0, f"child {name} exited {proc.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"
        child_results[name] = json.loads((barrier / f"{name}.result.json").read_text())

    report = run_journal.read_journal(journal_path)
    sequences = [event.sequence for event in report.events]
    expected = list(range(1, seed_count + 2 * rounds + 1))
    assert report.chain_errors == [], (
        f"cross-process append race forked the journal: {report.chain_errors}\n"
        f"child outcomes: {json.dumps(child_results)}"
    )
    assert sequences == expected, (
        f"expected contiguous unique sequences 1..{seed_count + 2 * rounds}, got "
        f"{len(sequences)} events (duplicates: {sorted({s for s in sequences if sequences.count(s) > 1})})\n"
        f"child outcomes: {json.dumps(child_results)}"
    )
    for previous, current in zip(report.events, report.events[1:], strict=False):
        assert current.previous_digest == previous.event_digest


@_POSIX_FCNTL_LOCK_ONLY
def test_append_critical_section_releases_fcntl_lock_after_exception(tmp_path, monkeypatch):
    """A raising mutation must still release the sibling flock file.

    The append critical section must hold an exclusive flock on
    ``<journal>.lock`` and release it in ``finally`` even when the protected
    mutation raises, so a later process can acquire the lock and append.
    """
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)

    class _InjectedMutationFailure(Exception):
        pass

    def _exploding_build_event(**kwargs):
        raise _InjectedMutationFailure("injected mutation failure")

    monkeypatch.setattr(run_events, "build_event", _exploding_build_event)

    with pytest.raises(_InjectedMutationFailure):
        _append(
            journal_path,
            event_type="run.planning.started",
            payload={"detail": "injected failure"},
            idempotency_key="plan-injected-failure",
            expected_previous_sequence=1,
            recorded_at="2026-07-27T15:30:46.000000Z",
        )

    lock_path = journal_path.with_name(f"{journal_path.name}.lock")
    assert lock_path.is_file(), (
        "append critical section must create the sibling cross-process lock file "
        f"{lock_path.name!r} even when the protected mutation raises"
    )
    assert not lock_path.is_symlink()
    assert_private_mode(lock_path, PRIVATE_FILE_MODE)

    script = r"""
import fcntl
import json
import os
import sys
from pathlib import Path

from brigade import run_journal

journal_path = Path(sys.argv[1])
lock_path = Path(sys.argv[2])
fd = os.open(lock_path, os.O_RDWR)
# Blocks forever if the failed append above leaked the exclusive flock; the
# parent's communicate timeout turns that hang into a test failure.
fcntl.flock(fd, fcntl.LOCK_EX)
fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)
event = run_journal.append_event(
    journal_path,
    run_id="20260727-153045-a1b2c3d4",
    event_type="run.planning.started",
    payload={"detail": "after release"},
    idempotency_key="plan-after-release",
    expected_previous_sequence=1,
    recorded_at="2026-07-27T15:30:47.000000Z",
)
print(json.dumps({"sequence": event.sequence}))
"""
    rc, stdout, stderr = _run_isolated_child(script, str(journal_path), str(lock_path), timeout=10.0)

    assert rc == 0, f"child exited {rc}\nstdout:\n{stdout}\nstderr:\n{stderr}"
    assert json.loads(stdout) == {"sequence": 2}
    report = run_journal.read_journal(journal_path)
    assert report.chain_errors == []
    assert [event.sequence for event in report.events] == [1, 2]


@_POSIX_FCNTL_LOCK_ONLY
def test_recover_partial_tail_serializes_on_fcntl_lock(tmp_path):
    """Recovery mutates the journal, so it must take the same cross-process lock.

    While another process holds the exclusive flock on the sibling lock file,
    ``recover_partial_tail`` must block instead of truncating underneath it.
    """
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    journal_path.write_bytes(journal_path.read_bytes() + b'{"partial-tail')
    lock_path = journal_path.with_name(f"{journal_path.name}.lock")
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    locked_file = barrier / "locked"
    release_file = barrier / "release"

    holder_script = tmp_path / "lock_holder.py"
    holder_script.write_text(
        """
import fcntl
import os
import sys
import time
from pathlib import Path

lock_path, locked_file, release_file = map(Path, sys.argv[1:4])
fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, int("600", 8))
fcntl.flock(fd, fcntl.LOCK_EX)
locked_file.write_text("locked")
deadline = time.monotonic() + 60.0
while not release_file.exists():
    if time.monotonic() > deadline:
        raise SystemExit("release file never appeared")
    time.sleep(0.02)
fcntl.flock(fd, fcntl.LOCK_UN)
os.close(fd)
"""
    )
    holder = _spawn_barrier_child(holder_script, str(lock_path), str(locked_file), str(release_file))
    try:
        _wait_for_files([locked_file], 15.0, "lock holder rendezvous")
        time.sleep(0.2)

        done = threading.Event()
        outcome: dict[str, object] = {}

        def run_recovery() -> None:
            try:
                outcome["report"] = run_journal.recover_partial_tail(journal_path, tmp_path / "quarantine")
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                outcome["error"] = exc
            finally:
                done.set()

        worker = threading.Thread(target=run_recovery, name="recovery-worker")
        worker.start()
        assert not done.wait(timeout=0.75), (
            "recover_partial_tail completed while another process held the journal "
            "flock; recovery mutations must serialize on the sibling lock file"
        )
        release_file.write_text("release")
        assert done.wait(timeout=15.0), "recover_partial_tail did not complete after the flock was released"
        worker.join(timeout=5.0)
    finally:
        if not release_file.exists():
            release_file.write_text("release")
        stdout, stderr = holder.communicate(timeout=15.0)
    assert holder.returncode == 0, f"lock holder exited {holder.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"

    assert "error" not in outcome, f"recovery raised: {outcome['error']!r}"
    report = outcome["report"]
    assert report.partial_bytes == b'{"partial-tail'
    assert report.quarantine_path is not None
    verified = run_journal.read_journal(journal_path)
    assert verified.chain_errors == []
    assert verified.partial_tail is None
    assert [event.sequence for event in verified.events] == [1]


# -- Slice 7 assignment 2: journal directory durability ----------------------


_POSIX_DIRECTORY_FSYNC_ONLY = pytest.mark.skipif(
    os.name != "posix",
    reason="directory fsync is intentionally a no-op off POSIX",
)


def _refers_to_directory(fd: int, directory: Path) -> bool:
    try:
        st = os.fstat(fd)
        dir_st = directory.stat()
    except OSError:
        return False
    return stat.S_ISDIR(st.st_mode) and st.st_ino == dir_st.st_ino and st.st_dev == dir_st.st_dev


@_POSIX_DIRECTORY_FSYNC_ONLY
def test_ensure_journal_directory_fsync_after_creating_lifecycle_file(tmp_path, monkeypatch):
    journal_path = _journal_path(_run_dir(tmp_path))
    events_dir = journal_path.parent

    real_fsync = os.fsync
    call_log: list[tuple[str, bool]] = []

    def tracking_fsync(fd):
        call_log.append(("fsync", _refers_to_directory(fd, events_dir)))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", tracking_fsync)

    run_journal.ensure_journal(journal_path)

    dir_fsyncs = [i for i, (kind, refers_events) in enumerate(call_log) if kind == "fsync" and refers_events]
    assert dir_fsyncs, "events directory was not fsynced after creating lifecycle.jsonl"
    assert journal_path.is_file()


@_POSIX_DIRECTORY_FSYNC_ONLY
def test_ensure_journal_skips_creation_directory_fsync_for_existing_journal(tmp_path, monkeypatch):
    journal_path = _journal_path(_run_dir(tmp_path))
    events_dir = journal_path.parent
    run_journal.ensure_journal(journal_path)

    real_fsync = os.fsync
    call_log: list[tuple[str, bool]] = []

    def tracking_fsync(fd):
        call_log.append(("fsync", _refers_to_directory(fd, events_dir)))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", tracking_fsync)

    run_journal.ensure_journal(journal_path)

    dir_fsyncs = [i for i, (kind, refers_events) in enumerate(call_log) if kind == "fsync" and refers_events]
    assert not dir_fsyncs, "existing journal must not trigger a creation directory fsync"


@_POSIX_DIRECTORY_FSYNC_ONLY
def test_recover_partial_tail_directory_fsyncs_quarantine_dir_before_truncating(tmp_path, monkeypatch):
    journal_path = _journal_path(_run_dir(tmp_path))
    _append_first_event(journal_path)
    partial_suffix = b'{"schema":"brigade.run_event.v1","event_type":"run.plan'
    journal_path.write_bytes(journal_path.read_bytes() + partial_suffix)
    quarantine_dir = tmp_path / "quarantine"

    real_fsync = os.fsync
    real_ftruncate = os.ftruncate
    call_log: list[tuple[str, bool | None]] = []

    def tracking_fsync(fd):
        call_log.append(("fsync", _refers_to_directory(fd, quarantine_dir)))
        return real_fsync(fd)

    def tracking_ftruncate(fd, length):
        call_log.append(("ftruncate", None))
        return real_ftruncate(fd, length)

    monkeypatch.setattr(os, "fsync", tracking_fsync)
    monkeypatch.setattr(os, "ftruncate", tracking_ftruncate)

    run_journal.recover_partial_tail(journal_path, quarantine_dir)

    truncate_indices = [i for i, (kind, _) in enumerate(call_log) if kind == "ftruncate"]
    assert truncate_indices, "journal was never truncated"
    first_truncate = truncate_indices[0]
    quarantine_dir_fsyncs_before_truncate = [
        i for i, (kind, refers_q) in enumerate(call_log) if kind == "fsync" and refers_q and i < first_truncate
    ]
    assert quarantine_dir_fsyncs_before_truncate, (
        "quarantine directory was not fsynced after quarantine file close and before journal truncate"
    )


@_POSIX_DIRECTORY_FSYNC_ONLY
def test_fsync_directory_refuses_symlink(tmp_path):
    real_dir = tmp_path / "events"
    real_dir.mkdir()
    symlink = tmp_path / "events-link"
    symlink.symlink_to(real_dir)

    with pytest.raises(run_journal.RunJournalError):
        run_journal._fsync_directory(symlink)


@_POSIX_DIRECTORY_FSYNC_ONLY
def test_fsync_directory_refuses_non_directory(tmp_path):
    file_path = tmp_path / "lifecycle.jsonl"
    file_path.write_bytes(b"")

    with pytest.raises(run_journal.RunJournalError):
        run_journal._fsync_directory(file_path)


def test_fsync_directory_without_o_directory_opens_readonly_and_fsyncs(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("POSIX directory fsync fallback is not exercised on this platform")

    events_dir = tmp_path / "events"
    events_dir.mkdir()
    monkeypatch.setattr(run_journal, "_HAS_O_DIRECTORY", False)

    real_fsync = os.fsync
    fsynced: list[bool] = []

    def tracking_fsync(fd):
        fsynced.append(_refers_to_directory(fd, events_dir))
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", tracking_fsync)

    run_journal._fsync_directory(events_dir)

    assert fsynced
    assert fsynced[0]


def test_chmod_fd_or_path_does_not_use_fchmod_off_posix():
    """Regression: presence of os.fchmod is not proof it works.

    os.fchmod exists on Windows under Python 3.14 but raises PermissionError
    (WinError 5) when the descriptor was opened O_RDONLY, which
    _enforce_file_mode always does. Gate on the platform so non-POSIX takes the
    verified-path os.chmod fallback instead.
    """
    assert run_journal._HAS_FCHMOD == (hasattr(os, "fchmod") and os.name == "posix")


def test_enforce_file_mode_succeeds_on_a_read_only_descriptor(tmp_path: Path):
    """ensure_journal must not raise while correcting the journal's mode."""
    journal = tmp_path / "lifecycle.jsonl"
    run_journal.ensure_journal(journal)

    assert journal.exists()
    # Second call re-enters the mode-enforcement path against an existing file.
    run_journal.ensure_journal(journal)
