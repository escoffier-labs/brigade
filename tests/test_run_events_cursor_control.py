"""Acceptance tests for durable cursors and idempotent live control (issue #604)."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

import pytest

from brigade import (
    cli,
    run_control,
    run_control_journal,
    run_event_cursor,
    run_events,
    run_journal,
    run_redaction,
    run_shadow,
)
from brigade.run_control_journal import ControlJournalError

RUN_ID = "20260731-604000-cursors01"
RECORDED_AT = "2026-07-31T12:00:00.000000Z"


def _run_dir(tmp_path: Path, run_id: str = RUN_ID) -> Path:
    run_dir = tmp_path / "runs" / run_id
    run_dir.mkdir(parents=True)
    return run_dir


def _journal_path(run_dir: Path) -> Path:
    return run_dir / "events" / "lifecycle.jsonl"


def _append(
    journal_path: Path,
    *,
    run_id: str,
    event_type: str,
    payload: dict,
    idempotency_key: str,
    expected_previous_sequence: int,
    recorded_at: str,
) -> run_journal.RunEvent:
    return run_journal.append_event(
        journal_path,
        run_id=run_id,
        event_type=event_type,
        payload=payload,
        idempotency_key=idempotency_key,
        expected_previous_sequence=expected_previous_sequence,
        recorded_at=recorded_at,
    )


def _seed_lifecycle(run_dir: Path, *, terminal: bool = False) -> list[run_journal.RunEvent]:
    journal = _journal_path(run_dir)
    run_id = run_dir.name
    events = []
    events.append(
        _append(
            journal,
            run_id=run_id,
            event_type="run.created",
            payload={"status": "started"},
            idempotency_key="create-1",
            expected_previous_sequence=0,
            recorded_at=RECORDED_AT,
        )
    )
    events.append(
        _append(
            journal,
            run_id=run_id,
            event_type="run.dispatching.started",
            payload={"detail": "dispatching"},
            idempotency_key="dispatching-1",
            expected_previous_sequence=1,
            recorded_at="2026-07-31T12:00:01.000000Z",
        )
    )
    if terminal:
        events.append(
            _append(
                journal,
                run_id=run_id,
                event_type="run.completed",
                payload={"status": "ok", "detail": "done"},
                idempotency_key="complete-1",
                expected_previous_sequence=2,
                recorded_at="2026-07-31T12:00:02.000000Z",
            )
        )
    return events


def _cursor_for(event: run_journal.RunEvent) -> str:
    return run_event_cursor.encode(
        run_event_cursor.DecodedCursor(
            schema=run_event_cursor.SUPPORTED_SCHEMA,
            run_id=event.run_id,
            sequence=event.sequence,
            digest=event.event_digest,
        )
    )


def _parse_ndjson(stdout: str) -> list[dict]:
    rows = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def test_events_follow_restart_with_cursor_emits_each_later_event_once(tmp_path, capsys):
    """AC: stop follow, restart with last cursor, receive later events once in order."""
    run_dir = _run_dir(tmp_path)
    seeded = _seed_lifecycle(run_dir)
    assert cli.main(["runs", "events", str(run_dir)]) == 0
    first = _parse_ndjson(capsys.readouterr().out)
    assert [row["sequence"] for row in first] == [1, 2]
    cursor = first[-1]["cursor"]

    _append(
        _journal_path(run_dir),
        run_id=run_dir.name,
        event_type="run.completed",
        payload={"status": "ok", "detail": "done"},
        idempotency_key="complete-1",
        expected_previous_sequence=2,
        recorded_at="2026-07-31T12:00:02.000000Z",
    )
    assert cli.main(["runs", "events", str(run_dir), "--after", cursor]) == 0
    second = _parse_ndjson(capsys.readouterr().out)
    assert [row["sequence"] for row in second] == [3]
    assert second[0]["event_type"] == "run.completed"
    assert second[0]["cursor"]
    # Restart with the same cursor again yields the same suffix once.
    assert cli.main(["runs", "events", str(run_dir), "--after", cursor]) == 0
    again = _parse_ndjson(capsys.readouterr().out)
    assert [row["event_digest"] for row in again] == [row["event_digest"] for row in second]
    assert seeded[0].event_type == "run.created"


def test_events_completed_run_same_cursor_produces_same_suffix(tmp_path, capsys):
    """AC: reading a completed run with the same cursor produces the same suffix."""
    run_dir = _run_dir(tmp_path)
    events = _seed_lifecycle(run_dir, terminal=True)
    cursor = _cursor_for(events[0])
    assert cli.main(["runs", "events", str(run_dir), "--after", cursor]) == 0
    first = _parse_ndjson(capsys.readouterr().out)
    assert cli.main(["runs", "events", str(run_dir), "--after", cursor]) == 0
    second = _parse_ndjson(capsys.readouterr().out)
    assert first == second
    assert [row["sequence"] for row in first] == [2, 3]


@pytest.mark.parametrize(
    "case",
    ["other_run", "unknown_sequence", "digest_mismatch", "unsupported_schema"],
)
def test_events_cursor_failures_emit_no_unverified_suffix(tmp_path, capsys, case):
    """AC: bad cursors fail closed without emitting an unverified suffix."""
    run_dir = _run_dir(tmp_path)
    events = _seed_lifecycle(run_dir, terminal=True)
    if case == "other_run":
        other = _run_dir(tmp_path, "20260731-604000-other99")
        other_events = _seed_lifecycle(other, terminal=True)
        cursor = _cursor_for(other_events[0])
    elif case == "unknown_sequence":
        cursor = run_event_cursor.encode(
            run_event_cursor.DecodedCursor(
                schema=run_event_cursor.SUPPORTED_SCHEMA,
                run_id=run_dir.name,
                sequence=99,
                digest="c" * 64,
            )
        )
    elif case == "digest_mismatch":
        cursor = run_event_cursor.encode(
            run_event_cursor.DecodedCursor(
                schema=run_event_cursor.SUPPORTED_SCHEMA,
                run_id=run_dir.name,
                sequence=events[0].sequence,
                digest="d" * 64,
            )
        )
    else:
        # Unsupported schema fails at decode before any emission.
        raw = json.dumps(
            {
                "digest": "e" * 64,
                "run_id": run_dir.name,
                "schema": "brigade.run_event_cursor.v0",
                "sequence": 1,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        import base64

        cursor = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    rc = cli.main(["runs", "events", str(run_dir), "--after", cursor])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "error:" in captured.err


def test_events_legacy_run_reports_legacy_no_journal(tmp_path, capsys):
    run_dir = _run_dir(tmp_path)
    (run_dir / "run.json").write_text(json.dumps({"status": "ok"}) + "\n")
    rc = cli.main(["runs", "events", str(run_dir)])
    assert rc == 2
    assert "legacy-no-journal" in capsys.readouterr().err


def test_idempotent_request_id_replays_without_second_transport_call(tmp_path):
    """AC: same request id + fingerprint returns first terminal result; no second transport call."""
    run_dir = _run_dir(tmp_path)
    _seed_lifecycle(run_dir)
    calls: list[dict] = []

    def send(payload: dict[str, object]) -> dict[str, object]:
        calls.append(dict(payload))
        return {"ok": True, "worker": "coder", "turn_id": "turn-1"}

    first = run_control_journal.execute_control_request(
        run_dir,
        op="steer",
        request_id="req-idem-1",
        worker="coder",
        text="keep going",
        send=send,
    )
    second = run_control_journal.execute_control_request(
        run_dir,
        op="steer",
        request_id="req-idem-1",
        worker="coder",
        text="keep going",
        send=send,
    )
    assert len(calls) == 1
    assert first.replayed is False
    assert second.replayed is True
    assert second.response["ok"] is True
    assert second.response["turn_id"] == "turn-1"
    report = run_journal.read_journal_bounded(_journal_path(run_dir))
    control_types = [event.event_type for event in report.events if event.event_type.startswith("control.")]
    assert control_types == ["control.requested", "control.observed"]
    for event in report.events:
        assert "keep going" not in json.dumps(event.payload)


@pytest.mark.parametrize(
    ("changed", "kwargs"),
    [
        ("worker", {"worker": "reviewer", "text": "keep going", "op": "steer"}),
        ("text", {"worker": "coder", "text": "different text", "op": "steer"}),
        ("op", {"worker": "coder", "text": None, "op": "interrupt"}),
        ("turn_id", {"worker": "coder", "text": "keep going", "op": "steer", "turn_id": "turn-9"}),
    ],
)
def test_request_id_fingerprint_conflict(tmp_path, changed, kwargs):
    """AC: same request id with changed worker/text/op/turn identity conflicts."""
    run_dir = _run_dir(tmp_path)
    _seed_lifecycle(run_dir)

    def send(_payload: dict[str, object]) -> dict[str, object]:
        return {"ok": True, "worker": "coder", "turn_id": "turn-1"}

    run_control_journal.execute_control_request(
        run_dir,
        op="steer",
        request_id="req-conflict-1",
        worker="coder",
        text="keep going",
        send=send,
    )
    with pytest.raises(ControlJournalError) as exc:
        run_control_journal.execute_control_request(
            run_dir,
            op=kwargs["op"],
            request_id="req-conflict-1",
            worker=kwargs.get("worker"),
            text=kwargs.get("text"),
            turn_id=kwargs.get("turn_id"),
            send=send,
        )
    assert exc.value.code == "control_fingerprint_conflict"
    assert changed  # parametrize label retained for AC mapping


def test_indeterminate_control_request_is_not_reissued(tmp_path):
    """AC: requested without terminal observation reports indeterminate; no reissue."""
    run_dir = _run_dir(tmp_path)
    _seed_lifecycle(run_dir)
    fingerprint = run_control_journal.control_fingerprint_payload(
        op="steer",
        request_id="req-indet-1",
        worker="coder",
        text="steer me",
    )
    run_journal.append_event(
        _journal_path(run_dir),
        run_id=run_dir.name,
        event_type="control.requested",
        payload=fingerprint,
        idempotency_key=run_control_journal.requested_idempotency_key("req-indet-1"),
        expected_previous_sequence=2,
        recorded_at="2026-07-31T12:00:03.000000Z",
    )
    calls: list[dict] = []

    def send(payload: dict[str, object]) -> dict[str, object]:
        calls.append(dict(payload))
        return {"ok": True, "worker": "coder", "turn_id": "turn-1"}

    with pytest.raises(ControlJournalError) as exc:
        run_control_journal.execute_control_request(
            run_dir,
            op="steer",
            request_id="req-indet-1",
            worker="coder",
            text="steer me",
            send=send,
        )
    assert exc.value.code == "indeterminate"
    assert calls == []


@pytest.mark.parametrize(
    "crash_point",
    ["before_requested", "after_requested", "after_transport", "after_terminal"],
)
def test_control_crash_points(tmp_path, monkeypatch, crash_point):
    """AC: crash coverage before requested, after sync, after transport, after terminal."""
    run_dir = _run_dir(tmp_path)
    _seed_lifecycle(run_dir)
    request_id = f"req-crash-{crash_point}"
    transport_calls: list[dict] = []

    def send(payload: dict[str, object]) -> dict[str, object]:
        transport_calls.append(dict(payload))
        return {"ok": True, "worker": "coder", "turn_id": "turn-1"}

    if crash_point == "before_requested":
        # No prior journal mutation; a fresh attempt proceeds and calls transport once.
        result = run_control_journal.execute_control_request(
            run_dir,
            op="steer",
            request_id=request_id,
            worker="coder",
            text="go",
            send=send,
        )
        assert result.replayed is False
        assert len(transport_calls) == 1
        return

    if crash_point == "after_requested":
        fingerprint = run_control_journal.control_fingerprint_payload(
            op="steer",
            request_id=request_id,
            worker="coder",
            text="go",
        )
        run_journal.append_event(
            _journal_path(run_dir),
            run_id=run_dir.name,
            event_type="control.requested",
            payload=fingerprint,
            idempotency_key=run_control_journal.requested_idempotency_key(request_id),
            expected_previous_sequence=2,
            recorded_at="2026-07-31T12:00:03.000000Z",
        )
        with pytest.raises(ControlJournalError) as exc:
            run_control_journal.execute_control_request(
                run_dir,
                op="steer",
                request_id=request_id,
                worker="coder",
                text="go",
                send=send,
            )
        assert exc.value.code == "indeterminate"
        assert transport_calls == []
        return

    if crash_point == "after_transport":
        original_append = run_control_journal.run_journal.append_event

        def crash_on_terminal(*args, **kwargs):
            event_type = kwargs.get("event_type")
            if event_type in {"control.observed", "control.failed"}:
                raise RuntimeError("simulated crash after transport before terminal append")
            return original_append(*args, **kwargs)

        monkeypatch.setattr(run_control_journal.run_journal, "append_event", crash_on_terminal)
        with pytest.raises(RuntimeError, match="simulated crash"):
            run_control_journal.execute_control_request(
                run_dir,
                op="steer",
                request_id=request_id,
                worker="coder",
                text="go",
                send=send,
            )
        assert len(transport_calls) == 1
        monkeypatch.undo()
        # Retry after the crash must report indeterminate and must not reissue.
        with pytest.raises(ControlJournalError) as exc:
            run_control_journal.execute_control_request(
                run_dir,
                op="steer",
                request_id=request_id,
                worker="coder",
                text="go",
                send=send,
            )
        assert exc.value.code == "indeterminate"
        assert len(transport_calls) == 1
        return

    # after_terminal: completed request replays without transport.
    run_control_journal.execute_control_request(
        run_dir,
        op="steer",
        request_id=request_id,
        worker="coder",
        text="go",
        send=send,
    )
    assert len(transport_calls) == 1
    replay = run_control_journal.execute_control_request(
        run_dir,
        op="steer",
        request_id=request_id,
        worker="coder",
        text="go",
        send=send,
    )
    assert replay.replayed is True
    assert len(transport_calls) == 1


def test_cli_steer_with_request_id_on_journal_run(tmp_path, capsys):
    run_dir = _run_dir(tmp_path)
    _seed_lifecycle(run_dir)
    registry = run_control.LiveTurnRegistry()

    class _Thread:
        def steer(self, text, turn_id):
            assert text == "please finish"
            assert turn_id == "turn-cli-1"

        def interrupt(self, turn_id):
            raise AssertionError("interrupt not expected")

    registry.register("coder", _Thread(), "turn-cli-1")
    socket_path = run_dir / "control.sock"
    server = run_control.ControlServer(socket_path, registry)
    transport = server.start()
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "status": "dispatching",
                "codex_transport": "app-server",
                "control_transport": transport.to_metadata(),
                "control_socket": str(socket_path),
            }
        )
        + "\n"
    )
    try:
        rc = cli.main(
            [
                "runs",
                "steer",
                str(run_dir),
                "coder",
                "please",
                "finish",
                "--request-id",
                "req-cli-1",
            ]
        )
    finally:
        server.close()
    captured = capsys.readouterr()
    assert rc == 0
    assert "request_id: req-cli-1" in captured.out
    assert "steer: coder" in captured.out
    # Replay
    rc = cli.main(
        [
            "runs",
            "steer",
            str(run_dir),
            "coder",
            "please",
            "finish",
            "--request-id",
            "req-cli-1",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert "control: replay" in captured.out


def test_cli_explicit_request_id_on_legacy_run_fails(tmp_path, capsys):
    run_dir = _run_dir(tmp_path)
    (run_dir / "run.json").write_text(
        json.dumps({"status": "started", "codex_transport": "app-server", "control_socket": str(run_dir / "x.sock")})
        + "\n"
    )
    rc = cli.main(["runs", "steer", str(run_dir), "coder", "go", "--request-id", "req-legacy"])
    assert rc == 2
    assert "legacy-no-journal" in capsys.readouterr().err


def test_events_follow_exits_after_terminal_event(tmp_path, capsys):
    run_dir = _run_dir(tmp_path)
    _seed_lifecycle(run_dir)
    result: dict[str, int] = {}

    def reader():
        result["rc"] = cli.main(["runs", "events", str(run_dir), "--follow", "--interval", "0.05"])

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    time.sleep(0.1)
    _append(
        _journal_path(run_dir),
        run_id=run_dir.name,
        event_type="run.completed",
        payload={"status": "ok", "detail": "done"},
        idempotency_key="complete-1",
        expected_previous_sequence=2,
        recorded_at="2026-07-31T12:00:02.000000Z",
    )
    thread.join(timeout=5.0)
    assert result.get("rc") == 0
    rows = _parse_ndjson(capsys.readouterr().out)
    assert [row["event_type"] for row in rows] == [
        "run.created",
        "run.dispatching.started",
        "run.completed",
    ]


def test_control_payload_never_contains_steering_text(tmp_path):
    """AC: lifecycle control events carry digests, not steering text."""
    run_dir = _run_dir(tmp_path)
    _seed_lifecycle(run_dir)
    secret = "super-secret-steering-instructions"

    def send(_payload: dict[str, object]) -> dict[str, object]:
        return {"ok": True, "worker": "coder", "turn_id": "turn-1"}

    run_control_journal.execute_control_request(
        run_dir,
        op="steer",
        request_id="req-privacy-1",
        worker="coder",
        text=secret,
        send=send,
    )
    raw = _journal_path(run_dir).read_text()
    assert secret not in raw
    report = run_journal.read_journal_bounded(_journal_path(run_dir))
    requested = next(event for event in report.events if event.event_type == "control.requested")
    assert requested.payload["text_digest"] == run_control_journal.text_digest(secret)
    assert "text" not in requested.payload


# ---------------------------------------------------------------------------
# Issue #651 regression tests (red phase).
# ---------------------------------------------------------------------------


def _write_run_json(run_dir: Path, snapshot: dict) -> None:
    (run_dir / "run.json").write_text(json.dumps(snapshot) + "\n")


def _journal_state(journal_path: Path) -> tuple[bytes, tuple[int, str | None]]:
    """Journal bytes plus the chain head (tail sequence + digest)."""
    data = journal_path.read_bytes()
    report = run_journal.read_journal_bounded(journal_path)
    if report.events:
        head: tuple[int, str | None] = (report.events[-1].sequence, report.events[-1].event_digest)
    else:
        head = (0, None)
    return data, head


def _shadow_state(run_dir: Path) -> bytes | None:
    shadow_path = run_shadow.shadow_artifact_path(run_dir)
    return shadow_path.read_bytes() if shadow_path.exists() else None


def test_control_request_refuses_completed_run_before_journal_or_transport(tmp_path):
    """Issue #651: a completed run refuses control before journaling or sending."""
    run_dir = _run_dir(tmp_path)
    _seed_lifecycle(run_dir, terminal=True)
    journal = _journal_path(run_dir)
    _write_run_json(
        run_dir,
        {
            "status": "ok",
            "codex_transport": "app-server",
            "control_socket": str(run_dir / "control.sock"),
        },
    )
    before_bytes, before_head = _journal_state(journal)
    shadow_before = _shadow_state(run_dir)
    run_json_before = (run_dir / "run.json").read_bytes()
    calls: list[dict] = []

    def send(payload: dict[str, object]) -> dict[str, object]:
        calls.append(dict(payload))
        return {"ok": True, "worker": "coder", "turn_id": "turn-1"}

    with pytest.raises(ControlJournalError) as exc:
        run_control_journal.execute_control_request(
            run_dir,
            op="steer",
            request_id="req-completed-1",
            worker="coder",
            text="keep going",
            send=send,
        )
    assert exc.value.code == "control_run_not_live"
    after_bytes, after_head = _journal_state(journal)
    assert after_bytes == before_bytes
    assert after_head == before_head
    assert _shadow_state(run_dir) == shadow_before
    assert (run_dir / "run.json").read_bytes() == run_json_before
    assert calls == []


def test_control_request_refuses_when_owner_not_active(tmp_path):
    """Issue #651: a live-status run without an active lock owner refuses control."""
    run_dir = _run_dir(tmp_path)
    _seed_lifecycle(run_dir)
    journal = _journal_path(run_dir)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _write_run_json(
        run_dir,
        {
            "status": "dispatching",
            "cwd": str(workspace),
            "lock_workspace": str(workspace),
            "codex_transport": "app-server",
            "control_socket": str(run_dir / "control.sock"),
        },
    )
    before_bytes, before_head = _journal_state(journal)
    shadow_before = _shadow_state(run_dir)
    run_json_before = (run_dir / "run.json").read_bytes()
    calls: list[dict] = []

    def send(payload: dict[str, object]) -> dict[str, object]:
        calls.append(dict(payload))
        return {"ok": True, "worker": "coder", "turn_id": "turn-1"}

    with pytest.raises(ControlJournalError) as exc:
        run_control_journal.execute_control_request(
            run_dir,
            op="steer",
            request_id="req-no-owner-1",
            worker="coder",
            text="keep going",
            send=send,
        )
    assert exc.value.code == "control_owner_not_active"
    after_bytes, after_head = _journal_state(journal)
    assert after_bytes == before_bytes
    assert after_head == before_head
    assert _shadow_state(run_dir) == shadow_before
    assert (run_dir / "run.json").read_bytes() == run_json_before
    assert calls == []


def test_control_failed_payload_uses_closed_error_class_and_detail_digest(tmp_path):
    """Issue #651: control.failed carries error_class + detail_digest, never raw detail."""
    secret = "provider-secret-body-7f3a9c"
    provider_code = "PROVIDER-ERR-4242"

    def send_raises(_payload: dict[str, object]) -> dict[str, object]:
        raise RuntimeError(f"transport exploded: {secret}")

    def send_rejected(_payload: dict[str, object]) -> dict[str, object]:
        return {"ok": False, "code": provider_code, "error": secret}

    def send_malformed(_payload: dict[str, object]) -> dict[str, object]:
        return {"ok": "yes", "error": secret}

    cases = [
        ("exc", send_raises, "transport_exception", f"transport exploded: {secret}"),
        ("rejected", send_rejected, "transport_rejected", secret),
        ("protocol", send_malformed, "transport_protocol_error", secret),
    ]
    for label, send, expected_class, detail in cases:
        run_dir = _run_dir(tmp_path, f"20260731-651000-failed-{label}")
        _seed_lifecycle(run_dir)
        run_control_journal.execute_control_request(
            run_dir,
            op="steer",
            request_id=f"req-failed-{label}",
            worker="coder",
            text="go",
            send=send,
        )
        raw = _journal_path(run_dir).read_bytes()
        assert secret.encode("utf-8") not in raw
        assert provider_code.encode("utf-8") not in raw
        report = run_journal.read_journal_bounded(_journal_path(run_dir))
        failed = next(event for event in report.events if event.event_type == "control.failed")
        assert set(failed.payload) == {
            "op",
            "worker",
            "request_id",
            "error_class",
            "detail_digest",
        }
        assert failed.payload["error_class"] == expected_class
        assert failed.payload["detail_digest"] == hashlib.sha256(detail.encode("utf-8")).hexdigest()


def test_control_failed_rejects_unrecognized_error_class(tmp_path):
    """Issue #651: control.failed error_class is a closed enum validated before append."""
    del tmp_path  # schema-level validation only; no journal needed
    base = {"op": "steer", "request_id": "req-enum-1", "detail_digest": "a" * 64}
    for klass in ("transport_exception", "transport_rejected", "transport_protocol_error"):
        run_events.validate_event_request(
            event_type="control.failed",
            payload={**base, "error_class": klass},
            idempotency_key=f"control.failed:req-enum-{klass}",
        )
    with pytest.raises(run_events.CanonicalizationError):
        run_events.validate_event_request(
            event_type="control.failed",
            payload={**base, "error_class": "provider-timeout"},
            idempotency_key="control.failed:req-enum-bogus",
        )


def test_concurrent_same_request_id_sends_once(tmp_path, monkeypatch):
    """Issue #651: two concurrent same-request-id callers invoke the transport at most once."""
    run_dir = _run_dir(tmp_path)
    _seed_lifecycle(run_dir)
    barrier = threading.Barrier(2)
    original_inspect = run_control_journal.inspect_control_request

    def rendezvous_inspect(run_dir_arg, *, request_id, fingerprint):
        state, requested, terminal = original_inspect(
            run_dir_arg,
            request_id=request_id,
            fingerprint=fingerprint,
        )
        if state == "absent":
            # Both callers observe "absent" before either may claim the request.
            barrier.wait(timeout=10.0)
        return state, requested, terminal

    monkeypatch.setattr(run_control_journal, "inspect_control_request", rendezvous_inspect)

    transport_calls: list[dict] = []
    transport_lock = threading.Lock()

    def send(payload: dict[str, object]) -> dict[str, object]:
        with transport_lock:
            transport_calls.append(dict(payload))
        return {"ok": True, "worker": "coder", "turn_id": "turn-1"}

    outcomes: dict[str, object] = {}

    def caller(name: str) -> None:
        try:
            outcomes[name] = run_control_journal.execute_control_request(
                run_dir,
                op="steer",
                request_id="req-race-1",
                worker="coder",
                text="keep going",
                send=send,
            )
        except Exception as exc:  # recorded for outcome assertions below
            outcomes[name] = exc

    threads = [threading.Thread(target=caller, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15.0)
        assert not thread.is_alive()

    # At-most-once: exactly one transport invocation across both callers.
    assert len(transport_calls) == 1
    report = run_journal.read_journal_bounded(_journal_path(run_dir))
    control_types = [event.event_type for event in report.events if event.event_type.startswith("control.")]
    assert control_types == ["control.requested", "control.observed"]
    # The losing caller must observe replay or indeterminate, never a second send.
    losers = [
        outcome
        for outcome in outcomes.values()
        if (
            isinstance(outcome, run_control_journal.ControlResult)
            and outcome.replayed is True
        )
        or (isinstance(outcome, ControlJournalError) and outcome.code == "indeterminate")
    ]
    assert len(losers) == 1


def test_events_reads_post_terminal_redaction_and_control_records(tmp_path, capsys):
    """Issue #651: static runs events reads past the first terminal lifecycle event."""
    run_dir = _run_dir(tmp_path)
    _seed_lifecycle(run_dir, terminal=True)
    journal = _journal_path(run_dir)
    _append(
        journal,
        run_id=run_dir.name,
        event_type="run.redaction.recorded",
        payload={
            "operation_id": "op-post-terminal-1",
            "affected_first_sequence": 2,
            "affected_last_sequence": 2,
            "reason_class": "credential-exposure",
            "record_sha256": "b" * 64,
        },
        idempotency_key="redact-post-terminal-1",
        expected_previous_sequence=3,
        recorded_at="2026-07-31T12:00:03.000000Z",
    )
    fingerprint = run_control_journal.control_fingerprint_payload(
        op="steer",
        request_id="req-post-terminal-1",
        worker="coder",
        text="go",
    )
    _append(
        journal,
        run_id=run_dir.name,
        event_type="control.requested",
        payload=fingerprint,
        idempotency_key=run_control_journal.requested_idempotency_key("req-post-terminal-1"),
        expected_previous_sequence=4,
        recorded_at="2026-07-31T12:00:04.000000Z",
    )
    _append(
        journal,
        run_id=run_dir.name,
        event_type="control.observed",
        payload={"op": "steer", "request_id": "req-post-terminal-1", "worker": "coder", "detail": "ok"},
        idempotency_key=run_control_journal.observed_idempotency_key("req-post-terminal-1"),
        expected_previous_sequence=5,
        recorded_at="2026-07-31T12:00:05.000000Z",
    )

    rc = cli.main(["runs", "events", str(run_dir)])
    rows = _parse_ndjson(capsys.readouterr().out)
    assert [row["sequence"] for row in rows] == [1, 2, 3, 4, 5, 6]
    assert [row["event_type"] for row in rows] == [
        "run.created",
        "run.dispatching.started",
        "run.completed",
        "run.redaction.recorded",
        "control.requested",
        "control.observed",
    ]
    assert all(row["cursor"] for row in rows)
    # The terminal-derived exit code is retained even though reads continue past it.
    assert rc == 0


def test_events_follow_refuses_suffix_after_redaction_rewrites_emitted_prefix(tmp_path, capsys):
    """Issue #651: follow mode revalidates chain continuity between polls."""
    run_dir = _run_dir(tmp_path)
    _seed_lifecycle(run_dir)
    journal = _journal_path(run_dir)
    result: dict[str, int] = {}

    def reader() -> None:
        result["rc"] = cli.main(["runs", "events", str(run_dir), "--follow", "--interval", "0.05"])

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()

    # Wait until the follower has emitted the initial prefix.
    emitted = ""
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        emitted += capsys.readouterr().out
        if len(_parse_ndjson(emitted)) >= 2:
            break
        time.sleep(0.02)
    assert [row["sequence"] for row in _parse_ndjson(emitted)] == [1, 2]

    # Perform a valid redaction rewrite of the already-emitted prefix: the journal
    # still verifies, but the re-chained digests no longer match what was emitted.
    report = run_journal.read_journal_bounded(journal)
    _rewritten, rewritten_bytes = run_redaction._rewrite_events(
        report.events,
        sequence_start=2,
        sequence_end=2,
        operation_id="op-follow-rewrite-1",
    )
    staging = journal.with_name(journal.name + ".rewrite-tmp")
    staging.write_bytes(rewritten_bytes)
    os.replace(staging, journal)

    # Expose a later suffix chained onto the rewritten tail.
    _append(
        journal,
        run_id=run_dir.name,
        event_type="run.planning.completed",
        payload={"detail": "planned"},
        idempotency_key="planning-complete-follow-1",
        expected_previous_sequence=2,
        recorded_at="2026-07-31T12:00:05.000000Z",
    )

    thread.join(timeout=5.0)
    captured = capsys.readouterr()
    emitted += captured.out
    rows = _parse_ndjson(emitted)
    # The follower must stop with a cursor-continuity error and emit no suffix
    # that fails to chain onto the previously emitted prefix.
    assert [row["sequence"] for row in rows] == [1, 2]
    assert not thread.is_alive()
    assert result.get("rc") == 2
    assert "continuity" in captured.err.lower()
