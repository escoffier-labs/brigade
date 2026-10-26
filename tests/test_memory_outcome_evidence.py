"""Verified card capture supplies reflection support without editing cards."""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade import cli, localio, outcome_cmd, verify_manifest, verify_trial
from brigade import memory_evidence_log as log
from brigade.work_cmd import verification as verify_mod
from tests.test_outcome_cmd import _write_run_receipt
from tests.test_memory_reflect import card, command, snapshot
from tests.test_verify_trial import _init_verify_git_repo, _run_manifest_verify, _track_workspace_manifest

pytestmark = pytest.mark.skipif(
    os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"),
    reason="safe directory descriptors unavailable",
)


@pytest.fixture
def verified_card(tmp_path, monkeypatch, request):
    monkeypatch.setattr(verify_mod.helpers, "_now", lambda: datetime(2026, 10, 20, 12, tzinfo=timezone.utc))
    _init_verify_git_repo(tmp_path)
    path = card(tmp_path, "cache")
    verify_manifest.write_workspace_manifest(
        tmp_path,
        {
            "schema": "brigade.verify_manifest.v1",
            "schema_version": 1,
            "manifest_id": "card-fixture",
            "binding_mode": "fixture_eval",
            "verifier_id": "brigade.verify.fixture",
            "subject": {
                "artifact_kind": "card",
                "artifact_id": "cache",
                "subject_path": "memory/cards/cache.md",
            },
            "fixture": {
                "manifest_id": "adversarial-failed-verification",
                "case_id": "report-failing-tests",
                "check_id": "fixture.echo-ok",
            },
            "checks": [
                {
                    "check_id": "fixture.echo-ok",
                    "check_role": getattr(request, "param", "effectiveness"),
                    "command": f"{sys.executable} -c \"print('ok')\"",
                }
            ],
        },
    )
    _track_workspace_manifest(tmp_path, "card-fixture")
    receipt = _run_manifest_verify(tmp_path, "card-fixture", monkeypatch=monkeypatch, start_session=False)
    assert verify_trial.project_trial(receipt, target=tmp_path).eligible
    return path, receipt


def capture(target, receipt, *, ident="cache", kind="card"):
    return cli.main(
        ["outcome", "capture", ident, "--kind", kind, "--target", str(target), "--run-id", receipt["run_id"]]
    )


def rewrite(receipt):
    receipt["digests"] = {
        "algorithm": "sha256",
        "logs": {},
        "receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"}),
    }
    (Path(receipt["path"]) / "receipt.json").write_text(json.dumps(receipt))


def test_verified_capture_reflects_one_proof_without_refreshing_or_writing_cards(tmp_path, verified_card, capsys):
    path, receipt = verified_card
    before = snapshot(tmp_path)
    for _ in range(2):
        assert capture(tmp_path, receipt) == 0
    rows, warnings, unsupported = log.read_evidence(tmp_path)
    assert not warnings and not unsupported
    ref = f".brigade/work/verify-runs/{receipt['run_id']}/receipt.json"
    assert rows
    assert {(r["evidence_id"], r["ts"], r["kind"]) for r in rows} == {(ref, receipt["completed_at"], "outcome")}
    capsys.readouterr()
    code, result = command(tmp_path, capsys, "run", "--dry-run")
    assert code == 0
    assert {c["kind"] for c in result["candidates"]} == {"reinforce", "stale-newer-evidence"}
    for candidate in result["candidates"]:
        assert candidate["proof_count"] == 1
        assert candidate["evidence_ids"] == [ref]
        assert candidate["evidence_sources"] == {ref: ".brigade/work/verify-runs"}
    assert f"+last_consolidated: {receipt['completed_at']}" in result["candidates"][0]["diff"]
    assert snapshot(tmp_path) == before
    assert path.read_bytes() == before["memory/cards/cache.md"][0]


@pytest.mark.parametrize(
    "case",
    [
        "caller",
        "skill",
        "changed",
        "missing",
        "symlink",
        "reused",
        "audit",
        "failed",
        "fingerprint",
        "kind",
        "id",
    ],
)
def test_capture_rejects_non_supporting_receipts(tmp_path, verified_card, case):
    path, receipt = verified_card
    ident, kind = "cache", "card"
    if case == "caller":
        card(tmp_path, "other")
        ident = "other"
    elif case == "skill":
        kind = "skill"
    elif case == "changed":
        path.write_text(path.read_text() + "Changed claim.\n")
    elif case == "missing":
        path.unlink()
    elif case == "symlink":
        other = tmp_path / "other.md"
        other.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(other)
    elif case == "reused":
        receipt["reused_from"] = receipt["run_id"]
    elif case == "audit":
        receipt.pop("subject_binding")
    elif case == "failed":
        receipt["status"] = "failed"
        receipt["commands"][0].update(status="failed", exit_code=1)
    else:
        receipt["subject_binding"][
            {"fingerprint": "content_fingerprint", "kind": "artifact_kind", "id": "artifact_id"}[case]
        ] = "other"
    rewrite(receipt)
    before = snapshot(tmp_path)
    assert capture(tmp_path, receipt, ident=ident, kind=kind) == 0
    assert log.read_evidence(tmp_path)[0] == []
    assert snapshot(tmp_path) == before


def test_optional_append_failure_warns_after_capture_and_retry_supplies_evidence(
    tmp_path, verified_card, monkeypatch, capsys
):
    _, receipt = verified_card
    before = snapshot(tmp_path)
    capsys.readouterr()
    with monkeypatch.context() as patch:

        def fail(*args, **kwargs):
            raise OSError("private failure " + "x" * 2000)

        patch.setattr(log, "append_evidence", fail)
        assert capture(tmp_path, receipt) == 0
    output = capsys.readouterr()
    assert "warning: evidence log append failed" in output.err
    assert len(output.err) < 400
    assert len(outcome_cmd.load_records(tmp_path)) == 1
    assert log.read_evidence(tmp_path)[0] == []
    assert capture(tmp_path, receipt) == 0
    assert len(log.read_evidence(tmp_path)[0]) == 1
    assert snapshot(tmp_path) == before


def test_failed_outcome_append_never_supplies_evidence(tmp_path, verified_card, monkeypatch):
    _, receipt = verified_card

    def fail(*args, **kwargs):
        raise outcome_cmd.OutcomeLedgerError("ledger unavailable")

    monkeypatch.setattr(outcome_cmd, "append_records", fail)
    assert capture(tmp_path, receipt) == 1
    assert log.read_evidence(tmp_path)[0] == []


@pytest.mark.parametrize("verified_card", ["utility_guardrail"], indirect=True)
def test_eligible_verification_without_effectiveness_is_not_support(tmp_path, verified_card):
    _, receipt = verified_card
    assert verify_trial.project_trial(receipt, target=tmp_path).eligible
    assert capture(tmp_path, receipt) == 0
    assert log.read_evidence(tmp_path)[0] == []


@pytest.mark.parametrize("source", ["run", "manual"])
def test_non_verify_card_signals_do_not_supply_support(tmp_path, verified_card, source):
    _, receipt = verified_card
    before = snapshot(tmp_path)
    if source == "run":
        _write_run_receipt(tmp_path, run_id="card-run", status="ok")
        assert (
            outcome_cmd.capture(target=tmp_path, artifact_id="cache", artifact_kind="card", run_receipt="card-run") == 0
        )
    else:
        assert (
            outcome_cmd.record(
                target=tmp_path,
                artifact_id="cache",
                artifact_kind="card",
                source="friction",
                status="cleared",
                evidence_ref=str(Path(receipt["path"]) / "receipt.json"),
            )
            == 0
        )
    assert log.read_evidence(tmp_path)[0] == []
    assert snapshot(tmp_path) == before
