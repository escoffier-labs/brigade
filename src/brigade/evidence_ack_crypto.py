"""Bounded, one-key SSHSIG observations for acknowledgement envelopes."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeGuard, cast

from . import attestation, evidence_ack_protocol, proc

MAX_PUBLIC_KEY_BYTES = 256
_KEY_LINE_BYTES = 80
_KEY_PREFIX = b"ssh-ed25519 "
_KEY_BLOB_BYTES = 51
_SYNTHETIC_PRINCIPAL = "brigade-ack-observer"
_NAMESPACE = "brigade-evidence-ack"
_OBSERVATION_ERROR = "invalid acknowledgement crypto observation"


class AckCryptoInputError(ValueError):
    """A closed input refusal for the acknowledgement crypto adapter."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or len(code) > 4 or code not in {"type", "size", "key"}:
            raise ValueError("invalid acknowledgement crypto input error code")
        self._code = code
        super().__init__(f"ack crypto input: {code}")

    @property
    def code(self) -> str:
        return self._code


@dataclass(frozen=True, slots=True)
class AckCryptoObservation:
    """One bounded observation, never a destination-authorization decision."""

    signature: Literal["VALID", "NOT-VERIFIED", "UNAVAILABLE"]
    keyid: Literal["MATCH", "MISMATCH", "UNRESOLVED"]
    verified_fingerprint: str | None
    asserted_request_id: str
    asserted_result: Literal["received", "rejected"]
    envelope_id: str

    def __post_init__(self) -> None:
        values = (
            self.signature,
            self.keyid,
            self.asserted_request_id,
            self.asserted_result,
            self.envelope_id,
        )
        if any(type(value) is not str for value in values) or (
            self.verified_fingerprint is not None and type(self.verified_fingerprint) is not str
        ):
            raise ValueError(_OBSERVATION_ERROR)
        if (
            len(self.signature) > 12
            or len(self.keyid) > 10
            or len(self.asserted_result) > 8
            or len(self.asserted_request_id) > 64
            or len(self.envelope_id) > 64
            or (self.verified_fingerprint is not None and len(self.verified_fingerprint) > 50)
        ):
            raise ValueError(_OBSERVATION_ERROR)
        if self.signature not in {"VALID", "NOT-VERIFIED", "UNAVAILABLE"}:
            raise ValueError(_OBSERVATION_ERROR)
        if self.keyid not in {"MATCH", "MISMATCH", "UNRESOLVED"}:
            raise ValueError(_OBSERVATION_ERROR)
        if self.asserted_result not in {"received", "rejected"}:
            raise ValueError(_OBSERVATION_ERROR)
        if not _digest(self.asserted_request_id) or not _digest(self.envelope_id):
            raise ValueError(_OBSERVATION_ERROR)
        if self.signature == "VALID":
            if self.keyid not in {"MATCH", "MISMATCH"} or not _fingerprint(self.verified_fingerprint):
                raise ValueError(_OBSERVATION_ERROR)
        elif self.keyid != "UNRESOLVED" or self.verified_fingerprint is not None:
            raise ValueError(_OBSERVATION_ERROR)


def _digest(value: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _fingerprint(value: str | None) -> bool:
    return type(value) is str and bool(re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", value))


def _input_error(code: str) -> None:
    raise AckCryptoInputError(code) from None


def _key_blob(raw_key: bytes) -> bytes:
    if len(raw_key) != _KEY_LINE_BYTES or not raw_key.startswith(_KEY_PREFIX):
        _input_error("key")
    encoded = raw_key[len(_KEY_PREFIX) :]
    if len(encoded) != 68:
        _input_error("key")
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        _input_error("key")
    if base64.b64encode(blob) != encoded or len(blob) != _KEY_BLOB_BYTES:
        _input_error("key")
    if blob[:4] != b"\x00\x00\x00\x0b" or blob[4:15] != b"ssh-ed25519":
        _input_error("key")
    if blob[15:19] != b"\x00\x00\x00\x20" or len(blob[19:]) != 32:
        _input_error("key")
    return blob


def _fingerprint_for(blob: bytes) -> str:
    encoded = base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    return f"SHA256:{encoded}"


def _write_private(path: Path, contents: bytes) -> None:
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
        0o600,
    )
    try:
        written = 0
        while written < len(contents):
            count = os.write(descriptor, contents[written:])
            if count <= 0:
                raise OSError("short write")
            written += count
    finally:
        os.close(descriptor)


def _run_verify(
    executable: str,
    signers: Path,
    signature: Path,
    directory: Path,
    pae: bytes,
    remaining: float,
) -> proc.Result:
    return proc.run(
        [
            executable,
            "-Y",
            "verify",
            "-f",
            str(signers),
            "-I",
            _SYNTHETIC_PRINCIPAL,
            "-n",
            _NAMESPACE,
            "-s",
            str(signature),
        ],
        timeout=remaining,
        cwd=directory,
        stdin=pae,
        supervise_group=True,
    )


def _usable_result(result: object) -> TypeGuard[proc.Result]:
    if type(result) is not proc.Result:
        return False
    if type(result.code) is not int:
        return False
    flags = (
        result.output_limit_exceeded,
        result.stream_limit_exceeded,
        result.incomplete_process_group,
        result.descendants_reaped,
    )
    if any(type(flag) is not bool for flag in flags):
        return False
    return all(
        value is None or type(value) is str for value in (result.stdout_decode_error, result.stderr_decode_error)
    )


def _unavailable(parsed: evidence_ack_protocol.AckEnvelope) -> AckCryptoObservation:
    return AckCryptoObservation(
        "UNAVAILABLE",
        "UNRESOLVED",
        None,
        parsed.claim.request_id,
        cast(Literal["received", "rejected"], parsed.claim.result),
        parsed.envelope_id,
    )


def _classified(
    result: object,
    parsed: evidence_ack_protocol.AckEnvelope,
    fingerprint: str,
) -> AckCryptoObservation:
    if not _usable_result(result):
        return _unavailable(parsed)
    if (
        result.stdout_decode_error is not None
        or result.stderr_decode_error is not None
        or result.output_limit_exceeded
        or result.stream_limit_exceeded
        or result.incomplete_process_group
        or not result.descendants_reaped
        or result.code < 0
        or result.code in {124, 125, 126, 127}
    ):
        return _unavailable(parsed)
    if result.code == 0:
        return AckCryptoObservation(
            "VALID",
            "MATCH" if parsed.keyid_hint == fingerprint else "MISMATCH",
            fingerprint,
            parsed.claim.request_id,
            cast(Literal["received", "rejected"], parsed.claim.result),
            parsed.envelope_id,
        )
    return AckCryptoObservation(
        "NOT-VERIFIED",
        "UNRESOLVED",
        None,
        parsed.claim.request_id,
        cast(Literal["received", "rejected"], parsed.claim.result),
        parsed.envelope_id,
    )


def observe_ack_sshsig(raw_envelope: bytes, *, signer_public_key: bytes) -> AckCryptoObservation:
    """Observe a canonical acknowledgement SSHSIG under one captured public key."""
    deadline = time.monotonic() + 5.0
    if type(raw_envelope) is not bytes or type(signer_public_key) is not bytes:
        _input_error("type")
    if len(raw_envelope) > evidence_ack_protocol.MAX_ENVELOPE_BYTES or len(signer_public_key) > MAX_PUBLIC_KEY_BYTES:
        _input_error("size")
    blob = _key_blob(signer_public_key)
    parsed = evidence_ack_protocol.parse_envelope(raw_envelope)
    if time.monotonic() >= deadline:
        return _unavailable(parsed)
    fingerprint = _fingerprint_for(blob)
    try:
        executable = shutil.which("ssh-keygen")
    except OSError:
        return _unavailable(parsed)
    if executable is None or time.monotonic() >= deadline:
        return _unavailable(parsed)
    provisional: AckCryptoObservation = _unavailable(parsed)
    cleanup_failed = False
    try:
        temporary_directory = tempfile.TemporaryDirectory()
    except OSError:
        return _unavailable(parsed)
    try:
        directory = Path(temporary_directory.name)
        signers = directory / "allowed_signers"
        signature = directory / "signature.sig"
        try:
            _write_private(
                signers,
                _SYNTHETIC_PRINCIPAL.encode("ascii")
                + b' namespaces="'
                + _NAMESPACE.encode("ascii")
                + b'" '
                + signer_public_key
                + b"\n",
            )
            _write_private(signature, parsed.signature_armor)
        except OSError:
            provisional = _unavailable(parsed)
        else:
            pae = attestation.dsse_pae(attestation.DSSE_PAYLOAD_TYPE, parsed.statement_bytes)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                try:
                    result = _run_verify(executable, signers, signature, directory, pae, remaining)
                except (OSError, subprocess.SubprocessError, RuntimeError, ValueError):
                    provisional = _unavailable(parsed)
                else:
                    provisional = _classified(result, parsed, fingerprint)
            if time.monotonic() >= deadline:
                provisional = _unavailable(parsed)
    finally:
        try:
            temporary_directory.cleanup()
        except OSError:
            cleanup_failed = True
    if cleanup_failed:
        return _unavailable(parsed)
    if time.monotonic() >= deadline:
        return _unavailable(parsed)
    return provisional
