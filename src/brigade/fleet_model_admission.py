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
    """Windows and other hosts without no-follow descriptors fail closed."""
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
    settings = _client.load_fleet_settings()
    return settings["node_token"] or _client.load_fleet_config().get("token") or ""


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


def _write_cache_locked(envelope: Mapping[str, Any], *, revision: int, dir_fd: int | None) -> None:
    record = {
        "cached_at": _iso_z(utc_now()),
        "highest_revision": revision,
        "roster": dict(envelope),
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


def _write_cache(envelope: Mapping[str, Any], *, revision: int) -> None:
    if not nofollow_supported():
        raise FleetClientError("cache-unsafe-platform")
    with _client._spool_lock(lkg_path()) as dir_fd:
        _write_cache_locked(envelope, revision=revision, dir_fd=dir_fd)


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
    record["highest_revision"] = max(int(record.get("highest_revision") or 0), high_water)
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
    digest = payload.get("roster_digest")
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
    if expires <= now:
        return "lkg-expired"
    if type(payload.get("revision")) is not int:
        return "malformed-roster"
    return None


def _lkg_mac_valid(token: str) -> bool:
    try:
        record = _load_lkg_record()
    except Exception:
        return False
    return _mac_valid(token, record["roster"])


def _safe_roster_payload(envelope: Mapping[str, Any], *, source: str) -> dict[str, Any]:
    payload = {key: envelope[key] for key in envelope if key in fleet_model_roster.CACHE_ENVELOPE_KEYS or key == "mac"}
    payload["source"] = source
    payload["schema"] = envelope.get("schema")
    payload["revision"] = envelope.get("revision")
    payload["roster_digest"] = envelope.get("roster_digest")
    payload["audience_node_id"] = envelope.get("audience_node_id")
    payload["mac"] = envelope.get("mac")
    payload["seats"] = envelope.get("seats")
    payload["consumer_defaults"] = envelope.get("consumer_defaults")
    payload["retired_models"] = envelope.get("retired_models")
    payload["issued_at"] = envelope.get("issued_at")
    payload["expires_at"] = envelope.get("expires_at")
    payload["revision_updated_at"] = envelope.get("revision_updated_at")
    return payload


def _accept_lkg(*, token: str, audience: str) -> ModelAdmissionDecision:
    try:
        record = _load_lkg_record()
        envelope = record["roster"]
        reason = _validate_envelope(envelope, token=token, audience=audience)
        if reason is not None:
            return _fail(reason)
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


def fetch_versioned_roster(*, allow_lkg: bool = True, hub_url: str | None = None) -> ModelAdmissionDecision:
    """GET the node-audience roster, cache it, or fall back to a valid LKG."""
    if not nofollow_supported():
        return _fail("cache-unsafe-platform")
    config = _client.load_fleet_config()
    hub = hub_url or config["hub_url"]
    token = config["token"]
    if not hub:
        return _fail("hub-unavailable")
    audience = _client.resolve_node_id()
    node_token = _node_token()
    try:
        payload = _client._run_with_deadline(
            lambda: _client._get_models_blocking(hub, "/models", token, timeout=_client.CLOUD_TIMEOUT_SECONDS),
            timeout=_client.CLOUD_TIMEOUT_SECONDS,
        )
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            if lkg_path().exists() and node_token and not _lkg_mac_valid(node_token):
                return _fail("token-rotated")
            return _fail("auth-failed")
        if exc.code == 409:
            return _fail("revision-conflict")
        if exc.code >= 500 and allow_lkg:
            return _accept_lkg(token=node_token or token, audience=audience)
        return _fail("hub-unavailable")
    except (TimeoutError, urllib.error.URLError):
        if allow_lkg:
            return _accept_lkg(token=node_token or token, audience=audience)
        return _fail("hub-unavailable")
    except FleetClientError as exc:
        if "size limit" in str(exc):
            return _fail("hub-unavailable")
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
    reason = _validate_envelope(payload, token=node_token or token, audience=audience)
    if reason is not None:
        return _fail(reason, 2 if reason == "unsupported-schema" else 1)
    try:
        if not nofollow_supported():
            return _fail("cache-unsafe-platform")
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
    bindings: dict[str, Any] = raw_bindings if isinstance(raw_bindings, dict) else {}
    if consumer == "t3-fleet":
        instance_id = str(bindings.get("t3_instance_id") or seat.get("t3_instance_id") or "")
        if not instance_id:
            return None
        return {
            "instance_id": instance_id,
            "service_tier": bindings.get("t3_service_tier") or seat.get("t3_service_tier") or None,
        }
    instance_id = str(bindings.get("brigade_cli") or seat.get("brigade_cli") or "")
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
                "roster_digest": roster.get("roster_digest"),
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
                    "roster_digest": roster.get("roster_digest"),
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
        "roster_digest": roster.get("roster_digest"),
        "seat": seat_name,
        "provider": None if match is None else match.get("provider"),
        "model": None if match is None else match.get("model"),
        "reasoning": None if match is None else match.get("reasoning"),
        "expires_at": roster.get("expires_at"),
    }
    return _fail(reason, 3, payload)


def _append_audit_spool(row: Mapping[str, Any]) -> None:
    if not nofollow_supported():
        raise FleetClientError("cache-unsafe-platform")
    path = audit_spool_path()
    encoded = json.dumps(dict(row), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    if len(encoded.encode("ascii")) > MODEL_ROSTER_MAX_BYTES:
        raise FleetClientError("audit spool exceeded the size limit")
    with _client._spool_lock(path) as dir_fd:
        existing = _read_leaf_bytes(path, dir_fd=dir_fd, missing_ok=True)
        lines = []
        if existing:
            for line in existing.decode("ascii").splitlines():
                if line.strip():
                    lines.append(line)
        lines.append(encoded)
        raw = ("\n".join(lines) + "\n").encode("ascii")
        _atomic_replace(path, raw, dir_fd=dir_fd)


def _read_audit_spool() -> list[dict[str, Any]]:
    if not audit_spool_path().exists():
        return []
    if not nofollow_supported():
        return []
    try:
        with _client._spool_lock(audit_spool_path()) as dir_fd:
            raw = _read_leaf_bytes(audit_spool_path(), dir_fd=dir_fd, missing_ok=True)
    except Exception:
        return []
    if not raw:
        return []
    rows: list[dict[str, Any]] = []
    for line in raw.decode("ascii").splitlines():
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            rows.append(parsed)
    return rows


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
    if expect_revision is not None and int(roster.get("revision") or 0) != expect_revision:
        return _fail(
            "roster_revision_conflict",
            4,
            {
                "schema": fleet_model_roster.ADMISSION_SCHEMA,
                "state": "denied",
                "source": "lkg",
                "error": "roster_revision_conflict",
                "roster_revision": roster.get("revision"),
                "roster_digest": roster.get("roster_digest"),
            },
        )
    if expect_digest is not None and expect_digest != roster.get("roster_digest"):
        return _fail(
            "roster_digest_conflict",
            4,
            {
                "schema": fleet_model_roster.ADMISSION_SCHEMA,
                "state": "denied",
                "source": "lkg",
                "error": "roster_digest_conflict",
                "roster_revision": roster.get("revision"),
                "roster_digest": roster.get("roster_digest"),
            },
        )
    request = {
        "consumer": consumer,
        "expect_digest": expect_digest,
        "expect_revision": expect_revision,
        "phase": phase,
        "seat": seat,
    }
    digest = _request_digest(request)
    existing = [row for row in _read_audit_spool() if row.get("request_id") == request_id and row.get("phase") == phase]
    if existing:
        row = existing[-1]
        if row.get("request_digest") != digest:
            return _fail("admission-conflict", 4, {"error": "admission-conflict"})
        raw_admission = row.get("admission")
        payload: dict[str, Any] = raw_admission if isinstance(raw_admission, dict) else {}
        ok = row.get("decision") == "admitted"
        exit_code = 0 if ok else (3 if row.get("decision") in _POLICY_DENIALS else 4)
        return ModelAdmissionDecision(ok, exit_code, str(row.get("decision") or "lkg"), payload)
    decision = _resolve_from_roster(roster, consumer=consumer, seat=seat, source="lkg")
    row = {
        "source": "lkg",
        "request_id": request_id,
        "phase": phase,
        "consumer": consumer,
        "request_digest": digest,
        "decision": "admitted" if decision.ok else decision.reason,
        "admission": decision.payload,
    }
    try:
        _append_audit_spool(row)
    except (FleetClientError, OSError):
        return _fail("lkg-unsafe")
    return decision


def _map_hub_admission(status: int, payload: Any) -> ModelAdmissionDecision:
    body = payload if isinstance(payload, dict) else {}
    error = body.get("error") if isinstance(body.get("error"), str) else None
    if status == 200 and body.get("schema") == fleet_model_roster.ADMISSION_SCHEMA:
        return _ok("admitted", body)
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
    }:
        return fetched
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
            return _map_hub_admission(status, payload)
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
        except Exception:
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


def doctor_model_roster(*, consumer: str) -> ModelAdmissionDecision:
    if consumer not in fleet_model_roster.CONSUMERS:
        return _fail("unsupported-schema", 2)
    fetched = fetch_versioned_roster()
    local = _local_roster_projection()
    spool = _read_audit_spool()
    if not fetched.ok:
        failed: dict[str, Any] = {
            "consumer": consumer,
            "hub": "unreachable" if fetched.reason != "auth-failed" else "auth-failed",
            "cache_valid": False,
            "local_roster_drift": False,
            "audit_spool": "present" if spool else "empty",
            "reason": fetched.reason,
        }
        return ModelAdmissionDecision(False, fetched.exit_code, fetched.reason, failed)
    roster = fetched.payload
    raw_defaults = roster.get("consumer_defaults")
    defaults: dict[str, Any] = raw_defaults if isinstance(raw_defaults, dict) else {}
    default_seat = defaults.get(consumer)
    resolved = _resolve_from_roster(roster, consumer=consumer, seat=None, source=str(roster.get("source") or "hub"))
    local_model = None
    local_agents = local.get("agents")
    if default_seat and isinstance(local_agents, dict):
        local_model = (local_agents.get(default_seat) or {}).get("model")
    drift = bool(default_seat and local_model and local_model != resolved.payload.get("model"))
    payload: dict[str, Any] = {
        "consumer": consumer,
        "hub": "reachable" if roster.get("source") == "hub" else "lkg",
        "roster_revision": roster.get("revision"),
        "roster_digest": roster.get("roster_digest"),
        "cache_valid": lkg_path().exists(),
        "cache_age_seconds": None,
        "consumer_default": default_seat,
        "provider": resolved.payload.get("provider"),
        "model": resolved.payload.get("model"),
        "reasoning": resolved.payload.get("reasoning"),
        "binding_present": isinstance(resolved.payload.get("binding"), dict),
        "retired": resolved.reason == "retired-model",
        "local_roster_drift": drift,
        "audit_spool": "present" if spool else "empty",
    }
    if lkg_path().exists():
        try:
            record = _load_lkg_record()
            cached_at = _parse_iso(record.get("cached_at"))
            if cached_at is not None:
                payload["cache_age_seconds"] = max(0, int((utc_now() - cached_at).total_seconds()))
        except Exception:
            payload["cache_valid"] = False
    return ModelAdmissionDecision(True, 0, "ok", payload)


def reconcile_model_roster(*, consumer: str) -> ModelAdmissionDecision:
    if consumer not in fleet_model_roster.CONSUMERS:
        return _fail("unsupported-schema", 2)
    fetched = fetch_versioned_roster()
    local = _local_roster_projection()
    findings: list[dict[str, Any]] = []
    raw_defaults = fetched.payload.get("consumer_defaults") if fetched.ok else None
    defaults: dict[str, Any] = raw_defaults if isinstance(raw_defaults, dict) else {}
    default_seat = defaults.get(consumer)
    raw_local_agents = local.get("agents")
    local_agents: dict[str, Any] = raw_local_agents if isinstance(raw_local_agents, dict) else {}
    if default_seat and default_seat in local_agents:
        hub_model = None
        if fetched.ok:
            for item in fetched.payload.get("seats") or []:
                if isinstance(item, dict) and item.get("seat") == default_seat:
                    hub_model = item.get("model")
                    break
        if hub_model and local_agents[default_seat].get("model") != hub_model:
            findings.append(
                {
                    "code": "local-roster-drift",
                    "seat": default_seat,
                    "local_model": local_agents[default_seat].get("model"),
                    "hub_model": hub_model,
                }
            )
    payload = {
        "read_only": True,
        "consumer": consumer,
        "local_roster": local,
        "project_default_model": None,
        "project_default_drift": False,
        "findings": findings,
        "hub": "reachable" if fetched.ok and fetched.payload.get("source") == "hub" else fetched.reason,
    }
    return ModelAdmissionDecision(True, 0, "ok", payload)


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
        return _fail("hub-unavailable")
    except Exception:
        return _fail("hub-unavailable")
    body_payload = payload if isinstance(payload, dict) else {}
    error = body_payload.get("error") if isinstance(body_payload.get("error"), str) else None
    if status == 200:
        return ModelAdmissionDecision(True, 0, "ok", body_payload)
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
