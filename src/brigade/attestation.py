"""In-toto Statement v1 and SSH-signed DSSE attestation export and verification."""

from __future__ import annotations

import base64
import getpass
import json
import os
import re
import shlex
import shutil
import struct
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import localio

IN_TOTO_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
IN_TOTO_TEST_RESULT_PREDICATE_TYPE = "https://in-toto.io/attestation/test-result/v0.1"
DSSE_PAYLOAD_TYPE = "application/vnd.in-toto+json"
ATTESTATION_PROFILE = "brigade.sshsig-dsse.v1"
ATTESTATION_NAMESPACE = "brigade-attestation"

KEY_ENV = "BRIGADE_ATTESTATION_KEY_FILE"
DEFAULT_KEY_NAME = "signing-key"
DEFAULT_ALLOWED_SIGNERS_NAME = "allowed_signers"
DEFAULT_REVOKED_KEYS_NAME = "revoked_keys"

STATUS_SIGNED_OK = "SIGNED-OK"
STATUS_SIGNATURE_MISMATCH = "SIGNATURE-MISMATCH"
STATUS_UNTRUSTED_KEY = "UNTRUSTED-KEY"
STATUS_UNVERIFIABLE_SIGNATURE = "UNVERIFIABLE-SIGNATURE"
STATUS_SUBJECT_MISMATCH = "SUBJECT-MISMATCH"

VERIFY_STATUSES = {
    STATUS_SIGNED_OK,
    STATUS_SIGNATURE_MISMATCH,
    STATUS_UNTRUSTED_KEY,
    STATUS_UNVERIFIABLE_SIGNATURE,
    STATUS_SUBJECT_MISMATCH,
}


class AttestationError(Exception):
    """Base exception for attestation operations."""


class AttestationExportError(AttestationError):
    """Raised when an attestation cannot be exported from a verify receipt."""


@dataclass
class AttestationVerifyResult:
    status: str
    principal: str | None = None
    keyid: str | None = None
    subject: list[dict[str, Any]] = field(default_factory=list)
    run_id: str | None = None

    def __getitem__(self, item: str) -> Any:
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)

    def get(self, item: str, default: Any = None) -> Any:
        return getattr(self, item, default)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": "brigade.attestation_verify_result.v1",
            "status": self.status,
            "principal": self.principal,
            "keyid": self.keyid,
            "subject": self.subject,
            "run_id": self.run_id,
        }


def default_key_path(target: Path) -> Path:
    configured = os.environ.get(KEY_ENV)
    if configured:
        return Path(configured).expanduser()
    return target.expanduser().resolve() / ".brigade" / "attestation" / DEFAULT_KEY_NAME


def default_allowed_signers_path(target: Path) -> Path:
    return target.expanduser().resolve() / ".brigade" / "attestation" / DEFAULT_ALLOWED_SIGNERS_NAME


def default_revoked_keys_path(target: Path) -> Path:
    return target.expanduser().resolve() / ".brigade" / "attestation" / DEFAULT_REVOKED_KEYS_NAME


def resolve_signing_key_path(target: Path | None = None, key_file: Path | None = None) -> Path:
    if key_file is not None:
        return key_file.expanduser().resolve()
    configured = os.environ.get(KEY_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    if target is not None:
        return default_key_path(target)
    return Path(".brigade") / "attestation" / DEFAULT_KEY_NAME


def _ensure_ssh_keygen() -> str:
    binary = shutil.which("ssh-keygen")
    if not binary:
        raise AttestationError("ssh-keygen is required on PATH for attestation operations")
    return binary


def get_key_fingerprint(key_or_pub_path: Path) -> str:
    """Return the SHA256:... fingerprint as printed by ssh-keygen -lf."""
    binary = _ensure_ssh_keygen()
    pub_path = Path(f"{key_or_pub_path}.pub")
    target = pub_path if pub_path.is_file() else key_or_pub_path
    res = subprocess.run([binary, "-lf", str(target)], capture_output=True, text=True)
    if res.returncode != 0:
        err = res.stderr.strip()
        raise AttestationError(f"ssh-keygen -lf failed for {target}: {err}")
    tokens = res.stdout.strip().split()
    if len(tokens) < 2:
        raise AttestationError(f"unexpected ssh-keygen -lf output: {res.stdout}")
    return tokens[1]


def get_public_key_fingerprint(pubkey_line: str) -> str:
    """Return the SHA256:... fingerprint for an OpenSSH public key line."""
    binary = _ensure_ssh_keygen()
    res = subprocess.run(
        [binary, "-lf", "-"],
        input=pubkey_line.strip().encode("utf-8"),
        capture_output=True,
    )
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", errors="replace").strip()
        raise AttestationError(f"ssh-keygen -lf - failed: {err}")
    out = res.stdout.decode("utf-8", errors="replace").strip()
    tokens = out.split()
    if len(tokens) < 2:
        raise AttestationError(f"unexpected ssh-keygen -lf output: {out}")
    return tokens[1]


def extract_keyid_from_sshsig(armored_sig: str) -> str | None:
    """Extract public key fingerprint from the wire format of an armored SSHSIG block."""
    lines = [line.strip() for line in armored_sig.splitlines() if line.strip() and not line.strip().startswith("-----")]
    b64_data = "".join(lines)
    try:
        raw = base64.b64decode(b64_data)
        if len(raw) >= 14 and raw[:6] == b"SSHSIG":
            _version, pk_len = struct.unpack(">II", raw[6:14])
            if len(raw) >= 14 + pk_len:
                pubkey = raw[14 : 14 + pk_len]
                kt_len = struct.unpack(">I", pubkey[:4])[0]
                key_type = pubkey[4 : 4 + kt_len].decode("ascii", errors="replace")
                pk_b64 = base64.b64encode(pubkey).decode("ascii")
                pubkey_line = f"{key_type} {pk_b64}"
                return get_public_key_fingerprint(pubkey_line)
    except Exception:
        pass
    return None


def _allowed_signers_key_data(line: str) -> str | None:
    """Return the key-data field of an OpenSSH allowed_signers line, or None if malformed."""
    tokens = line.strip().split()
    if len(tokens) < 2:
        return None
    idx = 1
    while idx < len(tokens) and "=" in tokens[idx]:
        idx += 1
    if idx + 1 < len(tokens):
        return tokens[idx + 1]
    return None


def keygen(
    target: Path,
    *,
    principal: str | None = None,
    force: bool = False,
    key_file: Path | None = None,
    allowed_signers_file: Path | None = None,
) -> tuple[Path, Path]:
    """Generate an Ed25519 signing key and append it to allowed_signers under principal."""
    binary = _ensure_ssh_keygen()
    key_path = key_file.expanduser().resolve() if key_file is not None else default_key_path(target)
    signers_path = (
        allowed_signers_file.expanduser().resolve()
        if allowed_signers_file is not None
        else default_allowed_signers_path(target)
    )

    resolved_principal = (principal or os.environ.get("USER") or getpass.getuser() or "unknown").strip()

    if key_path.exists() and not force:
        raise FileExistsError(f"attestation signing key already exists: {key_path}")

    key_path.parent.mkdir(parents=True, exist_ok=True)
    signers_path.parent.mkdir(parents=True, exist_ok=True)

    pub_path = Path(f"{key_path}.pub")
    old_key_data: str | None = None
    if force and key_path.exists():
        if not pub_path.is_file():
            raise AttestationError(f"refusing rotation: previous public key is missing: {pub_path}")
        old_pub_line = pub_path.read_text(encoding="utf-8").strip()
        old_parts = old_pub_line.split()
        if len(old_parts) >= 2:
            old_key_data = old_parts[1]
        key_path.unlink()
        pub_path.unlink()
    elif force:
        if key_path.exists():
            key_path.unlink()
        if pub_path.exists():
            pub_path.unlink()

    cmd = [
        binary,
        "-t",
        "ed25519",
        "-N",
        "",
        "-C",
        resolved_principal,
        "-f",
        str(key_path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        raise AttestationError(f"ssh-keygen failed: {res.stderr.strip()}")

    os.chmod(key_path, 0o600)

    if not pub_path.is_file():
        raise AttestationError(f"public key was not generated: {pub_path}")

    pub_line = pub_path.read_text(encoding="utf-8").strip()
    parts = pub_line.split()
    if len(parts) < 2:
        raise AttestationError(f"malformed public key file: {pub_path}")
    key_type, key_data = parts[0], parts[1]

    if force and old_key_data is not None and signers_path.is_file():
        existing_lines = signers_path.read_text(encoding="utf-8").splitlines(keepends=True)
        filtered: list[str] = []
        for line in existing_lines:
            if _allowed_signers_key_data(line) == old_key_data:
                continue
            filtered.append(line)
        signers_path.write_text("".join(filtered), encoding="utf-8")

    entry = f'{resolved_principal} namespaces="{ATTESTATION_NAMESPACE}" {key_type} {key_data}\n'
    with open(signers_path, "a", encoding="utf-8") as f:
        f.write(entry)

    return key_path, signers_path


def find_principals(
    sig_file: Path,
    allowed_signers_file: Path,
) -> list[str]:
    """Find matching principals in allowed_signers for a signature file."""
    binary = _ensure_ssh_keygen()
    if not sig_file.is_file() or not allowed_signers_file.is_file():
        return []
    res = subprocess.run(
        [binary, "-Y", "find-principals", "-s", str(sig_file), "-f", str(allowed_signers_file)],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return []
    lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
    return [line for line in lines if line != "No principal matched."]


def _compute_receipt_sha256(receipt: Mapping[str, Any]) -> str:
    return localio.canonical_json_digest(receipt, exclude_keys={"digests", "path"})


_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _test_name(cmd: Mapping[str, Any]) -> str:
    """Derive a safe test name from a command record, dropping env vars and paths."""
    check_id = cmd.get("check_id")
    if check_id:
        return str(check_id).strip()

    argv = cmd.get("argv")
    if isinstance(argv, list) and argv:
        tokens = [str(t) for t in argv]
    else:
        raw = str(cmd.get("command") or "")
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split()

    cleaned: list[str] = []
    for token in tokens:
        if _ENV_ASSIGNMENT_RE.match(token):
            continue
        cleaned.append(token)

    if not cleaned:
        return "command"

    cleaned[0] = Path(cleaned[0]).name
    for i in range(1, len(cleaned)):
        if os.path.sep in cleaned[i] or "/" in cleaned[i]:
            cleaned[i] = Path(cleaned[i]).name

    return " ".join(cleaned)


def build_statement(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Build an in-toto Statement v1 with Test Result v0.1 predicate from a verify receipt."""
    tree_fingerprint = receipt.get("tree_fingerprint")
    changes_patch_sha256 = receipt.get("changes_patch_sha256")

    if tree_fingerprint is None or changes_patch_sha256 is None:
        raise AttestationExportError(
            "cannot export attestation: tree_fingerprint and changes_patch_sha256 are required"
        )

    run_id = str(receipt.get("run_id", "")).strip()

    digests = receipt.get("digests")
    receipt_sha256 = (
        digests.get("receipt_sha256")
        if isinstance(digests, Mapping) and digests.get("receipt_sha256")
        else _compute_receipt_sha256(receipt)
    )

    passed_tests: list[str] = []
    failed_tests: list[str] = []

    commands = receipt.get("commands", [])
    if isinstance(commands, list):
        for cmd in commands:
            if not isinstance(cmd, Mapping):
                continue
            test_name = _test_name(cmd)
            exit_code = cmd.get("exit_code")
            if exit_code == 0:
                passed_tests.append(test_name)
            else:
                failed_tests.append(test_name)

    status = receipt.get("status")
    result_passed = status == "completed" and len(failed_tests) == 0
    predicate_result = "PASSED" if result_passed else "FAILED"

    brigade_annotations: dict[str, Any] = {}
    if receipt.get("run_id") is not None:
        brigade_annotations["run_id"] = receipt["run_id"]
    if receipt.get("baseline_commit") is not None:
        brigade_annotations["baseline_commit"] = receipt["baseline_commit"]
    if receipt.get("producer_run_id") is not None:
        brigade_annotations["producer_run_id"] = receipt["producer_run_id"]

    config_descriptor: dict[str, Any] = {
        "name": "receipt.json",
        "uri": f"urn:brigade:verify:{run_id}:receipt",
        "digest": {"sha256": receipt_sha256},
        "mediaType": "application/json",
    }
    if brigade_annotations:
        config_descriptor["annotations"] = {"brigade": brigade_annotations}

    return {
        "_type": IN_TOTO_STATEMENT_TYPE,
        "subject": [
            {"name": "git:tree", "digest": {"gitTree": tree_fingerprint}},
            {"name": "changes.patch", "digest": {"sha256": changes_patch_sha256}},
        ],
        "predicateType": IN_TOTO_TEST_RESULT_PREDICATE_TYPE,
        "predicate": {
            "result": predicate_result,
            "configuration": [config_descriptor],
            "url": f"urn:brigade:verify:{run_id}",
            "passedTests": passed_tests,
            "warnedTests": [],
            "failedTests": failed_tests,
        },
    }


def canonical_statement_bytes(statement: Mapping[str, Any]) -> bytes:
    """Serialize statement to canonical UTF-8 bytes with sorted keys and compact separators."""
    return json.dumps(statement, sort_keys=True, separators=(",", ":")).encode("utf-8")


def dsse_pae(payload_type: str, payload: bytes) -> bytes:
    """Compute Pre-Authentication Encoding (PAE) for DSSE v1."""
    type_bytes = payload_type.encode("utf-8")
    return (
        b"DSSEv1 "
        + str(len(type_bytes)).encode("ascii")
        + b" "
        + type_bytes
        + b" "
        + str(len(payload)).encode("ascii")
        + b" "
        + payload
    )


def sign_pae(
    pae: bytes,
    key_path: Path,
    *,
    namespace: str = ATTESTATION_NAMESPACE,
) -> str:
    """Sign PAE bytes using ssh-keygen -Y sign and return the armored SSHSIG string."""
    binary = _ensure_ssh_keygen()
    if not key_path.is_file():
        raise FileNotFoundError(f"signing key not found: {key_path}")

    try:
        res = subprocess.run(
            [binary, "-Y", "sign", "-f", str(key_path), "-n", namespace],
            input=pae,
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise AttestationError("ssh-keygen -Y sign timed out after 30 seconds") from exc
    if res.returncode != 0:
        err = res.stderr.decode("utf-8", errors="replace").strip()
        raise AttestationError(f"ssh-keygen -Y sign failed (code {res.returncode}): {err}")
    return res.stdout.decode("utf-8", errors="replace").strip()


def create_envelope(
    statement: Mapping[str, Any],
    key_path: Path,
    *,
    namespace: str = ATTESTATION_NAMESPACE,
) -> dict[str, Any]:
    """Create a signed DSSE v1 envelope over an in-toto statement."""
    statement_bytes = canonical_statement_bytes(statement)
    pae = dsse_pae(DSSE_PAYLOAD_TYPE, statement_bytes)
    armored_sig = sign_pae(pae, key_path, namespace=namespace)
    keyid = get_key_fingerprint(key_path)

    sig_b64 = base64.b64encode(armored_sig.encode("utf-8")).decode("ascii")
    payload_b64 = base64.b64encode(statement_bytes).decode("ascii")

    return {
        "payloadType": DSSE_PAYLOAD_TYPE,
        "payload": payload_b64,
        "signatures": [
            {
                "keyid": keyid,
                "sig": sig_b64,
            }
        ],
        "brigade": {
            "profile": ATTESTATION_PROFILE,
            "namespace": namespace,
        },
    }


def export_attestation(
    receipt: Mapping[str, Any],
    key_path: Path,
    *,
    namespace: str = ATTESTATION_NAMESPACE,
) -> dict[str, Any]:
    """Build statement and return signed DSSE envelope for a verify receipt."""
    statement = build_statement(receipt)
    return create_envelope(statement, key_path, namespace=namespace)


def write_attestation_file(
    envelope: Mapping[str, Any],
    out_path: Path,
    *,
    force: bool = False,
) -> Path:
    """Write attestation envelope JSON to out_path, refusing overwrite unless force=True."""
    target = out_path.expanduser().resolve()
    if target.exists() and not force:
        raise FileExistsError(f"attestation file already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    if force:
        localio.write_json(target, dict(envelope))
    else:
        localio.write_json_exclusive(target, dict(envelope))
    return target


def _decode_sig_armored(sig_value: str) -> str | None:
    cleaned = sig_value.strip()
    if "-----BEGIN SSH SIGNATURE-----" in cleaned:
        return cleaned
    try:
        decoded = base64.b64decode(cleaned).decode("utf-8", errors="replace")
        if "-----BEGIN SSH SIGNATURE-----" in decoded:
            return decoded
    except Exception:
        pass
    return None


def verify_attestation(
    envelope_data: Mapping[str, Any] | str | bytes | Path,
    *,
    allowed_signers_path: Path | None = None,
    principal: str | None = None,
    target: Path | None = None,
    krl_path: Path | None = None,
    namespace: str = ATTESTATION_NAMESPACE,
    expected_predicate_type: str = IN_TOTO_TEST_RESULT_PREDICATE_TYPE,
) -> AttestationVerifyResult:
    """Verify an attestation envelope against OpenSSH allowed_signers policy."""
    binary = shutil.which("ssh-keygen")
    if not binary:
        return AttestationVerifyResult(status=STATUS_UNVERIFIABLE_SIGNATURE)

    # 1. Parse envelope
    if isinstance(envelope_data, Path):
        if not envelope_data.is_file():
            return AttestationVerifyResult(status=STATUS_UNVERIFIABLE_SIGNATURE)
        try:
            envelope = json.loads(envelope_data.read_text(encoding="utf-8"))
        except Exception:
            return AttestationVerifyResult(status=STATUS_UNVERIFIABLE_SIGNATURE)
    elif isinstance(envelope_data, (str, bytes)):
        try:
            envelope = json.loads(envelope_data)
        except Exception:
            return AttestationVerifyResult(status=STATUS_UNVERIFIABLE_SIGNATURE)
    elif isinstance(envelope_data, Mapping):
        envelope = envelope_data
    else:
        return AttestationVerifyResult(status=STATUS_UNVERIFIABLE_SIGNATURE)

    if not isinstance(envelope, Mapping):
        return AttestationVerifyResult(status=STATUS_UNVERIFIABLE_SIGNATURE)

    payload_type = envelope.get("payloadType")
    payload_b64 = envelope.get("payload")
    signatures = envelope.get("signatures")
    brigade_meta = envelope.get("brigade")

    if (
        not isinstance(payload_type, str)
        or not isinstance(payload_b64, str)
        or not isinstance(signatures, list)
        or len(signatures) != 1
        or not isinstance(brigade_meta, Mapping)
    ):
        return AttestationVerifyResult(status=STATUS_UNVERIFIABLE_SIGNATURE)

    # 2. Decode payload & parse Statement
    try:
        payload_bytes = base64.b64decode(payload_b64)
        statement = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return AttestationVerifyResult(status=STATUS_UNVERIFIABLE_SIGNATURE)

    if not isinstance(statement, Mapping):
        return AttestationVerifyResult(status=STATUS_UNVERIFIABLE_SIGNATURE)

    if (
        payload_type != DSSE_PAYLOAD_TYPE
        or statement.get("_type") != IN_TOTO_STATEMENT_TYPE
        or statement.get("predicateType") != expected_predicate_type
        or brigade_meta.get("profile") != ATTESTATION_PROFILE
        or brigade_meta.get("namespace") != namespace
    ):
        return AttestationVerifyResult(status=STATUS_UNVERIFIABLE_SIGNATURE)

    subject_raw = statement.get("subject")
    subject: list[dict[str, Any]] = (
        [dict(s) for s in subject_raw if isinstance(s, Mapping)] if isinstance(subject_raw, list) else []
    )

    run_id: str | None = None
    pred = statement.get("predicate")
    if isinstance(pred, Mapping):
        url = pred.get("url")
        if isinstance(url, str) and url.startswith("urn:brigade:verify:"):
            candidate = url.removeprefix("urn:brigade:verify:")
            if re.fullmatch(r"[A-Za-z0-9._-]+", candidate) and candidate not in (".", ".."):
                run_id = candidate
        run_ref = pred.get("run")
        if run_id is None and isinstance(run_ref, Mapping):
            run_id_candidate = run_ref.get("id")
            if isinstance(run_id_candidate, str) and re.fullmatch(r"[A-Za-z0-9._-]+", run_id_candidate):
                run_id = run_id_candidate

    # 3. Recompute DSSE PAE
    pae_bytes = dsse_pae(payload_type, payload_bytes)

    # 4. Extract signature
    sig_entry = signatures[0]
    if not isinstance(sig_entry, Mapping):
        return AttestationVerifyResult(status=STATUS_UNVERIFIABLE_SIGNATURE, subject=subject, run_id=run_id)

    keyid_hint = sig_entry.get("keyid")
    raw_sig_str = sig_entry.get("sig")
    if not isinstance(raw_sig_str, str):
        return AttestationVerifyResult(status=STATUS_UNVERIFIABLE_SIGNATURE, subject=subject, run_id=run_id)

    armored_sig = _decode_sig_armored(raw_sig_str)
    if not armored_sig:
        return AttestationVerifyResult(status=STATUS_UNVERIFIABLE_SIGNATURE, subject=subject, run_id=run_id)

    if krl_path is not None and not krl_path.expanduser().resolve().is_file():
        return AttestationVerifyResult(status=STATUS_UNVERIFIABLE_SIGNATURE, subject=subject, run_id=run_id)

    # 5. Check signature validity with ssh-keygen -Y check-novalidate
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        sig_file = tmp_path / "signature.sig"
        sig_file.write_text(armored_sig, encoding="utf-8")

        res_check = subprocess.run(
            [binary, "-Y", "check-novalidate", "-n", namespace, "-s", str(sig_file)],
            input=pae_bytes,
            capture_output=True,
        )

        if res_check.returncode != 0:
            stderr_text = res_check.stderr.decode("utf-8", errors="replace")
            stdout_text = res_check.stdout.decode("utf-8", errors="replace")
            combined_err = f"{stdout_text}\n{stderr_text}".lower()

            if (
                "couldn't parse" in combined_err
                or "invalid format" in combined_err
                or "incomplete" in combined_err
                or "missing header" in combined_err
                or "dearmor" in combined_err
            ):
                return AttestationVerifyResult(status=STATUS_UNVERIFIABLE_SIGNATURE, subject=subject, run_id=run_id)
            return AttestationVerifyResult(status=STATUS_SIGNATURE_MISMATCH, subject=subject, run_id=run_id)

        # Signature is cryptographically valid over PAE bytes.
        # Extract actual signing key fingerprint.
        actual_keyid: str | None = None
        combined_text = (
            res_check.stdout.decode("utf-8", errors="replace")
            + "\n"
            + res_check.stderr.decode("utf-8", errors="replace")
        )
        match = re.search(r"SHA256:[A-Za-z0-9+/=]+", combined_text)
        if match:
            actual_keyid = match.group(0)
        else:
            actual_keyid = extract_keyid_from_sshsig(armored_sig)

        if isinstance(keyid_hint, str) and actual_keyid and keyid_hint != actual_keyid:
            return AttestationVerifyResult(
                status=STATUS_SIGNATURE_MISMATCH,
                keyid=actual_keyid,
                subject=subject,
                run_id=run_id,
            )

        # 6. Verify against allowed_signers policy
        signers_file = (
            allowed_signers_path.expanduser().resolve()
            if allowed_signers_path is not None
            else (default_allowed_signers_path(target) if target is not None else None)
        )

        if signers_file is None or not signers_file.is_file():
            return AttestationVerifyResult(
                status=STATUS_UNTRUSTED_KEY,
                keyid=actual_keyid,
                subject=subject,
                run_id=run_id,
            )

        effective_krl = (
            krl_path.expanduser().resolve()
            if krl_path is not None
            else (default_revoked_keys_path(target) if target is not None else None)
        )

        verified_principal: str | None = None
        if principal:
            verify_cmd = [
                binary,
                "-Y",
                "verify",
                "-f",
                str(signers_file),
                "-I",
                principal,
                "-n",
                namespace,
                "-s",
                str(sig_file),
            ]
            if effective_krl and effective_krl.is_file():
                verify_cmd.extend(["-r", str(effective_krl)])

            res_v = subprocess.run(verify_cmd, input=pae_bytes, capture_output=True)
            if res_v.returncode != 0:
                return AttestationVerifyResult(
                    status=STATUS_UNTRUSTED_KEY,
                    principal=principal,
                    keyid=actual_keyid,
                    subject=subject,
                    run_id=run_id,
                )
            verified_principal = principal
        else:
            principals = find_principals(sig_file, signers_file)
            if not principals:
                return AttestationVerifyResult(
                    status=STATUS_UNTRUSTED_KEY,
                    keyid=actual_keyid,
                    subject=subject,
                    run_id=run_id,
                )

            verified = False
            for p in principals:
                verify_cmd = [
                    binary,
                    "-Y",
                    "verify",
                    "-f",
                    str(signers_file),
                    "-I",
                    p,
                    "-n",
                    namespace,
                    "-s",
                    str(sig_file),
                ]
                if effective_krl and effective_krl.is_file():
                    verify_cmd.extend(["-r", str(effective_krl)])

                res_v = subprocess.run(verify_cmd, input=pae_bytes, capture_output=True)
                if res_v.returncode == 0:
                    verified_principal = p
                    verified = True
                    break

            if not verified:
                return AttestationVerifyResult(
                    status=STATUS_UNTRUSTED_KEY,
                    keyid=actual_keyid,
                    subject=subject,
                    run_id=run_id,
                )

        # 7. Check target receipt re-derivation (step 4)
        if target is not None and run_id and expected_predicate_type == IN_TOTO_TEST_RESULT_PREDICATE_TYPE:
            resolved_target = target.expanduser().resolve()
            run_dir = resolved_target / ".brigade" / "work" / "verify-runs" / run_id
            receipt_file = run_dir / "receipt.json"
            if receipt_file.is_file():
                try:
                    receipt_data = json.loads(receipt_file.read_text(encoding="utf-8"))
                    rederived_statement = build_statement(receipt_data)
                    rederived_bytes = canonical_statement_bytes(rederived_statement)
                    if rederived_bytes != payload_bytes:
                        return AttestationVerifyResult(
                            status=STATUS_SUBJECT_MISMATCH,
                            principal=verified_principal,
                            keyid=actual_keyid,
                            subject=subject,
                            run_id=run_id,
                        )
                except Exception:
                    return AttestationVerifyResult(
                        status=STATUS_SUBJECT_MISMATCH,
                        principal=verified_principal,
                        keyid=actual_keyid,
                        subject=subject,
                        run_id=run_id,
                    )

        return AttestationVerifyResult(
            status=STATUS_SIGNED_OK,
            principal=verified_principal,
            keyid=actual_keyid,
            subject=subject,
            run_id=run_id,
        )
