"""Filesystem-backed tests for the receipts lineage command."""

from __future__ import annotations

import json

from brigade import causal_receipt, cli, localio
from brigade.receipts_lineage import lineage


def test_lineage_resolves_recorded_chain_without_exposing_paths_or_payloads(tmp_path):
    run_id = "run-lineage"
    run_dir = tmp_path / ".brigade" / "runs" / run_id
    verify_id = "verify-lineage"
    verify_dir = tmp_path / ".brigade" / "work" / "verify-runs" / verify_id
    handoff_path = tmp_path / "handoffs" / "note.md"
    run_dir.mkdir(parents=True)
    verify_dir.mkdir(parents=True)
    handoff_path.parent.mkdir()

    plan = {"causal_receipt": causal_receipt.recorded_plan(run_id=run_id)}
    run = {
        "causal_receipt": causal_receipt.recorded_run(
            run_id=run_id,
            plan_digest=causal_receipt.receipt_digest(plan["causal_receipt"]),
        ),
        "lineage": {"kind": "child", "parent_run_id": "parent-run", "branch_point_event_id": "event-1"},
    }
    verify = {
        "causal_receipt": causal_receipt.recorded_verify(
            verify_id=verify_id,
            run_id=run_id,
            run_digest=causal_receipt.receipt_digest(run["causal_receipt"]),
        )
    }
    outcome = {
        "causal_receipt": causal_receipt.recorded_outcome(
            subject_id="outcome-lineage",
            parent_kind="verify",
            parent_id=verify_id,
            parent_digest=causal_receipt.receipt_digest(verify["causal_receipt"]),
        )
    }
    handoff = causal_receipt.recorded_handoff(
        handoff_id="handoff-lineage",
        parent_kind="outcome",
        parent_id="outcome-lineage",
        parent_digest=causal_receipt.receipt_digest(outcome["causal_receipt"]),
    )
    localio.write_json(run_dir / "plan.json", plan)
    localio.write_json(run_dir / "run.json", run)
    localio.write_json(verify_dir / "receipt.json", verify)
    (tmp_path / "memory" / "outcome").mkdir(parents=True)
    (tmp_path / "memory" / "outcome" / "records.jsonl").write_text(json.dumps(outcome) + "\n")
    handoff_path.write_text("private handoff body")
    localio.write_json(causal_receipt.handoff_sidecar_path(handoff_path), handoff)

    result = lineage(target=tmp_path, subject="handoff:handoff-lineage")

    assert [hop["status"] for hop in result["hops"]] == ["ok"] * 4
    assert [hop["relation"] for hop in result["hops"]] == [
        "handed_off_from",
        "captured_from",
        "executed_from",
        "planned_from",
    ]
    run_result = lineage(target=tmp_path, subject=f"run:{run_id}")
    assert run_result["run_lineage"] == {"parent_run_id": "parent-run", "branch_point_event_id": "event-1"}
    rendered = json.dumps(result)
    assert str(tmp_path) not in rendered
    assert "private handoff body" not in rendered


def test_lineage_reports_missing_and_digest_mismatch_and_legacy(tmp_path):
    run_dir = tmp_path / ".brigade" / "runs" / "run-a"
    run_dir.mkdir(parents=True)
    plan = {"causal_receipt": causal_receipt.recorded_plan(run_id="run-a")}
    run = {
        "causal_receipt": causal_receipt.recorded_run(run_id="run-a", plan_digest="0" * 64),
    }
    localio.write_json(run_dir / "plan.json", plan)
    localio.write_json(run_dir / "run.json", run)

    result = lineage(target=tmp_path, subject="run:run-a")
    assert [hop["status"] for hop in result["hops"]] == ["digest_mismatch"]

    broken_dir = tmp_path / ".brigade" / "runs" / "run-b"
    broken_dir.mkdir()
    localio.write_json(broken_dir / "plan.json", {"causal_receipt": causal_receipt.recorded_plan(run_id="run-b")})
    broken_receipt = causal_receipt.build_receipt(
        subject_kind="run",
        subject_id="run-b",
        parents=[
            causal_receipt.parent_ref(
                relation="planned_from",
                kind="plan",
                artifact_id="run-b",
                digest=causal_receipt.receipt_digest(causal_receipt.recorded_plan(run_id="run-b")),
            ),
            causal_receipt.parent_ref(relation="planned_from", kind="plan", artifact_id="missing-plan"),
        ],
    )
    localio.write_json(broken_dir / "run.json", {"causal_receipt": broken_receipt})
    broken = lineage(target=tmp_path, subject="run:run-b")
    assert [hop["status"] for hop in broken["hops"]] == ["ok", "broken"]

    legacy_dir = tmp_path / ".brigade" / "runs" / "legacy"
    legacy_dir.mkdir()
    localio.write_json(legacy_dir / "run.json", {"run_id": "legacy"})
    legacy = lineage(target=tmp_path, subject="run:legacy")
    assert legacy["message"] == "no lineage recorded"
    assert legacy["hops"] == []


def test_lineage_resolves_synthesis_parent_manifest_worker_results(tmp_path):
    run_id = "run-fan-in"
    run_dir = tmp_path / ".brigade" / "runs" / run_id
    run_dir.mkdir(parents=True)
    results = [
        {
            "worker": f"worker-seat-number-long-{index:02d}",
            "causal_receipt": causal_receipt.build_receipt(
                subject_kind="worker-result",
                subject_id=causal_receipt.worker_result_id(run_id, f"worker-seat-number-long-{index:02d}", index),
            ),
        }
        for index in range(1, 18)
    ]
    parents = causal_receipt.synthesis_worker_parents(run_id, results)
    synthesis = causal_receipt.recorded_synthesis(run_id=run_id, worker_parents=parents)
    localio.write_json(run_dir / "worker-results.json", {"results": results})
    localio.write_json(run_dir / "synthesis.json", {"causal_receipt": synthesis})
    manifest = causal_receipt.synthesis_parent_manifest(run_id=run_id, worker_parents=parents)
    assert manifest is not None
    localio.write_json(causal_receipt.parent_manifest_path(run_dir, manifest["id"]), manifest)

    result = lineage(target=tmp_path, subject=f"synthesis:{run_id}")

    assert len(result["hops"]) == 17
    assert {hop["status"] for hop in result["hops"]} == {"ok"}
    assert {hop["kind"] for hop in result["hops"]} == {"worker-result"}


def test_lineage_json_cli_is_versioned_and_path_free(tmp_path, capsys):
    run_dir = tmp_path / ".brigade" / "runs" / "run-cli"
    run_dir.mkdir(parents=True)
    localio.write_json(run_dir / "run.json", {"causal_receipt": causal_receipt.recorded_run(run_id="run-cli")})

    assert cli.main(["receipts", "lineage", "run:run-cli", "--target", str(tmp_path), "--json"]) == 0

    output = capsys.readouterr().out
    result = json.loads(output)
    assert result["schema"] == "brigade.receipts_lineage.v1"
    assert result["schema_version"] == 1
    assert [hop["status"] for hop in result["hops"]] == ["broken"]
    assert str(tmp_path) not in output
    assert not {"prompt", "model_output", "tool_arguments", "path"}.intersection(result)
