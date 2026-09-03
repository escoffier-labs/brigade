"""Unit tests for cosign attest-blob signer profile and Sigstore bundle validation."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from brigade import attestation, attestation_cmd, cli, cosign_attestation


def _sample_statement() -> dict[str, Any]:
    return {
        "_type": attestation.IN_TOTO_STATEMENT_TYPE,
        "subject": [
            {"name": "git:tree", "digest": {"gitTree": "1111111111111111111111111111111111111111"}},
            {
                "name": "changes.patch",
                "digest": {"sha256": "2222222222222222222222222222222222222222222222222222222222222222"},
            },
        ],
        "predicateType": attestation.IN_TOTO_TEST_RESULT_PREDICATE_TYPE,
        "predicate": {
            "result": "PASSED",
            "configuration": [
                {
                    "name": "receipt.json",
                    "uri": "urn:brigade:verify:test-run:receipt",
                    "digest": {"sha256": "4444444444444444444444444444444444444444444444444444444444444444"},
                    "mediaType": "application/json",
                }
            ],
            "url": "urn:brigade:verify:test-run",
            "passedTests": ["unit-tests"],
            "warnedTests": [],
            "failedTests": [],
        },
    }


def _make_valid_bundle(statement: Mapping[str, Any]) -> dict[str, Any]:
    payload_b64 = base64.b64encode(attestation.canonical_statement_bytes(statement)).decode("ascii")
    return {
        "mediaType": cosign_attestation.SIGSTORE_BUNDLE_MEDIA_TYPE,
        "verificationMaterial": {
            "publicKey": {
                "hint": "dummy-key-hint",
            },
            "tlogEntries": [],
            "timestampVerificationData": {},
        },
        "dsseEnvelope": {
            "payloadType": attestation.DSSE_PAYLOAD_TYPE,
            "payload": payload_b64,
            "signatures": [{"sig": base64.b64encode(b"dummy-sig").decode("ascii"), "keyid": "key1"}],
        },
    }


def test_parse_cosign_version_accepts_safe_stable_releases() -> None:
    assert cosign_attestation.parse_cosign_version('{"gitVersion":"v2.6.5"}') == (2, 6, 5)
    assert cosign_attestation.parse_cosign_version("GitVersion: v3.1.3") == (3, 1, 3)


@pytest.mark.parametrize(
    "version_str",
    [
        "v2.6.4",
        "v3.1.2",
        "v3.1.3-rc.1",
        "v1.13.6",
        "v3.1.3-1ubuntu1",
        "v2.6.5+distro",
        "v3.1.3~deb12",
    ],
)
def test_require_safe_cosign_rejects_unsafe_or_prerelease_versions(
    monkeypatch: pytest.MonkeyPatch, version_str: str
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/cosign")

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"gitVersion": version_str}), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(cosign_attestation.CosignAttestationError) as exc_info:
        cosign_attestation.require_safe_cosign()
    msg = str(exc_info.value)
    assert "2.6.5" in msg
    assert "3.1.3" in msg


def test_require_safe_cosign_rejects_nonzero_exit_even_with_parseable_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/cosign")

    # 1. Nonzero exit with parseable safe version on stdout and empty stderr
    def fake_run_stdout(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout=json.dumps({"gitVersion": "v3.1.3"}), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run_stdout)
    with pytest.raises(cosign_attestation.CosignAttestationError) as exc_info:
        cosign_attestation.require_safe_cosign()
    assert "cosign version failed with exit code 1" in str(exc_info.value)

    # 2. Nonzero exit with parseable safe version on stderr
    def fake_run_stderr(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="GitVersion: v3.1.3\nfatal: something broke")

    monkeypatch.setattr(subprocess, "run", fake_run_stderr)
    with pytest.raises(cosign_attestation.CosignAttestationError) as exc_info:
        cosign_attestation.require_safe_cosign()
    assert "cosign version failed with exit code 2: GitVersion: v3.1.3\nfatal: something broke" in str(exc_info.value)

    # 3. Nonzero exit with stderr exceeding 2000 chars is capped at 2000 chars
    long_err = "err_prefix: " + ("z" * 3500)

    def fake_run_long_err(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout=json.dumps({"gitVersion": "v3.1.3"}), stderr=long_err)

    monkeypatch.setattr(subprocess, "run", fake_run_long_err)
    with pytest.raises(cosign_attestation.CosignAttestationError) as exc_info:
        cosign_attestation.require_safe_cosign()
    err_str = str(exc_info.value)
    prefix = "cosign version failed with exit code 1: "
    assert err_str.startswith(prefix)
    exposed_stderr = err_str[len(prefix) :]
    assert len(exposed_stderr) <= 2000
    assert "z" * 2001 not in err_str


def test_create_bundle_v2_uses_statement_and_standard_bundle_flags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/cosign")
    captured_argv: list[str] = []

    statement = _sample_statement()
    key_path = tmp_path / "cosign.key"
    key_path.write_text("dummy-private-key", encoding="utf-8")

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"gitVersion": "v2.6.5"}), stderr="")
        captured_argv.extend(cmd)
        bundle_idx = cmd.index("--bundle")
        bundle_out = Path(cmd[bundle_idx + 1])
        valid_bundle = _make_valid_bundle(statement)
        bundle_out.write_text(json.dumps(valid_bundle), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    bundle = cosign_attestation.create_bundle(statement, key_path)
    assert bundle["mediaType"] == cosign_attestation.SIGSTORE_BUNDLE_MEDIA_TYPE

    assert "attest-blob" in captured_argv
    assert "--statement" in captured_argv
    assert "--key" in captured_argv
    assert "--bundle" in captured_argv
    assert "--new-bundle-format=true" in captured_argv
    assert "--tlog-upload=false" in captured_argv
    assert "--signing-config" not in captured_argv
    assert "--yes" in captured_argv

    forbidden_terms = ("oidc", "fulcio", "rekor", "identity", "certificate")
    for arg in captured_argv:
        for forbidden in forbidden_terms:
            assert forbidden not in arg.lower(), f"forbidden argument fragment '{forbidden}' in '{arg}'"


def test_create_bundle_v3_omits_legacy_bundle_switch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/cosign")
    captured_argv: list[str] = []
    captured_signing_config: dict[str, Any] | None = None

    statement = _sample_statement()
    key_path = tmp_path / "cosign.key"
    key_path.write_text("dummy-private-key", encoding="utf-8")

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal captured_signing_config
        if "version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"gitVersion": "v3.1.3"}), stderr="")
        captured_argv.extend(cmd)
        if "--signing-config" in cmd:
            cfg_path = Path(cmd[cmd.index("--signing-config") + 1])
            captured_signing_config = json.loads(cfg_path.read_text(encoding="utf-8"))
        bundle_idx = cmd.index("--bundle")
        bundle_out = Path(cmd[bundle_idx + 1])
        valid_bundle = _make_valid_bundle(statement)
        bundle_out.write_text(json.dumps(valid_bundle), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    bundle = cosign_attestation.create_bundle(statement, key_path)
    assert bundle["mediaType"] == cosign_attestation.SIGSTORE_BUNDLE_MEDIA_TYPE
    assert "--new-bundle-format=true" not in captured_argv
    assert "--tlog-upload=false" not in captured_argv
    assert "--signing-config" in captured_argv

    assert captured_signing_config == {
        "mediaType": "application/vnd.dev.sigstore.signingconfig.v0.2+json",
        "rekorTlogConfig": {},
        "tsaConfig": {},
    }


@pytest.mark.parametrize(
    ("anomaly_kind",),
    [
        ("wrong_media_type",),
        ("wrong_payload_type",),
        ("unequal_statement",),
        ("zero_signatures",),
        ("two_signatures",),
        ("missing_verification_material",),
        ("non_object_verification_material",),
        ("missing_public_key",),
        ("non_object_public_key",),
        ("missing_hint",),
        ("empty_hint",),
        ("whitespace_hint",),
        ("non_string_hint",),
        ("nonempty_tlog_entries",),
        ("nonempty_bundle_tlog_entries",),
        ("nonempty_timestamp_data",),
        ("nonempty_bundle_timestamp_data",),
        ("missing_sig",),
        ("empty_sig",),
        ("invalid_base64_sig",),
        ("non_string_sig",),
    ],
)
def test_create_bundle_rejects_nonconforming_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, anomaly_kind: str
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/cosign")
    statement = _sample_statement()
    key_path = tmp_path / "cosign.key"
    key_path.write_text("dummy-private-key", encoding="utf-8")

    def fake_run(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "version" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"gitVersion": "v3.1.3"}), stderr="")
        bundle_idx = cmd.index("--bundle")
        bundle_out = Path(cmd[bundle_idx + 1])
        malformed_bundle = _make_valid_bundle(statement)

        if anomaly_kind == "wrong_media_type":
            malformed_bundle["mediaType"] = "application/vnd.dev.sigstore.bundle.v0.2+json"
        elif anomaly_kind == "wrong_payload_type":
            malformed_bundle["dsseEnvelope"]["payloadType"] = "text/plain"
        elif anomaly_kind == "unequal_statement":
            different_statement = dict(statement)
            different_statement["predicate"] = {"result": "FAILED"}
            malformed_bundle["dsseEnvelope"]["payload"] = base64.b64encode(
                attestation.canonical_statement_bytes(different_statement)
            ).decode("ascii")
        elif anomaly_kind == "zero_signatures":
            malformed_bundle["dsseEnvelope"]["signatures"] = []
        elif anomaly_kind == "two_signatures":
            malformed_bundle["dsseEnvelope"]["signatures"] = [
                {"sig": "c2lnMQ==", "keyid": "k1"},
                {"sig": "c2lnMg==", "keyid": "k2"},
            ]
        elif anomaly_kind == "missing_verification_material":
            del malformed_bundle["verificationMaterial"]
        elif anomaly_kind == "non_object_verification_material":
            malformed_bundle["verificationMaterial"] = "not-an-object"
        elif anomaly_kind == "missing_public_key":
            del malformed_bundle["verificationMaterial"]["publicKey"]
        elif anomaly_kind == "non_object_public_key":
            malformed_bundle["verificationMaterial"]["publicKey"] = "not-an-object"
        elif anomaly_kind == "missing_hint":
            del malformed_bundle["verificationMaterial"]["publicKey"]["hint"]
        elif anomaly_kind == "empty_hint":
            malformed_bundle["verificationMaterial"]["publicKey"]["hint"] = ""
        elif anomaly_kind == "whitespace_hint":
            malformed_bundle["verificationMaterial"]["publicKey"]["hint"] = "   "
        elif anomaly_kind == "non_string_hint":
            malformed_bundle["verificationMaterial"]["publicKey"]["hint"] = 12345
        elif anomaly_kind == "nonempty_tlog_entries":
            malformed_bundle["verificationMaterial"]["tlogEntries"] = [{"logIndex": 42}]
        elif anomaly_kind == "nonempty_bundle_tlog_entries":
            malformed_bundle["tlogEntries"] = [{"logIndex": 42}]
        elif anomaly_kind == "nonempty_timestamp_data":
            malformed_bundle["verificationMaterial"]["timestampVerificationData"] = {
                "rfc3161Timestamps": [{"timestamp": "fake"}]
            }
        elif anomaly_kind == "nonempty_bundle_timestamp_data":
            malformed_bundle["timestampVerificationData"] = {"rfc3161Timestamps": [{"timestamp": "fake"}]}
        elif anomaly_kind == "missing_sig":
            malformed_bundle["dsseEnvelope"]["signatures"] = [{"keyid": "k1"}]
        elif anomaly_kind == "empty_sig":
            malformed_bundle["dsseEnvelope"]["signatures"] = [{"sig": "", "keyid": "k1"}]
        elif anomaly_kind == "invalid_base64_sig":
            malformed_bundle["dsseEnvelope"]["signatures"] = [{"sig": "not-valid-base64!", "keyid": "k1"}]
        elif anomaly_kind == "non_string_sig":
            malformed_bundle["dsseEnvelope"]["signatures"] = [{"sig": 12345, "keyid": "k1"}]

        bundle_out.write_text(json.dumps(malformed_bundle), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(cosign_attestation.CosignAttestationError):
        cosign_attestation.create_bundle(statement, key_path)


@pytest.mark.parametrize(
    ("mutate", "expected_err"),
    [
        (lambda b: b.pop("verificationMaterial"), "verificationMaterial"),
        (lambda b: b.__setitem__("verificationMaterial", None), "verificationMaterial"),
        (lambda b: b.__setitem__("verificationMaterial", "not-an-object"), "verificationMaterial"),
        (lambda b: b.__setitem__("verificationMaterial", [1, 2, 3]), "verificationMaterial"),
        (lambda b: b.__setitem__("verificationMaterial", 123), "verificationMaterial"),
        (lambda b: b.__setitem__("verificationMaterial", False), "verificationMaterial"),
        (lambda b: b["verificationMaterial"].pop("publicKey"), "publicKey"),
        (lambda b: b["verificationMaterial"].__setitem__("publicKey", None), "publicKey"),
        (lambda b: b["verificationMaterial"].__setitem__("publicKey", "not-an-object"), "publicKey"),
        (lambda b: b["verificationMaterial"].__setitem__("publicKey", [1, 2, 3]), "publicKey"),
        (lambda b: b["verificationMaterial"]["publicKey"].pop("hint"), "hint"),
        (lambda b: b["verificationMaterial"]["publicKey"].__setitem__("hint", ""), "hint"),
        (lambda b: b["verificationMaterial"]["publicKey"].__setitem__("hint", "   "), "hint"),
        (lambda b: b["verificationMaterial"]["publicKey"].__setitem__("hint", None), "hint"),
        (lambda b: b["verificationMaterial"]["publicKey"].__setitem__("hint", 12345), "hint"),
        (
            lambda b: b["verificationMaterial"].__setitem__("tlogEntries", [{"logIndex": 1}]),
            "tlogEntries",
        ),
        (lambda b: b.__setitem__("tlogEntries", [{"logIndex": 1}]), "tlogEntries"),
        (
            lambda b: b["verificationMaterial"].__setitem__("timestampVerificationData", {"rfc3161Timestamps": [{}]}),
            "timestampVerificationData",
        ),
        (
            lambda b: b.__setitem__("timestampVerificationData", {"rfc3161Timestamps": [{}]}),
            "timestampVerificationData",
        ),
        (lambda b: b["dsseEnvelope"]["signatures"][0].pop("sig"), "nonempty base64"),
        (lambda b: b["dsseEnvelope"]["signatures"][0].__setitem__("sig", ""), "nonempty base64"),
        (lambda b: b["dsseEnvelope"]["signatures"][0].__setitem__("sig", "   "), "nonempty base64"),
        (lambda b: b["dsseEnvelope"]["signatures"][0].__setitem__("sig", None), "nonempty base64"),
        (lambda b: b["dsseEnvelope"]["signatures"][0].__setitem__("sig", 12345), "nonempty base64"),
        (lambda b: b["dsseEnvelope"]["signatures"][0].__setitem__("sig", "invalid-base64!*"), "invalid base64"),
        (lambda b: b["dsseEnvelope"]["signatures"][0].__setitem__("sig", "abc"), "invalid base64"),
    ],
)
def test_validate_bundle_malformed_cases(mutate: Callable[[dict[str, Any]], None], expected_err: str) -> None:
    statement = _sample_statement()
    bundle = _make_valid_bundle(statement)
    mutate(bundle)
    with pytest.raises(cosign_attestation.CosignAttestationError) as exc_info:
        cosign_attestation.validate_bundle(bundle, statement)
    assert expected_err in str(exc_info.value)


def test_validate_bundle_accepts_conforming_bundle() -> None:
    statement = _sample_statement()
    bundle = _make_valid_bundle(statement)
    validated = cosign_attestation.validate_bundle(bundle, statement)
    assert validated["mediaType"] == cosign_attestation.SIGSTORE_BUNDLE_MEDIA_TYPE
    assert isinstance(validated["verificationMaterial"], Mapping)


@pytest.mark.parametrize("error_kind", ["missing_binary", "timeout", "nonzero_exit"])
def test_create_bundle_maps_missing_binary_timeout_and_nonzero_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_kind: str
) -> None:
    statement = _sample_statement()
    private_key_content = "SUPER_SECRET_PRIVATE_KEY_BYTES_CONTENT_GUARD_FIXTURE"
    key_path = tmp_path / "cosign.key"
    key_path.write_text(private_key_content, encoding="utf-8")

    long_stderr = "cosign attest-blob: signing failed: bad password or key error\n" + ("x" * 3000)

    if error_kind == "missing_binary":
        monkeypatch.setattr(shutil, "which", lambda _name: None)
    elif error_kind == "timeout":
        monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/cosign")

        def fake_run_timeout(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if "version" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"gitVersion": "v3.1.3"}), stderr="")
            raise subprocess.TimeoutExpired(cmd, 180)

        monkeypatch.setattr(subprocess, "run", fake_run_timeout)
    elif error_kind == "nonzero_exit":
        monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/cosign")

        def fake_run_error(cmd: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if "version" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"gitVersion": "v3.1.3"}), stderr="")
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr=long_stderr)

        monkeypatch.setattr(subprocess, "run", fake_run_error)

    with pytest.raises(cosign_attestation.CosignAttestationError) as exc_info:
        cosign_attestation.create_bundle(statement, key_path)

    err_msg = str(exc_info.value)
    assert len(err_msg) > 0
    assert private_key_content not in err_msg

    if error_kind == "missing_binary":
        assert "cosign" in err_msg.lower()
        assert "path" in err_msg.lower() or "not found" in err_msg.lower()
    elif error_kind == "timeout":
        assert "timed out" in err_msg.lower()
    elif error_kind == "nonzero_exit":
        assert "failed" in err_msg.lower() or "exit" in err_msg.lower()
        assert "COSIGN_PASSWORD" in err_msg
        # Stderr excerpt must be limited to at most 2,000 characters
        assert len(long_stderr) > 2000
        assert len(err_msg) <= 2500


def _sample_receipt(tmp_path: Path) -> dict[str, Any]:
    run_id = "20260903-120000-work-verify-cosign123"
    run_dir = tmp_path / ".brigade" / "work" / "verify-runs" / run_id
    return {
        "schema_version": 2,
        "run_id": run_id,
        "target": str(tmp_path),
        "status": "completed",
        "path": str(run_dir),
        "tree_fingerprint": "1111111111111111111111111111111111111111",
        "changes_patch_sha256": "2222222222222222222222222222222222222222222222222222222222222222",
        "commands": [
            {
                "command": "pytest -q tests/test_unit.py",
                "check_id": "unit-tests",
                "status": "completed",
                "exit_code": 0,
            }
        ],
        "digests": {
            "algorithm": "sha256",
            "receipt_sha256": "4444444444444444444444444444444444444444444444444444444444444444",
        },
    }


def _write_receipt_to_disk(receipt: dict[str, Any]) -> None:
    run_dir = Path(receipt["path"])
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")


def test_cli_export_attestation_defaults_to_sshsig(tmp_path: Path) -> None:
    attestation.keygen(tmp_path, principal="alice-signer")
    receipt = _sample_receipt(tmp_path)
    _write_receipt_to_disk(receipt)

    run_id = receipt["run_id"]
    rc = cli.main(["receipts", "export", "attestation", "--target", str(tmp_path), "--run-id", run_id])
    assert rc == 0

    attestation_path = Path(receipt["path"]) / "attestation.json"
    assert attestation_path.is_file()
    envelope = json.loads(attestation_path.read_text(encoding="utf-8"))
    assert envelope["brigade"]["profile"] == "brigade.sshsig-dsse.v1"


def test_cli_export_attestation_cosign_profile_uses_separate_default_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    receipt = _sample_receipt(tmp_path)
    _write_receipt_to_disk(receipt)
    run_id = receipt["run_id"]

    cosign_key_path = tmp_path / ".brigade" / "attestation" / "cosign.key"
    cosign_key_path.parent.mkdir(parents=True, exist_ok=True)
    cosign_key_path.write_text("dummy-cosign-key", encoding="utf-8")

    received_receipt: dict[str, Any] | None = None
    received_key: Path | None = None
    mock_bundle = {
        "mediaType": cosign_attestation.SIGSTORE_BUNDLE_MEDIA_TYPE,
        "mock": "cosign-bundle",
    }

    def fake_export(receipt_data: Mapping[str, Any], key_path: Path) -> dict[str, Any]:
        nonlocal received_receipt, received_key
        received_receipt = dict(receipt_data)
        received_key = key_path
        return mock_bundle

    monkeypatch.setattr(cosign_attestation, "export_attestation", fake_export)

    rc = cli.main(
        [
            "receipts",
            "export",
            "attestation",
            "--target",
            str(tmp_path),
            "--run-id",
            run_id,
            "--profile",
            "cosign",
        ]
    )
    assert rc == 0
    assert received_receipt is not None
    assert received_receipt["run_id"] == run_id
    assert received_key == cosign_key_path.resolve()

    bundle_path = Path(receipt["path"]) / "attestation.sigstore.json"
    assert bundle_path.is_file()
    saved_bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert saved_bundle == mock_bundle


def test_cli_export_attestation_cosign_stdout_and_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    receipt = _sample_receipt(tmp_path)
    _write_receipt_to_disk(receipt)
    run_id = receipt["run_id"]

    cosign_key_path = tmp_path / ".brigade" / "attestation" / "cosign.key"
    cosign_key_path.parent.mkdir(parents=True, exist_ok=True)
    cosign_key_path.write_text("dummy-cosign-key", encoding="utf-8")

    mock_bundle = {
        "mediaType": cosign_attestation.SIGSTORE_BUNDLE_MEDIA_TYPE,
        "z_field": 1,
        "a_field": 2,
    }
    monkeypatch.setattr(cosign_attestation, "export_attestation", lambda r, key_path: mock_bundle)

    # 1. stdout: --out - prints sorted bundle JSON
    rc0 = cli.main(
        [
            "receipts",
            "export",
            "attestation",
            "--target",
            str(tmp_path),
            "--run-id",
            run_id,
            "--profile",
            "cosign",
            "--out",
            "-",
        ]
    )
    captured0 = capsys.readouterr()
    assert rc0 == 0
    expected_stdout = json.dumps(mock_bundle, indent=2, sort_keys=True) + "\n"
    assert captured0.out == expected_stdout

    # 2. first file export succeeds
    rc1 = cli.main(
        [
            "receipts",
            "export",
            "attestation",
            "--target",
            str(tmp_path),
            "--run-id",
            run_id,
            "--profile",
            "cosign",
        ]
    )
    assert rc1 == 0
    capsys.readouterr()

    # 3. second file export exits 1 and names --force
    rc2 = cli.main(
        [
            "receipts",
            "export",
            "attestation",
            "--target",
            str(tmp_path),
            "--run-id",
            run_id,
            "--profile",
            "cosign",
        ]
    )
    captured2 = capsys.readouterr()
    assert rc2 == 1
    assert "--force" in captured2.err

    # 4. third export with --force exits 0
    rc3 = cli.main(
        [
            "receipts",
            "export",
            "attestation",
            "--target",
            str(tmp_path),
            "--run-id",
            run_id,
            "--profile",
            "cosign",
            "--force",
        ]
    )
    assert rc3 == 0


def test_cli_export_attestation_cosign_missing_key_is_actionable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
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
            "--profile",
            "cosign",
        ]
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert ".brigade/attestation/cosign.key" in captured.err
    assert "--key" in captured.err


@pytest.mark.parametrize("invalid_profile", ["bogus", "rsa", "gpg", "", "SSHsig", "COSIGN"])
def test_export_attestation_rejects_unsupported_profile_direct_python_caller(
    tmp_path: Path, invalid_profile: str, capsys: pytest.CaptureFixture[str]
) -> None:
    attestation.keygen(tmp_path, principal="alice-signer")
    receipt = _sample_receipt(tmp_path)
    _write_receipt_to_disk(receipt)
    run_id = receipt["run_id"]

    rc = attestation_cmd.export_attestation(
        target=tmp_path,
        run_id=run_id,
        profile=invalid_profile,
    )
    assert rc == 1
    captured = capsys.readouterr()
    assert f"unsupported attestation profile '{invalid_profile}'" in captured.err
    assert "sshsig" in captured.err
    assert "cosign" in captured.err

    # Must never silently fall back to SSHSIG or write any attestation artifact
    run_dir = Path(receipt["path"])
    assert not (run_dir / "attestation.json").exists()
    assert not (run_dir / "attestation.sigstore.json").exists()


@pytest.mark.skipif(shutil.which("cosign") is None, reason="cosign is required for interoperability test")
def test_real_cosign_bundle_verifies_with_git_tree_subject(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    try:
        cosign_bin, _version = cosign_attestation.require_safe_cosign()
    except cosign_attestation.CosignAttestationError as exc:
        pytest.skip(str(exc))

    monkeypatch.setenv("COSIGN_PASSWORD", "fixture-password")

    key_dir = tmp_path / ".brigade" / "attestation"
    key_dir.mkdir(parents=True, exist_ok=True)
    key_prefix = key_dir / "cosign"
    public_key_path = key_dir / "cosign.pub"

    keygen_cmd = [
        cosign_bin,
        "generate-key-pair",
        "--output-key-prefix",
        str(key_prefix),
    ]
    res_keygen = subprocess.run(
        keygen_cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert res_keygen.returncode == 0

    receipt = _sample_receipt(tmp_path)
    _write_receipt_to_disk(receipt)
    run_id = receipt["run_id"]
    tree_fingerprint = receipt["tree_fingerprint"]

    rc = cli.main(
        [
            "receipts",
            "export",
            "attestation",
            "--target",
            str(tmp_path),
            "--run-id",
            run_id,
            "--profile",
            "cosign",
        ]
    )
    assert rc == 0

    bundle_path = Path(receipt["path"]) / "attestation.sigstore.json"
    assert bundle_path.is_file()

    verify_cmd = [
        cosign_bin,
        "verify-blob-attestation",
        "--bundle",
        str(bundle_path),
        "--key",
        str(public_key_path),
        "--insecure-ignore-tlog=true",
        f"--type={attestation.IN_TOTO_TEST_RESULT_PREDICATE_TYPE}",
        f"--digest={tree_fingerprint}",
        "--digestAlg=gitTree",
    ]
    res_verify = subprocess.run(
        verify_cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert res_verify.returncode == 0, f"cosign verify failed: {res_verify.stderr}\nstdout: {res_verify.stdout}"


def test_cosign_key_path_resolution_and_env_isolation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # 1. Unset environment variable defaults to .brigade/attestation/cosign.key
    monkeypatch.delenv(cosign_attestation.COSIGN_KEY_ENV, raising=False)
    default_expected = tmp_path.resolve() / ".brigade" / "attestation" / "cosign.key"
    assert cosign_attestation.default_cosign_key_path(tmp_path) == default_expected
    assert cosign_attestation.resolve_cosign_key_path(tmp_path) == default_expected

    # 2. BRIGADE_COSIGN_KEY_FILE affects resolve_cosign_key_path but leaves default_cosign_key_path untouched
    custom_key = tmp_path / "custom_keys" / "my_cosign.key"
    monkeypatch.setenv(cosign_attestation.COSIGN_KEY_ENV, str(custom_key))
    assert cosign_attestation.default_cosign_key_path(tmp_path) == default_expected
    assert cosign_attestation.resolve_cosign_key_path(tmp_path) == custom_key.resolve()

    # 3. Explicit key argument overrides BRIGADE_COSIGN_KEY_FILE
    explicit_key = tmp_path / "explicit" / "chosen.key"
    assert cosign_attestation.resolve_cosign_key_path(tmp_path, key_file=explicit_key) == explicit_key.resolve()

    # 4. CLI export honoring BRIGADE_COSIGN_KEY_FILE without --key
    custom_key.parent.mkdir(parents=True, exist_ok=True)
    custom_key.write_text("dummy-custom-key", encoding="utf-8")
    receipt = _sample_receipt(tmp_path)
    _write_receipt_to_disk(receipt)
    run_id = receipt["run_id"]

    received_key: Path | None = None
    mock_bundle = _make_valid_bundle(_sample_statement())

    def fake_export(_receipt_data: Mapping[str, Any], key_path: Path) -> dict[str, Any]:
        nonlocal received_key
        received_key = key_path
        return mock_bundle

    monkeypatch.setattr(cosign_attestation, "export_attestation", fake_export)

    rc = cli.main(
        [
            "receipts",
            "export",
            "attestation",
            "--target",
            str(tmp_path),
            "--run-id",
            run_id,
            "--profile",
            "cosign",
        ]
    )
    assert rc == 0
    assert received_key == custom_key.resolve()
