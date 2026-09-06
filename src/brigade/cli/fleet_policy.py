"""brigade fleet policy command group (control-plane slice 2)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Mapping

from .. import fleet_client, fleet_client_policy, fleet_policy
from ..fleet_client_policy import FleetPolicyClientError


def register(fleet_sub: argparse._SubParsersAction) -> None:
    p_policy = fleet_sub.add_parser("policy", help="Read and apply the fleet control-plane policy document.")
    policy_sub = p_policy.add_subparsers(dest="policy_command", metavar="<policy-command>")
    policy_sub.required = True

    p_show = policy_sub.add_parser("show", help="Show the current hub policy revision.")
    p_show.add_argument("--json", action="store_true", help="Emit JSON.")
    p_show.set_defaults(func=_dispatch_show)

    p_delegation = policy_sub.add_parser("delegation", help="Create or show a Brigade-to-T3 fleet policy delegation.")
    delegation_sub = p_delegation.add_subparsers(dest="policy_delegation_command", metavar="<delegation-command>")
    delegation_sub.required = True

    p_del_create = delegation_sub.add_parser("create", help="Create a delegation for a routed decision.")
    p_del_create.add_argument("--decision-id", required=True, dest="decision_id")
    p_del_create.add_argument("--source-revision", required=True, dest="source_revision")
    p_del_create.add_argument("--parent-request-id", required=True, dest="parent_request_id")
    p_del_create.add_argument("--json", action="store_true", help="Emit JSON.")
    p_del_create.set_defaults(func=_dispatch_delegation_create)

    p_del_show = delegation_sub.add_parser("show", help="Show an existing delegation.")
    p_del_show.add_argument("--delegation-id", required=True, dest="delegation_id")
    p_del_show.add_argument("--json", action="store_true", help="Emit JSON.")
    p_del_show.set_defaults(func=_dispatch_delegation_show)

    p_history = policy_sub.add_parser("history", help="Show policy revision history.")
    p_history.add_argument("--json", action="store_true", help="Emit JSON.")
    p_history.set_defaults(func=_dispatch_history)

    p_preview = policy_sub.add_parser("preview", help="Dry-run a policy document save (admin token).")
    _add_document_flags(p_preview)
    p_preview.set_defaults(func=_dispatch_preview)

    p_save = policy_sub.add_parser("save", help="Save a policy document as a new revision (admin token).")
    _add_document_flags(p_save)
    p_save.set_defaults(func=_dispatch_save)

    p_rollback = policy_sub.add_parser("rollback", help="Restore an older document as a new revision (admin token).")
    p_rollback.add_argument("--revision", type=int, required=True, help="Revision to restore.")
    p_rollback.add_argument("--expected-version", type=int, required=True, dest="expected_version")
    p_rollback.add_argument("--reason", required=True, help="Why this rollback is being applied.")
    p_rollback.add_argument("--json", action="store_true", help="Emit JSON.")
    p_rollback.set_defaults(func=_dispatch_rollback)

    p_resolve = policy_sub.add_parser("resolve", help="Resolve effective policy for a consumer/repo/session.")
    p_resolve.add_argument("--consumer", required=True)
    p_resolve.add_argument("--repo", required=True)
    p_resolve.add_argument("--session-id", required=True, dest="session_id")
    p_resolve.add_argument("--origin", required=True)
    p_resolve.add_argument("--json", action="store_true", help="Emit JSON.")
    p_resolve.add_argument("--overrides-file", type=Path, default=None, dest="overrides_file")
    p_resolve.add_argument("--override-reason", default=None, dest="override_reason")
    p_resolve.add_argument("--decision-id", default=None, dest="decision_id")
    p_resolve.set_defaults(func=_dispatch_resolve)

    p_route = policy_sub.add_parser("route", help="Select a machine and seat under current routing policy.")
    p_route.add_argument("--consumer", required=True)
    p_route.add_argument("--repo", required=True)
    p_route.add_argument("--session-id", required=True, dest="session_id")
    p_route.add_argument("--origin", required=True)
    p_route.add_argument("--workload", required=True)
    p_route.add_argument("--machine", default=None)
    p_route.add_argument("--seat", default=None)
    p_route.add_argument("--override-reason", default=None, dest="override_reason")
    p_route.add_argument("--work-id", default=None, dest="work_id")
    p_route.add_argument("--decision-id", default=None, dest="decision_id")
    p_route.add_argument("--json", action="store_true", help="Emit JSON.")
    p_route.set_defaults(func=_dispatch_route)

    p_reservation = policy_sub.add_parser("reservation", help="Renew or release a routing reservation.")
    reservation_sub = p_reservation.add_subparsers(dest="policy_reservation_command", metavar="<reservation-command>")
    reservation_sub.required = True
    p_renew = reservation_sub.add_parser("renew", help="Renew a routing reservation.")
    p_renew.add_argument("--reservation-id", required=True, dest="reservation_id")
    p_renew.add_argument("--decision-id", required=True, dest="decision_id")
    p_renew.add_argument("--session-id", required=True, dest="session_id")
    p_renew.add_argument("--json", action="store_true", help="Emit JSON.")
    p_renew.set_defaults(func=_dispatch_reservation_renew)
    p_release = reservation_sub.add_parser("release", help="Release a routing reservation.")
    p_release.add_argument("--reservation-id", required=True, dest="reservation_id")
    p_release.add_argument("--decision-id", required=True, dest="decision_id")
    p_release.add_argument("--session-id", required=True, dest="session_id")
    p_release.add_argument("--json", action="store_true", help="Emit JSON.")
    p_release.set_defaults(func=_dispatch_reservation_release)

    p_observe = policy_sub.add_parser("observe", help="Publish one machine telemetry snapshot.")
    p_observe.add_argument("--file", type=Path, required=True, dest="file")
    p_observe.add_argument("--json", action="store_true", help="Emit JSON.")
    p_observe.set_defaults(func=_dispatch_observe)

    p_quota = policy_sub.add_parser("quota", help="Ingest bounded quota observations.")
    quota_sub = p_quota.add_subparsers(dest="policy_quota_command", metavar="<quota-command>")
    quota_sub.required = True
    p_quota_ingest = quota_sub.add_parser("ingest", help="Ingest a brigade.fleet_quota_observations.v1 document.")
    p_quota_ingest.add_argument("--file", type=Path, required=True, dest="file")
    p_quota_ingest.add_argument("--json", action="store_true", help="Emit JSON.")
    p_quota_ingest.set_defaults(func=_dispatch_quota_ingest)

    p_inventory = policy_sub.add_parser("inventory", help="Probe, ingest, or read fleet model inventory.")
    inventory_sub = p_inventory.add_subparsers(dest="policy_inventory_command", metavar="<inventory-command>")
    inventory_sub.required = True
    p_inventory_ingest = inventory_sub.add_parser("ingest", help="Ingest a brigade.fleet_model_inventory.v1 document.")
    p_inventory_ingest.add_argument("--file", type=Path, required=True, dest="file")
    p_inventory_ingest.add_argument("--json", action="store_true", help="Emit JSON.")
    p_inventory_ingest.set_defaults(func=_dispatch_inventory_ingest)
    p_inventory_status = inventory_sub.add_parser("status", help="Read the authenticated inventory projection.")
    p_inventory_status.add_argument("--json", action="store_true", help="Emit JSON.")
    p_inventory_status.set_defaults(func=_dispatch_inventory_status)
    p_inventory_probe = inventory_sub.add_parser(
        "probe", help="Run a bounded local CLI inventory probe. Does not publish unless --publish is set."
    )
    p_inventory_probe.add_argument("--harness", required=True)
    p_inventory_probe.add_argument("--provider", required=True)
    p_inventory_probe.add_argument("--account-id", default="", dest="account_id")
    p_inventory_probe.add_argument("--output", type=Path, default=None, dest="output")
    p_inventory_probe.add_argument(
        "--publish",
        action="store_true",
        help="POST the probe payload to the hub. Off by default; output is written locally.",
    )
    p_inventory_probe.add_argument("--json", action="store_true", help="Emit JSON.")
    p_inventory_probe.set_defaults(func=_dispatch_inventory_probe)

    p_status = policy_sub.add_parser("status", help="Read the authenticated control-plane status projection.")
    p_status.add_argument("--json", action="store_true", help="Emit JSON.")
    p_status.set_defaults(func=_dispatch_status)

    p_session = policy_sub.add_parser("session", help="Prepare or acknowledge a session policy receipt.")
    session_sub = p_session.add_subparsers(dest="policy_session_command", metavar="<session-command>")
    session_sub.required = True

    p_prepare = session_sub.add_parser("prepare", help="Resolve policy and select an exact consumer seat binding.")
    p_prepare.add_argument("--consumer", required=True)
    p_prepare.add_argument("--repo", required=True)
    p_prepare.add_argument("--session-id", required=True, dest="session_id")
    p_prepare.add_argument("--origin", required=True)
    p_prepare.add_argument("--provider", required=True)
    p_prepare.add_argument("--model", required=True)
    p_prepare.add_argument("--instance-id", required=True, dest="instance_id")
    p_prepare.add_argument("--reasoning", default=None)
    p_prepare.add_argument("--json", action="store_true", help="Emit JSON.")
    p_prepare.add_argument("--overrides-file", type=Path, default=None, dest="overrides_file")
    p_prepare.add_argument("--override-reason", default=None, dest="override_reason")
    p_prepare.add_argument("--decision-id", default=None, dest="decision_id")
    p_prepare.add_argument("--delegation-id", default=None, dest="delegation_id")
    p_prepare.set_defaults(func=_dispatch_prepare)

    p_ack = session_sub.add_parser("acknowledge", help="Record that a session loaded a prepared policy snapshot.")
    p_ack.add_argument("--session-id", required=True, dest="session_id")
    p_ack.add_argument("--consumer", required=True)
    p_ack.add_argument("--repo", required=True)
    p_ack.add_argument("--version", type=int, required=True)
    p_ack.add_argument("--digest", required=True)
    p_ack.add_argument("--status", choices=("applied", "failed"), default=None)
    p_ack.add_argument("--reason", default=None)
    p_ack.add_argument(
        "--context-hash",
        default=None,
        dest="context_hash",
        help="Exact prepared context hash (sha256:64 lowercase hex).",
    )
    p_ack.add_argument("--json", action="store_true", help="Emit JSON.")
    p_ack.set_defaults(func=_dispatch_acknowledge)


def _add_document_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--file", type=Path, required=True, dest="file")
    parser.add_argument("--expected-version", type=int, required=True, dest="expected_version")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--json", action="store_true", help="Emit JSON.")


def _emit(payload: Mapping[str, Any], *, json_out: bool, text: str | None = None) -> int:
    if json_out or text is None:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(text)
    return 0


def _fail(exc: BaseException) -> int:
    if isinstance(exc, FleetPolicyClientError):
        code, message = exc.code, str(exc)
    elif isinstance(exc, fleet_client.FleetClientError):
        text = str(exc)
        lowered = text.lower()
        if "401" in text or "403" in text or "unauthorized" in lowered:
            code = "auth-failed"
        elif "404" in text:
            code = "policy-unsupported"
        elif "409" in text or "conflict" in lowered:
            code = "revision-conflict"
        else:
            code = "network"
        message = text
    elif isinstance(exc, (OSError, ValueError, json.JSONDecodeError, fleet_policy.FleetPolicyError)):
        code, message = "invalid-request", str(exc)
    else:
        code, message = "invalid-request", str(exc)
    print(json.dumps({"error": {"code": code, "message": fleet_client_policy._scrub(message)}}, sort_keys=True))
    return 1 if code != "invalid-request" else 2


def _read_json_file(path: Path) -> dict[str, Any]:
    size = path.stat().st_size
    if size > fleet_policy.MAX_DOCUMENT_BYTES:
        raise fleet_policy.FleetPolicyError(
            f"fleet policy JSON must be at most {fleet_policy.MAX_DOCUMENT_BYTES} bytes"
        )
    return fleet_policy.parse_json_object(path.read_bytes())


def _canonical_repo(raw: str) -> str:
    from ..fleet_session_presence import repository_identity, validate_repo_identity

    path = Path(raw)
    if path.exists():
        identity = validate_repo_identity(repository_identity(path).value)
        if identity is None:
            raise fleet_policy.FleetPolicyError("repository identity is not a credential-free canonical id")
        return identity
    identity = validate_repo_identity(raw)
    if identity is None or fleet_policy.IDENTITY_PATTERN.match(identity) is None:
        raise fleet_policy.FleetPolicyError("repository identity is not a credential-free canonical id")
    return identity


def _overrides(args: argparse.Namespace) -> tuple[dict[str, Any] | None, str | None]:
    if args.overrides_file is None:
        return None, args.override_reason
    return _read_json_file(args.overrides_file), args.override_reason


def _dispatch_show(args: argparse.Namespace) -> int:
    try:
        payload = fleet_client_policy.show_policy()
    except Exception as exc:
        return _fail(exc)
    if args.json:
        return _emit(payload, json_out=True)
    return _emit(
        payload,
        json_out=False,
        text=f"policy {payload.get('schema')} version {payload.get('version')} digest {payload.get('digest')}",
    )


def _dispatch_history(args: argparse.Namespace) -> int:
    try:
        payload = fleet_client_policy.history_policy()
    except Exception as exc:
        return _fail(exc)
    if args.json:
        return _emit(payload, json_out=True)
    lines = [
        f"{row.get('revision')} {row.get('digest')} {row.get('actor')} {row.get('reason')}"
        for row in payload.get("revisions") or []
        if isinstance(row, dict)
    ]
    return _emit(payload, json_out=False, text="\n".join(lines) or "(no revisions)")


def _dispatch_preview(args: argparse.Namespace) -> int:
    try:
        document = _read_json_file(args.file)
        payload = fleet_client_policy.preview_policy(
            document, expected_version=args.expected_version, reason=args.reason
        )
    except Exception as exc:
        return _fail(exc)
    return _emit(payload, json_out=True)


def _dispatch_save(args: argparse.Namespace) -> int:
    try:
        document = _read_json_file(args.file)
        payload = fleet_client_policy.save_policy(document, expected_version=args.expected_version, reason=args.reason)
    except Exception as exc:
        return _fail(exc)
    return _emit(payload, json_out=True)


def _dispatch_rollback(args: argparse.Namespace) -> int:
    try:
        payload = fleet_client_policy.rollback_policy(
            revision=args.revision, expected_version=args.expected_version, reason=args.reason
        )
    except Exception as exc:
        return _fail(exc)
    return _emit(payload, json_out=True)


def _dispatch_resolve(args: argparse.Namespace) -> int:
    try:
        overrides, reason = _overrides(args)
        payload = fleet_client_policy.resolve_policy(
            consumer=args.consumer,
            repo=_canonical_repo(args.repo),
            session_id=args.session_id,
            origin=args.origin,
            overrides=overrides,
            override_reason=reason,
            decision_id=args.decision_id,
        )
    except Exception as exc:
        return _fail(exc)
    return _emit(payload, json_out=True)


def _dispatch_route(args: argparse.Namespace) -> int:
    try:
        payload = fleet_client_policy.route_work(
            consumer=args.consumer,
            repo=_canonical_repo(args.repo),
            session_id=args.session_id,
            origin=args.origin,
            workload=args.workload,
            machine=args.machine,
            seat=args.seat,
            override_reason=args.override_reason,
            work_id=args.work_id,
            decision_id=args.decision_id,
        )
    except Exception as exc:
        return _fail(exc)
    _emit(payload, json_out=True)
    return 0 if payload.get("selected") else 1


def _dispatch_reservation_renew(args: argparse.Namespace) -> int:
    try:
        payload = fleet_client_policy.renew_reservation(
            reservation_id=args.reservation_id,
            decision_id=args.decision_id,
            session_id=args.session_id,
        )
    except Exception as exc:
        return _fail(exc)
    return _emit(payload, json_out=True)


def _dispatch_reservation_release(args: argparse.Namespace) -> int:
    try:
        payload = fleet_client_policy.release_reservation(
            reservation_id=args.reservation_id,
            decision_id=args.decision_id,
            session_id=args.session_id,
        )
    except Exception as exc:
        return _fail(exc)
    return _emit(payload, json_out=True)


def _dispatch_observe(args: argparse.Namespace) -> int:
    try:
        body = _read_json_file(args.file)
        if "node_id" not in body:
            node_id = fleet_client.resolve_node_id()
            if not node_id or node_id == "unknown":
                raise fleet_policy.FleetPolicyError(
                    "local fleet node is not enrolled; cannot default telemetry node_id"
                )
            body["node_id"] = node_id
        payload = fleet_client_policy.observe_telemetry(body)
    except Exception as exc:
        return _fail(exc)
    return _emit(payload, json_out=True)


def _dispatch_quota_ingest(args: argparse.Namespace) -> int:
    try:
        payload = fleet_client_policy.ingest_quota(_read_json_file(args.file))
    except Exception as exc:
        return _fail(exc)
    return _emit(payload, json_out=True)


def _dispatch_inventory_ingest(args: argparse.Namespace) -> int:
    try:
        body = _read_json_file(args.file)
        admin = body.get("source") == "manual:browser" or body.get("evidence_type") == "manual_browser"
        payload = fleet_client_policy.ingest_inventory(body, admin=admin)
    except Exception as exc:
        return _fail(exc)
    return _emit(payload, json_out=True)


def _dispatch_inventory_status(args: argparse.Namespace) -> int:
    try:
        payload = fleet_client_policy.status_inventory()
    except Exception as exc:
        return _fail(exc)
    return _emit(payload, json_out=True)


def _dispatch_inventory_probe(args: argparse.Namespace) -> int:
    from .. import fleet_model_inventory

    try:
        payload = fleet_model_inventory.probe_cli_inventory(args.harness, provider=args.provider)
        if args.account_id:
            payload["account_id"] = args.account_id
        rendered = json.dumps(payload, indent=2, sort_keys=True)
        if args.output is not None:
            args.output.write_text(rendered + "\n", encoding="utf-8")
        elif not args.publish:
            print(rendered)
        if args.publish:
            published = fleet_client_policy.ingest_inventory(payload, admin=False)
            return _emit(published, json_out=True)
    except Exception as exc:
        return _fail(exc)
    return 0


def _dispatch_status(args: argparse.Namespace) -> int:
    try:
        payload = fleet_client_policy.status_policy()
    except Exception as exc:
        return _fail(exc)
    return _emit(payload, json_out=True)


def _dispatch_delegation_create(args: argparse.Namespace) -> int:
    try:
        payload = fleet_client_policy.create_delegation(
            decision_id=args.decision_id,
            source_revision=args.source_revision,
            parent_request_id=args.parent_request_id,
        )
    except Exception as exc:
        return _fail(exc)
    return _emit(payload, json_out=True)


def _dispatch_delegation_show(args: argparse.Namespace) -> int:
    try:
        payload = fleet_client_policy.show_delegation(
            delegation_id=args.delegation_id,
        )
    except Exception as exc:
        return _fail(exc)
    return _emit(payload, json_out=True)


def _dispatch_prepare(args: argparse.Namespace) -> int:
    try:
        overrides, reason = _overrides(args)
        payload = fleet_client_policy.prepare_session(
            consumer=args.consumer,
            repo=_canonical_repo(args.repo),
            session_id=args.session_id,
            origin=args.origin,
            provider=args.provider,
            model=args.model,
            instance_id=args.instance_id,
            reasoning=args.reasoning,
            overrides=overrides,
            override_reason=reason,
            decision_id=args.decision_id,
            delegation_id=args.delegation_id,
        )
    except Exception as exc:
        return _fail(exc)
    return _emit(payload, json_out=True)


def _dispatch_acknowledge(args: argparse.Namespace) -> int:
    try:
        payload = fleet_client_policy.acknowledge_session(
            session_id=args.session_id,
            consumer=args.consumer,
            repo=_canonical_repo(args.repo),
            version=args.version,
            digest=args.digest,
            status=args.status,
            reason=args.reason,
            context_hash=args.context_hash,
        )
    except Exception as exc:
        return _fail(exc)
    return _emit(payload, json_out=True)
