"""Tests for in-toto Statement v1 and SSHSIG DSSE attestation export and verification."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from brigade import attestation, cli
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
    run_id = "20260902-120000-work-verify-xyz12345"
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
    key_path, signers_path = attestation.keygen(tmp_path, principal="alice-signer")

    assert key_path.is_file()
    assert_private_mode(key_path, PRIVATE_FILE_MODE)
    assert signers_path.is_file()

    lines = signers_path.read_text("utf-8").splitlines()
    assert len(lines) == 1
    assert lines[0].startswith(f'alice-signer namespaces="{attestation.ATTESTATION_NAMESPACE}" ssh-ed25519 ')

    with pytest.raises(FileExistsError):
        attestation.keygen(tmp_path, principal="alice-signer")

    old_key_bytes = key_path.read_bytes()
    new_key_path, _ = attestation.keygen(tmp_path, principal="alice-signer", force=True)
    assert new_key_path == key_path
    assert key_path.read_bytes() != old_key_bytes
    assert_private_mode(key_path, PRIVATE_FILE_MODE)


def test_export_from_completed_receipt_produces_identical_payload_without_leakage(tmp_path: Path) -> None:
    key_path, _signers_path = attestation.keygen(tmp_path, principal="alice-signer")
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


def test_ad_hoc_command_name_drops_env_vars_and_paths(tmp_path: Path) -> None:
    key_path, _signers_path = attestation.keygen(tmp_path, principal="alice-signer")
    receipt = _sample_receipt(tmp_path)
    receipt["commands"][0]["check_id"] = None
    receipt["commands"][0]["command"] = "SECRET=abc /tmp/x/bin/pytest -q /tmp/x/tests"
    receipt["commands"][0]["argv"] = None

    envelope = attestation.export_attestation(receipt, key_path=key_path)
    payload_bytes = base64.b64decode(envelope["payload"])
    statement = json.loads(payload_bytes)
    pred = statement["predicate"]
    assert pred["passedTests"] == ["pytest -q tests"]
    assert pred["failedTests"] == []

    envelope_json = json.dumps(envelope)
    payload_json = payload_bytes.decode("utf-8")
    for secret in ("SECRET", "abc", "/tmp/x"):
        assert secret not in envelope_json
        assert secret not in payload_json


def test_ad_hoc_command_name_with_unbalanced_quote_still_exports(tmp_path: Path) -> None:
    key_path, _signers_path = attestation.keygen(tmp_path, principal="alice-signer")
    receipt = _sample_receipt(tmp_path)
    receipt["commands"][0]["check_id"] = None
    receipt["commands"][0]["command"] = "pytest -q tests/test_'foo"
    receipt["commands"][0]["argv"] = None

    envelope = attestation.export_attestation(receipt, key_path=key_path)
    payload_bytes = base64.b64decode(envelope["payload"])
    statement = json.loads(payload_bytes)
    pred = statement["predicate"]
    assert pred["passedTests"] == ["pytest -q test_'foo"]
    assert pred["failedTests"] == []


def test_ad_hoc_command_name_prefers_argv_over_command(tmp_path: Path) -> None:
    key_path, _signers_path = attestation.keygen(tmp_path, principal="alice-signer")
    receipt = _sample_receipt(tmp_path)
    receipt["commands"][0]["check_id"] = None
    receipt["commands"][0]["command"] = "SHOULD=ignore /old/path/bin/pytest /old/path/tests"
    receipt["commands"][0]["argv"] = ["TOKEN=xyz", "/new/bin/pytest", "-k", "/new/path/test_x.py"]

    envelope = attestation.export_attestation(receipt, key_path=key_path)
    statement = json.loads(base64.b64decode(envelope["payload"]))
    assert statement["predicate"]["passedTests"] == ["pytest -k test_x.py"]


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


def test_verify_rejects_unexpected_types_and_missing_brigade_metadata(tmp_path: Path) -> None:
    key_path, signers_path = attestation.keygen(tmp_path, principal="alice-signer")
    receipt = _sample_receipt(tmp_path)
    envelope = attestation.export_attestation(receipt, key_path=key_path)

    # Swap predicateType to a SLSA URI
    statement = json.loads(base64.b64decode(envelope["payload"]))
    statement["predicateType"] = "https://slsa.dev/provenance/v1"
    envelope["payload"] = base64.b64encode(
        json.dumps(statement, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    res = attestation.verify_attestation(envelope, allowed_signers_path=signers_path)
    assert res.status == attestation.STATUS_UNVERIFIABLE_SIGNATURE

    # Delete brigade metadata
    envelope2 = attestation.export_attestation(receipt, key_path=key_path)
    del envelope2["brigade"]
    res2 = attestation.verify_attestation(envelope2, allowed_signers_path=signers_path)
    assert res2.status == attestation.STATUS_UNVERIFIABLE_SIGNATURE


def test_verify_on_second_tmp_path_reports_signed_ok(tmp_path: Path) -> None:
    path_a = tmp_path / "a"
    path_b = tmp_path / "b"

    key_a, signers_a = attestation.keygen(path_a, principal="alice-signer")
    receipt = _sample_receipt(path_a)
    envelope = attestation.export_attestation(receipt, key_path=key_a)

    signers_b = path_b / ".brigade" / "attestation" / "allowed_signers"
    signers_b.parent.mkdir(parents=True)
    signers_b.write_text(signers_a.read_text("utf-8"), encoding="utf-8")

    res = attestation.verify_attestation(envelope, allowed_signers_path=signers_b)
    assert res.status == attestation.STATUS_SIGNED_OK
    assert res.principal == "alice-signer"
    assert res.keyid == envelope["signatures"][0]["keyid"]
    assert res.run_id == receipt["run_id"]
    assert len(res.subject) == 2


def test_flipping_one_payload_byte_reports_signature_mismatch(tmp_path: Path) -> None:
    key_path, signers_path = attestation.keygen(tmp_path, principal="alice-signer")
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

    key_alice, signers_alice = attestation.keygen(dir_alice, principal="alice-signer")
    key_mallory, _ = attestation.keygen(dir_mallory, principal="mallory-signer")

    receipt = _sample_receipt(tmp_path)
    envelope_mallory = attestation.export_attestation(receipt, key_path=key_mallory)

    res = attestation.verify_attestation(envelope_mallory, allowed_signers_path=signers_alice)
    assert res.status == attestation.STATUS_UNTRUSTED_KEY


def test_ssh_keygen_verify_by_hand_on_extracted_pae_and_sig(tmp_path: Path) -> None:
    key_path, signers_path = attestation.keygen(tmp_path, principal="alice-signer")
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
        "alice-signer",
        "-n",
        attestation.ATTESTATION_NAMESPACE,
        "-s",
        str(sig_file),
    ]
    proc = subprocess.run(cmd, input=pae_bytes, capture_output=True)
    assert proc.returncode == 0
    combined = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    assert f'Good "{attestation.ATTESTATION_NAMESPACE}" signature' in combined


def test_verify_rejects_unsafe_run_id_in_predicate_url(tmp_path: Path) -> None:
    key_path, signers_path = attestation.keygen(tmp_path, principal="alice-signer")
    receipt = _sample_receipt(tmp_path)
    statement = attestation.build_statement(receipt)
    statement["predicate"]["url"] = "urn:brigade:verify:../../etc/passwd"
    envelope = attestation.create_envelope(statement, key_path=key_path)
    res = attestation.verify_attestation(envelope, allowed_signers_path=signers_path)
    assert res.status == attestation.STATUS_SIGNED_OK
    assert res.run_id is None


def test_verify_rejects_dotdot_run_id_and_never_touches_target_work(tmp_path: Path) -> None:
    key_path, signers_path = attestation.keygen(tmp_path, principal="alice-signer")
    receipt = _sample_receipt(tmp_path)
    statement = attestation.build_statement(receipt)
    statement["predicate"]["url"] = "urn:brigade:verify:.."
    envelope = attestation.create_envelope(statement, key_path=key_path)

    work_dir = tmp_path / ".brigade" / "work"
    assert not work_dir.exists()

    res = attestation.verify_attestation(envelope, target=tmp_path, allowed_signers_path=signers_path)
    assert res.status == attestation.STATUS_SIGNED_OK
    assert res.run_id is None
    assert not work_dir.exists()


def test_subject_mismatch_detected_when_target_receipt_differs(tmp_path: Path) -> None:
    key_path, signers_path = attestation.keygen(tmp_path, principal="alice-signer")
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


def _write_receipt_to_disk(receipt: dict[str, Any]) -> None:
    run_dir = Path(receipt["path"])
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")


def test_keygen_force_rotates_allowed_signers_without_trusting_old_key(tmp_path: Path) -> None:
    key_path1, signers_path = attestation.keygen(tmp_path, principal="alice-signer")
    receipt = _sample_receipt(tmp_path)
    envelope = attestation.export_attestation(receipt, key_path=key_path1)

    key_path2, _signers_path2 = attestation.keygen(tmp_path, principal="alice-signer", force=True)
    assert key_path2 == key_path1

    lines = signers_path.read_text("utf-8").splitlines()
    assert len(lines) == 1

    res = attestation.verify_attestation(envelope, allowed_signers_path=signers_path)
    assert res.status == attestation.STATUS_UNTRUSTED_KEY


def test_keygen_force_removes_old_key_from_line_with_extra_options(tmp_path: Path) -> None:
    key_path1, signers_path = attestation.keygen(tmp_path, principal="alice-signer")
    pub_path1 = Path(f"{key_path1}.pub")
    pub_line = pub_path1.read_text(encoding="utf-8").strip()
    pub_parts = pub_line.split()
    assert len(pub_parts) >= 2

    # Append an entry with an extra option before the key type.
    signers_path.write_text(
        f'alice-signer namespaces="{attestation.ATTESTATION_NAMESPACE}" valid-before="20261201" {pub_parts[0]} {pub_parts[1]}\n',
        encoding="utf-8",
    )

    key_path2, _signers_path2 = attestation.keygen(tmp_path, principal="alice-signer", force=True)
    assert key_path2 == key_path1

    lines = signers_path.read_text("utf-8").splitlines()
    assert len(lines) == 1
    assert "valid-before" not in lines[0]
    assert lines[0].startswith(f'alice-signer namespaces="{attestation.ATTESTATION_NAMESPACE}" ssh-ed25519 ')


def test_keygen_force_without_old_public_key_refuses_rotation(tmp_path: Path) -> None:
    key_path1, signers_path = attestation.keygen(tmp_path, principal="alice-signer")
    pub_path1 = Path(f"{key_path1}.pub")
    pub_path1.unlink()

    original_lines = signers_path.read_text(encoding="utf-8")
    original_key_bytes = key_path1.read_bytes()

    with pytest.raises(attestation.AttestationError) as exc_info:
        attestation.keygen(tmp_path, principal="alice-signer", force=True)

    assert "refusing rotation" in str(exc_info.value)
    assert pub_path1.name in str(exc_info.value)
    assert signers_path.read_text(encoding="utf-8") == original_lines
    assert key_path1.read_bytes() == original_key_bytes


def test_cli_attestation_keygen_writes_key_and_refuses_without_force(tmp_path, capsys):
    rc = cli.main(["receipts", "attestation-keygen", "--target", str(tmp_path), "--principal", "alice-signer"])
    captured = capsys.readouterr()
    assert rc == 0

    key_path = tmp_path / ".brigade" / "attestation" / "signing-key"
    signers_path = tmp_path / ".brigade" / "attestation" / "allowed_signers"
    assert key_path.is_file()
    assert_private_mode(key_path, PRIVATE_FILE_MODE)
    assert signers_path.is_file()
    assert "attestation signing key" in captured.out

    rc2 = cli.main(["receipts", "attestation-keygen", "--target", str(tmp_path)])
    captured2 = capsys.readouterr()
    assert rc2 == 1
    assert "already exists" in captured2.err
    assert "--force" in captured2.err

    rc3 = cli.main(["receipts", "attestation-keygen", "--target", str(tmp_path), "--force"])
    assert rc3 == 0
    assert_private_mode(key_path, PRIVATE_FILE_MODE)


def test_cli_export_attestation_to_default_path(tmp_path):
    key_path, _signers = attestation.keygen(tmp_path, principal="alice-signer")
    receipt = _sample_receipt(tmp_path)
    _write_receipt_to_disk(receipt)

    run_id = receipt["run_id"]
    rc = cli.main(["receipts", "export", "attestation", "--target", str(tmp_path), "--run-id", run_id])
    assert rc == 0

    attestation_path = Path(receipt["path"]) / "attestation.json"
    assert attestation_path.is_file()
    envelope = json.loads(attestation_path.read_text(encoding="utf-8"))
    assert envelope["payloadType"] == attestation.DSSE_PAYLOAD_TYPE
    assert envelope["brigade"]["profile"] == attestation.ATTESTATION_PROFILE
    assert envelope["brigade"]["namespace"] == attestation.ATTESTATION_NAMESPACE


def test_cli_export_attestation_to_stdout(tmp_path, capsys):
    key_path, _signers = attestation.keygen(tmp_path, principal="alice-signer")
    receipt = _sample_receipt(tmp_path)
    _write_receipt_to_disk(receipt)

    run_id = receipt["run_id"]
    rc = cli.main(
        [
            "receipts",
            "export",
            "attestation",
            "--target",
            str(tmp_path),
            "--run-id",
            run_id,
            "--out",
            "-",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    envelope = json.loads(captured.out)
    assert envelope["payloadType"] == attestation.DSSE_PAYLOAD_TYPE
    assert envelope["brigade"]["profile"] == attestation.ATTESTATION_PROFILE


def test_cli_export_attestation_refuses_overwrite_without_force(tmp_path, capsys):
    key_path, _signers = attestation.keygen(tmp_path, principal="alice-signer")
    receipt = _sample_receipt(tmp_path)
    _write_receipt_to_disk(receipt)

    run_id = receipt["run_id"]
    rc1 = cli.main(["receipts", "export", "attestation", "--target", str(tmp_path), "--run-id", run_id])
    assert rc1 == 0

    rc2 = cli.main(["receipts", "export", "attestation", "--target", str(tmp_path), "--run-id", run_id])
    captured = capsys.readouterr()
    assert rc2 == 1
    assert "already exists" in captured.err
    assert "use --force" in captured.err

    rc3 = cli.main(["receipts", "export", "attestation", "--target", str(tmp_path), "--run-id", run_id, "--force"])
    assert rc3 == 0


def test_cli_export_attestation_run_id_latest(tmp_path):
    key_path, _signers = attestation.keygen(tmp_path, principal="alice-signer")
    receipt = _sample_receipt(tmp_path)
    _write_receipt_to_disk(receipt)

    rc = cli.main(["receipts", "export", "attestation", "--target", str(tmp_path), "--run-id", "latest"])
    assert rc == 0

    attestation_path = Path(receipt["path"]) / "attestation.json"
    assert attestation_path.is_file()
    envelope = json.loads(attestation_path.read_text(encoding="utf-8"))
    assert envelope["payloadType"] == attestation.DSSE_PAYLOAD_TYPE


def test_cli_export_without_receipt_digest_verifies_against_target(tmp_path, capsys):
    key_path, _signers = attestation.keygen(tmp_path, principal="alice-signer")
    receipt = _sample_receipt(tmp_path)
    receipt.pop("digests")
    receipt_path = Path(receipt["path"]) / "receipt.json"
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    disk_receipt = dict(receipt)
    disk_receipt.pop("path")
    receipt_path.write_text(json.dumps(disk_receipt), encoding="utf-8")

    run_id = receipt["run_id"]
    assert cli.main(["receipts", "export", "attestation", "--target", str(tmp_path), "--run-id", run_id]) == 0
    capsys.readouterr()

    attestation_path = Path(receipt["path"]) / "attestation.json"
    assert cli.main(["receipts", "verify-attestation", str(attestation_path), "--target", str(tmp_path)]) == 0
    assert capsys.readouterr().out.strip() == attestation.STATUS_SIGNED_OK


def test_cli_export_refuses_unsafe_run_id_before_path_resolution(tmp_path, capsys):
    attestation.keygen(tmp_path, principal="alice-signer")

    assert cli.main(["receipts", "export", "attestation", "--target", str(tmp_path), "--run-id", "../../etc"]) == 1
    assert (
        capsys.readouterr().err.strip()
        == "error: run id must be 'latest' or contain only letters, digits, dot, underscore, or hyphen"
    )
    assert list((tmp_path / ".brigade" / "work" / "verify-runs").glob("**/*")) == []


def test_sign_pae_converts_ssh_keygen_timeout_to_attestation_error(tmp_path, monkeypatch):
    key_path = tmp_path / "signing-key"
    key_path.write_text("placeholder", encoding="utf-8")

    def _timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(attestation.subprocess, "run", _timeout)
    with pytest.raises(attestation.AttestationError, match="timed out"):
        attestation.sign_pae(b"payload", key_path)


def test_cli_verify_attestation_with_revoked_keys_fails(tmp_path, capsys):
    key_path, signers_path = attestation.keygen(tmp_path, principal="alice-signer")
    receipt = _sample_receipt(tmp_path)
    _write_receipt_to_disk(receipt)

    run_id = receipt["run_id"]
    rc_export = cli.main(["receipts", "export", "attestation", "--target", str(tmp_path), "--run-id", run_id])
    assert rc_export == 0
    attestation_path = Path(receipt["path"]) / "attestation.json"

    pub_path = Path(f"{key_path}.pub")
    krl_path = tmp_path / ".brigade" / "attestation" / "revoked_keys"
    subprocess.run(["ssh-keygen", "-k", "-f", str(krl_path), str(pub_path)], check=True, capture_output=True)

    rc = cli.main(
        [
            "receipts",
            "verify-attestation",
            str(attestation_path),
            "--allowed-signers",
            str(signers_path),
            "--revoked-keys",
            str(krl_path),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out.strip() == attestation.STATUS_UNTRUSTED_KEY


def test_cli_verify_attestation_with_missing_revoked_keys_returns_unverifiable(tmp_path, capsys):
    key_path, signers_path = attestation.keygen(tmp_path, principal="alice-signer")
    receipt = _sample_receipt(tmp_path)
    _write_receipt_to_disk(receipt)

    run_id = receipt["run_id"]
    rc_export = cli.main(["receipts", "export", "attestation", "--target", str(tmp_path), "--run-id", run_id])
    assert rc_export == 0
    attestation_path = Path(receipt["path"]) / "attestation.json"

    rc = cli.main(
        [
            "receipts",
            "verify-attestation",
            str(attestation_path),
            "--allowed-signers",
            str(signers_path),
            "--revoked-keys",
            "/nonexistent",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert captured.out.strip() == attestation.STATUS_UNVERIFIABLE_SIGNATURE


def test_cli_verify_attestation_exit_and_json_shape(tmp_path, capsys):
    key_path, signers_path = attestation.keygen(tmp_path, principal="alice-signer")
    receipt = _sample_receipt(tmp_path)
    _write_receipt_to_disk(receipt)

    run_id = receipt["run_id"]
    rc_export = cli.main(["receipts", "export", "attestation", "--target", str(tmp_path), "--run-id", run_id])
    assert rc_export == 0
    attestation_path = Path(receipt["path"]) / "attestation.json"

    rc = cli.main(
        [
            "receipts",
            "verify-attestation",
            str(attestation_path),
            "--allowed-signers",
            str(signers_path),
        ]
    )
    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out.strip() == attestation.STATUS_SIGNED_OK

    rc_json = cli.main(
        [
            "receipts",
            "verify-attestation",
            str(attestation_path),
            "--allowed-signers",
            str(signers_path),
            "--json",
        ]
    )
    captured_json = capsys.readouterr()
    assert rc_json == 0
    out = json.loads(captured_json.out)
    assert out["schema"] == "brigade.attestation_verify_result.v1"
    assert out["status"] == attestation.STATUS_SIGNED_OK
    assert out["principal"] == "alice-signer"
    assert out["keyid"].startswith("SHA256:")
    assert out["run_id"] == run_id
    assert isinstance(out["subject"], list)
    assert len(out["subject"]) == 2

    envelope = json.loads(attestation_path.read_text(encoding="utf-8"))
    payload_bytes = base64.b64decode(envelope["payload"])
    tampered_bytes = payload_bytes.replace(b"PASSED", b"FAILED")
    envelope["payload"] = base64.b64encode(tampered_bytes).decode("ascii")
    attestation_path.write_text(json.dumps(envelope), encoding="utf-8")

    rc_bad = cli.main(
        [
            "receipts",
            "verify-attestation",
            str(attestation_path),
            "--allowed-signers",
            str(signers_path),
        ]
    )
    captured_bad = capsys.readouterr()
    assert rc_bad != 0
    assert captured_bad.out.strip() == attestation.STATUS_SIGNATURE_MISMATCH


def test_cli_cross_machine_verify_attestation_and_ssh_keygen(tmp_path):
    path_a = tmp_path / "a"
    path_b = tmp_path / "b"

    key_path, signers_path = attestation.keygen(path_a, principal="alice-signer")
    receipt = _sample_receipt(path_a)
    _write_receipt_to_disk(receipt)

    run_id = receipt["run_id"]
    rc_export = cli.main(["receipts", "export", "attestation", "--target", str(path_a), "--run-id", run_id])
    assert rc_export == 0
    attestation_path_a = Path(receipt["path"]) / "attestation.json"

    attestation_path_b = path_b / "attestation.json"
    signers_path_b = path_b / ".brigade" / "attestation" / "allowed_signers"
    pub_path_b = path_b / ".brigade" / "attestation" / "signing-key.pub"
    signers_path_b.parent.mkdir(parents=True)
    attestation_path_b.write_text(attestation_path_a.read_text(encoding="utf-8"), encoding="utf-8")
    signers_path_b.write_text(signers_path.read_text(encoding="utf-8"), encoding="utf-8")
    pub_path_b.write_text(key_path.with_suffix(".pub").read_text(encoding="utf-8"), encoding="utf-8")

    rc = cli.main(
        [
            "receipts",
            "verify-attestation",
            str(attestation_path_b),
            "--target",
            str(path_b),
        ]
    )
    assert rc == 0

    envelope = json.loads(attestation_path_b.read_text(encoding="utf-8"))
    payload_bytes = base64.b64decode(envelope["payload"])
    pae_bytes = attestation.dsse_pae(envelope["payloadType"], payload_bytes)
    sig_b64 = envelope["signatures"][0]["sig"]
    armored_sig = base64.b64decode(sig_b64)

    sig_file = tmp_path / "extracted.sig"
    sig_file.write_bytes(armored_sig)
    cmd = [
        "ssh-keygen",
        "-Y",
        "verify",
        "-f",
        str(signers_path_b),
        "-I",
        "alice-signer",
        "-n",
        attestation.ATTESTATION_NAMESPACE,
        "-s",
        str(sig_file),
    ]
    proc = subprocess.run(cmd, input=pae_bytes, capture_output=True)
    assert proc.returncode == 0
    combined = (proc.stdout + proc.stderr).decode("utf-8", errors="replace")
    assert f'Good "{attestation.ATTESTATION_NAMESPACE}" signature' in combined
