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
from collections import Counter
from pathlib import Path
from typing import Any

from . import memory_hub_client as hubc
from . import memory_hub_store as store
from . import memory_hub_sync as sync
from .memory_vault import parse_frontmatter

MARKER = ".brigade-memory-mirror.json"
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


def _atomic(path: Path, text: str) -> None:
    fd, name = tempfile.mkstemp(prefix=".mirror-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _readable_heads(target: Path, client: hubc.MemoryHubClient, settings: hubc.HubSettings) -> list[dict[str, Any]]:
    with store.locked(target):
        replica = store.replica(target)
        if replica.watermark(client.identity) is None or not replica.meta.get("principal"):
            raise hubc.HubUnavailable("hub and usable replica unavailable, mirror unchanged")
        if not (replica.directory / "heads.json").is_file() or replica.meta.get("cards") != len(replica.heads):
            raise hubc.HubConfigError("replica heads are missing or inconsistent, mirror unchanged")
        if replica.missing:
            raise hubc.HubConfigError("replica is incomplete. Increase [hub] replica_max_mb and pull, mirror unchanged")
        return list(replica.readable_heads(settings.read_scopes).values())


def _heads(target: Path, client: hubc.MemoryHubClient, settings: hubc.HubSettings) -> tuple[list[dict], list[str]]:
    warnings: list[str] = []
    offline = False
    try:
        report = sync.pull(target, client, settings)
        warnings.extend(report["warnings"])
        if report.get("seq") is None:
            raise hubc.HubConfigError("replica rebuild incomplete, mirror unchanged. Run memory hub pull again")
    except hubc.HubUnavailable as exc:
        offline = True
        warnings.append(f"hub unavailable ({exc.reason}), using this host's replica")
    heads = _readable_heads(target, client, settings)
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
        heads = _readable_heads(target, client, settings)
        if any("scan_state" not in head for head in heads):
            raise hubc.HubConfigError(PREDATES)
    return heads, warnings


def _split(text: str) -> tuple[dict[str, Any], str]:
    """A card's leading front matter and the body after it. A leading BOM is ignored."""
    text = text.removeprefix("\ufeff")
    meta = parse_frontmatter(text)
    if text.startswith(("---\n", "---\r\n")):
        lines = text.splitlines(keepends=True)
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                return meta, "".join(lines[index + 1 :]).lstrip("\r\n")
    return meta, text


def _select(heads: list[dict]) -> tuple[list[tuple[dict, dict]], dict[str, int]]:
    skipped: Counter[str] = Counter()
    rows = [(head, _split(str(head.get("text", "")))[0]) for head in heads]
    # Supersession is scoped. Chains name predecessors with supersedes, or a
    # successor with superseded_by. Never revive a predecessor if its head is withheld.
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
        if head.get("state") == "retired":
            reason = "retired"
        elif head.get("trust") == "flagged" or head.get("scan_state") == "flagged":
            reason = "flagged"
        elif head.get("scan_state") == "pending":
            reason = "scan-pending"
        elif meta.get("visibility") == "private":
            reason = "visibility-private"
        elif withheld := sync.withheld(str(head.get("text", ""))):
            reason = withheld
        elif head.get("scan_state") != "clean":
            reason = "scan-unknown"
        elif (head["scope"], head["card_id"]) in superseded or meta.get("superseded_by"):
            reason = "superseded"
        if reason:
            skipped[reason] += 1
        else:
            selected.append((head, meta))
    return selected, dict(sorted(skipped.items()))


def _note(head: dict, meta: dict) -> str:
    # The card's own front matter moves under `card` so the note has one block.
    own = {key: value for key, value in meta.items() if key != "tags"}
    fields = {
        "card_id": head["card_id"],
        "scope": head["scope"],
        "revision": head["revision"],
        "updated_at": head.get("updated_at"),
        "source": head.get("writer"),
        "tags": meta.get("tags", []),
        "scan_state": head["scan_state"],
        "aliases": [str(meta.get("title") or head["card_id"]), head["card_id"]],
        **({"card": own} if own else {}),
    }
    # JSON values are YAML-compatible and quote control characters safely.
    front = "\n".join(
        f"{key}: {json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)}" for key, value in fields.items()
    )
    return f"---\n{front}\n---\n\n{_split(str(head['text']))[1]}"


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


def _marker_value(files: dict[str, Any], notes: dict[str, str]) -> bytes:
    return (json.dumps({"kind": KIND, "files": files, "notes": notes}, sort_keys=True) + "\n").encode("utf-8")


def mirror(
    target: Path,
    client: hubc.MemoryHubClient,
    settings: hubc.HubSettings,
    out: Path,
    *,
    init: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    out = out.absolute()
    _check_destination(out)
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
    if raw is None and out.is_dir() and any(out.iterdir()) and not init:
        raise hubc.HubConfigError(
            "non-empty mirror destination lacks ownership marker. Use --init to preserve foreign files"
        )
    heads, warnings = _heads(target, client, settings)
    selected, skipped = _select(heads)
    desired: dict[str, str] = {}
    notes = {}
    groups: dict[str, list[str]] = {}
    for head, meta in sorted(selected, key=lambda row: (row[0]["scope"], row[0]["card_id"])):
        key = store.Replica.key(head["scope"], head["card_id"])
        name = marker["notes"].get(key) or _filename(str(meta.get("title") or head["card_id"]), key)
        if not _safe_name(name) or name == INDEX or name in desired:
            raise hubc.HubConfigError("mirror filename collision or invalid marker mapping")
        notes[key] = name
        desired[name] = _note(head, meta)
        groups.setdefault(head["scope"], []).append(f"- [[{name[:-3]}]]")
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
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise hubc.HubConfigError(f"cannot create mirror destination {out}: {exc.strerror or exc}") from exc
        _check_destination(out)
        current = raw
        if written or deleted:
            # Own everything about to be written or removed before touching it, so a
            # run killed midway leaves nothing foreign and nothing unowned.
            current = _marker_value({**marker["files"], **dict.fromkeys(desired, True)}, notes)
            if current != raw:
                _atomic(out / MARKER, current.decode("utf-8"))
        for name in written:
            _atomic(out / name, desired[name])
        for name in deleted:
            (out / name).unlink(missing_ok=True)
        value = _marker_value(dict.fromkeys(desired, True), notes)
        if current != value:
            _atomic(out / MARKER, value.decode("utf-8"))
    return {
        "ok": True,
        "written": written,
        "deleted": deleted,
        "skipped": skipped,
        "dry_run": dry_run,
        "warnings": warnings,
    }
