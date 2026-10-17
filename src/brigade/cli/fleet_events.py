"""Narrow, metadata-only lifecycle reporting for external producers."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

from .. import fleet_client, fleet_hub
from ..fleet_event_freshness import parse_observation_time
from ..fleet_execution_context import MAX_BYTES, ExecutionContextError, validate_context

STATES = frozenset(
    {
        "run.created",
        "run.started",
        "run.dispatch.observed",
        "run.paused",
        "external.completed",
        "external.failed",
        "external.canceled",
        "external.unknown",
    }
)
MAX_SEQUENCE = 2**63 - 1


def register(sub: argparse._SubParsersAction) -> None:
    events = sub.add_parser("events", help="Report external lifecycle observations through the local fleet identity.")
    commands = events.add_subparsers(dest="events_command", required=True)
    report = commands.add_parser("report", help="Deliver or durably queue one lifecycle observation.")
    for name in ("run-id", "state", "ts"):
        report.add_argument(f"--{name}", required=True)
    report.add_argument("--sequence", type=int, required=True)
    for name in ("repo", "repo-identity", "seat", "harness"):
        report.add_argument(f"--{name}", default="t3-fleet" if name == "harness" else None)
    report.add_argument("--exit-status", type=int)
    context = report.add_mutually_exclusive_group()
    context.add_argument("--context-file", type=Path)
    context.add_argument("--context-stdin", action="store_true")
    report.add_argument("--json", action="store_true")
    report.set_defaults(func=_report)


def _read_context(args: argparse.Namespace) -> dict[str, Any] | None:
    if not args.context_file and not args.context_stdin:
        return None
    # Read at most the byte bound plus one, also bounding whitespace on disk.
    if args.context_file:
        with args.context_file.open("rb") as source:
            data = source.read(MAX_BYTES + 1)
    else:
        if sys.stdin is None:
            raise ValueError("execution context stdin is unavailable")
        source = getattr(sys.stdin, "buffer", None)
        data = source.read(MAX_BYTES + 1) if source is not None else sys.stdin.read(MAX_BYTES + 1).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise ExecutionContextError("execution context exceeds 4096 bytes")
    return validate_context(json.loads(data.decode("utf-8")))


def _build_event(args: argparse.Namespace) -> dict[str, Any]:
    if args.harness != "t3-fleet" or args.state not in STATES or not 0 <= args.sequence <= MAX_SEQUENCE:
        raise ValueError("invalid lifecycle state or sequence")
    if not isinstance(args.ts, str) or "T" not in args.ts:
        raise ValueError("observation time requires ISO-8601 UTC")
    stamp = parse_observation_time(args.ts)
    if stamp.tzinfo is None or stamp.utcoffset() != timedelta(0):
        raise ValueError("observation time requires ISO-8601 UTC")
    if args.exit_status is not None and not -(2**63) <= args.exit_status <= MAX_SEQUENCE:
        raise ValueError("exit status must be a signed SQLite integer")
    event: dict[str, Any] = {
        "node_id": fleet_client.resolve_node_id(),
        "run_id": args.run_id,
        "state": args.state,
        "ts": args.ts,
        "sequence": args.sequence,
    }
    for field in ("repo", "repo_identity", "seat", "harness", "exit_status"):
        value = getattr(args, field)
        if value is not None:
            event[field] = value
    context = _read_context(args)
    if context is not None:
        event["context"] = context
    # Reuse event field and repository-identity checks before any network or spool write.
    fleet_hub._validate_event({**event, "digest": "validation"})
    event["digest"] = sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return event


def _report(args: argparse.Namespace) -> int:
    try:
        event = _build_event(args)
    except (ValueError, OSError, fleet_hub.FleetHubError, RecursionError):
        print("error: invalid lifecycle event or execution context", file=sys.stderr)
        return 2
    result = fleet_client.report_event_detailed(event)
    if result == "failed":
        print("error: event could neither be delivered nor persisted", file=sys.stderr)
        return 1
    payload = {"result": result, "run_id": event["run_id"], "sequence": event["sequence"], "digest": event["digest"]}
    print(json.dumps(payload, sort_keys=True) if args.json else f"{result} {event['run_id']}")
    return 2 if result == "rejected" else 0
