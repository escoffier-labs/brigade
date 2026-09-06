"""Bounded, strict JSON and DSSE input helpers for attestation consumers."""

from __future__ import annotations

import base64
import binascii
import json
import math
import os
import re
import stat
from collections.abc import Iterator, Mapping
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


def _base_string_characters(value: str) -> Iterator[str]:
    """Yield string contents without dispatching to a str subclass override."""
    for index in range(str.__len__(value)):
        yield str.__getitem__(value, index)


def _require_unicode_scalars(value: str, *, label: str) -> None:
    if any(0xD800 <= ord(char) <= 0xDFFF for char in _base_string_characters(value)):
        raise AttestationInputError(f"{label} contains invalid Unicode scalars")


def _bounded_utf8_size(value: str, *, limit: int, label: str) -> int:
    """Measure UTF-8 bytes without allocating an encoded copy beyond a limit."""
    size = 0
    for char in _base_string_characters(value):
        codepoint = ord(char)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise AttestationInputError(f"{label} contains invalid Unicode scalars")
        size += 1 if codepoint <= 0x7F else 2 if codepoint <= 0x7FF else 3 if codepoint <= 0xFFFF else 4
        if size > limit:
            raise AttestationInputError("JSON document exceeds byte limit")
    return size


def _bounded_json_string_size(value: str, *, limit: int, label: str) -> int:
    """Measure JSON string bytes before serializing an untrusted scalar."""
    size = 2
    if size > limit:
        raise AttestationInputError("JSON document exceeds byte limit")
    for char in _base_string_characters(value):
        codepoint = ord(char)
        if 0xD800 <= codepoint <= 0xDFFF:
            raise AttestationInputError(f"{label} contains invalid Unicode scalars")
        if codepoint in {0x22, 0x5C, 0x08, 0x09, 0x0A, 0x0C, 0x0D}:
            size += 2
        elif codepoint <= 0x1F:
            size += 6
        else:
            size += 1 if codepoint <= 0x7F else 2 if codepoint <= 0x7FF else 3 if codepoint <= 0xFFFF else 4
        if size > limit:
            raise AttestationInputError("JSON document exceeds byte limit")
    return size


def _dump_json_scalar(value: Any) -> str:
    try:
        return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError) as exc:
        raise AttestationInputError("JSON value cannot be serialized") from exc


def _snapshot_json_string(value: str, *, label: str, limit: int) -> str:
    """Return an exact ``str`` after validating its bounded JSON representation."""
    _bounded_json_string_size(value, limit=limit, label=label)
    return bytes.decode(str.encode(value, "utf-8", "strict"), "utf-8", "strict")


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
            raise AttestationInputError("JSON contains duplicate property names")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise AttestationInputError(f"JSON number {value!r} is not finite")


def validate_json_value(value: Any) -> Any:
    """Validate and snapshot a Python value under the JSON input contract."""
    nodes = 0
    encoded_bytes = 0
    active: set[int] = set()

    def reserve_json_bytes(token: str) -> None:
        nonlocal encoded_bytes
        encoded_bytes += len(token.encode("utf-8"))
        if encoded_bytes > MAX_JSON_BYTES:
            raise AttestationInputError("JSON document exceeds byte limit")

    def reserve_json_string(item: str, *, label: str) -> str:
        snapshot = _snapshot_json_string(item, label=label, limit=MAX_JSON_BYTES - encoded_bytes)
        reserve_json_bytes(_dump_json_scalar(snapshot))
        return snapshot

    def visit(item: Any, *, label: str, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_JSON_NODES:
            raise AttestationInputError("JSON value node count exceeds limit")
        if item is None or isinstance(item, bool):
            reserve_json_bytes("null" if item is None else "true" if item else "false")
            return item
        if isinstance(item, str):
            return reserve_json_string(item, label=label)
        if isinstance(item, int):
            int_snapshot = int.__add__(item, 0)
            minimum_digits = ((max(int_snapshot.bit_length() - 1, 0) * 30_102) // 100_000) + 1
            if minimum_digits + (1 if int_snapshot < 0 else 0) > MAX_JSON_BYTES - encoded_bytes:
                raise AttestationInputError("JSON document exceeds byte limit")
            reserve_json_bytes(_dump_json_scalar(int_snapshot))
            return int_snapshot
        if isinstance(item, float):
            float_snapshot = float.__mul__(item, 1.0)
            if not math.isfinite(float_snapshot):
                raise AttestationInputError("JSON number is not finite")
            reserve_json_bytes(_dump_json_scalar(float_snapshot))
            return float_snapshot
        if isinstance(item, Mapping):
            container_depth = depth + 1
            if container_depth > MAX_JSON_DEPTH:
                raise AttestationInputError("JSON nesting depth exceeds limit")
            identity = id(item)
            if identity in active:
                raise AttestationInputError("JSON mapping contains a cycle")
            active.add(identity)
            try:
                reserve_json_bytes("{")
                mapping_snapshot: dict[str, Any] = {}
                try:
                    for index, (key, child) in enumerate(item.items()):
                        if not isinstance(key, str):
                            raise AttestationInputError("JSON mapping keys must be strings")
                        if index:
                            reserve_json_bytes(",")
                        key_snapshot = reserve_json_string(key, label="JSON mapping key")
                        if key_snapshot in mapping_snapshot:
                            raise AttestationInputError("JSON contains duplicate property names")
                        reserve_json_bytes(":")
                        mapping_snapshot[key_snapshot] = visit(child, label="JSON value", depth=container_depth)
                except AttestationInputError:
                    raise
                except Exception as exc:
                    raise AttestationInputError("JSON mapping cannot be iterated") from exc
                reserve_json_bytes("}")
            finally:
                active.remove(identity)
            return mapping_snapshot
        if isinstance(item, list):
            container_depth = depth + 1
            if container_depth > MAX_JSON_DEPTH:
                raise AttestationInputError("JSON nesting depth exceeds limit")
            identity = id(item)
            if identity in active:
                raise AttestationInputError("JSON array contains a cycle")
            active.add(identity)
            try:
                reserve_json_bytes("[")
                array_snapshot: list[Any] = []
                for index, child in enumerate(list.__iter__(item)):
                    if index:
                        reserve_json_bytes(",")
                    array_snapshot.append(visit(child, label="JSON value", depth=container_depth))
                reserve_json_bytes("]")
            finally:
                active.remove(identity)
            return array_snapshot
        raise AttestationInputError(f"JSON value has unsupported type {type(item).__name__}")

    return visit(value, label="JSON value", depth=0)


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
        _bounded_utf8_size(source, limit=max_bytes, label="JSON document")
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
    except AttestationInputError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as exc:
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
    max_encoded_bytes = ((max_bytes + 2) // 3) * 4
    if len(value) > max_encoded_bytes:
        raise AttestationInputError(f"{label} exceeds decoded byte limit")
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError as exc:
        raise AttestationInputError(f"{label} is not valid base64") from exc
    if not _BASE64_RE.fullmatch(value):
        raise AttestationInputError(f"{label} is not valid base64")
    if (b"+" in encoded or b"/" in encoded) and (b"-" in encoded or b"_" in encoded):
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
