"""Pure bm1 cryptography, frozen to X-Wing-11 and HPKE-PQ-05.

No imports of the optional backend occur until a cryptographic operation. Trust,
key storage, freshness and replay policy belong to later slices. bytearray
scratch buffers are wiped in finally blocks. Python/pyca also make immutable
copies (including returned private seeds and plaintext); neither their lifetime
nor backend allocator zeroization can be guaranteed by Python.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import struct
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

VERSION = 1
SUITE = "bm1"
KEM_ID, KDF_ID, AEAD_ID = 0x647A, 0x0001, 0x0002
PUBLIC_KEY_BYTES, SEED_BYTES, ENC_BYTES, WRAP_BYTES = 1216, 32, 1120, 1168
MAX_ENVELOPE_BYTES = 96 * 1024
MAX_RAW_ENVELOPE_BYTES = MAX_ENVELOPE_BYTES + 1024
MAX_ENVELOPE_DEPTH = 4
PAYLOAD_LIMITS = {"subject": 200, "body": 65536, "ref": 256, "thread": 128, "sender_label": 64}
MAX_PAD_BYTES = ((sum(PAYLOAD_LIMITS.values()) + 5 * len(PAYLOAD_LIMITS) + 1 + 1023) // 1024) * 1024
LABELS = {
    name: b"brigade-mesh/v1/" + name.encode("ascii") + b"\0"
    for name in (
        "aad",
        "cek",
        "sig",
        "key",
        "kid",
        "retry",
        "ask",
        "hub-id",
        "hub-identity",
        "hub-migration",
        "manifest",
        "membership",
        "revocation",
        "ack",
    )
}
LABELS.update({name: b"brigade-invite/v1/" + name.encode("ascii") for name in ("redeem", "bind")})
_PAYLOAD_FIELDS = ("subject", "body", "ref", "thread", "sender_label")
_RH_FIELDS = frozenset(
    {
        "v",
        "suite",
        "hub",
        "from",
        "from_kid",
        "to",
        "kind",
        "priority",
        "client_id",
        "created_at",
        "deadline",
        "in_reply_to",
        "pad",
        "rcpt",
    }
)
_DIGEST_FIELDS = frozenset({"ct_sha256", "wraps_sha256", "rc"})
_REQUIRED_RH = _RH_FIELDS - {"deadline", "in_reply_to"}
_B64 = re.compile(r"[A-Za-z0-9_-]+", re.ASCII)
_NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", re.ASCII)
_AGENT = re.compile(r"agent:[a-z][a-z0-9._-]{0,62}", re.ASCII)
_CLIENT = re.compile(r"[A-Za-z0-9_-]{1,64}", re.ASCII)
_THREAD = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/#-]{0,127}", re.ASCII)
_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", re.ASCII)
_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{3}(?:[0-9]{3})?)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
    re.ASCII,
)


class MeshError(ValueError):
    """Fixed, non-secret diagnostics, suitable for branching by code."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(
            code
            + (
                ": install the mesh extra with a cryptography wheel supporting ML-KEM"
                if code == "mesh_backend_unsupported"
                else ""
            )
        )


class MeshExtraMissing(MeshError):
    def __init__(self) -> None:
        super().__init__("mesh_extra_missing")


def _require(condition: object, code: str = "envelope_malformed") -> None:
    if not condition:
        raise MeshError(code)


def _backend() -> Any:
    try:
        from cryptography.exceptions import UnsupportedAlgorithm
        from cryptography.hazmat.primitives import hpke
        from cryptography.hazmat.primitives.asymmetric import mlkem
    except ImportError:
        raise MeshExtraMissing() from None
    try:
        # Probe the actual backend, not merely the presence of Python symbols.
        mlkem.MLKEM768PrivateKey.from_seed_bytes(bytes(64))
        return hpke.Suite(hpke.KEM.MLKEM768_X25519, hpke.KDF.HKDF_SHA256, hpke.AEAD.AES_256_GCM)
    except (UnsupportedAlgorithm, AttributeError):
        raise MeshError("mesh_backend_unsupported") from None


def probe_backend() -> None:
    """Fail closed if the installed wheel/backend cannot implement bm1."""
    _backend()


def canonical(value: Any) -> bytes:
    """D2 canonical JSON. Callers validate the schema before authentication."""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode(
            "ascii"
        )
    except (ValueError, TypeError, RecursionError):
        raise MeshError("envelope_malformed") from None


def encode_binary(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def decode_binary(value: Any, size: int) -> bytes:
    _require(isinstance(value, str) and len(value) == (size * 8 + 5) // 6 and _B64.fullmatch(value))
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError:
        raise MeshError("envelope_malformed") from None
    _require(len(raw) == size and hmac.compare_digest(encode_binary(raw), value))
    return raw


def _bytes(value: bytes, size: int) -> None:
    _require(isinstance(value, bytes) and len(value) == size, "mesh_key_invalid")


def _wipe(value: bytearray) -> None:
    value[:] = bytes(len(value))


@dataclass(frozen=True)
class PrivateKeys:
    """Separate raw Ed25519 private key and portable 32-byte X-Wing seed."""

    signing_key: bytes = field(repr=False)
    kem_seed: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _bytes(self.signing_key, 32)
        _bytes(self.kem_seed, SEED_BYTES)


def generate_keys() -> PrivateKeys:
    probe_backend()
    return PrivateKeys(secrets.token_bytes(32), secrets.token_bytes(SEED_BYTES))


def _kem_private(seed: bytes) -> tuple[Any, bytes]:
    _bytes(seed, SEED_BYTES)
    from cryptography.hazmat.primitives import hpke
    from cryptography.hazmat.primitives.asymmetric import mlkem, x25519

    expanded = bytearray(hashlib.shake_256(seed).digest(96))
    try:
        ml = mlkem.MLKEM768PrivateKey.from_seed_bytes(bytes(expanded[:64]))
        x = x25519.X25519PrivateKey.from_private_bytes(bytes(expanded[64:]))
        return hpke.MLKEM768X25519PrivateKey(
            ml, x
        ), ml.public_key().public_bytes_raw() + x.public_key().public_bytes_raw()
    finally:
        _wipe(expanded)


def kem_public_key(seed: bytes) -> bytes:
    probe_backend()
    return _kem_private(seed)[1]


def _kem_public(raw: bytes) -> Any:
    from cryptography.hazmat.primitives import hpke
    from cryptography.hazmat.primitives.asymmetric import mlkem, x25519

    return hpke.MLKEM768X25519PublicKey(
        mlkem.MLKEM768PublicKey.from_public_bytes(raw[:1184]), x25519.X25519PublicKey.from_public_bytes(raw[1184:])
    )


def key_id(sig_pub: bytes, enc_pub: bytes) -> str:
    _bytes(sig_pub, 32)
    _bytes(enc_pub, PUBLIC_KEY_BYTES)
    return "k1_" + encode_binary(hashlib.sha256(LABELS["kid"] + sig_pub + enc_pub).digest()[:16])


def fingerprint(sig_pub: bytes) -> str:
    _bytes(sig_pub, 32)
    text = base64.b32encode(hashlib.sha256(sig_pub).digest()).decode("ascii").rstrip("=")
    return "-".join(text[i : i + 4] for i in range(0, len(text), 4))


def sign(value: dict[str, Any], signing_key: bytes, *, label: str) -> str:
    probe_backend()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    _bytes(signing_key, 32)
    _require(label in LABELS, "mesh_label_invalid")
    return encode_binary(Ed25519PrivateKey.from_private_bytes(signing_key).sign(LABELS[label] + canonical(value)))


def verify(value: dict[str, Any], signature: str, sig_pub: bytes, *, label: str) -> None:
    probe_backend()
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    _bytes(sig_pub, 32)
    _require(label in LABELS, "mesh_label_invalid")
    raw = decode_binary(signature, 64)
    try:
        Ed25519PublicKey.from_public_bytes(sig_pub).verify(raw, LABELS[label] + canonical(value))
    except InvalidSignature:
        raise MeshError("envelope_signature_invalid") from None


def _principal(value: Any) -> None:
    _require(isinstance(value, str) and (_NODE.fullmatch(value) or _AGENT.fullmatch(value)))


def _hub(value: Any) -> None:
    _require(isinstance(value, str))
    if value != "mesh":
        _require(value.startswith("h_"))
        decode_binary(value[2:], 16)


def _kid(value: Any) -> None:
    _require(isinstance(value, str) and value.startswith("k1_"))
    decode_binary(value[3:], 16)


def _timestamp(value: Any) -> None:
    _require(isinstance(value, str) and _TIME.fullmatch(value))
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MeshError("envelope_malformed") from None


def build_key_record(
    keys: PrivateKeys,
    *,
    principal: str,
    hub: str,
    created_at: str,
    transport: dict[str, str] | None = None,
    previous: PrivateKeys | None = None,
) -> dict[str, Any]:
    probe_backend()
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    sig_pub = Ed25519PrivateKey.from_private_bytes(keys.signing_key).public_key().public_bytes_raw()
    enc_pub = kem_public_key(keys.kem_seed)
    record: dict[str, Any] = {
        "v": VERSION,
        "principal": principal,
        "hub": hub,
        "kid": key_id(sig_pub, enc_pub),
        "sig_pub": encode_binary(sig_pub),
        "enc_pub": encode_binary(enc_pub),
        "suites": [SUITE],
        "created_at": created_at,
    }
    if transport is not None:
        record["transport"] = dict(transport)
    if previous is not None:
        prev_pub = Ed25519PrivateKey.from_private_bytes(previous.signing_key).public_key().public_bytes_raw()
        record["prev_kid"] = key_id(prev_pub, kem_public_key(previous.kem_seed))
    _validate_record(record)
    unsigned = dict(record)
    record["self_sig"] = sign(unsigned, keys.signing_key, label="key")
    if previous is not None:
        record["prev_sig"] = sign(unsigned, previous.signing_key, label="key")
    return record


def _validate_record(record: dict[str, Any]) -> None:
    required = {"v", "principal", "hub", "kid", "sig_pub", "enc_pub", "suites", "created_at"}
    _require(
        isinstance(record, dict)
        and required <= record.keys() <= required | {"transport", "prev_kid", "self_sig", "prev_sig"}
    )
    _require(
        type(record["v"]) is int and record["v"] == VERSION and record["suites"] == [SUITE], "envelope_unsupported"
    )
    _principal(record["principal"])
    _hub(record["hub"])
    _timestamp(record["created_at"])
    _kid(record["kid"])
    sig_pub, enc_pub = decode_binary(record["sig_pub"], 32), decode_binary(record["enc_pub"], PUBLIC_KEY_BYTES)
    _require(hmac.compare_digest(record["kid"], key_id(sig_pub, enc_pub)), "mesh_key_invalid")
    if "transport" in record:
        transport = record["transport"]
        _require(isinstance(transport, dict) and set(transport) == {"iroh"})
        _require(isinstance(transport["iroh"], str) and re.fullmatch(r"[0-9a-f]{64}", transport["iroh"]))
    if "prev_kid" in record:
        _kid(record["prev_kid"])
    for signature in ("self_sig", "prev_sig"):
        if signature in record:
            decode_binary(record[signature], 64)


def verify_key_record(
    record: dict[str, Any],
    *,
    principal: str | None = None,
    hub: str | None = None,
    previous_record: dict[str, Any] | None = None,
    accepted_hub_lineage: set[str] | frozenset[str] | None = None,
) -> None:
    """Verify signatures and identity continuity, denying cross-hub links by default.

    Only the caller's authenticated D4 lineage may authorize a changed hub. Both
    hub IDs must belong to that one accepted lineage. This helper neither learns
    nor authenticates lineage from the key records themselves.

    This checks one link, not a chain. Slice 2 enforces the eight-link limit,
    revocation and the pinned head that prevents replay of an already used link.
    """
    probe_backend()
    _validate_record(record)
    _require("self_sig" in record and ("prev_kid" in record) == ("prev_sig" in record))
    _require(principal is None or record["principal"] == principal, "mesh_key_invalid")
    _require(hub is None or record["hub"] == hub, "mesh_key_invalid")
    unsigned = {k: v for k, v in record.items() if k not in {"self_sig", "prev_sig"}}
    verify(unsigned, record["self_sig"], decode_binary(record["sig_pub"], 32), label="key")
    if accepted_hub_lineage is not None:
        _require(isinstance(accepted_hub_lineage, (set, frozenset)), "mesh_key_invalid")
        for accepted_hub in accepted_hub_lineage:
            _hub(accepted_hub)
    if previous_record is not None:
        verify_key_record(previous_record, principal=record["principal"])
        _require(
            previous_record["hub"] == record["hub"]
            or (accepted_hub_lineage is not None and {previous_record["hub"], record["hub"]} <= accepted_hub_lineage),
            "mesh_key_invalid",
        )
        _require(
            record.get("prev_kid") == previous_record["kid"]
            and record["kid"] != previous_record["kid"]
            and datetime.fromisoformat(record["created_at"].replace("Z", "+00:00"))
            > datetime.fromisoformat(previous_record["created_at"].replace("Z", "+00:00")),
            "mesh_key_invalid",
        )
        verify(unsigned, record["prev_sig"], decode_binary(previous_record["sig_pub"], 32), label="key")


def _payload(payload: dict[str, Any]) -> None:
    _require(isinstance(payload, dict) and set(payload) == set(_PAYLOAD_FIELDS))
    for name, limit in PAYLOAD_LIMITS.items():
        value = payload[name]
        if name not in {"subject", "body"} and value is None:
            continue
        _require(isinstance(value, str))
        if name in {"subject", "body"}:
            _require(bool(value.strip()))
        try:
            size = len(value.encode("utf-8"))
        except UnicodeError:
            raise MeshError("envelope_malformed") from None
        _require(size <= limit, "message_too_large")
        if name == "thread":
            _require(_THREAD.fullmatch(value))
        if name == "sender_label":
            _require(_LABEL.fullmatch(value))


def encode_payload(payload: dict[str, Any]) -> bytes:
    """Tags 1..5 in field order; optional null fields are omitted, not empty."""
    _payload(payload)
    raw = bytearray()
    for tag, name in enumerate(_PAYLOAD_FIELDS, 1):
        if payload[name] is not None:
            value = payload[name].encode("utf-8")
            raw.extend(struct.pack(">BI", tag, len(value)))
            raw.extend(value)
    raw.append(0x80)
    raw.extend(bytes(-len(raw) % 1024))
    return bytes(raw)


def decode_payload(raw: bytes) -> dict[str, Any]:
    _require(isinstance(raw, bytes) and 1024 <= len(raw) <= MAX_PAD_BYTES and len(raw) % 1024 == 0)
    result: dict[str, Any] = dict.fromkeys(_PAYLOAD_FIELDS)
    pos, last = 0, 0
    while pos < len(raw) and raw[pos] != 0x80:
        _require(pos + 5 <= len(raw))
        tag, length = struct.unpack_from(">BI", raw, pos)
        _require(last < tag <= 5 and pos + 5 + length < len(raw))
        pos += 5
        try:
            result[_PAYLOAD_FIELDS[tag - 1]] = raw[pos : pos + length].decode("utf-8", errors="strict")
        except UnicodeError:
            raise MeshError("envelope_malformed") from None
        pos, last = pos + length, tag
    _require(pos < len(raw) and raw[pos] == 0x80 and not any(raw[pos + 1 :]))
    _require(len(raw) == ((pos + 1 + 1023) // 1024) * 1024)
    _payload(result)
    return result


def _routing(header: dict[str, Any]) -> dict[str, Any]:
    _require(isinstance(header, dict) and _REQUIRED_RH <= header.keys())
    _require(type(header["v"]) is int and header["v"] == VERSION and header["suite"] == SUITE, "envelope_unsupported")
    _hub(header["hub"])
    _principal(header["from"])
    _principal(header["to"])
    _kid(header["from_kid"])
    _require(isinstance(header["kind"], str) and header["kind"] in {"message", "question", "completion"})
    _require(isinstance(header["priority"], str) and header["priority"] in {"low", "normal", "high", "urgent"})
    _require(isinstance(header["client_id"], str) and _CLIENT.fullmatch(header["client_id"]))
    _timestamp(header["created_at"])
    _require((header["kind"] == "question") == ("deadline" in header))
    if "deadline" in header:
        _timestamp(header["deadline"])
    if "in_reply_to" in header:
        reply = header["in_reply_to"]
        _require(isinstance(reply, dict) and set(reply) == {"hub", "from", "client_id"})
        _hub(reply["hub"])
        _principal(reply["from"])
        _require(isinstance(reply["client_id"], str) and _CLIENT.fullmatch(reply["client_id"]))
    _require(type(header["pad"]) is int and 1024 <= header["pad"] <= MAX_PAD_BYTES and header["pad"] % 1024 == 0)
    rcpt = header["rcpt"]
    _require(isinstance(rcpt, list) and 1 <= len(rcpt) <= 2)
    for kid in rcpt:
        _kid(kid)
    _require(rcpt == sorted(set(rcpt)) and header["from_kid"] in rcpt)
    return {k: v for k, v in header.items() if k in _RH_FIELDS}


def retry_commitment(retry_key: bytes, header: dict[str, Any], payload: dict[str, Any]) -> str:
    _bytes(retry_key, 32)
    _payload(payload)
    _routing(header)
    semantic: dict[str, Any] = {
        k: header[k]
        for k in ("hub", "from", "to", "kind", "priority", "client_id", "deadline", "in_reply_to")
        if k in header
    }
    semantic.update(payload)
    return encode_binary(hmac.digest(retry_key, LABELS["retry"] + canonical(semantic), "sha256"))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def parse_envelope(raw: bytes) -> dict[str, Any]:
    """Bound raw input before JSON allocation, validate schema and require canonical bytes.

    The returned object has exactly one accepted wire representation. Receipt
    hashes use SHA-256 of these canonical raw bytes, never a normalized input.
    Both complete envelopes and signed content-expired tombstones are accepted.
    """
    _require(isinstance(raw, bytes))
    _require(len(raw) <= MAX_RAW_ENVELOPE_BYTES, "message_too_large")
    _require(raw.isascii() and b"\x00" not in raw)
    text = raw.decode("ascii")
    depth, quoted, escaped = 0, False, False
    for byte in raw:
        if quoted:
            if escaped:
                escaped = False
            elif byte == 92:
                escaped = True
            elif byte == 34:
                quoted = False
        elif byte == 34:
            quoted = True
        elif byte in (91, 123):
            depth += 1
            _require(depth <= MAX_ENVELOPE_DEPTH)
        elif byte in (93, 125):
            depth -= 1
            _require(depth >= 0)
    _require(depth == 0 and not quoted)
    try:
        envelope = json.loads(text, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError, RecursionError):
        raise MeshError("envelope_malformed") from None
    _require(isinstance(envelope, dict))
    rh = _routing(envelope)
    required = set(rh) | _DIGEST_FIELDS | {"sig"}
    _require(required <= envelope.keys() <= required | {"ct", "recipients"})
    for name in _DIGEST_FIELDS:
        decode_binary(envelope[name], 32)
    decode_binary(envelope["sig"], 64)
    _require(("ct" in envelope) == ("recipients" in envelope))
    if "ct" in envelope:
        decode_binary(envelope["ct"], rh["pad"] + 16)
        recipients = envelope["recipients"]
        _require(isinstance(recipients, list) and len(recipients) == len(rh["rcpt"]))
        for kid, entry in zip(rh["rcpt"], recipients, strict=True):
            _require(isinstance(entry, dict) and set(entry) == {"kid", "wrap"} and entry["kid"] == kid)
            decode_binary(entry["wrap"], WRAP_BYTES)
    encoded = canonical(envelope)
    _require(len(encoded) <= MAX_ENVELOPE_BYTES, "message_too_large")
    _require(raw == encoded)
    return envelope


def verify_signed_header(raw: bytes, sender_record: dict[str, Any]) -> dict[str, Any]:
    """Parse and verify full envelopes or tombstones; no trust/freshness claim is made."""
    probe_backend()
    return _verify_signed_header(parse_envelope(raw), sender_record)


def _verify_signed_header(envelope: dict[str, Any], sender_record: dict[str, Any]) -> dict[str, Any]:
    rh = _routing(envelope)
    verify_key_record(sender_record, principal=rh["from"], hub=rh["hub"])
    _require(sender_record["kid"] == rh["from_kid"], "mesh_key_invalid")
    header = {k: v for k, v in envelope.items() if k not in {"ct", "recipients", "sig"}}
    verify(header, envelope["sig"], decode_binary(sender_record["sig_pub"], 32), label="sig")
    return header


def seal(
    payload: dict[str, Any],
    *,
    sender: PrivateKeys,
    sender_record: dict[str, Any],
    recipient_record: dict[str, Any],
    retry_key: bytes,
    client_id: str,
    created_at: str,
    kind: str = "message",
    priority: str = "normal",
    deadline: str | None = None,
    in_reply_to: dict[str, str] | None = None,
    max_envelope_bytes: int = MAX_ENVELOPE_BYTES,
) -> dict[str, Any]:
    suite = _backend()
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    verify_key_record(sender_record)
    verify_key_record(recipient_record, hub=sender_record["hub"])
    pub = Ed25519PrivateKey.from_private_bytes(sender.signing_key).public_key().public_bytes_raw()
    _require(
        hmac.compare_digest(pub, decode_binary(sender_record["sig_pub"], 32))
        and hmac.compare_digest(
            kem_public_key(sender.kem_seed), decode_binary(sender_record["enc_pub"], PUBLIC_KEY_BYTES)
        ),
        "mesh_key_invalid",
    )
    padded = encode_payload(payload)
    records = {r["kid"]: r for r in (recipient_record, sender_record)}
    # A shared key cannot stand for two different message principals, and a
    # self-send must name the sender's own record so it deduplicates to one wrap.
    if recipient_record["principal"] == sender_record["principal"]:
        _require(recipient_record == sender_record, "mesh_key_invalid")
    else:
        _require(len(records) == 2, "mesh_key_invalid")
    rh: dict[str, Any] = {
        "v": VERSION,
        "suite": SUITE,
        "hub": sender_record["hub"],
        "from": sender_record["principal"],
        "from_kid": sender_record["kid"],
        "to": recipient_record["principal"],
        "kind": kind,
        "priority": priority,
        "client_id": client_id,
        "created_at": created_at,
        "pad": len(padded),
        "rcpt": sorted(records),
    }
    if deadline is not None:
        rh["deadline"] = deadline
    if in_reply_to is not None:
        rh["in_reply_to"] = dict(in_reply_to)
    _routing(rh)
    rhb = canonical(rh)
    cek = bytearray(secrets.token_bytes(32))
    try:
        ct = AESGCM(bytes(cek)).encrypt(bytes(12), padded, LABELS["aad"] + rhb)
        recipients = [
            {
                "kid": kid,
                "wrap": encode_binary(
                    suite.encrypt(
                        bytes(cek),
                        _kem_public(decode_binary(records[kid]["enc_pub"], PUBLIC_KEY_BYTES)),
                        LABELS["cek"] + hashlib.sha256(rhb).digest(),
                    )
                ),
            }
            for kid in rh["rcpt"]
        ]
    except ValueError:
        raise MeshError("mesh_key_invalid") from None
    finally:
        _wipe(cek)
    header = dict(
        rh,
        ct_sha256=encode_binary(hashlib.sha256(ct).digest()),
        wraps_sha256=encode_binary(hashlib.sha256(canonical(recipients)).digest()),
        rc=retry_commitment(retry_key, rh, payload),
    )
    envelope = dict(
        header, recipients=recipients, ct=encode_binary(ct), sig=sign(header, sender.signing_key, label="sig")
    )
    _require(type(max_envelope_bytes) is int and 0 < max_envelope_bytes <= MAX_ENVELOPE_BYTES, "message_too_large")
    _require(len(canonical(envelope)) <= max_envelope_bytes, "message_too_large")
    return envelope


def open(raw: bytes, *, keys: PrivateKeys, sender_record: dict[str, Any], caller: str, context: str) -> dict[str, Any]:
    """Parse, check context, verify, check digests, unwrap, decrypt, decode.

    This primitive does not accept hub columns, pins or clocks. Slice 5 must
    perform D6 trust, hub lineage, freshness and replay checks around it.
    """
    suite = _backend()
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    envelope = parse_envelope(raw)
    rh = _routing(envelope)
    _require(
        context in {"inbound", "outbound"} and caller == rh["to" if context == "inbound" else "from"],
        "open_context_mismatch",
    )
    _verify_signed_header(envelope, sender_record)
    _require("ct" in envelope and "recipients" in envelope, "content_expired")
    ct = decode_binary(envelope["ct"], rh["pad"] + 16)
    recipients = envelope["recipients"]
    _require(isinstance(recipients, list) and len(recipients) == len(rh["rcpt"]))
    wraps = {}
    for kid, entry in zip(rh["rcpt"], recipients, strict=True):
        _require(isinstance(entry, dict) and set(entry) == {"kid", "wrap"} and entry["kid"] == kid)
        wraps[kid] = decode_binary(entry["wrap"], WRAP_BYTES)
    _require(
        hmac.compare_digest(hashlib.sha256(ct).digest(), decode_binary(envelope["ct_sha256"], 32))
        and hmac.compare_digest(
            hashlib.sha256(canonical(recipients)).digest(), decode_binary(envelope["wraps_sha256"], 32)
        ),
        "envelope_digest_mismatch",
    )
    private, enc_pub = _kem_private(keys.kem_seed)
    sig_pub = Ed25519PrivateKey.from_private_bytes(keys.signing_key).public_key().public_bytes_raw()
    kid = key_id(sig_pub, enc_pub)
    _require(kid in wraps, "recipient_key_missing")
    own_sender_wrap = context == "outbound" or rh["from"] == rh["to"]
    _require((kid == rh["from_kid"]) == own_sender_wrap, "mesh_key_invalid")
    rhb = canonical(rh)
    cek = bytearray()
    try:
        cek.extend(suite.decrypt(wraps[kid], private, LABELS["cek"] + hashlib.sha256(rhb).digest()))
        _require(len(cek) == 32, "envelope_decryption_failed")
        padded = AESGCM(bytes(cek)).decrypt(bytes(12), ct, LABELS["aad"] + rhb)
        return decode_payload(padded)
    except (ValueError, InvalidTag):
        raise MeshError("envelope_decryption_failed") from None
    finally:
        _wipe(cek)
