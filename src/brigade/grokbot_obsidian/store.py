"""Bounded persistent action store with out-of-band approval receipts."""

from __future__ import annotations

import json
import os
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn

from .. import grokbot_ops
from .catalog import get_catalog_row, hash_action_content
from .contracts import (
    ERROR_MESSAGES,
    REQUEST_ID_RE,
    REVISION_TOKEN_RE,
    ObsidianError,
    parse_operator_action,
    require_opaque_id,
    require_request_id,
)
from .path_policy import normalize_vault_path

THIRTY_MINUTES = timedelta(minutes=30)
TEN_MINUTES = timedelta(minutes=10)
THIRTY_DAYS = timedelta(days=30)
RETENTION_MAX = 1000
MAX_ACTIVE_PROPOSALS = 64
MAX_RECORD_BYTES = 262_144
HEX32_JSON = __import__("re").compile(r"^[0-9a-f]{32}\.json$")
HEX64 = __import__("re").compile(r"^[0-9a-f]{64}$")
RECEIPT_FORBIDDEN = ("stdout", "stderr", "api_key", "nonce", "upstream", "authorization")
ACTION_STATE_SUBDIRS = ("proposals", "consumed", "receipts")
CURRENT_REVISION = object()
RECEIPT_KINDS = frozenset({"proposal", "approval", "rejection", "execution", "verification"})
RECEIPT_OUTCOMES = frozenset(
    {
        "created",
        "approved",
        "denied",
        "expired",
        "replayed",
        "stale",
        "cross_bound",
        "unverified",
        "verified",
        "failed",
    }
)


class ObsidianActionStoreError(ObsidianError):
    def __init__(self, outcome: str):
        self.outcome = outcome
        super().__init__("denied", ERROR_MESSAGES["denied"])


def _state_invalid() -> NoReturn:
    raise ObsidianError("protocol_error", ERROR_MESSAGES["protocol_error"])


def _environment_invalid() -> NoReturn:
    raise ObsidianError("invalid_request", ERROR_MESSAGES["invalid_request"])


def _parse_iso(value: str) -> datetime:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        _state_invalid()
    if stamp.tzinfo is None:
        _state_invalid()
    return stamp


def _iso(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def approval_expiry(approved_at: str, proposal_expires_at: str) -> datetime:
    return min(_parse_iso(approved_at) + TEN_MINUTES, _parse_iso(proposal_expires_at))


def receipt_contains_forbidden(record: object) -> bool:
    return any(needle in json.dumps(record).lower() for needle in RECEIPT_FORBIDDEN)


def _require_offset(start: str, end: str, delta: timedelta) -> None:
    if _parse_iso(end) != _parse_iso(start) + delta:
        _state_invalid()


def _target_config_path(action: Mapping[str, Any], target_config: Mapping[str, str] | None) -> tuple[str, object]:
    kind = action["kind"]
    if kind in {"create_note", "create_canvas", "create_base"}:
        return action["path"], "absent"
    if kind == "trash_note":
        return action["path"], CURRENT_REVISION
    if kind in {"patch_note", "patch_canvas", "patch_base", "update_excalidraw"}:
        return action["path"], action["ifMatch"]
    if kind in {"copy_note", "move_note"}:
        return action["from"], CURRENT_REVISION
    if kind == "apply_template":
        return action["path"], action.get("ifMatch", "absent")
    if kind == "append_flashcard":
        if target_config is None:
            _state_invalid()
        return target_config["flashcardNote"], action["ifMatch"]
    if kind == "create_excalidraw":
        if target_config is None:
            _state_invalid()
        if action.get("embed_path") is not None:
            return action["embed_path"], CURRENT_REVISION
        return f"03 - Resources/Excalidraw/{action['name']}{target_config['excalidrawSuffix']}", "absent"
    _state_invalid()
    raise AssertionError("unreachable")


def _embed_binding(action: Mapping[str, Any]) -> dict[str, Any]:
    if action["kind"] == "update_excalidraw" and action.get("embed_path") is not None:
        return {
            "embed_path": normalize_vault_path(action["embed_path"]),
            "embed_version": CURRENT_REVISION,
        }
    return {}


def derive_binding(action: Mapping[str, Any], target_config: Mapping[str, str] | None) -> dict[str, Any]:
    target_path, target_version = _target_config_path(action, target_config)
    binding = {
        "target_path": normalize_vault_path(target_path),
        "target_version": target_version,
        "blast_radius": get_catalog_row(action["kind"])["blast_radius"],
    }
    binding.update(_embed_binding(action))
    return binding


def _assert_embed_binding(action: Mapping[str, Any], record: Mapping[str, Any]) -> None:
    expected = _embed_binding(action)
    if expected:
        if record.get("embed_path") != expected["embed_path"]:
            _state_invalid()
        version = record.get("embed_version")
        if not isinstance(version, str) or not REVISION_TOKEN_RE.fullmatch(version):
            _state_invalid()
        return
    if "embed_path" in record or "embed_version" in record:
        _state_invalid()


def _assert_binding(
    action: Mapping[str, Any], record: Mapping[str, Any], target_config: Mapping[str, str] | None
) -> None:
    expected = derive_binding(action, target_config)
    if record["target_path"] != expected["target_path"] or record["blast_radius"] != expected["blast_radius"]:
        _state_invalid()
    _assert_embed_binding(action, record)
    if expected["target_version"] is CURRENT_REVISION:
        if not isinstance(record["target_version"], str) or not REVISION_TOKEN_RE.fullmatch(record["target_version"]):
            _state_invalid()
        return
    if record["target_version"] != expected["target_version"]:
        _state_invalid()


def _assert_template_digest(action: Mapping[str, Any], digest: str | None) -> None:
    if action["kind"] == "apply_template":
        if digest is None:
            _state_invalid()
        return
    if digest is not None:
        _state_invalid()


def parse_proposal_record(
    raw: object,
    target_config: Mapping[str, str] | None,
    expected_action_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        _state_invalid()
    required = {
        "version",
        "action_id",
        "request_id",
        "content_hash",
        "kind",
        "action",
        "target_path",
        "target_version",
        "blast_radius",
        "nonce",
        "created_at",
        "expires_at",
    }
    allowed = required | {"template_digest", "embed_path", "embed_version"}
    if raw.get("version") != 1 or not required <= set(raw) <= allowed:
        _state_invalid()
    action = parse_operator_action(raw.get("action"))
    if raw.get("kind") != action["kind"]:
        _state_invalid()
    digest = raw.get("template_digest")
    if digest is not None and (not isinstance(digest, str) or not HEX64.fullmatch(digest)):
        _state_invalid()
    _assert_template_digest(action, digest)
    payload = {"request_id": raw.get("request_id"), "action": action}
    if digest is not None:
        payload["template_digest"] = digest
    if hash_action_content(payload) != raw.get("content_hash"):
        _state_invalid()
    target_path = raw.get("target_path")
    if not isinstance(target_path, str) or normalize_vault_path(target_path) != target_path:
        _state_invalid()
    parsed = {
        "version": 1,
        "action_id": require_opaque_id(raw.get("action_id")),
        "request_id": require_request_id(raw.get("request_id")),
        "content_hash": raw["content_hash"],
        "kind": action["kind"],
        "action": action,
        "target_path": target_path,
        "target_version": raw.get("target_version"),
        "blast_radius": raw.get("blast_radius"),
        "nonce": require_opaque_id(raw.get("nonce")),
        "created_at": raw.get("created_at"),
        "expires_at": raw.get("expires_at"),
    }
    if "embed_path" in raw or "embed_version" in raw:
        embed_path = raw.get("embed_path")
        if not isinstance(embed_path, str) or normalize_vault_path(embed_path) != embed_path:
            _state_invalid()
        parsed["embed_path"] = embed_path
        parsed["embed_version"] = raw.get("embed_version")
    if not isinstance(parsed["blast_radius"], str) or not parsed["blast_radius"]:
        _state_invalid()
    if parsed["target_version"] != "absent" and (
        not isinstance(parsed["target_version"], str) or not REVISION_TOKEN_RE.fullmatch(parsed["target_version"])
    ):
        _state_invalid()
    _assert_binding(action, parsed, target_config)
    if not isinstance(parsed["created_at"], str) or not isinstance(parsed["expires_at"], str):
        _state_invalid()
    _require_offset(parsed["created_at"], parsed["expires_at"], THIRTY_MINUTES)
    if expected_action_id is not None and parsed["action_id"] != expected_action_id:
        _state_invalid()
    if digest is not None:
        parsed["template_digest"] = digest
    return parsed


def parse_approval_record(
    raw: object,
    target_config: Mapping[str, str] | None,
    expected_action_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict):
        _state_invalid()
    required = {
        "version",
        "action_id",
        "request_id",
        "content_hash",
        "kind",
        "action",
        "target_path",
        "target_version",
        "blast_radius",
        "nonce",
        "expires_at",
        "approved_at",
        "approval_receipt",
    }
    allowed = required | {"template_digest", "embed_path", "embed_version"}
    if raw.get("version") != 1 or not required <= set(raw) <= allowed:
        _state_invalid()
    action = parse_operator_action(raw.get("action"))
    if raw.get("kind") != action["kind"]:
        _state_invalid()
    digest = raw.get("template_digest")
    if digest is not None and (not isinstance(digest, str) or not HEX64.fullmatch(digest)):
        _state_invalid()
    _assert_template_digest(action, digest)
    payload = {"request_id": raw.get("request_id"), "action": action}
    if digest is not None:
        payload["template_digest"] = digest
    if hash_action_content(payload) != raw.get("content_hash"):
        _state_invalid()
    target_path = raw.get("target_path")
    if not isinstance(target_path, str) or normalize_vault_path(target_path) != target_path:
        _state_invalid()
    parsed = {
        "version": 1,
        "action_id": require_opaque_id(raw.get("action_id")),
        "request_id": require_request_id(raw.get("request_id")),
        "content_hash": raw["content_hash"],
        "kind": action["kind"],
        "action": action,
        "target_path": target_path,
        "target_version": raw.get("target_version"),
        "blast_radius": raw.get("blast_radius"),
        "nonce": require_opaque_id(raw.get("nonce")),
        "expires_at": raw.get("expires_at"),
        "approved_at": raw.get("approved_at"),
        "approval_receipt": require_opaque_id(raw.get("approval_receipt")),
    }
    if "embed_path" in raw or "embed_version" in raw:
        embed_path = raw.get("embed_path")
        if not isinstance(embed_path, str) or normalize_vault_path(embed_path) != embed_path:
            _state_invalid()
        parsed["embed_path"] = embed_path
        parsed["embed_version"] = raw.get("embed_version")
    if not isinstance(parsed["blast_radius"], str) or not parsed["blast_radius"]:
        _state_invalid()
    if parsed["target_version"] != "absent" and (
        not isinstance(parsed["target_version"], str) or not REVISION_TOKEN_RE.fullmatch(parsed["target_version"])
    ):
        _state_invalid()
    _assert_binding(action, parsed, target_config)
    if not isinstance(parsed["expires_at"], str) or not isinstance(parsed["approved_at"], str):
        _state_invalid()
    if _parse_iso(parsed["approved_at"]) > _parse_iso(parsed["expires_at"]):
        _state_invalid()
    approval_expiry(parsed["approved_at"], parsed["expires_at"])
    if expected_action_id is not None and parsed["action_id"] != expected_action_id:
        _state_invalid()
    if digest is not None:
        parsed["template_digest"] = digest
    return parsed


def parse_consumed_claim(
    raw: object,
    target_config: Mapping[str, str] | None,
    expected_action_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, dict) or "consumed_at" not in raw:
        _state_invalid()
    approval = parse_approval_record(
        {key: value for key, value in raw.items() if key != "consumed_at"}, target_config, expected_action_id
    )
    consumed_at = raw.get("consumed_at")
    if not isinstance(consumed_at, str):
        _state_invalid()
    _parse_iso(consumed_at)
    approval["consumed_at"] = consumed_at
    return approval


def parse_receipt_record(raw: object, expected_receipt_id: str | None = None) -> dict[str, Any]:
    if receipt_contains_forbidden(raw) or not isinstance(raw, dict):
        _state_invalid()
    if set(raw) != {"version", "receipt_id", "kind", "action_id", "outcome", "created_at"}:
        _state_invalid()
    if raw.get("version") != 1 or raw.get("kind") not in RECEIPT_KINDS or raw.get("outcome") not in RECEIPT_OUTCOMES:
        _state_invalid()
    parsed = {
        "version": 1,
        "receipt_id": require_opaque_id(raw.get("receipt_id")),
        "kind": raw["kind"],
        "action_id": require_opaque_id(raw.get("action_id")),
        "outcome": raw["outcome"],
        "created_at": raw.get("created_at"),
    }
    if not isinstance(parsed["created_at"], str):
        _state_invalid()
    _parse_iso(parsed["created_at"])
    if receipt_contains_forbidden(parsed):
        _state_invalid()
    if expected_receipt_id is not None and parsed["receipt_id"] != expected_receipt_id:
        _state_invalid()
    return parsed


def _ensure_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        if grokbot_ops._path_is_symlink(path) or path.is_symlink() or not path.is_dir():
            _state_invalid()
        info = path.lstat()
        if stat.S_IMODE(info.st_mode) != 0o700:
            _state_invalid()
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            _state_invalid()
        return
    path.mkdir(mode=0o700, parents=True)
    os.chmod(path, 0o700)
    info = path.lstat()
    if path.is_symlink() or not path.is_dir() or stat.S_IMODE(info.st_mode) != 0o700:
        _state_invalid()


def _write_exclusive_json(directory: Path, name: str, record: Mapping[str, Any]) -> None:
    payload = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    if len(payload.encode("utf-8")) > MAX_RECORD_BYTES:
        _state_invalid()
    path = directory / name
    if grokbot_ops._path_is_symlink(path) or path.exists():
        raise FileExistsError(str(path))
    try:
        grokbot_ops._write_text_nofollow_atomic(path, payload, mode=0o600, replace=False)
    except OSError as exc:
        if getattr(exc, "errno", None) == 17:
            raise FileExistsError(str(path)) from exc
        _state_invalid()


def _read_json(directory: Path, name: str) -> object:
    path = directory / name
    if grokbot_ops._path_is_symlink(path) or path.is_symlink():
        _state_invalid()
    try:
        raw = grokbot_ops._read_regular_text(path)
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError):
        _state_invalid()
    if len(raw.encode("utf-8")) > MAX_RECORD_BYTES:
        _state_invalid()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ObsidianError("protocol_error", ERROR_MESSAGES["protocol_error"]) from exc


def _hex_json_files(directory: Path) -> list[str]:
    names = []
    try:
        for entry in os.listdir(directory):
            if HEX32_JSON.fullmatch(entry):
                names.append(entry)
    except OSError:
        _state_invalid()
    return names


def _map_snapshot(
    action_id: str,
    proposal: Mapping[str, Any],
    approval: Mapping[str, Any] | None,
    consumed: Mapping[str, Any] | None,
    receipts: list[dict[str, Any]],
    now: datetime,
) -> dict[str, Any]:
    proposal_state = "expired" if now >= _parse_iso(proposal["expires_at"]) else "created"
    approval_state = "missing"
    if consumed is not None:
        approval_state = "consumed"
    elif approval is not None:
        approval_state = (
            "expired" if now >= approval_expiry(approval["approved_at"], proposal["expires_at"]) else "approved"
        )
    latest = max(receipts, key=lambda item: (_parse_iso(item["created_at"]), item["receipt_id"]), default=None)
    latest_execution = max(
        (item for item in receipts if item["kind"] == "execution"),
        key=lambda item: (_parse_iso(item["created_at"]), item["receipt_id"]),
        default=None,
    )
    latest_verification = max(
        (item for item in receipts if item["kind"] == "verification"),
        key=lambda item: (_parse_iso(item["created_at"]), item["receipt_id"]),
        default=None,
    )
    if (latest_execution is not None or latest_verification is not None) and consumed is None:
        _state_invalid()
    execution = "not_started"
    verification = "not_started"
    if consumed is not None:
        execution = "claimed"
        verification = "unknown_after_claim"
        if latest_execution is not None:
            if latest_execution["outcome"] == "verified":
                execution = "completed"
            elif latest_execution["outcome"] == "failed":
                execution = "failed"
            else:
                _state_invalid()
        if latest_verification is not None:
            if latest_verification["outcome"] == "verified":
                verification = "verified"
            elif latest_verification["outcome"] == "unverified":
                verification = "unverified"
            else:
                _state_invalid()
    return {
        "action_id": action_id,
        "proposal": proposal_state,
        "approval": approval_state,
        "execution": execution,
        "verification": verification,
        "receipt_ref": None if latest is None else latest["receipt_id"],
    }


class ObsidianActionStore:
    def __init__(
        self,
        action_state_path: str,
        approval_dir: str,
        *,
        target_config: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ):
        self.action_state_path = Path(action_state_path)
        self.approval_dir = Path(approval_dir)
        self.target_config = dict(target_config) if target_config is not None else None
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()

    def _ready(self) -> None:
        _ensure_directory(self.action_state_path)
        for name in ACTION_STATE_SUBDIRS:
            _ensure_directory(self.action_state_path / name)
        if not self.approval_dir.is_dir() or self.approval_dir.is_symlink():
            _environment_invalid()
        info = self.approval_dir.lstat()
        if stat.S_IMODE(info.st_mode) != 0o700:
            _environment_invalid()

    def _retain(self) -> None:
        cutoff = self.clock() - THIRTY_DAYS
        proposals = self.action_state_path / "proposals"
        consumed = self.action_state_path / "consumed"
        receipts = self.action_state_path / "receipts"
        consumed_names = set(_hex_json_files(consumed))
        entries: list[tuple[Path, str, datetime, bool]] = []
        now = self.clock()
        for name in _hex_json_files(proposals):
            record = parse_proposal_record(_read_json(proposals, name), self.target_config, name[:32])
            created = _parse_iso(record["created_at"])
            expired = now >= _parse_iso(record["expires_at"])
            entries.append(
                (
                    proposals,
                    name,
                    created,
                    name not in consumed_names and not expired and created >= cutoff,
                )
            )
        for name in _hex_json_files(receipts):
            record = parse_receipt_record(_read_json(receipts, name), name[:32])
            entries.append((receipts, name, _parse_iso(record["created_at"]), False))
        for name in consumed_names:
            record = parse_consumed_claim(_read_json(consumed, name), self.target_config, name[:32])
            entries.append((consumed, name, _parse_iso(record["consumed_at"]), False))
        stale = [entry for entry in entries if not entry[3] and entry[2] < cutoff]
        for directory, name, _at, _protect in stale:
            try:
                grokbot_ops.remove_regular_file(directory / name)
            except FileNotFoundError:
                continue
            except OSError:
                _state_invalid()
        remaining = [entry for entry in entries if entry not in stale]
        overflow = len(remaining) - RETENTION_MAX
        eligible = sorted((entry for entry in remaining if not entry[3]), key=lambda item: item[2])
        for directory, name, _at, _protect in eligible:
            if overflow <= 0:
                break
            try:
                grokbot_ops.remove_regular_file(directory / name)
            except FileNotFoundError:
                continue
            except OSError:
                _state_invalid()
            overflow -= 1

    def _remove_record(self, directory: Path, name: str) -> None:
        try:
            grokbot_ops.remove_regular_file(directory / name)
        except FileNotFoundError:
            return
        except OSError:
            _state_invalid()

    def _prune_expired(self) -> None:
        now = self.clock()
        proposals = self.action_state_path / "proposals"
        consumed = self.action_state_path / "consumed"
        receipts = self.action_state_path / "receipts"
        expired_ids: set[str] = set()
        for name in _hex_json_files(proposals):
            record = parse_proposal_record(_read_json(proposals, name), self.target_config, name[:32])
            if now >= _parse_iso(record["expires_at"]):
                expired_ids.add(record["action_id"])
                self._remove_record(proposals, name)
        for name in _hex_json_files(consumed):
            if name[:32] in expired_ids:
                self._remove_record(consumed, name)
        for name in _hex_json_files(receipts):
            record = parse_receipt_record(_read_json(receipts, name), name[:32])
            if record["action_id"] in expired_ids:
                self._remove_record(receipts, name)

    def _active_proposal_count(self) -> int:
        now = self.clock()
        consumed = set(_hex_json_files(self.action_state_path / "consumed"))
        proposals = self.action_state_path / "proposals"
        count = 0
        for name in _hex_json_files(proposals):
            if name in consumed:
                continue
            record = parse_proposal_record(_read_json(proposals, name), self.target_config, name[:32])
            if now < _parse_iso(record["expires_at"]):
                count += 1
        return count

    def ready(self) -> None:
        with self._lock:
            self._ready()
            self._prune_expired()
            self._retain()

    def create_proposal(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self._ready()
            self._prune_expired()
            if self._active_proposal_count() >= MAX_ACTIVE_PROPOSALS:
                raise ObsidianActionStoreError("denied")
            parsed = parse_proposal_record(record, self.target_config)
            try:
                _write_exclusive_json(self.action_state_path / "proposals", f"{parsed['action_id']}.json", parsed)
            except FileExistsError as exc:
                raise ObsidianActionStoreError("denied") from exc
            self._retain()

    def read_proposal(self, action_id: str) -> dict[str, Any] | None:
        with self._lock:
            key = require_opaque_id(action_id)
            try:
                raw = _read_json(self.action_state_path / "proposals", f"{key}.json")
            except FileNotFoundError:
                return None
            return parse_proposal_record(raw, self.target_config, key)

    def read_approval(self, action_id: str) -> dict[str, Any] | None:
        with self._lock:
            key = require_opaque_id(action_id)
            try:
                raw = _read_json(self.approval_dir, f"{key}.json")
            except FileNotFoundError:
                return None
            return parse_approval_record(raw, self.target_config, key)

    def claim_consumed(self, record: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ready()
            self._prune_expired()
            parsed = parse_consumed_claim(record, self.target_config)
            try:
                _write_exclusive_json(self.action_state_path / "consumed", f"{parsed['action_id']}.json", parsed)
            except FileExistsError as exc:
                raise ObsidianActionStoreError("replayed") from exc
            claimed = parse_consumed_claim(
                _read_json(self.action_state_path / "consumed", f"{parsed['action_id']}.json"),
                self.target_config,
                parsed["action_id"],
            )
            self._retain()
            return claimed

    def write_receipt(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self._ready()
            parsed = parse_receipt_record(record)
            try:
                _write_exclusive_json(self.action_state_path / "receipts", f"{parsed['receipt_id']}.json", parsed)
            except FileExistsError as exc:
                raise ObsidianActionStoreError("denied") from exc
            self._retain()

    def find_proposal_by_request_id(self, request_id: str) -> dict[str, Any] | None:
        with self._lock:
            if not isinstance(request_id, str) or not REQUEST_ID_RE.fullmatch(request_id):
                _state_invalid()
            found = None
            proposals = self.action_state_path / "proposals"
            for name in _hex_json_files(proposals):
                record = parse_proposal_record(_read_json(proposals, name), self.target_config, name[:32])
                if record["request_id"] != request_id:
                    continue
                if found is not None:
                    _state_invalid()
                found = record
            return found

    def snapshot(self, action_id: str) -> dict[str, Any] | None:
        with self._lock:
            key = require_opaque_id(action_id)
            try:
                proposal = parse_proposal_record(
                    _read_json(self.action_state_path / "proposals", f"{key}.json"),
                    self.target_config,
                    key,
                )
            except FileNotFoundError:
                return None
            consumed = None
            try:
                consumed = parse_consumed_claim(
                    _read_json(self.action_state_path / "consumed", f"{key}.json"),
                    self.target_config,
                    key,
                )
            except FileNotFoundError:
                consumed = None
            receipts = []
            for name in _hex_json_files(self.action_state_path / "receipts"):
                receipt = parse_receipt_record(_read_json(self.action_state_path / "receipts", name), name[:32])
                if receipt["action_id"] == key:
                    receipts.append(receipt)
            approval = None
            try:
                approval = parse_approval_record(_read_json(self.approval_dir, f"{key}.json"), self.target_config, key)
            except FileNotFoundError:
                approval = None
            return _map_snapshot(key, proposal, approval, consumed, receipts, self.clock())


def write_approval_file(
    approval_dir: str,
    record: Mapping[str, Any],
    target_config: Mapping[str, str] | None = None,
) -> None:
    directory = Path(approval_dir)
    _ensure_directory(directory)
    parsed = parse_approval_record(record, target_config)
    try:
        _write_exclusive_json(directory, f"{parsed['action_id']}.json", parsed)
    except FileExistsError as exc:
        raise ObsidianActionStoreError("denied") from exc
