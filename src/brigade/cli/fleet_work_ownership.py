"""Bounded read-only Worklore ownership view; no combined atomic revision."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from typing import Any, Mapping

from .. import fleet_dot, worklore_client, worklore_ownership as ownership
from ..worklore_validate import KINDS, TRANSITIONS, WorkloreValidationError, safe_text

_REPORT_FIELDS = frozenset(
    {
        "version",
        "provider",
        "session_id",
        "agent_label",
        "parent_session_id",
        "repo_identity",
        "source",
        "source_scope",
        "coverage",
        "observed_at",
        "sequence",
        "task",
        "progress",
        "blocker",
        "result",
        "evidence_refs",
    }
)
_REFUSALS = {
    "not-found": ("missing", "Work item was not found."),
    "hub-unavailable": ("unavailable", "Ownership read is unavailable."),
    "version-conflict": ("stale", "Ownership read was refused as stale."),
    "stale-generation": ("stale", "Ownership read was refused as stale."),
    "invalid-response": ("unavailable", "Invalid ownership read response."),
    "invalid-id": ("unavailable", "Invalid work item identifier."),
    "read-failed": ("unavailable", "Ownership read is unavailable."),
}


def register(work_sub: argparse._SubParsersAction) -> None:
    parser = work_sub.add_parser("ownership", help="Read existing Worklore ownership metadata.")
    sub = parser.add_subparsers(dest="ownership_command", required=True)
    show = sub.add_parser("show", help="Show bounded item and ownership projections from separate GETs.")
    show.add_argument("work_id", help="Existing Worklore item identifier.")
    show.add_argument("--json", action="store_true", help="Emit JSON; refusals are JSON on stderr.")
    show.set_defaults(func=_show)


def _object(raw: object) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise ValueError("invalid object")
    return raw


def _only(raw: object, fields: set[str] | frozenset[str]) -> dict[str, Any]:
    value = _object(raw)
    return {key: value[key] for key in fields if key in value}


def _counter(raw: object) -> int:
    return ownership._integer(raw)


def _choice(raw: object, allowed: set[str] | frozenset[str]) -> str:
    if not isinstance(raw, str) or raw not in allowed:
        raise ValueError("invalid choice")
    return raw


def _timestamp(raw: object) -> str | None:
    if raw is None:
        return None
    text = safe_text(raw, "timestamp", max_len=64)
    stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return text


def _node(raw: object) -> str | None:
    return None if raw is None else ownership._node(raw)


def _checkpoint(raw: Mapping[str, Any]) -> dict[str, Any]:
    if raw.get("repo_identity") is None:
        return {"state": "unobserved"}
    fields = {"repo_identity", "write_scope", "source_revision"}
    body = _only(raw, fields)
    body["next_action"] = _only(raw.get("next_action"), {"kind", "resume_condition"})
    evidence = ownership._array(raw.get("evidence_refs"), ownership.EVIDENCE_MAX_ITEMS)
    body["evidence_refs"] = [_only(record, {"kind", "ref", "source_revision"}) for record in evidence]
    # Reuse the protocol's canonical relative paths, bounds, enums and references.
    parsed = ownership._parse(body | {"action": "checkpoint", "generation": raw["generation"]})
    return {"state": "observed", **{key: value for key, value in parsed.items() if key not in {"action", "generation"}}}


def _report(raw: object) -> dict[str, Any]:
    if raw is None:
        return {"state": "unobserved"}
    source = _object(raw)
    normalized = fleet_dot.validate_ownership_report(_only(source, _REPORT_FIELDS))
    return {
        **normalized,
        "observed_at": _timestamp(source.get("observed_at")),
        "reporter_node": _node(source.get("reporter_node")),
        "state": "observed",
        "freshness": "unassessed",
    }


def _projection(work_id: str, item_reply: object, ownership_reply: object) -> dict[str, Any]:
    item = _object(_object(item_reply).get("item"))
    raw = _object(_object(ownership_reply).get("ownership"))
    identifier = safe_text(item.get("work_id"), "work_id", max_len=128)
    if identifier != work_id:
        raise ValueError("wrong work item")
    item_view = {
        "work_id": identifier,
        "title": safe_text(item.get("title"), "title", max_len=240),
        "kind": _choice(item.get("kind"), KINDS),
        "status": _choice(item.get("status"), frozenset(TRANSITIONS)),
        "version": _counter(item.get("version")),
    }
    view = {
        "state": _choice(raw.get("state"), {"unowned", "offered", "owned", "handoff-pending"}),
        "revision": _counter(raw.get("revision")),
        "generation": _counter(raw.get("generation")),
        "owner_node": _node(raw.get("owner_node")),
        "offered_to": _node(raw.get("offered_to")),
        "last_seq": None if raw.get("last_seq") is None else _counter(raw["last_seq"]),
        "updated_at": _timestamp(raw.get("updated_at")),
        "checkpoint": _checkpoint(raw),
        "report": _report(raw.get("last_report")),
    }
    return {
        "item": item_view,
        "ownership": view,
        "consistency": "separate-reads",
        "liveness": "unknown",
        "conflict_check": "not-performed",
    }


def _refuse(args: argparse.Namespace, code: str) -> int:
    state, error = _REFUSALS[code]
    if args.json:
        print(json.dumps({"code": code, "state": state, "error": error}, sort_keys=True), file=sys.stderr)
    else:
        print(f"error: {code} ({state}): {error}", file=sys.stderr)
    return 1


def _human(payload: Mapping[str, Any]) -> None:
    item, view = payload["item"], payload["ownership"]
    print(f"{item['work_id']}: {item['title']} ({item['kind']}, {item['status']})")
    print(f"item version: {item['version']}; ownership revision: {view['revision']}; generation: {view['generation']}")
    print(f"ownership: {view['state']}; owner: {view['owner_node'] or '-'}; offered to: {view['offered_to'] or '-'}")
    print(f"ownership updated: {view['updated_at'] or 'unobserved'}; event sequence: {view['last_seq']}")
    checkpoint = view["checkpoint"]
    print(f"checkpoint: {checkpoint['state']}")
    if checkpoint["state"] == "observed":
        print(f"repository: {checkpoint['repo_identity']}; source revision: {checkpoint['source_revision']}")
        print(f"write scope: {', '.join(checkpoint['write_scope']) or '(empty)'}")
        action = checkpoint["next_action"]
        print(f"next action: {action['kind']}; resume condition: {action['resume_condition'] or '(empty)'}")
        print("checkpoint evidence (reported): " + json.dumps(checkpoint["evidence_refs"], ensure_ascii=True))
    report = view["report"]
    print(f"report: {report['state']}")
    if report["state"] == "observed":
        print(json.dumps(report, sort_keys=True, ensure_ascii=True))
    print(f"reads completed: item={payload['observations']['item']}; ownership={payload['observations']['ownership']}")
    print("consistency: separate-reads; liveness: unknown; conflict_check: not-performed")


def _show(args: argparse.Namespace) -> int:
    try:
        work_id = safe_text(args.work_id, "work_id", max_len=128)
    except WorkloreValidationError:
        return _refuse(args, "invalid-id")
    try:
        item_reply = worklore_client.get_item(work_id)
        item_observed = datetime.now(timezone.utc).isoformat()
        ownership_reply = worklore_client.get_ownership(work_id)
        ownership_observed = datetime.now(timezone.utc).isoformat()
    except (ValueError, RecursionError):
        return _refuse(args, "invalid-response")
    except worklore_client.FleetClientError as exc:
        # The existing client can carry an untrusted refusal body. Never echo it.
        code = getattr(exc, "code", None)
        return _refuse(args, code if isinstance(code, str) and code in _REFUSALS else "read-failed")
    try:
        payload = _projection(work_id, item_reply, ownership_reply)
    except (ValueError, TypeError, KeyError, WorkloreValidationError, fleet_dot.DotReportError):
        return _refuse(args, "invalid-response")
    payload["observations"] = {"item": item_observed, "ownership": ownership_observed}
    if args.json:
        print(json.dumps(payload, sort_keys=True, ensure_ascii=True))
    else:
        _human(payload)
    return 0
