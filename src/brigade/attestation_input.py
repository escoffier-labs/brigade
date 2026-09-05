"""Bounded, strict JSON and DSSE input helpers for attestation consumers."""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from . import dirfd

MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_SIGNATURE_BYTES = 64 * 1024
MAX_JSON_DEPTH = 64
MAX_JSON_NODES = 100_000

_BASE64_RE = re.compile(r"[A-Za-z0-9+/_-]*={0,2}\Z")


class AttestationInputError(ValueError):
    """Raised when an attestation input cannot be safely interpreted."""


def _require_unicode_scalars(value: str, *, label: str) -> None:
    try:
        value.encode("utf-8", "strict")
    except UnicodeEncodeError as exc:
        raise AttestationInputError(f"{label} contains invalid Unicode scalars") from exc


def _check_container_depth(text: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise AttestationInputError("JSON nesting depth exceeds limit")
        elif char in "]}":
            depth -= 1


def _duplicate_rejecting_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AttestationInputError(f"JSON contains duplicate property name {key!r}")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise AttestationInputError(f"JSON number {value!r} is not finite")


def validate_json_value(value: Any) -> Any:
    """Validate a Python value under the JSON input contract without rewriting it."""
    nodes = 0
    encoded_bytes = 0
    active: set[int] = set()

    def reserve_json_bytes(token: str) -> None:
        nonlocal encoded_bytes
        encoded_bytes += len(token.encode("utf-8"))
        if encoded_bytes > MAX_JSON_BYTES:
            raise AttestationInputError("JSON document exceeds byte limit")

    def visit(item: Any, *, label: str) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise AttestationInputError("JSON value node count exceeds limit")
        if item is None or isinstance(item, bool):
            reserve_json_bytes(json.dumps(item, separators=(",", ":")))
            return
        if isinstance(item, str):
            _require_unicode_scalars(item, label=label)
            reserve_json_bytes(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            return
        if isinstance(item, int):
            reserve_json_bytes(json.dumps(item, separators=(",", ":")))
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise AttestationInputError("JSON number is not finite")
            reserve_json_bytes(json.dumps(item, allow_nan=False, separators=(",", ":")))
            return
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                raise AttestationInputError("JSON mapping contains a cycle")
            active.add(identity)
            try:
                reserve_json_bytes("{")
                for index, (key, child) in enumerate(item.items()):
                    if not isinstance(key, str):
                        raise AttestationInputError("JSON mapping keys must be strings")
                    _require_unicode_scalars(key, label="JSON mapping key")
                    if index:
                        reserve_json_bytes(",")
                    reserve_json_bytes(json.dumps(key, ensure_ascii=False, separators=(",", ":")))
                    reserve_json_bytes(":")
                    visit(child, label="JSON value")
                reserve_json_bytes("}")
            finally:
                active.remove(identity)
            return
        if isinstance(item, list):
            identity = id(item)
            if identity in active:
                raise AttestationInputError("JSON array contains a cycle")
            active.add(identity)
            try:
                reserve_json_bytes("[")
                for index, child in enumerate(item):
                    if index:
                        reserve_json_bytes(",")
                    visit(child, label="JSON value")
                reserve_json_bytes("]")
            finally:
                active.remove(identity)
            return
        raise AttestationInputError(f"JSON value has unsupported type {type(item).__name__}")

    visit(value, label="JSON value")
    return value


def strict_json_loads(source: str | bytes, *, max_bytes: int = MAX_JSON_BYTES) -> Any:
    """Parse one JSON document with RFC 8259-compatible safety restrictions."""
    if isinstance(source, bytes):
        if len(source) > max_bytes:
            raise AttestationInputError("JSON document exceeds byte limit")
        try:
            text = source.decode("utf-8", "strict")
        except UnicodeDecodeError as exc:
            raise AttestationInputError("JSON document is not valid UTF-8") from exc
    elif isinstance(source, str):
        _require_unicode_scalars(source, label="JSON document")
        raw = source.encode("utf-8", "strict")
        if len(raw) > max_bytes:
            raise AttestationInputError("JSON document exceeds byte limit")
        text = source
    else:
        raise AttestationInputError("JSON document must be text or bytes")

    _check_container_depth(text)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_duplicate_rejecting_object,
            parse_constant=_reject_constant,
        )
    except (json.JSONDecodeError, RecursionError) as exc:
        raise AttestationInputError("JSON document is invalid") from exc
    return validate_json_value(value)


def read_bounded_file(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> bytes:
    """Read a regular final path component through a no-follow descriptor."""
    descriptor = dirfd.open_file_nofollow(path, os.O_RDONLY)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise AttestationInputError("input path is not a regular file")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise AttestationInputError("input file exceeds byte limit")
        return data
    finally:
        os.close(descriptor)


def read_json_object(path: Path, *, max_bytes: int = MAX_JSON_BYTES) -> dict[str, Any]:
    value = strict_json_loads(read_bounded_file(path, max_bytes=max_bytes), max_bytes=max_bytes)
    if not isinstance(value, dict):
        raise AttestationInputError("JSON document must contain an object")
    return value


def decode_dsse_base64(value: str, *, label: str, max_bytes: int) -> bytes:
    """Strictly decode standard or URL-safe DSSE base64."""
    if not isinstance(value, str):
        raise AttestationInputError(f"{label} must be a base64 string")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise AttestationInputError(f"{label} is not valid base64") from exc
    if not _BASE64_RE.fullmatch(value):
        raise AttestationInputError(f"{label} is not valid base64")
    if len(encoded) % 4:
        raise AttestationInputError(f"{label} is not valid base64")
    unpadded_length = len(encoded.rstrip(b"="))
    if unpadded_length % 4 == 1:
        raise AttestationInputError(f"{label} is not valid base64")
    if (unpadded_length * 3) // 4 > max_bytes:
        raise AttestationInputError(f"{label} exceeds decoded byte limit")
    try:
        decoded = base64.b64decode(encoded, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AttestationInputError(f"{label} is not valid base64") from exc
    if len(decoded) > max_bytes:
        raise AttestationInputError(f"{label} exceeds decoded byte limit")
    return decoded


def decode_ssh_signature(value: str) -> str:
    """Accept bounded raw SSH armor or bounded base64-encoded SSH armor."""
    if not isinstance(value, str):
        raise AttestationInputError("signature must be a string")
    cleaned = value.strip()
    if "-----BEGIN SSH SIGNATURE-----" in cleaned:
        _require_unicode_scalars(cleaned, label="signature")
        if len(cleaned.encode("utf-8")) > MAX_SIGNATURE_BYTES:
            raise AttestationInputError("signature exceeds decoded byte limit")
        return cleaned
    decoded = decode_dsse_base64(cleaned, label="signature", max_bytes=MAX_SIGNATURE_BYTES)
    try:
        armor = decoded.decode("utf-8", "strict")
    except UnicodeDecodeError as exc:
        raise AttestationInputError("signature is not UTF-8 SSH armor") from exc
    if "-----BEGIN SSH SIGNATURE-----" not in armor:
        raise AttestationInputError("signature is not SSH armor")
    return armor
