"""Cosign attest-blob signer profile and Sigstore bundle verification adapter."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import attestation, attestation_input

COSIGN_KEY_ENV = "BRIGADE_COSIGN_KEY_FILE"
DEFAULT_COSIGN_KEY_NAME = "cosign.key"
SIGSTORE_BUNDLE_MEDIA_TYPE = "application/vnd.dev.sigstore.bundle.v0.3+json"
OFFLINE_SIGNING_CONFIG_MEDIA_TYPE = "application/vnd.dev.sigstore.signingconfig.v0.2+json"
MIN_SAFE_V2 = (2, 6, 5)
MIN_SAFE_V3 = (3, 1, 3)


class CosignAttestationError(attestation.AttestationError):
    pass


def default_cosign_key_path(target: Path) -> Path:
    return target.expanduser().resolve() / ".brigade" / "attestation" / DEFAULT_COSIGN_KEY_NAME


def resolve_cosign_key_path(target: Path, key_file: Path | None = None) -> Path:
    if key_file is not None:
        return key_file.expanduser().resolve()
    configured = os.environ.get(COSIGN_KEY_ENV)
    if configured:
        return Path(configured).expanduser().resolve()
    return default_cosign_key_path(target)


def parse_cosign_version(output: str) -> tuple[int, int, int]:
    output_str = output.strip()
    raw_ver: str | None = None
    try:
        data = json.loads(output_str)
        if isinstance(data, Mapping):
            val = data.get("gitVersion") or data.get("GitVersion")
            if val is not None:
                raw_ver = str(val).strip()
    except Exception:
        pass

    if raw_ver is None:
        m = re.search(r"""["']?(?:GitVersion|gitVersion)["']?\s*[:=]?\s*["']?\s*([^"'\s,;]+)\s*["']?""", output_str)
        if m:
            raw_ver = m.group(1).strip().strip("\"'")

    if raw_ver is None:
        raise CosignAttestationError(
            f"unable to determine cosign version from output; "
            f"safe releases >= {MIN_SAFE_V2[0]}.{MIN_SAFE_V2[1]}.{MIN_SAFE_V2[2]} "
            f"or >= {MIN_SAFE_V3[0]}.{MIN_SAFE_V3[1]}.{MIN_SAFE_V3[2]} required"
        )

    v_str = raw_ver[1:] if raw_ver.startswith(("v", "V")) else raw_ver
    m_semver = re.match(r"^(\d+)\.(\d+)\.(\d+)(.*)$", v_str)
    if not m_semver:
        raise CosignAttestationError(
            f"unrecognized cosign version format '{raw_ver}'; "
            f"safe releases >= {MIN_SAFE_V2[0]}.{MIN_SAFE_V2[1]}.{MIN_SAFE_V2[2]} "
            f"or >= {MIN_SAFE_V3[0]}.{MIN_SAFE_V3[1]}.{MIN_SAFE_V3[2]} required"
        )

    major_s, minor_s, patch_s, suffix = m_semver.groups()
    if suffix:
        raise CosignAttestationError(
            f"unsupported cosign prerelease or distro-suffixed version '{raw_ver}'; "
            f"safe releases >= {MIN_SAFE_V2[0]}.{MIN_SAFE_V2[1]}.{MIN_SAFE_V2[2]} "
            f"or >= {MIN_SAFE_V3[0]}.{MIN_SAFE_V3[1]}.{MIN_SAFE_V3[2]} required"
        )

    return (int(major_s), int(minor_s), int(patch_s))


def require_safe_cosign(binary: str | None = None) -> tuple[str, tuple[int, int, int]]:
    resolved_binary = binary or shutil.which("cosign")
    if not resolved_binary:
        raise CosignAttestationError("cosign is required on PATH for cosign attestation operations")

    try:
        proc = subprocess.run(
            [resolved_binary, "version", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise CosignAttestationError("cosign version check timed out after 30 seconds") from exc
    except OSError as exc:
        raise CosignAttestationError(f"failed to execute cosign binary '{resolved_binary}': {exc}") from exc

    if proc.returncode != 0:
        err_excerpt = proc.stderr.strip()[:2000]
        if err_excerpt:
            raise CosignAttestationError(f"cosign version failed with exit code {proc.returncode}: {err_excerpt}")
        raise CosignAttestationError(f"cosign version failed with exit code {proc.returncode}")

    raw_output = proc.stdout if proc.stdout.strip() else proc.stderr
    version = parse_cosign_version(raw_output)
    if version[0] == 2 and version >= MIN_SAFE_V2:
        return resolved_binary, version
    if version[0] == 3 and version >= MIN_SAFE_V3:
        return resolved_binary, version

    raise CosignAttestationError(
        f"unsupported cosign version {version[0]}.{version[1]}.{version[2]}; "
        f"safe releases >= {MIN_SAFE_V2[0]}.{MIN_SAFE_V2[1]}.{MIN_SAFE_V2[2]} "
        f"or >= {MIN_SAFE_V3[0]}.{MIN_SAFE_V3[1]}.{MIN_SAFE_V3[2]} required"
    )


def validate_bundle(bundle: Mapping[str, Any], statement: Mapping[str, Any]) -> dict[str, Any]:
    try:
        bundle = attestation_input.validate_json_value(bundle)
        statement = attestation_input.validate_json_value(statement)
    except attestation_input.AttestationInputError as exc:
        raise CosignAttestationError("invalid cosign bundle input") from exc
    if not isinstance(bundle, Mapping):
        raise CosignAttestationError("cosign bundle must be a JSON object")

    media_type = bundle.get("mediaType")
    if media_type != SIGSTORE_BUNDLE_MEDIA_TYPE:
        raise CosignAttestationError(f"invalid bundle mediaType: expected {SIGSTORE_BUNDLE_MEDIA_TYPE}")

    verification_material = bundle.get("verificationMaterial")
    if not isinstance(verification_material, Mapping):
        raise CosignAttestationError("cosign bundle missing or invalid verificationMaterial object")

    public_key = verification_material.get("publicKey")
    if not isinstance(public_key, Mapping):
        raise CosignAttestationError("cosign bundle missing or invalid verificationMaterial.publicKey object")

    hint = public_key.get("hint")
    if not isinstance(hint, str) or not hint.strip():
        raise CosignAttestationError("cosign bundle verificationMaterial.publicKey.hint must be a nonempty string")

    if verification_material.get("tlogEntries") or bundle.get("tlogEntries"):
        raise CosignAttestationError("cosign bundle contains unexpected tlogEntries; profile excludes Rekor")

    if verification_material.get("timestampVerificationData") or bundle.get("timestampVerificationData"):
        raise CosignAttestationError(
            "cosign bundle contains unexpected timestampVerificationData; profile excludes TSA"
        )

    dsse_envelope = bundle.get("dsseEnvelope")
    if not isinstance(dsse_envelope, Mapping):
        raise CosignAttestationError("cosign bundle missing or invalid dsseEnvelope object")

    payload_type = dsse_envelope.get("payloadType")
    if payload_type != attestation.DSSE_PAYLOAD_TYPE:
        raise CosignAttestationError(f"invalid dsseEnvelope payloadType: expected {attestation.DSSE_PAYLOAD_TYPE}")

    signatures = dsse_envelope.get("signatures")
    if not isinstance(signatures, list):
        raise CosignAttestationError("dsseEnvelope signatures must be a list")
    if len(signatures) != 1:
        raise CosignAttestationError(f"dsseEnvelope must have exactly 1 signature, found {len(signatures)}")
    sig_entry = signatures[0]
    if not isinstance(sig_entry, Mapping):
        raise CosignAttestationError("dsseEnvelope signature entry must be an object")

    sig = sig_entry.get("sig")
    if not isinstance(sig, str) or not sig.strip():
        raise CosignAttestationError("dsseEnvelope signature sig must be a nonempty base64-encoded string")

    try:
        sig_bytes = attestation_input.decode_dsse_base64(
            sig,
            label="dsseEnvelope signature sig",
            max_bytes=attestation_input.MAX_SIGNATURE_BYTES,
        )
    except attestation_input.AttestationInputError as exc:
        raise CosignAttestationError(f"invalid base64 in dsseEnvelope signature sig: {exc}") from exc

    if not sig_bytes:
        raise CosignAttestationError("dsseEnvelope signature sig must not decode to empty bytes")

    raw_payload = dsse_envelope.get("payload")
    if not isinstance(raw_payload, str):
        raise CosignAttestationError("dsseEnvelope payload must be a base64-encoded string")

    try:
        payload_bytes = attestation_input.decode_dsse_base64(
            raw_payload,
            label="dsseEnvelope payload",
            max_bytes=attestation_input.MAX_PAYLOAD_BYTES,
        )
    except attestation_input.AttestationInputError as exc:
        raise CosignAttestationError(f"invalid base64 in dsseEnvelope payload: {exc}") from exc

    try:
        decoded_statement = attestation_input.strict_json_loads(
            payload_bytes,
            max_bytes=attestation_input.MAX_PAYLOAD_BYTES,
        )
    except attestation_input.AttestationInputError as exc:
        raise CosignAttestationError("dsseEnvelope payload is not valid JSON") from exc

    if not isinstance(decoded_statement, Mapping):
        raise CosignAttestationError("dsseEnvelope payload JSON must be an object")

    if decoded_statement != statement:
        raise CosignAttestationError("dsseEnvelope payload does not match expected statement")

    return dict(bundle)


def create_bundle(statement: Mapping[str, Any], key_path: Path) -> dict[str, Any]:
    binary, version = require_safe_cosign()

    with tempfile.TemporaryDirectory() as tmp_dir_str:
        tmp_dir = Path(tmp_dir_str)
        statement_path = tmp_dir / "statement.json"
        bundle_path = tmp_dir / "bundle.sigstore.json"

        statement_path.write_bytes(attestation.canonical_statement_bytes(statement))

        cmd = [
            binary,
            "attest-blob",
        ]
        if version[0] == 2:
            cmd.extend(["--new-bundle-format=true", "--tlog-upload=false"])
        elif version[0] == 3:
            signing_config_path = tmp_dir / "signing-config.json"
            signing_config = {
                "mediaType": OFFLINE_SIGNING_CONFIG_MEDIA_TYPE,
                "rekorTlogConfig": {},
                "tsaConfig": {},
            }
            signing_config_path.write_text(json.dumps(signing_config), encoding="utf-8")
            cmd.extend(["--signing-config", str(signing_config_path)])
        cmd.extend(
            [
                "--yes",
                "--key",
                str(key_path),
                "--statement",
                str(statement_path),
                "--bundle",
                str(bundle_path),
            ]
        )

        try:
            proc = subprocess.run(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=180,
            )
        except subprocess.TimeoutExpired as exc:
            raise CosignAttestationError("cosign attest-blob timed out after 180 seconds") from exc
        except OSError as exc:
            raise CosignAttestationError(f"failed to execute cosign attest-blob: {exc}") from exc

        if proc.returncode != 0:
            err_excerpt = proc.stderr.strip()[:2000]
            password_hint = " (if private key is password-encrypted, ensure COSIGN_PASSWORD is set)"
            if err_excerpt:
                raise CosignAttestationError(
                    f"cosign attest-blob failed with exit code {proc.returncode}: {err_excerpt}{password_hint}"
                )
            raise CosignAttestationError(f"cosign attest-blob failed with exit code {proc.returncode}{password_hint}")

        if not bundle_path.is_file():
            raise CosignAttestationError("cosign exited 0 but output bundle was not created")

        try:
            bundle_data = attestation_input.read_json_object(bundle_path)
        except (OSError, attestation_input.AttestationInputError) as exc:
            raise CosignAttestationError(f"failed to read or parse cosign bundle JSON: {exc}") from exc

        return validate_bundle(bundle_data, statement)


def export_attestation(receipt: Mapping[str, Any], key_path: Path) -> dict[str, Any]:
    statement = attestation.build_statement(receipt)
    return create_bundle(statement, key_path)
