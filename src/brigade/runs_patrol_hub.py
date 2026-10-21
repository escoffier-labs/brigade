"""Read-only Hub claim cross-check using the fleet client's read paths."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from . import fleet_client, run_reap
from .run_lane_state import Evidence, Lane, Lanes


def _safe_claim(row: dict[str, Any]) -> dict[str, Any]:
    # Never copy holder tokens or lock fences into a report or receipt.
    return {
        key: row[key]
        for key in ("target", "owner_node", "acquired_at", "renewed_at", "expires_at", "generation")
        if key in row
    }


def _snapshot(node: str, limit: int) -> tuple[list[tuple[dict[str, Any], str | None, str | None]], bool]:
    rows = fleet_client.fetch_claims()
    observations: list[tuple[dict[str, Any], str | None, str | None]] = []
    for row in rows[:limit]:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("target"), str)
            or not isinstance(row.get("owner_node"), str)
        ):
            observations.append(({}, None, "malformed Hub claim"))
            continue
        path = None
        error = None
        if row["owner_node"] == node:
            decision = fleet_client.inspect_claim(row["target"], node_id=node)
            if not decision.granted or not decision.claim or decision.claim.get("owner_node") != node:
                error = "Hub claim changed or inspection unavailable"
            else:
                row = decision.claim
                path = decision.lock_run_dir
        observations.append((_safe_claim(row), path, error))
    return observations, len(rows) > limit


def cross_check(lanes: Lanes, *, target: Path, now: datetime, limit: int, scan_incomplete: bool) -> None:
    """Enrolled nodes only. Failure adds an unknown observation, never a write."""
    settings = fleet_client.load_fleet_settings()
    if not settings.get("hub_url") or not settings.get("node_token"):
        return
    node = fleet_client.resolve_node_id()
    try:
        if node in {"", "unknown"}:
            raise ValueError("node identity unavailable")
        observations, truncated = fleet_client._run_with_deadline(
            lambda: _snapshot(node, limit), timeout=fleet_client.REPORT_TIMEOUT_SECONDS
        )
    except (fleet_client.FleetClientError, OSError, ValueError, RuntimeError):
        lanes.append(
            Lane("hub", errors=["Hub claims unavailable"], evidence=[Evidence("fleet:claims", now, "unavailable")])
        )
        return
    lanes.truncated |= truncated
    by_path = {lane.run_dir: lane for lane in lanes if lane.run_dir is not None}
    root = target / ".brigade/runs"
    for index, (claim, raw_path, error) in enumerate(observations):
        lane = None
        path = None
        if raw_path:
            candidate = Path(raw_path)
            # A basename is not a join key. Never probe remote or unrelated paths.
            if (
                candidate.is_absolute()
                and candidate.parent == root
                and run_reap._classify_child(candidate, root) is None
            ):
                path = candidate
                lane = by_path.get(path)
        if lane is None:
            lane = Lane(f"hub:{claim.get('owner_node', 'unknown')}:{claim.get('target', index)}")
            lanes.append(lane)
            if claim.get("owner_node") != node and claim:
                lane.display_only = True
            elif error:
                lane.errors.append(error)
            elif path is not None and not path.exists() and not scan_incomplete:
                lane.hub_missing_run = True
            else:
                lane.errors.append("no verified local lane for Hub claim")
        lane.hub_claim = claim
        lane.evidence.append(
            Evidence("fleet:claims", now, "display only" if lane.display_only else "this node's claim")
        )
