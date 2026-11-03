"""Bearer JSON API routes on the Fleet Hub.

The node and admin reads (``GET /status`` and friends), the ``PUT
/preference`` write, and the per-path checks and dispatch behind the JSON
``POST`` routes. ``fleet_hub_http`` keeps the ``POST`` error ladder and the
body read between :func:`admit_post` and :func:`post`, so an unauthenticated
or refused caller never gets the hub to read its body.
"""

from __future__ import annotations

import sqlite3
from typing import Any
from urllib.parse import parse_qs

from . import fleet_claim_display, fleet_hub as _hub, fleet_hub_grokbot, fleet_hub_identity, fleet_hub_messages
from . import fleet_hub_model_roster, fleet_hub_policy_api, fleet_hub_sessions, fleet_model_inventory, fleet_owners
from .fleet_hub import FleetHubError, FleetHubUnsupportedSchema
from .fleet_hub_claims import ClaimFenceError, status as claim_fencing_status
from .fleet_hub_routes import HubContext, RouteTable

GET_PATHS = (
    "/status",
    "/claims",
    "/nodes",
    "/cloud",
    "/models",
    "/preference",
    "/sessions",
    "/owners",
    "/policy",
    "/policy/status",
    "/policy/inventory",
)
POST_PATHS = (
    "/events",
    "/claims",
    "/nodes",
    "/cloud",
    "/models",
    "/grokbot",
    "/sessions",
    "/policy",
    "/messages",
    "/message-principals",
    "/mesh/identity",
    "/mesh/statements",
)
_POLICY_PATHS = frozenset({"/policy", "/policy/status", "/policy/inventory"})
_ADMIN_POST_REFUSALS = {
    "/mesh/identity": "the admin token is required for mesh uploads",
    "/mesh/statements": "the admin token is required for mesh uploads",
    "/nodes": "the admin token is required to manage nodes",
    "/message-principals": "the admin token is required to enroll message principals",
}
_ADMIN_WRITE_HINT = (
    "enroll this node with 'brigade fleet nodes add' and configure its node token, or start the hub with "
    "--allow-admin-writes"
)
_GROKBOT_ADMIN_READS = frozenset({"list", "status", "report-metadata", "enroll-actor"})


def register_routes(routes: RouteTable, hub: HubContext) -> None:
    """Add the JSON reads and ``PUT /preference``; ``fleet_hub_http`` adds ``POST``."""
    routes.add("GET", lambda request, path, query: serve_get(request, hub, path, query), paths=GET_PATHS)
    routes.add("PUT", lambda request, _path, _query: put_preference(request, hub), paths=("/preference",))


def refuse_missing_bearer(request: Any, path: str) -> bool:
    """Send the 401 for a request without a bearer and return ``True``."""
    if request._bearer() is not None:
        return False
    if path in _POLICY_PATHS:
        request._send_json(401, {"error": {"code": "auth-failed", "message": "unauthorized"}})
    else:
        request._send_json(401, {"error": "unauthorized"})
    return True


def open_conn(request: Any, hub: HubContext) -> sqlite3.Connection | None:
    """A non-migrating hub connection, or ``None`` after sending the 503 or 500."""
    try:
        return hub.open_db(hub.db_path)
    except FleetHubUnsupportedSchema:
        request._send_json(503, {"error": FleetHubUnsupportedSchema.public_message})
    except (FleetHubError, sqlite3.Error) as exc:
        request._send_json(500, {"error": f"hub database error: {exc}"})
    return None


def serve_get(request: Any, hub: HubContext, path: str, query: str) -> None:
    if refuse_missing_bearer(request, path):
        return
    include_all = parse_qs(query).get("all", [""])[0].lower() in ("1", "true", "yes")
    conn = open_conn(request, hub)
    if conn is None:
        return
    payload: dict[str, Any]
    try:
        caller = request._caller(conn)
        if caller is None:
            return
        is_admin, _node = caller
        presented = request._bearer()
        if path == "/nodes":
            if not is_admin:
                request._send_json(403, {"error": "the admin token is required to manage nodes"})
                return
            payload = {"nodes": _hub.list_nodes(conn)}
        elif path == "/status":
            payload = {
                "claim_fencing": claim_fencing_status(conn),
                "runs": _hub.latest_status(
                    conn,
                    include_all=include_all,
                    stale_history_after_seconds=hub.deck.stale_history_after_seconds,
                ),
            }
        elif path == "/cloud":
            if parse_qs(query).get("view", [""])[0] == "capabilities":
                # Fixed-size preflight, independent of leases and provider policy rows.
                payload = {"schema": "brigade.fleet_cloud.v1"}
            else:
                payload = _hub.cloud_snapshot(conn, hub.deck, include_all=include_all, include_grokbot=is_admin)
        elif path == "/models":
            payload = fleet_hub_model_roster.project_roster(
                conn,
                audience_node_id=None if is_admin else _node,
                raw_node_bearer=None if is_admin else presented,
            )
        elif path == "/preference":
            pref = _hub.get_run_preference(conn)
            payload = {"preference": {key: value for key, value in pref.items() if value is not None}}
        elif path == "/sessions":
            payload = {"sessions": fleet_hub_sessions.list_sessions(conn, include_all=include_all)}
        elif path == "/owners":
            conn.execute("PRAGMA query_only=ON")
            view = fleet_owners.load_view(conn, now_epoch=_hub._now_epoch(), include_grokbot=is_admin)
            payload = fleet_owners.view_payload(view)
        elif path == "/policy/status":
            payload = fleet_hub_policy_api.read_status(conn)
        elif path == "/policy/inventory":
            fleet_model_inventory.ensure_schema(conn)
            payload = fleet_hub_policy_api.read_inventory(conn)
        elif path == "/policy":
            params = parse_qs(query)
            history = params.get("history", [""])[0].lower() in ("1", "true", "yes")
            limit_raw = params.get("limit", ["50"])[0]
            try:
                limit = int(limit_raw)
            except ValueError:
                request._send_json(
                    400,
                    fleet_hub_policy_api.FleetPolicyApiError("invalid-request", "limit must be an integer").payload(),
                )
                return
            payload = fleet_hub_policy_api.read_policy(conn, history=history, limit=limit)
        else:
            payload = {"claims": fleet_claim_display.list_display_claims(conn, include_all=include_all)}
    except ClaimFenceError as exc:
        request._send_json(503, {"error": str(exc), "reason": exc.reason})
        return
    except sqlite3.Error as exc:
        request._send_json(500, {"error": f"hub database error: {exc}"})
        return
    finally:
        conn.close()
    request._send_json(200, payload)


def put_preference(request: Any, hub: HubContext) -> None:
    if request._bearer() is None:
        request._send_json(401, {"error": "unauthorized"})
        return
    conn = open_conn(request, hub)
    if conn is None:
        return
    try:
        caller = request._caller(conn)
        if caller is None:
            return
        is_admin, _node = caller
        if not is_admin:
            request._send_json(403, {"error": "the admin token is required to set run preference"})
            return
        parsed = request._read_api_body()
        if parsed is None:
            return
        body = parsed.get("preference") if isinstance(parsed, dict) and "preference" in parsed else parsed
        stored = _hub.set_run_preference(conn, body, updated_by="admin")
        payload = {"preference": {key: value for key, value in stored.items() if value is not None}}
    except FleetHubError as exc:
        request._send_json(400, {"error": str(exc)})
        return
    except sqlite3.Error as exc:
        request._send_json(500, {"error": f"hub database error: {exc}"})
        return
    finally:
        conn.close()
    request._send_json(200, payload)


def admit_post(request: Any, hub: HubContext, path: str, *, is_admin: bool) -> bool:
    """The per-path refusals that run before the body is read; ``False`` once refused."""
    # ``/policy``, ``/cloud`` and ``/grokbot`` decide admin writes per action, after the body.
    refusal = _ADMIN_POST_REFUSALS.get(path)
    if refusal is not None:
        if not is_admin:
            request._send_json(403, {"error": refusal})
            return False
    elif path in ("/events", "/claims", "/sessions", "/messages") and is_admin and not hub.allow_admin_writes:
        request._send_json(403, {"error": "the admin token may not post events or claims: " + _ADMIN_WRITE_HINT})
        return False
    return True


def post(
    request: Any,
    conn: sqlite3.Connection,
    hub: HubContext,
    path: str,
    parsed: Any,
    *,
    is_admin: bool,
    caller_node: str | None,
) -> tuple[int, dict[str, Any]] | None:
    """Dispatch an admitted ``POST``; ``None`` once a refusal has been sent."""
    if path == "/mesh/identity":
        return 200, fleet_hub_identity.upload_identity(conn, parsed, storage_policy=hub.storage_policy)
    if path == "/mesh/statements":
        return 200, fleet_hub_identity.upload_statement(conn, parsed)
    if path == "/events":
        return 200, dict(_hub.store_events(conn, parsed, caller_node=caller_node))
    if path == "/claims":
        return _hub.handle_claim(conn, parsed, caller_node=caller_node)
    if path == "/sessions":
        return fleet_hub_sessions.handle_session(conn, parsed, caller_node=caller_node)
    if path == "/messages":
        return fleet_hub_messages.handle(
            conn,
            parsed,
            caller=caller_node,
            open_conn=lambda: hub.open_db(hub.db_path),
            source_ip=request.client_address[0],
            storage_policy=hub.storage_policy,
            authenticated_generation=_hub.hash_node_token(request._bearer() or ""),
        )
    if path == "/message-principals":
        return fleet_hub_messages.handle_principal_admin(conn, parsed)
    if path == "/cloud":
        if (
            is_admin
            and not hub.allow_admin_writes
            and (not isinstance(parsed, dict) or parsed.get("action") != "policy")
        ):
            request._send_json(
                403,
                {"error": "the admin token may not admit, bind, renew, or release cloud leases: " + _ADMIN_WRITE_HINT},
            )
            return None
        return _hub.handle_cloud(conn, parsed, caller_node=caller_node, config=hub.deck)
    if path == "/models":
        return _hub.handle_model_policy(conn, parsed, caller_node=caller_node)
    if path == "/policy":
        return _post_policy(request, conn, hub, parsed, is_admin=is_admin, caller_node=caller_node)
    if path == "/grokbot":
        if (
            is_admin
            and not hub.allow_admin_writes
            and (not isinstance(parsed, dict) or parsed.get("action") not in _GROKBOT_ADMIN_READS)
        ):
            request._send_json(
                403, {"error": "the admin token may not mutate the Grok Bot queue: " + _ADMIN_WRITE_HINT}
            )
            return None
        return fleet_hub_grokbot.handle_grokbot(conn, parsed, caller_node=caller_node, config=hub.deck)
    return _hub.handle_node_request(conn, parsed)


def _post_policy(
    request: Any,
    conn: sqlite3.Connection,
    hub: HubContext,
    parsed: Any,
    *,
    is_admin: bool,
    caller_node: str | None,
) -> tuple[int, dict[str, Any]] | None:
    if not isinstance(parsed, dict):
        request._send_json(400, {"error": {"code": "invalid-request", "message": "body is not valid JSON"}})
        return None
    action = parsed.get("action")
    if action in fleet_hub_policy_api.ADMIN_ACTIONS and not is_admin:
        message = "the admin token is required for policy document writes"
        request._send_json(403, {"error": {"code": "auth-failed", "message": message}})
        return None
    if action in fleet_hub_policy_api.NODE_ACTIONS and is_admin and not hub.allow_admin_writes:
        message = "the admin token may not resolve or prepare policy: " + _ADMIN_WRITE_HINT
        request._send_json(403, {"error": {"code": "auth-failed", "message": message}})
        return None
    return fleet_hub_policy_api.handle_policy(conn, parsed, caller_node=caller_node, is_admin=is_admin)
