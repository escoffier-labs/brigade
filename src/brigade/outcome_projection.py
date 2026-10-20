"""Pure status projection reports for the outcome decision receipt log."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import localio, outcome as core


def load_transition_report(target: Path) -> tuple[list[core.StatusTransition], list[str]]:
    """Load valid decision transitions and the names of malformed receipts."""
    decisions = target / "memory" / "outcome" / "decisions"
    if not decisions.is_dir():
        return [], []
    transitions: list[core.StatusTransition] = []
    skipped: list[str] = []
    for path in sorted(decisions.glob("*.json")):
        payload = localio.read_json_dict(path) or {}
        artifact_id = payload.get("artifact_id")
        new_status = payload.get("new_status")
        created_at = payload.get("created_at")
        route_policy = payload.get("route_policy")
        if artifact_id and new_status and created_at:
            transitions.append(
                core.StatusTransition(
                    str(artifact_id),
                    str(new_status),
                    str(created_at),
                    route_policy=route_policy if isinstance(route_policy, dict) else None,
                )
            )
        else:
            skipped.append(path.name)
    return transitions, skipped


def status_drift(rebuilt: dict[str, dict], persisted: dict[str, dict]) -> list[dict[str, Any]]:
    """Return per-artifact differences between a fold and persisted status."""
    drift: list[dict[str, Any]] = []
    for artifact_id in sorted(set(rebuilt) | set(persisted)):
        want = rebuilt.get(artifact_id)
        have = persisted.get(artifact_id)
        if want == have:
            continue
        if want is None:
            drift.append({"artifact_id": artifact_id, "issue": "only-in-status", "persisted": have})
        elif have is None:
            drift.append({"artifact_id": artifact_id, "issue": "missing-from-status", "rebuilt": want})
        else:
            drift.append({"artifact_id": artifact_id, "issue": "mismatch", "rebuilt": want, "persisted": have})
    return drift


def status_drift_lines(drift: list[dict[str, Any]]) -> list[str]:
    """Format at most ten drift entries with the remaining count for doctor."""
    lines = [f"- {item['artifact_id']} [{item['issue']}]" for item in drift[:10]]
    if len(drift) > 10:
        lines.append(f"... and {len(drift) - 10} more")
    return lines


def status_projection_report(
    transitions: list[Any],
    persisted: dict[str, dict],
    *,
    decisions_exist: bool,
    skipped_receipt_names: list[str],
    skipped_receipts: int,
) -> dict[str, Any]:
    """Describe whether decision receipts reproduce the persisted status map."""
    rebuilt = core.fold_status(transitions)
    legacy = bool(persisted) and not decisions_exist
    drift = [] if legacy else status_drift(rebuilt, persisted)
    return {
        "reproducible": not drift and not legacy and skipped_receipts == 0,
        "rebuilt_count": len(rebuilt),
        "persisted_count": len(persisted),
        "drift": drift,
        "drift_count": len(drift),
        "legacy": legacy,
        "skipped_receipts": skipped_receipts,
        "skipped_receipt_names": sorted(skipped_receipt_names)[:10],
    }
