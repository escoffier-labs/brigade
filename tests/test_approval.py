"""Signed human approval events bound to run trees and verify receipts."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from brigade import approval, attestation, cli, localio, run_audit, run_events, run_journal

if not shutil.which("ssh-keygen"):
    pytest.skip("ssh-keygen is required for approval tests", allow_module_level=True)


RUN_ID = "approval-run-001"
VERIFY_ID = "20260903-120000-work-verify-approval"
TREE = "1" * 40
PATCH = "2" * 64


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_verify_receipt(
    target: Path,
    *,
    producer_run_id: str = RUN_ID,
    producer_key: Path | None = None,
    tree_fingerprint: str = TREE,
    hmac_keyid: str | None = None,
) -> dict[str, Any]:
    receipt_dir = target / ".brigade" / "work" / "verify-runs" / VERIFY_ID
    receipt_dir.mkdir(parents=True)
    stdout = receipt_dir / "command-1-stdout.log"
    stderr = receipt_dir / "command-1-stderr.log"
    stdout.write_text("test output that must not be approved\n", encoding="utf-8")
    stderr.write_text("stderr that must not be approved\n", encoding="utf-8")
    receipt: dict[str, Any] = {
        "schema_version": 2,
        "run_id": VERIFY_ID,
        "producer_run_id": producer_run_id,
        "target": str(target),
        "status": "completed",
        "started_at": "2026-09-03T12:00:00Z",
        "completed_at": "2026-09-03T12:00:05Z",
        "tree_fingerprint": tree_fingerprint,
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
    if hmac_keyid is not None:
        receipt["digests"]["key_id"] = hmac_keyid
    _write_json(receipt_dir / "receipt.json", receipt)
    if producer_key is not None:
        envelope = attestation.export_attestation(receipt, producer_key)
        _write_json(receipt_dir / "attestation.json", envelope)
    return receipt


def _workspace(
    tmp_path: Path,
    *,
    run_id: str = RUN_ID,
    with_receipt: bool = True,
    producer_is_approver: bool = False,
    requester_principal: str | None = None,
) -> tuple[Path, Path, Path]:
    target = tmp_path / "workspace"
    target.mkdir()
    _workspace_key, signers = attestation.keygen(target, principal="workspace")
    approver_home = tmp_path / "approver"
    key, approver_signers = attestation.keygen(approver_home, principal="alice")
    signers.write_text(
        signers.read_text(encoding="utf-8") + approver_signers.read_text(encoding="utf-8"), encoding="utf-8"
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
        _write_verify_receipt(target, producer_run_id=run_id, producer_key=key if producer_is_approver else None)
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
    assert {check["id"] for check in run_meta["approval"]["sod"]["checks"]} == {
        "approver-is-human",
        "approver-not-producer",
        "requester-unknown",
        "approver-key-not-workspace-key",
        "approval-before-merge-ship",
        "approval-not-expired",
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

    run_dir = target / ".brigade" / "runs" / RUN_ID
    run_meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    event = run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl").events[-1]
    envelope = json.loads((run_dir / event.payload["attestation_path"]).read_text(encoding="utf-8"))
    statement = json.loads(base64.b64decode(envelope["payload"]))
    receipts = approval.collect_verify_receipts(target, RUN_ID, tree_fingerprint=run_meta["tree_fingerprint"])
    events = run_journal.read_journal(run_dir / "events" / "lifecycle.jsonl").events
    expected_sod = approval.evaluate_sod(
        target=target,
        statement=statement,
        run_meta=run_meta,
        receipts=receipts,
        events=events,
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


def test_reason_refuses_absolute_paths(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path)

    assert _approve(target, key, "--reason", f"Reviewed {tmp_path / 'private.txt'}") == 2
    assert "absolute paths" in capsys.readouterr().err
    assert _approval_event(target).event_type == "run.created"


def test_hmac_receipt_keyid_is_a_producer_for_sod(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path)
    receipt_path = target / ".brigade" / "work" / "verify-runs" / VERIFY_ID / "receipt.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["digests"]["key_id"] = attestation.get_key_fingerprint(key)
    _write_json(receipt_path, receipt)

    assert _approve(target, key) == 3
    assert "approver-not-producer: failed" in capsys.readouterr().out


def test_forged_attestation_keyid_hint_does_not_hide_the_producer(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path, producer_is_approver=True)
    attestation_path = target / ".brigade" / "work" / "verify-runs" / VERIFY_ID / "attestation.json"
    envelope = json.loads(attestation_path.read_text())
    envelope["signatures"][0]["keyid"] = "SHA256:forged"
    _write_json(attestation_path, envelope)

    assert _approve(target, key) == 3
    assert "approver-not-producer: failed" in capsys.readouterr().out


def test_unverifiable_producer_attestation_fails_closed_at_approval(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    target, key, _signers = _workspace(tmp_path, producer_is_approver=True)
    attestation_path = target / ".brigade" / "work" / "verify-runs" / VERIFY_ID / "attestation.json"
    envelope = json.loads(attestation_path.read_text())
    envelope["signatures"][0]["sig"] = "not a signature"
    _write_json(attestation_path, envelope)

    assert _approve(target, key) == 3
    assert "producer-identity-unverifiable: failed" in capsys.readouterr().out


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


def test_missing_signed_receipt_is_stale_but_extra_receipt_is_allowed(
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
    assert _approval_event(target).event_type == "run.created"


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


def test_approval_after_ship_stage_event_is_late_sod_violation(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    """A ship stage event recorded before the approval makes the approval late."""
    target, key, _signers = _workspace(tmp_path)
    journal = target / ".brigade" / "runs" / RUN_ID / "events" / "lifecycle.jsonl"
    ship = run_journal.append_event(
        journal,
        run_id=RUN_ID,
        event_type="run.ship",
        payload={"stage": "ship"},
        idempotency_key="ship-stage-1",
        expected_previous_sequence=1,
        recorded_at="2020-01-01T00:00:00.000000Z",
    )
    assert ship.sequence == 2

    assert _approve(target, key) == 3
    approved = capsys.readouterr()
    assert "approval-before-merge-ship: failed" in approved.out

    capsys.readouterr()
    assert cli.main(["receipts", "verify", "--target", str(target)]) != 0
    assert "SOD-VIOLATION" in capsys.readouterr().out


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
