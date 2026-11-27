"""Evaluate and journal card filing for verified unknown-origin drafts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
import hashlib
import os
from pathlib import Path
from stat import S_IMODE
from tempfile import TemporaryDirectory

from . import dirfd, handoff_origin, handoff_worktrees, ingest, memory_content_scan
from . import memory_care_journal as journal
from .inbox_lock import InboxLockTimeout, held_file_lock


def decide(
    sections: dict[str, str], snapshot: bytes, *, owner: Path, promote_cards: bool, card_ids: ingest.CardIdIndex
) -> ingest.Outcome:
    try:
        outcome = ingest.decide(sections, owner, promote_cards, route_documents=False, card_ids=card_ids)
        if outcome.kind in {"promoted", "reinforced"}:
            verdict, rules = memory_content_scan.scan_verdict([snapshot.decode("utf-8")])
            if verdict in {memory_content_scan.BLOCKED, memory_content_scan.FLAGGED, memory_content_scan.PRIVATE}:
                return ingest.Outcome("inboxed", code="handoff-content-signal")
            if verdict != memory_content_scan.CLEAN or rules != () or outcome.dest is None:
                raise ValueError("incomplete content policy result")
            if outcome.kind == "reinforced" and outcome.match is None:
                raise ValueError("missing reinforcement match")
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
    path: Path,
    origin: handoff_origin.VerifiedOrigin,
    *,
    claim_origin: Callable[[], bool],
    card_ids: ingest.CardIdIndex,
    dry_run: bool,
) -> str | None:
    """Return the refusal code, or journal and publish the card."""
    from contextlib import nullcontext

    from .memory_inbox_io import read

    dest = outcome.dest
    assert dest is not None
    relative = dest.relative_to(owner).as_posix()
    try:
        lock = journal.safe_path(owner, ".brigade/memory/proposals/proposal.lock")
        if not dry_run:
            lock.parent.mkdir(parents=True, exist_ok=True)
        with nullcontext() if dry_run else held_file_lock(lock, deadline_seconds=1):
            before = read(owner, relative, limit=ingest.INSPECTION_CEILING)
            if outcome.kind == "promoted":
                expected = ""
                if sections.get("recommended memory action", "").strip().lower() == "update-card":
                    expected = sections.get(ingest.EXPECTED_SHA_SECTION.lower(), "").strip().lower()
                built = ingest._build_promoted_card(sections, ingest._check_card_state(before, expected))
                after = built.encode("utf-8")
            else:
                if before is None:
                    return "card-publication-changed"
                with TemporaryDirectory(prefix="handoff-reinforce-") as staging:
                    copy = Path(staging) / dest.name
                    copy.write_bytes(before)
                    ingest.reinforce_existing_card(
                        copy,
                        today=date.today(),
                        evidence_pointer=ingest._evidence_pointer(path, sections=sections, target=owner),
                    )
                    after = copy.read_bytes()
                built = after.decode("utf-8")
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
    except ingest.StaleCardError:
        return "expected-sha-stale"
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
