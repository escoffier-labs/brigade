"""Validate read-only, lane-scoped browser readiness evidence."""

from __future__ import annotations

from collections.abc import Mapping


def project(raw: object, *, lane: str, requested_model: str | None = None) -> dict[str, object]:
    """Return a stable readiness projection without probing or mutating a browser."""
    evidence = dict(raw) if isinstance(raw, Mapping) else {}
    expected_model = requested_model.strip().casefold() if isinstance(requested_model, str) else ""
    blocker: str | None = None

    if not evidence:
        blocker = "missing-readiness-evidence"
    elif not _nonempty_string(evidence.get("browser_identity")):
        blocker = "ambiguous-browser-profile"
    elif evidence.get("lane") != lane:
        blocker = "wrong-lane"
    elif (
        not _nonempty_string(evidence.get("expected_owner"))
        or not _nonempty_string(evidence.get("owner"))
        or evidence.get("owner") != evidence.get("expected_owner")
    ):
        blocker = "ownership-conflict"
    elif evidence.get("login_status") == "verification-required":
        blocker = "human-verification-required"
    elif evidence.get("login_status") == "quota-exhausted":
        blocker = "provider-quota"
    elif evidence.get("login_status") != "authenticated":
        blocker = "browser-auth"
    elif evidence.get("mode") != "deep-research":
        blocker = "mode-unavailable"
    elif not _nonempty_string(evidence.get("model")) or str(evidence.get("model")).strip().casefold() == "unverified":
        blocker = "unknown-model"
    elif expected_model and str(evidence.get("model")).strip().casefold() != expected_model:
        blocker = "model-mismatch"
    elif evidence.get("collector_status") != "available":
        blocker = "missing-collector"
    elif evidence.get("status") not in (None, "ready", "resumable"):
        blocker = "unknown-readiness-state"
    elif evidence.get("status") == "resumable" and not _nonempty_string(evidence.get("session_ref")):
        blocker = "missing-session-reference"
    elif evidence.get("status") == "resumable" and not _nonempty_string(evidence.get("output_receipt")):
        blocker = "missing-output-receipt"

    status = "resumable" if evidence.get("status") == "resumable" else "ready"
    if blocker is not None:
        status = "unverified" if blocker == "missing-readiness-evidence" else "blocked"
    return {**evidence, "status": status, "blocker": blocker}


def _nonempty_string(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def ready(projection: Mapping[str, object]) -> bool:
    return projection.get("status") in {"ready", "resumable"} and projection.get("blocker") is None
