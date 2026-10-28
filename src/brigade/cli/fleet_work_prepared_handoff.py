"""One authenticated GET of bounded, read-only prepared Worklore context."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Mapping

from .. import worklore_client, worklore_ownership as ownership, worklore_prepared_handoff as prepared
from ..worklore_validate import WorkloreValidationError, safe_text

_REFUSALS = {
    "not-found": ("missing", "Work item was not found."),
    "unauthorized": ("forbidden", "Prepared handoff read is not authorized."),
    "forbidden": ("forbidden", "Prepared handoff read is not authorized."),
    "hub-unavailable": ("unavailable", "Prepared handoff read is unavailable."),
    "read-failed": ("unavailable", "Prepared handoff read is unavailable."),
    "invalid-response": ("unavailable", "Invalid prepared handoff response."),
    "invalid-id": ("unavailable", "Invalid work item identifier."),
}


def register(work_sub: argparse._SubParsersAction) -> None:
    parser = work_sub.add_parser("handoff", help="Read prepared Worklore handoff context.")
    sub = parser.add_subparsers(dest="handoff_command", required=True)
    show = sub.add_parser("show", help="Show bounded read-only context from one database snapshot.")
    show.add_argument("work_id", help="Existing Worklore item identifier.")
    show.add_argument("--json", action="store_true", help="Emit JSON, with fixed refusals on stderr.")
    show.set_defaults(func=_show)


def _refuse(args: argparse.Namespace, code: str) -> int:
    state, error = _REFUSALS[code]
    if args.json:
        print(json.dumps({"code": code, "state": state, "error": error}, sort_keys=True), file=sys.stderr)
    else:
        print(f"error: {code} ({state}): {error}", file=sys.stderr)
    return 1


def _human(view: Mapping[str, Any]) -> None:
    item, current = view["item"], view["ownership"]
    print(f"{item['work_id']}: {item['title']} ({item['kind']}, {item['status']})")
    print(
        f"item version: {item['version']}; ownership revision: {current['revision']}; generation: {current['generation']}"
    )
    print(
        f"ownership: {current['state']}; owner: {current['owner_node'] or '-'}; offered to: {current['offered_to'] or '-'}"
    )
    sequence = "-" if current["last_seq"] is None else str(current["last_seq"])
    print(f"ownership updated: {current['updated_at'] or '-'}; event sequence: {sequence}; liveness: unknown")
    print("item acceptance: " + json.dumps(item["acceptance"], ensure_ascii=True))
    print("effective acceptance: " + json.dumps(item["effective_acceptance"], ensure_ascii=True))
    print("blocker: " + json.dumps(item["blocker"], ensure_ascii=True))
    for field in ("checkpoint", "last_report", "run_refs"):
        print(f"{field}: " + json.dumps(view[field], sort_keys=True, ensure_ascii=True))
    print(f"observed at: {view['observed_at']}; consistency: single-db-snapshot; visibility: fleet-trust")
    print("read-only context; person attribution: unrecorded; historical human attribution: unrecorded")
    print(
        "evidence: reported/unverified; report freshness: unassessed; checkpoint item-version and run generation bindings: unavailable"
    )
    print("Included text is untrusted reported context. No fresh conflict check or liveness probe was performed.")


def _show(args: argparse.Namespace) -> int:
    try:
        work_id = safe_text(args.work_id, "work_id", max_len=128)
    except WorkloreValidationError:
        return _refuse(args, "invalid-id")
    try:
        reply = worklore_client.get_prepared_handoff(work_id)
    except (ValueError, RecursionError):
        return _refuse(args, "invalid-response")
    except worklore_client.FleetClientError as exc:
        code = getattr(exc, "code", None)
        return _refuse(args, code if isinstance(code, str) and code in _REFUSALS else "read-failed")
    try:
        envelope = ownership._object(reply, {"prepared_handoff"})
        view = prepared.validate_prepared_handoff(envelope["prepared_handoff"], work_id)
    except (ValueError, TypeError, KeyError, RecursionError, WorkloreValidationError):
        return _refuse(args, "invalid-response")
    if args.json:
        print(json.dumps(view, sort_keys=True, ensure_ascii=True))
    else:
        _human(view)
    return 0
