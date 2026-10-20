"""Body-free evidence matches and read-only consolidation findings.

Storage uses proposal IO's no-follow directory descriptors and refuses platforms
without them. Neither append nor scan writes canonical card files.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

from . import memory_proposal_io as io
from .card_identity import CardIdentity, valid_card_id

if TYPE_CHECKING:
    from .card_fingerprint import CardMatch
    from .memory_cmd import MemoryCareConfig

LOG_PATH = ".brigade/memory/reflect/evidence-log.jsonl"
MAX_LINE_BYTES = 4096
MAX_SCAN_BYTES = 4 * 1024 * 1024
KINDS = {"near-match", "opposite-polarity", "reinforced", "outcome"}
_FIELDS = {"ts", "card_id", "card_path", "evidence_id", "kind", "similarity"}


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _valid(row: object) -> bool:
    if not isinstance(row, dict) or set(row) != _FIELDS:
        return False
    if any(
        not isinstance(row[key], str) or not row[key] or any(ord(c) < 32 for c in row[key])
        for key in _FIELDS - {"similarity"}
    ):
        return False
    similarity = row["similarity"]
    try:
        io.relative(row["card_path"])
    except ValueError:
        return False
    return (
        row["kind"] in KINDS
        and _timestamp(row["ts"]) is not None
        and isinstance(similarity, (int, float))
        and not isinstance(similarity, bool)
        and 0 <= similarity <= 1
    )


def append_evidence(target: Path, *, card_path: Path, evidence_id: str, kind: str, similarity: float) -> None:
    """Append one bounded row, failing closed on unsafe paths or capabilities."""
    from .card_identity import card_identity
    from .memory_cmd import _parse_frontmatter

    rel = card_path.relative_to(target).as_posix()
    meta, _ = _parse_frontmatter(io.text(card_path, limit=io.MAX_CARD_BYTES) or "")
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "card_id": card_identity(meta, rel).card_id,
        "card_path": rel,
        "evidence_id": evidence_id,
        "kind": kind,
        "similarity": similarity,
    }
    if not _valid(row):
        raise ValueError("invalid evidence log row")
    data = io.json_bytes(row)
    if len(data) > MAX_LINE_BYTES:
        raise ValueError("evidence log line size limit exceeded")
    path = target / LOG_PATH
    with io.directory(path.parent, create=True, private=True) as parent:
        fd = os.open(
            path.name, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent
        )
        try:
            io._regular(fd, private=True)
            # One append syscall keeps concurrent rows together. Never truncate
            # or replace this inode, including when a previous row is malformed.
            if os.write(fd, data) != len(data):
                raise OSError("short evidence log append")
            os.fsync(fd)
            os.fsync(parent)
        finally:
            os.close(fd)


def log_match(target: Path, match: CardMatch | None, handoff_path: Path) -> None:
    """Record an executed ingest match without changing its routing behavior."""
    if match is None:
        return
    # Cross-workspace evidence sections contain prose, not identifiers.
    # Keep their content out of this ledger as well as the card body.
    try:
        evidence_id = handoff_path.relative_to(target).as_posix()
    except ValueError:
        evidence_id = "sha256:" + hashlib.sha256(handoff_path.absolute().as_posix().encode()).hexdigest()
    kind = "reinforced" if match.kind == "exact" else "opposite-polarity" if match.opposite_polarity else "near-match"
    try:
        append_evidence(target, card_path=match.path, evidence_id=evidence_id, kind=kind, similarity=match.similarity)
    except (OSError, ValueError) as exc:
        print(f"warning: evidence log append failed: {exc}", file=sys.stderr)


def read_evidence(target: Path) -> tuple[list[dict[str, Any]], int, bool]:
    """Return newest bounded rows, warning count, and unsupported IO capability."""
    if os.open not in os.supports_dir_fd or not hasattr(os, "O_NOFOLLOW"):
        return [], 0, True
    path = target / LOG_PATH
    rows: list[dict[str, Any]] = []
    warnings = 0
    try:
        with io.directory(path.parent) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as handle:
                remaining = io._regular(handle.fileno()).st_size
                # Seek past old history so IO and retained rows stay bounded.
                # Inspect one preceding byte to preserve a row on the boundary.
                if remaining > MAX_SCAN_BYTES:
                    handle.seek(remaining - MAX_SCAN_BYTES - 1)
                    boundary = handle.read(1) == b"\n"
                    remaining = MAX_SCAN_BYTES
                    warnings += 1
                    if not boundary:
                        remaining -= len(handle.readline(remaining))
                # Bound the scan to the initial size, even during live appends.
                while remaining > 0:
                    line = handle.readline(min(MAX_LINE_BYTES + 1, remaining))
                    if not line:
                        break
                    remaining -= len(line)
                    oversized = len(line) > MAX_LINE_BYTES
                    terminated = line.endswith(b"\n")
                    while not terminated and remaining > 0:
                        chunk = handle.readline(min(MAX_LINE_BYTES + 1, remaining))
                        if not chunk:
                            break
                        remaining -= len(chunk)
                        terminated = chunk.endswith(b"\n")
                        oversized = True
                    if oversized or not terminated:
                        warnings += 1
                        continue
                    try:
                        row = json.loads(line)
                        if not _valid(row):
                            raise ValueError("invalid evidence log row")
                    except (ValueError, UnicodeError, RecursionError):
                        warnings += 1
                        continue
                    rows.append(row)
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        warnings += 1
    return rows, warnings, False


class EvidenceLog:
    """Read once per care scan and collect findings through the existing factory."""

    def __init__(self, target: Path) -> None:
        rows, warning_count, unsupported = read_evidence(target)
        self.summary = {
            "path": LOG_PATH,
            "row_count": len(rows),
            "warning_count": warning_count,
            "unsupported": unsupported,
        }
        self.by_identity: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            # Stable IDs survive renames. Legacy cards fall back to their path,
            # so duplicate topic names do not borrow another card's evidence.
            key = valid_card_id(row["card_id"]) or row["card_path"]
            self.by_identity.setdefault(key, []).append(row)
        if warning_count:
            print(
                f"warning: evidence log reported {warning_count} warning(s): "
                "invalid line(s), unreadable file(s), or scan limit exceeded",
                file=sys.stderr,
            )

    def findings(
        self, target: Path, config: MemoryCareConfig, rel: str, identity: CardIdentity, meta: dict[str, Any]
    ) -> list[dict[str, Any]]:
        from .memory_cmd import _frontmatter_value, _issue

        if "unconsolidated-evidence" not in config.enabled_checks:
            return []
        reference_value = meta.get("last_consolidated")
        reference = _timestamp(reference_value)
        consolidated = reference is not None
        if reference is None:
            reference_value = _frontmatter_value(meta, "last_reviewed", "last_reviewed_at", "reviewed_at")
            reference = _timestamp(reference_value)
        if reference is None or not isinstance(reference_value, str):
            return []
        try:
            date.fromisoformat(reference_value)
            date_only = True
        except ValueError:
            date_only = False
        rows = [
            (row, _timestamp(row["ts"]))
            for key in (identity.card_id, *identity.aliases)
            for row in self.by_identity.get(key, [])
        ]
        # Reinforcement bumps last_reviewed without reviewing pending matches.
        # Its date comes from local date.today(), unlike the ledger's UTC ts.
        machine_review = not consolidated and any(
            row["kind"] == "reinforced"
            and timestamp is not None
            and (timestamp.astimezone().date() >= reference.date() if date_only else timestamp >= reference)
            for row, timestamp in rows
        )
        references = {
            row["evidence_id"]
            for row, timestamp in rows
            if row["kind"] != "reinforced"
            and timestamp is not None
            and (machine_review or (timestamp.date() > reference.date() if date_only else timestamp > reference))
        }
        if not references:
            return []
        return [
            _issue(
                target=target,
                card_path=rel,
                card_id=identity.card_id,
                issue_type="unconsolidated-evidence",
                severity="medium",
                summary=f"{rel} has matched evidence newer than its last consolidation or review.",
                evidence=sorted(references),
                action="Review the matched evidence and consolidate the card through the reviewed memory workflow.",
            )
        ]
