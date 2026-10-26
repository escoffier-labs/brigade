"""Read-only Hub claim cross-check using the fleet client's read paths."""

from __future__ import annotations

import time
from concurrent.futures import Future, wait
from datetime import datetime
from pathlib import Path
from queue import Empty, SimpleQueue
from threading import Thread
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


_INSPECT_WORKERS = 8
_TIMED_OUT = "Hub claim inspection timed out"


def _inspect_owned(owned: list[dict[str, Any]], node: str, deadline: float) -> dict[str, Any]:
    """Bound inspection concurrency without joining stalled transports at process exit."""
    jobs: SimpleQueue[tuple[str, Future[Any]]] = SimpleQueue()
    futures: dict[str, Future[Any]] = {}
    for row in owned:
        future: Future[Any] = Future()
        futures[row["target"]] = future
        jobs.put((row["target"], future))

    def inspect() -> None:
        while time.monotonic() < deadline:
            try:
                target, future = jobs.get_nowait()
            except Empty:
                return
            if not future.set_running_or_notify_cancel():
                continue
            if time.monotonic() >= deadline:
                future.set_exception(TimeoutError(_TIMED_OUT))
                return
            try:
                future.set_result(fleet_client.inspect_claim(target, node_id=node))
            except BaseException as exc:  # Match Future's exception handling for each inspection.
                future.set_exception(exc)

    try:
        for _ in range(min(_INSPECT_WORKERS, len(owned))):
            Thread(target=inspect, name="brigade-patrol", daemon=True).start()
        wait(futures.values(), timeout=max(0.0, deadline - time.monotonic()))
        return {
            target: future.result()
            for target, future in futures.items()
            if future.done() and future.exception() is None
        }
    finally:
        for future in futures.values():
            future.cancel()


def _snapshot(node: str, limit: int) -> tuple[list[tuple[dict[str, Any], str | None, str | None]], bool]:
    """Fetch claims, then inspect this node's rows concurrently under one hard deadline.

    A slow inspection marks only its own row unavailable. Rows that finished keep
    their observation, so one latent claim cannot discard the rest.
    """
    started = time.monotonic()
    total = fleet_client.REPORT_TIMEOUT_SECONDS
    rows = fleet_client._run_with_deadline(fleet_client.fetch_claims, timeout=total)
    # Reserve the bounded window for inspectable local claims before display/error rows.
    owned: list[dict[str, Any]] = []
    others: list[Any] = []
    for row in rows:
        bucket = (
            owned
            if isinstance(row, dict) and isinstance(row.get("target"), str) and row.get("owner_node") == node
            else others
        )
        if len(bucket) < limit:
            bucket.append(row)
    window = owned + others[: limit - len(owned)]
    decisions = _inspect_owned(owned, node, started + total) if owned else {}
    observations: list[tuple[dict[str, Any], str | None, str | None]] = []
    for row in window:
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
            decision = decisions.get(row["target"])
            if decision is None:
                error = _TIMED_OUT
            elif not decision.granted or not decision.claim or decision.claim.get("owner_node") != node:
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
        observations, truncated = _snapshot(node, limit)
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
