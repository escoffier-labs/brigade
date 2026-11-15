"""Journal the recovery of a review draft after refused publication."""

from __future__ import annotations

import re
from pathlib import Path

from . import memory_care_journal as journal
from .memory_cmd import _config_or_default

RESOLVED_DIGEST_MISMATCH = "resolved-copy-digest-mismatch"
RESOLVED_READ_ONLY = "resolved-copy-read-only"


def _available_inbox_path(target: Path, draft: str) -> str:
    if journal.current(target, draft) is None:
        return draft
    path = Path(draft)
    for number in range(1, 10000):
        candidate = (path.parent / f"{path.stem}-append-refused-{number}{path.suffix}").as_posix()
        if journal.current(target, candidate) is None:
            return candidate
    raise journal.JournalConflict("refused draft namespace exhausted")


def restore(target: Path, decision_id: str, draft: str, *, reason: str) -> str | None:
    """Restore a draft without replacement and remove only its unchanged resolved copy."""
    for record in sorted(journal.safe_path(target, journal.JOURNAL_REL).glob("*/decision.json")):
        if not re.fullmatch(journal.DECISION_ID, record.parent.name):
            continue
        try:
            payload = journal._payload(target, record.parent.name)
        except journal.SymlinkPath:
            raise
        except (journal.JournalConflict, journal.store.StoreError):
            # Recovery's pending-index pass reports unreadable records and retains them.
            continue
        if payload.get("refusal_of") == decision_id:
            if payload.get("phase") == "prepared":
                journal.finish_prepared(target, record.parent.name)
                payload = journal._payload(target, record.parent.name)
            if payload.get("status") == "committed":
                return payload.get("refusal_reason")

    decision = journal.load(target, decision_id)
    original = journal.before_bytes(target, decision, draft)
    if original is None:
        raise journal.JournalConflict("refused draft before-image missing")

    # Cited drafts stay in place. A prior interrupted restore can also have put these bytes back.
    destination = None if journal.current(target, draft) == original else _available_inbox_path(target, draft)
    changes = [] if destination is None else [journal.Change(destination, None, original)]
    mismatch = False
    protected = False
    read_only = _config_or_default(target).read_only_paths
    for path in decision.paths:
        if path == draft or not path.startswith("memory/handoff-inbox/resolved/"):
            continue
        live = journal.current(target, path)
        if live is None:
            continue
        if journal.read_only_pattern(path, read_only):
            protected = True
            continue  # Optional cleanup must not block restoration or enter the undo journal.
        if journal.kernel.content_digest(live) == decision.after_sha256[path]:
            changes.append(journal.Change(path, live, None))
        else:
            mismatch = True

    refusal_reason = RESOLVED_DIGEST_MISMATCH if mismatch else RESOLVED_READ_ONLY if protected else None
    if not changes:
        return refusal_reason
    cleanup = journal.commit_phased(
        target,
        kind="inbox",
        reason=reason,
        card_id=None,
        changes=changes,
        prepared_paths=[] if destination is None else [destination],
        hub={},
        metadata={"refusal_of": decision_id, "refusal_reason": refusal_reason},
    )
    if journal._payload(target, cleanup.decision_id).get("phase") != "committed":
        raise journal.JournalConflict("refused draft restoration abandoned")
    return refusal_reason
