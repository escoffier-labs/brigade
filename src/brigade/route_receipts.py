"""Typed route-decision artifact serialization for brigade run output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

from . import localio
from . import roster as roster_mod
from .roster import Roster

ROUTE_DECISION_SCHEMA_VERSION = "brigade.route-decision.v1"

_POLICY_EXTENSION_KEYS = (
    "decided_at",
    "policy_version",
    "score_inputs",
    "skill_assignments",
    "exploration",
)


class RouteDecisionArtifact(TypedDict, total=False):
    schema_version: str
    chosen_route: list[str] | None
    confidence: str | None
    template_version: str | None
    admissible_seats: list[str]
    decided_at: str
    policy_version: str
    score_inputs: dict[str, Any]
    skill_assignments: list[dict[str, Any]]
    exploration: dict[str, Any]


def _apply_policy_extensions(
    payload: RouteDecisionArtifact,
    extensions: dict[str, Any],
) -> None:
    if "decided_at" in extensions:
        payload["decided_at"] = extensions["decided_at"]
    if "policy_version" in extensions:
        payload["policy_version"] = extensions["policy_version"]
    if "score_inputs" in extensions:
        payload["score_inputs"] = extensions["score_inputs"]
    if "skill_assignments" in extensions:
        payload["skill_assignments"] = extensions["skill_assignments"]
    if "exploration" in extensions:
        payload["exploration"] = extensions["exploration"]


def admissible_seats(roster: Roster) -> list[str]:
    """Sorted non-orchestrator worker seats from a validated roster."""
    return sorted(agent.name for agent in roster_mod.workers(roster))


def route_decision_payload(
    run_receipt: dict[str, Any],
    roster: Roster,
    *,
    target: Path | None = None,
    runs_dir: Path | None = None,
    exclude_decision_path: Path | None = None,
    policy_extensions: dict[str, Any] | None = None,
) -> RouteDecisionArtifact:
    route = run_receipt.get("route")
    if isinstance(route, dict) and route.get("attached"):
        raw_route = route.get("route")
        if isinstance(raw_route, list):
            chosen_route = [str(stage) for stage in raw_route]
            raw_confidence = route.get("confidence")
            confidence = str(raw_confidence) if raw_confidence is not None else None
            raw_template_version = route.get("template_version")
            template_version = str(raw_template_version) if raw_template_version is not None else None
        else:
            chosen_route = None
            confidence = None
            template_version = None
    else:
        chosen_route = None
        confidence = None
        template_version = None
    payload: RouteDecisionArtifact = {
        "schema_version": ROUTE_DECISION_SCHEMA_VERSION,
        "chosen_route": chosen_route,
        "confidence": confidence,
        "template_version": template_version,
        "admissible_seats": admissible_seats(roster),
    }
    if policy_extensions:
        _apply_policy_extensions(payload, policy_extensions)
    elif target is not None:
        from .route_policy import route_policy_payload

        budget = _route_budget_from_run_receipt(run_receipt)
        extensions = route_policy_payload(
            target,
            run_receipt,
            budget=budget,
            runs_dir=runs_dir,
            exclude_decision_path=exclude_decision_path,
        )
        _apply_policy_extensions(payload, extensions)
    return payload


def _route_budget_from_run_receipt(run_receipt: dict[str, Any]):
    route = run_receipt.get("route")
    if not isinstance(route, dict):
        return None
    raw = route.get("routing_budget")
    if not isinstance(raw, dict):
        return None
    token_budget = raw.get("token_budget")
    work_budget = raw.get("work_budget")
    token_spent = raw.get("token_spent", 0)
    work_spent = raw.get("work_spent", 0)
    from .route_policy import RouteBudget

    return RouteBudget(
        token_budget=int(token_budget) if isinstance(token_budget, int) else None,
        work_budget=int(work_budget) if isinstance(work_budget, int) else None,
        token_spent=int(token_spent) if isinstance(token_spent, int) else 0,
        work_spent=int(work_spent) if isinstance(work_spent, int) else 0,
    )


def _existing_policy_extensions(decision_path: Path) -> dict[str, Any] | None:
    existing = localio.read_json_dict(decision_path)
    if not isinstance(existing, dict) or "policy_version" not in existing:
        return None
    extensions: dict[str, Any] = {}
    for key in _POLICY_EXTENSION_KEYS:
        if key in existing:
            extensions[key] = existing[key]
    return extensions or None


def write_route_decision(
    output_dir: Path,
    roster: Roster,
    *,
    target: Path | None = None,
    runs_dir: Path | None = None,
    policy_extensions: dict[str, Any] | None = None,
) -> Path:
    run_receipt = localio.read_json_dict(output_dir / "run.json")
    if run_receipt is None:
        raise ValueError(f"missing or invalid run receipt: {output_dir / 'run.json'}")
    decision_path = output_dir / "route-decision.json"
    preserved = policy_extensions or _existing_policy_extensions(decision_path)
    payload = route_decision_payload(
        run_receipt,
        roster,
        target=target,
        runs_dir=runs_dir,
        exclude_decision_path=decision_path,
        policy_extensions=preserved,
    )
    ordered: dict[str, object] = {}
    for key in (
        "schema_version",
        "chosen_route",
        "confidence",
        "template_version",
        "admissible_seats",
        "decided_at",
        "policy_version",
        "score_inputs",
        "skill_assignments",
        "exploration",
    ):
        if key in payload:
            ordered[key] = payload[key]
    localio.write_text_atomic(decision_path, json.dumps(ordered, indent=2) + "\n")
    return decision_path
