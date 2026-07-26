"""Typed route-decision artifact serialization for brigade run output."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, TypedDict

from . import localio
from . import roster as roster_mod
from .roster import Roster

ROUTE_DECISION_SCHEMA_VERSION = "brigade.route-decision.v1"


class RouteDecisionArtifact(TypedDict):
    schema_version: str
    chosen_route: list[str] | None
    confidence: str | None
    template_version: str | None
    admissible_seats: list[str]


def admissible_seats(roster: Roster) -> list[str]:
    """Sorted non-orchestrator worker seats from a validated roster."""
    return sorted(agent.name for agent in roster_mod.workers(roster))


def route_decision_payload(
    run_receipt: dict[str, Any],
    roster: Roster,
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
    return {
        "schema_version": ROUTE_DECISION_SCHEMA_VERSION,
        "chosen_route": chosen_route,
        "confidence": confidence,
        "template_version": template_version,
        "admissible_seats": admissible_seats(roster),
    }


def write_route_decision(
    output_dir: Path,
    roster: Roster,
) -> Path:
    run_receipt = localio.read_json_dict(output_dir / "run.json")
    if run_receipt is None:
        raise ValueError(f"missing or invalid run receipt: {output_dir / 'run.json'}")
    payload = route_decision_payload(run_receipt, roster)
    ordered: dict[str, object] = {}
    for key, value in (
        ("schema_version", payload["schema_version"]),
        ("chosen_route", payload["chosen_route"]),
        ("confidence", payload["confidence"]),
        ("template_version", payload["template_version"]),
        ("admissible_seats", payload["admissible_seats"]),
    ):
        ordered[key] = value
    path = output_dir / "route-decision.json"
    localio.write_text_atomic(path, json.dumps(ordered, indent=2) + "\n")
    return path
