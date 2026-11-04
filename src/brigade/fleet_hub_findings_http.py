"""Additive v1 findings route. No findings key or canonical event key is logged."""

from __future__ import annotations

import sqlite3
from typing import Any

from . import fleet_hub_findings as findings, fleet_hub_messages
from .fleet_hub import FleetHubError, lookup_node_token
from .fleet_hub_routes import HubContext, RouteTable
from . import fleet_hub_event_bounds as request_bounds


def register_routes(routes: RouteTable, hub: HubContext) -> None:
    routes.add("POST", lambda request, path, query: serve(request, hub), paths=("/findings",))


def serve(request: Any, hub: HubContext) -> None:
    conn = None
    try:
        conn = hub.open_db(hub.db_path)
        admin = request._authorized()
        caller = "admin"
        if not admin:
            bearer = request._bearer()
            if not bearer:
                raise findings.FindingError("unauthorized", "finding_unauthorized", 401)
            node, revoked = lookup_node_token(conn, bearer)
            if node and not revoked:
                caller = node
            else:
                principal = fleet_hub_messages.lookup_principal_token(conn, bearer)
                if principal is None:
                    raise findings.FindingError("unauthorized", "finding_unauthorized", 401)
                if (
                    conn.execute(
                        "SELECT 1 FROM fleet_finding_grants g JOIN fleet_message_principals p USING(principal_id) WHERE g.principal_id=? AND g.enrollment_epoch=p.authorization_enrollment_epoch",
                        (principal,),
                    ).fetchone()
                    is None
                ):
                    raise findings.FindingError("findings grant required", "finding_forbidden", 403)
                caller = principal
        try:
            raw = request_bounds.read_json_body(request, limit=16 * 1024)
        except request_bounds.RequestBoundsError as exc:
            request.close_connection = True
            raise findings.FindingError(str(exc), "finding_invalid", exc.status) from exc
        request._send_json(
            200,
            findings.handle(
                conn, raw, caller=caller, owners=None, admin=admin, enforce_auth=True, credential=request._bearer()
            ),
        )
    except findings.FindingError as exc:
        body = {"error": str(exc), "code": exc.code}
        if exc.report_ref is not None:
            body["report_ref"] = exc.report_ref
        request._send_json(exc.status, body)
    except (sqlite3.Error, FleetHubError, OSError):
        request._send_json(503, {"error": "findings unavailable", "code": "finding_unavailable"})
    except ValueError:
        request._send_json(400, {"error": "invalid findings request", "code": "finding_invalid"})
    finally:
        if conn is not None:
            conn.close()
