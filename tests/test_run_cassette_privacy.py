"""Privacy boundary and retention regressions for opt-in private recording."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from brigade import (
    agents,
    cli,
    run_audit,
    run_cassette,
    run_checkpoint,
    run_events,
    run_journal,
    run_projector,
    run_redaction,
    runguard,
)
from brigade import run_cassette_store as store
from brigade import work_run_archive
from tests.test_run_cassette import _documents, _record
from tests.test_run_cassette import auto_recording as auto_recording
from tests.test_run_redaction import _authority_run
from tests.test_work_run_archive import RUN_ID, _seed_run


def _finish_recording(run_dir, snapshot):
    # Real dispatches are followed by the coordinator's final snapshot write.
    # Exercise that same checkpoint boundary after the cassette fact.
    events = run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl").events
    projected = run_projector.project_run_snapshot(snapshot, events, journal_present=True)
    with runguard.run_lock(run_dir.parent.parent.parent, run_dir=run_dir):
        run_checkpoint.write_checkpoint(
            run_dir,
            projected.to_bytes(),
            workspace=run_dir.parent.parent.parent,
            paired_event_type=None,
            body_kind="base-stripped",
        )
    events = run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl").events
    (run_dir / "run.json").write_bytes(
        run_projector.project_run_snapshot(snapshot, events, journal_present=True).to_bytes()
    )


def test_redaction_removes_cassettes_and_anchors_a_coherent_audit(tmp_path):
    run_dir, snapshot, _ = _authority_run(tmp_path)
    ref = _record(run_dir)
    _finish_recording(run_dir, snapshot)
    events = run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl").events
    (run_dir / "run.json").write_bytes(
        run_projector.project_run_snapshot(snapshot, events, journal_present=True).to_bytes()
    )
    report = run_redaction.redact_journal(
        run_dir,
        sequence_start=2,
        sequence_end=len(events),
        reason="credential-exposure",
        operator_confirmed=True,
    )
    assert report.operation_id
    assert not list((run_dir / "events" / "cassettes").iterdir())
    journal = run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl")
    assert not journal.chain_errors
    assert journal.events[-1].event_type == "run.redaction.recorded"
    redacted = next(event for event in journal.events if event.event_type == run_cassette.EVENT_TYPE)
    assert redacted.payload != ref
    assert redacted.payload["byte_size"] == 0
    assert all(value == "[REDACTED]" for key, value in redacted.payload.items() if key != "byte_size")
    assert run_audit.audit_run(run_dir).result == "match"


def test_export_strips_cassettes_and_archive_build_refuses_them(tmp_path):
    run_dir = _seed_run(tmp_path / "runs" / RUN_ID)
    run_path = run_dir / "run.json"
    terminal = run_path.read_bytes()
    run_meta = json.loads(terminal)
    run_meta.pop("finished_at")
    run_meta["status"] = "dispatching"
    run_path.write_text(json.dumps(run_meta))
    _record(run_dir, result=agents.AgentResult(text="adversarial-cassette-secret", ok=True))
    run_path.write_bytes(terminal)
    with pytest.raises(work_run_archive.WorkRunArchiveError, match="cassette"):
        work_run_archive.build_manifest_for_payload(run_dir, run_id=run_dir.name)
    destination = tmp_path / "archive"
    work_run_archive.export_run(run_dir, destination)
    assert (
        "adversarial-cassette-secret"
        not in b"".join(p.read_bytes() for p in destination.rglob("*") if p.is_file()).decode()
    )
    assert _documents(run_dir)


def test_export_with_cassette_journal_reference_validates_imports_and_audits(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.test_run_redaction.RUN_ID", RUN_ID)
    run, snapshot, _ = _authority_run(tmp_path)
    _record(run, result=agents.AgentResult(text="adversarial-cassette-secret", ok=True))
    _finish_recording(run, snapshot)
    assert any(
        e.event_type == run_cassette.EVENT_TYPE
        for e in run_journal.read_journal(run / "events" / "lifecycle.jsonl").events
    )
    destination = tmp_path / "archive"
    work_run_archive.export_run(run, destination)
    assert work_run_archive.validate_cli(destination) == 0
    assert b"adversarial-cassette-secret" not in b"".join(p.read_bytes() for p in destination.rglob("*") if p.is_file())
    imported = work_run_archive.import_archive(destination, runs_dir=tmp_path / "imported")
    assert run_audit.audit_run(Path(imported["run_dir"])).result == "match"


def test_purge_command_keeps_journal_and_audit_unchanged(tmp_path):
    run_dir, snapshot, _ = _authority_run(tmp_path)
    _record(run_dir)
    _finish_recording(run_dir, snapshot)
    before = run_audit.audit_run(run_dir).to_receipt()
    journal = (run_dir / "events" / "lifecycle.jsonl").read_bytes()
    assert cli.main(["runs", "cassette", "purge", str(run_dir)]) == 0
    assert not _documents(run_dir)
    assert (run_dir / "events" / "lifecycle.jsonl").read_bytes() == journal
    assert run_audit.audit_run(run_dir).to_receipt() == before


def test_purge_finishes_when_retention_removes_an_inventoried_file(tmp_path, monkeypatch):
    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    _record(run_dir)
    original = store.dirfd.unlink_child
    raced = False

    def unlink(parent, name, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            other = next(n for n in store.dirfd.child_names(parent, 100) if n != name)
            original(parent, other)
        original(parent, name, **kwargs)

    monkeypatch.setattr(store.dirfd, "unlink_child", unlink)
    store.purge(run_dir)
    assert not list((run_dir / "events" / "cassettes").iterdir())


@pytest.mark.parametrize("cap", ["MAX_ENTRY_BYTES", "MAX_RUN_BYTES"])
def test_caps_stop_recording_once_without_failing_run(tmp_path, monkeypatch, cap, capsys):
    monkeypatch.setattr(store, cap, 128)
    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    run_journal.ensure_journal(run_dir / "events" / "lifecycle.jsonl")
    assert _record(run_dir) is None
    assert _record(run_dir) is None
    docs = _documents(run_dir)
    assert not any(d["privacy_class"] == "private" for d in docs)
    assert any(d["status"] == "stopped" for d in docs)
    assert len(run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl").events) == 1
    assert "cassette recording stopped:" in capsys.readouterr().err


def test_stop_diagnostic_does_not_exceed_the_run_byte_cap(tmp_path, monkeypatch):
    reference = tmp_path / ".brigade" / "runs" / "20261020-120000-reference"
    _record(reference)
    size = sum(p.stat().st_size for p in (reference / "events" / "cassettes").iterdir())
    monkeypatch.setattr(store, "MAX_RUN_BYTES", size + 1)
    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120001-cassette"
    _record(run_dir)
    _record(run_dir, result=agents.AgentResult(text="different answer", ok=True))
    assert sum(p.stat().st_size for p in (run_dir / "events" / "cassettes").iterdir()) <= size + 1


@pytest.mark.parametrize("reason", ["age", "count"])
def test_retention_prunes_old_unpinned_private_bodies_only(tmp_path, monkeypatch, reason):
    monkeypatch.setattr(store, "KEEP_RUNS", 2)
    monkeypatch.setattr(store, "KEEP_DAYS", 30)
    runs = [tmp_path / ".brigade" / "runs" / f"20261020-12000{i}-cassette" for i in range(4)]
    for i, run in enumerate(runs):
        _record(run)
        (run / "run.json").write_text(json.dumps({"run_id": run.name}))
        timestamp = time.time() - (40 if i == 0 or (i == 1 and reason == "age") else 1) * 86400 + i
        os.utime(run, (timestamp, timestamp))
        if i == 0:
            ledger = tmp_path / "memory" / "outcome" / "records.jsonl"
            ledger.parent.mkdir(parents=True)
            ledger.write_text(json.dumps({"evidence_ref": str(run / "run.json")}) + "\n")
    store.retain_private(tmp_path, runs[0].parent, current=runs[-1])
    assert any(d["privacy_class"] == "private" for d in _documents(runs[0]))
    assert not any(d["privacy_class"] == "private" for d in _documents(runs[1]))
    assert any(d["privacy_class"] == "private" for d in _documents(runs[2]))
    assert {d["privacy_class"] for d in _documents(runs[1])} == {"scrubbed", "public-metadata"}


def test_unknown_transport_and_remote_have_no_private_body(tmp_path):
    for i, args in enumerate(
        ({"remote": True}, {"result": agents.AgentResult(text="unobserved", ok=True, transport="unknown")})
    ):
        run = tmp_path / ".brigade" / "runs" / f"20261020-12000{i}-cassette"
        _record(run, **args)
        docs = _documents(run)
        assert not any(d["privacy_class"] == "private" for d in docs)
        assert all(d.get("status") == "not-observed" for d in docs)


def test_remote_marker_does_not_resolve_remote_env_secrets(tmp_path):
    from brigade.roster import Agent

    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    agent = Agent(name="worker", cli="codex", role="write", env={"API_KEY_REF": "REMOTE_ONLY_MISSING_TOKEN"})
    _record(run_dir, agent=agent, remote=True)
    assert all(d.get("status") == "not-observed" for d in _documents(run_dir))


def test_reference_validator_refuses_extra_text_and_traversal(tmp_path):
    run = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    ref = _record(run)
    for change in ({"text": "secret"}, {"path": "../private"}, {"privacy_class": "private"}, {"sha256": "invalid"}):
        with pytest.raises(run_events.CanonicalizationError):
            run_events.build_event(
                run_id=run.name,
                sequence=1,
                event_type=run_cassette.EVENT_TYPE,
                payload={**ref, **change},
                idempotency_key="x",
                previous_digest=None,
                recorded_at="2026-10-20T12:00:00.000000Z",
            )


@pytest.mark.skipif(os.name != "posix", reason="POSIX links and permission modes")
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "directory"])
def test_sidecars_refuse_linked_files_and_directories(tmp_path, kind):
    run = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    _record(run)
    directory = run / "events" / "cassettes"
    assert directory.stat().st_mode & 0o777 == 0o700
    for path in directory.glob("*.json"):
        assert path.stat().st_mode & 0o777 == 0o600
    outside = tmp_path / "outside"
    outside.write_text("do not touch")
    if kind == "hardlink":
        os.link(outside, directory / "unsafe.json")
    elif kind == "directory":
        (directory / "unsafe.json").symlink_to(tmp_path, target_is_directory=True)
    else:
        (directory / "unsafe.json").symlink_to(outside)
    with pytest.raises((OSError, store.CassetteError)):
        store.purge(run)
    assert outside.read_text() == "do not touch"


@pytest.mark.parametrize("seat", ["worker", "security_review"])
def test_appserver_projection_contains_no_worker_text(tmp_path, seat):
    from brigade.aboyeur.planning import _worker_event_writer
    from brigade.roster import Agent

    run = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    events = run / "events"
    events.mkdir(parents=True)
    raw = {
        "method": "item/completed",
        "params": {
            "threadId": "t",
            "turnId": "u",
            "item": {
                "id": "i",
                "type": "agentMessage",
                "text": "adversarial-stream-secret",
            },
        },
    }
    _worker_event_writer(events, seat)(raw)
    _record(
        run,
        agent=Agent(name=seat, cli="codex", role="review"),
        result=agents.AgentResult(text="answer", ok=True, transport="codex-app-server"),
    )
    metadata = next(d for d in _documents(run) if d["privacy_class"] == "scrubbed")
    assert "adversarial-stream-secret" not in json.dumps(metadata)
    projection = metadata["notification_projection"]
    assert metadata["notification_projection_sha256"] == hashlib.sha256(store.encode(projection)).hexdigest()


def test_verify_archival_refuses_cassette_bodies_before_copy(tmp_path):
    from brigade.work_cmd.verification import _archive_verify_run
    from tests.test_evidence_package import _fabricated_run_dir

    run_dir, _ = _fabricated_run_dir(tmp_path)
    _record(run_dir, result=agents.AgentResult(text="adversarial-private-needle", ok=True))
    with pytest.raises(OSError, match="cassette"):
        _archive_verify_run(run_dir, tmp_path / "verify-archive")
    assert not (tmp_path / "verify-archive" / run_dir.name).exists()


def test_evidence_package_allowlist_excludes_cassette_bodies(tmp_path):
    from brigade import evidence_package
    from tests.test_evidence_package import _fabricated_run_dir

    run_dir, receipt = _fabricated_run_dir(tmp_path)
    _record(run_dir, result=agents.AgentResult(text="evidence-private-needle", ok=True))
    output = tmp_path / "package"
    assert evidence_package.export_package(target=tmp_path, run_id=receipt["run_id"], out_str=str(output)) == 0
    assert b"evidence-private-needle" not in b"".join(p.read_bytes() for p in output.rglob("*") if p.is_file())


def test_fleet_report_projects_only_lifecycle_metadata(tmp_path, monkeypatch):
    from brigade import fleet_client

    reported = []
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "https://invalid.example"})
    monkeypatch.setattr(fleet_client, "report_event", lambda event, **kw: reported.append(event))
    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    run_journal.ensure_journal(run_dir / "events" / "lifecycle.jsonl")
    _record(run_dir, result=agents.AgentResult(text="fleet-private-needle", ok=True))
    assert reported
    assert "fleet-private-needle" not in json.dumps(reported)
    assert all("payload" not in row and "artifacts" not in row for row in reported)


def test_retention_does_not_parse_stop_control_file(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MAX_RUN_BYTES", 128)
    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    _record(run_dir)
    assert store.purge(run_dir, private_only=True) == 0


def test_configuration_round_trip_and_invalid_policy(tmp_path):
    from brigade.config import Config, load_config, write_config
    from brigade.selection import Selection

    cfg = Config(version=1, selection=Selection(depth="repo", harnesses=["codex"]), run_record="off")
    write_config(tmp_path, cfg)
    assert load_config(tmp_path).run_record == "off"
    path = tmp_path / ".brigade" / "config.json"
    raw = json.loads(path.read_text())
    raw["run"]["record"] = "unsafe-typo"
    path.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="run.record"):
        load_config(tmp_path)


def test_journal_admission_does_not_publish_private_bodies_near_event_cap(tmp_path):
    run_dir = tmp_path / ".brigade" / "runs" / "20261020-120000-cassette"
    path = run_dir / "events" / "lifecycle.jsonl"
    path.parent.mkdir(parents=True)
    lines = []
    previous = None
    for sequence in range(1, 2041):
        event = run_events.build_event(
            run_id=run_dir.name,
            sequence=sequence,
            event_type="run.created",
            payload={"status": "started"},
            idempotency_key=f"created-{sequence}",
            recorded_at="2026-10-20T12:00:00.000000Z",
            previous_digest=previous,
        )
        previous = event["event_digest"]
        lines.append(run_events.canonical_bytes(event) + b"\n")
    path.write_bytes(b"".join(lines))
    before = path.read_bytes()
    assert _record(run_dir) is None
    assert path.read_bytes() == before
    assert not any(doc["privacy_class"] == "private" for doc in _documents(run_dir))
    assert len(lines) < 2048 and len(before) < 8 * 1024 * 1024


def test_audit_exposes_orchestrator_recording_gap(tmp_path):
    run_dir, _, _ = _authority_run(tmp_path)
    coverage = run_audit.audit_run(run_dir).transport_coverage["coverage"]
    assert "not recorded: orchestrator calls" in coverage["note"]
