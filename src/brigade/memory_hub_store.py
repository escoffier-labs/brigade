"""Per-host hub memory state under `.brigade/memory/hub/` (S14).

- `replica/`: the heads this host may answer from when the hub is away, the
  `seq` watermark, the `log_epoch` and the principal they were pulled under,
  and the read scopes they cover. A rebuild goes into `replica.building/` and
  is swapped in when the pull finishes; the old replica is removed first, so
  nothing ever answers from it again.
- `spool/<hub>/`: writes the hub has not acknowledged, one file per entry
  named by its time-ordered `client_event_id`, so name order is send order.
  Each hub has its own directory, so entries for one hub never block or count
  against another.
- `proposals/`: spooled writes the hub can no longer take as they are (a base
  mismatch, an epoch change, an expired id, a refusal). They wait for review
  and are never resent on their own. A dropped or resolved proposal moves to
  `proposals/dropped/`, so its text is never lost.
- `published.json`: the revision and epoch each published card was last
  written at, which the next `push` names as its base.
- `lock`: held by `push` and proposal changes for their whole run, and by
  `pull` only while it changes the replica, so two writers never interleave.
- `pull.lock`: held by `pull` for its whole run, network calls included, so
  two pulls never interleave and a long pull never blocks a push.

`hub/` is this module's own directory: 0700, owned by this user, with files
0600 written through a temporary file and an atomic replace. Symlinks are
refused. The shared `.brigade/memory/` around it keeps its own mode. A `*`
ignore marker inside `hub/` keeps all of it, temporary and discarded copies
included, out of Git.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import shutil
import stat
from pathlib import Path
from typing import Any, Iterator

from . import memory_hub_replica_limits as limits

PARENT_REL = ".brigade/memory"
ROOT_REL = ".brigade/memory/hub"
REPLICA = "replica"
BUILDING = "replica.building"
SPOOL = "spool"
PROPOSALS = "proposals"
PUBLISHED = "published.json"
LOCK = "lock"
PULL_LOCK = "pull.lock"
DROPPED = "dropped"
IGNORE_MARKER = "*\n"
SPOOL_CAP = 10_000
LOCK_DEADLINE_SECONDS = 30.0
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class StoreError(OSError):
    """Hub memory state is unsafe or unreadable."""


def root(target: Path) -> Path:
    return target / ROOT_REL


def private_dir(path: Path) -> Path:
    """Create `path` and its missing parents 0700; refuse a symlink or another owner.

    Callers create `hub/` with `ensure_root` first, so this never creates or
    changes the shared `.brigade/memory/` around it.
    """
    if path.is_symlink():
        raise StoreError(f"{path} is a symlink")
    if not path.parent.exists():
        private_dir(path.parent)
    path.mkdir(mode=0o700, exist_ok=True)
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise StoreError(f"{path} is not a directory")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise StoreError(f"{path} has another owner")
    if stat.S_IMODE(info.st_mode) != 0o700:
        os.chmod(path, 0o700)
    return path


def _ignore_dir(target: Path) -> None:
    """Keep hub state out of Git even when Brigade's .gitignore block was skipped."""
    marker = root(target) / ".gitignore"
    if marker.is_symlink():
        raise StoreError(f"{marker} is a symlink")
    if read_private(marker) != IGNORE_MARKER:
        write_private(marker, IGNORE_MARKER)


def ensure_root(target: Path) -> Path:
    parent = target / PARENT_REL
    if parent.is_symlink():
        raise StoreError(f"{parent} is a symlink")
    parent.mkdir(parents=True, exist_ok=True)
    base = private_dir(root(target))
    _ignore_dir(target)
    return base


@contextlib.contextmanager
def _held(target: Path, name: str, deadline_seconds: float | None) -> Iterator[None]:
    from .inbox_lock import held_file_lock

    ensure_root(target)
    deadline = LOCK_DEADLINE_SECONDS if deadline_seconds is None else deadline_seconds
    with held_file_lock(root(target) / name, deadline_seconds=deadline):
        yield


@contextlib.contextmanager
def locked(target: Path, *, deadline_seconds: float | None = None) -> Iterator[None]:
    """Hold this workspace's hub state lock: one change to the replica, spool, proposals or published state at a time.

    Raises `inbox_lock.InboxLockTimeout` when another run holds it past the deadline.
    """
    with _held(target, LOCK, deadline_seconds):
        yield


@contextlib.contextmanager
def pulling(target: Path, *, deadline_seconds: float | None = None) -> Iterator[None]:
    """Hold this workspace's pull lock: one pull at a time. Changes still take `locked`."""
    with _held(target, PULL_LOCK, deadline_seconds):
        yield


def write_private(path: Path, data: str) -> None:
    """Write `data` as UTF-8 to a 0600 file through a temporary name and an atomic replace.

    Newlines are written untranslated, so the file holds exactly `data`'s bytes on
    every platform and a digest of the serialized text matches the file.
    """
    private_dir(path.parent)
    if path.is_symlink():
        raise StoreError(f"{path} is a symlink")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def read_private(path: Path, *, max_bytes: int | None = None) -> str | None:
    if path.is_symlink():
        raise StoreError(f"{path} is a symlink")
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _CLOEXEC | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise StoreError(f"{path} is not a regular file")
        if max_bytes is not None and os.fstat(handle.fileno()).st_size > max_bytes:
            raise limits.ReplicaLimitError("metadata")
        raw = handle.read() if max_bytes is None else handle.read(max_bytes + 1)
        if max_bytes is not None and len(raw.encode("utf-8")) > max_bytes:
            raise limits.ReplicaLimitError("metadata")
        return raw


def read_json(path: Path, *, max_bytes: int | None = None) -> Any:
    raw = read_private(path, max_bytes=max_bytes)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except ValueError as exc:
        raise StoreError(f"{path} is not valid JSON") from exc


def _dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def write_json(path: Path, value: Any) -> None:
    write_private(path, _dumps(value) + "\n")


# --- replica ------------------------------------------------------------------


def head_size(key: str, head: dict[str, Any]) -> int:
    """Bytes `head` adds to `heads.json`, which is what `replica_max_mb` caps."""
    return len(_dumps({key: head}).encode("utf-8"))


class Replica:
    """Heads keyed by `(scope, card_id)` plus the watermark, in one directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        meta = read_json(directory / "meta.json", max_bytes=limits.MAX_META_BYTES) if directory.is_dir() else None
        self.meta: dict[str, Any] = meta if isinstance(meta, dict) else {}
        heads = (
            limits.read_heads(directory / "heads.json", self.meta)
            if self.meta and limits.active()
            else read_json(directory / "heads.json")
            if self.meta
            else None
        )
        self.heads: dict[str, dict[str, Any]] = heads if isinstance(heads, dict) else {}

    @staticmethod
    def key(scope: str, card_id: str) -> str:
        return f"{scope}\n{card_id}"

    @property
    def exists(self) -> bool:
        return bool(self.meta)

    @property
    def missing(self) -> dict[str, dict[str, Any]]:
        """Cards left out for the cap: `{key: {scope, card_id, revision, size}}`, fetched once space frees."""
        value = self.meta.get("missing")
        if not isinstance(value, dict):
            value = self.meta["missing"] = {}
        return value

    def watermark(self, hub: str) -> int | None:
        if self.meta.get("hub") != hub or not isinstance(self.meta.get("seq"), int):
            return None
        return int(self.meta["seq"])

    def belongs_to(self, hub: str, principal: object) -> bool:
        """Pulled from `hub` as `principal`, the identity the hub reported for this credential."""
        return (
            self.watermark(hub) is not None
            and isinstance(principal, str)
            and bool(principal)
            and self.meta.get("principal") == principal
        )

    def readable_heads(self, read_scopes: tuple[str, ...] | list[str]) -> dict[str, dict[str, Any]]:
        """Heads in scopes both configured now and readable when the replica was pulled."""
        allowed = set(read_scopes) & set(self.meta.get("read_scopes") or [])
        return {key: head for key, head in self.heads.items() if head.get("scope") in allowed}

    def retain_scopes(self, scopes: set[str]) -> int:
        """Drop heads and cap entries outside `scopes`. Returns how many heads went."""
        doomed = [key for key, head in self.heads.items() if head.get("scope") not in scopes]
        for key in doomed:
            del self.heads[key]
        for key in [k for k, item in self.missing.items() if item.get("scope") not in scopes]:
            del self.missing[key]
        return len(doomed)

    def bytes(self) -> int:
        return sum(head_size(key, head) for key, head in self.heads.items())

    def save(self) -> None:
        limits.check_count(len(self.heads))
        snapshot = _dumps(self.heads) + "\n"
        self.meta["cards"] = len(self.heads)
        self.meta["heads_sha256"] = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
        self.meta["bytes"] = self.bytes()
        self.meta["full"] = bool(self.missing)
        metadata = _dumps(self.meta) + "\n"
        if len(metadata.encode("utf-8")) > limits.MAX_META_BYTES:
            raise limits.ReplicaLimitError("metadata")
        private_dir(self.directory)
        write_json(self.directory / "heads.json", self.heads)
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


def _discard(target: Path, name: str) -> None:
    directory = root(target) / name
    if directory.exists():
        doomed = root(target) / f".{name}.discard.{secrets.token_hex(4)}"
        os.replace(directory, doomed)
        shutil.rmtree(doomed, ignore_errors=True)


def discard_replica(target: Path) -> None:
    """Remove the live replica so no read uses heads from an old epoch or another principal."""
    _discard(target, REPLICA)


def discard_building(target: Path) -> None:
    _discard(target, BUILDING)


def start_building(target: Path, *, hub: str, log_epoch: str, principal: str) -> Replica:
    ensure_root(target)
    current = building(target)
    if (
        current.meta.get("hub") == hub
        and current.meta.get("log_epoch") == log_epoch
        and current.meta.get("principal") == principal
    ):
        return current  # resume an interrupted rebuild under the same epoch and principal
    discard_building(target)
    fresh = building(target)
    fresh.meta = {"hub": hub, "log_epoch": log_epoch, "principal": principal, "seq": 0, "read_scopes": []}
    fresh.save()
    return fresh


def finish_building(target: Path) -> Replica:
    discard_replica(target)
    os.replace(root(target) / BUILDING, root(target) / REPLICA)
    return replica(target)


def building(target: Path) -> Replica:
    return Replica(root(target) / BUILDING)


# --- spool ----------------------------------------------------------------------


def _hub_dir_name(hub: str) -> str:
    # A file name for the hub identity, which is a URL (no credential in it).
    return hashlib.sha256(hub.encode("utf-8")).hexdigest()[:16]


def _spool_dir(target: Path, hub: str) -> Path:
    return root(target) / SPOOL / _hub_dir_name(hub)


def _spool_dirs(target: Path, hub: str | None) -> list[Path]:
    if hub is not None:
        directory = _spool_dir(target, hub)
        return [directory] if directory.is_dir() else []
    base = root(target) / SPOOL
    return sorted(path for path in base.iterdir() if path.is_dir()) if base.is_dir() else []


def spool_entries(target: Path, hub: str) -> list[dict[str, Any]]:
    """`hub`'s spooled writes, oldest first."""
    entries = []
    for directory in _spool_dirs(target, hub):
        for path in sorted(directory.glob("*.json")):
            value = read_json(path)
            if isinstance(value, dict) and value.get("hub") == hub:
                entries.append(value)
    return entries


def spool_count(target: Path, hub: str | None = None) -> int:
    """Spooled writes for `hub`, or for every hub when None."""
    return sum(len(list(directory.glob("*.json"))) for directory in _spool_dirs(target, hub))


def spool_append(target: Path, entry: dict[str, Any]) -> bool:
    """Spool one write. False when its hub's spool is at the cap and the write stays local. Hold `locked`."""
    ensure_root(target)
    if spool_count(target, entry["hub"]) >= SPOOL_CAP:
        return False
    write_json(_spool_dir(target, entry["hub"]) / f"{entry['client_event_id']}.json", entry)
    return True


def spool_remove(target: Path, hub: str, client_event_id: str) -> None:
    (_spool_dir(target, hub) / f"{client_event_id}.json").unlink(missing_ok=True)


# --- proposals --------------------------------------------------------------------


def propose(target: Path, entry: dict[str, Any], *, reason: str, hub_reply: dict[str, Any] | None = None) -> str:
    """Keep a write the hub cannot take as-is for review. Returns the proposal id."""
    ensure_root(target)
    proposal_id = f"hub-{entry['client_event_id'].lower()}"
    record: dict[str, Any] = {
        "proposal_id": proposal_id,
        "kind": "brigade.memory-hub-conflict.v1",
        "reason": reason,
        "entry": entry,
        "hub": {key: value for key, value in (hub_reply or {}).items() if key != "error"},
    }
    if entry.get("derived_id") and entry.get("base_revision") is None:
        record["derived_create"] = True
    write_json(root(target) / PROPOSALS / f"{proposal_id}.json", record)
    return proposal_id


def proposals(target: Path, hub: str | None = None) -> Iterator[dict[str, Any]]:
    """Open proposals, for `hub` only when given."""
    directory = root(target) / PROPOSALS
    if not directory.is_dir():
        return
    for path in sorted(directory.glob("*.json")):
        value = read_json(path)
        if isinstance(value, dict) and (hub is None or (value.get("entry") or {}).get("hub") == hub):
            yield value


def _proposal_path(target: Path, proposal_id: str) -> Path | None:
    if not (proposal_id.startswith("hub-") and proposal_id[4:].isalnum()):
        return None
    return root(target) / PROPOSALS / f"{proposal_id}.json"


def read_proposal(target: Path, proposal_id: str) -> dict[str, Any] | None:
    """One open proposal; None when there is no such proposal."""
    path = _proposal_path(target, proposal_id)
    record = read_json(path) if path is not None else None
    return record if isinstance(record, dict) else None


def dropped_dir(target: Path) -> Path:
    return root(target) / PROPOSALS / DROPPED


def drop_proposal(target: Path, proposal_id: str, *, outcome: str, **extra: Any) -> dict[str, Any] | None:
    """Close one proposal and return it, with `dropped_to` naming where it went. Hold `locked`.

    The record, with `outcome` and `extra` added, moves to `proposals/dropped/`
    (0600) before the open copy goes, so its text is never lost. None when there
    is no such proposal.
    """
    path = _proposal_path(target, proposal_id)
    record = read_proposal(target, proposal_id)
    if path is None or record is None:
        return None
    kept = dropped_dir(target) / path.name
    write_json(kept, {**record, **extra, "outcome": outcome})
    path.unlink(missing_ok=True)
    return {**record, "dropped_to": str(kept)}


def proposal_cards(target: Path, hub: str) -> dict[tuple[str, str], list[str]]:
    """`(scope, card_id)` to the ids of `hub`'s open proposals on it."""
    cards: dict[tuple[str, str], list[str]] = {}
    for record in proposals(target, hub):
        entry = record.get("entry") or {}
        cards.setdefault((str(entry.get("scope")), str(entry.get("card_id"))), []).append(str(record["proposal_id"]))
    return cards


def pending_cards(target: Path, hub: str) -> set[tuple[str, str]]:
    """Cards with a spooled write or an open conflict on `hub`: push leaves them alone."""
    keys = {(str(e.get("scope")), str(e.get("card_id"))) for e in spool_entries(target, hub)}
    return keys | set(proposal_cards(target, hub))


# --- published state ----------------------------------------------------------------


def published(target: Path) -> dict[str, Any]:
    value = read_json(root(target) / PUBLISHED)
    return value if isinstance(value, dict) else {}


def save_published(target: Path, value: dict[str, Any]) -> None:
    ensure_root(target)
    write_json(root(target) / PUBLISHED, value)
