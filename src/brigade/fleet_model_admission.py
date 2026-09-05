"""Node-bound model-roster admission, LKG cache, and operator projection."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import urllib.error
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from . import fleet_client as _client
from . import fleet_model_roster
from .fleet_client import FleetClientError

MODEL_ROSTER_MAX_BYTES = 1024 * 1024
LKG_NAME = "model-roster.lkg.json"
HIGH_WATER_NAME = "model-roster.high-water"
AUDIT_SPOOL_NAME = "model-admission-audit.jsonl"
_POLICY_DENIALS = frozenset(
    {
        "retired-model",
        "seat-disabled",
        "seat-missing",
        "binding-missing",
        "default-missing",
        "permanent-retirement-immutable",
    }
)
_CONFLICTS = frozenset(
    {
        "roster_revision_conflict",
        "roster_digest_conflict",
        "admission-conflict",
        "revision-conflict",
    }
)
_SAFE_ADMISSION_KEYS = (
    "schema",
    "state",
    "source",
    "roster_revision",
    "roster_digest",
    "seat",
    "provider",
    "model",
    "reasoning",
    "binding",
    "expires_at",
    "error",
    "reason",
    "remediation",
)
_SUCCESS_ADMISSION_KEYS = (
    "schema",
    "state",
    "source",
    "roster_revision",
    "roster_digest",
    "seat",
    "provider",
    "model",
    "reasoning",
    "binding",
    "expires_at",
)
_SAFE_ADMIN_KEYS = ("updated", "revision", "consumer", "seat", "policy", "retired")
_SAFE_ADMIN_POLICY_KEYS = (
    "seat",
    "provider",
    "model",
    "enabled",
    "limit",
    "notes",
    "reasoning",
    "brigade_cli",
    "t3_instance_id",
    "t3_service_tier",
)
_SAFE_ADMIN_RETIRED_KEYS = ("provider", "family", "match_kind", "permanent", "reason_code")
_AUDIT_SAFE_KEYS = (
    "source",
    "request_id",
    "phase",
    "consumer",
    "request_digest",
    "decision",
    "admission",
    "created_at",
    "node_id",
)


@dataclass(frozen=True)
class ModelAdmissionDecision:
    """Bounded admission outcome. Never carries a bearer, path, or prompt."""

    ok: bool
    exit_code: int
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)


def utc_now() -> datetime:
    """Client clock. Tests may monkeypatch this."""
    return datetime.now(timezone.utc)


def nofollow_supported() -> bool:
    """Windows and other hosts without no-follow descriptors fail closed.

    This is intentional: without a nofollow/dirfd primitive the high-water
    file cannot be trusted, so cache reads and writes must not proceed.
    """
    return bool(getattr(os, "O_NOFOLLOW", 0)) and _client._dir_fd_supported()


def lkg_path() -> Path:
    return _client.brigade_home() / _client.SPOOL_DIRNAME / LKG_NAME


def high_water_path() -> Path:
    return _client.brigade_home() / _client.SPOOL_DIRNAME / HIGH_WATER_NAME


def audit_spool_path() -> Path:
    return _client.brigade_home() / _client.SPOOL_DIRNAME / AUDIT_SPOOL_NAME


def _iso_z(value: datetime | str) -> str:
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _decision(
    ok: bool, exit_code: int, reason: str, payload: Mapping[str, Any] | None = None
) -> ModelAdmissionDecision:
    safe = {key: payload[key] for key in payload if key in _SAFE_ADMISSION_KEYS} if payload else {}
    if "reason" not in safe:
        safe["reason"] = reason
    return ModelAdmissionDecision(ok=ok, exit_code=exit_code, reason=reason, payload=dict(safe))


def _fail(reason: str, exit_code: int = 1, payload: Mapping[str, Any] | None = None) -> ModelAdmissionDecision:
    return _decision(False, exit_code, reason, payload)


def _ok(reason: str, payload: Mapping[str, Any]) -> ModelAdmissionDecision:
    return ModelAdmissionDecision(True, 0, reason, dict(payload))


def _node_token() -> str:
    return _client.load_fleet_settings()["node_token"] or ""


def _validate_leaf_stat(opened: os.stat_result, *, label: str) -> None:
    if not stat.S_ISREG(opened.st_mode):
        raise FleetClientError(f"{label} is not a regular file")
    if os.name == "posix" and opened.st_uid != os.getuid():
        raise FleetClientError(f"{label} is not owner-only")
    if stat.S_IMODE(opened.st_mode) != 0o600:
        raise FleetClientError(f"{label} is not mode 0600")
    if opened.st_size > MODEL_ROSTER_MAX_BYTES:
        raise FleetClientError(f"{label} exceeded the size limit")


def _inspect_existing(path: Path, *, dir_fd: int | None, missing_ok: bool) -> os.stat_result | None:
    try:
        if dir_fd is not None:
            opened = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
        else:
            opened = os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    if stat.S_ISLNK(opened.st_mode):
        raise FleetClientError(f"{path.name} is a symlink")
    _validate_leaf_stat(opened, label=path.name)
    return opened


def _open_leaf(path: Path, flags: int, *, dir_fd: int | None) -> int:
    _inspect_existing(path, dir_fd=dir_fd, missing_ok=False)
    fd = _client._open_private(path, flags, dir_fd=dir_fd)
    try:
        _validate_leaf_stat(os.fstat(fd), label=path.name)
    except Exception:
        os.close(fd)
        raise
    return fd


def _read_leaf_bytes(path: Path, *, dir_fd: int | None, missing_ok: bool = False) -> bytes | None:
    try:
        fd = _open_leaf(path, os.O_RDONLY, dir_fd=dir_fd)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    except OSError as exc:
        raise FleetClientError(f"{path.name} could not be opened safely") from exc
    try:
        before = os.fstat(fd)
        _validate_leaf_stat(before, label=path.name)
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = os.read(fd, 64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MODEL_ROSTER_MAX_BYTES:
                raise FleetClientError(f"{path.name} exceeded the size limit")
            chunks.append(chunk)
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise FleetClientError(f"{path.name} was replaced during read")
        _validate_leaf_stat(after, label=path.name)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _atomic_replace(path: Path, raw: bytes, *, dir_fd: int | None) -> None:
    if len(raw) > MODEL_ROSTER_MAX_BYTES:
        raise FleetClientError(f"{path.name} exceeded the size limit")
    if dir_fd is None:
        raise FleetClientError("descriptor-relative cache writes are unavailable")
    tmp_name = ""
    fd = -1
    try:
        for _ in range(_client._SPOOL_TEMP_ATTEMPTS):
            candidate = f".{path.name}.{secrets.token_hex(16)}.tmp"
            try:
                fd = os.open(
                    candidate,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | _client._O_NOFOLLOW | _client._O_CLOEXEC,
                    0o600,
                    dir_fd=dir_fd,
                )
            except FileExistsError:
                continue
            tmp_name = candidate
            break
        if not tmp_name:
            raise FleetClientError(f"{path.name}: exhausted private temp-name attempts")
        written = 0
        while written < len(raw):
            written += os.write(fd, raw[written:])
        os.fsync(fd)
        _validate_leaf_stat(os.fstat(fd), label=path.name)
        os.close(fd)
        fd = -1
        os.replace(tmp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        tmp_name = ""
        published = os.open(
            path.name,
            os.O_RDONLY | _client._O_NOFOLLOW | _client._O_CLOEXEC | _client._O_NONBLOCK,
            dir_fd=dir_fd,
        )
        try:
            _validate_leaf_stat(os.fstat(published), label=path.name)
        finally:
            os.close(published)
        if hasattr(os, "fsync"):
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
    except BaseException:
        if fd != -1:
            os.close(fd)
        if tmp_name:
            try:
                os.unlink(tmp_name, dir_fd=dir_fd)
            except FileNotFoundError:
                pass
        raise


def _encode_record(payload: Mapping[str, Any]) -> bytes:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if len(raw) > MODEL_ROSTER_MAX_BYTES:
        raise FleetClientError("model roster cache exceeded the size limit")
    return raw


def _read_high_water(*, dir_fd: int | None) -> int:
    raw = _read_leaf_bytes(high_water_path(), dir_fd=dir_fd, missing_ok=True)
    if raw is None:
        return 0
    try:
        parsed = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FleetClientError("high-water file was not valid JSON") from exc
    if isinstance(parsed, dict) and type(parsed.get("revision")) is int:
        return int(parsed["revision"])
    raise FleetClientError("high-water file is malformed")


def _cached_roster(envelope: Mapping[str, Any]) -> dict[str, Any]:
    cached = fleet_model_roster.cache_envelope(envelope)
    mac = envelope.get("mac")
    if isinstance(mac, dict):
        cached["mac"] = mac
    return cached


def _write_cache_locked(envelope: Mapping[str, Any], *, revision: int, dir_fd: int | None) -> None:
    record = {
        "cached_at": _iso_z(utc_now()),
        "highest_revision": revision,
        "roster": _cached_roster(envelope),
    }
    lkg = lkg_path()
    high_water = high_water_path()
    current = _read_high_water(dir_fd=dir_fd)
    if revision < current:
        raise FleetClientError("revision-rollback")
    _read_leaf_bytes(lkg, dir_fd=dir_fd, missing_ok=True)
    highest = max(current, revision)
    _atomic_replace(lkg, _encode_record(record), dir_fd=dir_fd)
    _atomic_replace(high_water, _encode_record({"revision": highest}), dir_fd=dir_fd)


def _load_lkg_record() -> dict[str, Any]:
    if not nofollow_supported():
        raise FleetClientError("cache-unsafe-platform")
    lkg = lkg_path()
    with _client._spool_lock(lkg) as dir_fd:
        raw = _read_leaf_bytes(lkg, dir_fd=dir_fd, missing_ok=False)
        high_water = _read_high_water(dir_fd=dir_fd)
    if raw is None:
        raise FleetClientError("lkg is missing")
    try:
        record = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FleetClientError("lkg was not valid JSON") from exc
    if not isinstance(record, dict) or not isinstance(record.get("roster"), dict):
        raise FleetClientError("lkg is malformed")
    try:
        cached_highest = int(record.get("highest_revision", 0))
    except (TypeError, ValueError) as exc:
        raise FleetClientError("lkg is malformed") from exc
    record["highest_revision"] = max(cached_highest, high_water)
    revision = record["roster"].get("revision")
    if type(revision) is not int or revision < record["highest_revision"]:
        raise FleetClientError("revision-rollback")
    return record


def _mac_valid(token: str, envelope: Mapping[str, Any]) -> bool:
    mac = envelope.get("mac")
    if not isinstance(mac, dict) or mac.get("algorithm") != fleet_model_roster.MAC_ALGORITHM:
        return False
    value = mac.get("value")
    if not isinstance(value, str):
        return False
    expected = fleet_model_roster.roster_mac(token, envelope)
    return hmac.compare_digest(value, expected)


def _validate_envelope(payload: Mapping[str, Any], *, token: str, audience: str) -> str | None:
    if payload.get("schema") != fleet_model_roster.ROSTER_SCHEMA:
        return "unsupported-schema"
    missing = [key for key in fleet_model_roster.CACHE_ENVELOPE_KEYS if key not in payload]
    if missing:
        return "malformed-roster"
    if payload.get("audience_node_id") != audience:
        return "audience-mismatch"
    if not isinstance(payload.get("mac"), dict):
        return "lkg-mac-invalid"
    digest = payload.get("document_sha256")
    if not isinstance(digest, str) or digest != fleet_model_roster.roster_digest(payload):
        return "digest-mismatch"
    if not _mac_valid(token, payload):
        return "lkg-mac-invalid"
    issued = _parse_iso(payload.get("issued_at"))
    expires = _parse_iso(payload.get("expires_at"))
    if issued is None or expires is None:
        return "malformed-roster"
    now = utc_now()
    if issued > now.astimezone(timezone.utc).replace(microsecond=0) + timedelta(
        seconds=fleet_model_roster.CLOCK_SKEW_SECONDS
    ):
        return "future-timestamp"
    if expires <= issued:
        return "malformed-roster"
    if expires <= now:
        return "lkg-expired"
    if type(payload.get("revision")) is not int:
        return "malformed-roster"
    if (expires - issued).total_seconds() > fleet_model_roster.LKG_TTL_SECONDS:
        return "malformed-roster"
    return fleet_model_roster.validate_roster_rows(payload)


def _validate_inspection_roster(payload: Mapping[str, Any]) -> str | None:
    """Validate an admin-readable roster without accepting it as node cache input."""
    if payload.get("schema") != fleet_model_roster.ROSTER_SCHEMA:
        return "unsupported-schema"
    required = set(fleet_model_roster.CACHE_ENVELOPE_KEYS).difference({"audience_node_id"})
    if any(key not in payload for key in required):
        return "malformed-roster"
    digest = payload.get("document_sha256")
    if not isinstance(digest, str) or digest != fleet_model_roster.roster_digest(payload):
        return "digest-mismatch"
    issued = _parse_iso(payload.get("issued_at"))
    expires = _parse_iso(payload.get("expires_at"))
    if issued is None or expires is None or expires <= issued or type(payload.get("revision")) is not int:
        return "malformed-roster"
    if (expires - issued).total_seconds() > fleet_model_roster.LKG_TTL_SECONDS:
        return "malformed-roster"
    return fleet_model_roster.validate_roster_rows(payload)


def _lkg_mac_valid(token: str) -> bool:
    try:
        record = _load_lkg_record()
    except Exception:
        return False
    return _mac_valid(token, record["roster"])


def _safe_roster_payload(envelope: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    payload = _cached_roster(envelope)
    payload["source"] = source
    return payload


def _accept_lkg(*, token: str, audience: str) -> ModelAdmissionDecision:
    try:
        record = _load_lkg_record()
        envelope = record["roster"]
        reason = _validate_envelope(envelope, token=token, audience=audience)
        if reason is not None:
            return _fail(reason)
        cached_at = _parse_iso(record.get("cached_at"))
        if cached_at is None:
            return _fail("malformed-roster")
        now = utc_now()
        if cached_at > now + timedelta(seconds=fleet_model_roster.CLOCK_SKEW_SECONDS):
            return _fail("future-timestamp")
        if (now - cached_at).total_seconds() > fleet_model_roster.LKG_TTL_SECONDS:
            return _fail("lkg-expired")
        if int(envelope["revision"]) < int(record.get("highest_revision") or 0):
            return _fail("revision-rollback")
        payload = _safe_roster_payload(envelope, source="lkg")
        return ModelAdmissionDecision(True, 0, "lkg", payload)
    except FleetClientError as exc:
        text = str(exc)
        if "size limit" in text:
            return _fail("lkg-oversized")
        if text == "cache-unsafe-platform":
            return _fail("cache-unsafe-platform")
        return _fail("lkg-unsafe")
    except OSError:
        return _fail("lkg-unsafe")


def fetch_versioned_roster(
    *,
    allow_lkg: bool = True,
    hub_url: str | None = None,
    cache_write: bool = True,
    inspect_only: bool = False,
) -> ModelAdmissionDecision:
    """GET the node-audience roster, optionally cache it, or fall back to a valid LKG."""
    cache_supported = nofollow_supported()
    config = _client.load_fleet_config()
    hub = hub_url or config["hub_url"]
    if not hub:
        return _fail("hub-unavailable")
    audience = _client.resolve_node_id()
    settings = _client.load_fleet_settings()
    node_token = settings["node_token"]
    admin_read = False
    if not node_token:
        if inspect_only and settings["admin_token"] and config["token"] == settings["admin_token"]:
            node_token = settings["admin_token"]
            admin_read = True
        if settings["admin_token"] and config["token"] == settings["admin_token"]:
            if not admin_read:
                return _fail("node-token-required")
        elif not admin_read:
            node_token = config["token"]
    if not node_token:
        return _fail("node-token-required")
    token = node_token
    try:
        payload = _client._run_with_deadline(
            lambda: _client._get_models_blocking(hub, "/models", token, timeout=_client.CLOUD_TIMEOUT_SECONDS),
            timeout=_client.CLOUD_TIMEOUT_SECONDS,
        )
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            if cache_supported and lkg_path().exists() and node_token and not _lkg_mac_valid(node_token):
                return _fail("token-rotated")
            return _fail("auth-failed")
        if exc.code == 409:
            return _fail("revision-conflict", 4)
        if exc.code >= 500 and allow_lkg and cache_supported:
            return _accept_lkg(token=node_token or token, audience=audience)
        return _fail("cache-unsafe-platform" if not cache_supported else "hub-unavailable")
    except (TimeoutError, urllib.error.URLError):
        if allow_lkg and cache_supported:
            return _accept_lkg(token=node_token or token, audience=audience)
        return _fail("cache-unsafe-platform" if not cache_supported else "hub-unavailable")
    except FleetClientError:
        return _fail("hub-unavailable")
    except Exception:
        return _fail("hub-unavailable")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return _fail("malformed-roster", 2)
    if not isinstance(payload, dict):
        return _fail("malformed-roster", 2)
    reason = (
        _validate_inspection_roster(payload)
        if admin_read
        else _validate_envelope(payload, token=node_token or token, audience=audience)
    )
    if reason is not None:
        return _fail(reason, 2 if reason == "unsupported-schema" else 1)
    if cache_write and cache_supported and not admin_read:
        try:
            with _client._spool_lock(lkg_path()) as dir_fd:
                _write_cache_locked(payload, revision=int(payload["revision"]), dir_fd=dir_fd)
        except FleetClientError as exc:
            text = str(exc)
            if text == "revision-rollback":
                return _fail("revision-rollback")
            if text == "cache-unsafe-platform":
                return _fail("cache-unsafe-platform")
            if "size limit" in text:
                return _fail("lkg-oversized")
            return _fail("lkg-unsafe")
        except OSError:
            return _fail("lkg-unsafe")
    body = _safe_roster_payload(payload, source="hub")
    return ModelAdmissionDecision(True, 0, "authoritative", body)


def _request_digest(body: Mapping[str, Any]) -> str:
    digest_body = {
        "consumer": body.get("consumer"),
        "expect_digest": body.get("expect_digest"),
        "expect_revision": body.get("expect_revision"),
        "phase": body.get("phase"),
        "seat": body.get("seat"),
    }
    return hashlib.sha256(fleet_model_roster.canonical_json(digest_body).encode("ascii")).hexdigest()


def _binding_for(consumer: str, seat: Mapping[str, Any]) -> dict[str, Any] | None:
    raw_bindings = seat.get("bindings")
    bindings: Mapping[str, Any] = raw_bindings if isinstance(raw_bindings, Mapping) else {}
    if consumer == "t3-fleet":
        t3_fleet = bindings.get("t3_fleet")
        if not isinstance(t3_fleet, Mapping):
            return None
        instance_id = str(t3_fleet.get("instance_id") or "")
        if not instance_id:
            return None
        return {
            "instance_id": instance_id,
            "service_tier": t3_fleet.get("service_tier") or None,
        }
    brigade = bindings.get("brigade")
    if not isinstance(brigade, Mapping):
        return None
    instance_id = str(brigade.get("cli") or "")
    if not instance_id:
        return None
    return {"instance_id": instance_id, "service_tier": None}


def _resolve_from_roster(
    roster: Mapping[str, Any],
    *,
    consumer: str,
    seat: str | None,
    source: str,
) -> ModelAdmissionDecision:
    raw_defaults = roster.get("consumer_defaults")
    defaults: dict[str, Any] = raw_defaults if isinstance(raw_defaults, dict) else {}
    seat_name = seat or defaults.get(consumer)
    raw_seats = roster.get("seats")
    seats: list[Any] = raw_seats if isinstance(raw_seats, list) else []
    raw_retired = roster.get("retired_models")
    retired_rows: list[Any] = raw_retired if isinstance(raw_retired, list) else []
    if not seat_name:
        return _fail(
            "default-missing",
            3,
            {
                "schema": fleet_model_roster.ADMISSION_SCHEMA,
                "state": "denied",
                "source": source,
                "error": "default-missing",
                "roster_revision": roster.get("revision"),
                "roster_digest": roster.get("document_sha256"),
                "expires_at": roster.get("expires_at"),
            },
        )
    match = next((item for item in seats if isinstance(item, dict) and item.get("seat") == seat_name), None)
    if match is None:
        reason = "seat-missing"
    elif not match.get("enabled"):
        reason = "seat-disabled"
    elif fleet_model_roster.retired_reason(
        str(match.get("provider") or ""), str(match.get("model") or ""), retired_rows
    ):
        reason = "retired-model"
    elif not isinstance(match.get("reasoning"), str) or not str(match.get("reasoning") or "").strip():
        reason = "binding-missing"
    else:
        binding = _binding_for(consumer, match)
        if binding is None:
            reason = "binding-missing"
        else:
            return _ok(
                "admitted",
                {
                    "schema": fleet_model_roster.ADMISSION_SCHEMA,
                    "state": "authoritative",
                    "source": source,
                    "roster_revision": roster.get("revision"),
                    "roster_digest": roster.get("document_sha256"),
                    "seat": seat_name,
                    "provider": match.get("provider"),
                    "model": match.get("model"),
                    "reasoning": match.get("reasoning"),
                    "binding": binding,
                    "expires_at": roster.get("expires_at"),
                },
            )
    payload = {
        "schema": fleet_model_roster.ADMISSION_SCHEMA,
        "state": "denied",
        "source": source,
        "error": reason,
        "roster_revision": roster.get("revision"),
        "roster_digest": roster.get("document_sha256"),
        "seat": seat_name,
        "provider": None if match is None else match.get("provider"),
        "model": None if match is None else match.get("model"),
        "reasoning": None if match is None else match.get("reasoning"),
        "expires_at": roster.get("expires_at"),
    }
    if reason == "binding-missing" and isinstance(match, dict):
        remediation = _set_command_remediation(
            provider=str(match.get("provider") or ""),
            model=str(match.get("model") or ""),
            seat=str(seat_name),
            reasoning=match.get("reasoning"),
            brigade_cli=_brigade_cli_from_row(match),
            t3_instance_id=_t3_instance_id_from_row(match),
            t3_service_tier=_t3_service_tier_from_row(match),
            revision=roster.get("revision"),
            consumer=consumer,
        )
        payload["error"] = f"{reason}\n{remediation}"
        payload["remediation"] = remediation
    return _fail(reason, 3, payload)


def _audit_created_at(row: Mapping[str, Any]) -> datetime | None:
    return _parse_iso(row.get("created_at"))


def _audit_row_fresh(row: Mapping[str, Any], now: datetime) -> bool:
    created = _audit_created_at(row)
    if created is None:
        return False
    if created > now + timedelta(seconds=fleet_model_roster.CLOCK_SKEW_SECONDS):
        return False
    return (now - created).total_seconds() <= fleet_model_roster.LKG_TTL_SECONDS


def _parse_audit_lines(raw: bytes) -> list[dict[str, Any]]:
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError as exc:
        raise FleetClientError("audit spool is corrupt") from exc
    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise FleetClientError("audit spool is corrupt") from exc
        if not isinstance(parsed, dict):
            raise FleetClientError("audit spool is corrupt")
        rows.append(parsed)
    return rows


def _safe_audit_row(row: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    safe = {key: row[key] for key in _AUDIT_SAFE_KEYS if key in row}
    if "created_at" not in safe:
        safe["created_at"] = _iso_z(now)
    if "node_id" not in safe:
        safe["node_id"] = _client.resolve_node_id()
    admission = safe.get("admission")
    if isinstance(admission, dict):
        safe["admission"] = {key: admission[key] for key in admission if key in _SAFE_ADMISSION_KEYS}
    return safe


def _append_audit_spool(row: Mapping[str, Any]) -> None:
    if not nofollow_supported():
        raise FleetClientError("cache-unsafe-platform")
    path = audit_spool_path()
    now = utc_now()
    encoded = json.dumps(_safe_audit_row(row, now=now), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("ascii")) > MODEL_ROSTER_MAX_BYTES:
        raise FleetClientError("audit spool exceeded the size limit")
    with _client._spool_lock(path) as dir_fd:
        existing = _read_leaf_bytes(path, dir_fd=dir_fd, missing_ok=True)
        kept: list[str] = []
        if existing:
            for item in _parse_audit_lines(existing):
                if _audit_row_fresh(item, now):
                    kept.append(json.dumps(item, sort_keys=True, separators=(",", ":"), ensure_ascii=True))
        kept.append(encoded)
        raw = ("\n".join(kept) + "\n").encode("ascii")
        if len(raw) > MODEL_ROSTER_MAX_BYTES:
            raise FleetClientError("audit spool exceeded the size limit")
        _atomic_replace(path, raw, dir_fd=dir_fd)


def _read_audit_spool() -> list[dict[str, Any]]:
    if not nofollow_supported():
        raise FleetClientError("cache-unsafe-platform")
    path = audit_spool_path()
    with _client._spool_lock(path) as dir_fd:
        raw = _read_leaf_bytes(path, dir_fd=dir_fd, missing_ok=True)
    if not raw:
        return []
    return _parse_audit_lines(raw)


def _roster_expectation_conflict(
    roster: Mapping[str, Any],
    *,
    expect_revision: int | None,
    expect_digest: str | None,
) -> ModelAdmissionDecision | None:
    source = roster.get("source") or "hub"
    digest = roster.get("document_sha256")
    if expect_revision is not None and int(roster.get("revision") or 0) != expect_revision:
        return _fail(
            "roster_revision_conflict",
            4,
            {
                "schema": fleet_model_roster.ADMISSION_SCHEMA,
                "state": "denied",
                "source": source,
                "error": "roster_revision_conflict",
                "roster_revision": roster.get("revision"),
                "roster_digest": digest,
            },
        )
    if expect_digest is not None and expect_digest != digest:
        return _fail(
            "roster_digest_conflict",
            4,
            {
                "schema": fleet_model_roster.ADMISSION_SCHEMA,
                "state": "denied",
                "source": source,
                "error": "roster_digest_conflict",
                "roster_revision": roster.get("revision"),
                "roster_digest": digest,
            },
        )
    return None


def _lkg_admit(
    roster: Mapping[str, Any],
    *,
    consumer: str,
    request_id: str,
    phase: str,
    seat: str | None,
    expect_revision: int | None,
    expect_digest: str | None,
) -> ModelAdmissionDecision:
    conflict = _roster_expectation_conflict(
        roster,
        expect_revision=expect_revision,
        expect_digest=expect_digest,
    )
    if conflict is not None:
        return conflict
    request = {
        "consumer": consumer,
        "expect_digest": expect_digest,
        "expect_revision": expect_revision,
        "phase": phase,
        "seat": seat,
    }
    digest = _request_digest(request)
    now = utc_now()
    try:
        matching = [
            row for row in _read_audit_spool() if row.get("request_id") == request_id and row.get("phase") == phase
        ]
        for row in matching:
            created = _audit_created_at(row)
            if created is not None and created > now + timedelta(seconds=fleet_model_roster.CLOCK_SKEW_SECONDS):
                return _fail("future-timestamp")
        existing = [row for row in matching if _audit_row_fresh(row, now)]
    except FleetClientError as exc:
        text = str(exc)
        if text == "cache-unsafe-platform":
            return _fail("cache-unsafe-platform")
        if "size limit" in text:
            return _fail("lkg-oversized")
        return _fail("lkg-unsafe")
    except OSError:
        return _fail("lkg-unsafe")
    if existing:
        row = existing[-1]
        if row.get("request_digest") != digest:
            return _fail("admission-conflict", 4, {"error": "admission-conflict"})
        raw_admission = row.get("admission")
        payload: dict[str, Any] = raw_admission if isinstance(raw_admission, dict) else {}
        if row.get("decision") == "admitted":
            exact = _exact_admission_success(payload)
            expires = _parse_iso(exact.get("expires_at")) if exact is not None else None
            expected = _resolve_from_roster(roster, consumer=consumer, seat=seat, source="lkg")
            if exact is None or expires is None or expires <= now or not expected.ok or exact != expected.payload:
                return _fail("lkg-unsafe")
            return ModelAdmissionDecision(True, 0, "admitted", exact)
        exit_code = 3 if row.get("decision") in _POLICY_DENIALS else 4
        return ModelAdmissionDecision(False, exit_code, str(row.get("decision") or "lkg"), payload)
    decision = _resolve_from_roster(roster, consumer=consumer, seat=seat, source="lkg")
    row = {
        "source": "lkg",
        "request_id": request_id,
        "phase": phase,
        "consumer": consumer,
        "request_digest": digest,
        "decision": "admitted" if decision.ok else decision.reason,
        "admission": decision.payload,
        "created_at": _iso_z(now),
        "node_id": _client.resolve_node_id(),
    }
    try:
        _append_audit_spool(row)
    except (FleetClientError, OSError):
        return _fail("lkg-unsafe")
    return decision


def _exact_admission_success(body: Mapping[str, Any]) -> dict[str, Any] | None:
    if set(body) != set(_SUCCESS_ADMISSION_KEYS):
        return None
    if body.get("schema") != fleet_model_roster.ADMISSION_SCHEMA:
        return None
    if body.get("state") != "authoritative":
        return None
    if body.get("error"):
        return None
    for key in ("seat", "provider", "model", "reasoning"):
        value = body.get(key)
        if not isinstance(value, str) or not value.strip():
            return None
    if type(body.get("roster_revision")) is not int:
        return None
    digest = body.get("roster_digest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        return None
    hex_part = digest[7:]
    if len(hex_part) != 64 or any(character not in "0123456789abcdef" for character in hex_part):
        return None
    if _parse_iso(body.get("expires_at")) is None:
        return None
    binding = body.get("binding")
    if not isinstance(binding, dict):
        return None
    instance_id = binding.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id:
        return None
    return {key: body[key] for key in _SUCCESS_ADMISSION_KEYS}


def _apply_admission_expectations(
    decision: ModelAdmissionDecision,
    *,
    expect_revision: int | None,
    expect_digest: str | None,
) -> ModelAdmissionDecision:
    if not decision.ok:
        return decision
    if expect_revision is not None and decision.payload.get("roster_revision") != expect_revision:
        return _fail(
            "roster_revision_conflict",
            4,
            {
                "schema": fleet_model_roster.ADMISSION_SCHEMA,
                "state": "denied",
                "source": decision.payload.get("source") or "hub",
                "error": "roster_revision_conflict",
                "roster_revision": decision.payload.get("roster_revision"),
                "roster_digest": decision.payload.get("roster_digest"),
            },
        )
    if expect_digest is not None and decision.payload.get("roster_digest") != expect_digest:
        return _fail(
            "roster_digest_conflict",
            4,
            {
                "schema": fleet_model_roster.ADMISSION_SCHEMA,
                "state": "denied",
                "source": decision.payload.get("source") or "hub",
                "error": "roster_digest_conflict",
                "roster_revision": decision.payload.get("roster_revision"),
                "roster_digest": decision.payload.get("roster_digest"),
            },
        )
    return decision


def _map_hub_admission(status: int, payload: Any) -> ModelAdmissionDecision:
    if status == 200:
        if not isinstance(payload, dict):
            return _fail("unsupported-schema", 2)
        exact = _exact_admission_success(payload)
        if exact is None:
            return _fail("unsupported-schema", 2)
        provider = str(exact.get("provider") or "")
        model = str(exact.get("model") or "")
        expires = _parse_iso(exact.get("expires_at"))
        if expires is not None and expires <= utc_now():
            return _fail("admission-expired")
        if fleet_model_roster.retired_reason(provider, model):
            denied = dict(exact)
            denied["state"] = "denied"
            denied["error"] = "retired-model"
            return _fail("retired-model", 3, denied)
        return _ok("admitted", exact)
    body = payload if isinstance(payload, dict) else {}
    error = body.get("error") if isinstance(body.get("error"), str) else None
    if status in (401, 403):
        return _fail("auth-failed")
    if status == 400:
        return _fail("unsupported-schema", 2)
    if error in _POLICY_DENIALS:
        return _fail(error, 3, body)
    if error in _CONFLICTS:
        return _fail(error, 4, body)
    if status == 409:
        return _fail(error or "admission-conflict", 4, body)
    if status >= 500:
        return _fail("hub-unavailable")
    return _fail("hub-unavailable")


def admit_model(
    *,
    consumer: str,
    request_id: str,
    phase: str,
    seat: str | None = None,
    expect_revision: int | None = None,
    expect_digest: str | None = None,
    allow_lkg: bool = True,
) -> ModelAdmissionDecision:
    """Admit one consumer/seat from Hub or a valid LKG."""
    if consumer not in fleet_model_roster.CONSUMERS or phase not in fleet_model_roster.ADMISSION_PHASES:
        return _fail("unsupported-schema", 2)
    if not isinstance(request_id, str) or not fleet_model_roster.REQUEST_ID_PATTERN.fullmatch(request_id):
        return _fail("unsupported-schema", 2)
    if seat is not None and seat != "":
        if not isinstance(seat, str) or not fleet_model_roster.SEAT_NAME_PATTERN.fullmatch(seat):
            return _fail("unsupported-schema", 2)
    if phase == "target":
        allow_lkg = False
    fetched = fetch_versioned_roster(allow_lkg=allow_lkg)
    if not fetched.ok and fetched.reason in {
        "auth-failed",
        "token-rotated",
        "revision-conflict",
        "cache-unsafe-platform",
        "unsupported-schema",
        "malformed-roster",
        "lkg-mac-invalid",
        "digest-mismatch",
        "audience-mismatch",
        "future-timestamp",
        "lkg-expired",
        "revision-rollback",
        "node-token-required",
        "admin-token-not-cacheable",
    }:
        return fetched
    if fetched.ok:
        conflict = _roster_expectation_conflict(
            fetched.payload,
            expect_revision=expect_revision,
            expect_digest=expect_digest,
        )
        if conflict is not None:
            return conflict
    config = _client.load_fleet_config()
    hub = config["hub_url"]
    token = config["token"]
    body = {
        "action": "admit",
        "schema": fleet_model_roster.ADMISSION_REQUEST_SCHEMA,
        "consumer": consumer,
        "seat": seat,
        "request_id": request_id,
        "phase": phase,
        "expect_revision": expect_revision,
        "expect_digest": expect_digest,
    }
    if hub and token:
        try:
            status, payload = _client._run_with_deadline(
                lambda: _client._post_model_policy_blocking(hub, token, body, timeout=_client.CLOUD_TIMEOUT_SECONDS),
                timeout=_client.CLOUD_TIMEOUT_SECONDS,
            )
            return _apply_admission_expectations(
                _map_hub_admission(status, payload),
                expect_revision=expect_revision,
                expect_digest=expect_digest,
            )
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                return _fail("auth-failed")
            if exc.code == 409:
                return _fail("revision-conflict", 4)
            if exc.code >= 500 and allow_lkg and fetched.ok:
                return _lkg_admit(
                    fetched.payload,
                    consumer=consumer,
                    request_id=request_id,
                    phase=phase,
                    seat=seat,
                    expect_revision=expect_revision,
                    expect_digest=expect_digest,
                )
            return _fail("hub-unavailable")
        except (TimeoutError, urllib.error.URLError):
            if allow_lkg and fetched.ok:
                return _lkg_admit(
                    fetched.payload,
                    consumer=consumer,
                    request_id=request_id,
                    phase=phase,
                    seat=seat,
                    expect_revision=expect_revision,
                    expect_digest=expect_digest,
                )
            return _fail("hub-unavailable")
        except FleetClientError:
            return _fail("hub-unavailable")
        except Exception:
            return _fail("hub-unavailable")
    if allow_lkg and fetched.ok:
        return _lkg_admit(
            fetched.payload,
            consumer=consumer,
            request_id=request_id,
            phase=phase,
            seat=seat,
            expect_revision=expect_revision,
            expect_digest=expect_digest,
        )
    return fetched if not fetched.ok else _fail("hub-unavailable")


def _local_roster_projection() -> dict[str, Any]:
    from . import roster as roster_mod

    try:
        resolution = roster_mod.resolve_roster(Path.cwd())
        loaded = roster_mod.load_roster(resolution.path, resolution=resolution)
    except Exception:
        return {"source": None, "agents": {}}
    return {
        "source": resolution.source,
        "agents": {
            name: {"cli": agent.cli, "model": agent.model, "reasoning": agent.reasoning}
            for name, agent in loaded.agents.items()
        },
    }


def _audit_spool_label() -> str:
    if not nofollow_supported():
        return "unsafe"
    if not _node_token():
        return "unavailable"
    try:
        rows = _read_audit_spool()
    except FleetClientError:
        return "unsafe"
    return "present" if rows else "empty"


def _cache_is_valid() -> bool:
    if not nofollow_supported() or not _node_token():
        return False
    config = _client.load_fleet_config()
    token = _node_token() or config["token"]
    return _accept_lkg(token=token, audience=_client.resolve_node_id()).ok


def _local_model_for(local_agents: Mapping[str, Any], seat: str | None) -> Any:
    if not seat or not isinstance(local_agents, Mapping):
        return None
    agent = local_agents.get(seat)
    if not isinstance(agent, Mapping):
        return None
    return agent.get("model")


def doctor_model_roster(*, consumer: str) -> ModelAdmissionDecision:
    if consumer not in fleet_model_roster.CONSUMERS:
        return _fail("unsupported-schema", 2)
    fetched = fetch_versioned_roster(allow_lkg=False, cache_write=False, inspect_only=True)
    local = _local_roster_projection()
    spool_label = _audit_spool_label()
    if not fetched.ok:
        failed: dict[str, Any] = {
            "consumer": consumer,
            "hub": "unreachable" if fetched.reason != "auth-failed" else "auth-failed",
            "cache_valid": False,
            "local_roster_drift": False,
            "audit_spool": spool_label,
            "reason": fetched.reason,
        }
        return ModelAdmissionDecision(False, fetched.exit_code, fetched.reason, failed)
    roster = fetched.payload
    raw_defaults = roster.get("consumer_defaults")
    defaults: dict[str, Any] = raw_defaults if isinstance(raw_defaults, dict) else {}
    default_seat = defaults.get(consumer)
    resolved = _resolve_from_roster(roster, consumer=consumer, seat=None, source=str(roster.get("source") or "hub"))
    local_agents = local.get("agents")
    local_model = _local_model_for(local_agents if isinstance(local_agents, dict) else {}, default_seat)
    missing_local = bool(default_seat) and not local_model
    drift = missing_local or bool(default_seat and local_model and local_model != resolved.payload.get("model"))
    payload: dict[str, Any] = {
        "consumer": consumer,
        "hub": "reachable" if roster.get("source") == "hub" else "lkg",
        "roster_revision": roster.get("revision"),
        "roster_digest": roster.get("document_sha256"),
        "cache_valid": _cache_is_valid(),
        "cache_age_seconds": None,
        "consumer_default": default_seat,
        "provider": resolved.payload.get("provider"),
        "model": resolved.payload.get("model"),
        "reasoning": resolved.payload.get("reasoning"),
        "binding_present": isinstance(resolved.payload.get("binding"), dict),
        "retired": resolved.reason == "retired-model",
        "local_roster_drift": drift,
        "audit_spool": spool_label,
    }
    if not payload["binding_present"]:
        payload["remediation"] = _empty_binding_remediation(
            {
                "seat": default_seat,
                "provider": resolved.payload.get("provider") or payload.get("provider"),
                "model": resolved.payload.get("model") or payload.get("model"),
            },
            roster,
            consumer=consumer,
        )
    try:
        record = _load_lkg_record()
        cached_at = _parse_iso(record.get("cached_at"))
        if cached_at is not None:
            payload["cache_age_seconds"] = max(0, int((utc_now() - cached_at).total_seconds()))
    except Exception:
        if payload["cache_valid"] is True:
            payload["cache_valid"] = False
    return ModelAdmissionDecision(True, 0, "ok", payload)


def _reconcile_findings(
    roster: Mapping[str, Any],
    *,
    consumer: str,
    local_agents: Mapping[str, Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    raw_defaults = roster.get("consumer_defaults")
    defaults: dict[str, Any] = raw_defaults if isinstance(raw_defaults, dict) else {}
    default_seat = defaults.get(consumer)
    raw_seats = roster.get("seats")
    seats: list[Any] = raw_seats if isinstance(raw_seats, list) else []
    seen: set[str] = set()
    for item in seats:
        if not isinstance(item, dict) or not item.get("enabled"):
            continue
        seat_name = item.get("seat")
        if not isinstance(seat_name, str) or not seat_name or seat_name in seen:
            continue
        relevant = seat_name == default_seat or _binding_for(consumer, item) is not None
        if not relevant:
            continue
        seen.add(seat_name)
        if _binding_for(consumer, item) is None:
            findings.append(
                {
                    "code": "empty-binding",
                    "seat": seat_name,
                    "remediation": _empty_binding_remediation(item, roster, consumer=consumer),
                }
            )
            continue
        local_model = _local_model_for(local_agents, seat_name)
        if not local_model:
            findings.append({"code": "instance-missing", "seat": seat_name})
            continue
        if local_model != item.get("model"):
            findings.append(
                {
                    "code": "local-roster-drift",
                    "seat": seat_name,
                    "local_model": local_model,
                    "hub_model": item.get("model"),
                }
            )
    if default_seat and default_seat not in seen:
        if not _local_model_for(local_agents, str(default_seat)):
            findings.append({"code": "instance-missing", "seat": default_seat})
    return findings


def _t3_instance_id_from_row(row: Mapping[str, Any]) -> str | None:
    """Return the T3 Fleet instance binding from a versioned roster seat row."""
    bindings = row.get("bindings")
    if not isinstance(bindings, dict):
        return None
    t3_fleet = bindings.get("t3_fleet")
    if not isinstance(t3_fleet, dict):
        return None
    instance_id = t3_fleet.get("instance_id")
    return instance_id if isinstance(instance_id, str) and instance_id else None


def _t3_service_tier_from_row(row: Mapping[str, Any]) -> str | None:
    """Return the T3 Fleet service tier from a versioned roster seat row."""
    bindings = row.get("bindings")
    if not isinstance(bindings, dict):
        return None
    t3_fleet = bindings.get("t3_fleet")
    if not isinstance(t3_fleet, dict):
        return None
    service_tier = t3_fleet.get("service_tier")
    return service_tier if isinstance(service_tier, str) and service_tier else None


def _set_command_remediation(
    *,
    provider: str,
    model: str,
    seat: str,
    reasoning: str | None,
    brigade_cli: str | None,
    t3_instance_id: str | None,
    t3_service_tier: str | None,
    revision: object,
    consumer: str = "brigade-run",
) -> str:
    """Human-readable 'brigade fleet models set' command for a seat row."""
    rev = revision if isinstance(revision, int) else "N"
    reason = reasoning if isinstance(reasoning, str) and reasoning else "none"
    cmd_parts = [f"brigade fleet models set {provider} {model} {seat} --enable"]
    cmd_parts.append(f"--reasoning {reason}")
    if consumer == "t3-fleet":
        instance_id = t3_instance_id if isinstance(t3_instance_id, str) and t3_instance_id else brigade_cli
        if isinstance(instance_id, str) and instance_id:
            cmd_parts.append(f"--t3-instance-id {instance_id}")
            if isinstance(t3_service_tier, str) and t3_service_tier:
                cmd_parts.append(f"--t3-service-tier {t3_service_tier}")
        else:
            cmd_parts.append("--t3-instance-id INSTANCE")
    else:
        cli = brigade_cli if isinstance(brigade_cli, str) and brigade_cli else t3_instance_id
        if isinstance(cli, str) and cli:
            cmd_parts.append(f"--brigade-cli {cli}")
        else:
            cmd_parts.append("--brigade-cli CLI")
    cmd_parts.append(f"--expect-revision {rev}")
    return f"bind it with: {' '.join(cmd_parts)}\nsee current rows: brigade fleet models list --seat {seat}"


def _brigade_cli_from_row(row: Mapping[str, Any]) -> str | None:
    """Return the brigade-run CLI binding from a versioned roster seat row."""
    bindings = row.get("bindings")
    if not isinstance(bindings, dict):
        return None
    brigade = bindings.get("brigade")
    if not isinstance(brigade, dict):
        return None
    cli = brigade.get("cli")
    return cli if isinstance(cli, str) and cli else None


def _empty_binding_remediation(
    seat: Mapping[str, Any],
    roster: Mapping[str, Any],
    *,
    consumer: str = "brigade-run",
) -> str:
    revision = roster.get("revision") or roster.get("roster_revision") or "N"
    name = seat.get("seat") or "SEAT"
    provider = seat.get("provider") or "provider"
    model = seat.get("model") or "model"
    reasoning = seat.get("reasoning")
    brigade_cli = None
    bindings = seat.get("bindings")
    if isinstance(bindings, dict):
        brigade = bindings.get("brigade")
        if isinstance(brigade, dict):
            brigade_cli = brigade.get("cli")
    return _set_command_remediation(
        provider=str(provider),
        model=str(model),
        seat=str(name),
        reasoning=reasoning if isinstance(reasoning, str) and reasoning else None,
        brigade_cli=brigade_cli if isinstance(brigade_cli, str) and brigade_cli else None,
        t3_instance_id=_t3_instance_id_from_row(seat),
        t3_service_tier=_t3_service_tier_from_row(seat),
        revision=revision,
        consumer=consumer,
    )


def reconcile_model_roster(*, consumer: str) -> ModelAdmissionDecision:
    if consumer not in fleet_model_roster.CONSUMERS:
        return _fail("unsupported-schema", 2)
    fetched = fetch_versioned_roster(allow_lkg=False, cache_write=False, inspect_only=True)
    local = _local_roster_projection()
    raw_local_agents = local.get("agents")
    local_agents: dict[str, Any] = raw_local_agents if isinstance(raw_local_agents, dict) else {}
    if not fetched.ok:
        return ModelAdmissionDecision(
            False,
            fetched.exit_code,
            fetched.reason,
            {
                "read_only": True,
                "consumer": consumer,
                "local_roster": local,
                "project_default_model": None,
                "project_default_drift": False,
                "findings": [],
                "hub": fetched.reason,
            },
        )
    findings = _reconcile_findings(fetched.payload, consumer=consumer, local_agents=local_agents)
    payload = {
        "read_only": True,
        "consumer": consumer,
        "local_roster": local,
        "project_default_model": None,
        "project_default_drift": False,
        "findings": findings,
        "hub": "reachable" if fetched.payload.get("source") == "hub" else fetched.reason,
    }
    return ModelAdmissionDecision(True, 0, "ok", payload)


def _safe_admin_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in _SAFE_ADMIN_KEYS:
        if key not in raw:
            continue
        value = raw[key]
        if key == "policy" and isinstance(value, dict):
            payload[key] = {item: value[item] for item in _SAFE_ADMIN_POLICY_KEYS if item in value}
        elif key == "retired" and isinstance(value, dict):
            payload[key] = {item: value[item] for item in _SAFE_ADMIN_RETIRED_KEYS if item in value}
        else:
            payload[key] = value
    return payload


def _admin_mutate(body: dict[str, Any]) -> ModelAdmissionDecision:
    settings = _client.load_fleet_settings()
    hub = settings["hub_url"]
    token = settings["admin_token"]
    if not hub or not token:
        return _fail("hub-unavailable")
    try:
        status, payload = _client._run_with_deadline(
            lambda: _client._post_model_policy_blocking(hub, token, body, timeout=_client.CLOUD_TIMEOUT_SECONDS),
            timeout=_client.CLOUD_TIMEOUT_SECONDS,
        )
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _fail("auth-failed")
        if exc.code == 400:
            return _fail("unsupported-schema", 2)
        if exc.code == 409:
            return _fail("roster_revision_conflict", 4)
        return _fail("hub-unavailable")
    except Exception:
        return _fail("hub-unavailable")
    body_payload = payload if isinstance(payload, dict) else {}
    error = body_payload.get("error") if isinstance(body_payload.get("error"), str) else None
    if status == 200:
        return ModelAdmissionDecision(True, 0, "ok", _safe_admin_payload(body_payload))
    if error in _POLICY_DENIALS:
        return _fail(error, 3, body_payload)
    if error in _CONFLICTS:
        return _fail(error, 4, body_payload)
    if status == 409:
        return _fail(error or "roster_revision_conflict", 4, body_payload)
    if status in (401, 403):
        return _fail("auth-failed")
    if status == 400:
        return _fail("unsupported-schema", 2)
    return _fail("hub-unavailable")


def retire_model(
    provider: str,
    family: str,
    *,
    permanent: bool,
    expected_revision: int,
    reason_code: str = "operator-retired",
) -> ModelAdmissionDecision:
    return _admin_mutate(
        {
            "action": "retire",
            "provider": provider,
            "family": family,
            "permanent": permanent,
            "expected_revision": expected_revision,
            "reason_code": reason_code,
        }
    )


def set_consumer_default(consumer: str, seat: str, *, expected_revision: int) -> ModelAdmissionDecision:
    return _admin_mutate(
        {
            "action": "set-default",
            "consumer": consumer,
            "seat": seat,
            "expected_revision": expected_revision,
        }
    )
