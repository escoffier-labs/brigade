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
