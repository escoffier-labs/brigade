"""Wire-only acknowledgement request and envelope contracts."""

from __future__ import annotations

import base64
import inspect
import json
from dataclasses import fields
from pathlib import Path
from unittest import mock

import pytest

from brigade import evidence_ack_protocol as ack
from brigade import evidence_package_snapshot


REQUEST_BYTES = b'{"artifact":{"receiptSha256":"1111111111111111111111111111111111111111111111111111111111111111","representation":"manifest-bytes","schema":"brigade.evidence_package.v1","sha256":"0000000000000000000000000000000000000000000000000000000000000000","verifyRunId":"verify-1"},"createdAt":"2026-09-08T00:00:00Z","destinationId":"archive-a","expiresAt":"2026-09-09T00:00:00Z","nonce":"22222222222222222222222222222222","schema":"brigade.evidence_ack_request.v1"}'
REQUEST_ID = "df95b4a17011964e625f3e37c570c19eb6cd5ca6df506895e1bf9dfee5ea9e00"
STATEMENT_BYTES = b'{"_type":"https://in-toto.io/Statement/v1","predicate":{"acknowledgedAt":"2026-09-08T01:00:00Z","destinationReference":"record-1","result":"received","schemaVersion":1},"predicateType":"https://brigade.dev/attestation/evidence-ack/v1","subject":[{"digest":{"sha256":"df95b4a17011964e625f3e37c570c19eb6cd5ca6df506895e1bf9dfee5ea9e00"},"name":"evidence-ack-request"}]}'
SIGNATURE_ARMOR = b"-----BEGIN SSH SIGNATURE-----\nYQ==\n-----END SSH SIGNATURE-----\n"
ENVELOPE_BYTES = b'{"brigade":{"namespace":"brigade-evidence-ack","profile":"brigade.sshsig-dsse.v1"},"payload":"eyJfdHlwZSI6Imh0dHBzOi8vaW4tdG90by5pby9TdGF0ZW1lbnQvdjEiLCJwcmVkaWNhdGUiOnsiYWNrbm93bGVkZ2VkQXQiOiIyMDI2LTA5LTA4VDAxOjAwOjAwWiIsImRlc3RpbmF0aW9uUmVmZXJlbmNlIjoicmVjb3JkLTEiLCJyZXN1bHQiOiJyZWNlaXZlZCIsInNjaGVtYVZlcnNpb24iOjF9LCJwcmVkaWNhdGVUeXBlIjoiaHR0cHM6Ly9icmlnYWRlLmRldi9hdHRlc3RhdGlvbi9ldmlkZW5jZS1hY2svdjEiLCJzdWJqZWN0IjpbeyJkaWdlc3QiOnsic2hhMjU2IjoiZGY5NWI0YTE3MDExOTY0ZTYyNWYzZTM3YzU3MGMxOWViNmNkNWNhNmRmNTA2ODk1ZTFiZjlkZmVlNWVhOWUwMCJ9LCJuYW1lIjoiZXZpZGVuY2UtYWNrLXJlcXVlc3QifV19","payloadType":"application/vnd.in-toto+json","signatures":[{"keyid":"SHA256:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA","sig":"LS0tLS1CRUdJTiBTU0ggU0lHTkFUVVJFLS0tLS0KWVE9PQotLS0tLUVORCBTU0ggU0lHTkFUVVJFLS0tLS0K"}]}'
ENVELOPE_ID = "4f314abe753eb3221c225710f3d5a0cb2325eb00d8b5b3421181ce4d1f61e050"


def _envelope(*, armor: bytes = SIGNATURE_ARMOR, payload: bytes = STATEMENT_BYTES) -> bytes:
    value = json.loads(ENVELOPE_BYTES)
    value["payload"] = base64.b64encode(payload).decode("ascii")
    value["signatures"][0]["sig"] = base64.b64encode(armor).decode("ascii")
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _request() -> dict[str, object]:
    return json.loads(REQUEST_BYTES)


def _encode(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def _statement(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _build_args() -> dict[str, str]:
    return {
        "nonce": "2" * 32,
        "destination_id": "archive-a",
        "manifest_sha256": "0" * 64,
        "verify_run_id": "verify-1",
        "receipt_sha256": "1" * 64,
        "created_at": "2026-09-08T00:00:00Z",
        "expires_at": "2026-09-09T00:00:00Z",
    }


def _assert_error(code: str, call, *args, **kwargs) -> None:
    with pytest.raises(ack.AckWireError, match=rf"^ack wire: {code}$") as raised:
        call(*args, **kwargs)
    assert raised.value.code == code


def test_request_literal_vector_and_factory_agree() -> None:
    parsed = ack.parse_request(REQUEST_BYTES)
    built = ack.build_request(
        nonce="22222222222222222222222222222222",
        destination_id="archive-a",
        manifest_sha256="0" * 64,
        verify_run_id="verify-1",
        receipt_sha256="1" * 64,
        created_at="2026-09-08T00:00:00Z",
        expires_at="2026-09-09T00:00:00Z",
    )
    assert parsed.canonical_bytes == REQUEST_BYTES
    assert parsed.request_id == REQUEST_ID
    assert built == parsed
    assert built.artifact.manifest_sha256 == "0" * 64
    with pytest.raises(AttributeError):
        parsed.destination_id = "other"  # type: ignore[misc]
    assert not hasattr(parsed, "__dict__")
    assert not hasattr(parsed.artifact, "__dict__")


def test_envelope_literal_vector_is_syntax_only() -> None:
    parsed = ack.parse_envelope(ENVELOPE_BYTES)
    assert parsed.canonical_bytes == ENVELOPE_BYTES
    assert parsed.envelope_id == ENVELOPE_ID
    assert parsed.statement_bytes == STATEMENT_BYTES
    assert parsed.signature_armor == SIGNATURE_ARMOR
    assert parsed.claim.result == "received"
    assert parsed.claim.request_id == REQUEST_ID
    assert not hasattr(parsed, "__dict__")
    assert not hasattr(parsed.claim, "__dict__")


def test_exact_input_types_reject_subclasses_before_hooks() -> None:
    class BadBytes(bytes):
        def __len__(self):  # pragma: no cover - must not be called
            raise AssertionError("len")

    class BadStr(str):
        def encode(self, *args, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("encode")

    _assert_error("type", ack.parse_request, BadBytes(REQUEST_BYTES))
    _assert_error("type", ack.parse_envelope, bytearray(ENVELOPE_BYTES))
    args = dict(
        nonce=BadStr("2" * 32),
        destination_id="archive-a",
        manifest_sha256="0" * 64,
        verify_run_id="verify-1",
        receipt_sha256="1" * 64,
        created_at="2026-09-08T00:00:00Z",
        expires_at="2026-09-09T00:00:00Z",
    )
    _assert_error("type", ack.build_request, **args)


def test_raw_caps_refuse_before_json_parser() -> None:
    assert ack.parse_request(REQUEST_BYTES + b" " * (ack.MAX_REQUEST_BYTES - len(REQUEST_BYTES))) == ack.parse_request(
        REQUEST_BYTES
    )
    assert ack.parse_envelope(
        ENVELOPE_BYTES + b" " * (ack.MAX_ENVELOPE_BYTES - len(ENVELOPE_BYTES))
    ) == ack.parse_envelope(ENVELOPE_BYTES)
    with mock.patch.object(ack.attestation_input, "strict_json_loads") as loads:
        _assert_error("size", ack.parse_request, b" " * (ack.MAX_REQUEST_BYTES + 1))
    loads.assert_not_called()
    with mock.patch.object(ack.attestation_input, "strict_json_loads") as loads:
        _assert_error("size", ack.parse_envelope, b" " * (ack.MAX_ENVELOPE_BYTES + 1))
    loads.assert_not_called()


def test_factory_generated_output_cap() -> None:
    run_id = "r" * (len("verify-1") + ack.MAX_REQUEST_BYTES - len(REQUEST_BYTES))
    request = ack.build_request(
        nonce="2" * 32,
        destination_id="archive-a",
        manifest_sha256="0" * 64,
        verify_run_id=run_id,
        receipt_sha256="1" * 64,
        created_at="2026-09-08T00:00:00Z",
        expires_at="2026-09-09T00:00:00Z",
    )
    assert len(request.canonical_bytes) == ack.MAX_REQUEST_BYTES
    _assert_error(
        "size",
        ack.build_request,
        nonce="2" * 32,
        destination_id="archive-a",
        manifest_sha256="0" * 64,
        verify_run_id=run_id + "r",
        receipt_sha256="1" * 64,
        created_at="2026-09-08T00:00:00Z",
        expires_at="2026-09-09T00:00:00Z",
    )


def test_base64_caps_and_canonical_padding() -> None:
    value = json.loads(ENVELOPE_BYTES)
    value["payload"] += "="
    _assert_error("encoding", ack.parse_envelope, json.dumps(value).encode())
    value = json.loads(ENVELOPE_BYTES)
    value["payload"] = "A" * (4 * ((ack.MAX_STATEMENT_BYTES + 2) // 3) + 1)
    with mock.patch.object(ack.base64, "b64decode", wraps=ack.base64.b64decode) as decode:
        _assert_error("size", ack.parse_envelope, json.dumps(value).encode())
    decode.assert_not_called()
    value = json.loads(ENVELOPE_BYTES)
    value["signatures"][0]["sig"] = "A" * (4 * ((ack.MAX_SIGNATURE_BYTES + 2) // 3) + 1)
    with mock.patch.object(ack.base64, "b64decode", wraps=ack.base64.b64decode) as decode:
        _assert_error("size", ack.parse_envelope, json.dumps(value).encode())
    assert decode.call_count == 1


def test_decoded_base64_caps_are_checked_after_only_the_admitted_decode() -> None:
    value = json.loads(ENVELOPE_BYTES)
    value["payload"] = base64.b64encode(b"x" * (ack.MAX_STATEMENT_BYTES + 1)).decode()
    with mock.patch.object(ack.base64, "b64decode", wraps=ack.base64.b64decode) as decode:
        _assert_error("size", ack.parse_envelope, json.dumps(value).encode())
    assert decode.call_count == 1
    value = json.loads(ENVELOPE_BYTES)
    value["signatures"][0]["sig"] = base64.b64encode(b"x" * (ack.MAX_SIGNATURE_BYTES + 1)).decode()
    with mock.patch.object(ack.base64, "b64decode", wraps=ack.base64.b64decode) as decode:
        _assert_error("size", ack.parse_envelope, json.dumps(value).encode())
    assert decode.call_count == 2


def test_decoded_payload_cap_precedes_parsing_but_not_canonical_comparison() -> None:
    padded = STATEMENT_BYTES + b" " * (ack.MAX_STATEMENT_BYTES - len(STATEMENT_BYTES))
    _assert_error("canonical", ack.parse_envelope, _envelope(payload=padded))
    _assert_error("size", ack.parse_envelope, _envelope(payload=padded + b" "))


def test_armor_terminal_lf_forms_preserve_identity() -> None:
    no_lf = SIGNATURE_ARMOR.rstrip(b"\n")
    first = ack.parse_envelope(_envelope(armor=no_lf))
    second = ack.parse_envelope(_envelope(armor=SIGNATURE_ARMOR))
    assert first.signature_armor == no_lf
    assert second.signature_armor == SIGNATURE_ARMOR
    assert first.envelope_id != second.envelope_id
    _assert_error("encoding", ack.parse_envelope, _envelope(armor=SIGNATURE_ARMOR + b"\n"))


def test_armor_maximum_body_and_outer_cap() -> None:
    body = base64.b64encode(b"a" * 49107)
    armor = b"-----BEGIN SSH SIGNATURE-----\n" + body[:1] + b"\n" + body[1:] + b"\n-----END SSH SIGNATURE-----\n"
    assert len(armor) == ack.MAX_SIGNATURE_BYTES
    assert ack.parse_envelope(_envelope(armor=armor)).signature_armor == armor
    _assert_error("size", ack.parse_envelope, _envelope(armor=armor + b"x"))


def test_closed_schemas_and_error_precedence() -> None:
    request = _request()
    request["unknown"] = True
    _assert_error("shape", ack.parse_request, json.dumps(request).encode())
    request = _request()
    request["nonce"] = "z" * 32
    _assert_error("field", ack.parse_request, json.dumps(request).encode())
    request = _request()
    request["destinationId"] = "x" * 129
    _assert_error("size", ack.parse_request, json.dumps(request).encode())
    value = json.loads(ENVELOPE_BYTES)
    value["signatures"] = []
    _assert_error("shape", ack.parse_envelope, json.dumps(value).encode())
    statement = json.loads(STATEMENT_BYTES)
    statement["predicate"]["schemaVersion"] = True
    _assert_error(
        "shape",
        ack.parse_envelope,
        _envelope(payload=json.dumps(statement, sort_keys=True, separators=(",", ":")).encode()),
    )
    statement = json.loads(STATEMENT_BYTES)
    statement["subject"][0]["digest"]["sha256"] = "z" * 64
    statement["predicate"]["schemaVersion"] = True
    _assert_error(
        "shape",
        ack.parse_envelope,
        _envelope(payload=json.dumps(statement, sort_keys=True, separators=(",", ":")).encode()),
    )
    _assert_error("json", ack.parse_request, b'{"schema":"x","schema":"x"}')


def test_translated_errors_suppress_input_context() -> None:
    with pytest.raises(ack.AckWireError) as raised:
        ack.parse_request(b"\xff")
    assert raised.value.code == "json"
    assert raised.value.__suppress_context__ is True


def test_overlong_fixed_fields_are_size_before_lexical_checks() -> None:
    request = _request()
    request["nonce"] = "z" * 33
    _assert_error("size", ack.parse_request, json.dumps(request).encode())
    request = _request()
    request["artifact"]["sha256"] = "z" * 65
    _assert_error("size", ack.parse_request, json.dumps(request).encode())
    request = _request()
    request["createdAt"] = "x" * 21
    _assert_error("size", ack.parse_request, json.dumps(request).encode())
    value = json.loads(ENVELOPE_BYTES)
    value["signatures"][0]["keyid"] = "x" * 51
    _assert_error("size", ack.parse_envelope, json.dumps(value).encode())


def test_parser_has_no_external_effects() -> None:
    with (
        mock.patch("subprocess.run", side_effect=AssertionError("subprocess")),
        mock.patch("builtins.open", side_effect=AssertionError("open")),
        mock.patch("pathlib.Path.open", side_effect=AssertionError("path open")),
        mock.patch("pathlib.Path.read_bytes", side_effect=AssertionError("read bytes")),
        mock.patch("pathlib.Path.read_text", side_effect=AssertionError("read text")),
        mock.patch.object(ack.attestation, "extract_keyid_from_sshsig", side_effect=AssertionError("key id")),
        mock.patch.object(ack.attestation, "verify_attestation", side_effect=AssertionError("verify")),
        mock.patch.object(evidence_package_snapshot, "snapshot_package", side_effect=AssertionError("snapshot")),
    ):
        assert ack.parse_request(REQUEST_BYTES).request_id == REQUEST_ID
        assert ack.parse_envelope(ENVELOPE_BYTES).envelope_id == ENVELOPE_ID


def test_request_field_boundaries_and_temporal_interval() -> None:
    request = _request()
    request["createdAt"] = "2026-02-29T00:00:00Z"
    request["expiresAt"] = "2026-02-28T00:00:00Z"
    _assert_error("field", ack.parse_request, json.dumps(request).encode())
    request = _request()
    request["artifact"]["verifyRunId"] = "."
    _assert_error("field", ack.parse_request, json.dumps(request).encode())
    request = _request()
    request["createdAt"] = request["expiresAt"]
    _assert_error("field", ack.parse_request, json.dumps(request).encode())


def test_rejected_envelope_and_noncanonical_statement_are_distinct_wire_cases() -> None:
    statement = json.loads(STATEMENT_BYTES)
    statement["predicate"]["result"] = "rejected"
    rejected = ack.parse_envelope(
        _envelope(payload=json.dumps(statement, sort_keys=True, separators=(",", ":")).encode())
    )
    assert rejected.claim.result == "rejected"
    _assert_error("canonical", ack.parse_envelope, _envelope(payload=b" " + STATEMENT_BYTES))


def test_destination_id_character_limit_precedes_ascii_rejection() -> None:
    for value in ("é" * 65, "é" * 128):
        request = _request()
        request["destinationId"] = value
        _assert_error("field", ack.parse_request, _encode(request))
    request = _request()
    request["destinationId"] = "a" * 129
    _assert_error("size", ack.parse_request, _encode(request))


@pytest.mark.parametrize(
    ("argument", "value"),
    [
        ("nonce", "2" * 31 + "\ud800"),
        ("manifest_sha256", "0" * 63 + "\ud800"),
        ("receipt_sha256", "1" * 63 + "\ud800"),
        ("created_at", "2026-09-08T00:00:0\ud800"),
        ("expires_at", "2026-09-09T00:00:0\ud800"),
    ],
)
def test_builder_invalid_unicode_scalar_is_json(argument: str, value: str) -> None:
    args = _build_args()
    args[argument] = value
    _assert_error("json", ack.build_request, **args)


def test_error_constructor_enforces_closed_exact_code_set() -> None:
    class Hostile(str):
        def __len__(self):  # pragma: no cover - must not be called
            raise AssertionError("len")

        def __str__(self):  # pragma: no cover - must not be called
            raise AssertionError("str")

        def __hash__(self):  # pragma: no cover - must not be called
            raise AssertionError("hash")

    for code in ("", "unknown", "x" * 10, Hostile("field"), None):
        with pytest.raises(ValueError, match="^invalid acknowledgement wire error code$"):
            ack.AckWireError(code)  # type: ignore[arg-type]
    error = ack.AckWireError("canonical")
    assert str(error) == "ack wire: canonical"
    assert error.code == "canonical"
    with pytest.raises(AttributeError):
        error.code = "field"  # type: ignore[misc]


@pytest.mark.parametrize(
    "parser, value",
    [
        (ack.parse_request, bytearray(REQUEST_BYTES)),
        (ack.parse_request, memoryview(REQUEST_BYTES)),
        (ack.parse_request, Path("wire.json")),
        (ack.parse_request, "wire"),
        (ack.parse_request, (REQUEST_BYTES,)),
        (ack.parse_request, [REQUEST_BYTES]),
        (ack.parse_request, (item for item in [REQUEST_BYTES])),
        (ack.parse_request, None),
        (ack.parse_envelope, bytearray(ENVELOPE_BYTES)),
        (ack.parse_envelope, memoryview(ENVELOPE_BYTES)),
        (ack.parse_envelope, Path("wire.json")),
        (ack.parse_envelope, "wire"),
        (ack.parse_envelope, (ENVELOPE_BYTES,)),
        (ack.parse_envelope, [ENVELOPE_BYTES]),
        (ack.parse_envelope, (item for item in [ENVELOPE_BYTES])),
        (ack.parse_envelope, None),
    ],
)
def test_parsers_require_exact_bytes(parser, value: object) -> None:
    _assert_error("type", parser, value)


def test_builder_rejects_each_hostile_string_subclass_before_hooks() -> None:
    class Hostile(str):
        def __len__(self):  # pragma: no cover - must not be called
            raise AssertionError("len")

        def encode(self, *args, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("encode")

        def __str__(self):  # pragma: no cover - must not be called
            raise AssertionError("str")

    for argument, value in _build_args().items():
        args = _build_args()
        args[argument] = Hostile(value)
        _assert_error("type", ack.build_request, **args)


def test_public_shape_and_retained_values_are_closed_and_immutable() -> None:
    request = ack.parse_request(REQUEST_BYTES)
    envelope = ack.parse_envelope(ENVELOPE_BYTES)
    assert tuple(inspect.signature(ack.build_request).parameters) == tuple(_build_args())
    assert tuple(inspect.signature(ack.parse_request).parameters) == ("raw",)
    assert tuple(inspect.signature(ack.parse_envelope).parameters) == ("raw",)
    expected = {
        ack.ArtifactClaim: ("manifest_sha256", "verify_run_id", "receipt_sha256"),
        ack.AckRequest: (
            "nonce",
            "destination_id",
            "artifact",
            "created_at",
            "expires_at",
            "canonical_bytes",
            "request_id",
        ),
        ack.ResponseClaim: ("request_id", "result", "destination_reference", "acknowledged_at"),
        ack.AckEnvelope: (
            "claim",
            "keyid_hint",
            "statement_bytes",
            "signature_armor",
            "canonical_bytes",
            "envelope_id",
        ),
    }
    for kind, names in expected.items():
        assert tuple(field.name for field in fields(kind)) == names
    for value in (request, request.artifact, envelope, envelope.claim):
        assert not hasattr(value, "__dict__")
        with pytest.raises((AttributeError, TypeError)):
            setattr(value, fields(type(value))[0].name, None)
    retained = (
        request.nonce,
        request.destination_id,
        request.artifact.manifest_sha256,
        request.artifact.verify_run_id,
        request.artifact.receipt_sha256,
        request.created_at,
        request.expires_at,
        request.canonical_bytes,
        request.request_id,
        envelope.claim.request_id,
        envelope.claim.result,
        envelope.claim.destination_reference,
        envelope.claim.acknowledged_at,
        envelope.keyid_hint,
        envelope.statement_bytes,
        envelope.signature_armor,
        envelope.canonical_bytes,
        envelope.envelope_id,
    )
    assert all(type(value) in {str, bytes} for value in retained)


@pytest.mark.parametrize("path", [("artifact",), ("artifact", "sha256")])
def test_request_closed_keys_reject_missing_and_unknown(path: tuple[str, ...]) -> None:
    request = _request()
    target = request
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    key = path[-1]
    value = target.pop(key)  # type: ignore[union-attr]
    _assert_error("shape", ack.parse_request, _encode(request))
    target[key] = value  # type: ignore[index]
    target["extra"] = True  # type: ignore[index]
    _assert_error("shape", ack.parse_request, _encode(request))


@pytest.mark.parametrize(
    "path, value",
    [
        (("schema",), "wrong"),
        (("artifact", "schema"), "wrong"),
        (("artifact", "representation"), "wrong"),
        (("nonce",), []),
        (("artifact",), []),
    ],
)
def test_request_constants_and_object_types_are_closed(path: tuple[str, ...], value: object) -> None:
    request = _request()
    target = request
    for part in path[:-1]:
        target = target[part]  # type: ignore[index]
    target[path[-1]] = value  # type: ignore[index]
    _assert_error("shape", ack.parse_request, _encode(request))


@pytest.mark.parametrize("schema_version", [True, 1.0, "1", None, 0, 2])
def test_statement_schema_version_requires_exact_integer_one(schema_version: object) -> None:
    statement = json.loads(STATEMENT_BYTES)
    statement["predicate"]["schemaVersion"] = schema_version
    _assert_error("shape", ack.parse_envelope, _envelope(payload=_statement(statement)))


@pytest.mark.parametrize(
    "field, values",
    [
        ("nonce", ["2" * 31, "2" * 33, "A" * 32, "z" * 32]),
        ("destinationId", ["", "-bad", "bad!", "a" * 129]),
        ("sha256", ["0" * 63, "0" * 65, "A" * 64, "z" * 64]),
        ("receiptSha256", ["1" * 63, "1" * 65, "A" * 64, "z" * 64]),
        ("verifyRunId", [".", "..", "bad space", "é"]),
    ],
)
def test_request_field_boundaries(field: str, values: list[str]) -> None:
    for value in values:
        request = _request()
        target = request["artifact"] if field in {"sha256", "receiptSha256", "verifyRunId"} else request
        target[field] = value  # type: ignore[index]
        _assert_error("size" if len(value) in {33, 65, 129} else "field", ack.parse_request, _encode(request))


@pytest.mark.parametrize("variant", ["missing-padding", "whitespace", "urlsafe", "non-ascii", "pad-bits"])
def test_payload_and_signature_base64_variants_are_encoding(variant: str) -> None:
    statement = json.loads(STATEMENT_BYTES)
    statement["predicate"]["destinationReference"] = "r"
    payload_base = json.loads(_envelope(payload=_statement(statement)))
    signature_base = json.loads(_envelope(armor=SIGNATURE_ARMOR.rstrip(b"\n")))
    for field, value in (("payload", payload_base), ("sig", signature_base)):
        location = value if field == "payload" else value["signatures"][0]
        original = location[field]
        location[field] = {
            "missing-padding": original.rstrip("="),
            "whitespace": original + " ",
            "urlsafe": "-w==",
            "non-ascii": "é",
            "pad-bits": "YR==",
        }[variant]
        _assert_error("encoding", ack.parse_envelope, _encode(value))


@pytest.mark.parametrize(
    "armor",
    [
        b"",
        b"raw",
        b"x" + SIGNATURE_ARMOR,
        SIGNATURE_ARMOR + b"x",
        SIGNATURE_ARMOR.replace(b"\n", b"\r\n"),
        SIGNATURE_ARMOR.replace(b"YQ==", b""),
        SIGNATURE_ARMOR.replace(b"YQ==", b"YQ"),
        SIGNATURE_ARMOR.replace(b"YQ==", b"-w=="),
        SIGNATURE_ARMOR.replace(b"YQ==", b"YQ==\n"),
    ],
)
def test_armor_variants_are_encoding(armor: bytes) -> None:
    _assert_error("encoding", ack.parse_envelope, _envelope(armor=armor))


def test_transport_canonicalization_and_claim_mutations_change_ids() -> None:
    request = _request()
    reordered = _encode({"nonce": request["nonce"], **request})
    assert ack.parse_request(reordered) == ack.parse_request(REQUEST_BYTES)
    for field in ("sha256", "verifyRunId", "receiptSha256"):
        changed = _request()
        changed["artifact"][field] = "2" * 64 if field != "verifyRunId" else "verify-2"
        assert ack.parse_request(_encode(changed)).request_id != REQUEST_ID
    original = ack.parse_envelope(ENVELOPE_BYTES)
    statement = json.loads(STATEMENT_BYTES)
    statement["predicate"]["destinationReference"] = "café"
    unicode_envelope = ack.parse_envelope(_envelope(payload=_statement(statement)))
    assert b"caf\\u00e9" in unicode_envelope.statement_bytes
    assert unicode_envelope.envelope_id != original.envelope_id
    for key, value in (
        ("result", "rejected"),
        ("destinationReference", "record-2"),
        ("acknowledgedAt", "2026-09-08T02:00:00Z"),
    ):
        changed = json.loads(STATEMENT_BYTES)
        changed["predicate"][key] = value
        assert ack.parse_envelope(_envelope(payload=_statement(changed))).envelope_id != ENVELOPE_ID


def _envelope_value(*, statement: dict[str, object] | None = None) -> dict[str, object]:
    value = json.loads(ENVELOPE_BYTES)
    if statement is not None:
        value["payload"] = base64.b64encode(_statement(statement)).decode("ascii")
    return value


def _statement_value() -> dict[str, object]:
    return json.loads(STATEMENT_BYTES)


def test_public_operations_and_hostile_envelope_bytes_are_closed() -> None:
    class Hostile(bytes):
        def __len__(self):  # pragma: no cover - must not be called
            raise AssertionError("len")

    public_functions = {
        name
        for name, value in inspect.getmembers(ack, inspect.isfunction)
        if value.__module__ == ack.__name__ and not name.startswith("_")
    }
    assert public_functions == {"build_request", "parse_request", "parse_envelope"}
    _assert_error("type", ack.parse_envelope, Hostile(ENVELOPE_BYTES))


def test_literal_vectors_expose_every_result_field() -> None:
    request = ack.parse_request(REQUEST_BYTES)
    envelope = ack.parse_envelope(ENVELOPE_BYTES)
    assert (
        request.nonce,
        request.destination_id,
        request.artifact.manifest_sha256,
        request.artifact.verify_run_id,
        request.artifact.receipt_sha256,
        request.created_at,
        request.expires_at,
        request.canonical_bytes,
        request.request_id,
    ) == (
        "2" * 32,
        "archive-a",
        "0" * 64,
        "verify-1",
        "1" * 64,
        "2026-09-08T00:00:00Z",
        "2026-09-09T00:00:00Z",
        REQUEST_BYTES,
        REQUEST_ID,
    )
    assert (
        envelope.claim.request_id,
        envelope.claim.result,
        envelope.claim.destination_reference,
        envelope.claim.acknowledged_at,
        envelope.keyid_hint,
        envelope.statement_bytes,
        envelope.signature_armor,
        envelope.canonical_bytes,
        envelope.envelope_id,
    ) == (
        REQUEST_ID,
        "received",
        "record-1",
        "2026-09-08T01:00:00Z",
        "SHA256:" + "A" * 43,
        STATEMENT_BYTES,
        SIGNATURE_ARMOR,
        ENVELOPE_BYTES,
        ENVELOPE_ID,
    )


@pytest.mark.parametrize(
    ("level", "keys"),
    [
        ("request", ("schema", "nonce", "destinationId", "artifact", "createdAt", "expiresAt")),
        ("artifact", ("schema", "representation", "sha256", "verifyRunId", "receiptSha256")),
        ("envelope", ("payloadType", "payload", "signatures", "brigade")),
        ("signature", ("keyid", "sig")),
        ("brigade", ("profile", "namespace")),
        ("statement", ("_type", "subject", "predicateType", "predicate")),
        ("subject", ("name", "digest")),
        ("digest", ("sha256",)),
        ("predicate", ("schemaVersion", "result", "destinationReference", "acknowledgedAt")),
    ],
)
def test_every_closed_object_rejects_each_missing_and_unknown_key(level: str, keys: tuple[str, ...]) -> None:
    def wire(target: dict[str, object], statement: dict[str, object] | None) -> bytes:
        if level in {"request", "artifact"}:
            return _encode(target)
        if level in {"envelope", "signature", "brigade"}:
            return _encode(target)
        assert statement is not None
        return _envelope(payload=_statement(statement))

    for extra in (False, True):
        for key in keys:
            request = _request()
            envelope = _envelope_value()
            statement = _statement_value()
            targets = {
                "request": request,
                "artifact": request["artifact"],
                "envelope": envelope,
                "signature": envelope["signatures"][0],
                "brigade": envelope["brigade"],
                "statement": statement,
                "subject": statement["subject"][0],
                "digest": statement["subject"][0]["digest"],
                "predicate": statement["predicate"],
            }
            target = targets[level]
            if extra:
                target["unexpected"] = True
            else:
                target.pop(key)
            raw = wire(request if level in {"request", "artifact"} else envelope, statement)
            parser = ack.parse_request if level in {"request", "artifact"} else ack.parse_envelope
            _assert_error("shape", parser, raw)


@pytest.mark.parametrize(
    ("level", "wrong_values"),
    [
        ("request", ([], "scalar")),
        ("artifact", ([], "scalar")),
        ("envelope", ([], "scalar")),
        ("signatures", ({}, "scalar")),
        ("signature", ([], "scalar")),
        ("brigade", ([], "scalar")),
        ("statement", ([], "scalar")),
        ("subject", ({}, "scalar")),
        ("subject_item", ([], "scalar")),
        ("digest", ([], "scalar")),
        ("predicate", ([], "scalar")),
    ],
)
def test_every_schema_level_rejects_wrong_container_and_scalar(level: str, wrong_values: tuple[object, object]) -> None:
    for wrong in wrong_values:
        request = _request()
        envelope = _envelope_value()
        statement = _statement_value()
        if level == "request":
            _assert_error("shape", ack.parse_request, _encode(wrong))
            continue
        if level == "artifact":
            request["artifact"] = wrong
            _assert_error("shape", ack.parse_request, _encode(request))
            continue
        if level == "envelope":
            _assert_error("shape", ack.parse_envelope, _encode(wrong))
            continue
        if level == "signatures":
            envelope["signatures"] = wrong
        elif level == "signature":
            envelope["signatures"] = [wrong]
        elif level == "brigade":
            envelope["brigade"] = wrong
        elif level == "statement":
            _assert_error("shape", ack.parse_envelope, _envelope(payload=_encode(wrong)))
            continue
        elif level == "subject":
            statement["subject"] = wrong
        elif level == "subject_item":
            statement["subject"] = [wrong]
        elif level == "digest":
            statement["subject"][0]["digest"] = wrong
        else:
            statement["predicate"] = wrong
        raw = (
            _encode(envelope)
            if level in {"signatures", "signature", "brigade"}
            else _envelope(payload=_statement(statement))
        )
        _assert_error("shape", ack.parse_envelope, raw)


@pytest.mark.parametrize("key", ["signatures", "subject"])
@pytest.mark.parametrize("count", [0, 2])
def test_signature_and_subject_cardinality_are_exactly_one(key: str, count: int) -> None:
    if key == "signatures":
        envelope = _envelope_value()
        signature = envelope["signatures"][0]
        envelope["signatures"] = [signature] * count
        _assert_error("shape", ack.parse_envelope, _encode(envelope))
        return
    statement = _statement_value()
    subject = statement["subject"][0]
    statement["subject"] = [subject] * count
    _assert_error("shape", ack.parse_envelope, _envelope(payload=_statement(statement)))


@pytest.mark.parametrize(
    "location",
    [
        "request.schema",
        "artifact.schema",
        "artifact.representation",
        "envelope.payloadType",
        "brigade.profile",
        "brigade.namespace",
        "statement._type",
        "statement.predicateType",
        "subject.name",
    ],
)
def test_every_fixed_constant_is_rejected_when_changed(location: str) -> None:
    request = _request()
    envelope = _envelope_value()
    statement = _statement_value()
    targets = {
        "request.schema": request,
        "artifact.schema": request["artifact"],
        "artifact.representation": request["artifact"],
        "envelope.payloadType": envelope,
        "brigade.profile": envelope["brigade"],
        "brigade.namespace": envelope["brigade"],
        "statement._type": statement,
        "statement.predicateType": statement,
        "subject.name": statement["subject"][0],
    }
    target = targets[location]
    target[location.rsplit(".", 1)[-1]] = "wrong"
    if location.startswith(("request", "artifact")):
        _assert_error("shape", ack.parse_request, _encode(request))
    elif location.startswith(("envelope", "brigade")):
        _assert_error("shape", ack.parse_envelope, _encode(envelope))
    else:
        _assert_error("shape", ack.parse_envelope, _envelope(payload=_statement(statement)))


def test_selected_validation_order_cases_are_isolated() -> None:
    request = _request()
    request["nonce"] = "z" * 32
    request["destinationId"] = "a" * 129
    _assert_error("field", ack.parse_request, _encode(request))
    request = _request()
    request["destinationId"] = "a" * 129
    request["artifact"]["schema"] = "wrong"
    _assert_error("size", ack.parse_request, _encode(request))
    envelope = _envelope_value()
    envelope["signatures"] = []
    envelope["brigade"]["profile"] = "wrong"
    _assert_error("shape", ack.parse_envelope, _encode(envelope))
    envelope = _envelope_value()
    envelope["brigade"]["profile"] = "wrong"
    envelope["signatures"][0]["keyid"] = "wrong"
    _assert_error("shape", ack.parse_envelope, _encode(envelope))
    envelope = _envelope_value()
    envelope["signatures"][0]["keyid"] = "wrong"
    envelope["payload"] = "="
    _assert_error("field", ack.parse_envelope, _encode(envelope))
    statement = _statement_value()
    statement["predicate"].pop("result")
    statement["subject"][0]["digest"]["sha256"] = "z" * 64
    _assert_error("shape", ack.parse_envelope, _envelope(payload=_statement(statement)))


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b'{"nested":{"x":1,"x":2}}',
        b"\xff",
        b'{"scalar":"\\ud800"}',
        b'{"nonfinite":NaN}',
        b"[" * 65 + b"0" + b"]" * 65,
    ],
)
def test_outer_json_failures_translate_to_json(raw: bytes) -> None:
    _assert_error("json", ack.parse_request, raw)


@pytest.mark.parametrize("payload", [b"\xff", b'{"scalar":"\\ud800"}', b'{"nonfinite":NaN}'])
def test_decoded_statement_json_failures_translate_to_json(payload: bytes) -> None:
    _assert_error("json", ack.parse_envelope, _envelope(payload=payload))


def test_inherited_lowered_node_budget_translates_without_claiming_production_reachability() -> None:
    assert ack.attestation_input.MAX_JSON_NODES == 100_000
    with mock.patch.object(ack.attestation_input, "MAX_JSON_NODES", 2):
        _assert_error("json", ack.parse_request, REQUEST_BYTES)


def test_destination_and_keyid_boundaries_cover_character_and_lexical_rules() -> None:
    for destination in ("a", "a" * 128):
        request = _request()
        request["destinationId"] = destination
        assert ack.parse_request(_encode(request)).destination_id == destination
    for keyid in (
        "SHA256:" + "A" * 42,
        "SHA257:" + "A" * 43,
        "SHA256:" + "A" * 42 + "=",
        "SHA256:" + "A" * 42 + "?",
        "SHA256:" + "A" * 42 + "é",
    ):
        envelope = _envelope_value()
        envelope["signatures"][0]["keyid"] = keyid
        _assert_error("field", ack.parse_envelope, _encode(envelope))


@pytest.mark.parametrize(
    ("reference", "code"),
    [
        ("a" * 2048, None),
        ("a" * 2049, "size"),
        ("é" * 1024, None),
        ("é" * 1024 + "a", "size"),
        ("", "field"),
        ("a\x00", "field"),
        ("a\x1f", "field"),
        ("a\x7f", "field"),
        ("a\x80", "field"),
    ],
)
def test_destination_reference_utf8_boundaries_and_controls(reference: str, code: str | None) -> None:
    statement = _statement_value()
    statement["predicate"]["destinationReference"] = reference
    raw = _envelope(payload=_statement(statement))
    if code:
        _assert_error(code, ack.parse_envelope, raw)
    else:
        assert ack.parse_envelope(raw).claim.destination_reference == reference


@pytest.mark.parametrize(
    ("created", "expires", "code"),
    [
        ("0001-01-01T00:00:00Z", "9999-12-31T23:59:59Z", None),
        ("1970-01-01T00:00:00Z", "2099-12-31T23:59:59Z", None),
        ("0000-01-01T00:00:00Z", "2026-09-09T00:00:00Z", "field"),
        ("2026-02-30T00:00:00Z", "2026-09-09T00:00:00Z", "field"),
        ("2026-09-08T00:00:00+00:00", "2026-09-09T00:00:00Z", "size"),
        ("2026-09-08T00:00:00.1Z", "2026-09-09T00:00:00Z", "size"),
        ("2026-09-08T00:00:60Z", "2026-09-09T00:00:00Z", "field"),
        ("2026-09-09T00:00:00Z", "2026-09-08T00:00:00Z", "field"),
    ],
)
def test_request_timestamp_calendar_forms_and_valid_reversed_interval(
    created: str, expires: str, code: str | None
) -> None:
    request = _request()
    request["createdAt"] = created
    request["expiresAt"] = expires
    if code:
        _assert_error(code, ack.parse_request, _encode(request))
    else:
        parsed = ack.parse_request(_encode(request))
        assert (parsed.created_at, parsed.expires_at) == (created, expires)


@pytest.mark.parametrize("value", [None, 1, True, [], {}])
def test_timestamp_wrong_json_types_are_shape(value: object) -> None:
    request = _request()
    request["createdAt"] = value
    _assert_error("shape", ack.parse_request, _encode(request))
    statement = _statement_value()
    statement["predicate"]["acknowledgedAt"] = value
    _assert_error("shape", ack.parse_envelope, _envelope(payload=_statement(statement)))


@pytest.mark.parametrize("result", ["", "accepted", "RECEIVED", None, 1, True, []])
def test_result_rejects_invalid_strings_and_wrong_types(result: object) -> None:
    statement = _statement_value()
    statement["predicate"]["result"] = result
    _assert_error(
        "field" if type(result) is str else "shape", ack.parse_envelope, _envelope(payload=_statement(statement))
    )


def test_signature_excess_padding_and_marker_spaces_are_encoding() -> None:
    envelope = _envelope_value()
    signature = base64.b64encode(SIGNATURE_ARMOR.rstrip(b"\n")).decode("ascii")
    assert signature.endswith("=")
    envelope["signatures"][0]["sig"] = signature + "="
    _assert_error("encoding", ack.parse_envelope, _encode(envelope))
    for armor in (
        SIGNATURE_ARMOR.replace(b"BEGIN", b"BEGAN"),
        SIGNATURE_ARMOR.replace(b"END SSH", b"FIN SSH"),
        b" " + SIGNATURE_ARMOR,
        SIGNATURE_ARMOR + b" ",
        SIGNATURE_ARMOR.replace(b"-----\n", b"----- \n", 1),
        SIGNATURE_ARMOR.replace(b"\nYQ==", b"\n YQ=="),
        SIGNATURE_ARMOR.replace(b"YQ==\n", b"YQ== \n"),
    ):
        _assert_error("encoding", ack.parse_envelope, _envelope(armor=armor))


def test_valid_internal_armor_wrappings_preserve_bytes_and_change_identity() -> None:
    wrapped = b"-----BEGIN SSH SIGNATURE-----\nY\nQ==\n-----END SSH SIGNATURE-----\n"
    assert b"".join(wrapped.splitlines()[1:-1]) == b"YQ=="
    first = ack.parse_envelope(_envelope(armor=SIGNATURE_ARMOR))
    second = ack.parse_envelope(_envelope(armor=wrapped))
    assert first.signature_armor == SIGNATURE_ARMOR
    assert second.signature_armor == wrapped
    assert first.envelope_id != second.envelope_id


def test_request_and_envelope_transport_forms_share_canonical_identity() -> None:
    request = _request()
    reordered_request = _encode({"expiresAt": request["expiresAt"], **request})
    escaped_request = REQUEST_BYTES.replace(b"archive-a", b"archive\\u002da")
    for raw in (b" \n" + REQUEST_BYTES + b"\t", reordered_request, escaped_request):
        assert ack.parse_request(raw) == ack.parse_request(REQUEST_BYTES)
    envelope = _envelope_value()
    reordered_envelope = _encode(
        {
            "signatures": envelope["signatures"],
            "payload": envelope["payload"],
            "brigade": envelope["brigade"],
            "payloadType": envelope["payloadType"],
        }
    )
    escaped_envelope = ENVELOPE_BYTES.replace(b"application", b"\\u0061pplication")
    for raw in (b"\n " + ENVELOPE_BYTES + b" ", reordered_envelope, escaped_envelope):
        assert ack.parse_envelope(raw) == ack.parse_envelope(ENVELOPE_BYTES)


def test_subject_digest_and_keyid_hint_are_retained_and_change_envelope_identity() -> None:
    original = ack.parse_envelope(ENVELOPE_BYTES)
    statement = _statement_value()
    replacement = "a" * 64
    statement["subject"][0]["digest"]["sha256"] = replacement
    changed_subject = ack.parse_envelope(_envelope(payload=_statement(statement)))
    assert changed_subject.claim.request_id == replacement
    assert changed_subject.envelope_id != original.envelope_id
    envelope = _envelope_value()
    envelope["signatures"][0]["keyid"] = "SHA256:" + "B" * 43
    changed_keyid = ack.parse_envelope(_encode(envelope))
    assert changed_keyid.keyid_hint == "SHA256:" + "B" * 43
    assert changed_keyid.envelope_id != original.envelope_id


def test_all_error_codes_are_exact_and_do_not_disclose_supplied_values() -> None:
    envelope = _envelope_value()
    envelope["payload"] = "="
    cases = (
        ("type", ack.parse_request, (bytearray(REQUEST_BYTES),)),
        ("size", ack.parse_request, (b" " * (ack.MAX_REQUEST_BYTES + 1),)),
        ("json", ack.parse_request, (b'{"secret-input":1,"secret-input":2}',)),
        ("shape", ack.parse_request, (b"{}",)),
        ("field", ack.parse_request, (_encode({**_request(), "nonce": "z" * 32}),)),
        ("encoding", ack.parse_envelope, (_encode(envelope),)),
        ("canonical", ack.parse_envelope, (_envelope(payload=b" " + STATEMENT_BYTES),)),
    )
    for code, parser, args in cases:
        with pytest.raises(ack.AckWireError) as raised:
            parser(*args)
        error = raised.value
        assert (str(error), error.code, error.__cause__, error.__suppress_context__) == (
            f"ack wire: {code}",
            code,
            None,
            True,
        )
        assert "secret-input" not in str(error)


def test_public_surfaces_exclude_authentication_policy_storage_and_association() -> None:
    forbidden = {"authenticated", "authentication", "trusted", "trust", "policy", "eligible", "storage", "association"}
    names = set(inspect.signature(ack.build_request).parameters)
    names.update(inspect.signature(ack.parse_request).parameters)
    names.update(inspect.signature(ack.parse_envelope).parameters)
    for kind in (ack.ArtifactClaim, ack.AckRequest, ack.ResponseClaim, ack.AckEnvelope):
        names.update(field.name for field in fields(kind))
    assert names.isdisjoint(forbidden)
