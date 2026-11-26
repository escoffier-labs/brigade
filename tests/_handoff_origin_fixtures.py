"""Trusted local origin for fixtures that exercise filing after admission."""

import sys
from pathlib import Path

import pytest

from brigade import handoff_origin, receipt_signing

requires_automatic_filing = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX filing fixtures and batch tests",
)


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
