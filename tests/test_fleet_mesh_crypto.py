"""Frozen bm1 wire contracts and adversarial envelope checks."""

import base64
from copy import deepcopy
import hashlib
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from brigade import fleet_mesh_crypto as mesh
from tests.mesh_hpke_reference import aead, decapsulate, kem_keys, open_wrap, schedule

FIXTURES = Path(__file__).parent / "fixtures" / "mesh"
BM1_PUBLIC_KEY_BYTES = 1216
BM1_ENC_BYTES = 1120
BM1_CONTENT_KEY_BYTES = 32
BM1_TAG_BYTES = 16
BM1_WRAP_BYTES = 1168
STAMP = "2026-10-22T12:00:00+00:00"
PAYLOAD = {"subject": "test", "body": "private text \u2603", "ref": None, "thread": "task:1", "sender_label": "build"}


@pytest.fixture
def parties():
    keys = [mesh.generate_keys() for _ in range(3)]
    records = [
        mesh.build_key_record(k, principal="node-" + c, hub="mesh", created_at=STAMP)
        for k, c in zip(keys, "abc", strict=True)
    ]
    return keys, records


def sealed(parties, payload=None, **kwargs):
    keys, records = parties
    return mesh.seal(
        PAYLOAD if payload is None else payload,
        sender=keys[0],
        sender_record=records[0],
        recipient_record=records[1],
        retry_key=b"r" * 32,
        client_id="test-1",
        created_at=kwargs.pop("created_at", STAMP),
        **kwargs,
    )


def opened(envelope, parties, **kwargs):
    keys, records = parties
    return mesh.open(envelope, keys=keys[1], sender_record=records[0], caller="node-b", context="inbound", **kwargs)


def resign(envelope, key):
    header = {k: v for k, v in envelope.items() if k not in {"ct", "recipients", "sig"}}
    envelope["sig"] = mesh.sign(header, key.signing_key, label="sig")


def unb64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


@pytest.mark.parametrize("index", range(3))
def test_xwing_draft_11_known_answers(index):
    vector = json.loads((FIXTURES / "xwing-draft-11.json").read_text())["vectors"][index]
    v = {k: bytes.fromhex(x) for k, x in vector.items()}
    assert kem_keys(v["seed"])[1] == v["pk"]
    assert mesh.kem_public_key(v["seed"]) == v["pk"]
    assert decapsulate(v["sk"], v["ct"]) == v["ss"]


def test_hpke_pq_05_a5_known_answer():
    vector = json.loads((FIXTURES / "hpke-pq-draft-05-a5.json").read_text())["vector"]
    v = {k: bytes.fromhex(x) for k, x in vector.items()}
    assert kem_keys(v["skRm"])[1] == v["pkRm"]
    shared = decapsulate(v["skRm"], v["enc"])
    assert shared == v["shared_secret"]
    key, nonce = schedule(shared, v["info"], 3)
    assert key == v["key"]
    assert nonce == v["base_nonce"]
    assert aead(key, 3).encrypt(nonce, v["pt"], v["aad"]) == v["ct"]


@pytest.mark.parametrize("aead_id", [2, 3])
def test_pyca_reference_agreement(aead_id):
    from cryptography.hazmat.primitives import hpke

    v = {
        k: bytes.fromhex(x)
        for k, x in json.loads((FIXTURES / "hpke-pq-draft-05-a5.json").read_text())["vector"].items()
    }
    suite = hpke.Suite(
        hpke.KEM.MLKEM768_X25519,
        hpke.KDF.HKDF_SHA256,
        hpke.AEAD.AES_256_GCM if aead_id == 2 else hpke.AEAD.CHACHA20_POLY1305,
    )
    private, _ = kem_keys(v["skRm"])
    key, nonce = schedule(v["shared_secret"], v["info"], aead_id)
    reference = v["enc"] + aead(key, aead_id).encrypt(nonce, b"independent plaintext", b"")
    assert suite.decrypt(reference, private, v["info"]) == b"independent plaintext"
    pyca = suite.encrypt(b"pyca plaintext", private.public_key(), v["info"])
    assert open_wrap(v["skRm"], pyca, v["info"], aead_id) == b"pyca plaintext"


def test_frozen_profile_and_wrap_lengths(parties):
    assert (mesh.KEM_ID, mesh.KDF_ID, mesh.AEAD_ID) == (0x647A, 1, 2)
    e = sealed(parties)
    assert e["suite"] == "bm1" and e["v"] == 1
    assert all(len(unb64(r["enc_pub"])) == BM1_PUBLIC_KEY_BYTES for r in parties[1])
    for entry in e["recipients"]:
        wrap = unb64(entry["wrap"])
        assert len(wrap) == BM1_WRAP_BYTES
        assert len(wrap[:BM1_ENC_BYTES]) == BM1_ENC_BYTES
        assert len(wrap[BM1_ENC_BYTES:]) == BM1_CONTENT_KEY_BYTES + BM1_TAG_BYTES == 48
    # A primitive-level open independently pins the production suite to AES-256-GCM.
    rh = {k: v for k, v in e.items() if k not in {"ct_sha256", "wraps_sha256", "rc", "sig", "ct", "recipients"}}
    wrap = next(r["wrap"] for r in e["recipients"] if r["kid"] == parties[1][1]["kid"])
    cek = open_wrap(
        parties[0][1].kem_seed, unb64(wrap), b"brigade-mesh/v1/cek\0" + hashlib.sha256(mesh.canonical(rh)).digest()
    )
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    raw = AESGCM(cek).decrypt(bytes(12), unb64(e["ct"]), b"brigade-mesh/v1/aad\0" + mesh.canonical(rh))
    assert mesh.decode_payload(raw) == PAYLOAD


@pytest.mark.parametrize(
    "field,value",
    [
        ("hub", "h_AAAAAAAAAAAAAAAAAAAAAA"),
        ("from", "node-c"),
        ("from_kid", "k1_AAAAAAAAAAAAAAAAAAAAAA"),
        ("to", "node-c"),
        ("kind", "completion"),
        ("priority", "urgent"),
        ("client_id", "another"),
        ("created_at", "2026-10-22T12:00:01+00:00"),
        ("pad", 2048),
        ("deadline", "2026-10-22T12:06:00+00:00"),
        ("in_reply_to", {"hub": "mesh", "from": "node-c", "client_id": "reply"}),
        ("ct_sha256", "A" * 43),
        ("wraps_sha256", "A" * 43),
        ("rc", "A" * 43),
    ],
)
def test_header_tampering_fails(parties, field, value):
    e = sealed(
        parties,
        kind="question",
        deadline="2026-10-22T12:05:00+00:00",
        in_reply_to={"hub": "mesh", "from": "node-b", "client_id": "original"},
    )
    e[field] = value
    with pytest.raises(mesh.MeshError):
        opened(e, parties)


@pytest.mark.parametrize(
    "target,code",
    [
        ("ct", "envelope_digest_mismatch"),
        ("wrap", "envelope_digest_mismatch"),
        ("sig", "envelope_signature_invalid"),
        ("rcpt", "envelope_signature_invalid"),
    ],
)
def test_binary_and_recipient_tampering(parties, target, code):
    e = sealed(parties)
    if target == "rcpt":
        e["rcpt"] = sorted({e["from_kid"], parties[1][2]["kid"]})
    else:
        field = e["recipients"][0] if target == "wrap" else e
        raw = bytearray(unb64(field[target]))
        raw[0] ^= 1
        field[target] = mesh.encode_binary(raw)
    with pytest.raises(mesh.MeshError, match=code):
        opened(e, parties)


def test_resigned_sender_cannot_claim_ciphertext(parties):
    keys, records = parties
    e = sealed(parties)
    e["from"], e["from_kid"] = "node-c", records[2]["kid"]
    e["rcpt"] = sorted([records[1]["kid"], records[2]["kid"]])
    old_wraps = {x["kid"]: x["wrap"] for x in e["recipients"]}
    e["recipients"] = [{"kid": kid, "wrap": old_wraps.get(kid, old_wraps[records[0]["kid"]])} for kid in e["rcpt"]]
    e["wraps_sha256"] = mesh.encode_binary(hashlib.sha256(mesh.canonical(e["recipients"])).digest())
    resign(e, keys[2])
    mesh.verify_signed_header(e, records[2])
    with pytest.raises(mesh.MeshError, match="envelope_decryption_failed"):
        mesh.open(e, keys=keys[1], sender_record=records[2], caller="node-b", context="inbound")
    # Even if the old content key were provided, the changed routing AAD fails.
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    original = sealed(parties)
    rh = {k: v for k, v in original.items() if k not in {"ct_sha256", "wraps_sha256", "rc", "sig", "ct", "recipients"}}
    wrap = next(x["wrap"] for x in original["recipients"] if x["kid"] == records[1]["kid"])
    cek = open_wrap(
        keys[1].kem_seed, unb64(wrap), b"brigade-mesh/v1/cek\0" + hashlib.sha256(mesh.canonical(rh)).digest()
    )
    rh["from"] = "node-c"
    with pytest.raises(InvalidTag):
        AESGCM(cek).decrypt(bytes(12), unb64(original["ct"]), b"brigade-mesh/v1/aad\0" + mesh.canonical(rh))


def test_forwarding_and_wrong_context(parties):
    e = sealed(parties)
    with pytest.raises(mesh.MeshError, match="open_context_mismatch"):
        mesh.open(e, keys=parties[0][2], sender_record=parties[1][0], caller="node-c", context="inbound")
    with pytest.raises(mesh.MeshError, match="open_context_mismatch"):
        mesh.open(e, keys=parties[0][1], sender_record=parties[1][0], caller="node-b", context="outbound")
    e["to"] = "node-c"
    with pytest.raises(mesh.MeshError, match="envelope_signature_invalid"):
        mesh.open(e, keys=parties[0][2], sender_record=parties[1][0], caller="node-c", context="inbound")


def test_signed_header_survives_purge(parties):
    e = sealed(parties)
    del e["ct"], e["recipients"]
    assert mesh.verify_signed_header(e, parties[1][0])["rc"] == e["rc"]
    with pytest.raises(mesh.MeshError, match="content_expired"):
        opened(e, parties)


@pytest.mark.parametrize("field,value", [("v", 2), ("suite", "bm2")])
def test_unknown_profile(parties, field, value):
    e = sealed(parties)
    e[field] = value
    with pytest.raises(mesh.MeshError, match="envelope_unsupported"):
        opened(e, parties)


@pytest.mark.parametrize("length", [1, 980, 1023, 1024, 2020, 65536])
def test_padding_buckets(length):
    payload = dict(PAYLOAD, body="b" * length)
    raw = mesh.encode_payload(payload)
    assert len(raw) % 1024 == 0
    assert mesh.decode_payload(raw) == payload
    assert 0x80 in raw
    with pytest.raises(mesh.MeshError):
        mesh.decode_payload(raw + bytes(1024))


def test_retry_commitment_and_fresh_content_keys(parties):
    first, second = sealed(parties), sealed(parties, created_at="2026-10-22T12:01:00+00:00")
    assert first["ct"] != second["ct"] and first["recipients"] != second["recipients"]
    assert first["rc"] == second["rc"]
    for name in PAYLOAD:
        changed = dict(PAYLOAD, **{name: "changed"})
        assert sealed(parties, changed)["rc"] != first["rc"]
    for field, value in [
        ("to", "node-c"),
        ("from", "node-c"),
        ("hub", "h_AAAAAAAAAAAAAAAAAAAAAA"),
        ("kind", "completion"),
        ("priority", "high"),
        ("client_id", "other"),
        ("deadline", "2026-10-22T12:05:00+00:00"),
        ("in_reply_to", {"hub": "mesh", "from": "node-b", "client_id": "other"}),
    ]:
        header = deepcopy(first)
        header[field] = value
        if field == "deadline":
            header["kind"] = "question"
        assert mesh.retry_commitment(b"r" * 32, header, PAYLOAD) != first["rc"]


def test_domain_separation(parties):
    labels = mesh.LABELS
    assert len(set(labels.values())) == len(labels)
    public = unb64(parties[1][0]["sig_pub"])
    for source in labels:
        sig = mesh.sign({"v": 1}, parties[0][0].signing_key, label=source)
        for destination in labels:
            if source != destination:
                with pytest.raises(mesh.MeshError, match="envelope_signature_invalid"):
                    mesh.verify({"v": 1}, sig, public, label=destination)


def test_size_budgets(parties):
    largest = dict(PAYLOAD, subject="s" * 200, body="b" * 65536, ref="r" * 256, thread="t" * 128, sender_label="l" * 64)
    e = sealed(parties, largest)
    maximum = ((sum(mesh.PAYLOAD_LIMITS.values()) + 5 * len(mesh.PAYLOAD_LIMITS) + 1 + 1023) // 1024) * 1024
    assert maximum == 66_560
    assert e["pad"] == maximum
    assert len(mesh.canonical(e)) < 96 * 1024
    with pytest.raises(mesh.MeshError, match="message_too_large"):
        sealed(parties, largest, max_envelope_bytes=32768)
    # 20 KiB is the padded payload budget, inclusive of binary framing.
    budget = dict(PAYLOAD, body="b" * (20 * 1024 - 100))
    e = sealed(parties, budget, max_envelope_bytes=32768)
    assert e["pad"] == 20 * 1024 and len(mesh.canonical(e)) <= 32768


@pytest.mark.parametrize(
    "field,value",
    [
        ("v", 2),
        ("principal", "node-c"),
        ("hub", "h_AAAAAAAAAAAAAAAAAAAAAA"),
        ("kid", "k1_AAAAAAAAAAAAAAAAAAAAAA"),
        ("sig_pub", "A" * 43),
        ("enc_pub", "A" * 1622),
        ("suites", ["bm2"]),
        ("created_at", "2026-10-22T12:00:01+00:00"),
        ("transport", {"iroh": "a" * 64}),
        ("prev_kid", "k1_AAAAAAAAAAAAAAAAAAAAAA"),
    ],
)
def test_key_record_covered_fields(parties, field, value):
    record = deepcopy(parties[1][0])
    record[field] = value
    with pytest.raises(mesh.MeshError):
        mesh.verify_key_record(record)


def test_key_record_continuity_and_fingerprint(parties):
    keys, records = parties
    new = mesh.build_key_record(
        keys[2],
        principal="node-a",
        hub="mesh",
        created_at="2026-10-22T12:01:00+00:00",
        previous=keys[0],
        transport={"iroh": "a" * 64},
    )
    mesh.verify_key_record(new, principal="node-a", hub="mesh", previous_record=records[0])
    sig_pub = unb64(records[0]["sig_pub"])
    expected = base64.b32encode(hashlib.sha256(sig_pub).digest()).decode().rstrip("=")
    assert mesh.fingerprint(sig_pub).replace("-", "") == expected
    new["transport"]["iroh"] = "b" * 64
    with pytest.raises(mesh.MeshError, match="envelope_signature_invalid"):
        mesh.verify_key_record(new)


@pytest.mark.parametrize(
    "field,value",
    [
        ("v", True),
        ("pad", True),
        ("pad", 1025),
        ("client_id", "bad space"),
        ("created_at", "2026-99-22T12:00:00Z"),
        ("from", "invalid:principal"),
        ("to", []),
        ("rcpt", []),
        ("rcpt", ["k1_AAAAAAAAAAAAAAAAAAAAAA"]),
        ("ct", "="),
        ("sig", "!" * 86),
        ("extra", "unknown"),
        ("deadline", STAMP),
        ("in_reply_to", {"client_id": "x"}),
    ],
)
def test_strict_envelope_parser(parties, field, value):
    e = sealed(parties)
    e[field] = value
    with pytest.raises(mesh.MeshError):
        opened(e, parties)


def test_strict_payload_parser():
    raw = mesh.encode_payload(PAYLOAD)
    for changed in [
        raw[:-1],
        bytes(1024),
        b"\x06" + raw[1:],
        b"\x01\xff\xff\xff\xff" + raw[5:],
        b"\x01\x00\x00\x00\x01\xff\x80" + bytes(1017),
        b"\x01\x00\x00\x00\x01a\x01\x00\x00\x00\x01b\x80" + bytes(1011),
    ]:
        with pytest.raises(mesh.MeshError):
            mesh.decode_payload(changed)


def test_noncanonical_base64_rejected():
    # Low unused bits in the last character must be zero.
    with pytest.raises(mesh.MeshError):
        mesh.decode_binary("AB", 1)


def test_missing_extra_and_core_cli_independence():
    script = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "cryptography" or fullname.startswith("cryptography."):
            raise ModuleNotFoundError("blocked cryptography")
sys.meta_path.insert(0, Block())
from brigade.cli import main
try:
    main(["--help"])
except SystemExit as exc:
    assert exc.code == 0
from brigade import fleet_mesh_crypto as mesh
for call in [mesh.probe_backend, mesh.generate_keys, lambda: mesh.kem_public_key(bytes(32)), lambda: mesh.sign({}, bytes(32), label="sig"), lambda: mesh.verify({}, "", bytes(32), label="sig"), lambda: mesh.build_key_record(mesh.PrivateKeys(bytes(32), bytes(32)), principal="node-a", hub="mesh", created_at=""), lambda: mesh.verify_key_record({}), lambda: mesh.verify_signed_header({}, {}), lambda: mesh.seal({}, sender=None, sender_record={}, recipient_record={}, retry_key=bytes(32), client_id="a", created_at=""), lambda: mesh.open({}, keys=None, sender_record={}, caller="node-a", context="inbound")]:
    try:
        call()
    except mesh.MeshExtraMissing as exc:
        assert exc.code == "mesh_extra_missing"
    else:
        raise AssertionError("missing extra did not fail closed")
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_unsupported_mlkem_never_falls_back(parties, monkeypatch):
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives.asymmetric import mlkem

    def unsupported(*args):
        raise UnsupportedAlgorithm("test-only unsupported backend")

    monkeypatch.setattr(mlkem.MLKEM768PrivateKey, "from_seed_bytes", unsupported)
    with pytest.raises(mesh.MeshError, match="mesh_backend_unsupported"):
        sealed(parties)


def test_checked_in_envelope_and_cross_version_exchange():
    fixture = json.loads((FIXTURES / "bm1-envelope.json").read_text())
    keys = mesh.PrivateKeys(
        bytes.fromhex(fixture["recipient_signing_key"]), bytes.fromhex(fixture["recipient_kem_seed"])
    )
    sender = mesh.PrivateKeys(bytes.fromhex(fixture["sender_signing_key"]), bytes.fromhex(fixture["sender_kem_seed"]))

    def check(envelope):
        assert (
            mesh.open(envelope, keys=keys, sender_record=fixture["sender_record"], caller="node-b", context="inbound")
            == fixture["payload"]
        )
        assert (
            mesh.open(
                envelope, keys=sender, sender_record=fixture["sender_record"], caller="node-a", context="outbound"
            )
            == fixture["payload"]
        )
        rh = {
            k: v for k, v in envelope.items() if k not in {"ct_sha256", "wraps_sha256", "rc", "sig", "ct", "recipients"}
        }
        info = b"brigade-mesh/v1/cek\0" + hashlib.sha256(mesh.canonical(rh)).digest()
        for private, record in [(keys, fixture["recipient_record"]), (sender, fixture["sender_record"])]:
            wrap = next(r["wrap"] for r in envelope["recipients"] if r["kid"] == record["kid"])
            cek = open_wrap(private.kem_seed, unb64(wrap), info)
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            assert AESGCM(cek).decrypt(
                bytes(12), unb64(envelope["ct"]), b"brigade-mesh/v1/aad\0" + mesh.canonical(rh)
            ) == bytes.fromhex(fixture["padded_payload"])

    check(fixture["envelope"])
    if os.environ.get("MESH_IMPORT"):
        check(json.loads(Path(os.environ["MESH_IMPORT"]).read_text()))
    if os.environ.get("MESH_EXPORT"):
        envelope = mesh.seal(
            fixture["payload"],
            sender=sender,
            sender_record=fixture["sender_record"],
            recipient_record=fixture["recipient_record"],
            retry_key=b"r" * 32,
            client_id="fixture-1",
            created_at=STAMP,
        )
        check(envelope)
        Path(os.environ["MESH_EXPORT"]).write_text(json.dumps(envelope))


def test_recipient_and_sender_round_trip():
    from brigade import fleet_mesh_crypto as mesh

    sender = mesh.generate_keys()
    recipient = mesh.generate_keys()
    sender_record = mesh.build_key_record(
        sender, principal="node-a", hub="mesh", created_at="2026-10-22T12:00:00+00:00"
    )
    recipient_record = mesh.build_key_record(
        recipient, principal="node-b", hub="mesh", created_at="2026-10-22T12:00:00+00:00"
    )
    payload = {"subject": "test", "body": "private text", "ref": None, "thread": None, "sender_label": None}
    envelope = mesh.seal(
        payload,
        sender=sender,
        sender_record=sender_record,
        recipient_record=recipient_record,
        retry_key=b"r" * 32,
        client_id="test-1",
        created_at="2026-10-22T12:00:00+00:00",
    )
    assert (
        mesh.open(envelope, keys=recipient, sender_record=sender_record, caller="node-b", context="inbound") == payload
    )
    assert mesh.open(envelope, keys=sender, sender_record=sender_record, caller="node-a", context="outbound") == payload


def test_resigned_corrupt_payload_fails_aead(parties):
    envelope = sealed(parties)
    ct = bytearray(unb64(envelope["ct"]))
    ct[0] ^= 1
    envelope["ct"] = mesh.encode_binary(ct)
    envelope["ct_sha256"] = mesh.encode_binary(hashlib.sha256(ct).digest())
    resign(envelope, parties[0][0])
    mesh.verify_signed_header(envelope, parties[1][0])
    with pytest.raises(mesh.MeshError, match="envelope_decryption_failed"):
        opened(envelope, parties)


def test_context_selects_own_wrap(parties):
    e = sealed(parties)
    with pytest.raises(mesh.MeshError, match="mesh_key_invalid"):
        mesh.open(e, keys=parties[0][1], sender_record=parties[1][0], caller="node-a", context="outbound")
    with pytest.raises(mesh.MeshError, match="mesh_key_invalid"):
        mesh.open(e, keys=parties[0][0], sender_record=parties[1][0], caller="node-b", context="inbound")


def test_record_rejects_malformed_previous_signature(parties):
    record = mesh.build_key_record(
        parties[0][2], principal="node-a", hub="mesh", created_at=STAMP, previous=parties[0][0]
    )
    record["prev_sig"] = "invalid"
    with pytest.raises(mesh.MeshError, match="envelope_malformed"):
        mesh.verify_key_record(record)


def test_record_rejects_invalid_timezone_offset(parties):
    with pytest.raises(mesh.MeshError, match="envelope_malformed"):
        mesh.build_key_record(parties[0][0], principal="node-a", hub="mesh", created_at="2026-10-22T12:00:00+00:99")


@pytest.mark.skipif("MESH_ENV" not in os.environ, reason="explicit mesh CI environment probe")
def test_ci_mesh_environment():
    root = Path(__file__).resolve().parents[1]
    assert Path(sys.prefix).resolve() == (root / ".venv").resolve()
    assert Path(mesh.__file__).resolve() == root / "src/brigade/fleet_mesh_crypto.py"
    expected = os.environ["MESH_ENV"]
    assert expected in {"present", "absent"}
    if expected == "absent":
        assert importlib.util.find_spec("cryptography") is None
        with pytest.raises(importlib.metadata.PackageNotFoundError):
            importlib.metadata.version("cryptography")
        with pytest.raises(ModuleNotFoundError):
            __import__("cryptography")
    else:
        import cryptography

        assert Path(cryptography.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
        mesh.probe_backend()


@pytest.mark.parametrize("fraction", ["", ".123", ".123456"])
def test_record_accepts_portable_fractional_seconds(parties, fraction):
    record = mesh.build_key_record(
        parties[0][0], principal="node-a", hub="mesh", created_at=f"2026-10-22T12:00:00{fraction}+00:00"
    )
    mesh.verify_key_record(record)


@pytest.mark.parametrize("fraction", [".1", ".12", ".1234", ".12345"])
def test_record_rejects_nonportable_fractional_seconds(parties, fraction):
    with pytest.raises(mesh.MeshError, match="envelope_malformed"):
        mesh.build_key_record(
            parties[0][0], principal="node-a", hub="mesh", created_at=f"2026-10-22T12:00:00{fraction}+00:00"
        )


@pytest.mark.parametrize(
    "previous_hub,lineage,accepted",
    [
        ("mesh", None, True),
        ("h_AAAAAAAAAAAAAAAAAAAAAA", None, False),
        ("h_AAAAAAAAAAAAAAAAAAAAAA", frozenset({"mesh"}), False),
        ("h_AAAAAAAAAAAAAAAAAAAAAA", frozenset({"h_AAAAAAAAAAAAAAAAAAAAAA"}), False),
        ("h_AAAAAAAAAAAAAAAAAAAAAA", frozenset({"mesh", "h_AAAAAAAAAAAAAAAAAAAAAA"}), True),
    ],
)
@pytest.mark.parametrize("principal", ["node-a", "node-b"])
def test_predecessor_identity_and_accepted_hub_lineage(parties, previous_hub, lineage, accepted, principal):
    keys, _ = parties
    previous = mesh.build_key_record(keys[0], principal=principal, hub=previous_hub, created_at=STAMP)
    current = mesh.build_key_record(keys[2], principal="node-a", hub="mesh", created_at=STAMP, previous=keys[0])
    kwargs = {} if lineage is None else {"accepted_hub_lineage": lineage}
    if accepted and principal == "node-a":
        mesh.verify_key_record(current, previous_record=previous, **kwargs)
    else:
        with pytest.raises(mesh.MeshError, match="mesh_key_invalid"):
            mesh.verify_key_record(current, previous_record=previous, **kwargs)
