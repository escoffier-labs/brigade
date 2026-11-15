"""Trusted local origin for fixtures that exercise filing after admission."""

import sys
from pathlib import Path

import pytest

from brigade import handoff_origin, receipt_signing

requires_automatic_filing = pytest.mark.skipif(
    sys.platform == "win32",
    reason="origin key confinement is unproven on Windows until #2588, so filing goes to review",
)


def windows_origin_review(target: Path, path: Path, *, owner: Path | None = None) -> bool:
    """Assert the Windows gate at the filing boundary before stamp-specific setup."""
    if sys.platform != "win32":
        return False
    from brigade import ingest, memory_inbox_drain
    from datetime import date

    owner = owner or target
    stats = ingest.IngestStats()
    assert ingest.ingest_into(source=target, owner=owner, stats=stats, promote_cards=True, route_documents=True) == 0
    assert stats.inboxed >= 1 and stats.promoted == stats.routed == stats.reinforced == 0
    assert all("origin-review-required" in action for action in stats.actions)
    result = memory_inbox_drain.drain(owner, today=date(2026, 11, 14), deadline_days=14)
    assert result["kept_for_person"] >= 1 and result["retried"] == 0
    return True


def windows_draft_review(target: Path, path: Path) -> bool:
    if sys.platform != "win32":
        return False
    from brigade import ingest, memory_inbox_drain
    from datetime import date

    assert handoff_origin.verify_review_draft(target, path, path.read_bytes()) is None
    result = memory_inbox_drain.drain(target, today=date(2026, 11, 14), deadline_days=14)
    assert result["kept_for_person"] == 1 and result["retried"] == 0
    assert result["decisions"][0]["reason"] == "trust-or-security:origin-review-required"
    assert path.exists()
    assert ingest.reprocess_review_draft(target, path).summary == "origin-review-required"
    return True


def trusted_handoff(path: Path, target: Path | None = None) -> Path:
    target = target or path.parent.parent.parent
    if receipt_signing.load_key(target) is None:
        receipt_signing.generate_key(target)
    assert handoff_origin.stamp_handoff(
        target, path, path.read_bytes(), handoff_origin.Origin("interactive", False, "claude", "test-host")
    )
    return path


def trusted_review_draft(target: Path, draft: Path, code: str, *, promote: bool = False, route: bool = True) -> Path:
    """Authorize a fixture's review decision, including a fresh review after refusal."""
    if receipt_signing.load_key(target) is None:
        receipt_signing.generate_key(target)
    assert handoff_origin.stamp_review_draft(
        target, draft, draft.read_bytes(), handoff_origin.ReviewDraft(code, promote, route)
    )
    return draft
