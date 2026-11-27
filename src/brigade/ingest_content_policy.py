"""Evaluate and journal card filing for verified unknown-origin drafts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
import hashlib
import os
from pathlib import Path
from stat import S_IMODE

from . import dirfd, handoff_origin, handoff_worktrees, ingest, memory_cmd, memory_content_scan
from . import memory_care_journal as journal
from .card_fingerprint import _insert_frontmatter_fields
from .handoff_ingest_rules import CARDS_RELATIVE, EXPECTED_SHA_SECTION, SHA256_RE, sha_matches
from .inbox_lock import InboxLockTimeout, held_file_lock
from .memory_inbox_io import read

_REPLACED_METADATA = frozenset({"trust", "author", "author_type", "pinned", "archive", "auto_filed"})


def _destination_refusal(sections: dict[str, str], before: bytes | None, *, publishing: bool = False) -> str | None:
    action = sections["recommended memory action"].strip().lower()
    if action == "create-card":
        return "card-name-occupied" if before is not None else None
    if action != "update-card":
        return "action-not-auto-handled"
    expected = sections.get(EXPECTED_SHA_SECTION.lower(), "").strip().lower()
    if not expected:
        return "expected-sha-required"
    if not SHA256_RE.fullmatch(expected):
        return "expected-sha-invalid"
    if before is None or not sha_matches(before, expected):
        return "expected-sha-stale" if publishing else "expected-sha-mismatch"
    return None


def _mark_filed(card: str) -> str:
    """Write the filing metadata and retain the rest of the card."""
    lines = card.replace("\r\n", "\n").split("\n")
    closing = next(i for i, line in enumerate(lines[1:], 1) if line.strip() == "---")
    kept = [lines[0]]
    dropped_indent = None
    for line in lines[1:closing]:
        key, sep, _ = line.partition(":")
        indent = len(line) - len(line.lstrip())
        if sep and key.strip().strip("\"'").casefold() in _REPLACED_METADATA:
            dropped_indent = indent
            continue
        if dropped_indent is not None and (indent > dropped_indent or line.lstrip().startswith("- ")):
            continue
        if line.strip() and not line.lstrip().startswith("#"):
            dropped_indent = None
        kept.append(line)
    text = "\n".join([*kept, *lines[closing:]])
    return _insert_frontmatter_fields(text, {"trust": "agent-unreviewed", "auto_filed": date.today().isoformat()})


def decide(
    sections: dict[str, str], snapshot: bytes, *, owner: Path, promote_cards: bool, card_ids: ingest.CardIdIndex
) -> ingest.Outcome:
    try:
        outcome = ingest.decide(sections, owner, promote_cards, route_documents=False, card_ids=card_ids)
        if outcome.kind in {"promoted", "reinforced"} or outcome.match is not None:
            relative = (CARDS_RELATIVE / sections["target card"].strip()).as_posix()
            refusal = _destination_refusal(sections, read(owner, relative, limit=ingest.INSPECTION_CEILING))
            if refusal:
                return ingest.Outcome("inboxed", code=refusal)
        if outcome.kind in {"promoted", "reinforced"}:
            text = snapshot.decode("utf-8")
            verdict, rules = memory_content_scan.scan_verdict([text])
            injection = ingest.scan_untrusted(text)
            if verdict in {memory_content_scan.BLOCKED, memory_content_scan.FLAGGED, memory_content_scan.PRIVATE}:
                return ingest.Outcome("inboxed", code="handoff-content-signal")
            if verdict != memory_content_scan.CLEAN or rules != () or type(injection.flagged) is not bool:
                raise ValueError("incomplete content policy scan")
            if injection.flagged:
                return ingest.Outcome("inboxed", code="handoff-content-signal")
            if outcome.kind == "reinforced":
                return ingest.Outcome("inboxed", code="duplicate-card-content", match=outcome.match)
            if outcome.dest is None:
                raise ValueError("missing card destination")
        elif outcome.kind not in {"inboxed", "skipped"}:
            raise ValueError("unexpected content policy decision")
        if not outcome.code:
            raise ValueError("missing content policy decision")
        return outcome
    except Exception:
        return ingest.Outcome("inboxed", code="content-policy-error")


def file_card(
    outcome: ingest.Outcome,
    owner: Path,
    sections: dict[str, str],
    origin: handoff_origin.VerifiedOrigin,
    *,
    claim_origin: Callable[[], bool],
    card_ids: ingest.CardIdIndex,
    dry_run: bool,
) -> str | None:
    """Return the refusal code, or journal and publish the card."""
    from contextlib import nullcontext

    dest = outcome.dest
    assert dest is not None
    relative = dest.relative_to(owner).as_posix()
    try:
        lock = journal.safe_path(owner, ".brigade/memory/proposals/proposal.lock")
        if not dry_run:
            lock.parent.mkdir(parents=True, exist_ok=True)
        with nullcontext() if dry_run else held_file_lock(lock, deadline_seconds=1):
            before = read(owner, relative, limit=ingest.INSPECTION_CEILING)
            refusal = _destination_refusal(sections, before, publishing=True)
            if refusal:
                return refusal
            existing_meta = ingest._check_card_state(before, "")
            built = _mark_filed(ingest._build_promoted_card(sections, existing_meta))
            after = built.encode("utf-8")
            if len(after) > memory_cmd._config_or_default(owner).max_card_bytes:
                return "card-budget-exceeded"
            if not dry_run:
                mode = 0o600 if before is None else S_IMODE(dest.stat(follow_symlinks=False).st_mode)
                if not claim_origin():
                    return "origin-review-required"
                journal.commit(
                    owner,
                    kind="inbox",
                    reason=outcome.code,
                    card_id=None,
                    changes=[journal.Change(relative, before, after, mode=mode)],
                    metadata={
                        "decision": "content-policy",
                        "origin": {
                            field: getattr(origin, field) for field in handoff_origin.Origin.__dataclass_fields__
                        },
                    },
                )
    except InboxLockTimeout:
        return "card-publication-busy"
    except Exception:
        return "content-policy-error"
    card_ids.register(relative, built)
    return None


def note_pending(inbox: Path, path: Path, code: str) -> None:
    """Retain a review entry while the original remains pending."""
    from .handoff_quarantine import _publish_metadata

    name = f"{code}-{hashlib.sha256(os.fsencode(path)).hexdigest()}.md"
    text = (
        f"<!-- routed from {handoff_worktrees.safe_text(path.name)}\n"
        f"     reason: {code}\n"
        f"     routed-at: {datetime.now(timezone.utc):%Y%m%dT%H%M%SZ} -->\n\n"
        "The original remains in the writer inbox. Inspect it there.\n"
    )
    parent = dirfd.open_directory_chain(inbox.absolute(), create=True)
    try:
        try:
            _publish_metadata(parent, name, text)
        except FileExistsError:
            pass
    finally:
        os.close(parent)
