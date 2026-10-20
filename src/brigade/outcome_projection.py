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
        if (
            isinstance(artifact_id, str)
            and artifact_id.strip()
            and isinstance(new_status, str)
            and new_status.strip()
            and isinstance(created_at, str)
            and localio.parse_iso_datetime(created_at) is not None
        ):
            transitions.append(
                core.StatusTransition(
                    artifact_id,
                    new_status,
                    created_at,
                    route_policy=route_policy if isinstance(route_policy, dict) else None,
                )
            )
        else:
            skipped.append(path.name)
    return transitions, skipped


def load_status_projection_report(target: Path) -> dict[str, Any]:
    """Read projection inputs without treating an invalid status file as empty."""
    status_path = target / "memory" / "outcome" / "status.json"
    payload = localio.read_json_dict(status_path)
    artifacts = payload.get("artifacts") if payload is not None else None
    status_valid = not status_path.exists() or (
        isinstance(artifacts, dict) and all(isinstance(entry, dict) for entry in artifacts.values())
    )
    transitions, skipped = load_transition_report(target)
    decisions = status_path.parent / "decisions"
    return status_projection_report(
        transitions,
        artifacts if status_valid and isinstance(artifacts, dict) else {},
        decisions_exist=decisions.is_dir() and any(decisions.glob("*.json")),
        skipped_receipt_names=skipped,
        skipped_receipts=len(skipped),
        status_valid=status_valid,
    )


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
    status_valid: bool,
) -> dict[str, Any]:
    """Describe whether decision receipts reproduce the persisted status map."""
    rebuilt = core.fold_status(transitions)
    legacy = status_valid and bool(persisted) and not decisions_exist
    drift = [] if legacy else status_drift(rebuilt, persisted)
    return {
        "reproducible": status_valid and not drift and not legacy and skipped_receipts == 0,
        "status_valid": status_valid,
        "rebuilt_count": len(rebuilt),
        "persisted_count": len(persisted),
        "drift": drift,
        "drift_count": len(drift),
        "legacy": legacy,
        "skipped_receipts": skipped_receipts,
        "skipped_receipt_names": sorted(skipped_receipt_names)[:10],
    }
