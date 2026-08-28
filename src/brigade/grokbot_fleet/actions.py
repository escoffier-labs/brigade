"""Fleet Steward action catalog, exclusive store, and research-bridge restarter."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import threading
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, NoReturn

from .contracts import (
    OPAQUE_ID_RE,
    FleetError,
    is_offset_datetime,
    omit_undefined,
    parse_identifier,
    parse_opaque_id,
)
from .exec import EXEC_DEFAULT_OUTPUT_BYTES, ExecRequest, Runner, run_exec
from .probes import PROBE_WORKING_DIRECTORY, _health_class
from .runtime_config import FLEET_SAFE_SERVICE_UNIT_PATTERN

PROPOSAL_TTL_MS = 15 * 60 * 1000
MAX_PROPOSALS = 64
MAX_PROPOSAL_BYTES = 262_144
MAX_RECEIPTS = 256
MAX_RECEIPT_BYTES = 1_048_576
MAX_CONSUMED = 128
MAX_CONSUMED_BYTES = 262_144
EXECUTABLE_ACTION: dict[str, Any] = {
    "action_id": "restart-service",
    "verification_id": "verify-service",
    "rollback_id": "rollback-service",
    "service_id": "research-bridge",
    "target_alias": "control-plane",
    "finding_kind": "unhealthy-service",
    "finding_id": "research-bridge:unhealthy-service",
    "automatic_rollback": False,
}
REVISION_RE = __import__("re").compile(r"^[0-9a-f]{64}$")
INVOCATION_ID_RE = __import__("re").compile(r"^[0-9a-f]{32}$")
MONOTONIC_RE = __import__("re").compile(r"^[0-9]+$")
SHOW_TIMEOUT_MS = 12_000
SHOW_PROPERTIES = (
    "Id",
    "ActiveState",
    "SubState",
    "StateChangeTimestampMonotonic",
    "InvocationID",
)
RECEIPT_FORBIDDEN = ("stdout", "stderr", "nonce", "finding_revision", "system_revision")
ACTION_STATE_SUBDIRS = ("proposals", "consumed", "receipts", "reservations")
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


class FleetActionStoreError(FleetError):
    def __init__(self, outcome: str):
        self.outcome = outcome
        super().__init__("denied", "Fleet request was denied")


def _action_state_invalid() -> NoReturn:
    raise FleetError("protocol_error", "Fleet action state is invalid")


def _environment_invalid() -> NoReturn:
    raise FleetError("invalid_request", "Fleet environment is invalid")


def _action_unavailable() -> NoReturn:
    raise FleetError("unavailable", "Fleet action is unavailable")


def is_action_eligible_finding(finding: Mapping[str, Any]) -> bool:
    observed = finding.get("observed_at")
    if not isinstance(observed, str) or not observed:
        return False
    try:
        parse_identifier(finding.get("incident_receipt_ref"))
    except FleetError:
        return False
    return True


def finding_revision(finding: Mapping[str, Any]) -> str:
    if not is_action_eligible_finding(finding):
        raise FleetError("denied", "Fleet request was denied")
    payload = {
        "blast_radius": finding["blast_radius"],
        "finding_id": finding["finding_id"],
        "incident_receipt_ref": finding["incident_receipt_ref"],
        "observed_at": finding["observed_at"],
        "proposed_action_id": finding["proposed_action_id"],
        "reason": finding["reason"],
        "rollback_id": finding["rollback_id"],
        "target_alias": finding["target_alias"],
        "verification_id": finding["verification_id"],
    }
    return hashlib.sha256(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()


def is_executable_remediation(finding: Mapping[str, Any]) -> bool:
    return (
        is_action_eligible_finding(finding)
        and finding.get("finding_id") == EXECUTABLE_ACTION["finding_id"]
        and finding.get("target_alias") == EXECUTABLE_ACTION["target_alias"]
        and finding.get("proposed_action_id") == EXECUTABLE_ACTION["action_id"]
        and finding.get("verification_id") == EXECUTABLE_ACTION["verification_id"]
        and finding.get("rollback_id") == EXECUTABLE_ACTION["rollback_id"]
        and finding.get("blast_radius") == "one registered service"
    )


def _parse_revision(value: object) -> str:
    if not isinstance(value, str) or not REVISION_RE.fullmatch(value):
        _action_state_invalid()
    return value


def _require_datetime(value: object) -> str:
    if not is_offset_datetime(value):
        _action_state_invalid()
    return str(value)


def _parse_proposal_record(raw: object, expected: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "version",
        "proposal_id",
        "finding_id",
        "service_id",
        "target_alias",
        "finding_revision",
        "system_revision",
        "action_id",
        "verification_id",
        "rollback_id",
        "nonce",
        "created_at",
        "expires_at",
    }:
        _action_state_invalid()
    if raw.get("version") != 1:
        _action_state_invalid()
    record = {
        "version": 1,
        "proposal_id": parse_opaque_id(raw.get("proposal_id")),
        "finding_id": parse_identifier(raw.get("finding_id")),
        "service_id": raw.get("service_id"),
        "target_alias": raw.get("target_alias"),
        "finding_revision": _parse_revision(raw.get("finding_revision")),
        "system_revision": _parse_revision(raw.get("system_revision")),
        "action_id": raw.get("action_id"),
        "verification_id": raw.get("verification_id"),
        "rollback_id": raw.get("rollback_id"),
        "nonce": parse_opaque_id(raw.get("nonce")),
        "created_at": _require_datetime(raw.get("created_at")),
        "expires_at": _require_datetime(raw.get("expires_at")),
    }
    if record["service_id"] != "research-bridge" or record["target_alias"] != "control-plane":
        _action_state_invalid()
    if record["action_id"] != "restart-service" or record["verification_id"] != "verify-service":
        _action_state_invalid()
    if record["rollback_id"] != "rollback-service":
        _action_state_invalid()
    if expected is not None and record["proposal_id"] != expected:
        _action_state_invalid()
    return record


def _parse_approval_record(raw: object, expected: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "version",
        "proposal_id",
        "finding_id",
        "service_id",
        "target_alias",
        "finding_revision",
        "system_revision",
        "action_id",
        "verification_id",
        "rollback_id",
        "nonce",
        "expires_at",
        "approved_at",
    }:
        _action_state_invalid()
    record = {
        **_parse_proposal_record(
            {
                **{key: raw[key] for key in raw if key not in {"approved_at"}},
                "created_at": raw.get("expires_at"),
            },
            expected,
        )
    }
    record.pop("created_at")
    record["approved_at"] = _require_datetime(raw.get("approved_at"))
    record["expires_at"] = _require_datetime(raw.get("expires_at"))
    return record


def _parse_consumed_claim(raw: object, expected: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or "consumed_at" not in raw:
        _action_state_invalid()
    approval = _parse_approval_record({key: raw[key] for key in raw if key != "consumed_at"}, expected)
    approval["consumed_at"] = _require_datetime(raw.get("consumed_at"))
    return approval


def _parse_revision_claim(raw: object, expected: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "version",
        "proposal_id",
        "system_revision",
        "claimed_at",
    }:
        _action_state_invalid()
    if raw.get("version") != 1:
        _action_state_invalid()
    record = {
        "version": 1,
        "proposal_id": parse_opaque_id(raw.get("proposal_id")),
        "system_revision": _parse_revision(raw.get("system_revision")),
        "claimed_at": _require_datetime(raw.get("claimed_at")),
    }
    if expected is not None and record["system_revision"] != expected:
        _action_state_invalid()
    return record


def _parse_reservation(raw: object, expected: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "version",
        "reservation_id",
        "proposal_id",
        "slot_count",
        "reserved_bytes",
        "created_at",
    }:
        _action_state_invalid()
    if raw.get("version") != 1:
        _action_state_invalid()
    slot_count = raw.get("slot_count")
    reserved_bytes = raw.get("reserved_bytes")
    if not isinstance(slot_count, int) or isinstance(slot_count, bool) or slot_count < 1:
        _action_state_invalid()
    if not isinstance(reserved_bytes, int) or isinstance(reserved_bytes, bool) or reserved_bytes < 1:
        _action_state_invalid()
    record = {
        "version": 1,
        "reservation_id": parse_opaque_id(raw.get("reservation_id")),
        "proposal_id": parse_opaque_id(raw.get("proposal_id")),
        "slot_count": slot_count,
        "reserved_bytes": reserved_bytes,
        "created_at": _require_datetime(raw.get("created_at")),
    }
    if expected is not None and record["reservation_id"] != expected:
        _action_state_invalid()
    return record


def _parse_receipt(raw: object) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        _action_state_invalid()
    allowed = {
        "version",
        "receipt_id",
        "kind",
        "proposal_id",
        "finding_id",
        "service_id",
        "target_alias",
        "action_id",
        "verification_id",
        "rollback_id",
        "outcome",
        "created_at",
    }
    required = {"version", "receipt_id", "kind", "proposal_id", "outcome", "created_at"}
    if not set(raw) <= allowed or not required <= set(raw) or raw.get("version") != 1:
        _action_state_invalid()
    if raw.get("kind") not in RECEIPT_KINDS or raw.get("outcome") not in RECEIPT_OUTCOMES:
        _action_state_invalid()
    receipt = {
        "version": 1,
        "receipt_id": parse_opaque_id(raw.get("receipt_id")),
        "kind": raw["kind"],
        "proposal_id": parse_opaque_id(raw.get("proposal_id")),
        "outcome": raw["outcome"],
        "created_at": _require_datetime(raw.get("created_at")),
    }
    if "finding_id" in raw:
        receipt["finding_id"] = parse_identifier(raw.get("finding_id"))
    for key, expected in (
        ("service_id", "research-bridge"),
        ("target_alias", "control-plane"),
        ("action_id", "restart-service"),
        ("verification_id", "verify-service"),
        ("rollback_id", "rollback-service"),
    ):
        if key in raw:
            if raw[key] != expected:
                _action_state_invalid()
            receipt[key] = raw[key]
    receipt = omit_undefined(receipt)
    serialized = json.dumps(receipt)
    if any(needle in serialized for needle in RECEIPT_FORBIDDEN):
        _action_state_invalid()
    return receipt


def _assert_owner_dir(path: Path, *, create: bool) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            _environment_invalid()
        try:
            path.mkdir(parents=True, mode=0o700)
            os.chmod(path, 0o700)
        except OSError:
            _action_state_invalid()
        info = path.lstat()
    except OSError:
        _environment_invalid()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        if create:
            _action_state_invalid()
        _environment_invalid()
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        if create:
            _action_state_invalid()
        _environment_invalid()


def _assert_owner_file(info: os.stat_result) -> None:
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        _action_state_invalid()
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        _action_state_invalid()


def _write_all(handle: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(handle, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _write_exclusive_json(path: Path, record: Mapping[str, Any]) -> None:
    handle = None
    created = False
    try:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        os.fchmod(handle, 0o600)
        _write_all(handle, json.dumps(record).encode("utf-8"))
        os.fsync(handle)
        os.close(handle)
        handle = None
    except FileExistsError as exc:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
        raise FleetActionStoreError("denied") from exc
    except FleetError:
        raise
    except OSError:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
        if created:
            _unlink_quiet(path)
        _action_state_invalid()


def _read_safe_json(path: Path) -> Any:
    handle = None
    try:
        handle = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        info = os.fstat(handle)
        _assert_owner_file(info)
        raw = os.read(handle, 1_048_576)
        os.close(handle)
        handle = None
        return json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        raise
    except FleetError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
        _action_state_invalid()


def _fsync_directory(path: Path) -> None:
    handle = None
    try:
        handle = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(handle)
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
            _action_state_invalid()
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            _action_state_invalid()
        os.fsync(handle)
        os.close(handle)
    except FleetError:
        raise
    except OSError:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
        _action_state_invalid()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_record_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _proposal_is_live(record: Mapping[str, Any], now: datetime) -> bool:
    return _parse_record_time(record["expires_at"]) > now


def _list_json_files(directory: Path) -> list[Path]:
    try:
        entries = list(directory.iterdir())
    except OSError:
        _action_state_invalid()
    files: list[Path] = []
    for path in entries:
        try:
            info = path.lstat()
        except OSError:
            _action_state_invalid()
        if stat.S_ISLNK(info.st_mode):
            _action_state_invalid()
        if not stat.S_ISREG(info.st_mode) or path.suffix != ".json":
            continue
        files.append(path)
    files.sort(key=lambda item: item.name)
    return files


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        _action_state_invalid()


def _unlink_required(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        _action_state_invalid()


def _encoded_size(record: Mapping[str, Any]) -> int:
    return len(json.dumps(record).encode("utf-8"))


def _path_exists_nofollow(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        _action_state_invalid()
    return True


class FleetActionStore:
    """Exclusive proposal, approval, claim, and receipt store."""

    def __init__(self, *, action_state_path: str, approval_dir: str):
        self._root = Path(action_state_path)
        self._approvals = Path(approval_dir)
        self._lock = threading.Lock()

    def ready(self) -> None:
        with self._lock:
            self._ensure_ready()

    def create_proposal(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self._ensure_ready()
            parsed = _parse_proposal_record(record)
            path = self._root / "proposals" / f"{parsed['proposal_id']}.json"
            if _path_exists_nofollow(path):
                raise FleetActionStoreError("denied")
            self._prune_action_state()
            self._assert_within_bounds(
                self._root / "proposals",
                incoming=_encoded_size(parsed),
                max_count=MAX_PROPOSALS,
                max_bytes=MAX_PROPOSAL_BYTES,
            )
            _write_exclusive_json(path, parsed)

    def read_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            key = parse_opaque_id(proposal_id)
            path = self._root / "proposals" / f"{key}.json"
            try:
                raw = _read_safe_json(path)
            except FileNotFoundError:
                return None
            except FleetError:
                raise
            except Exception:
                _action_state_invalid()
            return _parse_proposal_record(raw, key)

    def read_approval(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            key = parse_opaque_id(proposal_id)
            path = self._approvals / f"{key}.json"
            try:
                raw = _read_safe_json(path)
            except FileNotFoundError:
                return None
            except FleetError:
                raise
            except Exception:
                _action_state_invalid()
            return _parse_approval_record(raw, key)

    def claim_consumed(self, record: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ensure_ready()
            parsed = _parse_consumed_claim(record)
            revision_path = self._root / "consumed" / f"{parsed['system_revision']}.json"
            consumed_path = self._root / "consumed" / f"{parsed['proposal_id']}.json"
            revision_exists = _path_exists_nofollow(revision_path)
            consumed_exists = _path_exists_nofollow(consumed_path)
            if revision_exists or consumed_exists:
                if not (revision_exists and consumed_exists):
                    _action_state_invalid()
                raise FleetActionStoreError("replayed")
            self._prune_action_state()
            revision_claim = {
                "version": 1,
                "proposal_id": parsed["proposal_id"],
                "system_revision": parsed["system_revision"],
                "claimed_at": parsed["consumed_at"],
            }
            self._assert_within_bounds(
                self._root / "consumed",
                incoming=_encoded_size(revision_claim) + _encoded_size(parsed),
                incoming_count=2,
                max_count=MAX_CONSUMED,
                max_bytes=MAX_CONSUMED_BYTES,
            )
            try:
                _write_exclusive_json(revision_path, revision_claim)
            except FleetActionStoreError as exc:
                if exc.outcome == "denied":
                    raise FleetActionStoreError("replayed") from exc
                raise
            try:
                _write_exclusive_json(consumed_path, parsed)
            except FleetActionStoreError as exc:
                _unlink_quiet(revision_path)
                if exc.outcome == "denied":
                    raise FleetActionStoreError("replayed") from exc
                raise
            except Exception:
                _unlink_quiet(revision_path)
                raise
            _fsync_directory(consumed_path.parent)
            try:
                raw = _read_safe_json(consumed_path)
            except FleetError:
                raise
            except Exception:
                _action_state_invalid()
            return _parse_consumed_claim(raw, parsed["proposal_id"])

    def write_receipt(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            self._ensure_ready()
            receipt = _parse_receipt(record)
            self._prune_action_state()
            if self._has_identical_rejection(receipt):
                return
            self._assert_receipt_capacity(1, _encoded_size(receipt))
            _write_exclusive_json(self._root / "receipts" / f"{receipt['receipt_id']}.json", receipt)

    def reserve_receipt_capacity(self, records: Sequence[Mapping[str, Any]]) -> str:
        with self._lock:
            self._ensure_ready()
            parsed = [_parse_receipt(record) for record in records]
            if not parsed:
                _action_state_invalid()
            proposal_ids = {item["proposal_id"] for item in parsed}
            if len(proposal_ids) != 1:
                _action_state_invalid()
            proposal_id = next(iter(proposal_ids))
            incoming_bytes = sum(_encoded_size(item) for item in parsed)
            self._prune_action_state()
            if self._reservation_path_for_proposal(proposal_id) is not None:
                raise FleetActionStoreError("denied")
            self._assert_receipt_capacity(len(parsed), incoming_bytes)
            reservation_id = secrets.token_hex(16)
            reservation_path = self._root / "reservations" / f"{reservation_id}.json"
            reservation = {
                "version": 1,
                "reservation_id": reservation_id,
                "proposal_id": proposal_id,
                "slot_count": len(parsed),
                "reserved_bytes": incoming_bytes,
                "created_at": _utc_now().isoformat().replace("+00:00", "Z"),
            }
            _write_exclusive_json(reservation_path, reservation)
            _fsync_directory(reservation_path.parent)
            return reservation_id

    def commit_reserved_receipts(self, reservation_id: str, records: Sequence[Mapping[str, Any]]) -> None:
        with self._lock:
            self._ensure_ready()
            key = parse_opaque_id(reservation_id)
            reservation_path = self._root / "reservations" / f"{key}.json"
            try:
                raw = _read_safe_json(reservation_path)
            except FileNotFoundError:
                _action_state_invalid()
            reservation = _parse_reservation(raw, key)
            parsed = [_parse_receipt(record) for record in records]
            if len(parsed) != reservation["slot_count"]:
                _action_state_invalid()
            incoming = sum(_encoded_size(item) for item in parsed)
            if incoming > reservation["reserved_bytes"]:
                raise FleetActionStoreError("denied")
            written: list[Path] = []
            try:
                for receipt in parsed:
                    dest = self._root / "receipts" / f"{receipt['receipt_id']}.json"
                    _write_exclusive_json(dest, receipt)
                    written.append(dest)
                _unlink_required(reservation_path)
                _fsync_directory(reservation_path.parent)
                _fsync_directory(self._root / "receipts")
            except Exception:
                for dest in written:
                    _unlink_quiet(dest)
                raise

    def release_receipt_reservation(self, reservation_id: str) -> None:
        with self._lock:
            self._ensure_ready()
            key = parse_opaque_id(reservation_id)
            reservation_path = self._root / "reservations" / f"{key}.json"
            if _path_exists_nofollow(reservation_path):
                _unlink_required(reservation_path)
                _fsync_directory(reservation_path.parent)

    def _ensure_ready(self) -> None:
        _assert_owner_dir(self._root, create=True)
        for name in ACTION_STATE_SUBDIRS:
            _assert_owner_dir(self._root / name, create=True)
        _assert_owner_dir(self._approvals, create=False)

    def _live_proposal_ids(self, now: datetime) -> set[str]:
        live: set[str] = set()
        for path in _list_json_files(self._root / "proposals"):
            try:
                raw = _read_safe_json(path)
            except FileNotFoundError:
                _action_state_invalid()
            record = _parse_proposal_record(raw, path.stem)
            if _proposal_is_live(record, now):
                live.add(record["proposal_id"])
        return live

    def _prune_action_state(self) -> None:
        self._prune_expired_proposals()
        self._prune_terminal_receipts()
        self._prune_terminal_consumed()
        self._prune_stale_reservations()

    def _consumed_pairs(self) -> list[tuple[Path, Path, str]]:
        proposal_files: dict[str, tuple[Path, dict[str, Any]]] = {}
        revision_files: dict[str, tuple[Path, dict[str, Any]]] = {}
        for path in _list_json_files(self._root / "consumed"):
            try:
                raw = _read_safe_json(path)
            except FileNotFoundError:
                _action_state_invalid()
            stem = path.stem
            if REVISION_RE.fullmatch(stem):
                record = _parse_revision_claim(raw, stem)
                revision_files[record["system_revision"]] = (path, record)
            elif OPAQUE_ID_RE.fullmatch(stem):
                record = _parse_consumed_claim(raw, stem)
                proposal_files[record["proposal_id"]] = (path, record)
            else:
                _action_state_invalid()
        pairs: list[tuple[Path, Path, str]] = []
        matched: set[str] = set()
        for proposal_id, (path, record) in proposal_files.items():
            revision = record["system_revision"]
            if revision not in revision_files:
                _action_state_invalid()
            revision_path, revision_record = revision_files[revision]
            if revision_record["proposal_id"] != proposal_id:
                _action_state_invalid()
            matched.add(revision)
            pairs.append((path, revision_path, proposal_id))
        if set(revision_files) - matched:
            _action_state_invalid()
        return pairs

    def _prune_terminal_consumed(self) -> None:
        live = self._live_proposal_ids(_utc_now())
        for proposal_path, revision_path, proposal_id in self._consumed_pairs():
            if proposal_id not in live:
                _unlink_required(proposal_path)
                _unlink_required(revision_path)

    def _reservation_path_for_proposal(self, proposal_id: str) -> Path | None:
        for path in _list_json_files(self._root / "reservations"):
            try:
                raw = _read_safe_json(path)
            except FileNotFoundError:
                _action_state_invalid()
            reservation = _parse_reservation(raw, path.stem)
            if reservation["proposal_id"] == proposal_id:
                return path
        return None

    def _prune_stale_reservations(self) -> None:
        live = self._live_proposal_ids(_utc_now())
        for path in _list_json_files(self._root / "reservations"):
            try:
                raw = _read_safe_json(path)
            except FileNotFoundError:
                _action_state_invalid()
            reservation = _parse_reservation(raw, path.stem)
            if reservation["proposal_id"] not in live:
                _unlink_required(path)

    def _receipt_occupancy(self) -> tuple[int, int]:
        files = _list_json_files(self._root / "receipts")
        count = len(files)
        total = 0
        for path in files:
            total += _file_size(path)
        for path in _list_json_files(self._root / "reservations"):
            try:
                raw = _read_safe_json(path)
            except FileNotFoundError:
                _action_state_invalid()
            reservation = _parse_reservation(raw, path.stem)
            count += reservation["slot_count"]
            total += reservation["reserved_bytes"]
        return count, total

    def _assert_receipt_capacity(self, incoming_count: int, incoming_bytes: int) -> None:
        count, total = self._receipt_occupancy()
        if count + incoming_count > MAX_RECEIPTS or total + incoming_bytes > MAX_RECEIPT_BYTES:
            raise FleetActionStoreError("denied")

    def _prune_expired_proposals(self) -> None:
        now = _utc_now()
        for path in _list_json_files(self._root / "proposals"):
            try:
                raw = _read_safe_json(path)
            except FileNotFoundError:
                _action_state_invalid()
            record = _parse_proposal_record(raw, path.stem)
            if not _proposal_is_live(record, now):
                _unlink_required(path)

    def _prune_terminal_receipts(self) -> None:
        live = self._live_proposal_ids(_utc_now())
        for path in _list_json_files(self._root / "receipts"):
            try:
                raw = _read_safe_json(path)
            except FileNotFoundError:
                _action_state_invalid()
            receipt = _parse_receipt(raw)
            if receipt["proposal_id"] not in live:
                _unlink_required(path)

    def _has_identical_rejection(self, receipt: Mapping[str, Any]) -> bool:
        if receipt["kind"] != "rejection":
            return False
        for path in _list_json_files(self._root / "receipts"):
            try:
                raw = _read_safe_json(path)
            except FileNotFoundError:
                _action_state_invalid()
            existing = _parse_receipt(raw)
            if (
                existing["kind"] == "rejection"
                and existing["proposal_id"] == receipt["proposal_id"]
                and existing["outcome"] == receipt["outcome"]
            ):
                return True
        return False

    def _assert_within_bounds(
        self,
        directory: Path,
        *,
        incoming: int,
        max_count: int,
        max_bytes: int,
        incoming_count: int = 1,
    ) -> None:
        files = _list_json_files(directory)
        total_bytes = 0
        for path in files:
            total_bytes += _file_size(path)
        if len(files) + incoming_count > max_count or total_bytes + incoming > max_bytes:
            raise FleetActionStoreError("denied")


def write_fleet_approval_file(approval_dir: str, record: Mapping[str, Any]) -> None:
    directory = Path(approval_dir)
    _assert_owner_dir(directory, create=True)
    parsed = _parse_approval_record(record)
    _write_exclusive_json(directory / f"{parsed['proposal_id']}.json", parsed)


def _parse_show_stdout(stdout: str, unit: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in stdout.splitlines():
        if not line.strip():
            continue
        separator = line.find("=")
        if separator <= 0:
            _action_unavailable()
        key = line[:separator]
        value = line[separator + 1 :]
        if key not in SHOW_PROPERTIES or key in parsed:
            _action_unavailable()
        parsed[key] = value
    for property_name in SHOW_PROPERTIES:
        if not parsed.get(property_name):
            _action_unavailable()
    if parsed["Id"] != unit:
        _action_unavailable()
    if not MONOTONIC_RE.fullmatch(parsed["StateChangeTimestampMonotonic"]):
        _action_unavailable()
    if not INVOCATION_ID_RE.fullmatch(parsed["InvocationID"]):
        _action_unavailable()
    return parsed


def _canonical_revision(fields: Mapping[str, str]) -> str:
    payload = {
        "ActiveState": fields["ActiveState"],
        "Id": fields["Id"],
        "InvocationID": fields["InvocationID"],
        "StateChangeTimestampMonotonic": fields["StateChangeTimestampMonotonic"],
        "SubState": fields["SubState"],
    }
    return hashlib.sha256(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()


def _show_args(unit: str) -> tuple[str, ...]:
    return (
        "--user",
        "show",
        unit,
        "--property=Id",
        "--property=ActiveState",
        "--property=SubState",
        "--property=StateChangeTimestampMonotonic",
        "--property=InvocationID",
    )


class ResearchBridgeRestarter:
    def __init__(
        self,
        *,
        systemctl_file: str,
        mapping: Mapping[str, str],
        env: Mapping[str, str],
        runner: Runner | None = None,
    ):
        self._systemctl = systemctl_file
        self._mapping = mapping
        self._env = env
        self._runner = runner

    def _unit(self) -> str:
        if self._mapping.get("manager") != "user":
            _action_unavailable()
        unit = self._mapping.get("unit")
        if not isinstance(unit, str) or not FLEET_SAFE_SERVICE_UNIT_PATTERN.fullmatch(unit):
            _action_unavailable()
        return unit

    def _run_show(self, unit: str) -> dict[str, str]:
        result = run_exec(
            ExecRequest(
                file=self._systemctl,
                args=_show_args(unit),
                cwd=PROBE_WORKING_DIRECTORY,
                timeout_ms=SHOW_TIMEOUT_MS,
                max_buffer_bytes=EXEC_DEFAULT_OUTPUT_BYTES,
                env=self._env,
            ),
            runner=self._runner,
        )
        return _parse_show_stdout(result.stdout, unit)

    def observe_research_bridge_revision(self) -> dict[str, str]:
        unit = self._unit()
        try:
            fields = self._run_show(unit)
            return {
                "system_revision": _canonical_revision(fields),
                "health_class": _health_class(fields["ActiveState"]),
            }
        except FleetError:
            _action_unavailable()
        except Exception:
            _action_unavailable()

    def restart_research_bridge(self) -> None:
        unit = self._unit()
        try:
            run_exec(
                ExecRequest(
                    file=self._systemctl,
                    args=("--user", "restart", unit),
                    cwd=PROBE_WORKING_DIRECTORY,
                    timeout_ms=SHOW_TIMEOUT_MS,
                    max_buffer_bytes=EXEC_DEFAULT_OUTPUT_BYTES,
                    env=self._env,
                ),
                runner=self._runner,
            )
            fields = self._run_show(unit)
            if fields["Id"] != unit or fields["ActiveState"] != "active":
                _action_unavailable()
        except FleetError:
            _action_unavailable()
        except Exception:
            _action_unavailable()
