"""Pure syntactic acknowledgement request and response-envelope wire parsing."""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, NoReturn, cast

from . import approval_v2, attestation, attestation_input

MAX_REQUEST_BYTES = 65_536
MAX_ENVELOPE_BYTES = 131_072
MAX_STATEMENT_BYTES = 8_192
MAX_SIGNATURE_BYTES = attestation_input.MAX_SIGNATURE_BYTES
MAX_DESTINATION_ID_CHARS = 128
MAX_DESTINATION_REFERENCE_BYTES = 2_048

_REQUEST_SCHEMA = "brigade.evidence_ack_request.v1"
_ARTIFACT_SCHEMA = "brigade.evidence_package.v1"
_ARTIFACT_REPRESENTATION = "manifest-bytes"
_ACK_NAMESPACE = "brigade-evidence-ack"
_STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
_PREDICATE_TYPE = "https://brigade.dev/attestation/evidence-ack/v1"
_SUBJECT_NAME = "evidence-ack-request"
_HEX32_RE = re.compile(r"^[0-9a-f]{32}$")
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_DESTINATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_KEYID_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}$")
_TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
_ARMOR_PREFIX = b"-----BEGIN SSH SIGNATURE-----\n"
_ARMOR_SUFFIX = b"\n-----END SSH SIGNATURE-----"
_ERROR_CODES = frozenset({"type", "size", "json", "shape", "field", "encoding", "canonical"})


class AckWireError(ValueError):
    """A closed acknowledgement-wire validation error."""

    def __init__(self, code: str) -> None:
        if type(code) is not str or len(code) > 9 or code not in _ERROR_CODES:
            raise ValueError("invalid acknowledgement wire error code")
        self._code = code
        super().__init__(f"ack wire: {code}")

    @property
    def code(self) -> str:
        return self._code


@dataclass(frozen=True, slots=True)
class ArtifactClaim:
    manifest_sha256: str
    verify_run_id: str
    receipt_sha256: str


@dataclass(frozen=True, slots=True)
class AckRequest:
    nonce: str
    destination_id: str
    artifact: ArtifactClaim
    created_at: str
    expires_at: str
    canonical_bytes: bytes
    request_id: str


@dataclass(frozen=True, slots=True)
class ResponseClaim:
    request_id: str
    result: str
    destination_reference: str
    acknowledged_at: str


@dataclass(frozen=True, slots=True)
class AckEnvelope:
    claim: ResponseClaim
    keyid_hint: str
    statement_bytes: bytes
    signature_armor: bytes
    canonical_bytes: bytes
    envelope_id: str


def _error(code: str) -> NoReturn:
    raise AckWireError(code) from None


def _exact_str(value: object) -> str:
    if type(value) is not str:
        _error("shape")
    return cast(str, value)


def _unicode_scalar_bytes(value: str) -> bytes:
    try:
        return value.encode("utf-8", "strict")
    except UnicodeEncodeError:
        _error("json")


def _bounded_text(value: str, limit: int) -> bytes:
    if len(value) > limit:
        _error("size")
    encoded = _unicode_scalar_bytes(value)
    if len(encoded) > limit:
        _error("size")
    return encoded


def _closed_object(value: object, keys: frozenset[str]) -> dict[str, Any]:
    if type(value) is not dict or set(value) != keys:
        _error("shape")
    return cast(dict[str, Any], value)


def _constant(value: object, expected: str) -> None:
    if _exact_str(value) != expected:
        _error("shape")


def _nonce(value: object) -> str:
    value = _exact_str(value)
    if len(value) > 32:
        _error("size")
    _unicode_scalar_bytes(value)
    if not _HEX32_RE.fullmatch(value):
        _error("field")
    return value


def _destination_id(value: object) -> str:
    value = _exact_str(value)
    if len(value) > MAX_DESTINATION_ID_CHARS:
        _error("size")
    _unicode_scalar_bytes(value)
    if not _DESTINATION_ID_RE.fullmatch(value):
        _error("field")
    return value


def _digest(value: object) -> str:
    value = _exact_str(value)
    if len(value) > 64:
        _error("size")
    _unicode_scalar_bytes(value)
    if not _HEX64_RE.fullmatch(value):
        _error("field")
    return value


def _run_id(value: object) -> str:
    value = _exact_str(value)
    _bounded_text(value, MAX_REQUEST_BYTES)
    if value in {".", ".."} or not approval_v2._RUN_ID_RE.fullmatch(value):
        _error("field")
    return value


def _timestamp(value: object) -> tuple[str, datetime]:
    value = _exact_str(value)
    if len(value) > 20:
        _error("size")
    _unicode_scalar_bytes(value)
    if not _TIMESTAMP_RE.fullmatch(value):
        _error("field")
    try:
        return value, datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        _error("field")


def _destination_reference(value: object) -> str:
    value = _exact_str(value)
    encoded = _bounded_text(value, MAX_DESTINATION_REFERENCE_BYTES)
    if not value or any(ord(character) <= 0x1F or 0x7F <= ord(character) <= 0x9F for character in value):
        _error("field")
    if len(encoded) > MAX_DESTINATION_REFERENCE_BYTES:
        _error("size")
    return value


def _keyid(value: object) -> str:
    value = _exact_str(value)
    if len(value) > 50:
        _error("size")
    if not _KEYID_RE.fullmatch(value):
        _error("field")
    return value


def _canonical_bytes(value: dict[str, Any], cap: int) -> bytes:
    canonical = attestation.canonical_statement_bytes(value)
    if len(canonical) > cap:
        _error("size")
    return canonical


def _identity(domain: bytes, canonical: bytes) -> str:
    return hashlib.sha256(domain + b"\x00" + canonical).hexdigest()


def _parse_request_object(value: object) -> tuple[ArtifactClaim, str, str, str, str]:
    request = _closed_object(
        value,
        frozenset({"schema", "nonce", "destinationId", "artifact", "createdAt", "expiresAt"}),
    )
    _constant(request["schema"], _REQUEST_SCHEMA)
    nonce = _nonce(request["nonce"])
    destination_id = _destination_id(request["destinationId"])
    artifact = _closed_object(
        request["artifact"],
        frozenset({"schema", "representation", "sha256", "verifyRunId", "receiptSha256"}),
    )
    _constant(artifact["schema"], _ARTIFACT_SCHEMA)
    _constant(artifact["representation"], _ARTIFACT_REPRESENTATION)
    claim = ArtifactClaim(
        manifest_sha256=_digest(artifact["sha256"]),
        verify_run_id=_run_id(artifact["verifyRunId"]),
        receipt_sha256=_digest(artifact["receiptSha256"]),
    )
    created_at, created = _timestamp(request["createdAt"])
    expires_at, expires = _timestamp(request["expiresAt"])
    if created >= expires:
        _error("field")
    return claim, nonce, destination_id, created_at, expires_at


def _parse_json(raw: bytes, cap: int) -> object:
    if len(raw) > cap:
        _error("size")
    try:
        return attestation_input.strict_json_loads(raw, max_bytes=cap)
    except attestation_input.AttestationInputError:
        _error("json")


def build_request(
    *,
    nonce: str,
    destination_id: str,
    manifest_sha256: str,
    verify_run_id: str,
    receipt_sha256: str,
    created_at: str,
    expires_at: str,
) -> AckRequest:
    values = (
        nonce,
        destination_id,
        manifest_sha256,
        verify_run_id,
        receipt_sha256,
        created_at,
        expires_at,
    )
    if any(type(value) is not str for value in values):
        _error("type")
    request = {
        "schema": _REQUEST_SCHEMA,
        "nonce": nonce,
        "destinationId": destination_id,
        "artifact": {
            "schema": _ARTIFACT_SCHEMA,
            "representation": _ARTIFACT_REPRESENTATION,
            "sha256": manifest_sha256,
            "verifyRunId": verify_run_id,
            "receiptSha256": receipt_sha256,
        },
        "createdAt": created_at,
        "expiresAt": expires_at,
    }
    claim, parsed_nonce, parsed_destination, parsed_created, parsed_expires = _parse_request_object(request)
    canonical = _canonical_bytes(cast(dict[str, Any], request), MAX_REQUEST_BYTES)
    return AckRequest(
        nonce=parsed_nonce,
        destination_id=parsed_destination,
        artifact=claim,
        created_at=parsed_created,
        expires_at=parsed_expires,
        canonical_bytes=canonical,
        request_id=_identity(b"brigade.evidence_ack_request.v1", canonical),
    )


def parse_request(raw: bytes) -> AckRequest:
    if type(raw) is not bytes:
        _error("type")
    request = _parse_json(raw, MAX_REQUEST_BYTES)
    claim, nonce, destination_id, created_at, expires_at = _parse_request_object(request)
    canonical = _canonical_bytes(cast(dict[str, Any], request), MAX_REQUEST_BYTES)
    return AckRequest(
        nonce=nonce,
        destination_id=destination_id,
        artifact=claim,
        created_at=created_at,
        expires_at=expires_at,
        canonical_bytes=canonical,
        request_id=_identity(b"brigade.evidence_ack_request.v1", canonical),
    )


def _decode_base64(value: object, cap: int) -> bytes:
    value = _exact_str(value)
    if len(value) > 4 * ((cap + 2) // 3):
        _error("size")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        _error("encoding")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        _error("encoding")
    if len(decoded) > cap:
        _error("size")
    if base64.b64encode(decoded) != encoded:
        _error("encoding")
    return decoded


def _armor(armor: bytes) -> None:
    if b"\r" in armor:
        _error("encoding")
    if armor.endswith(b"\n\n"):
        _error("encoding")
    core = armor[:-1] if armor.endswith(b"\n") else armor
    if not core.startswith(_ARMOR_PREFIX) or not core.endswith(_ARMOR_SUFFIX):
        _error("encoding")
    body = core[len(_ARMOR_PREFIX) : -len(_ARMOR_SUFFIX)]
    lines = body.split(b"\n")
    if not lines or any(not line for line in lines):
        _error("encoding")
    encoded = b"".join(lines)
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        _error("encoding")
    if not decoded or base64.b64encode(decoded) != encoded:
        _error("encoding")


def _parse_statement(statement_bytes: bytes) -> ResponseClaim:
    try:
        value = attestation_input.strict_json_loads(statement_bytes, max_bytes=MAX_STATEMENT_BYTES)
    except attestation_input.AttestationInputError:
        _error("json")
    statement = _closed_object(value, frozenset({"_type", "subject", "predicateType", "predicate"}))
    _constant(statement["_type"], _STATEMENT_TYPE)
    _constant(statement["predicateType"], _PREDICATE_TYPE)
    subject = statement["subject"]
    if type(subject) is not list or len(subject) != 1:
        _error("shape")
    subject_item = _closed_object(subject[0], frozenset({"name", "digest"}))
    _constant(subject_item["name"], _SUBJECT_NAME)
    digest = _closed_object(subject_item["digest"], frozenset({"sha256"}))
    predicate = _closed_object(
        statement["predicate"],
        frozenset({"schemaVersion", "result", "destinationReference", "acknowledgedAt"}),
    )
    if type(predicate["schemaVersion"]) is not int or predicate["schemaVersion"] != 1:
        _error("shape")
    request_id = _digest(digest["sha256"])
    result = _exact_str(predicate["result"])
    if result not in {"received", "rejected"}:
        _error("field")
    reference = _destination_reference(predicate["destinationReference"])
    acknowledged_at, _ = _timestamp(predicate["acknowledgedAt"])
    if _canonical_bytes(statement, MAX_STATEMENT_BYTES) != statement_bytes:
        _error("canonical")
    return ResponseClaim(request_id, result, reference, acknowledged_at)


def parse_envelope(raw: bytes) -> AckEnvelope:
    if type(raw) is not bytes:
        _error("type")
    value = _parse_json(raw, MAX_ENVELOPE_BYTES)
    envelope = _closed_object(value, frozenset({"payloadType", "payload", "signatures", "brigade"}))
    _constant(envelope["payloadType"], attestation.DSSE_PAYLOAD_TYPE)
    signatures = envelope["signatures"]
    if type(signatures) is not list or len(signatures) != 1:
        _error("shape")
    signature = _closed_object(signatures[0], frozenset({"keyid", "sig"}))
    brigade = _closed_object(envelope["brigade"], frozenset({"profile", "namespace"}))
    _constant(brigade["profile"], attestation.ATTESTATION_PROFILE)
    _constant(brigade["namespace"], _ACK_NAMESPACE)
    keyid_hint = _keyid(signature["keyid"])
    statement_bytes = _decode_base64(envelope["payload"], MAX_STATEMENT_BYTES)
    claim = _parse_statement(statement_bytes)
    signature_armor = _decode_base64(signature["sig"], MAX_SIGNATURE_BYTES)
    _armor(signature_armor)
    canonical = _canonical_bytes(envelope, MAX_ENVELOPE_BYTES)
    return AckEnvelope(
        claim=claim,
        keyid_hint=keyid_hint,
        statement_bytes=statement_bytes,
        signature_armor=signature_armor,
        canonical_bytes=canonical,
        envelope_id=_identity(b"brigade.evidence_ack_response.v1", canonical),
    )
