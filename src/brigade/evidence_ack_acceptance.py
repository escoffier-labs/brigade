"""Pure composition of captured acknowledgement observations and external policy.

No I/O, cryptography, replay reservation, policy discovery, or clock lookup occurs
here. Observation provenance and admission of organizational evidence belong to
the caller. A digest identifies supplied evidence, it does not authenticate it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from datetime import datetime

from . import attestation_input, evidence_ack_crypto as ack_crypto, evidence_ack_protocol as wire
from . import evidence_package_snapshot as packages, localio

_STATES = frozenset({"accepted", "rejected", "expired", "replayed", "ambiguous", "unavailable", "indeterminate"})


@dataclass(frozen=True, slots=True)
class AuthorizedKey:
    fingerprint: str
    valid_from: str
    valid_until: str
    revocation: str
    revocation_evidence_sha256: str | None
    evidence_valid_from: str
    evidence_valid_until: str


@dataclass(frozen=True, slots=True)
class DestinationPolicy:
    destination_id: str
    audience: str
    revision: str
    valid_from: str
    valid_until: str
    keys: tuple[AuthorizedKey, ...]


@dataclass(frozen=True, slots=True)
class AckAssociation:
    request_id: str
    envelope_id: str
    nonce: str
    destination_id: str
    audience: str
    manifest_sha256: str
    verify_run_id: str
    receipt_sha256: str


@dataclass(frozen=True, slots=True)
class ReplayObservation:
    association: AckAssociation
    state: str
    evidence_sha256: str | None
    checked_at: str
    valid_until: str


@dataclass(frozen=True, slots=True)
class AuthorityObservation:
    association: AckAssociation
    kind: str
    state: str
    evidence_sha256: str | None
    source_class: str
    destination_reference: str
    acknowledged_at: str
    object_version: str | None


@dataclass(frozen=True, slots=True)
class AckAcceptance:
    state: str
    reasons: tuple[str, ...]
    association: AckAssociation | None
    signature: str
    keyid: str
    verified_fingerprint: str | None
    authorization: str
    asserted_result: str | None
    destination_reference: str | None
    acknowledged_at: str | None
    evaluated_at: str | None
    policy_sha256: str | None
    policy_revision: str | None
    custody_authority: str
    time_authority: str
    object_version: str | None
    replay: ReplayObservation | None
    authority: tuple[AuthorityObservation, ...]


def _text(value: object, limit: int = 128) -> bool:
    return type(value) is str and 0 < len(value) <= limit and all(0x20 <= ord(char) < 0x7F for char in value)


def _digest(value: object) -> bool:
    return type(value) is str and len(value) == 64 and bool(re.fullmatch("[0-9a-f]{64}", value))


def _time(value: object) -> datetime | None:
    if (
        type(value) is not str
        or len(value) != 20
        or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", value)
    ):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


def _window(start: str, end: str, now: datetime, ack: datetime) -> str:
    lower, upper = _time(start), _time(end)
    if lower is None or upper is None or lower >= upper or now < lower or ack < lower:
        return "indeterminate"
    if now >= upper or ack >= upper:
        return "expired"
    return "authorized"


def _package_bound(package: packages.PackageSnapshot, request: wire.AckRequest) -> bool:
    """Reconcile all duplicated fields against the existing strict manifest contract."""
    if (
        type(package.manifest_bytes) is not bytes
        or len(package.manifest_bytes) > 64 * 1024
        or hashlib.sha256(package.manifest_bytes).hexdigest() != package.manifest_sha256
        or type(package.source) is not packages.PackageSource
        or type(package.entries) is not tuple
        or any(type(entry) is not packages.PackageEntry for entry in package.entries)
    ):
        return False
    try:
        value = attestation_input.strict_json_loads(package.manifest_bytes, max_bytes=64 * 1024)
        _, source, entries = packages._validate_manifest(value)
    except (attestation_input.AttestationInputError, packages.PackageSnapshotError):
        return False
    expected_entries = tuple(
        packages.PackageEntry(entry["path"], entry["sha256"], entry["bytes"], entry["kind"], entry["media_type"])
        for entry in entries
    )
    return (
        asdict(package.source) == source
        and package.entries == expected_entries
        and request.artifact
        == wire.ArtifactClaim(package.manifest_sha256, package.source.verify_run_id, package.source.receipt_sha256)
    )


def _policy_state(
    policy: DestinationPolicy | None,
    fingerprint: str | None,
    destination_id: str,
    audience: str,
    now: datetime | None,
    ack: datetime,
) -> tuple[str, str | None, str | None]:
    if policy is None:
        return "unavailable", None, None
    if (
        type(policy) is not DestinationPolicy
        or not _text(policy.destination_id)
        or not _text(policy.audience)
        or not _text(policy.revision)
        or type(policy.keys) is not tuple
        or len(policy.keys) > 128
        or any(type(key) is not AuthorizedKey for key in policy.keys)
    ):
        return "indeterminate", None, None
    for key in policy.keys:
        if (
            type(key.fingerprint) is not str
            or len(key.fingerprint) != 50
            or not re.fullmatch(r"SHA256:[A-Za-z0-9+/]{43}", key.fingerprint)
            or type(key.revocation) is not str
            or key.revocation not in {"clear", "revoked", "unavailable", "indeterminate"}
            or any(
                _time(value) is None
                for value in (key.valid_from, key.valid_until, key.evidence_valid_from, key.evidence_valid_until)
            )
            or (key.revocation_evidence_sha256 is not None and not _digest(key.revocation_evidence_sha256))
        ):
            return "indeterminate", None, None
    if _time(policy.valid_from) is None or _time(policy.valid_until) is None:
        return "indeterminate", None, None
    digest = localio.canonical_json_digest(asdict(policy))
    revision = policy.revision
    if policy.destination_id != destination_id or policy.audience != audience:
        return "rejected", digest, revision
    matches = tuple(key for key in policy.keys if key.fingerprint == fingerprint)
    if len(matches) > 1:
        return "ambiguous", digest, revision
    if not matches:
        return "rejected", digest, revision
    key = matches[0]
    if key.revocation == "revoked":
        return "rejected", digest, revision
    if key.revocation in {"unavailable", "indeterminate"}:
        return key.revocation, digest, revision
    if now is None:
        return "indeterminate", digest, revision
    state = _window(policy.valid_from, policy.valid_until, now, ack)
    if state == "authorized":
        state = _window(key.valid_from, key.valid_until, now, ack)
    if state == "authorized" and (
        not _digest(key.revocation_evidence_sha256)
        or _window(key.evidence_valid_from, key.evidence_valid_until, now, ack) != "authorized"
    ):
        state = "indeterminate"
    return state, digest, revision


def _replay_state(
    replay: ReplayObservation | None, association: AckAssociation, now: datetime, created: datetime
) -> str:
    if replay is None:
        return "unavailable"
    if type(replay) is not ReplayObservation or type(replay.state) is not str:
        return "indeterminate"
    if replay.association != association:
        return "indeterminate"
    if replay.state in {"replayed", "ambiguous", "unavailable", "indeterminate"}:
        return replay.state
    checked, until = _time(replay.checked_at), _time(replay.valid_until)
    if (
        replay.state != "clear"
        or not _digest(replay.evidence_sha256)
        or checked is None
        or until is None
        or not created <= checked <= now < until
    ):
        return "indeterminate"
    return "accepted"


def _authority_state(
    authority: tuple[AuthorityObservation, ...],
    association: AckAssociation,
    envelope: wire.AckEnvelope,
) -> tuple[str, str, str, str | None]:
    custody = clock = "authenticated-assertion"
    version = None
    if (
        type(authority) is not tuple
        or len(authority) > 2
        or any(type(item) is not AuthorityObservation for item in authority)
    ):
        return "indeterminate", "not-established", "not-established", None
    if len({item.kind for item in authority if type(item.kind) is str}) != len(authority):
        return "ambiguous", "not-established", "not-established", None
    for item in authority:
        if (
            item.association != association
            or item.destination_reference != envelope.claim.destination_reference
            or item.acknowledged_at != envelope.claim.acknowledged_at
            or type(item.kind) is not str
            or item.kind not in {"custody", "time"}
            or item.source_class
            != {"custody": "independent-operating-evidence", "time": "independent-time-evidence"}[item.kind]
            or type(item.state) is not str
            or (item.object_version is not None and not _text(item.object_version, 2048))
        ):
            return "indeterminate", "not-established", "not-established", None
        if item.state in {"unavailable", "indeterminate", "ambiguous", "rejected"}:
            return item.state, "not-established", "not-established", None
        if item.state != "supported" or not _digest(item.evidence_sha256):
            return "indeterminate", "not-established", "not-established", None
        if item.kind == "custody":
            if item.object_version is None:
                return "indeterminate", "not-established", "not-established", None
            custody, version = item.source_class, item.object_version
        else:
            clock = item.source_class
    return "accepted", custody, clock, version


def accept_acknowledgement(
    *,
    package: packages.PackageSnapshot | packages.PackageSnapshotError,
    request: wire.AckRequest,
    envelope: wire.AckEnvelope,
    crypto: ack_crypto.AckCryptoObservation,
    policy: DestinationPolicy | None,
    destination_id: str,
    audience: str,
    evaluated_at: str | None,
    replay: ReplayObservation | None,
    authority: tuple[AuthorityObservation, ...] = (),
) -> AckAcceptance:
    """Evaluate captured inputs. Accepted means an authorized receipt assertion.

    The caller must independently admit policy, replay and authority evidence,
    retain its provenance, and obtain snapshot/crypto observations from their
    existing adapters. This function neither reserves nor consumes a nonce.
    """
    if (
        type(request) is not wire.AckRequest
        or type(envelope) is not wire.AckEnvelope
        or type(crypto) is not ack_crypto.AckCryptoObservation
    ):
        raise ValueError("invalid acknowledgement observation type")
    reasons: list[str] = []
    association = None
    authorization = "not-established"
    policy_digest = policy_revision = None
    custody = clock = "not-established"
    version = None
    claim: wire.ResponseClaim | None = None

    def result(state: str) -> AckAcceptance:
        if state not in _STATES:
            raise ValueError("invalid acceptance state")
        return AckAcceptance(
            state,
            tuple(reasons),
            association,
            crypto.signature,
            crypto.keyid,
            crypto.verified_fingerprint,
            authorization,
            claim.result if claim is not None else None,
            claim.destination_reference if claim is not None else None,
            claim.acknowledged_at if claim is not None else None,
            evaluated_at,
            policy_digest,
            policy_revision,
            custody,
            clock,
            version,
            replay,
            authority,
        )

    try:
        parsed_request = wire.parse_request(request.canonical_bytes)
        parsed_envelope = wire.parse_envelope(envelope.canonical_bytes)
        claim = parsed_envelope.claim
    except wire.AckWireError:
        reasons.append("wire-binding")
        return result("rejected")
    if parsed_request != request:
        reasons.append("request-binding")
        return result("rejected")
    if type(package) is packages.PackageSnapshotError:
        reasons.append("package-" + package.code)
        return result("unavailable" if package.code == "unavailable" else "rejected")
    if type(package) is not packages.PackageSnapshot or not _package_bound(package, request):
        reasons.append("package-binding")
        return result("rejected")
    if parsed_envelope != envelope or envelope.claim.request_id != request.request_id:
        reasons.append("envelope-binding")
        return result("rejected")
    if (
        crypto.envelope_id != envelope.envelope_id
        or crypto.asserted_request_id != request.request_id
        or crypto.asserted_result != envelope.claim.result
    ):
        reasons.append("crypto-binding")
        return result("rejected")
    if not _text(destination_id) or not _text(audience):
        reasons.append("destination-policy-input")
        return result("indeterminate")
    if request.destination_id != destination_id:
        reasons.append("destination-binding")
        return result("rejected")
    association = AckAssociation(
        request.request_id,
        envelope.envelope_id,
        request.nonce,
        destination_id,
        audience,
        package.manifest_sha256,
        package.source.verify_run_id,
        package.source.receipt_sha256,
    )
    if crypto.signature != "VALID":
        reasons.append("signature-" + crypto.signature.lower())
        return result("unavailable" if crypto.signature == "UNAVAILABLE" else "indeterminate")
    now = _time(evaluated_at)
    ack = _time(envelope.claim.acknowledged_at)
    created, expires = _time(request.created_at), _time(request.expires_at)
    assert ack is not None and created is not None and expires is not None
    authorization, policy_digest, policy_revision = _policy_state(
        policy, crypto.verified_fingerprint, destination_id, audience, now, ack
    )
    if authorization != "authorized":
        reasons.append("destination-authorization")
    if authorization in {"rejected", "ambiguous", "unavailable"}:
        return result(authorization)
    if evaluated_at is None:
        reasons.append("evaluation-time-unavailable")
        return result("unavailable")
    if now is None:
        reasons.append("evaluation-time-indeterminate")
        return result("indeterminate")
    if now >= expires:
        reasons.append("request-expired")
        return result("expired")
    if now < created or not created <= ack < expires or ack > now:
        reasons.append("acknowledged-time-indeterminate")
        return result("indeterminate")
    if authorization != "authorized":
        return result(authorization)
    if envelope.claim.result == "rejected":
        reasons.append("destination-rejected")
        return result("rejected")
    replay_state = _replay_state(replay, association, now, created)
    if replay_state != "accepted":
        reasons.append("replay-" + replay_state)
        return result(replay_state)
    authority_state, custody, clock, version = _authority_state(authority, association, envelope)
    if authority_state != "accepted":
        reasons.append("authority-" + authority_state)
    return result(authority_state)
