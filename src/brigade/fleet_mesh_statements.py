"""Pure revision-5 signed statements, without acceptance or policy state.

A verified signature proves only the supplied key signed these bytes. It does
not establish operator trust, freshness, endpoint acceptance, membership or an
activated policy. Callers must supply the verification key independently of
untrusted manifest data. Identity verification also binds that explicit key to
the advertised key and derived hub id. Migration is deliberately unsupported.

Parsing and digesting need no crypto extra. Signing and verification retain the
existing crypto core's backend requirements and domain labels; bm1 is unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import re
from datetime import datetime
from typing import Any

from . import fleet_mesh_crypto as crypto

_MAX_SEQUENCE = 2**53 - 1
_COMMON = frozenset({"v", "hub", "seq", "created_at", "sig"})
_FIELDS = {
    "membership": _COMMON | {"members"},
    "manifest": _COMMON | {"keys"},
    "revocation": _COMMON | {"revocations"},
    "hub-identity": frozenset(
        {
            "v",
            "hub_id",
            "hub_sig_pub",
            "endpoint",
            "require_sealed",
            "max_envelope_bytes",
            "envelope_versions",
            "suites",
            "backend",
            "capabilities",
            "created_at",
            "seq",
            "sig",
        }
    ),
}
_NODE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", re.ASCII)
_AGENT = re.compile(r"agent:[a-z][a-z0-9._-]{0,62}", re.ASCII)
_CAPABILITY = re.compile(r"[a-z][a-z0-9_-]{0,63}", re.ASCII)
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", re.ASCII)
_TIME = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:\.[0-9]{3}(?:[0-9]{3})?)?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])",
    re.ASCII,
)
_FINGERPRINT = re.compile(r"[A-Z2-7]{4}(?:-[A-Z2-7]{4}){12}", re.ASCII)


def _require(condition: object, code: str = "statement_malformed") -> None:
    if not condition:
        raise crypto.MeshError(code)


def _limit(kind: str) -> int:
    _require(isinstance(kind, str) and kind in _FIELDS, "statement_kind_unsupported")
    return 16384 if kind == "hub-identity" else 2097152


def _binary(value: Any, size: int) -> bytes:
    try:
        return crypto.decode_binary(value, size)
    except crypto.MeshError:
        raise crypto.MeshError("statement_malformed") from None


def _tag(value: Any, prefix: str) -> None:
    _require(isinstance(value, str) and value.startswith(prefix))
    _binary(value[len(prefix) :], 16)


def _principal(value: Any) -> None:
    _require(isinstance(value, str) and (_NODE.fullmatch(value) or _AGENT.fullmatch(value)))


def _timestamp(value: Any) -> None:
    _require(isinstance(value, str) and _TIME.fullmatch(value))
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise crypto.MeshError("statement_malformed") from None


def _fingerprint(value: Any) -> None:
    _require(isinstance(value, str) and _FINGERPRINT.fullmatch(value))
    text = value.replace("-", "")
    # The last base32 symbol has four unused bits, which must be zero.
    decoded = base64.b32decode(text + "====")
    _require(len(decoded) == 32 and base64.b32encode(decoded).decode("ascii").rstrip("=") == text)


def match_public_key_fingerprint(public_key: bytes, expected_fingerprint: str) -> None:
    """Compare explicit public material, without installing a trust anchor."""
    _fingerprint(expected_fingerprint)
    _require(isinstance(public_key, bytes) and len(public_key) == 32, "mesh_key_invalid")
    _require(
        hmac.compare_digest(crypto.fingerprint(public_key), expected_fingerprint),
        "statement_fingerprint_mismatch",
    )


def normalize_origin(value: str) -> str:
    """Strict D4 HTTP(S) origin normalization, without network or acceptance."""
    code = "statement_origin_invalid"
    _require(isinstance(value, str) and value.isascii() and len(value) <= 4096, code)
    _require(not any(ord(c) <= 32 or ord(c) == 127 or c in "%\\?#@" for c in value), code)
    match = re.fullmatch(r"(https?)://([^/]+)(?:/)?", value, re.IGNORECASE | re.ASCII)
    _require(match is not None, code)
    assert match is not None
    scheme, authority = match.groups()
    scheme = scheme.lower()
    port = None
    if authority.startswith("["):
        match = re.fullmatch(r"\[([^\[\]]+)\](?::([0-9]+))?", authority, re.ASCII)
        _require(match is not None, code)
        assert match is not None
        host, port = match.groups()
        try:
            host = "[" + ipaddress.IPv6Address(host).compressed + "]"
        except ValueError:
            raise crypto.MeshError(code) from None
    else:
        match = re.fullmatch(r"([^:\[\]]+)(?::([0-9]+))?", authority, re.ASCII)
        _require(match is not None, code)
        assert match is not None
        host, port = match.groups()
        _require(len(host) <= 253, code)
        # WHATWG treats a final decimal/hex label, including bare 0x, as an
        # IPv4 candidate. Require strict dotted decimal for that entire host
        # so clients cannot disagree about whether it names an address or DNS.
        final_label = host.rsplit(".", 1)[-1]
        if re.fullmatch(r"[0-9]+|0[xX][0-9a-fA-F]*", final_label, re.ASCII):
            try:
                host = str(ipaddress.IPv4Address(host))
            except ValueError:
                raise crypto.MeshError(code) from None
        else:
            _require(all(_DNS_LABEL.fullmatch(label) for label in host.split(".")), code)
            host = host.lower()
    suffix = ""
    if port is not None:
        _require(len(port) <= 5 and not port.startswith("0") and 1 <= int(port) <= 65535, code)
        if (scheme, port) not in {("https", "443"), ("http", "80")}:
            suffix = ":" + port
    origin = scheme + "://" + host + suffix
    _require(len(origin) <= 2048, code)
    return origin


def _raw_bound(raw: bytes, limit: int) -> None:
    """Scan containers without allocating JSON objects or decoded strings."""
    _require(isinstance(raw, bytes) and len(raw) <= limit + 1024 and raw.isascii() and b"\0" not in raw)
    depth = 0
    quoted = escaped = False
    for c in raw:
        if quoted:
            if escaped:
                escaped = False
            elif c == 92:
                escaped = True
            elif c == 34:
                quoted = False
        elif c == 34:
            quoted = True
        elif c in (91, 123):
            depth += 1
            _require(depth <= 4)
        elif c in (93, 125):
            depth -= 1
            _require(depth >= 0)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _invalid_number(value: str) -> Any:
    raise crypto.MeshError("statement_malformed")


def _integer(value: str) -> int:
    # Reject huge integers before Python's configurable decimal conversion limit.
    _require(len(value) <= 17)
    return int(value)


def _json_types(value: Any, depth: int = 0) -> None:
    if isinstance(value, str):
        _require(value.isascii())
    elif type(value) in (int, bool):
        pass
    elif type(value) is dict:
        _require(depth < 4)
        for key, item in value.items():
            _require(isinstance(key, str) and key.isascii())
            _json_types(item, depth + 1)
    elif type(value) is list:
        _require(depth < 4)
        for item in value:
            _json_types(item, depth + 1)
    else:
        _require(False)


def _fields(value: Any, fields: frozenset[str]) -> None:
    _require(type(value) is dict and value.keys() == fields)


def _ordered(values: Any, limit: int) -> None:
    _require(type(values) is list and len(values) <= limit)


def _identity(value: dict[str, Any]) -> None:
    public = _binary(value["hub_sig_pub"], 32)
    expected_hub = "h_" + crypto.encode_binary(hashlib.sha256(crypto.LABELS["hub-id"] + public).digest()[:16])
    _require(value["hub_id"] == expected_hub)
    try:
        origin = normalize_origin(value["endpoint"])
    except crypto.MeshError:
        raise crypto.MeshError("statement_malformed") from None
    _require(value["endpoint"] == origin)
    _require(type(value["require_sealed"]) is bool)
    maximum = value["max_envelope_bytes"]
    _require(type(maximum) is int and 1 <= maximum <= 98304)
    versions = value["envelope_versions"]
    _require(type(versions) is list and len(versions) == 1 and type(versions[0]) is int and versions[0] == 1)
    _require(value["suites"] == ["bm1"] and value["backend"] in ("full", "hub-lite"))
    if value["backend"] == "hub-lite":
        _require(value["require_sealed"] and maximum <= 32768)
    capabilities = value["capabilities"]
    _ordered(capabilities, 32)
    for name in capabilities:
        _require(isinstance(name, str) and _CAPABILITY.fullmatch(name))
    _require(capabilities == sorted(set(capabilities)))


def _validate(value: Any, kind: str, *, signed: bool) -> bytes:
    limit = _limit(kind)
    _json_types(value)
    _fields(value, _FIELDS[kind] if signed else _FIELDS[kind] - {"sig"})
    _require(type(value["v"]) is int and value["v"] == 1)
    _require(type(value["seq"]) is int and 0 <= value["seq"] <= _MAX_SEQUENCE)
    _timestamp(value["created_at"])
    if signed:
        _binary(value["sig"], 64)
    if kind == "hub-identity":
        _identity(value)
    else:
        _tag(value["hub"], "h_")
        if kind == "membership":
            members = value["members"]
            _ordered(members, 4096)
            for member in members:
                _principal(member)
            _require(members == sorted(set(members)))
        elif kind == "manifest":
            entries = value["keys"]
            _ordered(entries, 4096)
            order = []
            for entry in entries:
                _fields(entry, frozenset({"principal", "hub", "kid", "fingerprint"}))
                _principal(entry["principal"])
                _require(entry["hub"] == value["hub"])
                _tag(entry["kid"], "k1_")
                _fingerprint(entry["fingerprint"])
                order.append((entry["hub"], entry["principal"], entry["kid"]))
            _require(order == sorted(order))
            _require(len({e["principal"] for e in entries}) == len(entries))
            _require(len({e["kid"] for e in entries}) == len(entries))
        else:
            entries = value["revocations"]
            _ordered(entries, 8192)
            revocation_order = []
            for entry in entries:
                _fields(entry, frozenset({"principal", "kid", "compromised_from"}))
                _principal(entry["principal"])
                _tag(entry["kid"], "k1_")
                _timestamp(entry["compromised_from"])
                revocation_order.append((entry["principal"], entry["kid"]))
            _require(revocation_order == sorted(revocation_order))
            _require(len({e["kid"] for e in entries}) == len(entries))
    complete = value if signed else dict(value, sig="A" * 86)
    encoded = crypto.canonical(complete)
    _require(len(encoded) <= limit)
    return encoded


def parse_statement(raw: bytes, *, expected_kind: str) -> dict[str, Any]:
    """Bound and validate one complete object. This does not verify its signature."""
    limit = _limit(expected_kind)
    _raw_bound(raw, limit)
    try:
        value = json.loads(
            raw,
            object_pairs_hook=_pairs,
            parse_float=_invalid_number,
            parse_constant=_invalid_number,
            parse_int=_integer,
        )
    except (ValueError, RecursionError):
        raise crypto.MeshError("statement_malformed") from None
    _validate(value, expected_kind, signed=True)
    return value


def sign_statement(unsigned: dict[str, Any], signing_key: bytes, *, expected_kind: str) -> dict[str, Any]:
    """Validate and sign an exact unsigned schema, without publishing anything."""
    _validate(unsigned, expected_kind, signed=False)
    # Copy the validated structure before primitive use; never mutate caller data.
    value = json.loads(crypto.canonical(unsigned))
    signature = crypto.sign(value, signing_key, label=expected_kind)
    if expected_kind == "hub-identity":
        try:
            crypto.verify(value, signature, _binary(value["hub_sig_pub"], 32), label=expected_kind)
        except crypto.MeshError as exc:
            if exc.code == "envelope_signature_invalid":
                raise crypto.MeshError("statement_signer_mismatch") from None
            raise
    value["sig"] = signature
    return value


def verify_statement(raw: bytes, *, expected_kind: str, public_key: bytes) -> dict[str, Any]:
    """Verify with an explicit caller-supplied key, without accepting policy."""
    value = parse_statement(raw, expected_kind=expected_kind)
    _require(isinstance(public_key, bytes) and len(public_key) == 32, "mesh_key_invalid")
    if expected_kind == "hub-identity":
        _require(hmac.compare_digest(_binary(value["hub_sig_pub"], 32), public_key), "statement_signer_mismatch")
    unsigned = {k: v for k, v in value.items() if k != "sig"}
    try:
        crypto.verify(unsigned, value["sig"], public_key, label=expected_kind)
    except crypto.MeshError as exc:
        if exc.code == "envelope_signature_invalid":
            raise crypto.MeshError("statement_signature_invalid") from None
        raise
    return value


def statement_digest(raw: bytes, *, expected_kind: str) -> bytes:
    """Full signed canonical SHA-256, without implying signature validity."""
    return hashlib.sha256(crypto.canonical(parse_statement(raw, expected_kind=expected_kind))).digest()
