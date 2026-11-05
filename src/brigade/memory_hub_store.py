"""Per-host hub memory state under `.brigade/memory/` (S14).

- `hub-replica/`: the heads this credential can read, the `seq` watermark and
  the `log_epoch` they were pulled under. A rebuild after an epoch change goes
  into `hub-replica.building/` and is swapped in when the pull finishes; the
  old replica is removed first, so nothing ever answers from it again.
- `hub-spool/`: writes the hub has not acknowledged, one file per entry named
  by its time-ordered `client_event_id`, so name order is send order.
- `hub-proposals/`: spooled writes the hub can no longer take as they are
  (a base mismatch, an epoch change, an expired id, a refusal). They wait for
  review and are never resent on their own.
- `hub-published.json`: the revision and epoch each published card was last
  written at, which the next `push` names as its base.

Directories are 0700 and files 0600, written through a temporary file and an
atomic replace. Symlinks are refused. All of it is ignored by Git.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
from pathlib import Path
from typing import Any, Iterator

ROOT_REL = ".brigade/memory"
REPLICA = "hub-replica"
BUILDING = "hub-replica.building"
SPOOL = "hub-spool"
PROPOSALS = "hub-proposals"
PUBLISHED = "hub-published.json"
SPOOL_CAP = 10_000
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class StoreError(OSError):
    """Hub memory state is unsafe or unreadable."""


def root(target: Path) -> Path:
    return target / ROOT_REL


def private_dir(path: Path) -> Path:
    """Create `path` (and missing parents) 0700; refuse a symlink anywhere at the leaf."""
    if path.is_symlink():
        raise StoreError(f"{path} is a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise StoreError(f"{path} is not a directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise StoreError(f"{path} has another owner")
    os.chmod(path, 0o700)
    return path


def _ignore_dir(target: Path) -> None:
    """Keep hub state out of Git even when Brigade's .gitignore block was skipped."""
    marker = root(target) / ".gitignore"
    lines = [f"/{REPLICA}/", f"/{BUILDING}/", f"/{SPOOL}/", f"/{PROPOSALS}/", f"/{PUBLISHED}"]
    try:
        existing = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []
    except OSError:
        return
    missing = [line for line in lines if line not in existing]
    if missing:
        with open(marker, "a", encoding="utf-8") as handle:
            handle.write("".join(f"{line}\n" for line in missing))


def ensure_root(target: Path) -> Path:
    base = private_dir(root(target))
    _ignore_dir(target)
    return base


def write_private(path: Path, data: str) -> None:
    """Write `data` as UTF-8 to a 0600 file through a temporary name and an atomic replace."""
    private_dir(path.parent)
    if path.is_symlink():
        raise StoreError(f"{path} is a symlink")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read_private(path: Path) -> str | None:
    if path.is_symlink():
        raise StoreError(f"{path} is a symlink")
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _CLOEXEC)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise StoreError(f"{path} is not a regular file")
        return handle.read()


def read_json(path: Path) -> Any:
    raw = read_private(path)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise StoreError(f"{path} is not valid JSON") from exc


def write_json(path: Path, value: Any) -> None:
    write_private(path, json.dumps(value, sort_keys=True, ensure_ascii=False) + "\n")


# --- replica ------------------------------------------------------------------


class Replica:
    """Heads keyed by `(scope, card_id)` plus the watermark, in one directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        meta = read_json(directory / "meta.json") if directory.is_dir() else None
        self.meta: dict[str, Any] = meta if isinstance(meta, dict) else {}
        heads = read_json(directory / "heads.json") if self.meta else None
        self.heads: dict[str, dict[str, Any]] = heads if isinstance(heads, dict) else {}

    @staticmethod
    def key(scope: str, card_id: str) -> str:
        return f"{scope}\n{card_id}"

    @property
    def exists(self) -> bool:
        return bool(self.meta)

    def watermark(self, hub: str) -> int | None:
        if self.meta.get("hub") != hub or not isinstance(self.meta.get("seq"), int):
            return None
        return int(self.meta["seq"])

    def bytes(self) -> int:
        return sum(len(str(head.get("text", "")).encode("utf-8")) for head in self.heads.values())

    def save(self) -> None:
        private_dir(self.directory)
        write_json(self.directory / "heads.json", self.heads)
        self.meta["cards"] = len(self.heads)
        self.meta["bytes"] = self.bytes()
        write_json(self.directory / "meta.json", self.meta)


def replica(target: Path) -> Replica:
    """The replica that may answer reads. Absent while a rebuild is in progress."""
    return Replica(root(target) / REPLICA)


def has_watermark(target: Path, hub: str) -> bool:
    """Whether this workspace has pulled from `hub`: then a 404 is not "no memory"."""
    for name in (REPLICA, BUILDING):
        if Replica(root(target) / name).watermark(hub) is not None:
            return True
    return False


def discard_replica(target: Path) -> None:
    """Remove the live replica so no read uses heads from an old epoch."""
    live = root(target) / REPLICA
    if live.exists():
        doomed = root(target) / f".{REPLICA}.discard.{secrets.token_hex(4)}"
        os.replace(live, doomed)
        shutil.rmtree(doomed, ignore_errors=True)


def start_building(target: Path, *, hub: str, log_epoch: str) -> Replica:
    ensure_root(target)
    building = root(target) / BUILDING
    current = Replica(building)
    if current.meta.get("hub") == hub and current.meta.get("log_epoch") == log_epoch:
        return current  # resume an interrupted rebuild under the same epoch
    shutil.rmtree(building, ignore_errors=True)
    fresh = Replica(building)
    fresh.meta = {"hub": hub, "log_epoch": log_epoch, "seq": 0}
    fresh.save()
    return fresh


def finish_building(target: Path) -> Replica:
    discard_replica(target)
    os.replace(root(target) / BUILDING, root(target) / REPLICA)
    return replica(target)


def building(target: Path) -> Replica:
    return Replica(root(target) / BUILDING)


# --- spool ----------------------------------------------------------------------


def _spool_dir(target: Path) -> Path:
    return root(target) / SPOOL


def spool_entries(target: Path) -> list[dict[str, Any]]:
    directory = _spool_dir(target)
    if not directory.is_dir():
        return []
    entries = []
    for path in sorted(directory.glob("*.json")):
        value = read_json(path)
        if isinstance(value, dict):
            entries.append(value)
    return entries


def spool_count(target: Path) -> int:
    directory = _spool_dir(target)
    return len(list(directory.glob("*.json"))) if directory.is_dir() else 0


def spool_append(target: Path, entry: dict[str, Any]) -> bool:
    """Spool one write. False when the spool is at its cap and the write stays local."""
    ensure_root(target)
    if spool_count(target) >= SPOOL_CAP:
        return False
    write_json(_spool_dir(target) / f"{entry['client_event_id']}.json", entry)
    return True


def spool_remove(target: Path, client_event_id: str) -> None:
    (_spool_dir(target) / f"{client_event_id}.json").unlink(missing_ok=True)


# --- proposals --------------------------------------------------------------------


def propose(target: Path, entry: dict[str, Any], *, reason: str, hub_reply: dict[str, Any] | None = None) -> str:
    """Keep a write the hub cannot take as-is for review. Returns the proposal id."""
    ensure_root(target)
    proposal_id = f"hub-{entry['client_event_id'].lower()}"
    record = {
        "proposal_id": proposal_id,
        "kind": "brigade.memory-hub-conflict.v1",
        "reason": reason,
        "entry": entry,
        "hub": {key: value for key, value in (hub_reply or {}).items() if key != "error"},
    }
    write_json(root(target) / PROPOSALS / f"{proposal_id}.json", record)
    return proposal_id


def proposals(target: Path) -> Iterator[dict[str, Any]]:
    directory = root(target) / PROPOSALS
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json")):
        value = read_json(path)
        if isinstance(value, dict):
            yield value


def pending_cards(target: Path) -> set[tuple[str, str]]:
    """Cards with a spooled write or an open conflict: push leaves them alone."""
    keys = {(str(e.get("scope")), str(e.get("card_id"))) for e in spool_entries(target)}
    for record in proposals(target):
        entry = record.get("entry") or {}
        keys.add((str(entry.get("scope")), str(entry.get("card_id"))))
    return keys


# --- published state ----------------------------------------------------------------


def published(target: Path) -> dict[str, Any]:
    value = read_json(root(target) / PUBLISHED)
    return value if isinstance(value, dict) else {}


def save_published(target: Path, value: dict[str, Any]) -> None:
    ensure_root(target)
    write_json(root(target) / PUBLISHED, value)
