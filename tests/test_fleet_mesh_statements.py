"""Pure statement protocol boundaries, with independent public known answers."""

import base64
from copy import deepcopy
import importlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from brigade import fleet_mesh_crypto as crypto

FIXTURES = Path(__file__).parent / "fixtures" / "mesh" / "statements"
VECTORS = sorted(p.stem for p in FIXTURES.glob("*.json"))


@pytest.fixture
def statements():
    return importlib.import_module("brigade.fleet_mesh_statements")


def vector(name="membership"):
    return json.loads((FIXTURES / (name + ".json")).read_text())


def raw(value):
    return json.dumps(value, ensure_ascii=True).encode("ascii")


def public(v):
    return base64.urlsafe_b64decode(v["public_key"] + "=")


@pytest.mark.parametrize("name", VECTORS)
def test_independent_known_answers(statements, name):
    v = vector(name)
    value = v["statement"]
    unsigned = {k: x for k, x in value.items() if k != "sig"}
    # Expected bytes are checked in, produced without the implementation.
    assert crypto.canonical(unsigned) == v["unsigned_canonical_ascii"].encode("ascii")
    assert crypto.LABELS[v["kind"]] + crypto.canonical(unsigned) == bytes.fromhex(v["signing_input_hex"])
    assert crypto.canonical(value) == v["signed_canonical_ascii"].encode("ascii")
    assert value["sig"] == v["signature"]
    assert statements.parse_statement(raw(value), expected_kind=v["kind"]) == value
    assert statements.verify_statement(raw(value), expected_kind=v["kind"], public_key=public(v)) == value
    assert statements.sign_statement(unsigned, bytes(range(32)), expected_kind=v["kind"]) == value
    assert statements.statement_digest(raw(value), expected_kind=v["kind"]).hex() == v["signed_sha256_hex"]
    reformatted = json.dumps(dict(reversed(list(value.items()))), indent=3).encode("ascii")
    assert statements.statement_digest(reformatted, expected_kind=v["kind"]).hex() == v["signed_sha256_hex"]
    assert statements.verify_statement(reformatted, expected_kind=v["kind"], public_key=public(v)) == value
    assert statements.match_public_key_fingerprint(public(v), v["public_key_fingerprint"]) is None


@pytest.mark.parametrize("kind", ["membership", "manifest", "revocation", "hub-identity"])
def test_tampering_wrong_key_and_cross_domain(statements, kind):
    v = vector(kind)
    value = deepcopy(v["statement"])
    value["seq"] += 1
    with pytest.raises(crypto.MeshError, match="^statement_signature_invalid$"):
        statements.verify_statement(raw(value), expected_kind=kind, public_key=public(v))
    with pytest.raises(
        crypto.MeshError,
        match="^statement_signer_mismatch$" if kind == "hub-identity" else "^statement_signature_invalid$",
    ):
        statements.verify_statement(raw(v["statement"]), expected_kind=kind, public_key=bytes(32))
    unsigned = {k: x for k, x in v["statement"].items() if k != "sig"}
    wrong_label = "manifest" if kind != "manifest" else "membership"
    value = dict(v["statement"], sig=crypto.sign(unsigned, bytes(range(32)), label=wrong_label))
    with pytest.raises(crypto.MeshError, match="^statement_signature_invalid$"):
        statements.verify_statement(raw(value), expected_kind=kind, public_key=public(v))


def test_identity_signing_refuses_a_different_private_key_without_mutation(statements):
    unsigned = {k: x for k, x in vector("hub-identity")["statement"].items() if k != "sig"}
    before = deepcopy(unsigned)
    with pytest.raises(crypto.MeshError, match="^statement_signer_mismatch$"):
        statements.sign_statement(unsigned, bytes(reversed(range(32))), expected_kind="hub-identity")
    assert unsigned == before


@pytest.mark.parametrize("kind", ["migration", "hub-migration", "unknown", "", None, True, []])
def test_unsupported_kind_is_explicit(statements, kind):
    with pytest.raises(crypto.MeshError, match="^statement_kind_unsupported$"):
        statements.parse_statement(b"{}", expected_kind=kind)
    with pytest.raises(crypto.MeshError, match="^statement_kind_unsupported$"):
        statements.sign_statement({}, bytes(32), expected_kind=kind)
    with pytest.raises(crypto.MeshError, match="^statement_kind_unsupported$"):
        statements.verify_statement(b"{}", expected_kind=kind, public_key=bytes(32))


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"[]",
        b"null",
        b"true",
        b"1",
        b"{} junk",
        b"{",
        b"\xef\xbb\xbf{}",
        b"\x00{}",
        b'{"x":"\xff"}',
        b'{"x":"\\u00e9"}',
        b'{"x":"\\ud800"}',
        b'{"x":"\\udfff"}',
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b'{"x":1.0}',
        b'{"x":null}',
        b'{"x":1,"x":2}',
        b'{"x":[{"nested":1,"nested":2}]}',
        b'{"x":[[[[]]]]}',
        b'{"x":"unterminated}',
    ],
)
def test_malformed_raw_corpus(statements, payload):
    with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
        statements.parse_statement(payload, expected_kind="membership")


def test_size_depth_and_duplicates_before_json_allocation(statements, monkeypatch):
    # The real parser's json decoder must never receive these payloads.
    def forbidden(*args, **kwargs):
        pytest.fail("JSON allocation occurred before the raw/depth bound")

    monkeypatch.setattr(json, "loads", forbidden)
    for kind, bound in [
        ("membership", 2098176),
        ("manifest", 2098176),
        ("revocation", 2098176),
        ("hub-identity", 17408),
    ]:
        with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
            statements.parse_statement(b" " * (bound + 1), expected_kind=kind)
    with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
        statements.parse_statement(b"[[[[[]]]]]", expected_kind="membership")


def test_escaped_duplicate_recursively_and_string_brackets(statements):
    v = vector("manifest")["statement"]
    encoded = raw(v).replace(b'"principal":', b'"princ\\u0069pal":"test-node","principal":')
    with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
        statements.parse_statement(encoded, expected_kind="manifest")
    # Brackets and escaped quotes in strings must not count as containers.
    encoded = raw(vector()["statement"]).replace(b'"test-node"', b'"[[[[[\\""')
    with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
        statements.parse_statement(encoded, expected_kind="membership")


COMMON_BAD = [
    ("v", True),
    ("v", 2),
    ("v", 1.0),
    ("seq", True),
    ("seq", -1),
    ("seq", 9007199254740992),
    ("seq", None),
    ("seq", "1"),
    ("created_at", "now"),
    ("created_at", "2026-02-30T00:00:00Z"),
    ("created_at", "2026-01-01T00:00:60Z"),
    ("created_at", "2026-01-01T00:00:00.1Z"),
    ("created_at", "2026-01-01T00:00:00+24:00"),
    ("sig", "A" * 85 + "B"),
    ("sig", "A" * 86 + "="),
    ("sig", "A" * 85),
    ("extra", "unexpected"),
]


@pytest.mark.parametrize("kind", ["membership", "manifest", "revocation", "hub-identity"])
@pytest.mark.parametrize("field,value", COMMON_BAD)
def test_common_schema_rejections(statements, kind, field, value):
    obj = dict(vector(kind)["statement"])
    obj[field] = value
    with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
        statements.parse_statement(raw(obj), expected_kind=kind)


@pytest.mark.parametrize("kind", ["membership", "manifest", "revocation", "hub-identity"])
def test_missing_fields_and_wrong_expected_schema(statements, kind):
    obj = vector(kind)["statement"]
    for field in obj:
        changed = dict(obj)
        del changed[field]
        with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
            statements.parse_statement(raw(changed), expected_kind=kind)
    with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
        statements.parse_statement(raw(obj), expected_kind="manifest" if kind != "manifest" else "membership")


@pytest.mark.parametrize(
    "members",
    [
        None,
        {},
        [True],
        ["test-node", "agent:test-agent"],
        ["test-node", "test-node"],
        ["agent:A"],
        ["agent:a" + "x" * 63],
        ["-bad"],
        ["bad/slash"],
        ["a" * 129],
        ["test-node\n"],
        ["test-node"] * 4097,
    ],
)
def test_membership_rejections(statements, members):
    obj = dict(vector()["statement"], members=members)
    with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
        statements.parse_statement(raw(obj), expected_kind="membership")


@pytest.mark.parametrize("hub", ["mesh", "h_" + "A" * 21 + "B", "h_" + "A" * 22 + "=", "", True])
def test_manifest_hub_scope_and_encoding(statements, hub):
    obj = dict(vector()["statement"], hub=hub)
    with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
        statements.parse_statement(raw(obj), expected_kind="membership")


def test_manifest_entries_order_uniqueness_and_fingerprint(statements):
    obj = vector("manifest")["statement"]
    entry = obj["keys"][0]
    changes = [
        {"hub": "h_" + "A" * 22},
        {"kid": "k1_bad"},
        {"principal": "agent:Bad"},
        {"fingerprint": entry["fingerprint"].lower()},
        {"fingerprint": "AAAA-" * 12 + "AAAB"},
        {"extra": 1},
    ]
    for change in changes:
        with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
            statements.parse_statement(raw(dict(obj, keys=[dict(entry, **change)])), expected_kind="manifest")
    other = dict(entry, principal="test-z", kid="k1_" + "A" * 22)
    for entries in [
        [entry, entry],
        [other, entry],
        [entry, dict(other, kid=entry["kid"])],
        [entry, dict(other, principal=entry["principal"])],
        [entry] * 4097,
        [None],
    ]:
        with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
            statements.parse_statement(raw(dict(obj, keys=entries)), expected_kind="manifest")
    assert statements.parse_statement(raw(dict(obj, keys=[entry, other])), expected_kind="manifest")["keys"] == [
        entry,
        other,
    ]


def test_revocation_entries_are_cumulative_shape_only(statements):
    obj = vector("revocation")["statement"]
    entry = obj["revocations"][0]
    other = dict(entry, principal="test-z", kid="k1_" + "A" * 22)
    for entries in [
        [entry, entry],
        [other, entry],
        [entry, dict(other, kid=entry["kid"])],
        [dict(entry, compromised_from="now")],
        [dict(entry, extra=1)],
        [None],
        [entry] * 8193,
    ]:
        with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
            statements.parse_statement(raw(dict(obj, revocations=entries)), expected_kind="revocation")
    assert statements.parse_statement(raw(dict(obj, revocations=[entry, other])), expected_kind="revocation")[
        "revocations"
    ] == [entry, other]


@pytest.mark.parametrize(
    "field,value",
    [
        ("require_sealed", 1),
        ("require_sealed", "true"),
        ("max_envelope_bytes", True),
        ("max_envelope_bytes", 0),
        ("max_envelope_bytes", 98305),
        ("envelope_versions", [True]),
        ("envelope_versions", [1.0]),
        ("envelope_versions", [1, 1]),
        ("envelope_versions", [2]),
        ("suites", ["bm2"]),
        ("backend", "other"),
        ("capabilities", ["messages", "messages"]),
        ("capabilities", ["runs", "messages"]),
        ("capabilities", ["Upper"]),
        ("capabilities", ["a" * 65]),
        ("capabilities", ["feature" + str(i).zfill(2) for i in range(33)]),
        ("hub_id", "h_" + "A" * 22),
        ("hub_sig_pub", "A" * 43),
        ("endpoint", "https://EXAMPLE.invalid/"),
    ],
)
def test_identity_profile_rejections(statements, field, value):
    obj = dict(vector("hub-identity")["statement"])
    obj[field] = value
    with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
        statements.parse_statement(raw(obj), expected_kind="hub-identity")


@pytest.mark.parametrize("change", [{"require_sealed": False}, {"max_envelope_bytes": 32769}])
def test_lite_limits(statements, change):
    obj = dict(vector("hub-identity-lite")["statement"], **change)
    with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
        statements.parse_statement(raw(obj), expected_kind="hub-identity")


@pytest.mark.parametrize(
    "url,expected",
    [
        ("HTTPS://EXAMPLE.invalid:443/", "https://example.invalid"),
        ("http://example.invalid:80", "http://example.invalid"),
        ("https://xn--example-9d0b.invalid:8443", "https://xn--example-9d0b.invalid:8443"),
        ("http://192.0.2.1/", "http://192.0.2.1"),
        ("https://[2001:0DB8:0000::1]:443/", "https://[2001:db8::1]"),
        ("https://a", "https://a"),
        ("https://123.example.invalid:65535", "https://123.example.invalid:65535"),
        ("http://0x1.example", "http://0x1.example"),
    ],
)
def test_normalized_origin(statements, url, expected):
    assert statements.normalize_origin(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "//example.invalid",
        "ftp://example.invalid",
        "https://user@example.invalid",
        "https://example.invalid?",
        "https://example.invalid#",
        "https://example.invalid/a",
        "https://example.invalid/%2f",
        "https://example.invalid\\",
        " https://example.invalid",
        "https://example.invalid\n",
        "https://\texample.invalid",
        "https://ex\u00e4mple.invalid",
        "https://example.invalid.",
        "https://-bad.invalid",
        "https://bad-.invalid",
        "https://bad_.invalid",
        "https://a..invalid",
        "https://" + "a" * 64 + ".invalid",
        "https://" + ".".join(["a" * 63] * 4),
        "http://192.000.2.1",
        "http://192.0.2",
        "http://3232235777",
        "http://127.1",
        "http://0x7f.0.0.1",
        "http://0x",
        "http://1.2.3.0x",
        "http://0x.0x",
        "http://example.0x",
        "http://example.123",
        "http://example.0x1",
        "http://example.0XfF",
        "http://256.0.2.1",
        "https://example.invalid:0",
        "https://example.invalid:01",
        "https://example.invalid:65536",
        "https://example.invalid:",
        "https://example.invalid:+80",
        "https://[2001:db8::1%eth0]",
        "https://2001:db8::1",
        "https://[not-ip]",
        "https://[2001:db8::1]evil",
        "https://example.invalid//",
        "https:///",
        "https://example.invalid: 80",
        None,
        True,
    ],
)
def test_origin_rejections(statements, url):
    with pytest.raises(crypto.MeshError, match="^statement_origin_invalid$"):
        statements.normalize_origin(url)


def test_fingerprint_requires_canonical_encoding_and_exact_key(statements):
    v = vector()
    for fp in [v["public_key_fingerprint"].lower(), "AAAA-" * 12 + "AAAB", "AAAA-" * 12 + "AAAA"]:
        with pytest.raises(crypto.MeshError, match="^statement_(malformed|fingerprint_mismatch)$"):
            statements.match_public_key_fingerprint(public(v), fp)


def test_shape_validation_precedes_crypto(statements, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("primitive invoked on invalid statement")

    monkeypatch.setattr(crypto, "sign", forbidden)
    monkeypatch.setattr(crypto, "verify", forbidden)
    with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
        statements.sign_statement({"v": True}, bytes(32), expected_kind="membership")
    with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
        statements.verify_statement(b"{}", expected_kind="membership", public_key=bytes(32))


@pytest.mark.parametrize("kind", ["membership", "hub-identity"])
@pytest.mark.parametrize("code", ["mesh_extra_missing", "mesh_backend_unsupported"])
def test_unavailable_backend_diagnostics_survive(statements, monkeypatch, code, kind):
    def unavailable():
        raise crypto.MeshExtraMissing() if code == "mesh_extra_missing" else crypto.MeshError(code)

    monkeypatch.setattr(crypto, "probe_backend", unavailable)
    v = vector(kind)
    unsigned = {k: x for k, x in v["statement"].items() if k != "sig"}
    for operation in [
        lambda: statements.sign_statement(unsigned, bytes(32), expected_kind=kind),
        lambda: statements.verify_statement(raw(v["statement"]), expected_kind=kind, public_key=public(v)),
    ]:
        with pytest.raises(crypto.MeshError) as exc:
            operation()
        assert exc.value.code == code
        if code == "mesh_extra_missing":
            assert isinstance(exc.value, crypto.MeshExtraMissing)


def test_import_parse_and_digest_without_crypto_extra():
    script = r"""
import json,sys
sys.modules['cryptography'] = None
from brigade.fleet_mesh_statements import parse_statement, statement_digest, verify_statement
from brigade.fleet_mesh_crypto import MeshExtraMissing
v = json.loads(sys.stdin.read())
raw = v['signed_canonical_ascii'].encode('ascii')
assert parse_statement(raw, expected_kind=v['kind']) == v['statement']
assert statement_digest(raw, expected_kind=v['kind']).hex() == v['signed_sha256_hex']
try:
    verify_statement(raw, expected_kind=v['kind'], public_key=bytes(32))
except MeshExtraMissing:
    pass
else:
    raise AssertionError('optional crypto extra was not required by primitive use')
"""
    result = subprocess.run([sys.executable, "-c", script], input=json.dumps(vector()), text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "kind,field,count",
    [("membership", "members", 4096), ("manifest", "keys", 4096), ("revocation", "revocations", 8192)],
)
def test_inclusive_list_bounds(statements, kind, field, count):
    obj = vector(kind)["statement"]
    entries = []
    for i in range(count + 1):
        principal = "test-" + str(i).zfill(5)
        kid = "k1_" + base64.urlsafe_b64encode(i.to_bytes(16, "big")).decode("ascii").rstrip("=")
        if kind == "membership":
            entries.append(principal)
        else:
            entry = dict(obj[field][0], principal=principal, kid=kid)
            entries.append(entry)
    obj[field] = entries[:count]
    assert len(statements.parse_statement(raw(obj), expected_kind=kind)[field]) == count
    obj[field] = entries
    with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
        statements.parse_statement(raw(obj), expected_kind=kind)


@pytest.mark.parametrize(
    "kind,bound", [("membership", 2098176), ("manifest", 2098176), ("revocation", 2098176), ("hub-identity", 17408)]
)
def test_inclusive_raw_byte_bounds(statements, kind, bound):
    obj = vector(kind)["statement"]
    encoded = raw(obj)
    payload = encoded + b" " * (bound - len(encoded))
    assert statements.parse_statement(payload, expected_kind=kind) == obj
    with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
        statements.parse_statement(payload + b" ", expected_kind=kind)


def test_signing_preserves_caller_data_and_refuses_a_signed_input(statements):
    v = vector()
    unsigned = {k: x for k, x in v["statement"].items() if k != "sig"}
    before = deepcopy(unsigned)
    signed = statements.sign_statement(unsigned, bytes(range(32)), expected_kind="membership")
    assert unsigned == before
    signed["members"].clear()
    assert unsigned == before
    with pytest.raises(crypto.MeshError, match="^statement_malformed$"):
        statements.sign_statement(v["statement"], bytes(range(32)), expected_kind="membership")


def test_unknown_capability_names_are_data_only_at_the_bounds(statements):
    obj = vector("hub-identity")["statement"]
    obj["capabilities"] = ["a" * 62 + str(i).zfill(2) for i in range(32)]
    obj["max_envelope_bytes"] = 1
    assert statements.parse_statement(raw(obj), expected_kind="hub-identity") == obj
    # An escaped ASCII scalar has the same meaning as its literal spelling.
    payload = raw(obj).replace(b'"full"', b'"fu\\u006cl"')
    assert statements.parse_statement(payload, expected_kind="hub-identity") == obj
