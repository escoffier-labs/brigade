"""Validate read-only, lane-scoped browser readiness evidence."""

from __future__ import annotations

from collections.abc import Mapping


def project(raw: object, *, lane: str) -> dict[str, object]:
    """Return a stable readiness projection without probing or mutating a browser."""
    evidence = dict(raw) if isinstance(raw, Mapping) else {}
    blocker: str | None = None

    if not evidence:
        blocker = "missing-readiness-evidence"
    elif not str(evidence.get("browser_identity") or "").strip():
        blocker = "ambiguous-browser-profile"
    elif evidence.get("lane") != lane:
        blocker = "wrong-lane"
    elif not evidence.get("expected_owner") or evidence.get("owner") != evidence.get("expected_owner"):
        blocker = "ownership-conflict"
    elif evidence.get("login_status") == "verification-required":
        blocker = "human-verification-required"
    elif evidence.get("login_status") == "quota-exhausted":
        blocker = "provider-quota"
    elif evidence.get("login_status") != "authenticated":
        blocker = "browser-auth"
    elif evidence.get("mode") != "deep-research":
        blocker = "mode-unavailable"
    elif not str(evidence.get("model") or "").strip() or evidence.get("model") == "unverified":
        blocker = "unknown-model"
    elif evidence.get("collector_status") != "available":
        blocker = "missing-collector"
    elif evidence.get("status") == "resumable" and not evidence.get("session_ref"):
        blocker = "missing-session-reference"

    status = "resumable" if evidence.get("status") == "resumable" else "ready"
    if blocker is not None:
        status = "unverified" if blocker == "missing-readiness-evidence" else "blocked"
    return {**evidence, "status": status, "blocker": blocker}


def ready(projection: Mapping[str, object]) -> bool:
    return projection.get("status") in {"ready", "resumable"} and projection.get("blocker") is None
