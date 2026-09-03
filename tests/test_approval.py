"""Signed human approval events bound to run trees and verify receipts."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from brigade import attestation, cli, localio, run_audit, run_events, run_journal

if not shutil.which("ssh-keygen"):
    pytest.skip("ssh-keygen is required for approval tests", allow_module_level=True)


RUN_ID = "approval-run-001"
VERIFY_ID = "20260903-120000-work-verify-approval"
TREE = "1" * 40
PATCH = "2" * 64


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_verify_receipt(target: Path, *, producer_key: Path | None = None) -> dict[str, Any]:
    receipt_dir = target / ".brigade" / "work" / "verify-runs" / VERIFY_ID
    receipt_dir.mkdir(parents=True)
    stdout = receipt_dir / "command-1-stdout.log"
    stderr = receipt_dir / "command-1-stderr.log"
    stdout.write_text("test output that must not be approved\n", encoding="utf-8")
    stderr.write_text("stderr that must not be approved\n", encoding="utf-8")
    receipt: dict[str, Any] = {
        "schema_version": 2,
        "run_id": VERIFY_ID,
        "producer_run_id": RUN_ID,
        "target": str(target),
        "status": "completed",
        "started_at": "2026-09-03T12:00:00Z",
        "completed_at": "2026-09-03T12:00:05Z",
        "tree_fingerprint": TREE,
        "changes_patch_sha256": PATCH,
        "commands": [
            {
                "command": "SECRET_TOKEN=do-not-copy pytest -q",
                "status": "completed",
                "exit_code": 0,
                "stdout_log_path": str(stdout),
                "stderr_log_path": str(stderr),
                "env": ["SECRET_TOKEN=do-not-copy"],
            }
        ],
    }
    receipt["digests"] = {
        "algorithm": "sha256",
        "logs": {
            stdout.name: localio.file_sha256(stdout),
            stderr.name: localio.file_sha256(stderr),
        },
        "receipt_sha256": localio.canonical_json_digest(receipt, exclude_keys={"digests"}),
    }
    _write_json(receipt_dir / "receipt.json", receipt)
    if producer_key is not None:
        envelope = attestation.export_attestation(receipt, producer_key)
        _write_json(receipt_dir / "attestation.json", envelope)
    return receipt


def _workspace(
    tmp_path: Path,
    *,
    with_receipt: bool = True,
    producer_is_approver: bool = False,
    requester_principal: str | None = None,
) -> tuple[Path, Path, Path]:
    target = tmp_path / "workspace"
    target.mkdir()
    key, signers = attestation.keygen(target, principal="alice")
    run_dir = target / ".brigade" / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    created = run_events.build_event(
        run_id=RUN_ID,
        sequence=1,
        event_type="run.created",
        payload={"status": "started"},
        idempotency_key="created",
        recorded_at="2026-09-03T11:59:00.000000Z",
        previous_digest=None,
    )
    journal = run_dir / "events" / "lifecycle.jsonl"
    journal.parent.mkdir()
    journal.write_bytes(run_events.canonical_bytes(created) + b"\n")
    run_meta: dict[str, Any] = {
        "schema": "brigade.run.v1",
        "schema_version": 1,
        "status": "started",
        "tree_fingerprint": TREE,
        "journal_present": True,
        "journal_last_sequence": 1,
        "journal_last_event_digest": created["event_digest"],
    }
    if requester_principal is not None:
        run_meta["requester_principal"] = requester_principal
    _write_json(run_dir / "run.json", run_meta)
    if with_receipt:
        _write_verify_receipt(target, producer_key=key if producer_is_approver else None)
    return target, key, signers


def _approve(target: Path, key: Path, *extra: str) -> int:
    return cli.main(
        [
            "run",
            "approve",
            RUN_ID,
            "--decision",
            "allow",
            "--reason-code",
            "reviewed-tests",
            "--reason",
            "Reviewed the focused test receipt.",
            "--key",
            str(key),
            "--target",
            str(target),
            *extra,
        ]
    )


def _approval_event(target: Path):
    journal = target / ".brigade" / "runs" / RUN_ID / "events" / "lifecycle.jsonl"
    report = run_journal.read_journal(journal)
    assert report.chain_errors == []
    return report.events[-1]


def _statement(target: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    event = _approval_event(target)
    path = target / ".brigade" / "runs" / RUN_ID / event.payload["attestation_path"]
    envelope = json.loads(path.read_text(encoding="utf-8"))
    return envelope, json.loads(base64.b64decode(envelope["payload"]))


def test_allow_records_event_envelope_projection_and_verifies(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path)

    assert _approve(target, key) == 0
    approved = capsys.readouterr()
    assert "PASSED" in approved.out

    event = _approval_event(target)
    assert event.event_type == "approval"
    assert event.payload["decision"] == "allow"
    assert event.payload["scope"] == "run"
    assert event.payload["approver_principal"] == "alice"
    assert event.payload["subject_tree"] == TREE
    assert event.payload["attestation_path"] == f"approvals/{event.payload['nonce']}.json"

    envelope, statement = _statement(target)
    assert envelope["brigade"] == {
        "profile": attestation.ATTESTATION_PROFILE,
        "namespace": attestation.ATTESTATION_NAMESPACE,
    }
    assert statement["subject"] == [
        {"name": "git:tree", "digest": {"gitTree": TREE}},
        {"name": f"verify:{VERIFY_ID}", "digest": {"sha256": _write_receipt_digest(target)}},
    ]
    predicate = statement["predicate"]
    assert predicate["decision"] == "allow"
    assert predicate["approver"]["kind"] == "human"
    assert predicate["policy"]["name"] == "brigade.sod.v1"
    assert len(predicate["nonce"]) == 32

    run_meta = json.loads((target / ".brigade" / "runs" / RUN_ID / "run.json").read_text())
    assert run_meta["approval"]["decision"] == "allow"
    assert run_meta["approval"]["sod"]["result"] == "PASSED"
    assert {check["id"] for check in run_meta["approval"]["sod"]["checks"]} >= {
        "approver-is-human",
        "approver-not-producer",
        "requester-unknown",
    }

    assert cli.main(["receipts", "verify", "--target", str(target)]) == 0
    assert "APPROVED" in capsys.readouterr().out


def _write_receipt_digest(target: Path) -> str:
    receipt = json.loads((target / ".brigade" / "work" / "verify-runs" / VERIFY_ID / "receipt.json").read_text())
    return receipt["digests"]["receipt_sha256"]


def test_approver_cannot_be_a_verify_receipt_producer(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path, producer_is_approver=True)

    assert _approve(target, key) == 3
    assert "approver-not-producer: failed" in capsys.readouterr().out
    assert _approval_event(target).event_type == "approval"

    assert cli.main(["receipts", "verify", "--target", str(target)]) != 0
    assert "SOD-VIOLATION" in capsys.readouterr().out


def test_tree_change_invalidates_approval(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path)
    assert _approve(target, key) == 0
    capsys.readouterr()
    run_path = target / ".brigade" / "runs" / RUN_ID / "run.json"
    run_meta = json.loads(run_path.read_text())
    run_meta["tree_fingerprint"] = "9" * 40
    _write_json(run_path, run_meta)

    assert cli.main(["receipts", "verify", "--target", str(target)]) != 0
    assert "APPROVAL-INVALID" in capsys.readouterr().out


def test_expired_allow_is_reported_without_changing_verify_exit(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path)
    assert _approve(target, key, "--expires-in", "0s") == 3
    capsys.readouterr()

    assert cli.main(["receipts", "verify", "--target", str(target)]) == 0
    assert "APPROVAL-EXPIRED" in capsys.readouterr().out


def test_allow_without_verify_receipts_is_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path, with_receipt=False)

    assert _approve(target, key) == 2
    captured = capsys.readouterr()
    assert "allow requires at least one verify receipt" in captured.err
    assert _approval_event(target).event_type == "run.created"


@pytest.mark.parametrize(("decision", "status"), [("deny", "DENIED"), ("hold", "HELD")])
def test_deny_and_hold_need_no_receipts_and_do_not_fail_verify(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    decision: str,
    status: str,
):
    target, key, _signers = _workspace(tmp_path, with_receipt=False)
    argv = [
        "run",
        "approve",
        RUN_ID,
        "--decision",
        decision,
        "--reason-code",
        "needs-rework",
        "--key",
        str(key),
        "--target",
        str(target),
    ]
    assert cli.main(argv) == 0
    capsys.readouterr()

    assert cli.main(["receipts", "verify", "--target", str(target)]) == 0
    assert status in capsys.readouterr().out


def test_envelope_reverifies_with_openssh_and_omits_private_inputs(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, signers = _workspace(tmp_path)
    assert _approve(target, key) == 0
    capsys.readouterr()
    envelope, _statement_payload = _statement(target)
    envelope_text = json.dumps(envelope, sort_keys=True)
    for forbidden in (str(tmp_path), "SECRET_TOKEN", "do-not-copy", "test output", "stderr that"):
        assert forbidden not in envelope_text

    signature = base64.b64decode(envelope["signatures"][0]["sig"])
    sig_path = tmp_path / "approval.sig"
    sig_path.write_bytes(signature)
    pae = attestation.dsse_pae(envelope["payloadType"], base64.b64decode(envelope["payload"]))
    verified = subprocess.run(
        [
            "ssh-keygen",
            "-Y",
            "verify",
            "-f",
            str(signers),
            "-I",
            "alice",
            "-n",
            attestation.ATTESTATION_NAMESPACE,
            "-s",
            str(sig_path),
        ],
        input=pae,
        capture_output=True,
    )
    assert verified.returncode == 0, verified.stderr.decode(errors="replace")


def test_run_audit_receipt_includes_approval_decision_and_sod(tmp_path: Path):
    target, key, _signers = _workspace(tmp_path)
    assert _approve(target, key) == 0

    report = run_audit.audit_run(target / ".brigade" / "runs" / RUN_ID)
    assert report.result == run_audit.RESULT_MATCH
    assert report.to_receipt()["approval"] == {
        "decision": "allow",
        "sod": json.loads((target / ".brigade" / "runs" / RUN_ID / "run.json").read_text())["approval"]["sod"],
    }


def test_runs_show_prints_the_approval_projection(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path)
    assert _approve(target, key) == 0
    capsys.readouterr()

    assert cli.main(["runs", "show", RUN_ID, "--cwd", str(target)]) == 0
    shown = capsys.readouterr().out
    assert "approval: allow" in shown
    assert "approval sod: PASSED" in shown


def test_requester_cannot_approve_their_own_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path, requester_principal="alice")

    assert _approve(target, key) == 3
    assert "approver-not-requester: failed" in capsys.readouterr().out
    assert _approval_event(target).event_type == "approval"


def test_reason_refuses_absolute_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path)

    assert _approve(target, key, "--reason", f"Reviewed {tmp_path / 'private.txt'}") == 2
    assert "absolute paths" in capsys.readouterr().err
    assert _approval_event(target).event_type == "run.created"
