"""Bounded, versioned master-roster document contract for fleet synchronization."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

SCHEMA = "brigade.fleet_master_roster.v1"
MAX_DOCUMENT_BYTES = 262144
MAX_SEATS = 128
MAX_TEXT = 160
# Keep revisions in the interoperable integer range used by JSON implementations.
MAX_REVISION = (1 << 53) - 1
_MAX_JSON_INTEGER_DIGITS = len(str(MAX_REVISION))
_SEAT_NAME = re.compile(r"[a-z][a-z0-9._-]{0,63}")
_CLI_NAME = re.compile(r"[a-z][a-z0-9._-]{0,63}")
# Components exclude the separator, so matching cannot repartition a component.
_MODEL_COMPONENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
_MODEL_IDENTIFIER = re.compile(rf"{_MODEL_COMPONENT}(?:/{_MODEL_COMPONENT}){{0,3}}(?:\[[0-9]{{1,4}}[km]?\])?")
PURPOSES = frozenset({"implementation", "review", "research", "planning", "triage", "documentation", "security-review"})
CAVEATS = frozenset({"focused-verification", "authentication-required", "read-only", "manual-approval"})
_SURROGATE = re.compile("[\\ud800-\\udfff]")
_TOP_FIELDS = frozenset({"schema", "revision", "seats"})
_SEAT_FIELDS = frozenset({"name", "cli", "model", "purpose", "caveats", "fallbacks", "enabled"})


class MasterRosterError(ValueError):
    """A master-roster document does not match the allowed format."""


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"} for character in value)


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MasterRosterError("duplicate JSON key")
        result[key] = value
    return result


def _bounded_json_integer(value: str) -> int:
    """Parse bounded JSON integers consistently across supported Python versions."""
    digits = value.lstrip("-")
    if len(digits) > _MAX_JSON_INTEGER_DIGITS:
        raise ValueError("JSON integer exceeds the supported digit limit")
    return int(value)


def _text(value: Any, field: str) -> str:
    # Exact str only: a subclass could override __eq__/__hash__ to pass the
    # closed-vocabulary checks and then serialize a different value.
    if type(value) is not str or not value.strip():
        raise MasterRosterError(f"{field} must be a non-empty string")
    if len(value) > MAX_TEXT:
        raise MasterRosterError(f"{field} must be at most {MAX_TEXT} characters")
    if _SURROGATE.search(value):
        raise MasterRosterError(f"{field} must contain valid Unicode scalar values")
    if _has_control(value):
        raise MasterRosterError(f"{field} must be without controls or separators")
    return value


def _identifier(value: Any, field: str, pattern: re.Pattern[str], label: str) -> str:
    text = _text(value, field)
    if not pattern.fullmatch(text):
        raise MasterRosterError(f"{field} must be {label}")
    return text


def _choice(value: Any, field: str, allowed: frozenset[str]) -> str:
    text = _text(value, field)
    if text not in allowed:
        raise MasterRosterError(f"{field} must be an allowed value: {', '.join(sorted(allowed))}")
    return text


def _unknown_fields(value: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    if any(type(key) is not str for key in value):
        raise MasterRosterError(f"{where} keys must be strings")
    if any(_SURROGATE.search(key) for key in value):
        raise MasterRosterError(f"{where} keys must contain valid Unicode scalar values")
    if any(_has_control(key) for key in value):
        raise MasterRosterError(f"{where} keys must not contain controls or separators")
    unknown = set(value).difference(allowed)
    if unknown:
        raise MasterRosterError(f"unknown {where} field(s)")


def parse(raw: Mapping[str, Any] | bytes | str) -> dict[str, Any]:
    """Validate and copy a bounded v1 roster, returning its canonical SHA-256."""
    if isinstance(raw, (bytes, str)):
        try:
            encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
            if len(encoded) > MAX_DOCUMENT_BYTES:
                raise MasterRosterError(f"document must be at most {MAX_DOCUMENT_BYTES} bytes")
            decoded = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=_object_without_duplicate_keys,
                parse_int=_bounded_json_integer,
            )
        except MasterRosterError:
            raise
        except (UnicodeError, ValueError, RecursionError) as exc:
            raise MasterRosterError("document must be valid UTF-8 JSON") from exc
        if not isinstance(decoded, dict):
            raise MasterRosterError("document must be a JSON object")
        source: Mapping[str, Any] = decoded
    elif isinstance(raw, Mapping):
        source = raw
    else:
        raise MasterRosterError("document must be an object")

    _unknown_fields(source, _TOP_FIELDS, "document")
    if source.get("schema") != SCHEMA:
        raise MasterRosterError(f"schema must be {SCHEMA}")
    revision = source.get("revision")
    if type(revision) is not int or not 1 <= revision <= MAX_REVISION:
        raise MasterRosterError(f"revision must be a positive integer at most {MAX_REVISION}")
    seats = source.get("seats")
    if not isinstance(seats, list) or len(seats) > MAX_SEATS:
        raise MasterRosterError(f"seats must be a list of at most {MAX_SEATS} entries")

    clean_seats: list[dict[str, Any]] = []
    names: set[str] = set()
    for index, raw_seat in enumerate(seats):
        where = f"seats[{index}]"
        if not isinstance(raw_seat, Mapping):
            raise MasterRosterError(f"{where} must be an object")
        _unknown_fields(raw_seat, _SEAT_FIELDS, where)
        name = _identifier(raw_seat.get("name"), f"{where}.name", _SEAT_NAME, "a valid seat name")
        cli = _identifier(raw_seat.get("cli"), f"{where}.cli", _CLI_NAME, "a single executable name")
        model = _identifier(raw_seat.get("model"), f"{where}.model", _MODEL_IDENTIFIER, "a model identifier")
        purpose = _choice(raw_seat.get("purpose"), f"{where}.purpose", PURPOSES)
        if name in names:
            raise MasterRosterError(f"duplicate seat name: {name}")
        names.add(name)
        caveats = raw_seat.get("caveats", [])
        fallbacks = raw_seat.get("fallbacks", [])
        if not isinstance(caveats, list) or len(caveats) > 16:
            raise MasterRosterError(f"{where}.caveats must be a list of at most 16 entries")
        if not isinstance(fallbacks, list) or len(fallbacks) > MAX_SEATS:
            raise MasterRosterError(f"{where}.fallbacks must be a list of seat names")
        if type(raw_seat.get("enabled")) is not bool:
            raise MasterRosterError(f"{where}.enabled must be a boolean")
        clean_caveats = [_choice(item, f"{where}.caveats[{i}]", CAVEATS) for i, item in enumerate(caveats)]
        if len(set(clean_caveats)) != len(clean_caveats):
            raise MasterRosterError(f"{where}.caveats must not contain duplicate values")
        clean_seats.append(
            {
                "name": name,
                "cli": cli,
                "model": model,
                "purpose": purpose,
                "caveats": clean_caveats,
                "fallbacks": [
                    _identifier(item, f"{where}.fallbacks[{i}]", _SEAT_NAME, "a valid seat name")
                    for i, item in enumerate(fallbacks)
                ],
                "enabled": raw_seat["enabled"],
            }
        )
    seats_by_name = {seat["name"]: seat for seat in clean_seats}
    for seat in clean_seats:
        if len(set(seat["fallbacks"])) != len(seat["fallbacks"]):
            raise MasterRosterError(f"{seat['name']} has duplicate fallback seats")
        for fallback in seat["fallbacks"]:
            if not _SEAT_NAME.fullmatch(fallback) or fallback not in names or fallback == seat["name"]:
                raise MasterRosterError(f"{seat['name']} has an invalid fallback seat: {fallback}")
            if seats_by_name[fallback]["fallbacks"]:
                raise MasterRosterError(f"{seat['name']} fallback target {fallback!r} must not define its own fallback")

    document = {"schema": SCHEMA, "revision": revision, "seats": clean_seats}
    try:
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    except (ValueError, RecursionError) as exc:
        raise MasterRosterError("document cannot be represented as bounded JSON") from exc
    if len(canonical) > MAX_DOCUMENT_BYTES:
        raise MasterRosterError(f"document must be at most {MAX_DOCUMENT_BYTES} bytes")
    result = copy.deepcopy(document)
    result["sha256"] = "sha256:" + hashlib.sha256(canonical).hexdigest()
    return result
