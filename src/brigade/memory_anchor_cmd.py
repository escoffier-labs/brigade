"""`brigade memory anchor`: deterministic claim-anchor backfill for local memory cards.

Hub memory proposals address a claim by `block_id` and `expected_block_hash`,
so a card with no `^c-xxxxxx` anchors cannot be the target of a reviewed or
reflected proposal. Local proposal creation mints anchors on demand through
`memory_proposal_v2_ops.anchor_text`. This command runs that same function over
the card corpus up front, so the next sync push publishes anchored revisions.

It never carries a second anchoring algorithm. It reports counts only and never
prints card text.
"""

from __future__ import annotations

import codecs
import fnmatch
import json
import os
import secrets
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import memory_cmd, memory_hub_sync
from . import memory_proposal_io as io
from . import memory_proposal_v2_ops as delta
from .memory_proposals import ProposalError

SCHEMA = "brigade.memory-anchor.v1"

# Skip reasons, in report order. Each is a count in the output, never card text.
SKIP_REASONS = (
    "symlink",
    "outside-roots",
    "read-only",
    "unreadable",
    "byte-order-mark",
    "too-large",
    "withheld",
    "unsupported-frontmatter",
    "invalid-anchor-state",
    "reservation-limit",
    "opaque",
)


@dataclass
class AnchorReport:
    target: Path
    apply: bool
    limit: int | None = None
    files_scanned: int = 0
    files_already_anchored: int = 0
    files_to_change: int = 0
    blocks_to_anchor: int = 0
    files_changed: int = 0
    blocks_anchored: int = 0
    files_deferred: int = 0
    files_failed: int = 0
    skipped: dict[str, int] = field(default_factory=lambda: dict.fromkeys(SKIP_REASONS, 0))

    def payload(self) -> dict[str, Any]:
        return {
            "schema": SCHEMA,
            "target": str(self.target),
            "mode": "apply" if self.apply else "dry-run",
            "limit": self.limit,
            "files_scanned": self.files_scanned,
            "files_already_anchored": self.files_already_anchored,
            "files_to_change": self.files_to_change,
            "blocks_to_anchor": self.blocks_to_anchor,
            "files_changed": self.files_changed,
            "blocks_anchored": self.blocks_anchored,
            "files_deferred": self.files_deferred,
            "files_failed": self.files_failed,
            "files_skipped": dict(self.skipped),
            "files_skipped_total": sum(self.skipped.values()),
        }


@dataclass(frozen=True)
class _Change:
    original: bytes
    updated: bytes
    blocks: int


def _symlinked(path: Path, target: Path) -> bool:
    """True when the card or any directory between it and `target` is a symlink."""
    current = path
    while current != target:
        if current.is_symlink():
            return True
        if current.parent == current:
            return True
        current = current.parent
    return False


def _anchor_state_reason(exc: ProposalError) -> str:
    message = str(exc)
    if "reservation limit" in message:
        return "reservation-limit"
    if "duplicate claim block ids" in message or "claim anchor reservations" in message:
        return "invalid-anchor-state"
    return "unsupported-frontmatter"


def _plan_file(path: Path, rel: str) -> tuple[str | None, _Change | None]:
    """Return (skip reason, None), (None, None) when already anchored, or (None, change)."""
    try:
        original = io.read_bytes(path, limit=io.MAX_CARD_BYTES)
        assert original is not None
        # The shared proposal parser does not strip a BOM, so it would miss the
        # frontmatter fence and anchor frontmatter lines as claims. Leave these alone.
        if original.startswith(codecs.BOM_UTF8):
            return "byte-order-mark", None
        text = original.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return "unreadable", None
    except ValueError:
        return "too-large", None
    except OSError:
        return "unreadable", None
    # Sync never publishes a withheld card, so an anchored revision would be dead weight.
    if memory_hub_sync.withheld(text) is not None:
        return "withheld", None
    try:
        addressable = delta.blocks(text)
        delta.reservations(text)
        if not addressable:
            return "opaque", None
        pending = [block for block in addressable if not block.block_id]
        if not pending:
            return None, None
        # The exact function local proposals use, so ids and claim_anchor_ids match.
        updated = delta.anchor_text(text, rel)
    except ProposalError as exc:
        return _anchor_state_reason(exc), None
    except ValueError:
        return "unsupported-frontmatter", None
    encoded = updated.encode("utf-8")
    if len(encoded) > io.MAX_CARD_BYTES:
        return "too-large", None
    if encoded == original:
        return None, None
    return None, _Change(original, encoded, len(pending))


def _replace(path: Path, change: _Change) -> None:
    """Atomically swap in `change.updated`, only while the file still holds `change.original`.

    The card stays open and, where flock exists, exclusively locked from the
    content check until after the rename. Immediately before the rename the
    command rechecks the inode, size, mtime, ctime and bytes, so an edit to the
    same inode after the scan is refused instead of overwritten. The temp file
    lives in the card's directory, is fsynced before the rename, and takes the
    original mode and owner. A card whose owner cannot be kept is refused.
    """
    with io.directory(path.parent) as parent:
        name = path.name
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            _lock(fd)
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise OSError("card must be a regular single-link file")
            if _read_all(fd) != change.original:
                raise OSError("card changed since it was scanned")
            temp = f".{name}.anchor-{secrets.token_hex(4)}.tmp"
            out = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                try:
                    _write_all(out, change.updated)
                    os.fchmod(out, stat.S_IMODE(before.st_mode))
                    _keep_owner(out, before)
                    os.fsync(out)
                finally:
                    os.close(out)
                live = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if (live.st_dev, live.st_ino) != (before.st_dev, before.st_ino):
                    raise OSError("card was replaced since it was scanned")
                if _stamp(os.fstat(fd)) != _stamp(before) or _read_all(fd) != change.original:
                    raise OSError("card changed since it was scanned")
                os.replace(temp, name, src_dir_fd=parent, dst_dir_fd=parent)
            except BaseException:
                try:
                    os.unlink(temp, dir_fd=parent)
                except OSError:
                    pass
                raise
        finally:
            os.close(fd)
        os.fsync(parent)


def _lock(fd: int) -> None:
    """Take a cooperative exclusive lock so lock-aware writers and other runs wait."""
    try:
        import fcntl
    except ImportError:
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        raise OSError("card is locked by another writer") from exc


def _read_all(fd: int) -> bytes:
    os.lseek(fd, 0, os.SEEK_SET)
    data = b""
    while chunk := os.read(fd, 65536):
        data += chunk
        if len(data) > io.MAX_CARD_BYTES:
            raise OSError("card changed since it was scanned")
    return data


def _stamp(info: os.stat_result) -> tuple[int, int, int]:
    return (info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _keep_owner(fd: int, before: os.stat_result) -> None:
    """Give the temp file the card's owner, or refuse the card rather than change it."""
    created = os.fstat(fd)
    if (created.st_uid, created.st_gid) == (before.st_uid, before.st_gid):
        return
    try:
        os.fchown(fd, before.st_uid, before.st_gid)
    except OSError as exc:
        raise OSError("cannot keep the card's owner") from exc
    kept = os.fstat(fd)
    if (kept.st_uid, kept.st_gid) != (before.st_uid, before.st_gid):
        raise OSError("cannot keep the card's owner")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


def _selected(rel: str, patterns: tuple[str, ...]) -> bool:
    return not patterns or any(fnmatch.fnmatchcase(rel, pattern) for pattern in patterns)


def _validate_patterns(patterns: list[str] | None) -> tuple[str, ...]:
    clean: list[str] = []
    for pattern in patterns or []:
        normalized = pattern.replace("\\", "/")
        if not normalized or normalized.startswith("/") or ".." in normalized.split("/"):
            raise ValueError(f"--path must be a relative glob without traversal: {pattern!r}")
        clean.append(normalized)
    return tuple(clean)


def run_anchor(
    target: Path,
    *,
    apply: bool = False,
    patterns: list[str] | None = None,
    limit: int | None = None,
) -> AnchorReport:
    """Scan the card set sync publishes and, with `apply`, anchor each card in place."""
    if limit is not None and limit < 1:
        raise ValueError("--limit must be a positive integer")
    selected = _validate_patterns(patterns)
    target = target.expanduser().resolve()
    care = memory_cmd._config_or_default(target)
    report = AnchorReport(target=target, apply=apply, limit=limit)
    roots = [target / root for root in care.card_roots]
    # The same discovery `memory search` and the hub sync push use.
    for path in memory_cmd._iter_cards(target, care):
        rel = path.relative_to(target).as_posix()
        if not _selected(rel, selected):
            continue
        report.files_scanned += 1
        if _symlinked(path, target):
            report.skipped["symlink"] += 1
            continue
        if not any(path.is_relative_to(root) for root in roots):
            report.skipped["outside-roots"] += 1
            continue
        if memory_cmd._is_read_only(care, rel):
            report.skipped["read-only"] += 1
            continue
        reason, change = _plan_file(path, rel)
        if reason is not None:
            report.skipped[reason] += 1
            continue
        if change is None:
            report.files_already_anchored += 1
            continue
        if limit is not None and report.files_to_change >= limit:
            report.files_deferred += 1
            continue
        report.files_to_change += 1
        report.blocks_to_anchor += change.blocks
        if not apply:
            continue
        try:
            _replace(path, change)
        except OSError:
            report.files_failed += 1
            continue
        report.files_changed += 1
        report.blocks_anchored += change.blocks
    return report


def anchor(
    *,
    target: Path,
    apply: bool = False,
    json_output: bool = False,
    patterns: list[str] | None = None,
    limit: int | None = None,
) -> int:
    """CLI entry. Exit 0 on success, 1 when an apply could not write a card, 2 on bad input."""
    resolved = target.expanduser().resolve()
    if not resolved.is_dir():
        print(f"error: target is not a directory: {target}", file=sys.stderr)
        return 2
    try:
        report = run_anchor(resolved, apply=apply, patterns=patterns, limit=limit)
    except ValueError as exc:
        text = str(exc)
        prefix = "" if text.startswith("--") else "invalid memory-care config: "
        print(f"error: {prefix}{text}", file=sys.stderr)
        return 2
    payload = report.payload()
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _print_summary(report, payload)
    return 1 if report.files_failed else 0


def _print_summary(report: AnchorReport, payload: dict[str, Any]) -> None:
    mode = "apply" if report.apply else "dry run"
    print(f"memory anchor ({mode}): {report.target}")
    print(f"files scanned: {report.files_scanned}")
    print(f"files already anchored: {report.files_already_anchored}")
    if report.apply:
        print(f"files changed: {report.files_changed} ({report.blocks_anchored} block(s) anchored)")
    else:
        print(f"files that would change: {report.files_to_change} ({report.blocks_to_anchor} block(s) to anchor)")
    if report.files_deferred:
        print(f"files deferred by --limit: {report.files_deferred}")
    if report.files_failed:
        print(f"files that could not be written: {report.files_failed}")
    skipped = {reason: count for reason, count in report.skipped.items() if count}
    if skipped:
        print(f"files skipped: {payload['files_skipped_total']}")
        for reason, count in skipped.items():
            print(f"  {reason}: {count}")
    if not report.apply and report.files_to_change:
        print("Dry run only. Re-run with --apply to write the anchors.")
