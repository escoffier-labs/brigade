"""Parent-held authority broker: domain-separated HMAC for store and capability.

This is the single mint/verify surface for two secret lifetimes:

* store key: persisted 0600 under the operator config root, used to MAC
  directory-authority records across invocations
* capability secret: 32 fresh bytes in parent RSS for one operator command,
  never written to disk and never placed in a child env or inherited fd

No secret means no authority. Verification is fail-closed. A same-UID child
that can read the persisted store key can still forge a store envelope; that
residual is documented and encoded in the residual-honesty test. The
per-invocation capability secret is not persisted, so a scanner that invokes
``miseledger trust review`` directly cannot mint a valid trust transition.

Parent-to-engine hand-off (closes #1029)
---------------------------------------
The operator-initiated Python parent (``trust_gate.notify_miseledger_trust``)
generates the capability secret with ``secrets.token_bytes(32)`` and keeps it
in local memory. It mints a capability bound to ``{item_id, from_digest,
transition, nonce, expiry}`` and writes one JSON object to the engine child's
stdin, then closes the write end:

    {
      "v": 1,
      "kind": "brigade.authority.capability-handoff.v1",
      "secret": "<64 hex>",
      "capability": { ...fields, "mac": "<64 hex>" }
    }

The secret never enters ``env=``, argv, or any path ``CreateProcess`` /
``subprocess`` hands the child. Scanner children are launched with a scrubbed
env and ``stdin=DEVNULL``, so they have neither the secret nor this pipe.
Calling the engine binary directly is unsupported; ``brigade evidence trust
review`` is the only path that mints.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from collections.abc import Mapping
from typing import Any

STORE_DOMAIN = b"brigade.authority.store.v1\x00"
SEQUENCE_DOMAIN = b"brigade.authority.sequence.v1\x00"
CAPABILITY_DOMAIN = b"brigade.authority.capability.v1\x00"
HANDOFF_KIND = "brigade.authority.capability-handoff.v1"
CAPABILITY_TTL_SECONDS = 120
CAPABILITY_VERSION = 1


def canonical_dumps(payload: Mapping[str, Any]) -> bytes:
    """Return stable JSON bytes shared with the Go engine verifier."""

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def mac(secret: bytes, domain: bytes, payload: bytes) -> str:
    if not secret:
        raise ValueError("authority broker refuses to MAC without a secret")
    if not domain:
        raise ValueError("authority broker refuses to MAC without a domain prefix")
    return hmac.new(secret, domain + payload, hashlib.sha256).hexdigest()


def verify_mac(secret: bytes, domain: bytes, payload: bytes, expected: str) -> bool:
    if not secret or not isinstance(expected, str) or not expected:
        return False
    computed = mac(secret, domain, payload)
    if len(computed) != len(expected):
        return False
    return hmac.compare_digest(computed, expected)


def store_mac_payload(record: Mapping[str, Any], sequence: int) -> bytes:
    target = record.get("target")
    if not isinstance(target, str) or not target:
        raise ValueError("authority store record is missing target")
    record_digest = hashlib.sha256(canonical_dumps(record)).hexdigest()
    target_digest = hashlib.sha256(target.encode("utf-8")).hexdigest()
    return f"{record_digest}\x00{int(sequence)}\x00{target_digest}".encode("utf-8")


def sign_store_record(secret: bytes, record: Mapping[str, Any], sequence: int, key_id: str) -> dict[str, Any]:
    payload = store_mac_payload(record, sequence)
    return {
        "envelope_version": 1,
        "signature": {
            "alg": "HMAC-SHA256",
            "key_id": key_id,
            "sequence": int(sequence),
            "mac": mac(secret, STORE_DOMAIN, payload),
        },
        "record": dict(record),
    }


def verify_store_envelope(secret: bytes, envelope: Mapping[str, Any], key_id: str) -> dict[str, Any]:
    if envelope.get("envelope_version") != 1:
        raise ValueError("authority store envelope_version is not 1")
    signature = envelope.get("signature")
    record = envelope.get("record")
    if not isinstance(signature, Mapping) or not isinstance(record, Mapping):
        raise ValueError("authority store envelope is malformed")
    if signature.get("alg") != "HMAC-SHA256":
        raise ValueError("authority store envelope uses an unknown MAC algorithm")
    if signature.get("key_id") != key_id:
        raise ValueError("authority store envelope key_id does not match the loaded key")
    sequence = signature.get("sequence")
    if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence < 1:
        raise ValueError("authority store envelope sequence is malformed")
    expected = signature.get("mac")
    if not isinstance(expected, str):
        raise ValueError("authority store envelope MAC is missing")
    payload = store_mac_payload(record, sequence)
    if not verify_mac(secret, STORE_DOMAIN, payload, expected):
        raise ValueError("authority store envelope MAC mismatch")
    return dict(record)


def new_capability_secret() -> bytes:
    return secrets.token_bytes(32)


def mint_trust_capability(
    secret: bytes,
    *,
    item_id: str,
    from_digest: str,
    to_label: str,
    mark_injection_clean: bool,
    ttl_seconds: int = CAPABILITY_TTL_SECONDS,
    now: float | None = None,
    nonce: str | None = None,
    mint_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Mint a capability bound to one item, digest, and transition."""

    if not secret:
        raise ValueError("authority broker refuses to mint without a secret")
    if not item_id or not from_digest or to_label not in {"reviewed", "verified"}:
        raise ValueError("trust capability bindings are incomplete")
    issued_at = time.time() if now is None else now
    body: dict[str, Any] = {
        "v": CAPABILITY_VERSION,
        "item_id": item_id,
        "from_digest": from_digest.lower(),
        "transition": {
            "to_label": to_label,
            "mark_injection_clean": bool(mark_injection_clean),
        },
        "nonce": nonce or secrets.token_hex(16),
        "expiry": int(issued_at) + int(ttl_seconds),
        "mint_meta": dict(mint_meta) if mint_meta is not None else {"pid": os.getpid(), "time": int(issued_at)},
    }
    signed = dict(body)
    signed["mac"] = mac(secret, CAPABILITY_DOMAIN, canonical_dumps(capability_mac_body(body)))
    return signed


def capability_mac_body(capability: Mapping[str, Any]) -> dict[str, Any]:
    """Return the MAC-covered fields. mint_meta is audit-only and excluded."""

    transition = capability.get("transition")
    if not isinstance(transition, Mapping):
        raise ValueError("trust capability transition is malformed")
    return {
        "v": capability.get("v"),
        "item_id": capability.get("item_id"),
        "from_digest": capability.get("from_digest"),
        "transition": {
            "mark_injection_clean": bool(transition.get("mark_injection_clean")),
            "to_label": transition.get("to_label"),
        },
        "nonce": capability.get("nonce"),
        "expiry": capability.get("expiry"),
    }


def capability_body_without_mac(capability: Mapping[str, Any]) -> dict[str, Any]:
    body = {key: value for key, value in capability.items() if key != "mac"}
    if body.get("v") != CAPABILITY_VERSION:
        raise ValueError("trust capability version is not 1")
    return body


def verify_trust_capability(
    secret: bytes,
    capability: Mapping[str, Any],
    *,
    item_id: str,
    from_digest: str,
    to_label: str,
    mark_injection_clean: bool,
    now: float | None = None,
) -> dict[str, Any]:
    """Verify a capability against the in-memory secret and requested transition."""

    if not secret:
        raise ValueError("trust capability secret is missing")
    body = capability_body_without_mac(capability)
    expected_mac = capability.get("mac")
    if not isinstance(expected_mac, str) or not verify_mac(
        secret, CAPABILITY_DOMAIN, canonical_dumps(capability_mac_body(body)), expected_mac
    ):
        raise ValueError("trust capability MAC is invalid")
    expiry = body.get("expiry")
    if not isinstance(expiry, int) or isinstance(expiry, bool):
        raise ValueError("trust capability expiry is malformed")
    current = time.time() if now is None else now
    if int(current) >= expiry:
        raise ValueError("trust capability has expired")
    if body.get("item_id") != item_id:
        raise ValueError("trust capability item_id does not match the requested item")
    if str(body.get("from_digest") or "").lower() != from_digest.lower():
        raise ValueError("trust capability from_digest does not match the requested digest")
    transition = body.get("transition")
    if not isinstance(transition, Mapping):
        raise ValueError("trust capability transition is malformed")
    if transition.get("to_label") != to_label:
        raise ValueError("trust capability to_label does not match the requested transition")
    if bool(transition.get("mark_injection_clean")) != bool(mark_injection_clean):
        raise ValueError("trust capability mark_injection_clean does not match the requested transition")
    nonce = body.get("nonce")
    if not isinstance(nonce, str) or len(nonce) < 16:
        raise ValueError("trust capability nonce is malformed")
    return body


def encode_handoff(secret: bytes, capability: Mapping[str, Any]) -> bytes:
    """Encode the stdin hand-off. The secret stays off argv and env."""

    payload = {
        "v": 1,
        "kind": HANDOFF_KIND,
        "secret": secret.hex(),
        "capability": dict(capability),
    }
    return canonical_dumps(payload) + b"\n"


def decode_handoff(raw: bytes | str) -> tuple[bytes, dict[str, Any]]:
    text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    payload = json.loads(text)
    if not isinstance(payload, dict) or payload.get("kind") != HANDOFF_KIND or payload.get("v") != 1:
        raise ValueError("capability hand-off is malformed")
    secret_hex = payload.get("secret")
    capability = payload.get("capability")
    if not isinstance(secret_hex, str) or not isinstance(capability, dict):
        raise ValueError("capability hand-off is missing secret or capability")
    secret = bytes.fromhex(secret_hex)
    if len(secret) != 32:
        raise ValueError("capability hand-off secret must be 32 bytes")
    return secret, capability
