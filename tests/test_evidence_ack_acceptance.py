"""Offline acknowledgement association and authorization contracts."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from brigade import attestation, evidence_ack_crypto as crypto, evidence_ack_protocol as wire
from brigade import evidence_package_snapshot as packages, localio

FP = "SHA256:" + "A" * 43
OTHER_FP = "SHA256:" + "B" * 43
START = "2026-09-08T00:00:00Z"
ACK = "2026-09-08T01:00:00Z"
NOW = "2026-09-08T02:00:00Z"
END = "2026-09-09T00:00:00Z"


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _package(tmp_path: Path) -> packages.PackageSnapshot:
    # Synthetic immutable observation. Real file validation stays in snapshot_package.
    receipt = {"schema_version": 2, "run_id": "verify-1"}
    digest = localio.canonical_json_digest(receipt)
    receipt["digests"] = {"algorithm": "sha256", "receipt_sha256": digest}  # type: ignore[assignment]
    contents = {"receipt.json": _canonical(receipt), "summary.md": b"fixture summary\n"}
    metadata = {"receipt.json": ("receipt", "application/json"), "summary.md": ("summary", "text/plain")}
    entries = [
        {
            "path": name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "kind": metadata[name][0],
            "media_type": metadata[name][1],
        }
        for name, data in sorted(contents.items())
    ]
    manifest = {
        "schema": "brigade.evidence_package.v1",
        "schema_version": 1,
        "created_at": START,
        "source": {
            "verify_run_id": "verify-1",
            "receipt_sha256": digest,
            "producer_run_id": None,
            "tree_fingerprint": None,
            "baseline_commit": None,
            "changes_patch_sha256": None,
        },
        "entries": entries,
        "entries_sha256": localio.canonical_json_digest(entries),
        "limitations": ["receipt-contains-local-paths", "integrity-only"],
    }
    for name, data in contents.items():
        (tmp_path / name).write_bytes(data)
    (tmp_path / "manifest.json").write_bytes(_canonical(manifest))
    raw = _canonical(manifest)
    return packages.PackageSnapshot(
        raw,
        hashlib.sha256(raw).hexdigest(),
        packages.PackageSource("verify-1", digest, None, None, None, None),
        tuple(
            packages.PackageEntry(entry["path"], entry["sha256"], entry["bytes"], entry["kind"], entry["media_type"])
            for entry in entries
        ),
    )


def _envelope(
    request_id: str, *, result: str = "received", acknowledged_at: str = ACK, hint: str = FP
) -> wire.AckEnvelope:
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "subject": [{"name": "evidence-ack-request", "digest": {"sha256": request_id}}],
        "predicateType": "https://brigade.dev/attestation/evidence-ack/v1",
        "predicate": {
            "schemaVersion": 1,
            "result": result,
            "destinationReference": "object-1/version-2",
            "acknowledgedAt": acknowledged_at,
        },
    }
    armor = b"-----BEGIN SSH SIGNATURE-----\nYQ==\n-----END SSH SIGNATURE-----\n"
    return wire.parse_envelope(
        _canonical(
            {
                "payloadType": attestation.DSSE_PAYLOAD_TYPE,
                "payload": base64.b64encode(_canonical(statement)).decode(),
                "signatures": [{"keyid": hint, "sig": base64.b64encode(armor).decode()}],
                "brigade": {"profile": attestation.ATTESTATION_PROFILE, "namespace": "brigade-evidence-ack"},
            }
        )
    )


@pytest.fixture
def case(tmp_path):
    from brigade import evidence_ack_acceptance as acceptance

    package = _package(tmp_path)
    request = wire.build_request(
        nonce="1" * 32,
        destination_id="archive-a",
        manifest_sha256=package.manifest_sha256,
        verify_run_id="verify-1",
        receipt_sha256=package.source.receipt_sha256,
        created_at=START,
        expires_at=END,
    )
    envelope = _envelope(request.request_id)
    observed = crypto.AckCryptoObservation("VALID", "MATCH", FP, request.request_id, "received", envelope.envelope_id)
    policy = acceptance.DestinationPolicy(
        destination_id="archive-a",
        audience="audit-a",
        revision="revision-1",
        valid_from=START,
        valid_until=END,
        keys=(acceptance.AuthorizedKey(FP, START, END, "clear", "2" * 64, START, END),),
    )
    association = acceptance.AckAssociation(
        request.request_id,
        envelope.envelope_id,
        request.nonce,
        "archive-a",
        "audit-a",
        package.manifest_sha256,
        "verify-1",
        package.source.receipt_sha256,
    )
    replay = acceptance.ReplayObservation(association, "clear", "3" * 64, NOW, END)
    return dict(
        package=package,
        request=request,
        envelope=envelope,
        crypto=observed,
        policy=policy,
        destination_id="archive-a",
        audience="audit-a",
        evaluated_at=NOW,
        replay=replay,
    )


def _accept(case, **changes):
    from brigade.evidence_ack_acceptance import accept_acknowledgement

    return accept_acknowledgement(**(case | changes))


def test_acceptance_separates_authorization_assertion_and_authority(case):
    result = _accept(case)
    assert result.state == "accepted"
    assert result.signature == "VALID" and result.authorization == "authorized"
    assert result.asserted_result == "received"
    assert result.custody_authority == result.time_authority == "authenticated-assertion"
    assert result.destination_reference == "object-1/version-2"
    assert result.object_version is None
    assert result.association == case["replay"].association
    assert result.policy_revision == "revision-1"
    assert len(result.policy_sha256) == 64


@pytest.mark.parametrize(
    "field,value", [("request_id", "9" * 64), ("nonce", "9" * 32), ("destination_id", "archive-b")]
)
def test_replaced_request_fields_cannot_override_canonical_request(case, field, value):
    result = _accept(case, request=replace(case["request"], **{field: value}))
    assert result.state == "rejected" and "request-binding" in result.reasons


@pytest.mark.parametrize(
    "field,value", [("manifest_sha256", "9" * 64), ("verify_run_id", "verify-2"), ("receipt_sha256", "9" * 64)]
)
def test_request_artifact_must_bind_exact_manifest_run_and_receipt(case, field, value):
    claim = replace(case["request"].artifact, **{field: value})
    request = wire.build_request(
        nonce="1" * 32,
        destination_id="archive-a",
        created_at=START,
        expires_at=END,
        manifest_sha256=claim.manifest_sha256,
        verify_run_id=claim.verify_run_id,
        receipt_sha256=claim.receipt_sha256,
    )
    result = _accept(case, request=request)
    assert result.state == "rejected" and "package-binding" in result.reasons


@pytest.mark.parametrize("mutation", ["omitted", "injected", "metadata", "source", "bytes", "digest"])
def test_snapshot_metadata_must_equal_complete_exact_manifest(case, mutation):
    package = case["package"]
    if mutation == "omitted":
        package = replace(package, entries=package.entries[:1])
    elif mutation == "injected":
        package = replace(package, entries=package.entries + (package.entries[0],))
    elif mutation == "metadata":
        package = replace(package, entries=(replace(package.entries[0], size_bytes=0), *package.entries[1:]))
    elif mutation == "source":
        package = replace(package, source=replace(package.source, verify_run_id="verify-2"))
    elif mutation == "bytes":
        package = replace(package, manifest_bytes=package.manifest_bytes + b" ")
    else:
        package = replace(package, manifest_sha256="9" * 64)
    result = _accept(case, package=package)
    assert result.state == "rejected" and "package-binding" in result.reasons


@pytest.mark.parametrize(
    "field,value", [("envelope_id", "9" * 64), ("asserted_request_id", "9" * 64), ("asserted_result", "rejected")]
)
def test_crypto_observation_must_bind_exact_envelope_and_claim(case, field, value):
    result = _accept(case, crypto=replace(case["crypto"], **{field: value}))
    assert result.state == "rejected" and "crypto-binding" in result.reasons


def test_prior_batch_and_replaced_envelope_claim_are_rejected(case):
    envelope = _envelope("9" * 64)
    observed = replace(case["crypto"], asserted_request_id="9" * 64, envelope_id=envelope.envelope_id)
    assert _accept(case, envelope=envelope, crypto=observed).state == "rejected"
    assert (
        _accept(
            case, envelope=replace(case["envelope"], claim=replace(case["envelope"].claim, result="rejected"))
        ).state
        == "rejected"
    )


@pytest.mark.parametrize("changes", [{"destination_id": "archive-b"}, {"audience": "audit-b"}])
def test_independent_expected_destination_and_audience_control_authorization(case, changes):
    assert _accept(case, **changes).state == "rejected"


@pytest.mark.parametrize(
    "field,value,state",
    [
        ("fingerprint", OTHER_FP, "rejected"),
        ("revocation", "revoked", "rejected"),
        ("revocation", "unavailable", "unavailable"),
        ("revocation", "indeterminate", "indeterminate"),
        ("valid_until", ACK, "expired"),
        ("evidence_valid_until", ACK, "indeterminate"),
        ("revocation_evidence_sha256", None, "indeterminate"),
    ],
)
def test_key_policy_requires_identity_validity_and_fresh_revocation_evidence(case, field, value, state):
    policy = replace(case["policy"], keys=(replace(case["policy"].keys[0], **{field: value}),))
    result = _accept(case, policy=policy)
    assert result.state == state
    assert result.signature == "VALID" and result.authorization != "authorized"


def test_key_hint_never_authorizes_untrusted_verified_key(case):
    assert (
        _accept(case, crypto=replace(case["crypto"], verified_fingerprint=OTHER_FP, keyid="MISMATCH")).state
        == "rejected"
    )
    envelope = _envelope(case["request"].request_id, hint=OTHER_FP)
    observed = replace(case["crypto"], keyid="MISMATCH", envelope_id=envelope.envelope_id)
    replay = replace(case["replay"], association=replace(case["replay"].association, envelope_id=envelope.envelope_id))
    result = _accept(case, envelope=envelope, crypto=observed, replay=replay)
    assert result.state == "accepted" and result.keyid == "MISMATCH"


@pytest.mark.parametrize(
    "field,value,state",
    [
        ("valid_until", NOW, "expired"),
        ("valid_from", END, "indeterminate"),
        ("revision", "", "indeterminate"),
        ("keys", (), "rejected"),
    ],
)
def test_policy_validity_and_revision_are_required(case, field, value, state):
    assert _accept(case, policy=replace(case["policy"], **{field: value})).state == state


def test_conflicting_key_policy_is_ambiguous_and_policy_digest_tracks_revision(case):
    key = case["policy"].keys[0]
    assert _accept(case, policy=replace(case["policy"], keys=(key, key))).state == "ambiguous"
    before = _accept(case)
    after = _accept(case, policy=replace(case["policy"], revision="revision-2"))
    assert before.policy_sha256 != after.policy_sha256


@pytest.mark.parametrize("state", ["replayed", "ambiguous", "unavailable", "indeterminate"])
def test_replay_observation_preserves_nonclear_state(case, state):
    assert _accept(case, replay=replace(case["replay"], state=state)).state == state


@pytest.mark.parametrize(
    "changes", [{"valid_until": NOW}, {"checked_at": END}, {"evidence_sha256": None}, {"association": None}]
)
def test_stale_or_unbound_replay_clearance_cannot_accept(case, changes):
    assert _accept(case, replay=replace(case["replay"], **changes)).state == "indeterminate"


def test_request_expiry_is_half_open_and_local_evaluation_time_is_required(case):
    assert _accept(case, evaluated_at=END).state == "expired"
    assert _accept(case, evaluated_at="2026-09-07T23:59:59Z").state == "indeterminate"
    assert _accept(case, evaluated_at=None).state == "unavailable"
    assert _accept(case, evaluated_at="invalid").state == "indeterminate"


def test_acknowledged_time_must_fall_in_request_window_and_not_after_evaluation(case):
    for stamp in ("2026-09-07T23:59:59Z", END, "2026-09-08T03:00:00Z"):
        envelope = _envelope(case["request"].request_id, acknowledged_at=stamp)
        observed = replace(case["crypto"], envelope_id=envelope.envelope_id)
        replay = replace(
            case["replay"], association=replace(case["replay"].association, envelope_id=envelope.envelope_id)
        )
        result = _accept(case, envelope=envelope, crypto=observed, replay=replay)
        assert result.state == "indeterminate" and "acknowledged-time-indeterminate" in result.reasons


@pytest.mark.parametrize("refusal", ["rejected", "ambiguous", "unavailable"])
def test_conclusive_policy_refusal_is_preserved_when_request_is_expired(case, refusal):
    policy = case["policy"]
    if refusal == "rejected":
        policy = replace(policy, keys=(replace(policy.keys[0], revocation="revoked"),))
    elif refusal == "ambiguous":
        policy = replace(policy, keys=(policy.keys[0], policy.keys[0]))
    else:
        policy = None
    result = _accept(case, policy=policy, evaluated_at=END)
    assert result.state == result.authorization == refusal
    assert result.reasons == ("destination-authorization",)


@pytest.mark.parametrize("raw", [b"invalid", None])
def test_malformed_exact_class_envelope_has_a_bounded_refusal(case, raw):
    envelope = replace(
        case["envelope"], claim=None, canonical_bytes=raw if raw is not None else case["envelope"].canonical_bytes
    )
    result = _accept(case, envelope=envelope)
    assert result.state == "rejected"
    if raw is not None:
        assert result.asserted_result is None
        assert result.destination_reference is None and result.acknowledged_at is None


def test_authenticated_rejection_never_establishes_custody(case):
    envelope = _envelope(case["request"].request_id, result="rejected")
    observed = replace(case["crypto"], asserted_result="rejected", envelope_id=envelope.envelope_id)
    replay = replace(case["replay"], association=replace(case["replay"].association, envelope_id=envelope.envelope_id))
    result = _accept(case, envelope=envelope, crypto=observed, replay=replay)
    assert result.state == "rejected" and result.asserted_result == "rejected"
    assert result.authorization == "authorized" and result.custody_authority == "not-established"


@pytest.mark.parametrize(
    "changes,state",
    [
        ({"package": packages.PackageSnapshotError("unavailable")}, "unavailable"),
        ({"package": packages.PackageSnapshotError("changed")}, "rejected"),
        ({"policy": None}, "unavailable"),
        ({"replay": None}, "unavailable"),
    ],
)
def test_missing_capabilities_never_become_acceptance(case, changes, state):
    assert _accept(case, **changes).state == state


@pytest.mark.parametrize("signature,state", [("UNAVAILABLE", "unavailable"), ("NOT-VERIFIED", "indeterminate")])
def test_crypto_capability_and_validity_stay_distinct(case, signature, state):
    observed = replace(case["crypto"], signature=signature, keyid="UNRESOLVED", verified_fingerprint=None)
    result = _accept(case, crypto=observed)
    assert result.state == state and result.signature == signature
    assert result.authorization == "not-established"


def test_independent_authority_requires_exact_association_reference_and_time(case):
    from brigade.evidence_ack_acceptance import AuthorityObservation

    custody = AuthorityObservation(
        case["replay"].association,
        "custody",
        "supported",
        "4" * 64,
        "independent-operating-evidence",
        "object-1/version-2",
        ACK,
        "version-2",
    )
    clock = replace(custody, kind="time", source_class="independent-time-evidence", object_version=None)
    result = _accept(case, authority=(custody, clock))
    assert result.custody_authority == "independent-operating-evidence"
    assert result.time_authority == "independent-time-evidence" and result.object_version == "version-2"
    for changed in (
        replace(custody, destination_reference="object-2"),
        replace(custody, acknowledged_at=NOW),
        replace(custody, association=replace(custody.association, manifest_sha256="9" * 64)),
    ):
        result = _accept(case, authority=(changed,))
        assert result.state == "indeterminate" and result.custody_authority == "not-established"


def test_unavailable_or_conflicting_independent_authority_is_preserved(case):
    from brigade.evidence_ack_acceptance import AuthorityObservation

    evidence = AuthorityObservation(
        case["replay"].association,
        "custody",
        "unavailable",
        None,
        "independent-operating-evidence",
        "object-1/version-2",
        ACK,
        None,
    )
    assert _accept(case, authority=(evidence,)).state == "unavailable"
    assert _accept(case, authority=(evidence, evidence)).state == "ambiguous"
