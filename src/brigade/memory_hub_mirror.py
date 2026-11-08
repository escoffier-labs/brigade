"""Disposable hub-to-folder Markdown export. Never publishes local edits.

A future edit-to-proposal workflow belongs beside memory_hub_sync's proposals,
with explicit review, rather than in this one-way exporter.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from collections import Counter
from pathlib import Path
from typing import Any

from . import memory_hub_client as hubc
from . import memory_hub_store as store
from . import memory_hub_sync as sync
from .inbox_lock import held_file_lock
from .memory_vault import parse_frontmatter

MARKER = ".brigade-memory-mirror.json"
# Held for a whole run, so two runs on one folder never interleave. A dotfile the
# mirror never owns: it is not a safe note name, so it is never deleted or foreign.
LOCK = ".brigade-memory-mirror.lock"
LOCK_DEADLINE_SECONDS = 30.0
STALE_TEMP_SECONDS = 10 * 60
# Temp files carry a prefix that belongs to this tool, so a user's own `.mirror-*` file is never touched.
TEMP_PREFIX = ".brigade-mirror-"
_TEMP = re.compile(r"\.brigade-mirror-[a-z0-9_]{8}\.tmp")
INDEX = "Hub Memory Index.md"
KIND = "brigade.memory-mirror.v1"
# Windows has no O_NOFOLLOW; _read then checks is_symlink before it opens.
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_OPEN_FLAGS = os.O_RDONLY | _NOFOLLOW | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
PREDATES = (
    "this host's hub replica or the hub predates scan metadata (a head has no scan_state), mirror unchanged. "
    "Upgrade the hub, then run memory hub pull"
)


def _filename(title: str, key: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:64] or "card"
    # A suffix also makes CON, NUL, COM1 and other Windows device names safe.
    return f"{slug}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:16]}.md"


def _safe_name(name: object) -> bool:
    return (
        isinstance(name, str) and re.fullmatch(r"[a-z0-9-]+-[0-9a-f]{16}\.md", name) is not None and len(name) < 100
    ) or name == INDEX


def _read(path: Path) -> bytes | None:
    """The bytes of a regular file, or None when absent. Never follows a symlink."""
    refusal = hubc.HubConfigError(f"mirror refuses symlink: {path}")
    if not _NOFOLLOW and path.is_symlink():
        raise refusal
    try:
        # Opening a directory raises PermissionError on Windows, so look first.
        if stat.S_ISDIR(os.lstat(path).st_mode):
            raise hubc.HubConfigError(f"mirror refuses {path}: not a regular file")
    except FileNotFoundError:
        return None
    except NotADirectoryError as exc:
        raise hubc.HubConfigError(f"mirror destination is not a directory: {path.parent}") from exc
    except OSError:
        pass  # The open below reports it.
    try:
        fd = os.open(path, _OPEN_FLAGS)
    except FileNotFoundError:
        return None
    except NotADirectoryError as exc:
        raise hubc.HubConfigError(f"mirror destination is not a directory: {path.parent}") from exc
    except OSError as exc:
        if exc.errno in (errno.ELOOP, getattr(errno, "EMLINK", None)) or path.is_symlink():
            raise refusal from exc
        raise hubc.HubConfigError(f"mirror cannot read {path}: {exc.strerror or exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise hubc.HubConfigError(f"mirror refuses {path}: not a regular file")
    except BaseException:
        os.close(fd)
        raise
    with os.fdopen(fd, "rb") as stream:
        return stream.read()


# Some FUSE, SMB, 9p and vboxsf mounts refuse fsync on a directory. Such a folder gets no sync, as on Windows.
_NO_DIRECTORY_SYNC = {errno.EINVAL, errno.EBADF, errno.ENOTSUP, errno.EOPNOTSUPP}


def _sync_directory(directory: Path) -> None:
    """Make a rename in `directory` durable. POSIX only: Windows cannot open a directory to fsync it."""
    if os.name == "nt":
        return
    try:
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError as exc:
        if exc.errno in _NO_DIRECTORY_SYNC:
            return
        raise
    try:
        os.fsync(fd)
    except OSError as exc:
        if exc.errno not in _NO_DIRECTORY_SYNC:
            raise
    finally:
        os.close(fd)


def _atomic(path: Path, text: str) -> None:
    """Write `path` through a temp file and a rename. The caller syncs the folder when the rename must outlive a crash."""
    fd, name = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _readable_heads(
    target: Path, client: hubc.MemoryHubClient, settings: hubc.HubSettings
) -> tuple[list[dict[str, Any]], str]:
    """The replica's readable heads and the principal it was pulled as. Takes `store.locked` and nothing else."""
    with store.locked(target):
        replica = store.replica(target)
        if replica.snapshot_missing:
            raise hubc.HubConfigError("replica heads are missing or inconsistent, mirror unchanged")
        if replica.watermark(client.identity) is None or not replica.meta.get("principal"):
            raise hubc.HubUnavailable("hub and usable replica unavailable, mirror unchanged")
        if not store.heads_path(replica.directory, replica.meta).is_file() or replica.meta.get("cards") != len(
            replica.heads
        ):
            raise hubc.HubConfigError("replica heads are missing or inconsistent, mirror unchanged")
        if replica.missing:
            raise hubc.HubConfigError("replica is incomplete. Increase [hub] replica_max_mb and pull, mirror unchanged")
        return list(replica.readable_heads(settings.read_scopes).values()), str(replica.meta["principal"])


def _heads(target: Path, client: hubc.MemoryHubClient, settings: hubc.HubSettings) -> tuple[list[dict], str, list[str]]:
    warnings: list[str] = []
    offline = False
    authenticated: str | None = None
    try:
        report = sync.pull(target, client, settings)
        authenticated = report.get("principal")
        warnings.extend(report["warnings"])
        if report.get("seq") is None:
            raise hubc.HubConfigError("replica rebuild incomplete, mirror unchanged. Run memory hub pull again")
    except hubc.HubUnavailable as exc:
        offline = True
        warnings.append(f"hub unavailable ({exc.reason}), using this host's replica")
    heads, principal = _readable_heads(target, client, settings)
    _require_principal(authenticated, principal)
    if any("scan_state" not in head for head in heads):
        # A head without scan_state cannot be judged clean, and skipping it would
        # delete its note. Refetch only those heads in place, never discard the
        # replica, and refuse when the hub cannot say.
        if offline:
            raise hubc.HubConfigError(PREDATES)
        try:
            sync.refresh_scan_metadata(target, client, settings)
        except hubc.HubUnavailable as exc:
            raise hubc.HubConfigError(f"{PREDATES} (hub unavailable: {exc.reason})") from exc
        heads, principal = _readable_heads(target, client, settings)
        _require_principal(authenticated, principal)
        _require_scan_state(heads)
    # Offline there is no authentication to keep: the replica's own principal is the one to hold to.
    return heads, authenticated or principal, warnings


def _require_principal(expected: str | None, actual: str) -> None:
    """Refuse a replica another principal pulled into since `expected` authenticated, so its cards are not exported."""
    if expected is not None and actual != expected:
        raise hubc.HubConfigError(
            "the replica was pulled as another principal since this run authenticated, mirror unchanged. Run again"
        )


def _require_scan_state(heads: list[dict]) -> None:
    if any("scan_state" not in head for head in heads):
        raise hubc.HubConfigError(PREDATES)


def _split_raw(text: str) -> tuple[dict[str, Any], str | None, str]:
    """A card's leading front matter, that block's text as written, and the body after it. A leading BOM is ignored.

    The text is None when no block was stripped: the body is then the whole card.
    """
    text = text.removeprefix("\ufeff")
    meta = parse_frontmatter(text)
    # A block that parses to nothing stays in the body, so none of it is lost.
    if meta and text.startswith(("---\n", "---\r\n")):
        lines = text.splitlines(keepends=True)
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return meta, "".join(lines[1:index]), "".join(lines[index + 1 :]).lstrip("\r\n")
    return meta, None, text


def _split(text: str) -> tuple[dict[str, Any], str]:
    meta, _, body = _split_raw(text)
    return meta, body


def _select(heads: list[dict], exclude_scopes: tuple[str, ...] = ()) -> tuple[list[tuple[dict, dict]], dict[str, int]]:
    skipped: Counter[str] = Counter()
    rows = [(head, _split(str(head.get("text", "")))[0]) for head in heads]
    # Supersession is scoped. Chains name predecessors with supersedes, or a
    # successor with superseded_by. Never revive a predecessor if its head is withheld.
    # Keep all rows here: exclusions must not change scoped supersession semantics.
    superseded: set[tuple[str, str]] = set()
    for head, meta in rows:
        previous = meta.get("supersedes", [])
        if isinstance(previous, str):
            previous = [previous]
        if isinstance(previous, list):
            superseded.update((head["scope"], str(card)) for card in previous)
    selected = []
    for head, meta in rows:
        reason = None
        if head["scope"] in exclude_scopes:
            reason = "excluded-scope"
        elif head.get("state") == "retired":
            reason = "retired"
        elif head.get("trust") == "flagged" or head.get("scan_state") == "flagged":
            reason = "flagged"
        elif head.get("scan_state") == "pending":
            reason = "scan-pending"
        elif meta.get("visibility") == "private":
            reason = "visibility-private"
        elif withheld := sync.withheld(str(head.get("text", ""))):
            reason = withheld
        elif head.get("scan_state") not in ("clean", "private"):
            # Private scans name the operator's own machines and contacts. The
            # mirror writes to a local folder, so they are exported (S18).
            reason = "scan-unknown"
        elif (head["scope"], head["card_id"]) in superseded or meta.get("superseded_by"):
            reason = "superseded"
        if reason:
            skipped[reason] += 1
        else:
            selected.append((head, meta))
    return selected, dict(sorted(skipped.items()))


def _aliases(head: dict, meta: dict) -> list[str]:
    """The note's aliases: the title, the id, then the card's own string aliases, each once.

    Obsidian offers only a top-level `aliases` in link suggestions, so the
    card's own are merged here as well as kept under `card`.
    """
    own = meta.get("aliases", [])
    own = [own] if isinstance(own, str) else own if isinstance(own, list) else []
    seen: dict[str, None] = {}
    for alias in (str(meta.get("title") or head["card_id"]), head["card_id"], *own):
        if isinstance(alias, str) and alias.strip():
            seen.setdefault(alias.strip(), None)
    return list(seen)


def _note(head: dict, meta: dict) -> str:
    # The card's own front matter moves under `card` so the note has one block.
    own = {key: value for key, value in meta.items() if key != "tags"}
    # The parser is lossy (a multiline `description` or a nested map keeps nothing), so the
    # stripped block is also kept verbatim. Detecting what the parser dropped would be the same
    # lossy guess, so it is always kept when a block was removed.
    _, raw_block, body = _split_raw(str(head["text"]))
    fields = {
        "card_id": head["card_id"],
        "scope": head["scope"],
        "revision": head["revision"],
        "updated_at": head.get("updated_at"),
        "source": head.get("writer"),
        "tags": meta.get("tags", []),
        "scan_state": head["scan_state"],
        "aliases": _aliases(head, meta),
        **({"card": own} if own else {}),
        **({"card_frontmatter": raw_block} if raw_block is not None else {}),
    }
    # JSON values are YAML-compatible and quote control characters safely.
    front = "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)}" for key, value in fields.items()
    )
    return f"---\n{front}\n---\n\n{body}"


def _check_destination(out: Path) -> None:
    """Refuse `out` when it is a symlink, or when a directory this tool would create is a dangling one.

    An existing symlinked ancestor (a symlinked home, /tmp on macOS) is
    the caller's own layout and is fine.
    """
    for path in (out, *out.parents):
        if path == out and path.is_symlink():
            raise hubc.HubConfigError(f"mirror destination must not be a symlink: {out}")
        if path.exists():
            break
        if path.is_symlink():
            raise hubc.HubConfigError(f"mirror destination must not pass through a symlink: {path}")
    if out.exists() and not out.is_dir():
        raise hubc.HubConfigError(f"mirror destination is not a directory: {out}")


def _marker_value(files: dict[str, Any], notes: dict[str, str], identity: dict[str, str]) -> bytes:
    body = {"kind": KIND, **identity, "files": files, "notes": notes}
    return (json.dumps(body, sort_keys=True) + "\n").encode("utf-8")


def _is_own_temp(path: Path) -> bool:
    """A regular file named like this tool's temp files. Nothing else in a destination is ever its own."""
    if not _TEMP.fullmatch(path.name):
        return False
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _foreign_entries(out: Path) -> bool:
    """Whether `out` holds anything but this tool's lock and temp files. A run killed before its marker leaves only those."""
    return out.is_dir() and any(path.name != LOCK and not _is_own_temp(path) for path in out.iterdir())


def _drop_stale_temps(out: Path) -> None:
    """Remove temp files a killed run left behind. Call only for a folder with a valid marker.

    A live run holds the lock, so none is in use. Only this tool's own
    `.brigade-mirror-<random>.tmp` names go. Any other name, such as a user's
    `.mirror-backup.tmp`, stays.
    """
    cutoff = time.time() - STALE_TEMP_SECONDS
    for path in out.iterdir():
        if not _TEMP.fullmatch(path.name):
            continue
        try:
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) and info.st_mtime < cutoff:
                path.unlink(missing_ok=True)
        except OSError:
            continue


def mirror(
    target: Path,
    client: hubc.MemoryHubClient,
    settings: hubc.HubSettings,
    out: Path,
    *,
    init: bool = False,
    dry_run: bool = False,
    exclude_scopes: tuple[str, ...] = (),
) -> dict[str, Any]:
    for scope in exclude_scopes:
        if not hubc.valid_scope(scope):
            raise hubc.HubConfigError(f"invalid excluded scope: {scope!r}")
    exclude_scopes = tuple(sorted(set(exclude_scopes)))
    out = out.absolute()
    _check_destination(out)
    if not init and not os.path.lexists(out / MARKER) and _foreign_entries(out):
        # Refuse before taking the lock, which would leave a file in a folder that is not ours.
        raise hubc.HubConfigError(
            "non-empty mirror destination lacks ownership marker. Use --init to preserve foreign files"
        )
    # The pull stays outside the destination lock: it can wait on the network.
    heads, principal, warnings = _heads(target, client, settings)
    identity = {"hub": client.identity, "principal": principal, "workspace": str(target.resolve())}
    if dry_run:
        selected, skipped = _select(heads, exclude_scopes)
        return {
            **_transact(out, identity, heads, selected, skipped, warnings, init=init, dry_run=True),
            "excluded_scopes": list(exclude_scopes),
        }
    try:
        out.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise hubc.HubConfigError(f"cannot create mirror destination {out}: {exc.strerror or exc}") from exc
    _check_destination(out)
    # The lock helper refuses a symlinked path component, which a destination may legitimately have.
    # Lock and writes use this one path, so a later swap of `out` cannot split them.
    resolved = out.resolve()
    # Lock order: the destination lock, then `store.locked` inside `_readable_heads`. Nothing takes them reversed.
    with held_file_lock(resolved / LOCK, deadline_seconds=LOCK_DEADLINE_SECONDS):
        # A run that pulled earlier may reach the lock after a newer run finished. Plan
        # from the replica as it is now, so the last run to write always writes the newest state.
        heads, current = _readable_heads(target, client, settings)
        _require_principal(principal, current)
        _require_scan_state(heads)
        selected, skipped = _select(heads, exclude_scopes)
        return {
            **_transact(resolved, identity, heads, selected, skipped, warnings, init=init, dry_run=False),
            "excluded_scopes": list(exclude_scopes),
        }


def _transact(
    out: Path,
    identity: dict[str, str],
    heads: list[dict],
    selected: list[tuple[dict, dict]],
    skipped: dict[str, int],
    warnings: list[str],
    *,
    init: bool,
    dry_run: bool,
) -> dict[str, Any]:
    """Marker read, plan, writes, deletes and the final marker. A real run holds the destination lock throughout."""
    raw = _read(out / MARKER)
    try:
        marker: dict[str, Any] = json.loads(raw) if raw is not None else {"kind": KIND, "files": {}, "notes": {}}
        if (
            marker.get("kind") != KIND
            or not isinstance(marker.get("files"), dict)
            or not isinstance(marker.get("notes"), dict)
        ):
            raise ValueError("invalid marker")
        if any(not _safe_name(name) for name in marker["files"]):
            raise ValueError("unsafe owned filename")
    except (ValueError, AttributeError) as exc:
        raise hubc.HubConfigError(f"invalid mirror marker: {exc}") from exc
    if raw is None and not init and _foreign_entries(out):
        raise hubc.HubConfigError(
            "non-empty mirror destination lacks ownership marker. Use --init to preserve foreign files"
        )
    # One folder per workspace identity. A marker from before a field is adopted by this run.
    if not init and any(marker.get(name) not in (None, value) for name, value in identity.items()):
        raise hubc.HubConfigError(
            "mirror folder belongs to another hub, principal or workspace. "
            "Use one folder per workspace, or --init to adopt it"
        )
    if not dry_run and raw is not None:
        # Only a folder this tool already owns: a first run on someone's vault never deletes their files.
        _drop_stale_temps(out)
    desired: dict[str, str] = {}
    notes: dict[str, str] = {}
    groups: dict[str, list[str]] = {}
    for head, meta in sorted(selected, key=lambda row: (row[0]["scope"], row[0]["card_id"])):
        key = store.Replica.key(head["scope"], head["card_id"])
        name = marker["notes"].get(key) or _filename(str(meta.get("title") or head["card_id"]), key)
        if not _safe_name(name) or name == INDEX or name in desired:
            raise hubc.HubConfigError("mirror filename collision or invalid marker mapping")
        notes[key] = name
        desired[name] = _note(head, meta)
        groups.setdefault(head["scope"], []).append(f"- [[{name[:-3]}]]")
    # A card skipped for now keeps its filename, so it returns under the name its links use.
    for head in heads:
        key = store.Replica.key(head["scope"], head["card_id"])
        kept = marker["notes"].get(key)
        if key not in notes and kept is not None and _safe_name(kept) and kept != INDEX:
            notes[key] = kept
    desired[INDEX] = "# Hub Memory\n\nGenerated from hub memory. Local edits are replaced.\n" + "".join(
        f"\n## {scope}\n\n" + "\n".join(links) + "\n" for scope, links in sorted(groups.items())
    )
    written = []
    for name, text in desired.items():
        existing = _read(out / name)
        # An unowned file that already holds exactly the output is adopted: a run
        # killed before its marker write leaves such files behind.
        if existing is not None and name not in marker["files"] and existing != text.encode("utf-8"):
            raise hubc.HubConfigError(f"mirror refuses to overwrite foreign file: {name}")
        if existing != text.encode("utf-8"):
            written.append(name)
    deleted = sorted(set(marker["files"]) - set(desired))
    for name in deleted:
        _read(out / name)  # Refuse symlinks before making any changes.
    if not dry_run:
        current = raw
        if written or deleted:
            # Own everything about to be written or removed before touching it, so a
            # run killed midway leaves nothing foreign and nothing unowned.
            current = _marker_value({**marker["files"], **dict.fromkeys(desired, True)}, notes, identity)
            if current != raw:
                # Synced before any note changes, so a crash cannot keep a note and lose its owner.
                _atomic(out / MARKER, current.decode("utf-8"))
                _sync_directory(out)
        for name in written:
            _atomic(out / name, desired[name])
        for name in deleted:
            (out / name).unlink(missing_ok=True)
        if deleted:
            # Unlinks are durable before the marker stops naming them, or a crash could leave an unowned note.
            _sync_directory(out)
        value = _marker_value(dict.fromkeys(desired, True), notes, identity)
        if current != value:
            _atomic(out / MARKER, value.decode("utf-8"))
            _sync_directory(out)
    return {
        "ok": True,
        "written": written,
        "deleted": deleted,
        "skipped": skipped,
        "dry_run": dry_run,
        "warnings": warnings,
    }
