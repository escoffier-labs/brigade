"""Safe, versioned master-roster document contract for fleet synchronization."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from .worklore_validate import contains_credential

SCHEMA = "brigade.fleet_master_roster.v1"
MAX_DOCUMENT_BYTES = 262144
MAX_SEATS = 128
MAX_TEXT = 512
_SEAT_NAME = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_CLI_NAME = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_PROVIDER_MODEL = re.compile(r"^[A-Za-z0-9_-]+/[A-Za-z0-9._:-]+$")
_DANGEROUS_VALUE = re.compile(
    r"(?:(?<![A-Za-z0-9_])sk_(?:live|test)_[A-Za-z0-9]+|(?<![A-Za-z0-9_])sk-[A-Za-z0-9_-]{12,}|github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{16,}|"
    r"\bBearer\s+\S+|\b(?:Proxy-)?Authorization\s*:\s*Basic\s+"
    r"[A-Za-z0-9+/]{4,}={0,2}(?![A-Za-z0-9+/=])|"
    r"(?<![A-Za-z0-9])[A-Z_][A-Z0-9_]*\s*=|"
    r"\bauth(?:entication)?[ _-]profile(?:\s*[:=]\s*|\s+)"
    r"(?:[A-Za-z0-9_.-]+|\"[A-Za-z0-9_.-]+\"|'[A-Za-z0-9_.-]+'|`[A-Za-z0-9_.-]+`|\[[A-Za-z0-9_.-]+\]|\([A-Za-z0-9_.-]+\))|"
    r"(?:[A-Za-z0-9_.-]+|\"[A-Za-z0-9_.-]+\"|'[A-Za-z0-9_.-]+'|`[A-Za-z0-9_.-]+`|\[[A-Za-z0-9_.-]+\]|\([A-Za-z0-9_.-]+\))\s+auth(?:entication)?[ _-]profile\b|"
    r"\b(?:password|passwd|passphrase|secret|api[_ -]?key|access[_ -]?token|"
    r"client[_ -]?secret|private[_ -]?key)\s*[:=]\s*\S+|"
    r"(?:^|[^A-Za-z0-9_.-])(?:[A-Za-z]:(?:[/\\]|[^\s])|%USERPROFILE%))",
    re.IGNORECASE,
)
_ROOTED_PATH = re.compile(r"(?:^|[^A-Za-z0-9_.-])(?:~(?:[A-Za-z0-9_.-]+)?[/\\]|[/\\]|\.{1,2}[/\\])(?=[A-Za-z0-9_.~-])")
_HTTP_URL = re.compile(r"\bhttps?://[^\s<>\"']+", re.IGNORECASE)
_RELATIVE_PATH = re.compile(
    r"(?<![A-Za-z0-9_.-])(?P<path>(?:[A-Za-z0-9_.~-]+[/\\])+[A-Za-z0-9_.~-]+)(?![A-Za-z0-9_.~-])"
)
_PATH_CONTEXT = re.compile(
    r"\b(?:run|execute|inspect|open|read|write|load|save|path|file|directory|folder|script|repository|repo)\s+$",
    re.IGNORECASE,
)
_FILESYSTEM_COMPONENTS = frozenset(
    {
        "automation",
        "bin",
        "boot",
        "cache",
        "checks",
        "config",
        "credentials",
        "dev",
        "docs",
        "etc",
        "home",
        "lib",
        "lib64",
        "logs",
        "media",
        "mnt",
        "opt",
        "passwd",
        "private",
        "proc",
        "root",
        "run",
        "scripts",
        "sbin",
        "secret",
        "secrets",
        "srv",
        "sys",
        "tests",
        "tmp",
        "usr",
        "var",
        "workspace",
        "project",
        "src",
    }
)
_EXTENSIONLESS_FILES = frozenset(
    {"containerfile", "dockerfile", "gnumakefile", "makefile", "justfile", "procfile", "readme"}
)
_PRIVATE_KEY_FILES = frozenset({"id_dsa", "id_ecdsa", "id_ed25519", "id_rsa"})
_FILE_SUFFIX = re.compile(r"\.[A-Za-z]{2,8}$")
_MODEL_PATH_SUFFIX = re.compile(
    r"\.(?:7z|avif|bmp|bz2|c|cfg|conf|cpp|cs|css|csv|docx|exe|gif|go|gz|h|hpp|html|ico|ini|java|jpe?g|js|json|jsx|lock|md|mov|mp3|mp4|pdf|png|pptx|py|rar|rst|rs|sh|so|sql|svg|tar|tiff?|toml|ts|tsx|txt|wav|webp|xml|xlsx|yaml|yml|zip)$",
    re.IGNORECASE,
)
_STANDALONE_FILE = re.compile(r"(?<![A-Za-z0-9_.-])(?P<file>\.[A-Za-z0-9_-]+|[A-Za-z0-9_.-]+)(?![A-Za-z0-9_.-])")
_SURROGATE = re.compile("[\\ud800-\\udfff]")

_TOP_FIELDS = frozenset({"schema", "revision", "seats"})
_SEAT_FIELDS = frozenset({"name", "cli", "model", "purpose", "caveats", "fallbacks", "enabled"})


class MasterRosterError(ValueError):
    """A master-roster document is malformed or contains forbidden material."""


def _has_control(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cf", "Zl", "Zp"} for character in value)


def _has_relative_path_evidence(value: str, *, provider_model: bool = False) -> bool:
    path_text = _HTTP_URL.sub("", value)
    for match in _STANDALONE_FILE.finditer(path_text):
        filename = match.group("file").rstrip(".,;:!?")
        file_like = (
            filename.startswith(".")
            or filename.casefold() in _EXTENSIONLESS_FILES | _PRIVATE_KEY_FILES | {"credentials"}
            or _FILE_SUFFIX.search(filename)
        )
        standalone = path_text.strip().rstrip(".,;:!?") == filename
        if file_like and (_PATH_CONTEXT.search(path_text[: match.start()]) or standalone):
            return True
    for match in _RELATIVE_PATH.finditer(path_text):
        components = re.split(r"[/\\]", match.group("path"))
        path_context = _PATH_CONTEXT.search(path_text[: match.start()]) is not None
        if path_context and (
            any(component.startswith(".") for component in components)
            or any(_FILE_SUFFIX.search(component) for component in components)
            or components[-1].casefold() in _EXTENSIONLESS_FILES | _PRIVATE_KEY_FILES
        ):
            return True
        if any(component.startswith(".") or component.casefold() in _FILESYSTEM_COMPONENTS for component in components):
            return True
        suffix = _MODEL_PATH_SUFFIX if provider_model else _FILE_SUFFIX
        if components[-1].casefold() in _EXTENSIONLESS_FILES or suffix.search(components[-1]):
            return True
    return False


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise MasterRosterError("duplicate JSON key")
        result[key] = value
    return result


def _text(value: Any, field: str, *, check_paths: bool = True) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MasterRosterError(f"{field} must be a non-empty string")
    if _SURROGATE.search(value):
        raise MasterRosterError(f"{field} must contain valid Unicode scalar values")
    if len(value) > MAX_TEXT or _has_control(value):
        raise MasterRosterError(f"{field} must be at most {MAX_TEXT} characters without controls or separators")
    if (
        _DANGEROUS_VALUE.search(value)
        or contains_credential(value)
        or (
            check_paths
            and (
                _ROOTED_PATH.search(_HTTP_URL.sub("", value))
                or _has_relative_path_evidence(
                    value,
                    provider_model=field.endswith(".model") and _PROVIDER_MODEL.fullmatch(value) is not None,
                )
            )
        )
    ):
        raise MasterRosterError(f"{field} contains a credential, environment assignment, or home path")
    return value


def _unknown_fields(value: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    if any(not isinstance(key, str) for key in value):
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
            decoded = json.loads(encoded.decode("utf-8"), object_pairs_hook=_object_without_duplicate_keys)
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
    if type(revision) is not int or revision < 1:
        raise MasterRosterError("revision must be a positive integer")
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
        name = _text(raw_seat.get("name"), f"{where}.name", check_paths=False)
        cli = _text(raw_seat.get("cli"), f"{where}.cli", check_paths=False)
        model = _text(raw_seat.get("model"), f"{where}.model")
        purpose = _text(raw_seat.get("purpose"), f"{where}.purpose")
        if not _SEAT_NAME.fullmatch(name):
            raise MasterRosterError(f"{where}.name is not a valid seat name")
        if not _CLI_NAME.fullmatch(cli):
            raise MasterRosterError(f"{where}.cli must be a single executable name")
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
        clean_seats.append(
            {
                "name": name,
                "cli": cli,
                "model": model,
                "purpose": purpose,
                "caveats": [_text(item, f"{where}.caveats[{i}]") for i, item in enumerate(caveats)],
                "fallbacks": [
                    _text(item, f"{where}.fallbacks[{i}]", check_paths=False) for i, item in enumerate(fallbacks)
                ],
                "enabled": raw_seat["enabled"],
            }
        )
    seats_by_name = {seat["name"]: seat for seat in clean_seats}
    for seat in clean_seats:
        for fallback in seat["fallbacks"]:
            if not _SEAT_NAME.fullmatch(fallback) or fallback not in names or fallback == seat["name"]:
                raise MasterRosterError(f"{seat['name']} has an invalid fallback seat: {fallback}")
            if seats_by_name[fallback]["fallbacks"]:
                raise MasterRosterError(f"{seat['name']} fallback target {fallback!r} must not define its own fallback")

    document = {"schema": SCHEMA, "revision": revision, "seats": clean_seats}
    try:
        canonical = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    except (ValueError, RecursionError) as exc:
        raise MasterRosterError("document cannot be represented as bounded JSON") from exc
    if len(canonical.encode("ascii")) > MAX_DOCUMENT_BYTES:
        raise MasterRosterError(f"document must be at most {MAX_DOCUMENT_BYTES} bytes")
    result = copy.deepcopy(document)
    result["sha256"] = "sha256:" + hashlib.sha256(canonical.encode("ascii")).hexdigest()
    return result
