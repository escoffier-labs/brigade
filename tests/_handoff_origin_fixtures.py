"""Trusted local origin for fixtures that exercise filing after admission."""

from pathlib import Path

from brigade import handoff_origin, receipt_signing


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
