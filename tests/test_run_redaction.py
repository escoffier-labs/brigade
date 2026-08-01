"""Tests for the operator-only lifecycle journal redaction procedure."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

from brigade import cli, run_checkpoint, run_events, run_journal, run_projector, run_redaction, run_shadow

RUN_ID = "20260730-190000-redact"
SECRET = "secret-value-that-must-not-survive"
REASON_CODE = "credential-exposure"


def _journal_path(run_dir: Path) -> Path:
    return run_dir / "events" / "lifecycle.jsonl"


def _append(
    journal: Path,
    *,
    event_type: str,
    payload: dict,
    key: str,
    prior: int,
    second: int,
) -> run_journal.RunEvent:
    return run_journal.append_event(
        journal,
        run_id=RUN_ID,
        event_type=event_type,
        payload=payload,
        idempotency_key=key,
        expected_previous_sequence=prior,
        recorded_at=f"2026-07-30T19:00:{second:02d}.000000Z",
    )


def _authority_run(
    tmp_path: Path,
    *,
    first_idempotency_key: str = "created",
) -> tuple[Path, dict, list[run_journal.RunEvent]]:
    run_dir = tmp_path / "workspace" / ".brigade" / "runs" / RUN_ID
    journal = _journal_path(run_dir)
    base = {
        "schema": "brigade.run.v1",
        "schema_version": 1,
        "task": "redaction fixture",
        "status": "ok",
        "cwd": str(tmp_path / "workspace"),
        "lock_workspace": str(tmp_path / "workspace"),
        "lifecycle_journal_requested": True,
        "run_journal_authority_requested": True,
    }
    checkpoint_bytes = run_projector.encode_snapshot_bytes(base)
    run_checkpoint.publish_checkpoint_file(run_dir, checkpoint_bytes)
    events = [
        _append(
            journal,
            event_type="run.created",
            payload={"status": "started"},
            key=first_idempotency_key,
            prior=0,
            second=0,
        ),
        _append(
            journal,
            event_type="run.planning.started",
            payload={"detail": SECRET},
            key="planning",
            prior=1,
            second=1,
        ),
        _append(
            journal,
            event_type=run_checkpoint.CHECKPOINT_EVENT_TYPE,
            payload=run_checkpoint._checkpoint_payload(
                checkpoint_bytes,
                paired_event_type="run.completed",
                body_kind="base-stripped",
            ),
            key=run_checkpoint._checkpoint_idempotency_key(
                run_checkpoint._checkpoint_payload(
                    checkpoint_bytes,
                    paired_event_type="run.completed",
                    body_kind="base-stripped",
                )["sha256"],
                paired_event_type="run.completed",
                body_kind="base-stripped",
            ),
            prior=2,
            second=2,
        ),
        _append(
            journal,
            event_type="run.completed",
            payload={"status": "ok", "detail": "complete"},
            key="completed",
            prior=3,
            second=3,
        ),
    ]
    projection = run_projector.project_run_snapshot(base, events, journal_present=True)
    (run_dir / "run.json").write_bytes(projection.to_bytes())
    return run_dir, projection.snapshot, events


def _without_tail_digest(snapshot: dict) -> dict:
    return {key: value for key, value in snapshot.items() if key != "journal_last_event_digest"}


def _latest_checkpoint_path(run_dir: Path) -> Path:
    report = run_journal.read_journal_bounded(_journal_path(run_dir))
    event = run_checkpoint.latest_checkpoint_event(report.events)
    assert event is not None
    return run_checkpoint.checkpoint_path(run_dir, event.payload["sha256"])


def _append_uncovered_terminal_event(run_dir: Path) -> None:
    journal = _journal_path(run_dir)
    report = run_journal.read_journal_bounded(journal)
    _append(
        journal,
        event_type="run.completed",
        payload={"status": "ok", "detail": "late terminal event"},
        key="late-completed",
        prior=report.events[-1].sequence,
        second=4,
    )
    events = run_journal.read_journal_bounded(journal).events
    snapshot = json.loads((run_dir / "run.json").read_text())
    projection = run_projector.project_run_snapshot(snapshot, events, journal_present=True)
    (run_dir / "run.json").write_bytes(projection.to_bytes())


def _artifact_snapshot(run_dir: Path) -> dict[str, bytes]:
    return {str(path.relative_to(run_dir)): path.read_bytes() for path in sorted(run_dir.rglob("*")) if path.is_file()}


@pytest.mark.parametrize("platform_name", ["nt", "java"])
def test_redaction_refuses_unsupported_platform_before_lock_or_mutation(
    tmp_path,
    monkeypatch,
    platform_name,
):
    run_dir, _, _ = _authority_run(tmp_path)
    before = _artifact_snapshot(run_dir)
    external = tmp_path / "external"
    external.mkdir()
    original_events = run_dir / "events"
    parked_events = run_dir / "events-parked"
    monkeypatch.setattr(run_redaction, "_PLATFORM_NAME", platform_name, raising=False)

    def raceable_parent_replacement(*args, **kwargs):
        original_events.rename(parked_events)
        original_events.symlink_to(external, target_is_directory=True)
        (external / "escaped").write_text("unsafe fallback reached")
        raise AssertionError("platform gate ran after the write-capable lock path")

    monkeypatch.setattr(
        run_redaction,
        "_exclusive_redaction_lock",
        raceable_parent_replacement,
    )

    with pytest.raises(run_redaction.RedactionError, match="unsupported platform"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )

    assert _artifact_snapshot(run_dir) == before
    assert original_events.is_dir()
    assert not original_events.is_symlink()
    assert list(external.iterdir()) == []
    assert not (tmp_path / "workspace" / ".brigade" / "run.lock").exists()


@pytest.mark.parametrize("missing_operation", [os.open, os.mkdir, os.rename, os.unlink, os.link])
def test_redaction_refuses_missing_dirfd_operation_before_any_write(
    tmp_path,
    monkeypatch,
    missing_operation,
):
    run_dir, _, _ = _authority_run(tmp_path)
    before = _artifact_snapshot(run_dir)
    supported = set(os.supports_dir_fd)
    supported.discard(missing_operation)
    monkeypatch.setattr(run_redaction.os, "supports_dir_fd", supported)
    monkeypatch.setattr(
        run_redaction,
        "_exclusive_redaction_lock",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unsupported capability reached lock acquisition")
        ),
    )

    with pytest.raises(run_redaction.RedactionError, match="unsupported platform"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )

    assert _artifact_snapshot(run_dir) == before
    assert not (tmp_path / "workspace" / ".brigade" / "run.lock").exists()


def test_redaction_refuses_missing_fd_listdir_before_any_write(tmp_path, monkeypatch):
    run_dir, _, _ = _authority_run(tmp_path)
    before = _artifact_snapshot(run_dir)
    supported = set(os.supports_fd)
    supported.discard(os.listdir)
    monkeypatch.setattr(run_redaction.os, "supports_fd", supported)
    monkeypatch.setattr(
        run_redaction,
        "_exclusive_redaction_lock",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unsupported capability reached lock acquisition")
        ),
    )

    with pytest.raises(run_redaction.RedactionError, match="unsupported platform"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )

    assert _artifact_snapshot(run_dir) == before


@pytest.mark.parametrize(
    "missing_capability",
    ["directory-fsync", "nofollow", "directory-open", "fchmod", "link-nofollow"],
)
def test_redaction_refuses_missing_durability_or_containment_before_any_write(
    tmp_path,
    monkeypatch,
    missing_capability,
):
    run_dir, _, _ = _authority_run(tmp_path)
    before = _artifact_snapshot(run_dir)
    if missing_capability == "directory-fsync":
        monkeypatch.setattr(run_redaction.os, "fsync", None)
    elif missing_capability == "nofollow":
        monkeypatch.setattr(run_journal, "_O_NOFOLLOW", 0)
    elif missing_capability == "directory-open":
        monkeypatch.setattr(run_journal, "_O_DIRECTORY", 0)
    elif missing_capability == "fchmod":
        monkeypatch.setattr(run_journal, "_HAS_FCHMOD", False)
    else:
        supported = set(os.supports_follow_symlinks)
        supported.discard(os.link)
        monkeypatch.setattr(run_redaction.os, "supports_follow_symlinks", supported)
    monkeypatch.setattr(
        run_redaction,
        "_exclusive_redaction_lock",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("unsupported durability or containment reached lock acquisition")
        ),
    )

    with pytest.raises(run_redaction.RedactionError, match="unsupported platform"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )

    assert _artifact_snapshot(run_dir) == before


def test_redaction_refuses_directory_fsync_probe_before_lock_or_mutation(
    tmp_path,
    monkeypatch,
):
    run_dir, _, _ = _authority_run(tmp_path)
    before = _artifact_snapshot(run_dir)
    monkeypatch.setattr(
        run_redaction.os,
        "fsync",
        lambda fd: (_ for _ in ()).throw(OSError("directory fsync unsupported")),
    )
    monkeypatch.setattr(
        run_redaction,
        "_exclusive_redaction_lock",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("failed directory fsync probe reached lock acquisition")
        ),
    )

    with pytest.raises(run_redaction.RedactionError, match="unsupported platform"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )

    assert _artifact_snapshot(run_dir) == before
    assert not (tmp_path / "workspace" / ".brigade" / "run.lock").exists()


def test_unsupported_platform_does_not_break_redaction_cli_help(monkeypatch, capsys):
    monkeypatch.setattr(run_redaction, "_PLATFORM_NAME", "nt")

    with pytest.raises(SystemExit) as raised:
        cli.main(["runs", "redact", "--help"])

    assert raised.value.code == 0
    help_text = capsys.readouterr().out
    assert "usage: brigade runs redact" in help_text
    assert "Closed incident reason code" in help_text


def test_cleanup_refuses_unsupported_platform_before_lock_or_mutation(tmp_path, monkeypatch):
    run_dir, _, _ = _authority_run(tmp_path)
    report = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    before = _artifact_snapshot(run_dir)
    monkeypatch.setattr(run_redaction, "_PLATFORM_NAME", "nt", raising=False)
    monkeypatch.setattr(
        run_redaction,
        "_exclusive_redaction_lock",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("unsupported cleanup reached lock acquisition")),
    )

    with pytest.raises(run_redaction.RedactionError, match="unsupported platform"):
        run_redaction.cleanup_redaction_quarantine(
            run_dir,
            operation_id=report.operation_id,
            operator_confirmed=True,
        )

    assert _artifact_snapshot(run_dir) == before
    assert report.quarantine_path.is_file()
    assert not (tmp_path / "workspace" / ".brigade" / "run.lock").exists()


@pytest.mark.parametrize(
    ("failure_point", "exception_type"),
    [
        ("fstat", KeyboardInterrupt),
        ("identity", SystemExit),
        ("mode", KeyboardInterrupt),
    ],
)
def test_open_directory_handle_closes_fd_once_on_baseexception(
    tmp_path,
    monkeypatch,
    failure_point,
    exception_type,
):
    directory = tmp_path / "directory"
    directory.mkdir(mode=0o755)
    real_open = run_journal._open_nofollow
    real_close = os.close
    real_fstat = os.fstat
    opened: list[int] = []
    closed: list[int] = []

    def capture_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def track_close(fd):
        if opened and fd == opened[0]:
            closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(run_journal, "_open_nofollow", capture_open)
    monkeypatch.setattr(run_redaction.os, "close", track_close)
    if failure_point == "fstat":
        monkeypatch.setattr(
            run_redaction.os,
            "fstat",
            lambda fd: (_ for _ in ()).throw(exception_type()),
        )
    elif failure_point == "identity":

        class ExplodingIdentity:
            def __init__(self, fd):
                info = real_fstat(fd)
                self.st_mode = info.st_mode
                self.st_ino = info.st_ino

            @property
            def st_dev(self):
                raise exception_type()

        monkeypatch.setattr(
            run_redaction.os,
            "fstat",
            ExplodingIdentity,
        )
    else:
        monkeypatch.setattr(
            run_journal,
            "_chmod_fd_or_path",
            lambda *args, **kwargs: (_ for _ in ()).throw(exception_type()),
        )

    with pytest.raises(exception_type):
        run_redaction._open_directory_handle(directory, category="test directory")

    assert len(opened) == 1
    assert closed == opened
    with pytest.raises(OSError):
        real_fstat(opened[0])


def test_open_directory_handle_preserves_baseexception_when_close_raises(
    tmp_path,
    monkeypatch,
):
    directory = tmp_path / "directory"
    directory.mkdir()
    real_open = run_journal._open_nofollow
    real_close = os.close
    opened: list[int] = []
    closed: list[int] = []

    def capture_open(*args, **kwargs):
        fd = real_open(*args, **kwargs)
        opened.append(fd)
        return fd

    def close_then_raise(fd):
        closed.append(fd)
        real_close(fd)
        raise OSError("simulated close failure")

    monkeypatch.setattr(run_journal, "_open_nofollow", capture_open)
    monkeypatch.setattr(
        run_redaction.os,
        "fstat",
        lambda fd: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    monkeypatch.setattr(run_redaction.os, "close", close_then_raise)

    with pytest.raises(KeyboardInterrupt):
        run_redaction._open_directory_handle(directory, category="test directory")

    assert closed == opened


def test_redaction_quarantines_rewrites_rechains_and_reprojects(tmp_path):
    run_dir, before_projection, original_events = _authority_run(tmp_path)

    report = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )

    active = _journal_path(run_dir).read_bytes()
    record = report.record_path.read_bytes()
    assert SECRET.encode() not in active
    assert SECRET.encode() not in record
    assert b'"reason_code": "credential-exposure"' in record
    assert b'"sequence_start": 2' in record
    assert b'"sequence_end": 2' in record
    assert report.quarantine_path.is_file()
    assert SECRET.encode() in report.quarantine_path.read_bytes()
    assert stat.S_IMODE(report.quarantine_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(report.quarantine_path.parent.stat().st_mode) == 0o700

    verified = run_journal.read_journal_bounded(_journal_path(run_dir))
    assert verified.chain_errors == []
    assert verified.partial_tail is None
    assert len(verified.events) == len(original_events) + 1
    assert verified.events[1].payload == {"detail": "[REDACTED]"}
    assert verified.events[0].event_digest == original_events[0].event_digest
    assert verified.events[1].event_digest != original_events[1].event_digest
    assert verified.events[2].previous_digest == verified.events[1].event_digest

    current = json.loads((run_dir / "run.json").read_text())
    after_projection = run_projector.project_run_snapshot(current, verified.events, journal_present=True).snapshot
    assert current == after_projection
    expected_projection = dict(before_projection)
    expected_projection["journal_last_sequence"] = len(verified.events)
    assert _without_tail_digest(current) == _without_tail_digest(expected_projection)
    assert current["journal_last_event_digest"] == verified.events[-1].event_digest


def test_redaction_preserves_first_anchor_when_second_range_contains_it(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)

    first = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    first_anchor = next(
        event
        for event in run_journal.read_journal_bounded(_journal_path(run_dir)).events
        if event.event_type == "run.redaction.recorded" and event.payload["operation_id"] == first.operation_id
    )
    second_sequence_start = 2
    second_sequence_end = first_anchor.sequence
    assert second_sequence_start <= first_anchor.sequence <= second_sequence_end

    second = run_redaction.redact_journal(
        run_dir,
        sequence_start=second_sequence_start,
        sequence_end=second_sequence_end,
        reason=REASON_CODE,
        operator_confirmed=True,
    )

    verified = run_journal.read_journal_bounded(_journal_path(run_dir))
    assert verified.chain_errors == []
    assert verified.partial_tail is None
    anchors = [event for event in verified.events if event.event_type == "run.redaction.recorded"]
    assert {anchor.payload["operation_id"] for anchor in anchors} == {first.operation_id, second.operation_id}
    assert all(
        set(anchor.payload)
        == {
            "operation_id",
            "affected_first_sequence",
            "affected_last_sequence",
            "reason_class",
            "record_sha256",
        }
        for anchor in anchors
    )
    record_hashes = {
        report.operation_id: hashlib.sha256(report.record_path.read_bytes()).hexdigest() for report in (first, second)
    }
    assert {anchor.payload["operation_id"]: anchor.payload["record_sha256"] for anchor in anchors} == record_hashes
    second_record = json.loads(second.record_path.read_text())
    assert second_record["parent_operation_id"] == first.operation_id


def test_redaction_rejects_preserved_structural_only_range_without_mutation(tmp_path):
    run_dir, _, events = _authority_run(tmp_path)
    checkpoint = next(event for event in events if event.event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE)
    before = _artifact_snapshot(run_dir)

    with pytest.raises(run_redaction.RedactionError, match="redaction range contains no redactable payloads"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=checkpoint.sequence,
            sequence_end=checkpoint.sequence,
            reason=REASON_CODE,
            operator_confirmed=True,
        )

    assert _artifact_snapshot(run_dir) == before
    assert not (run_dir / "events" / "redactions").exists()


def test_resume_projection_accepts_only_trailing_redaction_anchor_lag(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    events = run_journal.read_journal_bounded(_journal_path(run_dir)).events
    projected_before_anchor = json.loads((run_dir / "run.json").read_text())
    projected_before_anchor["journal_last_sequence"] = events[-2].sequence
    projected_before_anchor["journal_last_event_digest"] = events[-2].event_digest
    (run_dir / "run.json").write_text(json.dumps(projected_before_anchor, indent=2, sort_keys=True) + "\n")

    run_redaction._resume_projection_after_rewrite(run_dir, events)

    assert (
        json.loads((run_dir / "run.json").read_text())
        == run_projector.project_run_snapshot(projected_before_anchor, events, journal_present=True).snapshot
    )


def test_resume_projection_rejects_non_anchor_sequence_lag(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    events = run_journal.read_journal_bounded(_journal_path(run_dir)).events
    stale = json.loads((run_dir / "run.json").read_text())
    stale["journal_last_sequence"] = events[-3].sequence
    stale["journal_last_event_digest"] = events[-3].event_digest
    (run_dir / "run.json").write_text(json.dumps(stale, indent=2, sort_keys=True) + "\n")

    with pytest.raises(run_redaction.RedactionError, match="projection sequence lag"):
        run_redaction._resume_projection_after_rewrite(run_dir, events)


def test_redaction_retry_appends_missing_anchor_after_verified_record(tmp_path, monkeypatch):
    run_dir, _, _ = _authority_run(tmp_path)
    real_append = run_redaction._append_redaction_anchor
    failed = False

    def fail_before_anchor(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise run_redaction.RedactionError("simulated anchor append failure")
        return real_append(*args, **kwargs)

    monkeypatch.setattr(run_redaction, "_append_redaction_anchor", fail_before_anchor)
    with pytest.raises(run_redaction.RedactionError, match="anchor append"):
        run_redaction.redact_journal(
            run_dir, sequence_start=2, sequence_end=2, reason=REASON_CODE, operator_confirmed=True
        )

    monkeypatch.setattr(run_redaction, "_append_redaction_anchor", real_append)
    report = run_redaction.redact_journal(
        run_dir, sequence_start=2, sequence_end=2, reason=REASON_CODE, operator_confirmed=True
    )
    events = run_journal.read_journal_bounded(_journal_path(run_dir)).events
    anchors = [event for event in events if event.event_type == "run.redaction.recorded"]
    assert [anchor.payload["operation_id"] for anchor in anchors] == [report.operation_id]
    assert (
        json.loads((run_dir / "run.json").read_text())
        == run_projector.project_run_snapshot(
            json.loads((run_dir / "run.json").read_text()), events, journal_present=True
        ).snapshot
    )


def test_redaction_retry_reprojects_after_anchor_before_projection_failure(tmp_path, monkeypatch):
    run_dir, _, _ = _authority_run(tmp_path)
    real_replace_projection = run_redaction._replace_projection
    failed = False

    def fail_anchor_projection(path, projection):
        nonlocal failed
        if not failed and projection.snapshot["journal_last_sequence"] == 5:
            failed = True
            raise run_redaction.RedactionError("simulated anchor projection failure")
        return real_replace_projection(path, projection)

    monkeypatch.setattr(run_redaction, "_replace_projection", fail_anchor_projection)
    with pytest.raises(run_redaction.RedactionError, match="anchor projection"):
        run_redaction.redact_journal(
            run_dir, sequence_start=2, sequence_end=2, reason=REASON_CODE, operator_confirmed=True
        )

    monkeypatch.setattr(run_redaction, "_replace_projection", real_replace_projection)
    report = run_redaction.redact_journal(
        run_dir, sequence_start=2, sequence_end=2, reason=REASON_CODE, operator_confirmed=True
    )
    events = run_journal.read_journal_bounded(_journal_path(run_dir)).events
    anchors = [event for event in events if event.event_type == "run.redaction.recorded"]
    assert [anchor.payload["operation_id"] for anchor in anchors] == [report.operation_id]
    assert (
        json.loads((run_dir / "run.json").read_text())
        == run_projector.project_run_snapshot(
            json.loads((run_dir / "run.json").read_text()), events, journal_present=True
        ).snapshot
    )


def test_redaction_refuses_anchor_append_after_ownership_loss(tmp_path, monkeypatch):
    run_dir, _, _ = _authority_run(tmp_path)
    real_assert_owner = run_redaction._assert_active_owner
    calls = 0

    def lose_owner(workspace, resolved_run_dir):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise run_redaction.RedactionError("redaction lost exclusive run lock ownership")
        return real_assert_owner(workspace, resolved_run_dir)

    monkeypatch.setattr(run_redaction, "_assert_active_owner", lose_owner)
    with pytest.raises(run_redaction.RedactionError, match="lost exclusive"):
        run_redaction.redact_journal(
            run_dir, sequence_start=2, sequence_end=2, reason=REASON_CODE, operator_confirmed=True
        )

    assert not [
        event
        for event in run_journal.read_journal_bounded(_journal_path(run_dir)).events
        if event.event_type == "run.redaction.recorded"
    ]


def test_replaced_redaction_record_set_fails_chained_anchor_validation(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    report = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    record = json.loads(report.record_path.read_text())
    replacement = (json.dumps(record, separators=(",", ":")) + "\n").encode()
    assert replacement != report.record_path.read_bytes()
    report.record_path.write_bytes(replacement)

    with pytest.raises(run_redaction.RedactionError, match="anchor"):
        run_redaction._post_replace_verify(run_dir, expected_digest=None)


def test_redaction_refuses_live_lock_without_mutation(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    workspace = tmp_path / "workspace"
    lock = workspace / ".brigade" / "run.lock"
    lock.mkdir(parents=True)
    (lock / "pid").write_text(f"{os.getpid()}\n")
    (lock / "owner.json").write_text(
        json.dumps(
            {
                "schema": "brigade.run_lock.v1",
                "owner_token": "active-owner",
                "pid": os.getpid(),
                "run_dir": str(run_dir.resolve()),
                "acquired_at": "2026-07-30T19:00:00+00:00",
            }
        )
    )
    before = _journal_path(run_dir).read_bytes()

    with pytest.raises(run_redaction.RedactionError, match="run lock state is live"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )

    assert _journal_path(run_dir).read_bytes() == before
    assert not (run_dir / "events" / "redactions").exists()


@pytest.mark.parametrize("lock_kind", ["malformed", "stale", "foreign"])
def test_redaction_fails_closed_on_ambiguous_lock_state(tmp_path, lock_kind):
    run_dir, _, _ = _authority_run(tmp_path)
    workspace = tmp_path / "workspace"
    lock = workspace / ".brigade" / "run.lock"
    if lock_kind == "malformed":
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("not a directory")
    else:
        lock.mkdir(parents=True)
        (lock / "pid").write_text("99999999\n")
        owner_run = run_dir if lock_kind == "stale" else tmp_path / "other-run"
        (lock / "owner.json").write_text(
            json.dumps(
                {
                    "schema": "brigade.run_lock.v1",
                    "owner_token": "dead-owner",
                    "pid": 99999999,
                    "run_dir": str(owner_run.resolve()),
                    "acquired_at": "2026-07-30T19:00:00+00:00",
                }
            )
        )
    before = _journal_path(run_dir).read_bytes()

    with pytest.raises(run_redaction.RedactionError, match="run lock state"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )

    assert _journal_path(run_dir).read_bytes() == before


def test_redaction_refuses_malformed_journal_before_quarantine(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    journal = _journal_path(run_dir)
    journal.write_bytes(journal.read_bytes() + b'{"partial":')
    before = journal.read_bytes()

    with pytest.raises(run_redaction.RedactionError, match="journal"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )

    assert journal.read_bytes() == before
    assert not (run_dir / "events" / "redactions").exists()


@pytest.mark.parametrize(
    ("start", "end"),
    [(0, 1), (1, 0), (2, 5), (True, 2), (2, False)],
)
def test_redaction_rejects_invalid_sequence_range_without_mutation(tmp_path, start, end):
    run_dir, _, _ = _authority_run(tmp_path)
    before = _journal_path(run_dir).read_bytes()

    with pytest.raises(run_redaction.RedactionError, match="sequence range"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=start,
            sequence_end=end,
            reason=REASON_CODE,
            operator_confirmed=True,
        )

    assert _journal_path(run_dir).read_bytes() == before


def test_redaction_requires_explicit_operator_confirmation(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    before = _journal_path(run_dir).read_bytes()

    with pytest.raises(run_redaction.RedactionError, match="operator confirmation"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
        )

    assert _journal_path(run_dir).read_bytes() == before


@pytest.mark.parametrize("reason", ["", " ", "x" * 241, "line one\nline two"])
def test_redaction_rejects_unbounded_or_multiline_reason(tmp_path, reason):
    run_dir, _, _ = _authority_run(tmp_path)
    before = _journal_path(run_dir).read_bytes()

    with pytest.raises(run_redaction.RedactionError, match="reason"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=reason,
            operator_confirmed=True,
        )

    assert _journal_path(run_dir).read_bytes() == before


@pytest.mark.parametrize("reason", [SECRET, "planning"])
def test_redaction_rejects_reason_that_copies_affected_private_value(tmp_path, reason):
    run_dir, _, _ = _authority_run(tmp_path)
    before = _journal_path(run_dir).read_bytes()

    with pytest.raises(run_redaction.RedactionError, match="reason"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=reason,
            operator_confirmed=True,
        )

    assert _journal_path(run_dir).read_bytes() == before


def test_redaction_retry_is_idempotent(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    first = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    active = _journal_path(run_dir).read_bytes()

    replay = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )

    assert replay.operation_id == first.operation_id
    assert replay.quarantine_path == first.quarantine_path
    assert replay.record_path == first.record_path
    assert _journal_path(run_dir).read_bytes() == active
    assert len(list((run_dir / "events" / "redactions").glob("*/original.jsonl"))) == 1


def test_redaction_retry_refuses_tampered_quarantine(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    first = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    first.quarantine_path.write_bytes(b"tampered")

    with pytest.raises(run_redaction.RedactionError, match="quarantine verification"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )


def test_redaction_retry_refuses_symlinked_record(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    first = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    outside = tmp_path / "outside-record.json"
    outside.write_text("{}")
    first.record_path.unlink()
    first.record_path.symlink_to(outside)

    with pytest.raises(run_redaction.RedactionError, match="redaction record"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )
    assert outside.read_text() == "{}"


def test_redaction_replace_failure_retains_original_and_durable_quarantine(tmp_path, monkeypatch):
    run_dir, _, _ = _authority_run(tmp_path)
    before = _journal_path(run_dir).read_bytes()

    def fail_replace(*args, **kwargs):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(run_redaction, "_replace_relative", fail_replace)

    with pytest.raises(run_redaction.RedactionError, match="replace"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )

    assert _journal_path(run_dir).read_bytes() == before
    quarantines = list((run_dir / "events" / "redactions").glob("*/original.jsonl"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == before


def test_redaction_retry_completes_after_record_crash_window(tmp_path, monkeypatch):
    run_dir, _, _ = _authority_run(tmp_path)
    real_write_record = run_redaction._write_redaction_record
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated record failure")
        return real_write_record(*args, **kwargs)

    monkeypatch.setattr(run_redaction, "_write_redaction_record", fail_once)

    with pytest.raises(run_redaction.RedactionError, match="record"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )
    assert SECRET.encode() not in _journal_path(run_dir).read_bytes()
    assert list((run_dir / "events" / "redactions").glob("*/original.jsonl"))

    replay = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    assert replay.record_path.is_file()
    assert SECRET.encode() not in replay.record_path.read_bytes()


def test_different_request_refuses_incomplete_recordless_operation(tmp_path, monkeypatch):
    run_dir, _, _ = _authority_run(tmp_path)
    real_write_record = run_redaction._write_redaction_record

    def fail_record(*args, **kwargs):
        raise OSError("simulated record failure")

    monkeypatch.setattr(run_redaction, "_write_redaction_record", fail_record)
    with pytest.raises(run_redaction.RedactionError, match="record"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )

    first_operation = run_redaction._operation_id(RUN_ID, 2, 2, REASON_CODE)
    first_dir, first_quarantine, first_record = run_redaction._operation_paths(run_dir, first_operation)
    assert first_quarantine.is_file()
    assert not first_record.exists()
    assert json.loads((first_dir / "state.json").read_text())["phase"] == "replaced"

    monkeypatch.setattr(run_redaction, "_write_redaction_record", real_write_record)
    with pytest.raises(run_redaction.RedactionError, match="incomplete redaction transaction"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=4,
            sequence_end=4,
            reason="personal-data-exposure",
            operator_confirmed=True,
        )

    quarantines = list((run_dir / "events" / "redactions").glob("*/original.jsonl"))
    assert quarantines == [first_quarantine]
    recovered = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    assert recovered.record_path == first_record
    assert recovered.record_path.is_file()


def test_redaction_retry_repairs_projection_after_replace_crash_window(tmp_path, monkeypatch):
    run_dir, _, _ = _authority_run(tmp_path)
    real_replace_projection = run_redaction._replace_projection
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise run_redaction.RedactionError("simulated projection replace failure")
        return real_replace_projection(*args, **kwargs)

    monkeypatch.setattr(run_redaction, "_replace_projection", fail_once)

    with pytest.raises(run_redaction.RedactionError, match="projection replace"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )
    assert SECRET.encode() not in _journal_path(run_dir).read_bytes()
    stale = json.loads((run_dir / "run.json").read_text())

    replay = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )

    repaired = json.loads((run_dir / "run.json").read_text())
    assert stale["journal_last_event_digest"] != repaired["journal_last_event_digest"]
    assert replay.record_path.is_file()


def test_cleanup_is_explicit_and_gated_by_post_rewrite_verification(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    report = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    assert report.quarantine_path.is_file()

    active = _journal_path(run_dir)
    active.write_bytes(active.read_bytes() + b'{"partial":')
    with pytest.raises(run_redaction.RedactionError, match="verification"):
        run_redaction.cleanup_redaction_quarantine(
            run_dir,
            operation_id=report.operation_id,
            operator_confirmed=True,
        )
    assert report.quarantine_path.is_file()


def test_cleanup_removes_only_verified_quarantine(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    report = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )

    cleaned = run_redaction.cleanup_redaction_quarantine(
        run_dir,
        operation_id=report.operation_id,
        operator_confirmed=True,
    )

    assert cleaned.quarantine_path == report.quarantine_path
    assert not report.quarantine_path.exists()
    assert report.record_path.is_file()
    assert SECRET.encode() not in _journal_path(run_dir).read_bytes()
    assert SECRET.encode() not in report.record_path.read_bytes()


def test_cleanup_rebases_shadow_history_without_pre_redaction_digest_oracles(tmp_path):
    run_dir, snapshot, events = _authority_run(tmp_path)
    prior_projection = run_projector.project_run_snapshot(snapshot, events, journal_present=True)
    prior_projection_digest = run_redaction._digest(prior_projection.to_bytes())
    prior_tail = events[-1]
    run_shadow._record_match(
        run_dir,
        RUN_ID,
        prior_tail.sequence,
        prior_tail.event_digest,
        prior_projection_digest,
        prior_projection_digest,
    )
    prior_shadow = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    forbidden_digests = {
        prior_shadow["last_compared_event_digest"],
        prior_shadow["last_shadow_digest"],
        prior_shadow["last_projected_digest"],
    }
    artifact_path = run_shadow.shadow_artifact_path(run_dir)
    corrupt_sibling = artifact_path.with_name(f"{artifact_path.name}.corrupt-20260730T190000000000Z")
    stale_sibling = artifact_path.with_name(".stale-projector-v2-20260730T190000000000Z")
    corrupt_sibling.write_text(json.dumps(prior_shadow))
    stale_sibling.write_text(json.dumps(prior_shadow))

    report = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    run_redaction.cleanup_redaction_quarantine(
        run_dir,
        operation_id=report.operation_id,
        operator_confirmed=True,
    )

    refreshed = json.loads(run_shadow.shadow_artifact_path(run_dir).read_text())
    encoded = json.dumps(refreshed, sort_keys=True)
    assert refreshed["comparisons"] == 1
    assert refreshed["matches"] == 1
    assert len(refreshed["recent_records"]) == 1
    assert not corrupt_sibling.exists()
    assert not stale_sibling.exists()
    for digest in forbidden_digests:
        assert digest not in encoded


def test_cleanup_retry_reverifies_retained_quarantine_after_authorization_crash(tmp_path, monkeypatch):
    run_dir, _, _ = _authority_run(tmp_path)
    report = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    real_unlink = run_redaction.os.unlink

    def fail_unlink(path, *args, **kwargs):
        if Path(path).name == "original.jsonl":
            raise OSError("simulated cleanup failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(run_redaction.os, "unlink", fail_unlink)
    with pytest.raises(run_redaction.RedactionError, match="quarantine cleanup"):
        run_redaction.cleanup_redaction_quarantine(
            run_dir,
            operation_id=report.operation_id,
            operator_confirmed=True,
        )

    report.quarantine_path.write_bytes(b"tampered")
    monkeypatch.setattr(run_redaction.os, "unlink", real_unlink)
    with pytest.raises(run_redaction.RedactionError, match="cleanup verification"):
        run_redaction.cleanup_redaction_quarantine(
            run_dir,
            operation_id=report.operation_id,
            operator_confirmed=True,
        )
    assert report.quarantine_path.is_file()


def test_redaction_refuses_symlinked_journal(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    journal = _journal_path(run_dir)
    target = tmp_path / "outside.jsonl"
    target.write_bytes(journal.read_bytes())
    journal.unlink()
    journal.symlink_to(target)

    with pytest.raises(run_redaction.RedactionError, match="journal"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )

    assert SECRET.encode() in target.read_bytes()
    assert not (run_dir / "events" / "redactions").exists()


def test_redacted_payloads_remain_valid_for_projection_sensitive_status_fields(tmp_path):
    run_dir, before_projection, _ = _authority_run(tmp_path)

    run_redaction.redact_journal(
        run_dir,
        sequence_start=1,
        sequence_end=4,
        reason="personal-data-exposure",
        operator_confirmed=True,
    )

    report = run_journal.read_journal_bounded(_journal_path(run_dir))
    assert report.chain_errors == []
    assert report.events[0].payload == {"status": "started"}
    assert report.events[1].payload == {"detail": "[REDACTED]"}
    assert report.events[2].event_type == run_checkpoint.CHECKPOINT_EVENT_TYPE
    assert report.events[3].payload == {"status": "ok", "detail": "[REDACTED]"}
    current = json.loads((run_dir / "run.json").read_text())
    assert current["status"] == before_projection["status"] == "ok"
    assert current["journal_last_sequence"] == 5
    for event in report.events:
        assert run_events.validate_event(event.to_dict()) == []


def test_two_operator_processes_cannot_publish_concurrent_rewrites(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    marker = tmp_path / "first-operator-inside-transaction"
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = os.pathsep.join(
        filter(None, (str(Path(__file__).parents[1] / "src"), child_env.get("PYTHONPATH")))
    )
    script = textwrap.dedent(
        """
        import sys
        import time
        from pathlib import Path
        from brigade import run_redaction

        run_dir = Path(sys.argv[1])
        marker = Path(sys.argv[2])
        reason = sys.argv[3]
        should_pause = sys.argv[4] == "pause"
        original = run_redaction._rewrite_events

        def paused_rewrite(*args, **kwargs):
            marker.write_text("inside")
            time.sleep(1.0)
            return original(*args, **kwargs)

        if should_pause:
            run_redaction._rewrite_events = paused_rewrite
        try:
            run_redaction.redact_journal(
                run_dir,
                sequence_start=2,
                sequence_end=2,
                reason=reason,
                operator_confirmed=True,
            )
        except run_redaction.RedactionError as exc:
            print(exc.diagnostic)
            raise SystemExit(2)
        print("ok")
        """
    )
    first = subprocess.Popen(
        [sys.executable, "-c", script, str(run_dir), str(marker), REASON_CODE, "pause"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env,
    )
    for _ in range(100):
        if marker.exists():
            break
        time.sleep(0.02)
    if not marker.exists():
        first_stdout, first_stderr = first.communicate(timeout=10)
        pytest.fail(f"first operator never entered transaction\nstdout:\n{first_stdout}\nstderr:\n{first_stderr}")

    second = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(run_dir),
            str(marker),
            "personal-data-exposure",
            "no-pause",
        ],
        text=True,
        capture_output=True,
        check=False,
        env=child_env,
    )
    first_stdout, first_stderr = first.communicate(timeout=10)

    assert first.returncode == 0, first_stderr
    assert first_stdout.strip() == "ok"
    assert second.returncode == 2
    assert "run lock state is live" in second.stdout
    assert run_journal.read_journal_bounded(_journal_path(run_dir)).chain_errors == []


@pytest.mark.parametrize("symlink_level", ["redactions", "operation"])
def test_redaction_rejects_symlinked_transaction_parent_without_external_write(tmp_path, symlink_level):
    run_dir, _, _ = _authority_run(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    redactions = run_dir / "events" / "redactions"
    if symlink_level == "redactions":
        redactions.symlink_to(external, target_is_directory=True)
    else:
        redactions.mkdir(mode=0o700)
        operation_id = run_redaction._operation_id(RUN_ID, 2, 2, REASON_CODE)
        (redactions / operation_id).symlink_to(external, target_is_directory=True)

    with pytest.raises(run_redaction.RedactionError, match="path|symlink"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )

    assert list(external.iterdir()) == []


def test_redaction_rejects_raced_operation_parent_without_external_write(tmp_path, monkeypatch):
    run_dir, _, _ = _authority_run(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    original_publish = run_redaction._publish_quarantine

    def race_parent(*args, **kwargs):
        operation_dir = args[0]
        operation_dir.rmdir()
        operation_dir.symlink_to(external, target_is_directory=True)
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(run_redaction, "_publish_quarantine", race_parent)
    with pytest.raises(run_redaction.RedactionError, match="path|symlink|quarantine"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )

    assert list(external.iterdir()) == []


def test_redaction_fsyncs_created_parents_before_journal_replace(tmp_path, monkeypatch):
    run_dir, _, _ = _authority_run(tmp_path)
    order: list[str] = []
    original_fsync = run_redaction._fsync_directory_handle
    original_replace = run_redaction._replace_journal

    def record_fsync(path, fd, *, category):
        order.append(f"fsync:{Path(path).name}")
        return original_fsync(path, fd, category=category)

    def record_replace(*args, **kwargs):
        order.append("replace:journal")
        return original_replace(*args, **kwargs)

    monkeypatch.setattr(run_redaction, "_fsync_directory_handle", record_fsync)
    monkeypatch.setattr(run_redaction, "_replace_journal", record_replace)

    run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )

    replace_index = order.index("replace:journal")
    assert order.index("fsync:events") < replace_index
    assert order.index("fsync:redactions") < replace_index


def test_redaction_retries_after_partial_quarantine_write(tmp_path, monkeypatch):
    run_dir, _, _ = _authority_run(tmp_path)
    original_write_all = run_redaction._write_all

    def partial_write(fd, data, *, category):
        if category == "redaction quarantine":
            os.write(fd, data[: max(1, len(data) // 2)])
            raise run_redaction.RedactionError("redaction quarantine write failed")
        return original_write_all(fd, data, category=category)

    monkeypatch.setattr(run_redaction, "_write_all", partial_write)
    with pytest.raises(run_redaction.RedactionError, match="quarantine"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )
    monkeypatch.setattr(run_redaction, "_write_all", original_write_all)

    report = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    assert report.quarantine_path.is_file()
    assert not list(report.quarantine_path.parent.glob(".original.jsonl.*.tmp"))


def test_redaction_refsyncs_exact_quarantine_after_file_fsync_failure(tmp_path, monkeypatch):
    run_dir, _, _ = _authority_run(tmp_path)
    original_fsync = run_redaction._fsync_file
    failed = False

    def fail_first_fsync(fd, *, category):
        nonlocal failed
        if category == "redaction quarantine" and not failed:
            failed = True
            raise run_redaction.RedactionError("simulated quarantine fsync failure")
        return original_fsync(fd, category=category)

    monkeypatch.setattr(run_redaction, "_fsync_file", fail_first_fsync)
    with pytest.raises(run_redaction.RedactionError, match="quarantine"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )

    operation_id = run_redaction._operation_id(RUN_ID, 2, 2, REASON_CODE)
    operation_dir, quarantine, _ = run_redaction._operation_paths(run_dir, operation_id)
    assert operation_dir.is_dir()
    quarantine.write_bytes(_journal_path(run_dir).read_bytes())
    quarantine.chmod(0o644)
    quarantine_inode = quarantine.stat().st_ino
    resynced = False

    def track_fsync(fd, *, category):
        nonlocal resynced
        if os.fstat(fd).st_ino == quarantine_inode:
            resynced = True
        return original_fsync(fd, category=category)

    monkeypatch.setattr(run_redaction, "_fsync_file", track_fsync)
    run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    assert resynced is True


def test_redaction_refsyncs_exact_quarantine_parent_before_state(tmp_path, monkeypatch):
    run_dir, _, _ = _authority_run(tmp_path)
    original_fsync_dir = run_redaction._fsync_directory_handle
    failed = False

    def fail_quarantine_parent(path, fd, *, category):
        nonlocal failed
        path = Path(path)
        if path.name.startswith("redact-") and (path / "original.jsonl").exists() and not failed:
            failed = True
            raise run_redaction.RedactionError("simulated quarantine directory fsync failure")
        return original_fsync_dir(path, fd, category=category)

    monkeypatch.setattr(run_redaction, "_fsync_directory_handle", fail_quarantine_parent)
    with pytest.raises(run_redaction.RedactionError, match="quarantine"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )

    parent_resynced = False
    original_write_state = run_redaction._write_state

    def track_fsync_dir(path, fd, *, category):
        nonlocal parent_resynced
        path = Path(path)
        if path.name.startswith("redact-"):
            parent_resynced = True
        return original_fsync_dir(path, fd, category=category)

    def require_resync_before_state(*args, **kwargs):
        assert parent_resynced is True
        return original_write_state(*args, **kwargs)

    monkeypatch.setattr(run_redaction, "_fsync_directory_handle", track_fsync_dir)
    monkeypatch.setattr(run_redaction, "_write_state", require_resync_before_state)
    run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )


@pytest.mark.parametrize("failure_name", ["publish"])
def test_redaction_retries_after_quarantine_publish_failure(tmp_path, monkeypatch, failure_name):
    run_dir, _, _ = _authority_run(tmp_path)
    original_publish = run_redaction._publish_no_replace
    failed = False

    def fail_once(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("simulated quarantine publish failure")
        return original_publish(*args, **kwargs)

    monkeypatch.setattr(run_redaction, "_publish_no_replace", fail_once)
    with pytest.raises(run_redaction.RedactionError, match="quarantine"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )
    monkeypatch.setattr(run_redaction, "_publish_no_replace", original_publish)

    report = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    assert report.quarantine_path.is_file()


@pytest.mark.parametrize("checkpoint_fault", ["missing", "tampered", "hardlinked", "oversize"])
def test_redaction_refuses_invalid_checkpoint_artifact_before_quarantine(tmp_path, checkpoint_fault):
    run_dir, _, _ = _authority_run(tmp_path)
    checkpoint = _latest_checkpoint_path(run_dir)
    if checkpoint_fault == "missing":
        checkpoint.unlink()
    elif checkpoint_fault == "tampered":
        checkpoint.write_bytes(b"{}")
    elif checkpoint_fault == "hardlinked":
        os.link(checkpoint, tmp_path / "checkpoint-copy")
    else:
        checkpoint.write_bytes(b"x" * (run_checkpoint.MAX_CHECKPOINT_BYTES + 1))

    with pytest.raises(run_redaction.RedactionError, match="checkpoint"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )
    assert not (run_dir / "events" / "redactions").exists()


def test_redaction_refuses_uncovered_checkpoint_tail_before_quarantine(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    _append_uncovered_terminal_event(run_dir)

    with pytest.raises(run_redaction.RedactionError, match="checkpoint"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )
    assert not (run_dir / "events" / "redactions").exists()


def test_cleanup_refuses_missing_checkpoint_and_preserves_quarantine(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    report = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    _latest_checkpoint_path(run_dir).unlink()

    with pytest.raises(run_redaction.RedactionError, match="checkpoint"):
        run_redaction.cleanup_redaction_quarantine(
            run_dir,
            operation_id=report.operation_id,
            operator_confirmed=True,
        )
    assert report.quarantine_path.is_file()


def test_redaction_rejects_generated_idempotency_collision(tmp_path):
    operation_id = run_redaction._operation_id(RUN_ID, 2, 2, REASON_CODE)
    collision = f"redaction:{operation_id}:2"
    run_dir, _, _ = _authority_run(tmp_path, first_idempotency_key=collision)

    with pytest.raises(run_redaction.RedactionError, match="idempotency"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )


@pytest.mark.parametrize("status", ["started", "planning", "dispatching", "paused", "unknown"])
def test_redaction_refuses_nonterminal_or_ambiguous_projection_status(tmp_path, status):
    run_dir, _, _ = _authority_run(tmp_path)
    snapshot = json.loads((run_dir / "run.json").read_text())
    snapshot["status"] = status
    (run_dir / "run.json").write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")

    with pytest.raises(run_redaction.RedactionError, match="terminal"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )
    assert not (run_dir / "events" / "redactions").exists()


def test_overlapping_redactions_are_deterministic_and_idempotent(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    first = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    second = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason="policy-removal",
        operator_confirmed=True,
    )
    active = _journal_path(run_dir).read_bytes()

    replay = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason="policy-removal",
        operator_confirmed=True,
    )
    assert first.operation_id != second.operation_id
    assert replay.operation_id == second.operation_id
    assert _journal_path(run_dir).read_bytes() == active


def test_sequential_redaction_lineage_allows_each_quarantine_cleanup(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    first = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    second = run_redaction.redact_journal(
        run_dir,
        sequence_start=4,
        sequence_end=4,
        reason="personal-data-exposure",
        operator_confirmed=True,
    )

    first_cleaned = run_redaction.cleanup_redaction_quarantine(
        run_dir,
        operation_id=first.operation_id,
        operator_confirmed=True,
    )
    second_cleaned = run_redaction.cleanup_redaction_quarantine(
        run_dir,
        operation_id=second.operation_id,
        operator_confirmed=True,
    )
    assert first_cleaned.cleaned is True
    assert second_cleaned.cleaned is True
    assert not first.quarantine_path.exists()
    assert not second.quarantine_path.exists()


def test_sequential_cleanup_removes_prior_rewritten_digest_alias_oracle(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    first = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    first_state_path = first.record_path.parent / "state.json"
    first_state = json.loads(first_state_path.read_text())
    first_rewritten_digest = first_state["rewritten_sha256"]
    first_post_anchor_digest = hashlib.sha256(_journal_path(run_dir).read_bytes()).hexdigest()
    assert first_rewritten_digest in first.record_path.read_text()

    second = run_redaction.redact_journal(
        run_dir,
        sequence_start=4,
        sequence_end=4,
        reason="personal-data-exposure",
        operator_confirmed=True,
    )
    second_state = json.loads((second.record_path.parent / "state.json").read_text())
    assert second_state["original_sha256"] == first_post_anchor_digest
    assert second_state["original_sha256"] != first_rewritten_digest
    parent_anchor = next(
        event
        for event in run_journal.read_journal_bounded(_journal_path(run_dir)).events
        if event.event_type == "run.redaction.recorded" and event.payload["operation_id"] == first.operation_id
    )
    assert json.loads(second.record_path.read_text())["parent_record_sha256"] == parent_anchor.payload["record_sha256"]

    run_redaction.cleanup_redaction_quarantine(
        run_dir,
        operation_id=second.operation_id,
        operator_confirmed=True,
    )

    for path in sorted((run_dir / "events" / "redactions").rglob("*")):
        if path.is_file():
            assert first_rewritten_digest.encode() not in path.read_bytes()

    first_cleaned = run_redaction.cleanup_redaction_quarantine(
        run_dir,
        operation_id=first.operation_id,
        operator_confirmed=True,
    )
    assert first_cleaned.cleaned is True


def test_cleanup_retry_converges_parent_record_state_retirement_split(tmp_path, monkeypatch):
    run_dir, _, _ = _authority_run(tmp_path)
    first = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    second = run_redaction.redact_journal(
        run_dir,
        sequence_start=4,
        sequence_end=4,
        reason="personal-data-exposure",
        operator_confirmed=True,
    )
    first_state_path = first.record_path.parent / "state.json"
    first_digest = json.loads(first_state_path.read_text())["rewritten_sha256"]
    real_write_state = run_redaction._write_state
    failed = False

    def fail_parent_state(*args, **kwargs):
        nonlocal failed
        if (
            kwargs.get("operation_id") == first.operation_id
            and kwargs.get("rewritten_digest_retired_by") == second.operation_id
            and not failed
        ):
            failed = True
            raise run_redaction.RedactionError("simulated parent retirement state failure")
        return real_write_state(*args, **kwargs)

    monkeypatch.setattr(run_redaction, "_write_state", fail_parent_state)
    with pytest.raises(run_redaction.RedactionError, match="parent retirement state"):
        run_redaction.cleanup_redaction_quarantine(
            run_dir,
            operation_id=second.operation_id,
            operator_confirmed=True,
        )

    assert not second.quarantine_path.exists()
    assert json.loads((second.record_path.parent / "state.json").read_text())["phase"] == "cleanup-authorized"
    assert json.loads(first.record_path.read_text())["rewritten_digest_retired_by"] == second.operation_id
    assert json.loads(first_state_path.read_text())["rewritten_sha256"] == first_digest

    monkeypatch.setattr(run_redaction, "_write_state", real_write_state)
    cleaned = run_redaction.cleanup_redaction_quarantine(
        run_dir,
        operation_id=second.operation_id,
        operator_confirmed=True,
    )
    assert cleaned.cleaned is True
    assert first_digest.encode() not in first.record_path.read_bytes()
    assert first_digest.encode() not in first_state_path.read_bytes()


def test_cleanup_retry_after_parent_retirement_before_child_realign_crash(tmp_path, monkeypatch):
    """Crash between parent retirement write and child realign must stay recoverable."""
    run_dir, _, _ = _authority_run(tmp_path)
    first = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    second = run_redaction.redact_journal(
        run_dir,
        sequence_start=4,
        sequence_end=4,
        reason="personal-data-exposure",
        operator_confirmed=True,
    )
    preretirement_parent_digest = json.loads(second.record_path.read_text())["parent_record_sha256"]
    real_realign = run_redaction._realign_child_parent_record_digest
    failed = False

    def fail_after_parent_record_write(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise run_redaction.RedactionError("simulated child parent-digest realign failure")
        return real_realign(*args, **kwargs)

    monkeypatch.setattr(run_redaction, "_realign_child_parent_record_digest", fail_after_parent_record_write)
    with pytest.raises(run_redaction.RedactionError, match="parent-digest realign"):
        run_redaction.cleanup_redaction_quarantine(
            run_dir,
            operation_id=second.operation_id,
            operator_confirmed=True,
        )

    assert not second.quarantine_path.exists()
    assert json.loads((second.record_path.parent / "state.json").read_text())["phase"] == "cleanup-authorized"
    assert json.loads(first.record_path.read_text())["rewritten_digest_retired_by"] == second.operation_id
    assert json.loads(second.record_path.read_text())["parent_record_sha256"] == preretirement_parent_digest
    current_parent_digest = run_redaction._redaction_record_sha256(first.record_path)
    assert current_parent_digest != preretirement_parent_digest
    first_state = json.loads((first.record_path.parent / "state.json").read_text())
    assert "rewritten_sha256" in first_state
    assert first_state.get("rewritten_digest_retired_by") is None

    # Retry inventory load must accept the mid-flight split (pre-retirement child
    # parent_record_sha256) instead of permanently fail-closing the lineage.
    records, states, record_digests = run_redaction._load_operation_inventory(
        run_dir / "events" / "redactions",
        run_dir.name,
        resumable_operation_id=second.operation_id,
    )
    assert first.operation_id in records
    assert second.operation_id in records
    assert records[second.operation_id]["parent_record_sha256"] == preretirement_parent_digest
    assert record_digests[first.operation_id] == current_parent_digest
    assert states[second.operation_id]["phase"] == "cleanup-authorized"

    monkeypatch.setattr(run_redaction, "_realign_child_parent_record_digest", real_realign)
    cleaned = run_redaction.cleanup_redaction_quarantine(
        run_dir,
        operation_id=second.operation_id,
        operator_confirmed=True,
    )
    assert cleaned.cleaned is True
    assert json.loads(second.record_path.read_text())["parent_record_sha256"] == run_redaction._redaction_record_sha256(
        first.record_path
    )


def test_cleanup_retry_finishes_after_child_record_before_cleaned_state(tmp_path, monkeypatch):
    run_dir, _, _ = _authority_run(tmp_path)
    first = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    second = run_redaction.redact_journal(
        run_dir,
        sequence_start=4,
        sequence_end=4,
        reason="personal-data-exposure",
        operator_confirmed=True,
    )
    second_state_path = second.record_path.parent / "state.json"
    second_original_digest = json.loads(second_state_path.read_text())["original_sha256"]
    real_write_state = run_redaction._write_state
    failed = False

    def fail_child_cleaned_state(*args, **kwargs):
        nonlocal failed
        if kwargs.get("operation_id") == second.operation_id and kwargs.get("phase") == "cleaned" and not failed:
            failed = True
            raise run_redaction.RedactionError("simulated child cleaned state failure")
        return real_write_state(*args, **kwargs)

    monkeypatch.setattr(run_redaction, "_write_state", fail_child_cleaned_state)
    with pytest.raises(run_redaction.RedactionError, match="child cleaned state"):
        run_redaction.cleanup_redaction_quarantine(
            run_dir,
            operation_id=second.operation_id,
            operator_confirmed=True,
        )

    assert not second.quarantine_path.exists()
    assert json.loads(second_state_path.read_text())["phase"] == "cleanup-authorized"
    assert json.loads(second.record_path.read_text())["quarantine_retained"] is False
    assert json.loads(first.record_path.read_text())["rewritten_digest_retired_by"] == second.operation_id
    stale_parent_state = first.record_path.parent / f".state.json.{'a' * 32}.tmp"
    stale_parent_record = first.record_path.parent / f".record.json.{'b' * 32}.tmp"
    stale_parent_state.write_text(second_original_digest)
    stale_parent_record.write_text(second_original_digest)

    monkeypatch.setattr(run_redaction, "_write_state", real_write_state)
    cleaned = run_redaction.cleanup_redaction_quarantine(
        run_dir,
        operation_id=second.operation_id,
        operator_confirmed=True,
    )
    assert cleaned.cleaned is True
    assert not stale_parent_state.exists()
    assert not stale_parent_record.exists()
    for path in sorted((run_dir / "events" / "redactions").rglob("*")):
        if path.is_file():
            assert second_original_digest.encode() not in path.read_bytes()


def test_redaction_after_cleaned_parent_uses_active_anchor_reference(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    first = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    run_redaction.cleanup_redaction_quarantine(
        run_dir,
        operation_id=first.operation_id,
        operator_confirmed=True,
    )
    parent_anchor = next(
        event
        for event in run_journal.read_journal_bounded(_journal_path(run_dir)).events
        if event.event_type == "run.redaction.recorded" and event.payload["operation_id"] == first.operation_id
    )

    second = run_redaction.redact_journal(
        run_dir,
        sequence_start=4,
        sequence_end=4,
        reason="personal-data-exposure",
        operator_confirmed=True,
    )

    second_record = json.loads(second.record_path.read_text())
    assert second_record["parent_operation_id"] == first.operation_id
    assert second_record["parent_record_sha256"] == parent_anchor.payload["record_sha256"]


def test_second_cleanup_verify_pass_performs_zero_anchor_refresh_writes(tmp_path, monkeypatch):
    run_dir, _, _ = _authority_run(tmp_path)
    report = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    cleaned = run_redaction.cleanup_redaction_quarantine(
        run_dir,
        operation_id=report.operation_id,
        operator_confirmed=True,
    )
    assert cleaned.cleaned is True

    writes: list[str] = []

    def track_journal(path, data):
        writes.append("journal")
        raise AssertionError("second verify pass must not rewrite the journal")

    def track_projection(run_dir_arg, projection):
        writes.append("projection")
        raise AssertionError("second verify pass must not rewrite the projection")

    monkeypatch.setattr(run_redaction, "_replace_journal", track_journal)
    monkeypatch.setattr(run_redaction, "_replace_projection", track_projection)

    again = run_redaction.cleanup_redaction_quarantine(
        run_dir,
        operation_id=report.operation_id,
        operator_confirmed=True,
    )
    assert again.cleaned is True
    assert writes == []


def test_refresh_chained_anchors_asserts_owner_on_noop_early_return(tmp_path, monkeypatch):
    """No-op anchor refresh must still call _assert_active_owner before returning."""
    run_dir, _, _ = _authority_run(tmp_path)
    workspace = tmp_path / "workspace"
    report = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    cleaned = run_redaction.cleanup_redaction_quarantine(
        run_dir,
        operation_id=report.operation_id,
        operator_confirmed=True,
    )
    assert cleaned.cleaned is True

    owner_calls: list[tuple[Path, Path]] = []

    def track_owner(workspace_arg, run_dir_arg):
        owner_calls.append((workspace_arg, run_dir_arg))

    journal_writes = 0
    projection_writes = 0

    def track_journal(path, data):
        nonlocal journal_writes
        journal_writes += 1
        raise AssertionError("no-op refresh must not rewrite the journal")

    def track_projection(run_dir_arg, projection):
        nonlocal projection_writes
        projection_writes += 1
        raise AssertionError("no-op refresh must not rewrite the projection")

    monkeypatch.setattr(run_redaction, "_assert_active_owner", track_owner)
    monkeypatch.setattr(run_redaction, "_replace_journal", track_journal)
    monkeypatch.setattr(run_redaction, "_replace_projection", track_projection)

    digest = run_redaction._refresh_chained_anchors(
        run_dir,
        workspace=workspace,
        resumable_operation_id=report.operation_id,
    )
    assert isinstance(digest, str) and len(digest) == 64
    assert owner_calls == [(workspace, run_dir)]
    assert journal_writes == 0
    assert projection_writes == 0


def test_tampered_split_retirement_record_fails_digest_cross_check(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    first = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    second = run_redaction.redact_journal(
        run_dir,
        sequence_start=4,
        sequence_end=4,
        reason="personal-data-exposure",
        operator_confirmed=True,
    )
    run_redaction.cleanup_redaction_quarantine(
        run_dir,
        operation_id=second.operation_id,
        operator_confirmed=True,
    )
    assert json.loads(first.record_path.read_text())["rewritten_digest_retired_by"] == second.operation_id

    second_record = json.loads(second.record_path.read_text())
    second_record["parent_record_sha256"] = "0" * 64
    second.record_path.write_text(json.dumps(second_record, indent=2, sort_keys=True) + "\n")

    with pytest.raises(run_redaction.RedactionError, match="digest mismatch") as excinfo:
        run_redaction.cleanup_redaction_quarantine(
            run_dir,
            operation_id=first.operation_id,
            operator_confirmed=True,
        )
    assert len(excinfo.value.diagnostic) <= run_events.MAX_DIAGNOSTIC_LEN


def test_redaction_inventory_rejects_tampered_parent_record_anchor_reference(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    second = run_redaction.redact_journal(
        run_dir,
        sequence_start=4,
        sequence_end=4,
        reason="personal-data-exposure",
        operator_confirmed=True,
    )
    second_record = json.loads(second.record_path.read_text())
    second_record["parent_record_sha256"] = "0" * 64
    second.record_path.write_text(json.dumps(second_record, indent=2, sort_keys=True) + "\n")

    with pytest.raises(run_redaction.RedactionError, match="parent reference"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=3,
            sequence_end=3,
            reason="other-sensitive-data",
            operator_confirmed=True,
        )


def test_redaction_inventory_rejects_record_without_state(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    first = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    (first.record_path.parent / "state.json").unlink()

    with pytest.raises(run_redaction.RedactionError, match="state/record mismatch"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=4,
            sequence_end=4,
            reason="personal-data-exposure",
            operator_confirmed=True,
        )
    assert len(list((run_dir / "events" / "redactions").glob("*/original.jsonl"))) == 1


def test_redaction_inventory_rejects_forged_multiple_tip_graph(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    first = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    second = run_redaction.redact_journal(
        run_dir,
        sequence_start=4,
        sequence_end=4,
        reason="personal-data-exposure",
        operator_confirmed=True,
    )
    second_state_path = second.record_path.parent / "state.json"
    second_state = json.loads(second_state_path.read_text())
    second_record = json.loads(second.record_path.read_text())
    second_state["parent_operation_id"] = None
    second_record["parent_operation_id"] = None
    second_record.pop("parent_record_sha256")
    second_state_path.write_text(json.dumps(second_state))
    second.record_path.write_text(json.dumps(second_record))

    with pytest.raises(run_redaction.RedactionError, match="multiple tips"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=3,
            sequence_end=3,
            reason="other-sensitive-data",
            operator_confirmed=True,
        )
    assert first.quarantine_path.exists()
    assert second.quarantine_path.exists()


@pytest.mark.parametrize("artifact", ["journal", "quarantine", "state"])
def test_redaction_refuses_hardlinked_sensitive_artifact(tmp_path, artifact):
    run_dir, _, _ = _authority_run(tmp_path)
    if artifact == "journal":
        os.link(_journal_path(run_dir), tmp_path / "journal-copy")
        with pytest.raises(run_redaction.RedactionError, match="link"):
            run_redaction.redact_journal(
                run_dir,
                sequence_start=2,
                sequence_end=2,
                reason=REASON_CODE,
                operator_confirmed=True,
            )
        return

    report = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    target = report.quarantine_path if artifact == "quarantine" else report.record_path.parent / "state.json"
    os.link(target, tmp_path / f"{artifact}-copy")
    with pytest.raises(run_redaction.RedactionError, match="link"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )


def test_redaction_normalizes_private_file_modes(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    journal = _journal_path(run_dir)
    journal.chmod(0o644)

    report = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    assert stat.S_IMODE(report.quarantine_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((report.record_path.parent / "state.json").stat().st_mode) == 0o600
    assert stat.S_IMODE(report.record_path.stat().st_mode) == 0o600

    report.quarantine_path.chmod(0o644)
    run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    assert stat.S_IMODE(report.quarantine_path.stat().st_mode) == 0o600


@pytest.mark.parametrize(
    "reason",
    [
        "credential exposure",
        "Credential-Exposure",
        "secret",
        "credential-exposure-extra",
        "x" * 241,
    ],
)
def test_redaction_accepts_only_closed_reason_codes(tmp_path, reason):
    run_dir, _, _ = _authority_run(tmp_path)

    with pytest.raises(run_redaction.RedactionError, match="reason code"):
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=reason,
            operator_confirmed=True,
        )


def test_cleanup_removes_original_digest_oracle_from_durable_metadata(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    report = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=2,
        reason=REASON_CODE,
        operator_confirmed=True,
    )
    operation_dir = report.record_path.parent
    stale_state = operation_dir / f".state.json.{'a' * 32}.tmp"
    stale_quarantine = operation_dir / f".original.jsonl.{'b' * 32}.tmp"
    state_before = json.loads((operation_dir / "state.json").read_text())
    stale_state.write_text(json.dumps({"original_sha256": state_before["original_sha256"]}))
    stale_quarantine.write_bytes(report.quarantine_path.read_bytes())
    run_redaction.cleanup_redaction_quarantine(
        run_dir,
        operation_id=report.operation_id,
        operator_confirmed=True,
    )

    state = json.loads((report.record_path.parent / "state.json").read_text())
    record = json.loads(report.record_path.read_text())
    assert "original_sha256" not in state
    assert "original_sha256" not in record
    assert "original_journal_sha256" not in state
    assert "original_journal_sha256" not in record
    assert not stale_state.exists()
    assert not stale_quarantine.exists()


# -- Issue #651 step 1: redaction rewrite vs concurrent external append ------

_EXTERNAL_APPEND_CHILD = r"""
import json
import os
import sys
import time
from pathlib import Path

from brigade import run_journal

journal_path = Path(sys.argv[1])
window_open = Path(sys.argv[2])
child_done = Path(sys.argv[3])
result_path = Path(sys.argv[4])
run_id = sys.argv[5]

deadline = time.monotonic() + 60.0
while not window_open.exists():
    if time.monotonic() > deadline:
        raise SystemExit("redaction replace window never opened")
    time.sleep(0.01)

result = {"appended": False}
tail = None
for attempt in range(8):
    report = run_journal.read_journal(journal_path)
    if report.chain_errors:
        result["error"] = "chain errors: " + "; ".join(report.chain_errors)
        break
    tail = report.events[-1].sequence if report.events else 0
    try:
        event = run_journal.append_event(
            journal_path,
            run_id=run_id,
            event_type="run.planning.started",
            payload={"detail": "external append during redaction"},
            idempotency_key="external-append-1",
            expected_previous_sequence=tail,
            recorded_at="2026-07-30T19:01:00.000000Z",
        )
        result = {"appended": True, "sequence": event.sequence, "attempts": attempt + 1}
        break
    except run_journal.StaleSequenceError:
        result["stale_retries"] = attempt + 1
        continue
    except run_journal.RunJournalError as exc:
        result["error"] = f"{type(exc).__name__}: {exc.diagnostic}"
        break

child_done.write_text("done")
result_path.write_text(json.dumps(result))
"""


@pytest.mark.skipif(
    os.name != "posix",
    reason="the cross-process journal mutation lock is an fcntl.flock sibling file",
)
def test_redaction_replace_serializes_against_external_journal_append(tmp_path, monkeypatch):
    """A redaction rewrite must not silently drop a concurrent external append.

    The redaction journal rewrite must hold the cross-process journal lock
    from its verified read through replacement. A child subprocess appends an
    external event exactly inside that read-to-replace window (signalled via
    filesystem barrier files). On the fixed locking behavior the child blocks
    on the flock until the rewrite finishes and then appends after it; without
    it, the child's committed append is overwritten by the rewrite and lost.
    """
    run_dir, _, _ = _authority_run(tmp_path)
    journal = _journal_path(run_dir)

    window_open = tmp_path / "redaction-window-open"
    child_done = tmp_path / "child-append-done"
    child_result = tmp_path / "child-result.json"

    child_script = tmp_path / "external_append_child.py"
    child_script.write_text(_EXTERNAL_APPEND_CHILD)
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    child = subprocess.Popen(
        [
            sys.executable,
            str(child_script),
            str(journal),
            str(window_open),
            str(child_done),
            str(child_result),
            RUN_ID,
        ],
        env=child_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    real_replace_journal = run_redaction._replace_journal

    def windowed_replace_journal(journal_path, rewritten):
        # The rewrite has been computed from the verified read and is about to
        # replace the journal: this is the read-to-replace window. Signal the
        # child and give it the opportunity to land its external append. On
        # fixed code the child is blocked on the journal flock, so the wait
        # times out and the rewrite proceeds; the child appends afterwards.
        window_open.write_text("open")
        deadline = time.monotonic() + 3.0
        while not child_done.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        real_replace_journal(journal_path, rewritten)

    monkeypatch.setattr(run_redaction, "_replace_journal", windowed_replace_journal)

    try:
        run_redaction.redact_journal(
            run_dir,
            sequence_start=2,
            sequence_end=2,
            reason=REASON_CODE,
            operator_confirmed=True,
        )
    finally:
        if not window_open.exists():
            window_open.write_text("open")
        stdout, stderr = child.communicate(timeout=30.0)
    assert child.returncode == 0, f"child exited {child.returncode}\nstdout:\n{stdout}\nstderr:\n{stderr}"

    outcome = json.loads(child_result.read_text())
    assert outcome.get("appended") is True, f"external child failed to append: {outcome}"

    verified = run_journal.read_journal_bounded(journal)
    assert verified.chain_errors == []
    assert verified.partial_tail is None
    committed_keys = [event.idempotency_key for event in verified.events]
    assert "external-append-1" in committed_keys, (
        "the external append committed during the redaction read-to-replace window "
        f"was lost; final journal keys: {committed_keys}"
    )
