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
    from .outcome import OutcomeRecord

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


def append_evidence(
    target: Path,
    *,
    card_path: Path,
    evidence_id: str,
    kind: str,
    similarity: float,
    ts: str | None = None,
    expected_card_text: str | None = None,
) -> None:
    """Append one bounded row, failing closed on unsafe paths or capabilities."""
    from .card_identity import card_identity
    from .memory_cmd import _parse_frontmatter

    rel = card_path.relative_to(target).as_posix()
    text = io.text(card_path, limit=io.MAX_CARD_BYTES) or ""
    if expected_card_text is not None and text != expected_card_text:
        raise ValueError("card changed before evidence append")
    meta, _ = _parse_frontmatter(text)
    row = {
        "ts": ts if ts is not None else datetime.now(timezone.utc).isoformat(),
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


def verify_outcome_ref(ref: str) -> bool:
    """Recognize the current outcome writer's canonical verify receipt pointer."""
    try:
        io.relative(ref)
    except ValueError:
        return False
    parts = Path(ref).parts
    return len(parts) == 5 and parts[:3] == (".brigade", "work", "verify-runs") and parts[-1] == "receipt.json"


def _card_stems(cards_dir: Path) -> dict[str, str]:
    """Bound discovery before reading any card, without resolving directory links."""
    stems: dict[str, str] = {}
    with io.directory(cards_dir) as parent, os.scandir(parent) as entries:
        for count, entry in enumerate(entries, start=1):
            if count > io.MAX_FILES:
                raise ValueError("card directory entry limit exceeded")
            if not entry.name.endswith(".md"):
                continue
            io.relative(entry.name)
            stem = entry.name[:-3]
            lower = stem.lower()
            if lower in stems:
                # The legacy discovery order is ambiguous for colliding names.
                raise ValueError("ambiguous card stem")
            stems[lower] = stem
    return stems


def _linked_snapshot(path: Path) -> dict[str, bytes]:
    """Read a bounded closure, charging its byte budget before each read/decode."""
    from .memory_doctor.parsing import extract_wiki_links
    from .outcome_cmd import _link_target_slug

    stems = _card_stems(path.parent)
    pending = [path.stem]
    queued = {path.stem.lower()}
    snapshot: dict[str, bytes] = {}
    total = 0
    while pending:
        if len(snapshot) >= io.MAX_FILES:
            raise ValueError("linked card count limit exceeded")
        stem = pending.pop()
        raw = io.read_bytes(path.parent / f"{stem}.md", limit=min(io.MAX_CARD_BYTES, io.MAX_CORPUS_BYTES - total))
        assert raw is not None
        total += len(raw)
        snapshot[stem] = raw
        for link in extract_wiki_links(raw.decode("utf-8", errors="replace")):
            slug = _link_target_slug(link).lower()
            actual = stems.get(slug)
            if actual is None or slug in queued:
                continue
            # Refuse unsafe links conservatively; reads always use O_NOFOLLOW.
            io.relative(f"{actual}.md")
            if len(queued) >= io.MAX_FILES:
                raise ValueError("linked card count limit exceeded")
            queued.add(slug)
            pending.append(actual)
    return snapshot


def _snapshot_fingerprint(target: Path, card_path: str, text: str, artifact_id: str) -> str | None:
    """Recheck bounded no-follow snapshots using the exact legacy digest format."""
    from .outcome_cmd import _safe_card_id_component

    if _safe_card_id_component(artifact_id) is None:
        return None
    path = target / "memory" / "cards" / f"{artifact_id}.md"
    if path != target / io.relative(card_path):
        return None
    snapshot = _linked_snapshot(path)
    raw = snapshot.pop(path.stem)
    if raw != text.encode():
        return None
    digest = hashlib.sha256()
    digest.update(hashlib.sha256(raw).hexdigest().encode("ascii"))
    digest.update(b"\0")
    for stem, linked in sorted(snapshot.items()):
        digest.update(stem.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(linked).hexdigest().encode("ascii"))
        digest.update(b"\0")
    fingerprint = digest.hexdigest() if snapshot else hashlib.sha256(raw).hexdigest()
    snapshot[path.stem] = raw
    # Rediscover as well as reread: a formerly absent link may now exist.
    return fingerprint if _linked_snapshot(path) == snapshot else None


def validate_outcome_support(
    target: Path,
    *,
    card_path: str,
    card_text: str,
    ref: str,
    timestamp: str | None = None,
    observed: dict[str, Any] | None = None,
) -> bool:
    """Revalidate logged verify support against a specific card and retained receipt.

    Generic evidence is deliberately outside this contract. Callers select only
    canonical verify pointers attributed by outcome log rows.
    """
    from . import scorecard, verify_trial

    try:
        if not verify_outcome_ref(ref):
            return False
        path = target / ref
        raw = io.read_bytes(path)
        receipt = json.loads(raw or b"")
        if not isinstance(receipt, dict) or (observed is not None and receipt != observed):
            return False
        if receipt.get("run_id") != path.parent.name or receipt.get("path") != str(path.parent):
            return False
        if receipt.get("status") != "completed" or receipt.get("reused_from"):
            return False
        binding = receipt.get("subject_binding")
        if not isinstance(binding, dict) or binding.get("artifact_kind") != "card":
            return False
        artifact_id = binding.get("artifact_id")
        if not isinstance(artifact_id, str):
            return False
        stamp = receipt.get("completed_at") or receipt.get("started_at")
        if _timestamp(stamp) is None or (timestamp is not None and timestamp != stamp):
            return False
        if not verify_trial.project_trial(receipt, target=target).eligible:
            return False
        if scorecard.effectiveness_outcome(receipt) != 1:
            return False
        # Admission may read other state. Recheck both retained bytes and the
        # full linked-card fingerprint after it, rather than trusting old data.
        fingerprint = _snapshot_fingerprint(target, card_path, card_text, artifact_id)
        return bool(fingerprint and fingerprint == binding.get("content_fingerprint") and io.read_bytes(path) == raw)
    except (OSError, ValueError, UnicodeError, RecursionError, TypeError):
        return False


def log_outcome(target: Path, record: OutcomeRecord, receipt: dict[str, Any]) -> None:
    """Admit a durable card outcome, preserving original time and distinct proof."""
    from . import outcome_cmd, scorecard, verify_trial

    if record.source != "verify" or record.artifact_kind != "card":
        return
    if receipt.get("status") != "completed" or receipt.get("reused_from"):
        return
    binding = receipt.get("subject_binding")
    if (
        not isinstance(binding, dict)
        or binding.get("artifact_kind") != "card"
        or binding.get("artifact_id") != record.artifact_id
    ):
        return
    path = outcome_cmd._artifact_content_path(target, record.artifact_id, "card")
    if path is None:
        return
    text = io.text(path, limit=io.MAX_CARD_BYTES)
    if text is None:
        return
    fingerprint = _snapshot_fingerprint(target, path.relative_to(target).as_posix(), text, record.artifact_id)
    if not fingerprint or fingerprint != binding.get("content_fingerprint"):
        return
    # Ordinary exclusions are silent. Only a receipt admitted here can lose
    # consistency at the subsequent safe read and request a capture retry.
    try:
        eligible = verify_trial.project_trial(receipt, target=target).eligible
    except (ValueError, TypeError, RecursionError):
        return
    if not eligible:
        return
    if scorecard.effectiveness_outcome(receipt) != 1:
        return
    timestamp = receipt.get("completed_at") or receipt.get("started_at")
    if _timestamp(timestamp) is None:
        return
    ref = Path(record.evidence_ref).relative_to(target).as_posix()
    if not validate_outcome_support(
        target, card_path=path.relative_to(target).as_posix(), card_text=text, ref=ref, observed=receipt
    ):
        raise ValueError("outcome support is not consistent with retained evidence")
    append_evidence(
        target,
        card_path=path,
        evidence_id=ref,
        kind="outcome",
        similarity=1.0,
        ts=timestamp,
        expected_card_text=text,
    )


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
