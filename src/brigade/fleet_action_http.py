"""Bounded API and browser routes for action requests."""

from __future__ import annotations

import json
import secrets
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode

from . import fleet_action_auth as auth, fleet_action_page as page, fleet_action_sources as sources
from . import fleet_action_targets as targets, fleet_hub_actions as actions, fleet_hub_enrollment as enrollment
from .fleet_action_auth import ActionError


def _html(handler: Any, *args: Any, **kwargs: Any) -> None:
    # no-referrer makes real browser form POSTs send Origin:null. Keep exact Origin validation.
    handler._send_html(*args, referrer_policy="same-origin", **kwargs)


def handle_api(conn: sqlite3.Connection, caller: tuple[bool, str | None], payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ActionError("invalid-request", 400)
    admin, node_id = caller
    op = payload.get("op")
    if op in {"grant-operator", "revoke-operator", "grant-broker", "revoke-broker"}:
        if not admin:
            raise ActionError("admin-required")
        if op == "grant-operator":
            return {"grant_id": auth.grant_operator(conn, payload.get("login", ""), payload.get("scopes"))}
        if op == "revoke-operator":
            auth.revoke_operator(conn, payload.get("grant_id", ""))
            return {"revoked": True}
        if op == "revoke-broker":
            auth.revoke_broker(conn, payload.get("node_id", ""))
            return {"revoked": True}
        scope = payload.get("scope")
        if not isinstance(scope, dict) or set(scope) != {"node_id", "target_ids", "subjects"}:
            raise ActionError("invalid-scopes", 400)
        auth.grant_broker(conn, scope["node_id"], scope["target_ids"], scope["subjects"])
        return {"granted": True}
    if op == "show":
        action = actions.show(conn, payload.get("action_id", ""))
        if not admin:
            if action["node_id"] != node_id:
                raise ActionError("broker-scope-refused")
            if (
                node_id is None
                or not auth.active_node(conn, node_id)
                or conn.execute(
                    "SELECT 1 FROM action_broker_grants WHERE node_id=? AND revoked_at IS NULL", (node_id,)
                ).fetchone()
                is None
            ):
                raise ActionError("enrolled-broker-required")
            if not auth.broker_allowed(conn, node_id, action["target_id"], action["request"]["source"]):
                raise ActionError("broker-scope-refused")
        return action
    if (
        admin
        or node_id is None
        or not auth.active_node(conn, node_id)
        or conn.execute(
            "SELECT 1 FROM action_broker_grants WHERE node_id=? AND revoked_at IS NULL", (node_id,)
        ).fetchone()
        is None
    ):
        raise ActionError("enrolled-broker-required")
    if op == "targets-sync":
        targets.sync(conn, node_id, payload.get("targets"))
        return {"synced": True}
    if op == "claim":
        return {"action": actions.claim(conn, node_id)}
    if op == "start":
        return actions.start(conn, node_id, payload.get("action_id", ""), payload.get("generation", 0))
    if op == "report":
        return actions.report(
            conn, node_id, payload.get("action_id", ""), payload.get("generation", 0), payload.get("receipt")
        )
    raise ActionError("unknown-operation", 400)


def _params(raw: str) -> dict[str, str]:
    if raw == "":
        return {}
    if len(raw) > 65536:
        raise ActionError("invalid-form", 400)
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=12, errors="strict")
    except (ValueError, UnicodeError):
        raise ActionError("invalid-form", 400) from None
    if len(dict(pairs)) != len(pairs):
        raise ActionError("duplicate-field", 400)
    return dict(pairs)


def _form_body(handler: Any) -> bytes | None:
    lengths = handler.headers.get_all("Content-Length", [])
    types = handler.headers.get_all("Content-Type", [])
    if handler.headers.get_all("Transfer-Encoding", []) or len(lengths) != 1 or len(types) != 1:
        raise ActionError("invalid-form", 400)
    if types[0].partition(";")[0].strip().lower() != "application/x-www-form-urlencoded":
        raise ActionError("invalid-content-type", 415)
    try:
        length = int(lengths[0])
    except ValueError:
        raise ActionError("invalid-form", 400) from None
    if not 0 < length <= 65536:
        raise ActionError("form-too-large", 413)
    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise ActionError("incomplete-form", 400)
    return bytes(raw)


def _login(handler: Any) -> str | None:
    values = handler.headers.get_all("Tailscale-User-Login", [])
    if len(values) != 1 or not handler._tailscale_identity_authorized() or values[0] != values[0].strip():
        return None
    return str(values[0])


def _same_origin(handler: Any) -> bool:
    hosts = handler.headers.get_all("Host", [])
    if len(hosts) != 1 or handler.headers.get_all("Sec-Fetch-Site", []) != ["same-origin"]:
        return False
    try:
        expected = enrollment.canonical_origin("https" if handler._secure_transport() else "http", hosts[0])
        return handler.headers.get_all("Origin", []) == [expected]
    except enrollment.EnrollmentError:
        return False


def _cookie(handler: Any, name: str) -> str:
    return enrollment.cookie_credential(handler.headers.get_all("Cookie", []), name) or ""


def _set_cookie(handler: Any, name: str, credential: str, ttl: int) -> str:
    return f"{name}={credential}; Path=/deck/actions; Max-Age={ttl}; HttpOnly; SameSite=Strict" + (
        "; Secure" if handler._secure_transport() else ""
    )


def _session(handler: Any, conn: sqlite3.Connection, csrf: str | None = None) -> dict[str, Any]:
    result = auth.session(conn, _cookie(handler, auth.COOKIE), csrf)
    if _login(handler) != result["login"]:
        raise ActionError("operator-login-refused")
    return result


def handle_browser(handler: Any, conn: sqlite3.Connection, signing_key: str) -> None:
    path, _, query = handler.path.partition("?")
    nonce = secrets.token_urlsafe(16)
    values = _params(query)
    if handler.command == "POST":
        if not _same_origin(handler) or handler._prefetch():
            raise ActionError("origin-refused")
        raw = _form_body(handler)
        if raw is None:
            return
        values = _params(raw.decode("utf-8", errors="strict"))
    if path == "/deck/actions/enable":
        login = _login(handler)
        if login is None:
            raise ActionError("trusted-login-required")
        auth.login_grant(conn, login)
        if handler.command == "GET":
            bootstrap_nonce, proof = auth.bootstrap(login, signing_key)
            _html(
                handler,
                200,
                page.enable(proof, values.get("source", ""), nonce),
                nonce=nonce,
                extra_headers={
                    "Set-Cookie": _set_cookie(handler, auth.BOOT_COOKIE, bootstrap_nonce, auth.BOOTSTRAP_TTL)
                },
            )
        else:
            credential, _ = auth.activate(
                conn, login, _cookie(handler, auth.BOOT_COOKIE), values.get("proof", ""), signing_key
            )
            location = (
                "/deck/actions/compose?" + urlencode({"source": values.get("source", "")})
                if values.get("source")
                else "/deck"
            )
            _html(
                handler,
                303,
                "",
                extra_headers={
                    "Location": location,
                    "Set-Cookie": _set_cookie(handler, auth.COOKIE, credential, auth.SESSION_TTL),
                },
            )
        return
    if path == "/deck/actions/compose" and handler.command == "GET":
        values = {key: val for key, val in values.items() if key in {"source", "family", "target_id"}}
        if not (handler._cookie_authorized() or _login(handler) is not None):
            raise ActionError("viewing-access-required", 401)
        snapshot = sources.resolve(conn, json.loads(values.get("source", "null")))
        csrf = None
        catalog = targets.available(conn, source=snapshot["source"])
        try:
            operator = _session(handler, conn)
        except ActionError:
            pass
        else:
            csrf = auth.csrf_for(_cookie(handler, auth.COOKIE), signing_key)
            catalog = [
                t
                for t in catalog
                if auth.operator_allowed(conn, operator["grant_id"], t["target_id"], snapshot["source"])
            ]
        _html(handler, 200, page.compose(snapshot, catalog, nonce=nonce, csrf=csrf, values=values), nonce=nonce)
        return
    if path == "/deck/actions/submit" and handler.command == "POST":
        operator = _session(handler, conn, values.get("csrf", ""))
        raw_request: dict[str, Any] = {key: val for key, val in values.items() if key not in {"csrf", "family"}}
        raw_request["source"] = json.loads(values.get("source", "null"))
        try:
            if values.get("family"):
                selected = targets.selected(
                    conn, values.get("target_id", ""), raw_request["source"], values.get("model_id", "")
                )
                if selected["family"] != values["family"]:
                    raise ActionError("destination-family-mismatch", 400)
            action = actions.submit(conn, operator["grant_id"], raw_request)
        except ActionError as exc:
            if exc.status not in {400, 409}:
                raise
            if exc.reason == "client-id-conflict":
                values = {**values, "client_id": "click_" + secrets.token_hex(16)}
            snapshot = sources.resolve(conn, raw_request["source"])
            catalog = [
                t
                for t in targets.available(conn, source=snapshot["source"])
                if auth.operator_allowed(conn, operator["grant_id"], t["target_id"], snapshot["source"])
            ]
            _html(
                handler,
                exc.status,
                page.compose(
                    snapshot,
                    catalog,
                    nonce=nonce,
                    csrf=values["csrf"],
                    values=values,
                    message=exc.reason + ". Review current evidence and submit again.",
                ),
                nonce=nonce,
            )
            return
        _html(handler, 303, "", extra_headers={"Location": "/deck/actions/" + action["action_id"]})
        return
    if handler.command == "GET" and path.startswith("/deck/actions/aa_"):
        operator = _session(handler, conn)
        action = actions.show(conn, path.rsplit("/", 1)[1])
        if action["grant_id"] != operator["grant_id"]:
            raise ActionError("scope-refused")
        _html(handler, 200, page.status(action, nonce), nonce=nonce)
        return
    raise ActionError("not-found", 404)


def route(handler: Any, db_path: Path, signing_key: str) -> None:
    from .fleet_hub import FleetHubError, open_db

    api = handler.path.partition("?")[0] == "/actions"
    try:
        conn = open_db(db_path)
        try:
            if api:
                caller = handler._caller(conn)
                if caller is None:
                    return
                payload = handler._read_json_body(65536)
                if payload is None:
                    return
                handler._send_json(200, {"schema": actions.SCHEMA, **handle_api(conn, caller, payload)})
            else:
                handle_browser(handler, conn, signing_key)
        finally:
            conn.close()
    except ActionError as exc:
        if api:
            handler._send_json(exc.status, {"schema": actions.SCHEMA, "error": exc.reason})
        else:
            _html(
                handler,
                exc.status,
                "Action request refused: " + page.esc(exc.reason),
                content_type="text/plain; charset=utf-8",
            )
    except (ValueError, UnicodeError, TypeError, KeyError, RecursionError):
        if api:
            handler._send_json(400, {"error": "invalid-request"})
        else:
            _html(handler, 400, "Action request refused.", content_type="text/plain; charset=utf-8")
    except (FleetHubError, sqlite3.Error):
        if api:
            handler._send_json(503, {"error": "action-store-unavailable"})
        else:
            _html(handler, 503, "Action store unavailable.", content_type="text/plain; charset=utf-8")
