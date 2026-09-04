"""Signed human approval events bound to run trees and portable Test Results."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from brigade import (
    agent_request,
    approval,
    approval_v2,
    attestation,
    cli,
    localio,
    run_audit,
    run_events,
    run_journal,
)

if not shutil.which("ssh-keygen"):
    pytest.skip("ssh-keygen is required for approval tests", allow_module_level=True)


RUN_ID = "approval-run-001"
VERIFY_ID = "20260903-120000-work-verify-approval"
TREE = "1" * 40
BASELINE = "3" * 40
PATCH_BYTES = b"diff --git a/example.txt b/example.txt\n"
PATCH = hashlib.sha256(PATCH_BYTES).hexdigest()


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_verify_receipt(
    target: Path,
    *,
    verify_id: str = VERIFY_ID,
    producer_run_id: str = RUN_ID,
    producer_key: Path | None = None,
    tree_fingerprint: str = TREE,
    baseline_commit: str = BASELINE,
    patch_bytes: bytes = PATCH_BYTES,
    changes_patch_sha256: str | None = None,
    hmac_keyid: str | None = None,
) -> dict[str, Any]:
    receipt_dir = target / ".brigade" / "work" / "verify-runs" / verify_id
    receipt_dir.mkdir(parents=True, exist_ok=True)
    stdout = receipt_dir / "command-1-stdout.log"
    stderr = receipt_dir / "command-1-stderr.log"
    stdout.write_text("test output that must not be approved\n", encoding="utf-8")
    stderr.write_text("stderr that must not be approved\n", encoding="utf-8")
    (receipt_dir / "changes.patch").write_bytes(patch_bytes)
    receipt: dict[str, Any] = {
        "schema_version": 2,
        "run_id": verify_id,
        "producer_run_id": producer_run_id,
        "target": str(target),
        "status": "completed",
        "started_at": "2026-09-03T12:00:00Z",
        "completed_at": "2026-09-03T12:00:05Z",
        "baseline_commit": baseline_commit,
        "tree_fingerprint": tree_fingerprint,
        "changes_patch_sha256": changes_patch_sha256 or hashlib.sha256(patch_bytes).hexdigest(),
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
    if hmac_keyid is not None:
        receipt["digests"]["key_id"] = hmac_keyid
    _write_json(receipt_dir / "receipt.json", receipt)
    if producer_key is not None:
        envelope = attestation.export_attestation(receipt, producer_key)
        _write_json(receipt_dir / "attestation.json", envelope)
    return receipt


def _resign_verify_receipt(target: Path, key: Path, *, verify_id: str = VERIFY_ID) -> None:
    receipt_dir = target / ".brigade" / "work" / "verify-runs" / verify_id
    receipt = json.loads((receipt_dir / "receipt.json").read_text(encoding="utf-8"))
    _write_json(receipt_dir / "attestation.json", attestation.export_attestation(receipt, key))


def _append_signed_request(
    target: Path,
    run_dir: Path,
    *,
    run_id: str,
    key: Path,
    principal: str,
    nonce: str = "a" * 32,
    requested_at: str = "2026-09-03T11:59:30.000000Z",
) -> dict[str, str]:
    statement = agent_request.build_statement(
        run_id=run_id,
        baseline_commit=BASELINE,
        task="private task",
        requested_at=requested_at,
        nonce=nonce,
    )
    envelope = attestation.create_envelope(statement, key)
    relative_path = f"requests/{nonce}.json"
    _write_json(run_dir / relative_path, envelope)
    payload = agent_request.event_payload(
        requester_principal=principal,
        requester_keyid=attestation.get_key_fingerprint(key),
        baseline_commit=BASELINE,
        task="private task",
        nonce=nonce,
        statement_sha256=hashlib.sha256(attestation.canonical_statement_bytes(statement)).hexdigest(),
        attestation_path=relative_path,
    )
    report = run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl")
    run_journal.append_event(
        run_dir / "events" / "lifecycle.jsonl",
        run_id=run_id,
        event_type="request.signed",
        payload=payload,
        idempotency_key=f"request:{nonce}",
        expected_previous_sequence=report.events[-1].sequence,
        recorded_at=requested_at,
    )
    return payload


def _workspace(
    tmp_path: Path,
    *,
    run_id: str = RUN_ID,
    with_receipt: bool = True,
    producer_is_approver: bool = False,
    requester_principal: str | None = "requester",
    requester_uses_approver_key: bool = False,
) -> tuple[Path, Path, Path]:
    target = tmp_path / "workspace"
    target.mkdir()
    _workspace_key, signers = attestation.keygen(target, principal="workspace")
    approver_home = tmp_path / "approver"
    key, approver_signers = attestation.keygen(approver_home, principal="alice")
    signers.write_text(
        signers.read_text(encoding="utf-8") + approver_signers.read_text(encoding="utf-8"), encoding="utf-8"
    )
    verifier_home = tmp_path / "verifier"
    verifier_key, verifier_signers = attestation.keygen(verifier_home, principal="verifier")
    requester_home = tmp_path / "requester"
    requester_key, requester_signers = attestation.keygen(
        requester_home,
        principal=requester_principal or "unused-requester",
    )
    signers.write_text(
        signers.read_text(encoding="utf-8")
        + verifier_signers.read_text(encoding="utf-8")
        + requester_signers.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    if requester_principal is not None and requester_uses_approver_key:
        key_type, key_data, *_ = key.with_suffix(".pub").read_text(encoding="utf-8").split()
        with signers.open("a", encoding="utf-8") as handle:
            handle.write(
                f'{requester_principal} namespaces="{attestation.ATTESTATION_NAMESPACE}" {key_type} {key_data}\n'
            )
    run_dir = target / ".brigade" / "runs" / run_id
    run_dir.mkdir(parents=True)
    created = run_events.build_event(
        run_id=run_id,
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
    request = (
        _append_signed_request(
            target,
            run_dir,
            run_id=run_id,
            key=key if requester_uses_approver_key else requester_key,
            principal=requester_principal,
        )
        if requester_principal is not None
        else None
    )
    report = run_journal.read_journal(journal)
    run_meta: dict[str, Any] = {
        "schema": "brigade.run.v1",
        "schema_version": 1,
        "status": "started",
        "tree_fingerprint": TREE,
        "journal_present": True,
        "journal_last_sequence": report.events[-1].sequence,
        "journal_last_event_digest": report.events[-1].event_digest,
    }
    if request is not None:
        run_meta["requester_principal"] = request["requester_principal"]
        run_meta["requester_keyid"] = request["requester_keyid"]
        run_meta["request"] = request
    _write_json(run_dir / "run.json", run_meta)
    if with_receipt:
        _write_verify_receipt(
            target,
            producer_run_id=run_id,
            producer_key=key if producer_is_approver else verifier_key,
        )
    return target, key, signers


def _approve(target: Path, key: Path, *extra: str, run_id: str = RUN_ID) -> int:
    return cli.main(
        [
            "run",
            "approve",
            run_id,
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


def _approval_event(target: Path, run_id: str = RUN_ID):
    journal = target / ".brigade" / "runs" / run_id / "events" / "lifecycle.jsonl"
    report = run_journal.read_journal(journal)
    assert report.chain_errors == []
    return report.events[-1]


def _statement(target: Path, run_id: str = RUN_ID) -> tuple[dict[str, Any], dict[str, Any]]:
    event = _approval_event(target, run_id=run_id)
    path = target / ".brigade" / "runs" / run_id / event.payload["attestation_path"]
    envelope = json.loads(path.read_text(encoding="utf-8"))
    return envelope, json.loads(base64.b64decode(envelope["payload"]))


def _record_v1_approval(target: Path, key: Path, *, run_id: str = RUN_ID) -> None:
    run_dir = target / ".brigade" / "runs" / run_id
    report = run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl")
    receipts = approval.collect_verify_receipts(target, run_id, tree_fingerprint=TREE)
    nonce = "c" * 32
    keyid = attestation.get_key_fingerprint(key)
    statement = approval.build_statement(
        run_id=run_id,
        journal_chain_head=report.events[-1].event_digest,
        tree_fingerprint=TREE,
        receipts=receipts,
        decision="allow",
        scope="run",
        principal="alice",
        keyid=keyid,
        decided_at="2026-09-03T12:01:00.000000Z",
        expires_at="2030-09-03T12:01:00.000000Z",
        nonce=nonce,
        reason_code="reviewed-tests",
        reason="Reviewed the focused test receipt.",
    )
    envelope = attestation.create_envelope(statement, key)
    relative_path = f"approvals/{nonce}.json"
    _write_json(run_dir / relative_path, envelope)
    run_journal.append_event(
        run_dir / "events" / "lifecycle.jsonl",
        run_id=run_id,
        event_type="approval",
        payload={
            "decision": "allow",
            "scope": "run",
            "approver_principal": "alice",
            "approver_keyid": keyid,
            "subject_tree": TREE,
            "nonce": nonce,
            "expires_at": "2030-09-03T12:01:00.000000Z",
            "statement_sha256": hashlib.sha256(attestation.canonical_statement_bytes(statement)).hexdigest(),
            "attestation_path": relative_path,
            "producer_keyids": sorted(
                {producer_keyid for receipt in receipts for producer_keyid in receipt.signing_keyids}
            ),
        },
        idempotency_key=f"approval:{nonce}",
        expected_previous_sequence=report.events[-1].sequence,
        recorded_at="2026-09-03T12:01:00.000000Z",
    )


def test_allow_records_event_envelope_projection_and_verifies(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path)
    test_result_sha256, test_result_envelope_sha256 = _test_result_digests(target)

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
    assert "reason" not in event.payload

    envelope, statement = _statement(target)
    assert envelope["brigade"] == {
        "profile": attestation.ATTESTATION_PROFILE,
        "namespace": attestation.ATTESTATION_NAMESPACE,
    }
    assert statement["subject"] == [
        {"name": "git:tree", "digest": {"gitTree": TREE}},
        {"name": "changes.patch", "digest": {"sha256": PATCH}},
        {"name": f"test-result:{VERIFY_ID}", "digest": {"sha256": test_result_sha256}},
    ]
    predicate = statement["predicate"]
    assert statement["predicateType"] == approval.HUMAN_APPROVAL_V2_PREDICATE_TYPE
    assert predicate["schemaVersion"] == 2
    assert predicate["decision"] == "allow"
    assert event.payload["decided_at"] == event.recorded_at == predicate["decidedAt"]
    assert predicate["approver"]["kind"] == "human"
    assert predicate["policy"]["name"] == "brigade.sod.v2"
    assert predicate["requester"]["status"] == "verified"
    assert predicate["requester"]["principal"] == "requester"
    request_envelope = json.loads((target / ".brigade" / "runs" / RUN_ID / "requests" / f"{'a' * 32}.json").read_text())
    request_event = next(
        event
        for event in run_journal.read_journal(
            target / ".brigade" / "runs" / RUN_ID / "events" / "lifecycle.jsonl"
        ).events
        if event.event_type == "request.signed"
    )
    assert predicate["requester"]["statementSha256"] == request_event.payload["statement_sha256"]
    assert predicate["requester"]["envelopeSha256"] == localio.canonical_json_digest(request_envelope)
    assert predicate["evidence"]["baselineCommit"] == BASELINE
    assert predicate["evidence"]["changesPatchSha256"] == PATCH
    assert predicate["evidence"]["testResultPayloadSha256"] == [test_result_sha256]
    assert predicate["evidence"]["envelopes"] == [
        {
            "verifyRunId": VERIFY_ID,
            "payloadSha256": test_result_sha256,
            "envelopeSha256": test_result_envelope_sha256,
            "profile": attestation.ATTESTATION_PROFILE,
            "signerKeyid": predicate["evidence"]["envelopes"][0]["signerKeyid"],
            "producerKeyids": [predicate["evidence"]["envelopes"][0]["signerKeyid"]],
        }
    ]
    assert len(predicate["nonce"]) == 32

    run_meta = json.loads((target / ".brigade" / "runs" / RUN_ID / "run.json").read_text())
    assert run_meta["approval"]["decision"] == "allow"
    assert run_meta["approval"]["reason"] == "Reviewed the focused test receipt."
    assert run_meta["approval"]["sod"]["result"] == "PASSED"
    assert {check["id"] for check in run_meta["approval"]["sod"]["checks"]} == {
        "approver-is-human",
        "approver-not-producer",
        "approver-not-requester",
        "approver-key-not-workspace-key",
        "approval-before-merge-ship",
        "approval-not-expired",
    }

    assert cli.main(["receipts", "verify", "--target", str(target), "--json"]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["approvals"]["runs"][0]["status"] == "APPROVED"
    assert verified["approvals"]["runs"][0]["binding"] == "test-result"
    assert "Reviewed the focused test receipt." not in json.dumps(verified)


def test_v1_approval_verification_stays_receipt_bound(tmp_path: Path) -> None:
    target, key, _signers = _workspace(tmp_path, requester_principal=None)
    _record_v1_approval(target, key)

    verified = approval.verify_run_approval(target, target / ".brigade" / "runs" / RUN_ID)

    assert verified.status == "APPROVED"
    assert verified.binding == "receipt"


def test_v2_binds_the_exact_sorted_test_result_payload_digest_set(tmp_path: Path) -> None:
    target, key, _signers = _workspace(tmp_path)
    verifier_key = tmp_path / "verifier" / ".brigade" / "attestation" / "signing-key"
    second_verify_id = "20260903-120001-work-verify-approval"
    _write_verify_receipt(target, verify_id=second_verify_id, producer_key=verifier_key)

    assert _approve(target, key) == 0
    _envelope, statement = _statement(target)

    evidence = statement["predicate"]["evidence"]
    payload_digests = evidence["testResultPayloadSha256"]
    assert payload_digests == sorted(payload_digests)
    assert payload_digests == [item["payloadSha256"] for item in evidence["envelopes"]]
    assert payload_digests == [
        item["digest"]["sha256"] for item in statement["subject"] if item["name"].startswith("test-result:")
    ]


def test_v2_ignores_matching_receipts_for_older_trees(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, key, _signers = _workspace(tmp_path)
    verifier_key = tmp_path / "verifier" / ".brigade" / "attestation" / "signing-key"
    stale_verify_id = "20260903-115900-work-verify-stale-tree"
    _write_verify_receipt(
        target,
        verify_id=stale_verify_id,
        producer_key=verifier_key,
        tree_fingerprint="9" * 40,
    )

    assert _approve(target, key) == 0
    _envelope, statement = _statement(target)
    assert [subject["name"] for subject in statement["subject"] if subject["name"].startswith("test-result:")] == [
        f"test-result:{VERIFY_ID}"
    ]

    capsys.readouterr()
    assert cli.main(["receipts", "verify", "--target", str(target)]) == 0
    assert "APPROVED" in capsys.readouterr().out


def test_v2_reports_when_matching_receipts_exist_only_for_other_trees(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, key, _signers = _workspace(tmp_path)
    receipt_path = target / ".brigade" / "work" / "verify-runs" / VERIFY_ID / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["tree_fingerprint"] = "9" * 40
    receipt["digests"]["receipt_sha256"] = localio.canonical_json_digest(receipt, exclude_keys={"digests"})
    _write_json(receipt_path, receipt)
    _resign_verify_receipt(target, tmp_path / "verifier" / ".brigade" / "attestation" / "signing-key")

    assert _approve(target, key) == 2
    assert "final tree" in capsys.readouterr().err
    assert _approval_event(target).event_type == "request.signed"


def test_attestation_write_failure_leaves_no_approval_event(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """If publishing the signed envelope fails, no durable approval event is appended."""
    target, key, _signers = _workspace(tmp_path)

    def _raise(*_args, **_kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(attestation, "write_attestation_file", _raise)

    with pytest.raises(RuntimeError, match="disk full"):
        approval.record_approval(
            target=target,
            run_id=RUN_ID,
            decision="allow",
            scope="run",
            reason_code="reviewed-tests",
            reason="Reviewed the focused test receipt.",
            key=key,
        )

    assert _approval_event(target).event_type == "request.signed"


def test_journal_append_failure_removes_unreferenced_approval_envelope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, key, _signers = _workspace(tmp_path)

    def _raise(*_args, **_kwargs):
        raise RuntimeError("append refused")

    monkeypatch.setattr(run_journal, "append_event", _raise)

    with pytest.raises(RuntimeError, match="append refused"):
        approval.record_approval(
            target=target,
            run_id=RUN_ID,
            decision="allow",
            scope="run",
            reason_code="reviewed-tests",
            reason="Reviewed the focused test receipt.",
            key=key,
        )

    approvals_dir = target / ".brigade" / "runs" / RUN_ID / "approvals"
    assert not approvals_dir.exists() or list(approvals_dir.iterdir()) == []
    assert _approval_event(target).event_type == "request.signed"


def _test_result_digests(target: Path, *, verify_id: str = VERIFY_ID) -> tuple[str, str]:
    envelope = json.loads((target / ".brigade" / "work" / "verify-runs" / verify_id / "attestation.json").read_text())
    return (
        hashlib.sha256(base64.b64decode(envelope["payload"])).hexdigest(),
        localio.canonical_json_digest(envelope),
    )


def test_approver_cannot_be_a_verify_receipt_producer(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path, producer_is_approver=True)

    assert _approve(target, key) == 3
    assert "approver-not-producer: failed" in capsys.readouterr().out
    assert _approval_event(target).event_type == "approval"

    assert cli.main(["receipts", "verify", "--target", str(target)]) != 0
    assert "SOD-VIOLATION" in capsys.readouterr().out


def test_committed_live_tree_change_makes_allow_stale(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path)
    subprocess.run(["git", "init", "-q", str(target)], check=True)
    tracked = target / "tracked.txt"
    tracked.write_text("before\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(target), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    tree = localio.tree_fingerprint(target)
    assert tree is not None
    run_path = target / ".brigade" / "runs" / RUN_ID / "run.json"
    run_meta = json.loads(run_path.read_text())
    run_meta["tree_fingerprint"] = tree
    _write_json(run_path, run_meta)
    receipt_path = target / ".brigade" / "work" / "verify-runs" / VERIFY_ID / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["tree_fingerprint"] = tree
    receipt["digests"].pop("receipt_sha256")
    receipt["digests"]["receipt_sha256"] = localio.canonical_json_digest(receipt, exclude_keys={"digests"})
    _write_json(receipt_path, receipt)
    _resign_verify_receipt(target, tmp_path / "verifier" / ".brigade" / "attestation" / "signing-key")
    assert _approve(target, key) == 0
    capsys.readouterr()
    tracked.write_text("after\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(target), "add", "tracked.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "-c",
            "user.name=test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "after",
        ],
        check=True,
    )

    assert cli.main(["receipts", "verify", "--target", str(target)]) == 0
    assert "APPROVAL-STALE (allow)" in capsys.readouterr().out


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
    assert "allow requires at least one re-derived SIGNED-OK Test Result envelope" in captured.err
    assert _approval_event(target).event_type == "request.signed"


def test_allow_requires_rederived_signed_ok_test_result_before_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, key, _signers = _workspace(tmp_path)
    (target / ".brigade" / "work" / "verify-runs" / VERIFY_ID / "attestation.json").unlink()

    assert _approve(target, key) == 2
    assert "Test Result envelope is missing" in capsys.readouterr().err
    assert _approval_event(target).event_type == "request.signed"


def test_allow_refuses_failed_test_result_before_writing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, key, _signers = _workspace(tmp_path)
    receipt_path = target / ".brigade" / "work" / "verify-runs" / VERIFY_ID / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["status"] = "failed"
    receipt["commands"][0]["status"] = "failed"
    receipt["commands"][0]["exit_code"] = 1
    receipt["digests"]["receipt_sha256"] = localio.canonical_json_digest(receipt, exclude_keys={"digests"})
    _write_json(receipt_path, receipt)
    _resign_verify_receipt(target, tmp_path / "verifier" / ".brigade" / "attestation" / "signing-key")

    assert _approve(target, key) == 2
    assert "PASSED Test Result" in capsys.readouterr().err
    assert _approval_event(target).event_type == "request.signed"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("baseline_commit", "4" * 40, "baseline commit"),
        ("changes_patch_sha256", hashlib.sha256(b"different patch\n").hexdigest(), "patch digest"),
    ],
)
def test_allow_refuses_inconsistent_matching_receipts_before_writing(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    field: str,
    value: str,
    message: str,
) -> None:
    target, key, _signers = _workspace(tmp_path)
    verifier_key = tmp_path / "verifier" / ".brigade" / "attestation" / "signing-key"
    _write_verify_receipt(
        target,
        verify_id="20260903-120001-work-verify-approval",
        producer_key=verifier_key,
        baseline_commit=value if field == "baseline_commit" else BASELINE,
        changes_patch_sha256=value if field == "changes_patch_sha256" else PATCH,
        patch_bytes=b"different patch\n" if field == "changes_patch_sha256" else PATCH_BYTES,
    )

    assert _approve(target, key) == 2
    assert message in capsys.readouterr().err
    assert _approval_event(target).event_type == "request.signed"


def test_allow_refuses_tampered_signed_request_before_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, key, _signers = _workspace(tmp_path)
    request_path = target / ".brigade" / "runs" / RUN_ID / "requests" / f"{'a' * 32}.json"
    envelope = json.loads(request_path.read_text())
    envelope["signatures"][0]["sig"] = "not-a-signature"
    _write_json(request_path, envelope)

    assert _approve(target, key) == 2
    assert "signed request" in capsys.readouterr().err
    assert _approval_event(target).event_type == "request.signed"


def test_allow_refuses_ambiguous_signed_requests_before_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, key, _signers = _workspace(tmp_path)
    run_dir = target / ".brigade" / "runs" / RUN_ID
    requester_key = tmp_path / "requester" / ".brigade" / "attestation" / "signing-key"
    _append_signed_request(
        target,
        run_dir,
        run_id=RUN_ID,
        key=requester_key,
        principal="requester",
        nonce="b" * 32,
        requested_at="2026-09-03T11:59:31.000000Z",
    )

    assert _approve(target, key) == 2
    assert "signed request evidence is ambiguous" in capsys.readouterr().err
    assert _approval_event(target).event_type == "request.signed"


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
    for forbidden in (
        str(tmp_path),
        "SECRET_TOKEN",
        "do-not-copy",
        "test output",
        "stderr that",
        "Reviewed the focused test receipt.",
    ):
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

    run_dir = target / ".brigade" / "runs" / RUN_ID
    event = run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl").events[-1]
    envelope = json.loads((run_dir / event.payload["attestation_path"]).read_text(encoding="utf-8"))
    statement = json.loads(base64.b64decode(envelope["payload"]))
    signed = approval_v2.parse_signed_bindings(statement)
    events = run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl").events
    expected_sod = approval_v2.evaluate_sod(
        statement=statement,
        evidence=signed.evidence,
        requester=signed.requester,
        events=events,
        approval_sequence=event.sequence,
        workspace_keyid=attestation.get_key_fingerprint(attestation.resolve_signing_key_path(target)),
        now=datetime.now(timezone.utc),
    )

    report = run_audit.audit_run(run_dir)
    assert report.result == run_audit.RESULT_MATCH
    assert report.to_receipt()["approval"] == {
        "decision": "allow",
        "sod": expected_sod,
    }


def test_runs_show_prints_the_approval_projection(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path)
    assert _approve(target, key) == 0
    capsys.readouterr()

    assert cli.main(["runs", "show", RUN_ID, "--cwd", str(target)]) == 0
    shown = capsys.readouterr().out
    assert "approval: allow" in shown
    assert "approval sod: PASSED" in shown


def test_run_audit_detects_approval_keyid_projection_drift(tmp_path: Path):
    target, key, _signers = _workspace(tmp_path)
    assert _approve(target, key) == 0
    run_dir = target / ".brigade" / "runs" / RUN_ID
    run_meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    run_meta["approval"]["approver_keyid"] = "SHA256:rewritten"
    _write_json(run_dir / "run.json", run_meta)

    report = run_audit.audit_run(run_dir)
    assert report.result == run_audit.RESULT_DIVERGE
    assert report.first_divergence is not None
    assert report.first_divergence.divergence_class == run_audit.CLASS_APPROVAL_STATE_DRIFT


def test_requester_cannot_approve_their_own_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path, requester_principal="alice")

    assert _approve(target, key) == 3
    assert "approver-not-requester: failed" in capsys.readouterr().out
    assert _approval_event(target).event_type == "approval"


def test_requester_key_cannot_approve_under_an_alias(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(
        tmp_path,
        requester_principal="requester-alias",
        requester_uses_approver_key=True,
    )

    assert _approve(target, key) == 3
    assert "approver-not-requester: failed" in capsys.readouterr().out
    assert _approval_event(target).event_type == "approval"


def test_unknown_requester_is_indeterminate_and_only_strict_verify_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, key, _signers = _workspace(tmp_path, requester_principal=None)

    assert _approve(target, key) == 3
    output = capsys.readouterr().out
    assert "sod: INDETERMINATE" in output
    assert "approver-not-requester: indeterminate" in output

    assert cli.main(["receipts", "verify", "--target", str(target)]) == 0
    assert "SOD-INDETERMINATE" in capsys.readouterr().out
    assert cli.main(["receipts", "verify", "--target", str(target), "--strict-approvals"]) != 0


def test_v2_uses_reverified_request_instead_of_projection(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, key, _signers = _workspace(tmp_path)
    run_path = target / ".brigade" / "runs" / RUN_ID / "run.json"
    run_meta = json.loads(run_path.read_text())
    run_meta["requester_principal"] = "alice"
    run_meta["requester_keyid"] = attestation.get_key_fingerprint(key)
    _write_json(run_path, run_meta)

    assert _approve(target, key) == 0
    assert "approver-not-requester: passed" in capsys.readouterr().out


def test_reason_refuses_absolute_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path)

    assert _approve(target, key, "--reason", f"Reviewed {tmp_path / 'private.txt'}") == 2
    assert "absolute paths" in capsys.readouterr().err
    assert _approval_event(target).event_type == "request.signed"


def test_hmac_receipt_keyid_is_not_a_verified_producer_identity(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, key, _signers = _workspace(tmp_path)
    receipt_path = target / ".brigade" / "work" / "verify-runs" / VERIFY_ID / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["digests"]["key_id"] = attestation.get_key_fingerprint(key)
    _write_json(receipt_path, receipt)

    assert _approve(target, key) == 0
    assert "approver-not-producer: passed" in capsys.readouterr().out
    _envelope, statement = _statement(target)
    descriptor = statement["predicate"]["evidence"]["envelopes"][0]
    assert descriptor["producerKeyids"] == [descriptor["signerKeyid"]]
    assert attestation.get_key_fingerprint(key) not in descriptor["producerKeyids"]


def test_forged_attestation_keyid_hint_does_not_hide_the_producer(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path, producer_is_approver=True)
    attestation_path = target / ".brigade" / "work" / "verify-runs" / VERIFY_ID / "attestation.json"
    envelope = json.loads(attestation_path.read_text())
    envelope["signatures"][0]["keyid"] = "SHA256:forged"
    _write_json(attestation_path, envelope)

    assert _approve(target, key) == 2
    assert "SIGNED-OK" in capsys.readouterr().err
    assert _approval_event(target).event_type == "request.signed"


def test_unverifiable_producer_attestation_fails_closed_at_approval(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path, producer_is_approver=True)
    attestation_path = target / ".brigade" / "work" / "verify-runs" / VERIFY_ID / "attestation.json"
    envelope = json.loads(attestation_path.read_text())
    envelope["signatures"][0]["sig"] = "not a signature"
    _write_json(attestation_path, envelope)

    assert _approve(target, key) == 2
    assert "SIGNED-OK" in capsys.readouterr().err
    assert _approval_event(target).event_type == "request.signed"


def test_missing_producer_receipt_keeps_recorded_sod_violation(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path, producer_is_approver=True)
    assert _approve(target, key) == 3
    capsys.readouterr()

    shutil.rmtree(target / ".brigade" / "work" / "verify-runs" / VERIFY_ID)

    assert cli.main(["receipts", "verify", "--target", str(target)]) != 0
    output = capsys.readouterr().out
    assert "- SOD-VIOLATION" in output


def test_revoked_approver_key_makes_approval_invalid(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path)
    assert _approve(target, key) == 0
    capsys.readouterr()

    krl_path = target / ".brigade" / "attestation" / "revoked_keys"
    subprocess.run(
        ["ssh-keygen", "-k", "-f", str(krl_path), str(key.with_suffix(".pub"))], check=True, capture_output=True
    )

    assert cli.main(["receipts", "verify", "--target", str(target)]) != 0
    assert "APPROVAL-INVALID" in capsys.readouterr().out


def test_v2_exact_test_result_set_makes_missing_or_extra_receipt_stale(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    target, key, _signers = _workspace(tmp_path)
    assert _approve(target, key) == 0
    capsys.readouterr()
    receipt_dir = target / ".brigade" / "work" / "verify-runs" / VERIFY_ID
    extra_dir = receipt_dir.with_name("20260903-120001-work-verify-extra")
    shutil.copytree(receipt_dir, extra_dir)
    extra_receipt = json.loads((extra_dir / "receipt.json").read_text())
    extra_receipt["run_id"] = extra_dir.name
    extra_receipt["digests"]["receipt_sha256"] = localio.canonical_json_digest(extra_receipt, exclude_keys={"digests"})
    _write_json(extra_dir / "receipt.json", extra_receipt)
    _resign_verify_receipt(
        target,
        tmp_path / "verifier" / ".brigade" / "attestation" / "signing-key",
        verify_id=extra_dir.name,
    )

    assert cli.main(["receipts", "verify", "--target", str(target)]) == 0
    assert "APPROVAL-STALE (allow)" in capsys.readouterr().out
    shutil.rmtree(extra_dir)

    assert cli.main(["receipts", "verify", "--target", str(target)]) == 0
    assert "APPROVED" in capsys.readouterr().out
    shutil.rmtree(receipt_dir)

    assert cli.main(["receipts", "verify", "--target", str(target)]) == 0
    assert "APPROVAL-STALE (allow)" in capsys.readouterr().out


def test_deny_stays_non_exit_changing_when_the_tree_is_stale(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path, with_receipt=False)
    assert (
        cli.main(
            [
                "run",
                "approve",
                RUN_ID,
                "--decision",
                "deny",
                "--reason-code",
                "needs-rework",
                "--key",
                str(key),
                "--target",
                str(target),
            ]
        )
        == 0
    )
    capsys.readouterr()
    run_path = target / ".brigade" / "runs" / RUN_ID / "run.json"
    run_meta = json.loads(run_path.read_text())
    run_meta["tree_fingerprint"] = "9" * 40
    _write_json(run_path, run_meta)

    assert cli.main(["receipts", "verify", "--target", str(target)]) == 0
    assert "DENIED" in capsys.readouterr().out


def test_invalid_projection_refuses_approval_without_appending_an_event(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    target, key, _signers = _workspace(tmp_path)
    run_path = target / ".brigade" / "runs" / RUN_ID / "run.json"
    run_meta = json.loads(run_path.read_text())
    run_meta["unknown_field"] = True
    _write_json(run_path, run_meta)

    assert _approve(target, key) == 1
    assert "unknown fields" in capsys.readouterr().err
    assert _approval_event(target).event_type == "request.signed"


def test_later_approval_supersedes_earlier_decision_and_keeps_history(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    target, key, _signers = _workspace(tmp_path, with_receipt=False)
    deny = [
        "run",
        "approve",
        RUN_ID,
        "--decision",
        "deny",
        "--scope",
        "merge",
        "--reason-code",
        "needs-rework",
        "--key",
        str(key),
        "--target",
        str(target),
    ]
    assert cli.main(deny) == 0
    capsys.readouterr()
    allow = [
        "run",
        "approve",
        RUN_ID,
        "--decision",
        "hold",
        "--reason-code",
        "needs-rework",
        "--key",
        str(key),
        "--target",
        str(target),
    ]
    assert cli.main(allow) == 0
    capsys.readouterr()

    assert cli.main(["receipts", "verify", "--target", str(target), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    result = payload["approvals"]["runs"][0]
    assert result["decision"] == "hold"
    assert result["prior_approvals"] == [
        {"decision": "deny", "principal": "alice", "decided_at": result["prior_approvals"][0]["decided_at"]}
    ]


def test_backdated_approval_after_ship_stage_is_late_by_sequence(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """An approval appended after ship fails even when its signed time is earlier."""
    target, key, _signers = _workspace(tmp_path)
    journal = target / ".brigade" / "runs" / RUN_ID / "events" / "lifecycle.jsonl"
    before_ship = run_journal.read_journal(journal)
    ship = run_journal.append_event(
        journal,
        run_id=RUN_ID,
        event_type="run.ship",
        payload={"stage": "ship"},
        idempotency_key="ship-stage-1",
        expected_previous_sequence=before_ship.events[-1].sequence,
        recorded_at="2030-01-01T00:00:00.000000Z",
    )
    assert ship.sequence == before_ship.events[-1].sequence + 1

    result = approval.record_approval(
        target=target,
        run_id=RUN_ID,
        decision="allow",
        scope="run",
        reason_code="reviewed-tests",
        reason="Reviewed the focused test receipt.",
        key=key,
        now=datetime(2026, 9, 3, 12, 1, tzinfo=timezone.utc),
    )
    ordering_check = next(
        check for check in result["approval"]["sod"]["checks"] if check["id"] == "approval-before-merge-ship"
    )
    assert ordering_check["status"] == "failed"

    capsys.readouterr()
    assert cli.main(["receipts", "verify", "--target", str(target)]) != 0
    assert "SOD-VIOLATION" in capsys.readouterr().out


def test_backdated_ship_after_approval_does_not_violate_sequence_order(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, key, _signers = _workspace(tmp_path)
    assert _approve(target, key) == 0
    capsys.readouterr()
    journal = target / ".brigade" / "runs" / RUN_ID / "events" / "lifecycle.jsonl"
    before_ship = run_journal.read_journal(journal)
    run_journal.append_event(
        journal,
        run_id=RUN_ID,
        event_type="run.ship",
        payload={"stage": "ship"},
        idempotency_key="ship-stage-after-approval",
        expected_previous_sequence=before_ship.events[-1].sequence,
        recorded_at="2020-01-01T00:00:00.000000Z",
    )

    assert cli.main(["receipts", "verify", "--target", str(target)]) == 0
    assert "APPROVED" in capsys.readouterr().out


def test_event_decision_time_must_match_the_signed_decision_time(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, key, _signers = _workspace(tmp_path)
    assert _approve(target, key) == 0
    capsys.readouterr()

    journal = target / ".brigade" / "runs" / RUN_ID / "events" / "lifecycle.jsonl"
    report = run_journal.read_journal(journal)
    events = [event.to_dict() for event in report.events]
    approval_event = events[-1]
    approval_event["payload"]["decided_at"] = "2026-09-03T12:02:00.000000Z"
    rebuilt = run_events.build_event(
        run_id=approval_event["run_id"],
        sequence=approval_event["sequence"],
        event_type=approval_event["event_type"],
        payload=approval_event["payload"],
        idempotency_key=approval_event["idempotency_key"],
        recorded_at=approval_event["recorded_at"],
        previous_digest=approval_event["previous_digest"],
    )
    journal.write_bytes(
        b"".join(run_events.canonical_bytes(event) + b"\n" for event in events[:-1])
        + run_events.canonical_bytes(rebuilt)
        + b"\n"
    )

    assert cli.main(["receipts", "verify", "--target", str(target)]) != 0
    assert "APPROVAL-INVALID" in capsys.readouterr().out


def test_tampered_signature_yields_approval_invalid(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """A tampered signature on the approval envelope is detected as invalid."""
    target, key, _signers = _workspace(tmp_path)
    assert _approve(target, key) == 0
    capsys.readouterr()

    envelope, _statement_payload = _statement(target)
    path = target / ".brigade" / "runs" / RUN_ID / _approval_event(target).payload["attestation_path"]
    sig = envelope["signatures"][0]["sig"]
    raw = base64.b64decode(sig)
    tampered = raw[:-1] + bytes([raw[-1] ^ 1])
    envelope["signatures"][0]["sig"] = base64.b64encode(tampered).decode()
    _write_json(path, envelope)

    assert cli.main(["receipts", "verify", "--target", str(target)]) != 0
    assert "APPROVAL-INVALID" in capsys.readouterr().out


def test_wrong_statement_sha256_yields_approval_invalid(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """A mismatched statement_sha256 in the approval event is detected as invalid."""
    target, key, _signers = _workspace(tmp_path)
    assert _approve(target, key) == 0
    capsys.readouterr()

    journal = target / ".brigade" / "runs" / RUN_ID / "events" / "lifecycle.jsonl"
    report = run_journal.read_journal(journal)
    events = [ev.to_dict() for ev in report.events]
    approval_env = events[-1]
    approval_env["payload"]["statement_sha256"] = "0" * 64
    rebuilt = run_events.build_event(
        run_id=approval_env["run_id"],
        sequence=approval_env["sequence"],
        event_type=approval_env["event_type"],
        payload=approval_env["payload"],
        idempotency_key=approval_env["idempotency_key"],
        recorded_at=approval_env["recorded_at"],
        previous_digest=approval_env["previous_digest"],
    )
    journal.write_bytes(
        b"".join(run_events.canonical_bytes(ev) + b"\n" for ev in events[:-1])
        + run_events.canonical_bytes(rebuilt)
        + b"\n"
    )

    assert cli.main(["receipts", "verify", "--target", str(target)]) != 0
    assert "APPROVAL-INVALID" in capsys.readouterr().out


def test_wrong_journal_chain_head_yields_approval_invalid(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """A statement whose journal chain head does not match the event is invalid."""
    target, key, _signers = _workspace(tmp_path)
    assert _approve(target, key) == 0
    capsys.readouterr()

    envelope, statement = _statement(target)
    path = target / ".brigade" / "runs" / RUN_ID / _approval_event(target).payload["attestation_path"]
    statement["predicate"]["run"]["journalChainHead"]["sha256"] = "0" * 64
    envelope["payload"] = base64.b64encode(attestation.canonical_statement_bytes(statement)).decode()
    _write_json(path, envelope)

    assert cli.main(["receipts", "verify", "--target", str(target)]) != 0
    assert "APPROVAL-INVALID" in capsys.readouterr().out


def test_envelope_copied_from_another_run_yields_approval_invalid(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """An approval envelope copied from a different run is detected as invalid."""
    run_a = "approval-run-a"
    run_b = "approval-run-b"
    target, key_a, _signers = _workspace(tmp_path, run_id=run_a)
    assert _approve(target, key_a, run_id=run_a) == 0
    capsys.readouterr()

    event_a = _approval_event(target, run_id=run_a)
    nonce_a = event_a.payload["nonce"]
    envelope_a, _statement_payload = _statement(target, run_id=run_a)

    run_b_dir = target / ".brigade" / "runs" / run_b
    run_b_dir.mkdir(parents=True)
    created_b = run_events.build_event(
        run_id=run_b,
        sequence=1,
        event_type="run.created",
        payload={"status": "started"},
        idempotency_key="created",
        recorded_at="2026-09-03T11:59:00.000000Z",
        previous_digest=None,
    )
    journal_b = run_b_dir / "events" / "lifecycle.jsonl"
    journal_b.parent.mkdir(parents=True)
    journal_b.write_bytes(run_events.canonical_bytes(created_b) + b"\n")
    _write_json(
        run_b_dir / "run.json",
        {
            "schema": "brigade.run.v1",
            "schema_version": 1,
            "status": "started",
            "tree_fingerprint": TREE,
            "journal_present": True,
            "journal_last_sequence": 1,
            "journal_last_event_digest": created_b["event_digest"],
        },
    )

    dest_dir = run_b_dir / "approvals"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / f"{nonce_a}.json"
    _write_json(dest_path, envelope_a)

    payload_b = dict(event_a.payload)
    payload_b["attestation_path"] = f"approvals/{nonce_a}.json"
    event_b = run_events.build_event(
        run_id=run_b,
        sequence=2,
        event_type="approval",
        payload=payload_b,
        idempotency_key=f"approval:{nonce_a}",
        recorded_at=event_a.recorded_at,
        previous_digest=created_b["event_digest"],
    )
    with journal_b.open("ab") as handle:
        handle.write(run_events.canonical_bytes(event_b) + b"\n")

    capsys.readouterr()
    assert cli.main(["receipts", "verify", "--target", str(target)]) != 0
    assert "APPROVAL-INVALID" in capsys.readouterr().out


def test_scope_merge_round_trip_records_scope_and_verifies_approved(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """A --scope merge approval round-trips through the event and verifies APPROVED."""
    target, key, _signers = _workspace(tmp_path)
    assert _approve(target, key, "--scope", "merge") == 0
    capsys.readouterr()

    event = _approval_event(target)
    assert event.payload["scope"] == "merge"

    run_meta = json.loads((target / ".brigade" / "runs" / RUN_ID / "run.json").read_text())
    assert run_meta["approval"]["scope"] == "merge"

    assert cli.main(["receipts", "verify", "--target", str(target)]) == 0
    assert "APPROVED" in capsys.readouterr().out


def test_strict_approvals_makes_an_expired_allow_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path)
    assert _approve(target, key, "--expires-in", "0s") != 0
    capsys.readouterr()

    assert cli.main(["receipts", "verify", "--target", str(target)]) == 0
    assert "APPROVAL-EXPIRED" in capsys.readouterr().out

    assert cli.main(["receipts", "verify", "--target", str(target), "--strict-approvals"]) != 0


def test_strict_approvals_makes_a_stale_allow_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    target, key, _signers = _workspace(tmp_path)
    assert _approve(target, key) == 0
    capsys.readouterr()
    run_path = target / ".brigade" / "runs" / RUN_ID / "run.json"
    run_meta = json.loads(run_path.read_text())
    run_meta["tree_fingerprint"] = "9" * 40
    _write_json(run_path, run_meta)

    assert cli.main(["receipts", "verify", "--target", str(target)]) == 0
    assert "APPROVAL-STALE" in capsys.readouterr().out
    assert cli.main(["receipts", "verify", "--target", str(target), "--strict-approvals"]) != 0


def test_default_and_strict_approval_exit_behavior_for_unapproved_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target, _key, _signers = _workspace(tmp_path)

    assert cli.main(["receipts", "verify", "--target", str(target)]) == 0
    assert "UNAPPROVED" in capsys.readouterr().out

    assert cli.main(["receipts", "verify", "--target", str(target), "--strict-approvals"]) != 0
    assert "UNAPPROVED" in capsys.readouterr().out


def test_strict_approvals_help_names_each_additional_failure_status(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        cli.main(["receipts", "verify", "--help"])

    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    for status in ("stale", "expired", "indeterminate", "unapproved"):
        assert status in help_text
    assert "invalid" not in help_text
    assert "segregation-of-duties-failed" not in help_text
