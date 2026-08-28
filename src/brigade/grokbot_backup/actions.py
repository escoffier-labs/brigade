"""Backup Steward action catalog, exclusive store, and fixed-runner executor."""

from __future__ import annotations

import json
import os
import secrets
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn, Sequence

from .contracts import (
    ACTION_IDS,
    OPERATION_STATES,
    REVISION_RE,
    BackupError,
    is_offset_datetime,
    parse_action_id,
    parse_identifier,
)
from .exec import EXEC_DEFAULT_OUTPUT_BYTES, BackupProcessLimiter, ExecRequest, Runner, create_process_limiter, run_exec
from .ledger import backup_finding_revision
from .normalize import sanitize_backup_detail

PROPOSAL_TTL_MS = 15 * 60 * 1000
MAX_PROPOSALS = 64
MAX_OPERATIONS = 64
MAX_CONSUMED = 128
MAX_SUMMARY_BYTES = 4_096
RESTIC_TARGETS = ("configuration-nas", "configuration-cloud", "media-archive")
ACTION_RUNNERS = {
    "run-backup": "restic-backup-runner-v1",
    "run-integrity-check": "restic-integrity-runner-v1",
    "run-restore-rehearsal": "restic-rehearsal-runner-v1",
}
ACTION_COPY = {
    "run-backup": {
        "blast_radius": "one registered restic target",
        "verification_statement": "compare the next snapshot receipt",
        "recovery_statement": "operator reruns the approved backup",
    },
    "run-integrity-check": {
        "blast_radius": "one registered restic target",
        "verification_statement": "compare the integrity receipt",
        "recovery_statement": "operator reruns the approved integrity check",
    },
    "run-restore-rehearsal": {
        "blast_radius": "one registered restic target",
        "verification_statement": "compare the rehearsal receipt",
        "recovery_statement": "operator reruns the approved rehearsal",
    },
}
ACTION_STATE_SUBDIRS = ("proposals", "consumed", "operations")
WORKING_DIRECTORY = "/"
TARGET_SELECTOR_RE = __import__("re").compile(r"^(?:--)?[A-Za-z0-9][A-Za-z0-9._:-]*$")


def _denied() -> NoReturn:
    raise BackupError("denied", "Backup request was denied")


def _action_state_invalid() -> NoReturn:
    raise BackupError("protocol_error", "Backup action state is invalid")


def _environment_invalid() -> NoReturn:
    raise BackupError("invalid_request", "Backup environment is invalid")


def _write_all(handle: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(handle, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def catalog_entry(alias: str, action_id: str) -> dict[str, Any] | None:
    if alias not in RESTIC_TARGETS or action_id not in ACTION_IDS:
        return None
    copy = ACTION_COPY[action_id]
    return {
        "action_id": action_id,
        "target_alias": alias,
        "runner_id": ACTION_RUNNERS[action_id],
        "blast_radius": copy["blast_radius"],
        "verification_statement": copy["verification_statement"],
        "recovery_statement": copy["recovery_statement"],
        "automatic_rollback": False,
    }


def catalog_actions_for(alias: str) -> tuple[str, ...]:
    return tuple(
        entry["action_id"] for entry in (catalog_entry(alias, action_id) for action_id in sorted(ACTION_IDS)) if entry
    )


def catalog_target_selector(alias: str, action_id: str) -> str:
    entry = catalog_entry(alias, action_id)
    if entry is None or len(entry["target_alias"]) > 64 or not TARGET_SELECTOR_RE.fullmatch(entry["target_alias"]):
        _denied()
    return entry["target_alias"]


def proposal_finding_revision(finding: Mapping[str, Any]) -> str:
    observed = finding.get("observed_at")
    receipt = finding.get("receipt_ref")
    if not isinstance(observed, str) or not observed or not isinstance(receipt, str) or not receipt:
        _denied()
    return backup_finding_revision(finding)


def _require_datetime(value: object) -> str:
    if not is_offset_datetime(value):
        _action_state_invalid()
    return str(value)


def _parse_revision(value: object) -> str:
    if not isinstance(value, str) or not REVISION_RE.fullmatch(value):
        _action_state_invalid()
    return value


def _parse_proposal_record(raw: object, expected: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "version",
        "proposal_id",
        "target_alias",
        "action_id",
        "finding_id",
        "finding_revision",
        "nonce",
        "created_at",
        "expires_at",
    }:
        _action_state_invalid()
    if raw.get("version") != 1:
        _action_state_invalid()
    record = {
        "version": 1,
        "proposal_id": parse_identifier(raw.get("proposal_id")),
        "target_alias": parse_identifier(raw.get("target_alias")),
        "action_id": parse_action_id(raw.get("action_id")),
        "finding_id": parse_identifier(raw.get("finding_id")),
        "finding_revision": _parse_revision(raw.get("finding_revision")),
        "nonce": parse_identifier(raw.get("nonce")),
        "created_at": _require_datetime(raw.get("created_at")),
        "expires_at": _require_datetime(raw.get("expires_at")),
    }
    if expected is not None and record["proposal_id"] != expected:
        _action_state_invalid()
    return record


def _parse_approval_record(raw: object, expected: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "version",
        "proposal_id",
        "target_alias",
        "action_id",
        "finding_id",
        "finding_revision",
        "nonce",
        "expires_at",
        "approved_at",
    }:
        _action_state_invalid()
    if raw.get("version") != 1:
        _action_state_invalid()
    record = {
        "version": 1,
        "proposal_id": parse_identifier(raw.get("proposal_id")),
        "target_alias": parse_identifier(raw.get("target_alias")),
        "action_id": parse_action_id(raw.get("action_id")),
        "finding_id": parse_identifier(raw.get("finding_id")),
        "finding_revision": _parse_revision(raw.get("finding_revision")),
        "nonce": parse_identifier(raw.get("nonce")),
        "expires_at": _require_datetime(raw.get("expires_at")),
        "approved_at": _require_datetime(raw.get("approved_at")),
    }
    if expected is not None and record["proposal_id"] != expected:
        _action_state_invalid()
    return record


def _parse_consumed_record(raw: object, expected: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or "consumed_at" not in raw:
        _action_state_invalid()
    approval = _parse_approval_record({key: raw[key] for key in raw if key != "consumed_at"}, expected)
    approval["consumed_at"] = _require_datetime(raw.get("consumed_at"))
    return approval


def _parse_operation_record(raw: object, expected: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != {
        "version",
        "operation_id",
        "target_alias",
        "action_id",
        "state",
        "created_at",
        "updated_at",
        "summary",
        "receipt_ref",
    }:
        _action_state_invalid()
    if raw.get("version") != 1 or raw.get("state") not in OPERATION_STATES:
        _action_state_invalid()
    summary = raw.get("summary")
    if not isinstance(summary, str) or len(summary.encode("utf-8")) > MAX_SUMMARY_BYTES:
        _action_state_invalid()
    record = {
        "version": 1,
        "operation_id": parse_identifier(raw.get("operation_id")),
        "target_alias": parse_identifier(raw.get("target_alias")),
        "action_id": parse_action_id(raw.get("action_id")),
        "state": raw["state"],
        "created_at": _require_datetime(raw.get("created_at")),
        "updated_at": _require_datetime(raw.get("updated_at")),
        "summary": summary,
        "receipt_ref": None if raw.get("receipt_ref") is None else parse_identifier(raw.get("receipt_ref")),
    }
    if expected is not None and record["operation_id"] != expected:
        _action_state_invalid()
    return record


def _public_operation(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": record["operation_id"],
        "target_alias": record["target_alias"],
        "action_id": record["action_id"],
        "state": record["state"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "summary": record["summary"],
        "receipt_ref": record["receipt_ref"],
    }


def _is_active(state: str) -> bool:
    return state in {"queued", "running"}


def _same_proposal_fingerprint(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return (
        left["target_alias"] == right["target_alias"]
        and left["action_id"] == right["action_id"]
        and left["finding_id"] == right["finding_id"]
        and left["finding_revision"] == right["finding_revision"]
    )


def _assert_safe_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except OSError:
        _environment_invalid()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        _environment_invalid()


def _ensure_writable_directory(path: Path) -> None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        try:
            path.mkdir(parents=True, mode=0o700)
            os.chmod(path, 0o700)
        except OSError:
            _action_state_invalid()
        info = path.lstat()
    except OSError:
        _action_state_invalid()
    if path.is_symlink() or not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        _action_state_invalid()


def _read_json_file(path: Path) -> object:
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise
    except OSError:
        _action_state_invalid()
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        _action_state_invalid()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _action_state_invalid()


def _write_exclusive_json(path: Path, record: Mapping[str, Any]) -> None:
    handle = None
    try:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.fchmod(handle, 0o600)
        _write_all(handle, json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        os.fsync(handle)
        os.close(handle)
        handle = None
    except FileExistsError:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
        _denied()
    except OSError as exc:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
        raise BackupError("protocol_error", "Backup action state is invalid") from exc


def _write_atomic_json(path: Path, record: Mapping[str, Any]) -> None:
    temp = Path(f"{path}.tmp")
    handle = None
    try:
        handle = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        os.fchmod(handle, 0o600)
        _write_all(handle, json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        os.fsync(handle)
        os.close(handle)
        handle = None
        os.replace(temp, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        if handle is not None:
            try:
                os.close(handle)
            except OSError:
                pass
        try:
            temp.unlink()
        except OSError:
            pass
        if isinstance(exc, FileExistsError):
            _denied()
        raise BackupError("protocol_error", "Backup action state is invalid") from exc


class BackupActionStore:
    """Exclusive proposal, approval, consumed-claim, and operation store."""

    def __init__(
        self,
        *,
        action_state_path: str,
        approval_dir: str,
        now: Callable[[], datetime] | None = None,
        create_proposal_id: Callable[[], str] | None = None,
        create_nonce: Callable[[], str] | None = None,
    ):
        self._root = Path(action_state_path)
        self._approvals = Path(approval_dir)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._create_proposal_id = create_proposal_id or (lambda: secrets.token_hex(16))
        self._create_nonce = create_nonce or (lambda: secrets.token_hex(16))
        self._lock = threading.Lock()

    def ready(self) -> None:
        with self._lock:
            self._ensure_ready()

    def create_proposal(self, input_value: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._ensure_ready()
            parsed = {
                "target_alias": parse_identifier(input_value.get("target_alias")),
                "action_id": parse_action_id(input_value.get("action_id")),
                "finding_id": parse_identifier(input_value.get("finding_id")),
                "finding_revision": _parse_revision(input_value.get("finding_revision")),
            }
            existing = [
                record
                for record in self._list_proposals()
                if _same_proposal_fingerprint(record, parsed) and _parse_iso(record["expires_at"]) > self._now()
            ]
            if existing:
                return existing[0]
            self._prune_expired_proposals()
            created_at = self._now()
            proposal_id = parse_identifier(self._create_proposal_id())
            record = {
                "version": 1,
                "proposal_id": proposal_id,
                "target_alias": parsed["target_alias"],
                "action_id": parsed["action_id"],
                "finding_id": parsed["finding_id"],
                "finding_revision": parsed["finding_revision"],
                "nonce": parse_identifier(self._create_nonce()),
                "created_at": _iso(created_at),
                "expires_at": _iso(created_at + timedelta(milliseconds=PROPOSAL_TTL_MS)),
            }
            _write_exclusive_json(self._proposal_path(proposal_id), record)
            return record

    def read_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._load_proposal(proposal_id)

    def read_approval(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._load_approval(proposal_id)

    def consume_approved_proposal(self, proposal_id: str) -> dict[str, Any]:
        with self._lock:
            self._ensure_ready()
            proposal = self._load_proposal(proposal_id)
            approval = self._load_approval(proposal_id)
            if proposal is None or approval is None:
                _denied()
            current = self._now()
            if _parse_iso(proposal["expires_at"]) <= current or _parse_iso(approval["expires_at"]) <= current:
                _denied()
            if any(
                proposal[key] != approval[key]
                for key in ("nonce", "target_alias", "action_id", "finding_id", "finding_revision")
            ):
                _denied()
            consumed = {**approval, "consumed_at": _iso(current)}
            _write_exclusive_json(self._consumed_path(proposal["proposal_id"]), consumed)
            return consumed

    def write_operation(self, operation: Mapping[str, Any]) -> None:
        with self._lock:
            self._ensure_ready()
            parsed = _parse_operation_record({"version": 1, **dict(operation)})
            current = self._list_operations()
            existing = next((item for item in current if item["operation_id"] == parsed["operation_id"]), None)
            if existing is None and _is_active(parsed["state"]):
                if any(
                    item["target_alias"] == parsed["target_alias"] and _is_active(item["state"]) for item in current
                ):
                    _denied()
            self._prune_terminal_operations(current)
            _write_atomic_json(self._operation_path(parsed["operation_id"]), parsed)

    def read_operation(self, operation_id: str) -> dict[str, Any] | None:
        with self._lock:
            loaded = self._load_operation(operation_id)
            return None if loaded is None else _public_operation(loaded)

    def active_operations(self) -> list[dict[str, Any]]:
        with self._lock:
            return [_public_operation(item) for item in self._list_operations() if _is_active(item["state"])]

    def write_approval_file(self, record: Mapping[str, Any]) -> None:
        with self._lock:
            _ensure_writable_directory(self._approvals)
            parsed = _parse_approval_record(record)
            _write_exclusive_json(self._approval_path(parsed["proposal_id"]), parsed)

    def _ensure_ready(self) -> None:
        _ensure_writable_directory(self._root)
        for name in ACTION_STATE_SUBDIRS:
            _ensure_writable_directory(self._root / name)
        _assert_safe_directory(self._approvals)

    def _proposal_path(self, proposal_id: str) -> Path:
        return self._root / "proposals" / f"{proposal_id}.json"

    def _consumed_path(self, proposal_id: str) -> Path:
        return self._root / "consumed" / f"{proposal_id}.json"

    def _operation_path(self, operation_id: str) -> Path:
        return self._root / "operations" / f"{operation_id}.json"

    def _approval_path(self, proposal_id: str) -> Path:
        return self._approvals / f"{proposal_id}.json"

    def _load_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        key = parse_identifier(proposal_id)
        try:
            raw = _read_json_file(self._proposal_path(key))
        except FileNotFoundError:
            return None
        return _parse_proposal_record(raw, key)

    def _load_approval(self, proposal_id: str) -> dict[str, Any] | None:
        key = parse_identifier(proposal_id)
        try:
            raw = _read_json_file(self._approval_path(key))
        except FileNotFoundError:
            return None
        return _parse_approval_record(raw, key)

    def _load_operation(self, operation_id: str) -> dict[str, Any] | None:
        key = parse_identifier(operation_id)
        try:
            raw = _read_json_file(self._operation_path(key))
        except FileNotFoundError:
            return None
        return _parse_operation_record(raw, key)

    def _list_json_records(
        self, directory: Path, loader: Callable[[str], dict[str, Any] | None]
    ) -> list[dict[str, Any]]:
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            return []
        except OSError:
            _action_state_invalid()
        records = []
        for name in names:
            if not name.endswith(".json"):
                continue
            loaded = loader(name[:-5])
            if loaded is not None:
                records.append(loaded)
        return records

    def _list_proposals(self) -> list[dict[str, Any]]:
        return self._list_json_records(self._root / "proposals", self._load_proposal)

    def _list_operations(self) -> list[dict[str, Any]]:
        return self._list_json_records(self._root / "operations", self._load_operation)

    def _list_consumed(self) -> list[Path]:
        directory = self._root / "consumed"
        try:
            return [directory / name for name in os.listdir(directory) if name.endswith(".json")]
        except FileNotFoundError:
            return []
        except OSError:
            _action_state_invalid()

    def _prune_expired_proposals(self) -> None:
        now = self._now()
        for record in self._list_proposals():
            if _parse_iso(record["expires_at"]) > now:
                continue
            try:
                self._proposal_path(record["proposal_id"]).unlink()
            except OSError:
                _action_state_invalid()
        remaining = self._list_proposals()
        if len(remaining) >= MAX_PROPOSALS:
            _action_state_invalid()
        live_ids = {record["proposal_id"] for record in remaining}
        consumed = self._list_consumed()
        for path in sorted(consumed, key=lambda item: item.name):
            if path.stem in live_ids:
                continue
            try:
                path.unlink()
                consumed.remove(path)
            except OSError:
                _action_state_invalid()
        if len(consumed) > MAX_CONSUMED:
            _action_state_invalid()

    def _prune_terminal_operations(self, current: list[dict[str, Any]]) -> None:
        if len(current) < MAX_OPERATIONS:
            return
        removable = [item for item in current if not _is_active(item["state"])]
        for item in removable:
            if len(current) < MAX_OPERATIONS:
                break
            try:
                self._operation_path(item["operation_id"]).unlink()
                current.remove(item)
            except OSError:
                _action_state_invalid()
        if len(current) >= MAX_OPERATIONS:
            _action_state_invalid()


class BackupActionExecutor:
    def __init__(
        self,
        *,
        runtime: Mapping[str, Any],
        store: BackupActionStore,
        env: Mapping[str, str],
        now: Callable[[], datetime],
        create_operation_id: Callable[[], str],
        create_receipt_ref: Callable[[], str],
        secrets: Sequence[str],
        runner: Runner | None = None,
        process_limiter: BackupProcessLimiter | None = None,
        schedule: Callable[[Callable[[], None]], None] | None = None,
    ):
        self.runtime = runtime
        self.store = store
        self.env = env
        self.now = now
        self.create_operation_id = create_operation_id
        self.create_receipt_ref = create_receipt_ref
        self.secrets = secrets
        self.runner = runner
        self.limiter = process_limiter or create_process_limiter()
        self.schedule = schedule or _default_schedule

    def start(self, input_value: Mapping[str, Any]) -> dict[str, Any]:
        try:
            target_alias = parse_identifier(input_value.get("target_alias"))
            action_id = parse_action_id(input_value.get("action_id"))
            parse_identifier(input_value.get("proposal_id"))
        except BackupError:
            raise BackupError("invalid_request", "Backup action request is invalid") from None
        entry = catalog_entry(target_alias, action_id)
        if entry is None or not self.runtime["safety_ready"]:
            _denied()
        command = self.runtime["action_runners"].get(entry["runner_id"])
        if command is None:
            _denied()
        created_at = _iso(self.now())
        queued = {
            "operation_id": parse_identifier(self.create_operation_id()),
            "target_alias": target_alias,
            "action_id": action_id,
            "state": "queued",
            "created_at": created_at,
            "updated_at": created_at,
            "summary": "queued",
            "receipt_ref": self.create_receipt_ref(),
        }
        self.store.write_operation(queued)
        self.schedule(lambda: self._run(queued, entry, command))
        return queued

    def _run(self, operation: Mapping[str, Any], entry: Mapping[str, Any], command: Mapping[str, Any]) -> None:
        target = self.runtime["targets"][operation["target_alias"]]
        running = {
            **operation,
            "state": "running",
            "updated_at": _iso(self.now()),
            "summary": "running",
        }
        self.store.write_operation(running)
        try:
            selector = catalog_target_selector(entry["target_alias"], entry["action_id"])
            self.limiter.run(
                lambda: run_exec(
                    ExecRequest(
                        file=command["executable"],
                        args=tuple(command["args"]) + (selector,),
                        cwd=WORKING_DIRECTORY,
                        timeout_ms=target["timeout_ms"],
                        max_buffer_bytes=EXEC_DEFAULT_OUTPUT_BYTES,
                        env=self.env,
                    ),
                    runner=self.runner,
                )
            )
            self.store.write_operation(
                {**running, "state": "succeeded", "updated_at": _iso(self.now()), "summary": "succeeded"}
            )
        except Exception as exc:
            failure = exc if isinstance(exc, BackupError) else BackupError("unavailable", "Backup action failed")
            self.store.write_operation(
                {
                    **running,
                    "state": "failed",
                    "updated_at": _iso(self.now()),
                    "summary": sanitize_backup_detail(failure.message, self.secrets),
                }
            )


def _default_schedule(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True).start()


def _iso(value: datetime) -> str:
    stamp = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return stamp.isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
