"""``brigade fleet place``: advisory, read-only machine placement for a new child agent."""

from __future__ import annotations

import argparse
import math
from typing import Any, Mapping

from .. import fleet_client_policy
from .fleet_policy import _canonical_repo, _emit, _fail

DEFAULT_THRESHOLD = 0.70


def _threshold(raw: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("threshold must be a number in 0..1") from exc
    if not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise argparse.ArgumentTypeError("threshold must be a number in 0..1")
    return value


def register(fleet_sub: argparse._SubParsersAction) -> None:
    parser = fleet_sub.add_parser(
        "place",
        help="Advise which machine should run a new child agent for a repository (read only, never reserves).",
    )
    parser.add_argument("--repo", required=True, help="Canonical repository key, or a local checkout path.")
    parser.add_argument(
        "--threshold",
        type=_threshold,
        default=DEFAULT_THRESHOLD,
        help="Effective load at or over which a machine is shed, 0..1 (default 0.70).",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON.")
    parser.set_defaults(func=_dispatch_place)


def _line(payload: Mapping[str, Any]) -> str:
    recommended = payload.get("recommended")
    if isinstance(recommended, Mapping):
        return (
            f"place: {recommended['machine']} environment={recommended['environment_id']} "
            f"project={recommended['project_id']} reason={payload['reason']} (advisory)"
        )
    return f"place: none reason={payload.get('reason')} (advisory)"


def _dispatch_place(args: argparse.Namespace) -> int:
    try:
        payload = fleet_client_policy.place(repo=_canonical_repo(args.repo), threshold=args.threshold)
    except Exception as exc:
        return _fail(exc)
    _emit(payload, json_out=args.json, text=_line(payload))
    return 0 if payload.get("status") == "recommended" else 1
