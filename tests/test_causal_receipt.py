"""Tests for brigade.causal_receipt.v1 schema, emission, and telemetry projection."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from brigade import aboyeur, causal_receipt, localio, outcome_cmd, receipt_schema, telemetry_export
from brigade.roster import Agent, Roster
from brigade.run_transport import Assignment, WorkerResult
from brigade.work_cmd import verification as verify_mod

FIXTURES = Path(__file__).resolve().parents[1] / "src" / "brigade" / "fixtures"
GOLDEN_PATH = FIXTURES / "causal-receipt.v1.golden.json"
RUN_ID = "20260101-120000-abcd1234"
VERIFY_ID = "20260101-120100-work-verify-aa11"
DIGEST_A = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
DIGEST_B = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


def _golden_cases():
    return json.loads(GOLDEN_PATH.read_text())["cases"]


def _case(name):
    return next(item for item in _golden_cases() if item["name"] == name)


def _hex64(seed: str) -> str:
    return hashlib.sha256(seed.encode()).hexdigest()


def _recorded_chain():
    plan = causal_receipt.recorded_plan(run_id=RUN_ID)
    run = causal_receipt.recorded_run(run_id=RUN_ID, plan_digest=causal_receipt.receipt_digest(plan))
    verify = causal_receipt.recorded_verify(
        verify_id=VERIFY_ID,
        run_id=RUN_ID,
        run_digest=causal_receipt.receipt_digest(run),
    )
    outcome = causal_receipt.recorded_outcome(
        subject_id=f"{VERIFY_ID}--brigade-work",
        parent_kind="verify",
        parent_id=VERIFY_ID,
        parent_digest=causal_receipt.receipt_digest(verify),
    )
    handoff = causal_receipt.recorded_handoff(
        handoff_id="2026-01-01-1200-brigade-run-demo",
        parent_kind="outcome",
        parent_id=outcome["subject"]["id"],
        parent_digest=causal_receipt.receipt_digest(outcome),
    )
    return {
        ("plan", RUN_ID): plan,
        ("run", RUN_ID): run,
        ("verify", VERIFY_ID): verify,
        ("outcome", outcome["subject"]["id"]): outcome,
        ("handoff", handoff["subject"]["id"]): handoff,
    }


def test_golden_receipts_validate():
    for case in _golden_cases():
        assert causal_receipt.validate_receipt(case["receipt"]) == []


def test_build_matches_golden_plan_and_inferred():
    assert causal_receipt.recorded_plan(run_id=RUN_ID) == _case("plan_root")["receipt"]
    inferred = causal_receipt.build_receipt(
        subject_kind="verify",
        subject_id=VERIFY_ID,
        parents=[
            causal_receipt.parent_ref(
                relation="executed_from",
                kind="run",
                artifact_id=RUN_ID,
                link="inferred",
            )
        ],
    )
    assert inferred == _case("inferred_backfill")["receipt"]
    assert inferred["parents"][0]["link"] == "inferred"
    assert causal_receipt.recorded_run(run_id=RUN_ID)["parents"][0]["link"] == "recorded"


def test_recorded_chain_traverses_without_paths():
    index = _recorded_chain()
    handoff = index[("handoff", "2026-01-01-1200-brigade-run-demo")]

    def resolve(kind, artifact_id):
        return index.get((kind, artifact_id))

    hops = causal_receipt.walk_ancestors(handoff, resolve)
    assert [hop["status"] for hop in hops] == ["ok", "ok", "ok", "ok"]
    assert causal_receipt.chain_kinds(hops) == ["handoff", "outcome", "verify", "run", "plan"]
    assert all(hop["link"] == "recorded" for hop in hops)
    assert all("path" not in hop and "ts" not in hop for hop in hops)


def test_synthesis_can_reference_multiple_worker_results():
    receipt = causal_receipt.recorded_synthesis(
        run_id=RUN_ID,
        worker_parents=causal_receipt.synthesis_worker_parents(
            RUN_ID,
            [{"worker": "coder"}, {"worker": "reviewer"}],
        ),
    )
    assert receipt == _case("synthesis_multi_parent")["receipt"]
    assert [parent["id"] for parent in receipt["parents"]] == [
        f"{RUN_ID}:coder:1",
        f"{RUN_ID}:reviewer:2",
    ]


def test_parent_count_boundary():
    parents = [
        causal_receipt.parent_ref(
            relation="synthesized_from",
            kind="worker-result",
            artifact_id=causal_receipt.worker_result_id(RUN_ID, "worker", ordinal),
        )
        for ordinal in range(1, causal_receipt.MAX_PARENTS + 1)
    ]
    ok = causal_receipt.build_receipt(subject_kind="synthesis", subject_id=RUN_ID, parents=parents)
    assert len(ok["parents"]) == causal_receipt.MAX_PARENTS
    overflow = dict(ok)
    overflow["parents"] = [
        *parents,
        causal_receipt.parent_ref(
            relation="synthesized_from",
            kind="worker-result",
            artifact_id=causal_receipt.worker_result_id(RUN_ID, "worker", causal_receipt.MAX_PARENTS + 1),
        ),
    ]
    errors = causal_receipt.validate_receipt(overflow)
    assert any("parent count exceeds" in item and "parent_manifest" in item for item in errors)


def test_encoded_size_boundary():
    digest = _hex64("size-boundary")
    parents = [
        causal_receipt.parent_ref(
            relation="synthesized_from",
            kind="worker-result",
            artifact_id=causal_receipt.worker_result_id(RUN_ID, f"seat{index}", index),
            digest=digest,
        )
        for index in range(1, 9)
    ]
    receipt = causal_receipt.build_receipt(subject_kind="synthesis", subject_id=RUN_ID, parents=parents)
    encoded = causal_receipt.compact_bytes(receipt)
    assert len(encoded) <= causal_receipt.MAX_COMPACT_BYTES
    padded = dict(receipt)
    padded["subject"] = {"kind": "synthesis", "id": "pad-" + ("n" * 1800)}
    errors = causal_receipt.validate_receipt(padded)
    assert any("compact JSON size exceeds" in item for item in errors)


def test_parent_manifest_replaces_inline_parents():
    receipt = causal_receipt.build_receipt(
        subject_kind="synthesis",
        subject_id=RUN_ID,
        parent_manifest={"id": "fan-in-manifest-1", "digest": DIGEST_A},
    )
    assert receipt == _case("parent_manifest")["receipt"]
    mixed = dict(receipt)
    mixed["parents"] = [
        causal_receipt.parent_ref(relation="synthesized_from", kind="worker-result", artifact_id=f"{RUN_ID}:coder:1")
    ]
    errors = causal_receipt.validate_receipt(mixed)
    assert any("parent_manifest replaces inline parents" in item for item in errors)


def test_broken_unknown_version_and_digest_diagnostics_are_bounded():
    index = _recorded_chain()
    broken = causal_receipt.recorded_outcome(
        subject_id="missing-parent--brigade-work",
        parent_kind="verify",
        parent_id="missing-verify",
        parent_digest=DIGEST_A,
    )

    def resolve(kind, artifact_id):
        return index.get((kind, artifact_id))

    hops = causal_receipt.walk_ancestors(broken, resolve)
    assert hops[0]["status"] == "broken"
    assert "broken lineage" in hops[0]["diagnostic"]
    assert len(hops[0]["diagnostic"]) <= causal_receipt.MAX_DIAGNOSTIC_LEN

    mismatch = causal_receipt.recorded_run(run_id=RUN_ID, plan_digest=DIGEST_B)
    hops = causal_receipt.walk_ancestors(mismatch, resolve, digest_of=causal_receipt.receipt_digest)
    assert hops[0]["status"] == "digest_mismatch"
    assert hops[0]["diagnostic"] == "parent digest mismatch"

    unknown = causal_receipt.recorded_plan(run_id=RUN_ID)
    unknown["parents"] = [
        {
            "relation": "caused_by",
            "kind": "plan",
            "id": RUN_ID,
            "link": "recorded",
        }
    ]
    errors = causal_receipt.validate_receipt(unknown)
    assert any("closed lineage relation" in item for item in errors)
    assert "caused_by" not in json.dumps(errors) or all(
        len(item) <= causal_receipt.MAX_DIAGNOSTIC_LEN for item in errors
    )

    future = dict(_case("plan_root")["receipt"])
    future["schema_version"] = 2
    errors = causal_receipt.validate_receipt(future)
    assert any("unsupported causal receipt schema_version" in item for item in errors)
    malformed = dict(_case("recorded_chain_run")["receipt"])
    malformed["parents"][0]["digest"] = "not-a-digest"
    errors = causal_receipt.validate_receipt(malformed)
    assert any("digest must be a bare lowercase 64-char hex string" in item for item in errors)


def test_private_field_fixture_stores_references_not_payloads():
    case = _case("private_field_exclusion")
    receipt = case["receipt"]
    assert causal_receipt.validate_receipt(receipt) == []
    rendered = json.dumps(receipt)
    for secret in case["sensitive"].values():
        if isinstance(secret, dict):
            secret = json.dumps(secret)
        assert secret not in rendered
    leaked = dict(receipt)
    leaked["text"] = "PRIVATE OUTPUT"
    errors = causal_receipt.validate_receipt(leaked)
    assert any("must not embed sensitive" in item for item in errors)
    assert "PRIVATE OUTPUT" not in "".join(errors)


def test_legacy_artifacts_remain_readable_without_companion(tmp_path):
    plan = receipt_schema.run_plan_document([])
    assert "causal_receipt" not in plan
    assert causal_receipt.read_causal_receipt(plan) is None
    row = {
        "artifact_id": "brigade-work",
        "artifact_kind": "skill",
        "task_id": "",
        "source": "friction",
        "signal_value": 1,
        "evidence_ref": "",
        "ts": "2026-01-01T00:00:00+00:00",
        "prev_digest": None,
        "digest": "abc",
    }
    path = tmp_path / "memory" / "outcome" / "records.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(row, sort_keys=True) + "\n")
    records = outcome_cmd.load_records(tmp_path)
    assert records[0].artifact_id == "brigade-work"
    assert causal_receipt.read_causal_receipt(row) is None


def test_writers_emit_recorded_plan_run_verify_outcome_handoff_chain(tmp_path, monkeypatch):
    run_dir = tmp_path / ".brigade" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    plan = receipt_schema.run_plan_document([{"stage": 1, "worker": "coder", "task": "do work"}], run_id=RUN_ID)
    localio.write_json(run_dir / "plan.json", plan)
    roster = Roster(
        orchestrator="chef",
        max_workers=1,
        agents={
            "chef": Agent(
                name="chef",
                cli="echo",
                role="orchestrator",
                model="test",
                transport="local",
            )
        },
    )
    payload = aboyeur._run_payload(
        task="do work",
        cwd=tmp_path,
        lock_workspace=tmp_path,
        roster=roster,
        dry_run=True,
        read_only=True,
        status="started",
        started_at=aboyeur.datetime.now(aboyeur.timezone.utc),
        output_dir=run_dir,
        include_git=False,
        causal_receipt_payload=causal_receipt.recorded_run(
            run_id=RUN_ID,
            plan_digest=causal_receipt.receipt_digest(plan["causal_receipt"]),
        ),
    )
    localio.write_json(run_dir / "run.json", payload)
    monkeypatch.setenv(receipt_schema.BRIGADE_RUN_ID_ENV, RUN_ID)
    rc = verify_mod.verify_run(
        target=tmp_path,
        commands=[f"{sys.executable} -c \"print('ok')\""],
        reuse=False,
        json_output=False,
    )
    assert rc == 0
    verify_root = tmp_path / ".brigade" / "work" / "verify-runs"
    verify_dirs = [path for path in verify_root.iterdir() if path.is_dir()]
    assert len(verify_dirs) == 1
    verify_receipt = json.loads((verify_dirs[0] / "receipt.json").read_text())
    assert verify_receipt["producer_run_id"] == RUN_ID
    assert verify_receipt["causal_receipt"]["parents"][0]["link"] == "recorded"
    assert verify_receipt["causal_receipt"]["parents"][0]["id"] == RUN_ID

    rc = outcome_cmd.capture(target=tmp_path, artifact_id="brigade-work", run_id=verify_receipt["run_id"])
    assert rc == 0
    outcome_row = json.loads((tmp_path / "memory" / "outcome" / "records.jsonl").read_text().splitlines()[0])
    assert outcome_row["causal_receipt"]["parents"][0]["kind"] == "verify"
    assert outcome_row["causal_receipt"]["parents"][0]["link"] == "recorded"

    inbox = tmp_path / "handoffs"
    handoff = aboyeur.write_run_handoff(
        inbox,
        task="do work",
        cwd=tmp_path,
        output_dir=run_dir,
        assignments=[Assignment(stage=1, worker="coder", task="do work")],
        worker_results=[WorkerResult(worker="coder", task="do work", ok=True, text="PRIVATE OUTPUT")],
        final_text="PRIVATE OUTPUT",
        outcome_id=outcome_row["causal_receipt"]["subject"]["id"],
        outcome_digest=causal_receipt.receipt_digest(outcome_row["causal_receipt"]),
    )
    sidecar = json.loads(causal_receipt.handoff_sidecar_path(handoff).read_text())
    assert sidecar["parents"][0]["kind"] == "outcome"
    assert sidecar["parents"][0]["link"] == "recorded"
    rendered = json.dumps(sidecar)
    assert "PRIVATE OUTPUT" not in rendered
    assert str(tmp_path) not in rendered

    index = {
        ("plan", RUN_ID): plan["causal_receipt"],
        ("run", RUN_ID): payload["causal_receipt"],
        ("verify", verify_receipt["run_id"]): verify_receipt["causal_receipt"],
        ("outcome", outcome_row["causal_receipt"]["subject"]["id"]): outcome_row["causal_receipt"],
        ("handoff", sidecar["subject"]["id"]): sidecar,
    }
    hops = causal_receipt.walk_ancestors(sidecar, lambda kind, artifact_id: index.get((kind, artifact_id)))
    assert causal_receipt.chain_kinds(hops) == ["handoff", "outcome", "verify", "run", "plan"]
    assert all(hop["status"] == "ok" for hop in hops)


def test_telemetry_projects_lineage_parentage_and_keeps_deterministic_ids(tmp_path):
    run_dir = tmp_path / ".brigade" / "runs" / "run-1"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps({"started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:02Z"})
    )
    (run_dir / "roster.json").write_text(json.dumps({"agents": {"worker": {"cli": "grok", "model": "grok-4.5"}}}))
    (run_dir / "worker-results.json").write_text(
        json.dumps(
            {
                "results": [
                    {
                        "worker": "worker",
                        "ok": True,
                        "text": "PRIVATE OUTPUT",
                        "duration_seconds": 2.0,
                        "transport": "acpx",
                        "requested_model": "grok-4.5",
                        "effective_model": "grok-4.5",
                        "stop_reason": "end_turn",
                        "exit_code": 0,
                    }
                ]
            }
        )
    )
    verify_id = "20260101-000000-work-verify-lineage"
    verify_dir = tmp_path / ".brigade" / "work" / "verify-runs" / verify_id
    verify_dir.mkdir(parents=True)
    receipt = {
        "run_id": verify_id,
        "target": str(tmp_path),
        "status": "completed",
        "started_at": "2026-01-01T00:00:01Z",
        "completed_at": "2026-01-01T00:00:03Z",
        "duration_seconds": 2.0,
        "commands": [
            {
                "command": "pytest -q",
                "status": "completed",
                "exit_code": 0,
                "duration_seconds": 1.5,
                "started_at": "2026-01-01T00:00:01Z",
                "completed_at": "2026-01-01T00:00:02Z",
            }
        ],
        "causal_receipt": causal_receipt.recorded_verify(verify_id=verify_id, run_id="run-1"),
    }
    localio.write_json(verify_dir / "receipt.json", receipt)
    outcome_row = {
        "artifact_id": "brigade-work",
        "artifact_kind": "skill",
        "task_id": "task-1",
        "source": "verify",
        "signal_value": 1,
        "evidence_ref": "opaque-not-a-path",
        "ts": "2026-01-01T00:00:04Z",
        "prev_digest": None,
    }
    outcome_row["causal_receipt"] = causal_receipt.recorded_outcome(
        subject_id=f"{verify_id}--brigade-work",
        parent_kind="verify",
        parent_id=verify_id,
    )
    outcome_row["digest"] = localio.canonical_json_digest(outcome_row, exclude_keys={"digest"})
    records_path = tmp_path / "memory" / "outcome" / "records.jsonl"
    records_path.parent.mkdir(parents=True)
    records_path.write_text(json.dumps(outcome_row, sort_keys=True) + "\n")
    (tmp_path / "memory" / "outcome" / "status.json").write_text(
        json.dumps({"artifacts": {"brigade-work": {"status": "promoted", "last_action_ts": "2026-01-01T00:00:05Z"}}})
    )

    rows = telemetry_export.records(tmp_path, "otel-genai")
    worker = next(row for row in rows if row["name"] == "brigade.run.worker")
    verify = next(row for row in rows if "brigade.work.verify" in row["name"])
    outcome = next(row for row in rows if "brigade.outcome.capture" in row["name"])
    assert worker["trace_id"] == telemetry_export._ids("run-1", "worker", 1)[0]
    assert worker["span_id"] == telemetry_export._ids("run-1", "worker", 1)[1]
    assert verify["parent_span_id"] == worker["span_id"]
    assert verify["trace_id"] == worker["trace_id"]
    assert outcome["parent_span_id"] == worker["span_id"]
    assert outcome["trace_id"] == worker["trace_id"]
    assert "PRIVATE OUTPUT" not in json.dumps(rows)
    assert telemetry_export._ids("run-1", "worker", 1) == telemetry_export._ids("run-1", "worker", 1)


def test_forbidden_home_path_ids_are_rejected():
    with pytest.raises(ValueError, match="safe identity label"):
        causal_receipt.build_receipt(subject_kind="run", subject_id="/home/user/secret")
