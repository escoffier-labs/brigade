from __future__ import annotations

import base64
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from brigade import approval, approval_v2, attestation, attestation_input, cosign_attestation


class _UntrustedMapping(Mapping[str, Any]):
    """A mapping whose read API must not be used after validation snapshots it."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        self._values = dict(values)

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def get(self, key: str, default: Any = None) -> Any:
        return default


def test_verify_rejects_duplicate_names(monkeypatch) -> None:
    called = False
    parse_error: str | None = None
    strict_json_loads = attestation_input.strict_json_loads

    def fake_which(_: str) -> str:
        return "ssh-keygen"

    def fake_run(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal called
        called = True
        raise AssertionError("signature program must not run for invalid input")

    monkeypatch.setattr(attestation.shutil, "which", fake_which)
    monkeypatch.setattr(attestation.subprocess, "run", fake_run)

    def capture_duplicate_error(source, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal parse_error
        try:
            return strict_json_loads(source, **kwargs)
        except attestation_input.AttestationInputError as exc:
            parse_error = str(exc)
            raise

    monkeypatch.setattr(attestation.attestation_input, "strict_json_loads", capture_duplicate_error)

    result = attestation.verify_attestation(
        '{"payloadType":"application/vnd.in-toto+json","payload":"e30=","payload":"e30=","signatures":[],"brigade":{}}'
    )

    assert result.status == attestation.STATUS_UNVERIFIABLE_SIGNATURE
    assert called is False
    assert parse_error == "JSON contains duplicate property names"


def test_approval_payload_decoders_accept_bounded_canonical_json() -> None:
    statement = {"subject": []}
    payload = base64.b64encode(attestation.canonical_statement_bytes(statement)).decode("ascii")

    v1_statement, v1_bytes = approval._decode_statement({"payload": payload})
    v2_statement, v2_bytes = approval_v2._decode_payload({"payload": payload}, "approval")

    assert v1_statement == statement
    assert v1_bytes == attestation.canonical_statement_bytes(statement)
    assert v2_statement == statement
    assert v2_bytes == attestation.canonical_statement_bytes(statement)


def test_approval_json_object_loaders_reject_duplicate_names(tmp_path) -> None:
    path = tmp_path / "approval.json"
    path.write_text('{"kind":"one","kind":"two"}', encoding="utf-8")

    with pytest.raises(approval.ApprovalError, match="not readable JSON"):
        approval._load_json_object(path, label="approval")
    with pytest.raises(approval_v2.ApprovalV2Error, match="not readable JSON"):
        approval_v2._load_json_object(path, "approval")


def test_cosign_bundle_uses_validated_mapping_snapshot() -> None:
    statement = {"subject": []}
    payload = base64.b64encode(attestation.canonical_statement_bytes(statement)).decode("ascii")
    bundle = _UntrustedMapping(
        {
            "mediaType": cosign_attestation.SIGSTORE_BUNDLE_MEDIA_TYPE,
            "verificationMaterial": {
                "publicKey": {"hint": "test-key"},
                "tlogEntries": [],
                "timestampVerificationData": {},
            },
            "dsseEnvelope": {
                "payloadType": attestation.DSSE_PAYLOAD_TYPE,
                "payload": payload,
                "signatures": [{"sig": "c2ln", "keyid": "test-key"}],
            },
        }
    )

    validated = cosign_attestation.validate_bundle(bundle, _UntrustedMapping(statement))

    assert validated["mediaType"] == cosign_attestation.SIGSTORE_BUNDLE_MEDIA_TYPE


def test_cosign_duplicate_parse_error_does_not_echo_property_name(monkeypatch: pytest.MonkeyPatch) -> None:
    statement = {"subject": []}
    payload = base64.b64encode(attestation.canonical_statement_bytes(statement)).decode("ascii")
    bundle = {
        "mediaType": cosign_attestation.SIGSTORE_BUNDLE_MEDIA_TYPE,
        "verificationMaterial": {"publicKey": {"hint": "test-key"}, "tlogEntries": []},
        "dsseEnvelope": {
            "payloadType": attestation.DSSE_PAYLOAD_TYPE,
            "payload": payload,
            "signatures": [{"sig": "c2ln"}],
        },
    }

    def duplicate_name(*args: object, **kwargs: object) -> object:
        raise attestation_input.AttestationInputError("JSON contains duplicate property names")

    monkeypatch.setattr(cosign_attestation.attestation_input, "strict_json_loads", duplicate_name)

    with pytest.raises(cosign_attestation.CosignAttestationError) as exc_info:
        cosign_attestation.validate_bundle(bundle, statement)

    assert str(exc_info.value) == "dsseEnvelope payload is not valid JSON"


def test_verify_discards_dot_only_fallback_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    statement = {
        "_type": attestation.IN_TOTO_STATEMENT_TYPE,
        "predicateType": attestation.IN_TOTO_TEST_RESULT_PREDICATE_TYPE,
        "predicate": {"run": {"id": "."}},
    }
    payload = base64.b64encode(attestation.canonical_statement_bytes(statement)).decode("ascii")
    envelope = {
        "payloadType": attestation.DSSE_PAYLOAD_TYPE,
        "payload": payload,
        "signatures": [{"sig": "ignored"}],
        "brigade": {
            "profile": attestation.ATTESTATION_PROFILE,
            "namespace": attestation.ATTESTATION_NAMESPACE,
        },
    }

    monkeypatch.setattr(attestation.shutil, "which", lambda _: "ssh-keygen")
    monkeypatch.setattr(attestation, "_decode_sig_armored", lambda _: None)

    result = attestation.verify_attestation(envelope)

    assert result.status == attestation.STATUS_UNVERIFIABLE_SIGNATURE
    assert result.run_id is None
