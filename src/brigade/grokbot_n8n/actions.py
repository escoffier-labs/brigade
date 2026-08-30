"""n8n-operator action catalog, exclusive proposal store, and consume-before-write."""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import stat
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, NoReturn

from .contracts import (
    ACTION_STATES,
    ERROR_MESSAGES,
    PROPOSAL_TTL_MS,
    REVISION_RE,
    N8nError,
    is_offset_datetime,
    parse_action_id,
    parse_safe_path_segment,
    target_type_for_action,
)
from . import runtime_config

APPROVAL_KEYS = frozenset(
    {
        "version",
        "proposal_id",
        "action_id",
        "target_id",
        "target_type",
        "revision",
        "approved_at",
    }
)
PROPOSAL_KEYS = frozenset(
    {
        "version",
        "proposal_id",
        "action_id",
        "target_id",
        "target_type",
        "revision",
        "created_at",
        "expires_at",
    }
)
CONSUMED_KEYS = PROPOSAL_KEYS | frozenset({"approved_at", "consumed_at", "result"})
ACTION_STATE_SUBDIRS = ("proposals", "consumed")
MAX_PROPOSALS = 64
MAX_CONSUMED = 128
MAX_ACTION_STATE_BYTES = 16_384
RESULTS = frozenset({"pending_write", "succeeded", "failed"})


def _denied() -> NoReturn:
    raise N8nError("denied", ERROR_MESSAGES["denied"])


def _action_state_invalid() -> NoReturn:
    raise N8nError("protocol_error", "n8n action state is invalid")


def _environment_invalid() -> NoReturn:
    raise N8nError("invalid_request", "n8n environment is invalid")


def _write_all(handle: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(handle, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


def _close_fd(handle: int) -> None:
    try:
        os.close(handle)
    except OSError:
        pass


def _fsync_dir(parent: int) -> None:
    try:
        os.fsync(parent)
    except OSError:
        pass


def _is_reparse(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_point = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse_point)


def _require_posix_permissions() -> None:
    if runtime_config.permission_policy() != "posix":
        _environment_invalid()


def _directory_flags() -> int:
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)


def _open_child(parent: int, name: str, flags: int, mode: int = 0o600) -> int:
    _require_posix_permissions()
    return os.open(
        name,
        flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
        mode,
        dir_fd=parent,
    )


def _unlink_child(parent: int, name: str) -> None:
    _require_posix_permissions()
    os.unlink(name, dir_fd=parent)


def _replace_children(parent: int, source: str, destination: str) -> None:
    _require_posix_permissions()
    os.replace(source, destination, src_dir_fd=parent, dst_dir_fd=parent)


def _assert_dir_info(info: os.stat_result, *, private: bool, on_error: Callable[[], NoReturn]) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
        on_error()
    if private:
        if stat.S_IMODE(info.st_mode) != 0o700:
            on_error()
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            on_error()


def _open_or_create_dir(
    parent: int,
    name: str,
    *,
    create: bool,
    private: bool,
    on_error: Callable[[], NoReturn],
) -> int:
    _require_posix_permissions()
    created = False
    try:
        child = os.open(name, _directory_flags(), dir_fd=parent)
    except FileNotFoundError:
        if not create:
            on_error()
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            created = True
            child = os.open(name, _directory_flags(), dir_fd=parent)
        except OSError:
            on_error()
    except OSError:
        on_error()
    try:
        info = os.fstat(child)
        _assert_dir_info(info, private=private or created, on_error=on_error)
        if private or created:
            os.fchmod(child, 0o700)
            _assert_dir_info(os.fstat(child), private=True, on_error=on_error)
        return child
    except N8nError:
        _close_fd(child)
        raise


def _walk_directory(path: Path, *, create: bool, private: bool, on_error: Callable[[], NoReturn]) -> int:
    _require_posix_permissions()
    absolute = Path(os.path.abspath(path))
    if not str(absolute).startswith("/") or "\0" in str(absolute):
        on_error()
    root_flags = getattr(os, "O_PATH", os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(absolute.anchor, root_flags)
    except OSError:
        on_error()
    try:
        parts = absolute.parts[1:]
        for index, component in enumerate(parts):
            if component in {"", ".", ".."}:
                on_error()
            is_final = index == len(parts) - 1
            child = _open_or_create_dir(
                descriptor,
                component,
                create=create,
                private=private and is_final,
                on_error=on_error,
            )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except N8nError:
        _close_fd(descriptor)
        raise
    except OSError:
        _close_fd(descriptor)
        on_error()


class _HeldState:
    def __init__(self, root: int, proposals: int, consumed: int, approvals: int):
        self.root = root
        self.proposals = proposals
        self.consumed = consumed
        self.approvals = approvals

    def close(self) -> None:
        for handle in (self.approvals, self.consumed, self.proposals, self.root):
            if handle != -1:
                _close_fd(handle)


def target_revision(projection: Mapping[str, Any]) -> str:
    canonical = json.dumps(dict(projection), separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _iso(value: datetime) -> str:
    stamp = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return stamp.isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        _action_state_invalid()
    if parsed.tzinfo is None:
        _action_state_invalid()
    return parsed


def _require_datetime(value: object) -> str:
    if not is_offset_datetime(value):
        _action_state_invalid()
    _parse_iso(str(value))
    return str(value)


def _parse_revision(value: object) -> str:
    if not isinstance(value, str) or not REVISION_RE.fullmatch(value):
        _action_state_invalid()
    return value


def _parse_proposal_record(raw: object, expected: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != PROPOSAL_KEYS:
        _action_state_invalid()
    if raw.get("version") != 1:
        _action_state_invalid()
    action_id = parse_action_id(raw.get("action_id"))
    record = {
        "version": 1,
        "proposal_id": parse_safe_path_segment(raw.get("proposal_id")),
        "action_id": action_id,
        "target_id": parse_safe_path_segment(raw.get("target_id")),
        "target_type": raw.get("target_type"),
        "revision": _parse_revision(raw.get("revision")),
        "created_at": _require_datetime(raw.get("created_at")),
        "expires_at": _require_datetime(raw.get("expires_at")),
    }
    if record["target_type"] != target_type_for_action(action_id):
        _action_state_invalid()
    if expected is not None and record["proposal_id"] != expected:
        _action_state_invalid()
    return record


def _parse_approval_record(raw: object, expected: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != APPROVAL_KEYS:
        _denied()
    if raw.get("version") != 1:
        _denied()
    try:
        action_id = parse_action_id(raw.get("action_id"))
        record = {
            "version": 1,
            "proposal_id": parse_safe_path_segment(raw.get("proposal_id")),
            "action_id": action_id,
            "target_id": parse_safe_path_segment(raw.get("target_id")),
            "target_type": raw.get("target_type"),
            "revision": _parse_revision(raw.get("revision")),
            "approved_at": _require_datetime(raw.get("approved_at")),
        }
    except N8nError:
        _denied()
    if record["target_type"] != target_type_for_action(action_id):
        _denied()
    if expected is not None and record["proposal_id"] != expected:
        _denied()
    return record


def _parse_consumed_record(raw: object, expected: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != CONSUMED_KEYS:
        _action_state_invalid()
    proposal = _parse_proposal_record({key: raw[key] for key in PROPOSAL_KEYS}, expected)
    if raw.get("result") not in RESULTS:
        _action_state_invalid()
    proposal["approved_at"] = _require_datetime(raw.get("approved_at"))
    proposal["consumed_at"] = _require_datetime(raw.get("consumed_at"))
    proposal["result"] = raw["result"]
    return proposal


def _assert_readable_file(info: os.stat_result, *, required_mode: bool, on_error: Callable[[], NoReturn]) -> None:
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or _is_reparse(info):
        on_error()
    if required_mode and stat.S_IMODE(info.st_mode) != 0o600:
        on_error()
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        on_error()


def _file_identity(info: os.stat_result) -> tuple[int, int]:
    return (info.st_dev, info.st_ino)


def _pin_identity(current: tuple[int, int] | None, handle: int) -> tuple[int, int]:
    identity = _file_identity(os.fstat(handle))
    if current is not None and current != identity:
        _action_state_invalid()
    return identity


def _read_json_from_parent(
    parent: int,
    name: str,
    *,
    required_mode: bool,
    on_error: Callable[[], NoReturn],
) -> object:
    descriptor = -1
    try:
        try:
            prior = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            raise
        except OSError:
            on_error()
        _assert_readable_file(prior, required_mode=required_mode, on_error=on_error)
        descriptor = _open_child(parent, name, os.O_RDONLY)
        info = os.fstat(descriptor)
        _assert_readable_file(info, required_mode=required_mode, on_error=on_error)
        if _file_identity(info) != _file_identity(prior):
            on_error()
        if info.st_size > MAX_ACTION_STATE_BYTES:
            on_error()
        payload = os.read(descriptor, MAX_ACTION_STATE_BYTES + 1)
        if len(payload) > MAX_ACTION_STATE_BYTES:
            on_error()
        return json.loads(payload.decode("utf-8"))
    except N8nError:
        raise
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        on_error()
    finally:
        if descriptor != -1:
            _close_fd(descriptor)


def _write_exclusive_json(parent: int, name: str, record: Mapping[str, Any]) -> None:
    handle = None
    try:
        handle = _open_child(parent, name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.fchmod(handle, 0o600)
        _write_all(handle, json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        os.fsync(handle)
        os.close(handle)
        handle = None
        _fsync_dir(parent)
    except FileExistsError:
        if handle is not None:
            _close_fd(handle)
            handle = None
        _denied()
    except OSError as exc:
        if handle is not None:
            _close_fd(handle)
            handle = None
        raise N8nError("protocol_error", "n8n action state is invalid") from exc
    finally:
        if handle is not None:
            _close_fd(handle)


def _write_atomic_json(parent: int, name: str, record: Mapping[str, Any]) -> None:
    handle = None
    temporary = f".{name}.{secrets.token_hex(16)}.tmp"
    created = False
    try:
        handle = _open_child(parent, temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        os.fchmod(handle, 0o600)
        _write_all(handle, json.dumps(record, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        os.fsync(handle)
        os.close(handle)
        handle = None
        _replace_children(parent, temporary, name)
        created = False
        _fsync_dir(parent)
    except OSError as exc:
        raise N8nError("protocol_error", "n8n action state is invalid") from exc
    finally:
        if handle is not None:
            _close_fd(handle)
        if created:
            try:
                _unlink_child(parent, temporary)
            except OSError:
                pass


def _enumerate_names(dir_fd: int, *, cap: int) -> list[str]:
    names: list[str] = []
    try:
        with os.scandir(dir_fd) as entries:
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    _action_state_invalid()
                if entry.is_symlink() or stat.S_ISLNK(info.st_mode) or _is_reparse(info):
                    _action_state_invalid()
                names.append(entry.name)
                if len(names) > cap:
                    _action_state_invalid()
    except OSError:
        _action_state_invalid()
    return [name for name in names if name.endswith(".json") and not name.startswith(".")]


class N8nActionStore:
    """Exclusive proposal and consumed-claim store. Approvals are operator-created."""

    def __init__(
        self,
        *,
        action_state_path: str,
        approval_dir: str,
        now: Callable[[], datetime] | None = None,
        create_proposal_id: Callable[[], str] | None = None,
    ):
        self._root = Path(action_state_path)
        self.approval_dir = approval_dir
        self._approvals = Path(approval_dir)
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._create_proposal_id = create_proposal_id or (lambda: secrets.token_hex(16))
        self._lock = threading.Lock()
        self._root_identity: tuple[int, int] | None = None
        self._proposals_identity: tuple[int, int] | None = None
        self._consumed_identity: tuple[int, int] | None = None
        self._approvals_identity: tuple[int, int] | None = None

    def ready(self) -> None:
        with self._lock:
            state = self._hold_state()
            state.close()

    def create_proposal(self, input_value: Mapping[str, Any], *, revision: str) -> dict[str, Any]:
        with self._lock:
            state = self._hold_state()
            try:
                self._prune_expired_proposals(state)
                action_id = parse_action_id(input_value.get("action_id"))
                target_id = parse_safe_path_segment(input_value.get("target_id"))
                created_at = self._now()
                proposal_id = parse_safe_path_segment(self._create_proposal_id())
                record = {
                    "version": 1,
                    "proposal_id": proposal_id,
                    "action_id": action_id,
                    "target_id": target_id,
                    "target_type": target_type_for_action(action_id),
                    "revision": _parse_revision(revision),
                    "created_at": _iso(created_at),
                    "expires_at": _iso(created_at + timedelta(milliseconds=PROPOSAL_TTL_MS)),
                }
                _write_exclusive_json(state.proposals, f"{proposal_id}.json", record)
                return record
            finally:
                state.close()

    def read_proposal(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._hold_state()
            try:
                return self._load_proposal(state, proposal_id)
            finally:
                state.close()

    def read_approval(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._hold_state()
            try:
                return self._load_approval(state, proposal_id)
            finally:
                state.close()

    def read_consumed(self, proposal_id: str) -> dict[str, Any] | None:
        with self._lock:
            state = self._hold_state()
            try:
                return self._load_consumed(state, proposal_id)
            finally:
                state.close()

    def consume_approved_proposal(self, proposal_id: str, *, current_revision: str) -> dict[str, Any]:
        with self._lock:
            state = self._hold_state()
            try:
                if self._load_consumed(state, proposal_id) is not None:
                    _denied()
                proposal = self._load_proposal(state, proposal_id)
                approval = self._load_approval(state, proposal_id)
                if proposal is None or approval is None:
                    _denied()
                current = self._now()
                if _parse_iso(proposal["expires_at"]) <= current:
                    _denied()
                if any(
                    proposal[key] != approval[key]
                    for key in ("proposal_id", "action_id", "target_id", "target_type", "revision")
                ):
                    _denied()
                if current_revision != proposal["revision"]:
                    _denied()
                consumed = {
                    **proposal,
                    "approved_at": approval["approved_at"],
                    "consumed_at": _iso(current),
                    "result": "pending_write",
                }
                _write_exclusive_json(state.consumed, f"{proposal['proposal_id']}.json", consumed)
                return consumed
            finally:
                state.close()

    def mark_consumed_result(self, proposal_id: str, result: str) -> None:
        with self._lock:
            state = self._hold_state()
            try:
                if result not in RESULTS:
                    _action_state_invalid()
                consumed = self._load_consumed(state, proposal_id)
                if consumed is None:
                    _action_state_invalid()
                consumed["result"] = result
                _write_atomic_json(state.consumed, f"{parse_safe_path_segment(proposal_id)}.json", consumed)
            finally:
                state.close()

    def public_status(self, proposal_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._hold_state()
            try:
                key = parse_safe_path_segment(proposal_id)
                consumed = self._load_consumed(state, key)
                if consumed is not None:
                    if consumed["result"] == "succeeded":
                        status = "consumed"
                    elif consumed["result"] == "failed":
                        status = "failed"
                    else:
                        status = "unknown"
                    return {
                        "proposal_id": consumed["proposal_id"],
                        "action_id": consumed["action_id"],
                        "target_type": consumed["target_type"],
                        "state": status,
                    }
                proposal = self._load_proposal(state, key)
                if proposal is None:
                    return {"proposal_id": key, "state": "unknown"}
                if _parse_iso(proposal["expires_at"]) <= self._now():
                    return {
                        "proposal_id": proposal["proposal_id"],
                        "action_id": proposal["action_id"],
                        "target_type": proposal["target_type"],
                        "state": "expired",
                    }
                try:
                    approval = self._load_approval(state, key)
                except N8nError:
                    approval = None
                bound = approval is not None and all(
                    approval[field] == proposal[field]
                    for field in ("proposal_id", "action_id", "target_id", "target_type", "revision")
                )
                status = "approved" if bound else "pending"
                if status not in ACTION_STATES:
                    status = "unknown"
                return {
                    "proposal_id": proposal["proposal_id"],
                    "action_id": proposal["action_id"],
                    "target_type": proposal["target_type"],
                    "state": status,
                }
            finally:
                state.close()

    def _hold_state(self) -> _HeldState:
        root = _walk_directory(self._root, create=True, private=True, on_error=_action_state_invalid)
        proposals = -1
        consumed = -1
        approvals = -1
        try:
            self._root_identity = _pin_identity(self._root_identity, root)
            proposals = _open_or_create_dir(
                root, ACTION_STATE_SUBDIRS[0], create=True, private=True, on_error=_action_state_invalid
            )
            self._proposals_identity = _pin_identity(self._proposals_identity, proposals)
            consumed = _open_or_create_dir(
                root, ACTION_STATE_SUBDIRS[1], create=True, private=True, on_error=_action_state_invalid
            )
            self._consumed_identity = _pin_identity(self._consumed_identity, consumed)
            approvals = _walk_directory(self._approvals, create=False, private=True, on_error=_environment_invalid)
            self._approvals_identity = _pin_identity(self._approvals_identity, approvals)
            return _HeldState(root, proposals, consumed, approvals)
        except Exception:
            if approvals != -1:
                _close_fd(approvals)
            if consumed != -1:
                _close_fd(consumed)
            if proposals != -1:
                _close_fd(proposals)
            _close_fd(root)
            raise

    def _proposal_path(self, proposal_id: str) -> Path:
        return self._root / "proposals" / f"{proposal_id}.json"

    def _consumed_path(self, proposal_id: str) -> Path:
        return self._root / "consumed" / f"{proposal_id}.json"

    def _approval_path(self, proposal_id: str) -> Path:
        return self._approvals / f"{proposal_id}.json"

    def _load_proposal(self, state: _HeldState, proposal_id: str) -> dict[str, Any] | None:
        key = parse_safe_path_segment(proposal_id)
        try:
            raw = _read_json_from_parent(
                state.proposals, f"{key}.json", required_mode=True, on_error=_action_state_invalid
            )
        except FileNotFoundError:
            return None
        except N8nError:
            raise
        return _parse_proposal_record(raw, key)

    def _load_approval(self, state: _HeldState, proposal_id: str) -> dict[str, Any] | None:
        key = parse_safe_path_segment(proposal_id)
        try:
            raw = _read_json_from_parent(state.approvals, f"{key}.json", required_mode=True, on_error=_denied)
        except FileNotFoundError:
            return None
        except N8nError as exc:
            if exc.code == "denied":
                raise
            return None
        try:
            return _parse_approval_record(raw, key)
        except N8nError:
            _denied()

    def _load_consumed(self, state: _HeldState, proposal_id: str) -> dict[str, Any] | None:
        key = parse_safe_path_segment(proposal_id)
        try:
            raw = _read_json_from_parent(
                state.consumed, f"{key}.json", required_mode=True, on_error=_action_state_invalid
            )
        except FileNotFoundError:
            return None
        return _parse_consumed_record(raw, key)

    def _list_proposals(self, state: _HeldState) -> list[dict[str, Any]]:
        records = []
        for name in _enumerate_names(state.proposals, cap=MAX_PROPOSALS):
            loaded = self._load_proposal(state, name[:-5])
            if loaded is not None:
                records.append(loaded)
        return records

    def _prune_expired_proposals(self, state: _HeldState) -> None:
        now = self._now()
        for record in self._list_proposals(state):
            if _parse_iso(record["expires_at"]) > now:
                continue
            try:
                _unlink_child(state.proposals, f"{record['proposal_id']}.json")
            except OSError:
                _action_state_invalid()
        remaining = self._list_proposals(state)
        if len(remaining) >= MAX_PROPOSALS:
            _action_state_invalid()
        consumed = _enumerate_names(state.consumed, cap=MAX_CONSUMED)
        if len(consumed) > MAX_CONSUMED:
            _action_state_invalid()
