"""Filesystem-backed tests for the receipts lineage command."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from brigade import causal_receipt, cli, localio, receipts_lineage, run_receipts
from brigade.run_transport import WorkerResult
from brigade.receipts_lineage import lineage


def test_lineage_resolves_recorded_chain_without_exposing_paths_or_payloads(tmp_path):
    run_id = "run-lineage"
    run_dir = tmp_path / ".brigade" / "runs" / run_id
    verify_id = "verify-lineage"
    verify_dir = tmp_path / ".brigade" / "work" / "verify-runs" / verify_id
    handoff_path = tmp_path / ".claude" / "memory-handoffs" / "handoff-lineage.md"
    run_dir.mkdir(parents=True)
    verify_dir.mkdir(parents=True)
    handoff_path.parent.mkdir(parents=True)

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

    before = _snapshot(tmp_path)
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
    assert _snapshot(tmp_path) == before


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


def test_lineage_resolves_synthesis_parent_manifest_worker_results(tmp_path, monkeypatch):
    _ordered_glob(monkeypatch)
    run_id = "run-fan-in"
    run_dir = tmp_path / ".brigade" / "runs" / run_id
    run_dir.mkdir(parents=True)
    # Use the production serializer, including a payload larger than the generic read cap.
    results = run_receipts.worker_payload(
        [
            WorkerResult(
                worker=f"worker-seat-number-long-{index:02d}",
                task="review",
                text="x" * 100_000,
                detail="x" * 100_000,
                ok=True,
            )
            for index in range(1, 18)
        ]
    )
    assert len(json.dumps(results).encode()) > receipts_lineage.MAX_ARTIFACT_BYTES
    # Direct manifest lookup must work even after the old directory scan limit.
    for index in range(501):
        (run_dir.parent / f"other-{index}").mkdir()
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
    forbidden = set()
    causal_receipt._collect_forbidden_keys(result, found=forbidden)
    assert not forbidden
    assert '"path":' not in output


def _snapshot(root):
    return {
        str(path.relative_to(root)): (path.stat().st_mtime_ns, path.read_bytes() if path.is_file() else None)
        for path in root.rglob("*")
    }


@pytest.mark.parametrize(
    "inbox,config",
    [
        (".codex/memory-handoffs", None),
        ("custom-inbox", "dogfood"),
        ("custom-run-inbox", "run"),
    ],
)
def test_lineage_finds_writer_and_configured_handoff_inboxes(tmp_path, inbox, config):
    handoff = tmp_path / inbox / "note.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("private body")
    localio.write_json(
        causal_receipt.handoff_sidecar_path(handoff),
        causal_receipt.recorded_handoff(handoff_id="note", parent_kind="run", parent_id="run-a"),
    )
    run_dir = tmp_path / ".brigade" / "runs" / "run-a"
    run_dir.mkdir(parents=True)
    localio.write_json(
        run_dir / "run.json",
        {
            "causal_receipt": causal_receipt.build_receipt(subject_kind="run", subject_id="run-a"),
            "handoff_inbox": str(handoff.parent) if config == "run" else None,
        },
    )
    if config == "dogfood":
        (tmp_path / ".brigade" / "dogfood.toml").write_text('handoff_inbox = "custom-inbox"\n')
    assert [hop["status"] for hop in lineage(target=tmp_path, subject="handoff:note")["hops"]] == ["ok"]


def test_lineage_streams_large_outcome_ledger_with_bounded_lines(tmp_path):
    path = tmp_path / "memory" / "outcome" / "records.jsonl"
    path.parent.mkdir(parents=True)
    record = {
        "causal_receipt": causal_receipt.recorded_outcome(
            subject_id="outcome-a", parent_kind="verify", parent_id="verify-a"
        )
    }
    # Include an oversized malformed row, many ordinary rows, and the requested row.
    path.write_bytes(
        b"x" * (receipts_lineage.MAX_ARTIFACT_BYTES + 1)
        + b"\n"
        + (json.dumps({"id": "unrelated", "detail": "x" * 1800}) + "\n").encode() * 3300
        + (json.dumps(record) + "\n").encode()
    )
    verify = tmp_path / ".brigade" / "work" / "verify-runs" / "verify-a" / "receipt.json"
    localio.write_json(
        verify, {"causal_receipt": causal_receipt.build_receipt(subject_kind="verify", subject_id="verify-a")}
    )
    assert [hop["status"] for hop in lineage(target=tmp_path, subject="outcome:outcome-a")["hops"]] == ["ok"]


@pytest.mark.parametrize("kind", ["verify", "outcome", "handoff"])
def test_lineage_distinguishes_legacy_artifact_from_missing_id(tmp_path, capsys, kind):
    verify = tmp_path / ".brigade" / "work" / "verify-runs" / "legacy" / "receipt.json"
    localio.write_json(verify, {"run_id": "legacy"})
    outcome = tmp_path / "memory" / "outcome" / "records.jsonl"
    outcome.parent.mkdir(parents=True)
    outcome.write_text(
        json.dumps(
            {
                "artifact_id": "skill-a",
                "source": "verify",
                "evidence_ref": ".brigade/work/verify-runs/legacy/receipt.json",
            }
        )
        + "\n"
    )
    handoff = tmp_path / ".claude" / "memory-handoffs" / "legacy.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("legacy private body")
    localio.write_json(
        causal_receipt.handoff_sidecar_path(handoff.with_name("unrelated.md")),
        causal_receipt.build_receipt(subject_kind="handoff", subject_id="unrelated"),
    )
    before = _snapshot(tmp_path)
    assert (
        cli.main(
            [
                "receipts",
                "lineage",
                f"{kind}:{'legacy--skill-a' if kind == 'outcome' else 'legacy'}",
                "--target",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert "no lineage recorded" in capsys.readouterr().out
    assert cli.main(["receipts", "lineage", f"{kind}:missing", "--target", str(tmp_path)]) == 2
    assert "receipt not found" in capsys.readouterr().err
    assert _snapshot(tmp_path) == before


def test_legacy_child_run_keeps_branch_lineage(tmp_path):
    path = tmp_path / ".brigade" / "runs" / "legacy" / "run.json"
    localio.write_json(
        path, {"lineage": {"kind": "child", "parent_run_id": "parent", "branch_point_event_id": "event-1"}}
    )
    before = _snapshot(tmp_path)
    result = lineage(target=tmp_path, subject="run:legacy")
    assert result["run_lineage"] == {"parent_run_id": "parent", "branch_point_event_id": "event-1"}
    assert result["message"] == "no lineage recorded"
    assert _snapshot(tmp_path) == before


def test_lineage_continues_deeper_after_broken_and_mismatched_hops(tmp_path):
    run_dir = tmp_path / ".brigade" / "runs" / "run-a"
    localio.write_json(run_dir / "plan.json", {"causal_receipt": causal_receipt.recorded_plan(run_id="run-a")})
    localio.write_json(run_dir / "run.json", {"causal_receipt": causal_receipt.recorded_run(run_id="run-a")})
    parents = [
        causal_receipt.parent_ref(relation="executed_from", kind="run", artifact_id="missing"),
        causal_receipt.parent_ref(relation="executed_from", kind="run", artifact_id="run-a", digest="0" * 64),
        causal_receipt.parent_ref(relation="executed_from", kind="run", artifact_id="run-a"),
    ]
    localio.write_json(
        tmp_path / ".brigade" / "work" / "verify-runs" / "verify-a" / "receipt.json",
        {"causal_receipt": causal_receipt.build_receipt(subject_kind="verify", subject_id="verify-a", parents=parents)},
    )
    assert [hop["status"] for hop in lineage(target=tmp_path, subject="verify:verify-a")["hops"]] == [
        "broken",
        "digest_mismatch",
        "ok",
        "ok",
    ]


def test_lineage_text_shows_diagnostics_and_truncation(tmp_path, capsys, monkeypatch):
    path = tmp_path / ".brigade" / "runs" / "run-a" / "run.json"
    localio.write_json(path, {"causal_receipt": causal_receipt.recorded_run(run_id="run-a")})
    monkeypatch.setattr(receipts_lineage, "MAX_RESOLUTIONS", 1)
    assert cli.main(["receipts", "lineage", "run:run-a", "--target", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "broken lineage:" in output
    assert "truncated" in output


def test_verify_lookup_uses_requested_directory_beyond_scan_limit(tmp_path, monkeypatch):
    _ordered_glob(monkeypatch)
    root = tmp_path / ".brigade" / "work" / "verify-runs"
    for index in range(501):
        localio.write_json(root / f"other-{index}" / "receipt.json", {"run_id": f"other-{index}"})
    localio.write_json(
        root / "verify-a" / "receipt.json",
        {"causal_receipt": causal_receipt.recorded_verify(verify_id="verify-a", run_id="run-a")},
    )
    localio.write_json(
        tmp_path / ".brigade" / "runs" / "run-a" / "run.json",
        {"causal_receipt": causal_receipt.build_receipt(subject_kind="run", subject_id="run-a")},
    )
    assert [hop["status"] for hop in lineage(target=tmp_path, subject="verify:verify-a")["hops"]] == ["ok"]


def _ordered_glob(monkeypatch):
    # Glob order is unspecified. Put the requested artifact after unrelated entries.
    original = Path.glob
    monkeypatch.setattr(Path, "glob", lambda root, pattern: iter(sorted(original(root, pattern))))


@pytest.mark.parametrize("kind", sorted(causal_receipt.ARTIFACT_KINDS))
@pytest.mark.parametrize("mismatch", ["kind", "id"])
def test_lineage_rejects_receipts_bound_to_another_subject(tmp_path, capsys, kind, mismatch):
    artifact_id = "run-a:w:1" if kind == "worker-result" else "artifact-a"
    subject = {"kind": kind, "id": artifact_id}
    subject[mismatch] = ("run" if kind == "plan" else "plan") if mismatch == "kind" else "other-id"
    receipt = causal_receipt.build_receipt(
        subject_kind=subject["kind"],
        subject_id=subject["id"],
        parents=[causal_receipt.parent_ref(relation="planned_from", kind="plan", artifact_id="witness")],
    )
    runs = tmp_path / ".brigade" / "runs"
    localio.write_json(
        runs / "witness" / "plan.json", {"causal_receipt": causal_receipt.recorded_plan(run_id="witness")}
    )
    payload = {"causal_receipt": receipt}
    if kind == "worker-result":
        path = runs / "run-a" / "worker-results.json"
        payload = {"results": [{"worker": "w", "causal_receipt": receipt}]}
    elif kind in {"plan", "run", "synthesis"}:
        path = runs / artifact_id / f"{kind}.json"
    elif kind == "verify":
        path = tmp_path / ".brigade" / "work" / "verify-runs" / artifact_id / "receipt.json"
    elif kind == "outcome":
        path = tmp_path / "memory" / "outcome" / "records.jsonl"
    else:
        path = causal_receipt.handoff_sidecar_path(tmp_path / ".claude" / "memory-handoffs" / f"{artifact_id}.md")
        payload = receipt
    if kind == "outcome":
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(payload) + "\n")
    else:
        localio.write_json(path, payload)
    probe = causal_receipt.build_receipt(
        subject_kind="synthesis",
        subject_id="probe",
        parents=[causal_receipt.parent_ref(relation="synthesized_from", kind=kind, artifact_id=artifact_id)],
    )
    localio.write_json(runs / "probe" / "synthesis.json", {"causal_receipt": probe})

    result = lineage(target=tmp_path, subject="synthesis:probe")

    assert [hop["status"] for hop in result["hops"]] == ["broken"]
    code = cli.main(["receipts", "lineage", f"{kind}:{artifact_id}", "--target", str(tmp_path), "--json"])
    output = capsys.readouterr()
    if code == 0:
        assert json.loads(output.out)["hops"] == []
    else:
        assert code == 2
        assert "receipt not found" in output.err


def test_lineage_preserves_worker_prefix_above_scan_cap(tmp_path, monkeypatch):
    run_id = "run-a"
    run_dir = tmp_path / ".brigade" / "runs" / run_id
    results = run_receipts.worker_payload(
        [WorkerResult(worker="w", task="review", text="done", ok=True) for _ in range(501)]
    )
    parents = causal_receipt.synthesis_worker_parents(run_id, results)
    synthesis = causal_receipt.recorded_synthesis(run_id=run_id, worker_parents=parents)
    manifest = causal_receipt.synthesis_parent_manifest(run_id=run_id, worker_parents=parents)
    assert manifest is not None
    localio.write_json(run_dir / "worker-results.json", {"results": results})
    localio.write_json(run_dir / "synthesis.json", {"causal_receipt": synthesis})
    localio.write_json(causal_receipt.parent_manifest_path(run_dir, manifest["id"]), manifest)
    # Expose the entire permitted scan prefix instead of the smaller default hop cap.
    monkeypatch.setattr(receipts_lineage, "MAX_HOPS", 500)
    before = _snapshot(tmp_path)

    result = lineage(target=tmp_path, subject=f"synthesis:{run_id}")

    assert [hop["id"] for hop in result["hops"]] == [f"run-a:w:{ordinal}" for ordinal in range(1, 501)]
    assert {hop["status"] for hop in result["hops"]} == {"ok"}
    assert result["truncated"] is True
    # A direct lookup also exposes the scan limit even when its hop count is small.
    worker = lineage(target=tmp_path, subject="worker-result:run-a:w:500")
    assert worker["truncated"] is True
    assert _snapshot(tmp_path) == before
