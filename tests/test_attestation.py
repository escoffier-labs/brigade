"""Tests for in-toto Statement v1 and SSHSIG DSSE attestation export and verification."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from brigade import attestation
from tests.support import PRIVATE_FILE_MODE, assert_private_mode

if not shutil.which("ssh-keygen"):
    pytest.skip("ssh-keygen is required for attestation tests", allow_module_level=True)


def _sample_receipt(
    tmp_path: Path,
    *,
    status: str = "completed",
    exit_code: int = 0,
    tree_fingerprint: str | None = "1111111111111111111111111111111111111111",
    changes_patch_sha256: str | None = "2222222222222222222222222222222222222222222222222222222222222222",
    baseline_commit: str | None = "3333333333333333333333333333333333333333",
    producer_run_id: str | None = "run-orch-001",
    include_env: bool = True,
) -> dict[str, Any]:
    run_id = "20260902-120000-work-verify-abcdef12"
    run_dir = tmp_path / ".brigade" / "work" / "verify-runs" / run_id
    return {
        "schema_version": 2,
        "run_id": run_id,
        "target": str(tmp_path / "workspace" / "secret_project"),
        "status": status,
        "started_at": "2026-09-02T12:00:00Z",
        "completed_at": "2026-09-02T12:00:05Z",
        "duration_seconds": 5.0,
        "path": str(run_dir),
        "baseline_commit": baseline_commit,
        "tree_fingerprint": tree_fingerprint,
        "changes_patch_sha256": changes_patch_sha256,
        "producer_run_id": producer_run_id,
        "commands": [
            {
                "command": "pytest -q tests/test_unit.py",
                "check_id": "unit-tests",
                "status": "completed" if exit_code == 0 else "failed",
                "exit_code": exit_code,
                "started_at": "2026-09-02T12:00:01Z",
                "completed_at": "2026-09-02T12:00:04Z",
                "stdout_summary": "1 passed in 0.1s",
                "stdout_log_path": str(run_dir / "stdout.log"),
                "stderr_log_path": str(run_dir / "stderr.log"),
                "env": ["SECRET_KEY=supersecret", "PATH=/usr/bin"] if include_env else [],
            }
        ],
        "digests": {
            "algorithm": "sha256",
            "receipt_sha256": "4444444444444444444444444444444444444444444444444444444444444444",
        },
    }


def test_keygen_writes_private_key_and_allowed_signers_entry(tmp_path: Path) -> None:
    key_path, signers_path = attestation.keygen(tmp_path, principal="alice@example.com")

    assert key_path.is_file()
    assert_private_mode(key_path, PRIVATE_FILE_MODE)
    assert signers_path.is_file()

    lines = signers_path.read_text("utf-8").splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f'alice@example.com namespaces="{attestation.ATTESTATION_NAMESPACE}" ssh-ed25519 ')

    with pytest.raises(FileExistsError):
        attestation.keygen(tmp_path, principal="alice@example.com")

    old_key_bytes = key_path.read_bytes()
    new_key_path, _ = attestation.keygen(tmp_path, principal="alice@example.com", force=True)
    assert new_key_path == key_path
    assert key_path.read_bytes() != old_key_bytes
    assert_private_mode(key_path, PRIVATE_FILE_MODE)


def test_export_from_completed_receipt_produces_identical_payload_without_leakage(tmp_path: Path) -> None:
    key_path, _signers_path = attestation.keygen(tmp_path, principal="alice@example.com")
    receipt = _sample_receipt(tmp_path)

    envelope = attestation.export_attestation(receipt, key_path=key_path)

    assert envelope["payloadType"] == attestation.DSSE_PAYLOAD_TYPE
    assert envelope["brigade"]["profile"] == attestation.ATTESTATION_PROFILE
    assert envelope["brigade"]["namespace"] == attestation.ATTESTATION_NAMESPACE

    payload_bytes = base64.b64decode(envelope["payload"])
    statement = attestation.build_statement(receipt)
    rederived_bytes = attestation.canonical_statement_bytes(statement)
    assert payload_bytes == rederived_bytes

    envelope_json = json.dumps(envelope)
    payload_json = payload_bytes.decode("utf-8")

    assert "SECRET_KEY" not in envelope_json and "SECRET_KEY" not in payload_json
    assert "supersecret" not in envelope_json and "supersecret" not in payload_json
    assert "secret_project" not in envelope_json and "secret_project" not in payload_json
    assert receipt["target"] not in envelope_json and receipt["target"] not in payload_json
    assert str(tmp_path) not in envelope_json and str(tmp_path) not in payload_json
    assert "stdout_log_path" not in envelope_json and "stdout_log_path" not in payload_json
    assert '"env"' not in envelope_json and '"env"' not in payload_json

    assert statement["_type"] == attestation.IN_TOTO_STATEMENT_TYPE
    assert statement["predicateType"] == attestation.IN_TOTO_TEST_RESULT_PREDICATE_TYPE
    assert statement["subject"] == [
        {"name": "git:tree", "digest": {"gitTree": receipt["tree_fingerprint"]}},
        {"name": "changes.patch", "digest": {"sha256": receipt["changes_patch_sha256"]}},
    ]

    pred = statement["predicate"]
    assert pred["result"] == "PASSED"
    assert pred["url"] == f"urn:brigade:verify:{receipt['run_id']}"
    assert pred["passedTests"] == ["unit-tests"]
    assert pred["warnedTests"] == []
    assert pred["failedTests"] == []

    config = pred["configuration"]
    assert len(config) == 1
    assert config[0]["name"] == "receipt.json"
    assert config[0]["uri"] == f"urn:brigade:verify:{receipt['run_id']}:receipt"
    assert config[0]["digest"]["sha256"] == receipt["digests"]["receipt_sha256"]
    assert config[0]["mediaType"] == "application/json"
    assert config[0]["annotations"]["brigade"] == {
        "run_id": receipt["run_id"],
        "baseline_commit": receipt["baseline_commit"],
        "producer_run_id": receipt["producer_run_id"],
    }


def test_export_refuses_receipt_with_null_tree_fingerprint(tmp_path: Path) -> None:
    receipt_null_tree = _sample_receipt(tmp_path, tree_fingerprint=None)
    with pytest.raises(attestation.AttestationExportError) as exc_info:
        attestation.build_statement(receipt_null_tree)
    assert "tree_fingerprint and changes_patch_sha256 are required" in str(exc_info.value)

    receipt_null_patch = _sample_receipt(tmp_path, changes_patch_sha256=None)
    with pytest.raises(attestation.AttestationExportError) as exc_info2:
        attestation.build_statement(receipt_null_patch)
    assert "tree_fingerprint and changes_patch_sha256 are required" in str(exc_info2.value)


def test_receipt_with_nonzero_or_null_exit_code_yields_failed(tmp_path: Path) -> None:
    receipt_failed = _sample_receipt(tmp_path, exit_code=1)
    stmt_failed = attestation.build_statement(receipt_failed)
    assert stmt_failed["predicate"]["result"] == "FAILED"
    assert stmt_failed["predicate"]["passedTests"] == []
    assert stmt_failed["predicate"]["failedTests"] == ["unit-tests"]

    receipt_null = _sample_receipt(tmp_path)
    receipt_null["commands"][0]["exit_code"] = None
    stmt_null = attestation.build_statement(receipt_null)
    assert stmt_null["predicate"]["result"] == "FAILED"
    assert stmt_null["predicate"]["passedTests"] == []
    assert stmt_null["predicate"]["failedTests"] == ["unit-tests"]

    receipt_status_failed = _sample_receipt(tmp_path, status="failed", exit_code=0)
    stmt_status_failed = attestation.build_statement(receipt_status_failed)
    assert stmt_status_failed["predicate"]["result"] == "FAILED"
    assert stmt_status_failed["predicate"]["passedTests"] == ["unit-tests"]
    assert stmt_status_failed["predicate"]["failedTests"] == []


def test_verify_on_second_tmp_path_reports_signed_ok(tmp_path: Path) -> None:
    path_a = tmp_path / "a"
    path_b = tmp_path / "b"

    key_a, signers_a = attestation.keygen(path_a, principal="alice@example.com")
    receipt = _sample_receipt(path_a)
    envelope = attestation.export_attestation(receipt, key_path=key_a)

    signers_b = path_b / ".brigade" / "attestation" / "allowed_signers"
    signers_b.parent.mkdir(parents=True)
    signers_b.write_text(signers_a.read_text("utf-8"), encoding="utf-8")

    res = attestation.verify_attestation(envelope, allowed_signers_path=signers_b)
    assert res.status == attestation.STATUS_SIGNED_OK
    assert res.principal == "alice@example.com"
    assert res.keyid == envelope["signatures"][0]["keyid"]
    assert res.run_id == receipt["run_id"]
    assert len(res.subject) == 2


def test_flipping_one_payload_byte_reports_signature_mismatch(tmp_path: Path) -> None:
    key_path, signers_path = attestation.keygen(tmp_path, principal="alice@example.com")
    receipt = _sample_receipt(tmp_path)
    envelope = attestation.export_attestation(receipt, key_path=key_path)

    raw_bytes = base64.b64decode(envelope["payload"])
    assert b"PASSED" in raw_bytes
    tampered_bytes = raw_bytes.replace(b"PASSED", b"FAILED")
    envelope["payload"] = base64.b64encode(tampered_bytes).decode("ascii")

    res = attestation.verify_attestation(envelope, allowed_signers_path=signers_path)
    assert res.status == attestation.STATUS_SIGNATURE_MISMATCH


def test_key_absent_from_allowed_signers_reports_untrusted_key(tmp_path: Path) -> None:
    dir_alice = tmp_path / "alice"
    dir_mallory = tmp_path / "mallory"

    key_alice, signers_alice = attestation.keygen(dir_alice, principal="alice@example.com")
    key_mallory, _ = attestation.keygen(dir_mallory, principal="mallory@example.com")

    receipt = _sample_receipt(tmp_path)
    envelope_mallory = attestation.export_attestation(receipt, key_path=key_mallory)

    res = attestation.verify_attestation(envelope_mallory, allowed_signers_path=signers_alice)
    assert res.status == attestation.STATUS_UNTRUSTED_KEY


def test_ssh_keygen_verify_by_hand_on_extracted_pae_and_sig(tmp_path: Path) -> None:
    key_path, signers_path = attestation.keygen(tmp_path, principal="alice@example.com")
    receipt = _sample_receipt(tmp_path)
    envelope = attestation.export_attestation(receipt, key_path=key_path)

    payload_bytes = base64.b64decode(envelope["payload"])
    pae_bytes = attestation.dsse_pae(envelope["payloadType"], payload_bytes)

    sig_b64 = envelope["signatures"][0]["sig"]
    armored_sig_bytes = base64.b64decode(sig_b64)
    sig_file = tmp_path / "extracted.sig"
    sig_file.write_bytes(armored_sig_bytes)

    cmd = [
        "ssh-keygen",
        "-Y",
        "verify",
        "-f",
        str(signers_path),
        "-I",
        "alice@example.com",
        "-n",
        attestation.ATTESTATION_NAMESPACE,
        "-s",
        str(sig_file),
    ]
    proc = subprocess.run(cmd, input=pae_bytes, capture_output=True)
    assert proc.returncode == 0
    combined = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    assert f'Good "{attestation.ATTESTATION_NAMESPACE}" signature' in combined


def test_subject_mismatch_detected_when_target_receipt_differs(tmp_path: Path) -> None:
    key_path, signers_path = attestation.keygen(tmp_path, principal="alice@example.com")
    receipt = _sample_receipt(tmp_path)
    envelope = attestation.export_attestation(receipt, key_path=key_path)

    # Save original receipt to run_dir
    run_dir = Path(receipt["path"])
    run_dir.mkdir(parents=True, exist_ok=True)
    receipt_file = run_dir / "receipt.json"
    receipt_file.write_text(json.dumps(receipt), encoding="utf-8")

    # With matching receipt, verify with target reports SIGNED-OK
    res_ok = attestation.verify_attestation(envelope, target=tmp_path, allowed_signers_path=signers_path)
    assert res_ok.status == attestation.STATUS_SIGNED_OK

    # Modify receipt on disk
    modified_receipt = dict(receipt)
    modified_receipt["tree_fingerprint"] = "9999999999999999999999999999999999999999"
    receipt_file.write_text(json.dumps(modified_receipt), encoding="utf-8")

    # Now verify with target reports SUBJECT-MISMATCH
    res_mismatch = attestation.verify_attestation(envelope, target=tmp_path, allowed_signers_path=signers_path)
    assert res_mismatch.status == attestation.STATUS_SUBJECT_MISMATCH
