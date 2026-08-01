"""Acceptance tests for offline coordinator decision audit (issue #595)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from brigade import cli, run_audit, run_events, run_journal, run_projector

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "run-lifecycle"
GOLDEN_LIFECYCLE = FIXTURES / "golden-lifecycle.jsonl"
GOLDEN_BASE = FIXTURES / "golden-projection.base.json"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _copy_golden_journal(dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(GOLDEN_LIFECYCLE, dest)


def _golden_run_dir(tmp_path: Path, *, mutate_run: dict | None = None) -> Path:
    """Build an auditable run directory from the golden lifecycle fixtures."""
    run_id = "20260727-153045-a1b2c3d4"
    run_dir = tmp_path / ".brigade" / "runs" / run_id
    run_dir.mkdir(parents=True)
    base = json.loads(GOLDEN_BASE.read_text(encoding="utf-8"))
    # Project so journal_* derived fields are present for consistency checks.
    report = run_journal.read_journal(GOLDEN_LIFECYCLE)
    assert report.chain_errors == []
    projection = run_projector.project_run_snapshot(base, report.events, journal_present=True)
    run_meta = dict(projection.snapshot)
    if mutate_run:
        run_meta.update(mutate_run)
    _write_json(run_dir / "run.json", run_meta)
    _copy_golden_journal(run_dir / "events" / "lifecycle.jsonl")
    _write_json(
        run_dir / "plan.json",
        {
            "assignments": [
                {"worker": "coder", "task": "Implement the projector"},
            ]
        },
    )
    _write_json(
        run_dir / "roster.json",
        {
            "orchestrator": "chef",
            "agents": {
                "coder": {"cli": "codex", "role": "Implement code changes"},
                "chef": {"cli": "codex", "role": "Plan and synthesize"},
            },
        },
    )
    return run_dir


def test_forbid_live_side_effects_sentinel():
    """The audit path cannot construct a provider adapter or subprocess runner."""
    run_audit.forbid_live_side_effects()
    source = Path(run_audit.__file__).read_text(encoding="utf-8")
    for banned in (
        "import subprocess",
        "from subprocess",
        "import socket",
        "from socket",
        "import urllib",
        "from urllib",
        "codex_appserver",
    ):
        # Comments may mention the ban; import forms must not appear.
        if banned.startswith("import ") or banned.startswith("from "):
            assert banned not in source
        else:
            assert f"import {banned}" not in source
            assert f"from {banned}" not in source


def test_golden_run_audits_twice_to_byte_identical_normalized_events(tmp_path: Path):
    """AC: A golden run audits twice to byte-identical normalized coordinator events."""
    run_dir = _golden_run_dir(tmp_path)
    first = run_audit.audit_run(run_dir)
    second = run_audit.audit_run(run_dir)
    assert first.result == run_audit.RESULT_MATCH
    assert second.result == run_audit.RESULT_MATCH
    assert first.normalized_events_bytes() == second.normalized_events_bytes()
    assert first.evidence_digests["normalized_events"] == second.evidence_digests["normalized_events"]


def test_changing_prompt_template_stops_at_first_composed_prompt(tmp_path: Path):
    """AC: Changing a prompt template stops at the first affected composed worker prompt."""
    run_dir = _golden_run_dir(tmp_path)
    baseline = run_audit.audit_run(run_dir)
    assert baseline.result == run_audit.RESULT_MATCH
    drifted = run_audit.audit_run(
        run_dir,
        expected_normalized_events=baseline.normalized_events,
        template_revision="brigade.worker_prompt.v1-mutated",
    )
    assert drifted.result == run_audit.RESULT_DIVERGE
    assert drifted.first_divergence is not None
    assert drifted.first_divergence.divergence_class == run_audit.CLASS_PROMPT_TEMPLATE_DRIFT
    assert drifted.first_divergence.observed is not None
    assert drifted.first_divergence.observed.get("kind") == "composed_prompt"


def test_changing_route_selection_stops_at_first_coordinator_decision(tmp_path: Path):
    """AC: Changing route selection stops at the first affected coordinator decision."""
    run_dir = _golden_run_dir(tmp_path)
    baseline = run_audit.audit_run(run_dir)
    run_meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_meta["route"] = {"skill": "mutated-route", "matched": ["x"], "score": 99}
    _write_json(run_dir / "run.json", run_meta)
    drifted = run_audit.audit_run(run_dir, expected_normalized_events=baseline.normalized_events)
    assert drifted.result == run_audit.RESULT_DIVERGE
    assert drifted.first_divergence is not None
    assert drifted.first_divergence.divergence_class == run_audit.CLASS_POLICY_OR_ROUTING_DRIFT


def test_changing_approval_state_stops_at_first_coordinator_decision(tmp_path: Path):
    """AC: Changing approval state stops at the first affected coordinator decision."""
    run_dir = tmp_path / "approval-run"
    run_dir.mkdir()
    run_id = "approval-run-1"
    created = run_events.build_event(
        run_id=run_id,
        sequence=1,
        event_type="run.created",
        payload={"status": "started"},
        idempotency_key="create-1",
        recorded_at="2026-07-27T15:30:45.123456Z",
        previous_digest=None,
    )
    paused = run_events.build_event(
        run_id=run_id,
        sequence=2,
        event_type="run.paused",
        payload={"approval_id": "appr-1", "reason": "approval-wait"},
        idempotency_key="pause-1",
        recorded_at="2026-07-27T15:30:46.000000Z",
        previous_digest=created["event_digest"],
    )
    requested = run_events.build_event(
        run_id=run_id,
        sequence=3,
        event_type="approval.requested",
        payload={
            "approval_id": "appr-1",
            "source": "daily",
            "contract_fingerprint": "a" * 64,
        },
        idempotency_key="appr-req-1",
        recorded_at="2026-07-27T15:30:46.123456Z",
        previous_digest=paused["event_digest"],
    )
    granted = run_events.build_event(
        run_id=run_id,
        sequence=4,
        event_type="approval.granted",
        payload={
            "approval_id": "appr-1",
            "decided_at": "2026-07-27T15:30:47.123456Z",
            "decision_state": "approved",
        },
        idempotency_key="appr-grant-1",
        recorded_at="2026-07-27T15:30:47.123456Z",
        previous_digest=requested["event_digest"],
    )
    events_dir = run_dir / "events"
    events_dir.mkdir()
    journal = events_dir / "lifecycle.jsonl"
    with journal.open("wb") as handle:
        for env in (created, paused, requested, granted):
            handle.write(run_events.canonical_bytes(env) + b"\n")
    _write_json(
        run_dir / "run.json",
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "running",
            "task": "approve something",
            "orchestrator": "chef",
            "roster": "default",
            "worker": "coder",
            "scheduler": "immediate",
            "codex_transport": "app-server",
            "approval_reference": {
                "approval_id": "appr-1",
                "source": "daily",
                "fingerprint": "fp",
                "source_fingerprint": "sfp",
                "contract_fingerprint": "a" * 64,
                "evidence_fingerprint": "efp",
                "decision_state": "approved",
            },
        },
    )
    baseline = run_audit.audit_run(run_dir)
    assert baseline.result == run_audit.RESULT_MATCH
    # Mutate archived approval decision_state.
    run_meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_meta["approval_reference"]["decision_state"] = "rejected"
    _write_json(run_dir / "run.json", run_meta)
    drifted = run_audit.audit_run(run_dir)
    assert drifted.result == run_audit.RESULT_DIVERGE
    assert drifted.first_divergence is not None
    assert drifted.first_divergence.divergence_class == run_audit.CLASS_APPROVAL_STATE_DRIFT


def test_missing_corrupt_evidence_reports_exact_artifact(tmp_path: Path):
    """AC: Missing or corrupt evidence reports the exact artifact and event."""
    run_dir = tmp_path / "corrupt-run"
    run_dir.mkdir()
    _write_json(
        run_dir / "run.json",
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "started",
            "task": "t",
            "orchestrator": "chef",
            "roster": "r",
            "worker": "coder",
            "scheduler": "immediate",
        },
    )
    events = run_dir / "events"
    events.mkdir()
    (events / "lifecycle.jsonl").write_text("{not-json\n", encoding="utf-8")
    report = run_audit.audit_run(run_dir)
    assert report.result == run_audit.RESULT_ERROR
    assert report.first_divergence is not None
    assert report.first_divergence.divergence_class == run_audit.CLASS_MISSING_OR_CORRUPT_FIXTURE
    assert report.first_divergence.expected == {"artifact": "events/lifecycle.jsonl"}


def test_unsupported_transport_states_coverage(tmp_path: Path):
    """AC: Unsupported transports state their coverage instead of claiming full replay."""
    run_dir = _golden_run_dir(tmp_path, mutate_run={"codex_transport": "mystery-bus"})
    report = run_audit.audit_run(run_dir)
    assert report.transport_coverage["supported"] is False
    assert report.transport_coverage["coverage"]["provider_response_fixtures"] is False
    assert "unsupported transport" in report.transport_coverage["coverage"]["note"]


def test_unsupported_projector_version_stops_with_compatibility_diagnostics(tmp_path: Path):
    """AC: Unsupported lifecycle or audit schema versions stop with bounded diagnostics."""
    run_dir = _golden_run_dir(tmp_path, mutate_run={"projector_version": 999})
    report = run_audit.audit_run(run_dir)
    assert report.result == run_audit.RESULT_NOT_AUDITABLE
    assert report.first_divergence is not None
    assert report.first_divergence.divergence_class == run_audit.CLASS_UNSUPPORTED_SCHEMA
    assert "unsupported projector version" in (report.not_auditable_reason or "")


def test_legacy_run_without_journal_reports_not_auditable(tmp_path: Path):
    """AC: Legacy runs without required evidence report not auditable."""
    run_dir = tmp_path / "legacy"
    run_dir.mkdir()
    _write_json(
        run_dir / "run.json",
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "ok",
            "task": "legacy",
            "orchestrator": "chef",
            "roster": "r",
            "worker": "coder",
            "scheduler": "immediate",
        },
    )
    report = run_audit.audit_run(run_dir)
    assert report.result == run_audit.RESULT_NOT_AUDITABLE
    assert report.not_auditable_reason is not None
    assert "lifecycle journal" in report.not_auditable_reason


def test_legacy_run_symlinked_plan_json_returns_bounded_not_auditable(tmp_path: Path):
    """Symlinked sidecar evidence must not crash digest computation on legacy runs."""
    run_dir = tmp_path / "legacy-symlink-plan"
    run_dir.mkdir()
    _write_json(
        run_dir / "run.json",
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "ok",
            "task": "legacy",
            "orchestrator": "chef",
            "roster": "r",
            "worker": "coder",
            "scheduler": "immediate",
        },
    )
    real_plan = tmp_path / "real-plan.json"
    _write_json(real_plan, {"assignments": [{"worker": "coder", "task": "t"}]})
    (run_dir / "plan.json").symlink_to(real_plan)
    report = run_audit.audit_run(run_dir)
    assert report.result == run_audit.RESULT_NOT_AUDITABLE
    assert report.not_auditable_reason is not None
    assert "lifecycle journal" in report.not_auditable_reason
    assert "run.json" in report.evidence_digests
    assert "plan.json" not in report.evidence_digests


def test_seat_order_permutation_reports_routing_drift(tmp_path: Path):
    """Dispatch seat order must match plan.json assignments, not just the seat set."""
    run_dir = _golden_run_dir(tmp_path)
    _write_json(
        run_dir / "plan.json",
        {
            "assignments": [
                {"worker": "coder", "task": "first"},
                {"worker": "chef", "task": "second"},
            ]
        },
    )
    report = run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl")
    assert report.chain_errors == []
    created = report.events[0]
    # Build a journal with dispatch seats reversed versus plan order.
    dispatch_chef = run_events.build_event(
        run_id=created.run_id,
        sequence=3,
        event_type="run.dispatch.requested",
        payload={"attempt": 1, "seat": "chef"},
        idempotency_key="dispatch-chef",
        recorded_at="2026-07-27T15:30:47.000000Z",
        previous_digest=report.events[1].event_digest,
    )
    dispatch_coder = run_events.build_event(
        run_id=created.run_id,
        sequence=4,
        event_type="run.dispatch.requested",
        payload={"attempt": 1, "seat": "coder"},
        idempotency_key="dispatch-coder",
        recorded_at="2026-07-27T15:30:48.000000Z",
        previous_digest=dispatch_chef["event_digest"],
    )
    journal = run_dir / "events" / "lifecycle.jsonl"
    with journal.open("wb") as handle:
        for env in (report.events[0].to_dict(), report.events[1].to_dict(), dispatch_chef, dispatch_coder):
            handle.write(run_events.canonical_bytes(env) + b"\n")
    journal_report = run_journal.read_journal(journal)
    base = json.loads(GOLDEN_BASE.read_text(encoding="utf-8"))
    projection = run_projector.project_run_snapshot(base, journal_report.events, journal_present=True)
    _write_json(run_dir / "run.json", dict(projection.snapshot))
    drifted = run_audit.audit_run(run_dir)
    assert drifted.result == run_audit.RESULT_DIVERGE
    assert drifted.first_divergence is not None
    assert drifted.first_divergence.divergence_class == run_audit.CLASS_POLICY_OR_ROUTING_DRIFT


def test_private_data_fixtures_expose_only_safe_summaries(tmp_path: Path):
    """AC: Reports expose only safe summaries, fingerprints, and classified references."""
    secret = "SUPER-SECRET-PROMPT-BODY-do-not-leak"
    run_dir = _golden_run_dir(tmp_path)
    _write_json(
        run_dir / "plan.json",
        {"assignments": [{"worker": "coder", "task": secret}]},
    )
    report = run_audit.audit_run(run_dir)
    receipt_text = json.dumps(report.to_receipt(), sort_keys=True)
    events_text = json.dumps(report.normalized_events, sort_keys=True)
    assert secret not in receipt_text
    assert secret not in events_text
    assert "fingerprint" in events_text


def test_receipt_records_required_fields(tmp_path: Path):
    """AC: Receipt records source run, projector version, code revision, result, divergence, digests."""
    run_dir = _golden_run_dir(tmp_path)
    report = run_audit.audit_run(run_dir, code_revision="test-rev")
    receipt = report.to_receipt()
    assert receipt["schema"] == run_audit.AUDIT_SCHEMA
    assert receipt["source_run"] == "20260727-153045-a1b2c3d4"
    assert receipt["projector_version"] == run_projector.PROJECTOR_VERSION
    assert receipt["code_revision"] == "test-rev"
    assert receipt["result"] == run_audit.RESULT_MATCH
    assert receipt["first_divergence"] is None
    assert "events/lifecycle.jsonl" in receipt["evidence_digests"]
    assert "run.json" in receipt["evidence_digests"]
    assert "normalized_events" in receipt["evidence_digests"]


def test_audit_does_not_write_run_directory(tmp_path: Path):
    """Hard constraint: audit mode cannot mutate the run directory."""
    run_dir = _golden_run_dir(tmp_path)
    before = {path.relative_to(run_dir): path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}
    run_audit.audit_run(run_dir)
    after = {path.relative_to(run_dir): path.read_bytes() for path in run_dir.rglob("*") if path.is_file()}
    assert before == after


def test_cli_runs_audit_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    run_dir = _golden_run_dir(tmp_path)
    rc = cli.main(["runs", "audit", str(run_dir), "--json"])
    captured = capsys.readouterr()
    assert rc == 0
    payload = json.loads(captured.out)
    assert payload["result"] == "match"
    assert payload["source_run"] == "20260727-153045-a1b2c3d4"
