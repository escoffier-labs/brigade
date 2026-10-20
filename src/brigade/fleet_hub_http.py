"""HTTP adapter for the Fleet Hub domain service.

Keeping request parsing, authentication, and HTML rendering outside the
SQLite domain module makes the hub's security boundary easier to inspect.
"""

import hmac
import html
import ssl
import importlib.resources
import json
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qs, urlencode

from . import fleet_hub_enrollment as enrollment
from . import fleet_claim_display, fleet_admin_stats_http
from . import (
    fleet_command_deck,
    fleet_dashboard,
    fleet_hub_grokbot,
    fleet_hub_messages,
    fleet_hub_sessions,
    worklore_http,
)
from . import fleet_hub as _hub
from . import fleet_hub_model_roster
from . import fleet_hub_policy_api
from . import fleet_hub_roster_page
from . import fleet_hub_routing
from . import fleet_hub_status
from . import fleet_hub_event_bounds as request_bounds
from .fleet_hub_claim_cardinality import ClaimCardinalityError
from . import fleet_model_inventory
from . import fleet_owners
from . import fleet_owners_page
from . import fleet_policy_page
from . import fleet_repo_policy_page
from . import fleet_work_page
from .fleet_hub_claims import ClaimFenceError, ClaimTargetError, status as claim_fencing_status
from .worklore_validate import WorkloreValidationError
from .fleet_hub import (
    DASHBOARD_COOKIE,
    DASHBOARD_COOKIE_MAX_AGE,
    MAX_BODY_BYTES,
    FleetHubConflict,
    FleetHubError,
    FleetHubForbidden,
    FleetHubUnprocessable,
    FleetHubUnsupportedSchema,
    _ASSET_CACHE_CONTROL,
    _ASSET_CONTENT_TYPES,
    _ASSET_ROUTES,
    _DASHBOARD_PREFIX,
    _TAILSCALE_IDENTITY_HEADER,
    _TAILSCALE_IDENTITY_MAX_LEN,
)


def _is_loopback_address(address: str) -> bool:
    return _hub._is_loopback_address(address)


def open_db(*args: Any, **kwargs: Any) -> Any:
    return _hub.open_db(*args, **kwargs)


def store_events(*args: Any, **kwargs: Any) -> Any:
    return _hub.store_events(*args, **kwargs)


def latest_status(*args: Any, **kwargs: Any) -> Any:
    return _hub.latest_status(*args, **kwargs)


def board_status(*args: Any, **kwargs: Any) -> Any:
    return _hub.board_status(*args, **kwargs)


def run_started_at(*args: Any, **kwargs: Any) -> Any:
    return _hub.run_started_at(*args, **kwargs)


def node_summary(*args: Any, **kwargs: Any) -> Any:
    return _hub.node_summary(*args, **kwargs)


def _board_include_all(params: dict[str, list[str]]) -> bool:
    return params.get("all", [""])[0].lower() in ("1", "true", "yes", "on")


def _board_more_href(
    path: str,
    params: dict[str, list[str]],
    *,
    next_offset: int,
    unread_offset: int | None = None,
    unread_only_page: bool = False,
    claim_offset: int | None = None,
    claim_only_page: bool = False,
) -> str:
    next_params = {key: list(values) for key, values in params.items()}
    next_params["offset"] = [str(next_offset)]
    if unread_offset is not None:
        next_params["unread_offset"] = [str(unread_offset)]
    if unread_only_page:
        next_params["unread_only"] = ["1"]
    else:
        next_params.pop("unread_only", None)
    if claim_offset is not None:
        next_params["claim_offset"] = [str(claim_offset)]
    elif not claim_only_page:
        next_params.pop("claim_offset", None)
    if claim_only_page:
        next_params["claim_only"] = ["1"]
    else:
        next_params.pop("claim_only", None)
    rest = urlencode(next_params, doseq=True)
    return f"{path}?{rest}" if rest else path


def handle_claim(*args: Any, **kwargs: Any) -> Any:
    return _hub.handle_claim(*args, **kwargs)


def handle_cloud(*args: Any, **kwargs: Any) -> Any:
    return _hub.handle_cloud(*args, **kwargs)


def handle_grokbot(*args: Any, **kwargs: Any) -> Any:
    return fleet_hub_grokbot.handle_grokbot(*args, **kwargs)


def cloud_snapshot(*args: Any, **kwargs: Any) -> Any:
    return _hub.cloud_snapshot(*args, **kwargs)


def handle_model_policy(*args: Any, **kwargs: Any) -> Any:
    return _hub.handle_model_policy(*args, **kwargs)


def handle_node_request(*args: Any, **kwargs: Any) -> Any:
    return _hub.handle_node_request(*args, **kwargs)


def list_claims(*args: Any, **kwargs: Any) -> Any:
    return _hub.list_claims(*args, **kwargs)


def handle_session(*args: Any, **kwargs: Any) -> Any:
    return fleet_hub_sessions.handle_session(*args, **kwargs)


def list_sessions(*args: Any, **kwargs: Any) -> Any:
    return fleet_hub_sessions.list_sessions(*args, **kwargs)


def list_model_policy(*args: Any, **kwargs: Any) -> Any:
    return _hub.list_model_policy(*args, **kwargs)


def list_nodes(*args: Any, **kwargs: Any) -> Any:
    return _hub.list_nodes(*args, **kwargs)


def get_run_preference(*args: Any, **kwargs: Any) -> Any:
    return _hub.get_run_preference(*args, **kwargs)


def set_run_preference(*args: Any, **kwargs: Any) -> Any:
    return _hub.set_run_preference(*args, **kwargs)


def lookup_node_token(*args: Any, **kwargs: Any) -> Any:
    return _hub.lookup_node_token(*args, **kwargs)


def control_plane_from_hub(conn: Any) -> Any:
    """Adapt the live routing snapshot for the Deck.

    Missing or unreadable projection stays unknown. This helper does not
    recompute routing or quota precedence.
    """
    try:
        raw = fleet_hub_routing.control_plane_status(conn)
    except (FleetHubError, sqlite3.Error, KeyError, ValueError, TypeError):
        return None
    return fleet_command_deck.control_plane_from_snapshot(raw)


def make_handler(
    token: str,
    db_path: Path,
    *,
    allow_admin_writes: bool = False,
    deck_config: fleet_command_deck.DeckConfig | None = None,
    trust_tailscale_identity: bool = False,
    trust_forwarded_proto: bool = False,
    policy_inventory: Callable[[], fleet_policy_page.Inventory] | None = None,
) -> type[BaseHTTPRequestHandler]:
    """Request handler bound to the admin ``token`` and the hub database.

    ``allow_admin_writes`` lets the admin token ``POST /events`` and
    ``POST /claims`` under any ``node_id`` (the pre-#1150 shared-token
    behaviour); off by default so a migration is an explicit choice.
    ``deck_config`` is the startup-frozen Command Deck configuration: it is
    captured immutably in the handler closure and never re-read from disk.
    ``trust_tailscale_identity`` enables dashboard authorization from the
    Tailscale-User-Login header added by a Tailscale Serve reverse proxy. It
    must only be used when the hub is bound to a loopback interface and the
    proxy strips spoofed headers; the identity value is never logged or rendered.
    ``policy_inventory`` supplies an injected provider snapshot for fixtures.
    When it is unset the policy page loads the persisted SQL inventory store
    (or reports ``unavailable`` when that store is empty). The page never
    probes a provider per render.
    """
    frozen_deck = deck_config if deck_config is not None else fleet_command_deck.DeckConfig()

    class _Handler(BaseHTTPRequestHandler):
        server_version = "brigade-fleet-hub/1"
        sys_version = ""
        # Idle-socket guard: a peer that connects and never sends a request
        # line cannot pin a handler thread forever (pre-auth).
        timeout = 30

        def log_message(self, fmt: str, *log_args: Any) -> None:  # quiet by default
            pass

        def _bearer(self) -> str | None:
            """The presented bearer credential, or ``None`` without one."""
            scheme, _, presented = self.headers.get("Authorization", "").partition(" ")
            if scheme != "Bearer" or not presented:
                return None
            return presented

        def _authorized(self) -> bool:
            """True for the admin token (constant-time)."""
            auth = self.headers.get("Authorization", "")
            return hmac.compare_digest(auth.encode("utf-8"), f"Bearer {token}".encode("utf-8"))

        def _caller(self, conn: sqlite3.Connection) -> tuple[bool, str | None] | None:
            """``(is_admin, node_id)`` for the request's bearer, having sent a
            401 and returned ``None`` when it is missing, unknown, or revoked.
            The admin token is checked first, in constant time; anything
            else is looked up as a node token."""
            presented = self._bearer()
            if presented is None:
                self._send_json(401, {"error": "unauthorized"})
                return None
            if self._authorized():
                return True, None
            node_id, revoked = lookup_node_token(conn, presented)
            if node_id is None:
                if self.path.partition("?")[0] == "/messages":
                    principal = fleet_hub_messages.lookup_principal_token(conn, presented)
                    if principal is not None:
                        return False, principal
                self._send_json(401, {"error": "unauthorized"})
                return None
            if revoked:
                self._send_json(401, {"error": "unauthorized: node token revoked"})
                return None
            return False, node_id

        def _cookie_authorized(self) -> bool:
            credential = enrollment.cookie_credential(self.headers.get_all("Cookie", []), DASHBOARD_COOKIE)
            if credential is None:
                return False
            try:
                conn = open_db(Path(db_path))
                try:
                    return enrollment.authorized(conn, credential)
                finally:
                    conn.close()
            except (FleetHubError, sqlite3.Error):
                return False

        def _secure_transport(self) -> bool:
            if isinstance(self.connection, ssl.SSLSocket):
                return True
            bound = self.server.server_address
            return (
                trust_forwarded_proto
                and isinstance(bound, tuple)
                and _is_loopback_address(str(bound[0]))
                and _is_loopback_address(self.client_address[0])
                and self.headers.get_all("X-Forwarded-Proto", []) == ["https"]
            )

        def _enrollment_same_origin(self) -> bool:
            hosts = self.headers.get_all("Host", [])
            origins = self.headers.get_all("Origin", [])
            sites = self.headers.get_all("Sec-Fetch-Site", [])
            if len(hosts) != 1 or len(origins) > 1 or len(sites) > 1:
                return False
            try:
                expected = enrollment.canonical_origin("https" if self._secure_transport() else "http", hosts[0])
            except enrollment.EnrollmentError:
                return False
            if sites and sites != ["same-origin"]:
                return False
            try:
                return enrollment.serialized_origin(origins[0]) == expected if origins else sites == ["same-origin"]
            except enrollment.EnrollmentError:
                return False

        def _prefetch(self) -> bool:
            return any(
                "prefetch" in value.lower()
                for header in ("Purpose", "Sec-Purpose")
                for value in self.headers.get_all(header, [])
            )

        def _dashboard_error(self, status: int) -> None:
            self._send_html(status, "Dashboard request refused.\n", content_type="text/plain; charset=utf-8")

        def _bounded_dashboard_body(self, media_type: str) -> bytes | None:
            lengths = self.headers.get_all("Content-Length", [])
            types = self.headers.get_all("Content-Type", [])
            if self.headers.get_all("Transfer-Encoding", []) or len(lengths) != 1:
                self._dashboard_error(400)
                return None
            if len(types) != 1 or types[0].partition(";")[0].strip().lower() != media_type:
                self._dashboard_error(415)
                return None
            try:
                length = int(lengths[0])
            except ValueError:
                self._dashboard_error(400)
                return None
            if not 0 < length <= enrollment.BODY_LIMIT:
                self._dashboard_error(413 if length > enrollment.BODY_LIMIT else 400)
                return None
            return self.rfile.read(length)

        def _serve_enrollment(self, query: str) -> None:
            try:
                if self._prefetch():
                    raise enrollment.EnrollmentError()
                code = enrollment.code_param(query)
                conn = open_db(Path(db_path))
                try:
                    if not enrollment.available(conn, code):
                        raise enrollment.EnrollmentError()
                finally:
                    conn.close()
            except enrollment.EnrollmentError:
                self._dashboard_error(400)
                return
            except (FleetHubError, sqlite3.Error):
                self._dashboard_error(500)
                return
            page = (
                '<!doctype html><html lang="en"><meta charset="utf-8">'
                "<title>Confirm dashboard access</title><h1>Read-only dashboard access</h1>"
                '<p>Confirm access for this browser for 30 days.</p><form method="post" action="/enroll">'
                f'<input type="hidden" name="code" value="{html.escape(code, quote=True)}">'
                '<button type="submit">Enroll this browser</button></form></html>'
            )
            self._send_html(200, page)

        def _post_enrollment(self, query: str) -> None:
            if query or self._prefetch() or not self._enrollment_same_origin():
                self._dashboard_error(403)
                return
            body = self._bounded_dashboard_body("application/x-www-form-urlencoded")
            if body is None:
                return
            try:
                code = enrollment.code_param(body.decode("ascii"))
                conn = open_db(Path(db_path))
                try:
                    credential = enrollment.redeem(conn, code)
                finally:
                    conn.close()
            except (enrollment.EnrollmentError, UnicodeError):
                self._dashboard_error(400)
                return
            except (FleetHubError, sqlite3.Error):
                self._dashboard_error(500)
                return
            cookie = (
                f"{DASHBOARD_COOKIE}={credential}; Path=/; HttpOnly; SameSite=Strict; "
                f"Max-Age={DASHBOARD_COOKIE_MAX_AGE}" + ("; Secure" if self._secure_transport() else "")
            )
            self._send_html(303, "", extra_headers={"Location": "/deck", "Set-Cookie": cookie})

        def _dashboard_api(self, path: str, query: str) -> None:
            # Authenticate before reading a mutation body. Never fall back to a node token.
            if not self._authorized():
                self._send_json(403 if self._bearer() else 401, {"error": "unauthorized"})
                return
            try:
                params = enrollment.strict_params(query)
                if self.command == "GET":
                    if path != "/dashboard/sessions" or set(params) - {"all", "after"}:
                        raise enrollment.EnrollmentError()
                    if "all" in params and params["all"] != "1":
                        raise enrollment.EnrollmentError()
                    after = params.get("after")
                    if after is not None and not enrollment.valid_session_id(after):
                        raise enrollment.EnrollmentError()
                    conn = open_db(Path(db_path))
                    try:
                        payload = enrollment.list_sessions(conn, include_all=params.get("all") == "1", after=after)
                    finally:
                        conn.close()
                    status = 200
                else:
                    if params:
                        raise enrollment.EnrollmentError()
                    body = self._bounded_dashboard_body("application/json")
                    if body is None:
                        return

                    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
                        result = dict(pairs)
                        if len(result) != len(pairs):
                            raise enrollment.EnrollmentError()
                        return result

                    raw = request_bounds.bounded_json_loads(body, object_pairs_hook=unique)
                    if not isinstance(raw, dict):
                        raise enrollment.EnrollmentError()
                    if path == "/dashboard/enrollment":
                        if set(raw) - {"label"}:
                            raise enrollment.EnrollmentError()
                        label = enrollment.label_value(raw.get("label"))
                    elif (
                        set(raw) != {"action", "session_id"}
                        or raw["action"] != "revoke"
                        or not enrollment.valid_session_id(raw["session_id"])
                    ):
                        raise enrollment.EnrollmentError()
                    conn = open_db(Path(db_path))
                    try:
                        if path == "/dashboard/enrollment":
                            payload = enrollment.mint(conn, label)
                            status = 201
                        elif enrollment.revoke(conn, raw["session_id"]):
                            payload = {"revoked": True, "session_id": raw["session_id"]}
                            status = 200
                        else:
                            payload = {"error": "dashboard session not found"}
                            status = 404
                    finally:
                        conn.close()
            except (enrollment.EnrollmentError, ValueError, UnicodeError, RecursionError):
                self._send_json(400, {"error": "dashboard request refused"})
                return
            except (FleetHubError, sqlite3.Error):
                self._send_json(500, {"error": "hub database error"})
                return
            self._send_json(status, payload)

        def _refuse_token_query(self, query: str) -> bool:
            if "token" in parse_qs(query, keep_blank_values=True):
                self._dashboard_error(401)
                return True
            return False

        def _tailscale_identity_authorized(self) -> bool:
            """True when a trusted Tailscale Serve proxy presents a valid user login header.

            The immediate TCP peer must be loopback; the header must be present,
            non-empty, bounded, and free of control characters. The identity value
            is never logged or rendered in any response. Only enabled when the hub
            starts with ``--trust-tailscale-identity``.
            """
            if not trust_tailscale_identity:
                return False
            if not _is_loopback_address(self.client_address[0]):
                return False
            identity = self.headers.get(_TAILSCALE_IDENTITY_HEADER, "")
            if not isinstance(identity, str):
                return False
            if len(identity) > _TAILSCALE_IDENTITY_MAX_LEN:
                return False
            if re.search(r"[\x00-\x1f\x7f-\x9f]", identity):
                return False
            identity = identity.strip()
            if not identity:
                return False
            return True

        def _worklore_path(self) -> str | None:
            path = self.path.partition("?")[0]
            if path == "/work" or path.startswith("/work/"):
                return path
            return None

        def _read_worklore_body(self, path: str) -> dict[str, Any] | None:
            limit = (
                request_bounds.WORKLORE_IMPORT_BODY_BYTES
                if path == "/work/imports"
                else request_bounds.WORKLORE_BODY_BYTES
            )
            parsed = self._read_json_body(limit, empty_delete=self.command == "DELETE")
            if parsed is None:
                return None
            if not isinstance(parsed, dict):
                self._send_json(400, {"error": "body is not valid JSON"})
                return None
            return parsed

        def _handle_worklore(self) -> bool:
            path = self._worklore_path()
            if path is None:
                return False
            if not worklore_http.enabled():
                self._send_json(404, {"error": "not found"})
                return True
            try:
                conn = open_db(Path(db_path))
            except (FleetHubError, sqlite3.Error):
                self._send_json(500, {"error": "hub database error"})
                return True
            try:
                caller = self._caller(conn)
                if caller is None:
                    return True
                is_admin, node_id = caller
                is_operator = bool(node_id) and node_id in worklore_http.operator_nodes()
                body: dict[str, Any] = {}
                if self.command in {"POST", "PATCH", "DELETE"}:
                    parsed = self._read_worklore_body(path)
                    if parsed is None:
                        return True
                    body = parsed
                status, payload = worklore_http.handle(
                    conn,
                    worklore_http.Request(
                        method=self.command,
                        path=self.path,
                        is_admin=is_admin,
                        node_id=node_id,
                        is_operator=is_operator,
                        operator_authorization_resolved=True,
                        body=body,
                        headers={key: value for key, value in self.headers.items()},
                    ),
                )
            except sqlite3.Error:
                self._send_json(500, {"error": "hub database error"})
                return True
            finally:
                conn.close()
            self._send_json(status, payload)
            return True

        def _read_json_body(self, limit: int = MAX_BODY_BYTES, *, empty_delete: bool = False) -> Any:
            try:
                return request_bounds.read_json_body(self, limit=limit, empty_delete=empty_delete)
            except request_bounds.RequestBoundsError as exc:
                self.close_connection = True
                self._send_json(exc.status, {"error": str(exc)})
                return None

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(
            self,
            status: int,
            body: str,
            *,
            content_type: str = "text/html; charset=utf-8",
            nonce: str | None = None,
            referrer_policy: str = "no-referrer",
            extra_headers: dict[str, str] | None = None,
        ) -> None:
            """Dashboard response with the same security headers as ``center serve``."""
            nonce = nonce or secrets.token_urlsafe(16)
            csp = (
                f"default-src 'none'; script-src 'nonce-{nonce}'; script-src-attr 'none'; "
                f"style-src 'nonce-{nonce}'; img-src 'self'; manifest-src 'self'; connect-src 'none'; base-uri 'none'; "
                "form-action 'self'; frame-ancestors 'none'"
            )
            data = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Security-Policy", csp)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", referrer_policy)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Vary", "Cookie")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve_asset(self, path: str) -> None:
            """Serve an exact branding asset from the package resources.

            No auth is required: these files carry no fleet data. The path is
            looked up by basename inside ``brigade/assets/fleet_hub`` via
            ``importlib.resources`` so there is no caller-selected filesystem
            path and no traversal.
            """
            name = _ASSET_ROUTES.get(path)
            if name is None:
                self._send_json(404, {"error": "not found"})
                return
            try:
                resource = importlib.resources.files("brigade") / "assets" / "fleet_hub" / name
                data = resource.read_bytes()
            except (OSError, ValueError, KeyError):
                self._send_json(404, {"error": "not found"})
                return
            content_type = _ASSET_CONTENT_TYPES.get(name, "application/octet-stream")
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", _ASSET_CACHE_CONTROL)
            self.end_headers()
            self.wfile.write(data)

        def _serve_dashboard(self, path: str, query: str) -> None:
            plain = "text/plain; charset=utf-8"
            view = fleet_dashboard.DEFAULT_VIEW if path == "/" else path[len(_DASHBOARD_PREFIX) :]
            if view not in fleet_dashboard.VIEWS:
                self._send_html(404, "Not found.\n", content_type=plain)
                return
            params = parse_qs(query, keep_blank_values=False)
            dashboard_authorized = (
                self._authorized() or self._cookie_authorized() or self._tailscale_identity_authorized()
            )
            if not dashboard_authorized:
                self._send_html(
                    401,
                    "Unauthorized: use brigade fleet enroll for read-only dashboard access.\n",
                    content_type=plain,
                )
                return
            try:
                # Non-migrating connection (#1161): the schema exists because
                # server startup created it; this open is read/write only.
                conn = open_db(Path(db_path))
            except FleetHubUnsupportedSchema:
                self._send_json(503, {"error": FleetHubUnsupportedSchema.public_message})
                return
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            try:
                include_all = _board_include_all(params)
                current = datetime.now(timezone.utc)
                offset = fleet_hub_status.clamp_board_offset(params.get("offset", ["0"])[0])
                query_model = (
                    fleet_dashboard.parse_query(urlencode(params, doseq=True), view=view)
                    if view == "machines"
                    else None
                )
                complete_base_node_ids: set[str] = set()
                truncated_node_ids: set[str] = set()
                if query_model is not None:

                    def machine_priority(entry: dict[str, Any]) -> tuple[Any, ...]:
                        rows = fleet_dashboard.build_rows([entry], {}, now=current)
                        return fleet_dashboard.machine_sort_key(str(entry["node_id"]), rows, query_model)

                    def machine_matches(entry: dict[str, Any]) -> bool:
                        return bool(
                            fleet_dashboard.filter_rows(
                                fleet_dashboard.build_rows([entry], {}, now=current), query_model
                            )
                        )

                    complete_base_node_ids, machine_node_ids, machine_more = fleet_hub_status.board_machine_page(
                        conn,
                        include_all=include_all,
                        offset=offset,
                        now=current,
                        history_window_seconds=frozen_deck.stale_history_after_seconds,
                        stale_history_after_seconds=frozen_deck.stale_history_after_seconds,
                        predicate=machine_matches,
                        resolve_last_known_seat=bool(query_model.seat),
                        priority=machine_priority,
                    )
                    machine_limit = fleet_hub_status.BOARD_LIMIT
                    board_runs, truncated_node_ids = fleet_hub_status.board_status_for_nodes(
                        conn,
                        machine_node_ids,
                        include_all=include_all,
                        now=current,
                        history_window_seconds=frozen_deck.stale_history_after_seconds,
                        stale_history_after_seconds=frozen_deck.stale_history_after_seconds,
                        resolve_last_known_seat=True,
                        predicate=machine_matches,
                        priority=machine_priority,
                    )
                    board = fleet_hub_status.BoardStatus(
                        runs=board_runs,
                        more=machine_more,
                        offset=offset,
                        limit=machine_limit,
                    )
                else:
                    board = board_status(
                        conn,
                        include_all=include_all,
                        offset=offset,
                        now=current,
                        history_window_seconds=frozen_deck.stale_history_after_seconds,
                        stale_history_after_seconds=frozen_deck.stale_history_after_seconds,
                    )
                claims = fleet_claim_display.list_display_claims(conn, include_all=include_all)
                started_at = run_started_at(
                    conn,
                    [
                        (
                            str(row["node_id"]),
                            str(row["run_id"]),
                            row["harness"] if isinstance(row.get("harness"), str) else None,
                        )
                        for row in board.runs
                    ],
                )
                nodes = node_summary(board.runs)
                unread_counts = (
                    fleet_hub_messages.unread_counts(conn) if self._authorized() and view == "machines" else {}
                )
                agent_unread_counts, agent_unread_more = (
                    fleet_hub_messages.agent_unread_counts(conn)
                    if self._authorized() and view == "machines"
                    else ({}, False)
                )
            except ClaimFenceError as exc:
                self._send_json(503, {"error": str(exc), "reason": exc.reason})
                return
            except sqlite3.Error as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            finally:
                conn.close()
            more_href = _board_more_href(path, params, next_offset=board.offset + board.limit) if board.more else None
            unread_offset = fleet_hub_status.clamp_board_offset(params.get("unread_offset", ["0"])[0])
            unread_only_page = params.get("unread_only", [""])[0] == "1"
            claim_offset = fleet_hub_status.clamp_board_offset(params.get("claim_offset", ["0"])[0])
            claim_only_page = params.get("claim_only", [""])[0] == "1"
            render_runs = board.runs
            render_claims = claims
            render_nodes = nodes
            render_started_at = started_at
            render_unread_counts = unread_counts
            if view == "machines":
                assert query_model is not None
                page_rows = fleet_dashboard.filter_rows(
                    fleet_dashboard.build_rows(board.runs, started_at, now=current), query_model
                )
                page_claims = fleet_dashboard.build_claims(claims, now=current)
                event_node_ids = fleet_dashboard._machine_base_node_ids(page_rows, nodes, [], query_model)
                complete_claim_node_ids = fleet_dashboard._machine_base_node_ids([], [], page_claims, query_model)
                claim_only_ids = sorted(complete_claim_node_ids - complete_base_node_ids)
                excluded_node_ids = (
                    fleet_dashboard._machine_unread_exclusions(query_model, event_node_ids, complete_base_node_ids)
                    | complete_claim_node_ids
                )
                unread_ids = fleet_dashboard._machine_unread_candidates(
                    query_model, unread_counts, excluded_node_ids=excluded_node_ids
                )
                if claim_only_page:
                    render_runs = []
                    render_nodes = []
                    render_started_at = {}
                    claim_ids = claim_only_ids[claim_offset : claim_offset + fleet_dashboard.MACHINE_CARD_LIMIT]
                    render_claims = [claim for claim in claims if claim.get("owner_node") in claim_ids]
                    render_unread_counts = {
                        node_id: unread_counts[node_id] for node_id in claim_ids if node_id in unread_counts
                    }
                    next_claim_offset = claim_offset + len(claim_ids)
                    if next_claim_offset < len(claim_only_ids):
                        more_href = _board_more_href(
                            path,
                            params,
                            next_offset=board.offset,
                            claim_offset=next_claim_offset,
                            claim_only_page=True,
                        )
                    elif unread_offset < len(unread_ids):
                        more_href = _board_more_href(
                            path,
                            params,
                            next_offset=board.offset,
                            unread_offset=unread_offset,
                            unread_only_page=True,
                            claim_offset=next_claim_offset,
                        )
                    elif board.more:
                        more_href = _board_more_href(
                            path,
                            params,
                            next_offset=board.offset + board.limit,
                            unread_offset=unread_offset,
                            claim_offset=next_claim_offset,
                        )
                    else:
                        more_href = None
                else:
                    event_claims = [claim for claim in claims if claim.get("owner_node") in event_node_ids]
                    room = (
                        fleet_dashboard.MACHINE_CARD_LIMIT
                        if unread_only_page
                        else max(0, fleet_dashboard.MACHINE_CARD_LIMIT - len(event_node_ids))
                    )
                    selected_claim_ids: list[str] = []
                    if not board.more and not unread_only_page and unread_offset >= len(unread_ids):
                        selected_claim_ids = claim_only_ids[claim_offset : claim_offset + room]
                        render_claims = event_claims + [
                            claim for claim in claims if claim.get("owner_node") in selected_claim_ids
                        ]
                    else:
                        render_claims = event_claims
                    rendered_node_ids = event_node_ids | set(selected_claim_ids)
                    render_unread_counts = {
                        node_id: count
                        for node_id, count in unread_counts.items()
                        if node_id not in complete_claim_node_ids or node_id in rendered_node_ids
                    }
                    unread_room = max(0, room - len(selected_claim_ids))
                    consumed = len(unread_ids[unread_offset : unread_offset + unread_room])
                    next_unread_offset = unread_offset + consumed
                    unread_more = next_unread_offset < len(unread_ids)
                    next_claim_offset = claim_offset + len(selected_claim_ids)
                    claim_more = next_claim_offset < len(claim_only_ids)
                    if unread_more:
                        # Unread cards continue before the next event or claim-only page.
                        more_href = _board_more_href(
                            path,
                            params,
                            next_offset=board.offset,
                            unread_offset=next_unread_offset,
                            unread_only_page=True,
                            claim_offset=next_claim_offset,
                        )
                    elif claim_more:
                        more_href = _board_more_href(
                            path,
                            params,
                            next_offset=board.offset,
                            unread_offset=next_unread_offset,
                            claim_offset=next_claim_offset,
                            claim_only_page=True,
                        )
                    elif board.more:
                        more_href = _board_more_href(
                            path,
                            params,
                            next_offset=board.offset + board.limit,
                            unread_offset=next_unread_offset,
                            claim_offset=next_claim_offset,
                        )
                    else:
                        more_href = None
            nonce = secrets.token_urlsafe(16)
            page = fleet_dashboard.render_page(
                view=view,
                query_string=urlencode(params, doseq=True),
                runs=render_runs,
                claims=render_claims,
                nodes=render_nodes,
                started_at=render_started_at,
                nonce=nonce,
                now=current,
                more_href=more_href,
                unread_counts=render_unread_counts,
                agent_unread_counts=agent_unread_counts,
                agent_unread_more=agent_unread_more,
                unread_offset=unread_offset,
                unread_only_page=unread_only_page,
                complete_base_node_ids=complete_base_node_ids,
                truncated_node_ids=truncated_node_ids,
            )
            # The legacy board used ``/`` as its machines route. Root now
            # belongs to the Command Deck, so keep every legacy navigation
            # target and filter form under its explicit /view/machines route.
            page = (
                page.replace('href="/?', 'href="/view/machines?')
                .replace('href="/"', 'href="/view/machines"')
                .replace('action="/"', 'action="/view/machines"')
            )
            self._send_html(200, page, nonce=nonce)

        def _serve_deck(self, path: str, query: str) -> None:
            """Command Deck HTML (/, /deck, /deck/repos): admin bearer, live
            read-only session, or trusted Tailscale identity authorization,
            with the same security headers as ``_serve_dashboard``. Token query
            parameters are refused before dispatch. Other query parameters are
            ignored, never reflected. Renders from the startup-frozen config."""
            plain = "text/plain; charset=utf-8"
            repos_page = path == "/deck/repos"
            if path not in ("/", "/deck") and not repos_page:
                self._send_html(404, "Not found.\n", content_type=plain)
                return
            if not (self._authorized() or self._cookie_authorized() or self._tailscale_identity_authorized()):
                self._send_html(
                    401,
                    "Unauthorized: use brigade fleet enroll for read-only dashboard access.\n",
                    content_type=plain,
                )
                return
            try:
                conn = open_db(Path(db_path))
            except FleetHubUnsupportedSchema:
                self._send_json(503, {"error": FleetHubUnsupportedSchema.public_message})
                return
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            try:
                control_plane = None
                now = datetime.now(timezone.utc)
                live_runs = fleet_command_deck.fetch_live_runs(
                    conn, now=now, stale_after_seconds=frozen_deck.stale_after_seconds
                )
                claims: list[fleet_command_deck.Claim] = []
                for row in list_claims(conn):
                    expires = datetime.fromisoformat(str(row["expires_at"]))
                    ttl_remaining = max(0, int((expires - now).total_seconds()))
                    claims.append(
                        fleet_command_deck.Claim(
                            target=str(row["target"]),
                            owner_node=str(row["owner_node"]),
                            owner_conductor=str(row["owner_conductor"] or ""),
                            ttl_remaining=ttl_remaining,
                        )
                    )
                outcomes = fleet_command_deck.fetch_outcomes(conn, outcome_window=frozen_deck.outcome_window)
                failed_outcomes = fleet_command_deck.fetch_failed_outcomes(
                    conn,
                    now=now,
                    lookback_seconds=frozen_deck.failed_lookback_seconds,
                    stale_after_seconds=frozen_deck.stale_after_seconds,
                )
                cloud_workers = fleet_command_deck.cloud_workers_from_snapshot(cloud_snapshot(conn, frozen_deck))
                # Only unrevoked enrollments feed the label/enrolled mapping.
                enrolled_labels = {
                    node["node_id"]: str(node["label"] or "")
                    for node in list_nodes(conn)
                    if node.get("revoked_at") is None
                }
                station_ids = [station.node_id for station in frozen_deck.stations]
                last_heard = fleet_command_deck.fetch_last_heard(conn, station_ids)
                observers = fleet_command_deck.fetch_observers(conn, frozenset(station_ids))
                interactive_sessions = fleet_command_deck.fetch_interactive_sessions(conn, now=now)
                # The repos page joins the policy authority to the coordination
                # rows. It is read on the same connection, and a failure to read
                # it is reported as unknown rather than dropping the page.
                policy_view: Any = None
                policy_error = ""
                if repos_page:
                    identities = sorted(
                        {
                            str(session.get("repo_identity") or "")
                            for session in interactive_sessions
                            if session.get("repo_identity")
                        }
                        | {run.repo_identity for run in live_runs if run.repo_identity}
                    )
                    try:
                        policy_view = fleet_policy_page.load_view(
                            conn,
                            inventory=policy_inventory() if policy_inventory is not None else None,
                            activation=fleet_policy_page.probe_activation(conn),
                            known_repositories=identities,
                        )
                    except (FleetHubError, sqlite3.Error, KeyError, ValueError) as exc:
                        policy_error = f"the policy authority could not be read ({type(exc).__name__})"
                control_plane = control_plane_from_hub(conn)
                from .fleet_action_targets import linked_subjects

                view = fleet_command_deck.build_view(
                    frozen_deck,
                    live_runs=live_runs,
                    claims=claims,
                    enrolled_labels=enrolled_labels,
                    last_heard=last_heard,
                    outcomes=outcomes,
                    failed_outcomes=failed_outcomes,
                    observers=observers,
                    now=now,
                    cloud_workers=cloud_workers,
                    interactive_sessions=interactive_sessions,
                    control_plane=control_plane,
                )
                from .fleet_deck_overview import page_alerts

                try:
                    action_sources = linked_subjects(conn, [a.source_ref for a in page_alerts(view, now)])
                except (FleetHubError, KeyError, TypeError, ValueError):
                    action_sources = frozenset()
            except ClaimFenceError as exc:
                self._send_json(503, {"error": str(exc), "reason": exc.reason})
                return
            except sqlite3.Error as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            finally:
                conn.close()
            nonce = secrets.token_urlsafe(16)
            if repos_page:
                page = fleet_repo_policy_page.render(
                    view,
                    rows=fleet_repo_policy_page.build_rows(view, policy_view, unavailable_reason=policy_error),
                    policy_available=policy_view is not None,
                    unavailable_reason=policy_error,
                    nonce=nonce,
                    now=now,
                )
            else:
                page = fleet_command_deck.render_deck(
                    view, nonce=nonce, now=now, worklore_enabled=worklore_http.enabled(), action_sources=action_sources
                )
            self._send_html(200, page, nonce=nonce)

        def _roster_auth(self) -> tuple[bool, bool]:
            """Only the admin header permits editing. Browser capabilities read."""
            if self._authorized():
                return True, True
            if self._cookie_authorized() or self._tailscale_identity_authorized():
                return True, False
            return False, False

        def _render_roster(
            self,
            *,
            status: int,
            editable: bool,
            saved_revision: int | None = None,
            error: str | None = None,
            submission: Any = None,
        ) -> None:
            plain = "text/plain; charset=utf-8"
            try:
                conn = open_db(Path(db_path))
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            try:
                view = fleet_hub_roster_page.load_view(
                    conn, frozen_deck, activation=fleet_policy_page.probe_activation(conn)
                )
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            finally:
                conn.close()
            nonce = secrets.token_urlsafe(16)
            banner = f"saved as revision {view.revision}" if saved_revision == view.revision else None
            page = fleet_hub_roster_page.render(
                view,
                nonce=nonce,
                now=datetime.now(timezone.utc),
                csrf=fleet_hub_roster_page.csrf_value(token) if editable else "",
                editable=editable,
                banner=banner,
                error=error,
                submission=submission,
                policy_csrf=fleet_policy_page.csrf_value(token) if editable else "",
            )
            self._send_html(status, page, nonce=nonce)

        def _serve_observed_work(self, path: str, query: str) -> None:
            plain = "text/plain; charset=utf-8"
            if not worklore_http.enabled():
                self._send_html(404, "Not found.\n", content_type=plain)
                return
            authorized, _editable = self._roster_auth()
            if not authorized:
                self._send_html(
                    401, "Unauthorized: use brigade fleet enroll for read-only dashboard access.\n", content_type=plain
                )
                return
            key = path.removeprefix("/deck/observed/")
            group = next((group for group in frozen_deck.observed_work_groups if group.key == key), None)
            if group is None:
                self._send_html(404, "Not found.\n", content_type=plain)
                return
            if query:
                self._send_html(400, "Invalid observed work page query.\n", content_type=plain)
                return
            try:
                conn = open_db(Path(db_path))
                try:
                    conn.execute("PRAGMA query_only=ON")
                    page = fleet_work_page.load_observed_group(conn, group)
                finally:
                    conn.close()
            except (FleetHubError, sqlite3.Error):
                self._send_html(500, "Worklore task store unavailable.\n", content_type=plain)
                return
            nonce = secrets.token_urlsafe(16)
            body = fleet_work_page.render_observed_group(
                page, nonce=nonce, now=datetime.now(timezone.utc), stale_after_seconds=frozen_deck.stale_after_seconds
            )
            self._send_html(200, body, nonce=nonce)

        def _serve_owners_page(self, query: str) -> None:
            plain = "text/plain; charset=utf-8"
            authorized, editable = self._roster_auth()
            if not authorized:
                self._send_html(
                    401, "Unauthorized: use brigade fleet enroll for read-only dashboard access.\n", content_type=plain
                )
                return
            if query:
                self._send_html(400, "Invalid owners page query.\n", content_type=plain)
                return
            try:
                conn = open_db(Path(db_path))
                try:
                    conn.execute("PRAGMA query_only=ON")
                    # Grok Bot job rows are admin-only, as on /cloud and /grokbot.
                    view = fleet_owners.load_view(conn, now_epoch=_hub._now_epoch(), include_grokbot=editable)
                finally:
                    conn.close()
            except (FleetHubError, sqlite3.Error):
                self._send_html(500, "Ownership view unavailable.\n", content_type=plain)
                return
            nonce = secrets.token_urlsafe(16)
            page = fleet_owners_page.render(view, nonce=nonce, now=datetime.now(timezone.utc))
            self._send_html(200, page, nonce=nonce)

        def _serve_work(self, query: str) -> None:
            plain = "text/plain; charset=utf-8"
            if not worklore_http.enabled():
                self._send_html(404, "Not found.\n", content_type=plain)
                return
            authorized, _editable = self._roster_auth()
            if not authorized:
                self._send_html(
                    401,
                    "Unauthorized: use brigade fleet enroll for read-only dashboard access.\n",
                    content_type=plain,
                )
                return
            try:
                cursor = fleet_work_page.parse_query(query)
            except ValueError:
                self._send_html(400, "Invalid Worklore page query.\n", content_type=plain)
                return
            try:
                conn = open_db(Path(db_path))
                try:
                    page = fleet_work_page.load_page(conn, cursor=cursor)
                finally:
                    conn.close()
            except (ValueError, WorkloreValidationError):
                self._send_html(400, "Invalid Worklore page cursor.\n", content_type=plain)
                return
            except (FleetHubError, sqlite3.Error):
                self._send_html(500, "Worklore task store unavailable.\n", content_type=plain)
                return
            nonce = secrets.token_urlsafe(16)
            self._send_html(200, fleet_work_page.render(page, nonce=nonce, now=datetime.now(timezone.utc)), nonce=nonce)

        def _serve_roster(self, query: str) -> None:
            plain = "text/plain; charset=utf-8"
            params = parse_qs(query, keep_blank_values=False)
            authorized, editable = self._roster_auth()
            if not authorized:
                self._send_html(
                    401,
                    "Unauthorized: use brigade fleet enroll for read-only dashboard access.\n",
                    content_type=plain,
                )
                return
            saved = params.get("saved", [""])[0]
            saved_revision = int(saved) if saved.isdigit() and len(saved) <= 12 else None
            self._render_roster(status=200, editable=editable, saved_revision=saved_revision)

        # --- policy page (UI lane; the JSON /policy API above is untouched) ---

        def _render_policy(
            self,
            *,
            status: int,
            editable: bool,
            banner: str | None = None,
            saved_revision: str = "",
            error: str | None = None,
            plan: Any = None,
            draft: Any = None,
            review: str | None = None,
        ) -> None:
            plain = "text/plain; charset=utf-8"
            try:
                conn = open_db(Path(db_path))
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            try:
                fleet_model_inventory.ensure_schema(conn)
                view = fleet_policy_page.load_view(
                    conn,
                    inventory=policy_inventory() if policy_inventory is not None else None,
                    activation=fleet_policy_page.probe_activation(conn),
                )
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            finally:
                conn.close()
            if saved_revision:
                # Composed here rather than at the redirect: what a save did to
                # runtime depends on the activation state, which is only known
                # once the view is loaded.
                banner = f"saved as revision {saved_revision}. {fleet_policy_page.application_note(view.activation)}"
            nonce = secrets.token_urlsafe(16)
            page = fleet_policy_page.render(
                view,
                nonce=nonce,
                now=datetime.now(timezone.utc),
                csrf=fleet_policy_page.csrf_value(token) if editable else "",
                editable=editable,
                banner=banner,
                error=error,
                plan=plan,
                draft=draft,
                review=review,
            )
            self._send_html(status, page, nonce=nonce)

        def _serve_policy_page(self, query: str) -> None:
            plain = "text/plain; charset=utf-8"
            params = parse_qs(query, keep_blank_values=False)
            authorized, editable = self._roster_auth()
            if not authorized:
                self._send_html(
                    401,
                    "Unauthorized: use brigade fleet enroll for read-only dashboard access.\n",
                    content_type=plain,
                )
                return
            saved = params.get("saved", [""])[0]
            saved_revision = saved if saved.isdigit() and len(saved) <= 12 else ""
            self._render_policy(status=200, editable=editable, saved_revision=saved_revision)

        def _post_policy_page(self) -> None:
            """Same admin-only, same-origin, CSRF-checked path as the roster form."""
            plain = "text/plain; charset=utf-8"
            body = self._read_form_body(fleet_policy_page.MAX_FORM_BYTES, fleet_policy_page.FORM_CONTENT_TYPE)
            if body is None:
                return
            try:
                submission = fleet_policy_page.parse_form(body)
            except fleet_policy_page.FormError as exc:
                self._send_html(400, f"{exc}\n", content_type=plain)
                return
            expected = fleet_policy_page.csrf_value(token)
            if not hmac.compare_digest(submission.csrf.encode("utf-8"), expected.encode("utf-8")):
                self._send_html(403, "form token mismatch: reload the page\n", content_type=plain)
                return
            try:
                conn = open_db(Path(db_path))
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            try:
                fleet_model_inventory.ensure_schema(conn)
                result = fleet_policy_page.apply(
                    conn,
                    submission,
                    actor=fleet_policy_page.UPDATED_BY,
                    inventory=(
                        policy_inventory()
                        if policy_inventory is not None
                        else fleet_policy_page.load_server_inventory(conn)
                    ),
                    activation=fleet_policy_page.probe_activation(conn),
                    review_key=fleet_policy_page.review_secret(token),
                )
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            finally:
                conn.close()
            if result.status == "saved":
                self._send_html(
                    303, "", content_type=plain, extra_headers={"Location": f"/deck/policy?saved={result.revision}"}
                )
                return
            status = {"previewed": 200, "conflict": 409}.get(result.status, 422)
            self._render_policy(
                status=status,
                editable=True,
                banner=result.message if result.status == "previewed" else None,
                error=None if result.status == "previewed" else result.message,
                plan=result.plan,
                draft=submission,
                review=result.review,
            )

        def _read_form_body(self, max_bytes: int, content_type_expected: str) -> bytes | None:
            """Shared form gate: media type, length, admin identity, same origin."""
            plain = "text/plain; charset=utf-8"
            content_type = self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
            if content_type != content_type_expected:
                self._send_html(415, "Unsupported Media Type: send a form body.\n", content_type=plain)
                return None
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_html(400, "bad Content-Length\n", content_type=plain)
                return None
            if length <= 0:
                self._send_html(400, "missing body\n", content_type=plain)
                return None
            if length > max_bytes:
                self._send_html(413, "form body too large\n", content_type=plain)
                return None
            presented = self._bearer()
            if presented is not None:
                try:
                    conn = open_db(Path(db_path))
                except (FleetHubError, sqlite3.Error) as exc:
                    self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                    return None
                try:
                    if self._authorized():
                        is_admin = True
                    else:
                        node_id, _revoked = lookup_node_token(conn, presented)
                        is_admin = False
                        if node_id is None:
                            self._send_html(401, "Unauthorized.\n", content_type=plain)
                            return None
                finally:
                    conn.close()
                if not is_admin:
                    self._send_html(403, "the admin token is required to edit fleet policy\n", content_type=plain)
                    return None
            elif self._cookie_authorized() or self._tailscale_identity_authorized():
                self._send_html(403, "read-only: an admin bearer header is required to edit\n", content_type=plain)
                return None
            else:
                self._send_html(401, "Unauthorized.\n", content_type=plain)
                return None
            if not self._same_origin():
                self._send_html(403, "cross-origin form post refused\n", content_type=plain)
                return None
            return self.rfile.read(length)

        # --- end policy page lane ---

        def _same_origin(self) -> bool:
            site = self.headers.get("Sec-Fetch-Site")
            if site is not None:
                return site.strip().lower() == "same-origin"
            origin = self.headers.get("Origin")
            if origin is None:
                return True
            host = self.headers.get("Host", "")
            origin_host = origin.partition("://")[2].partition("/")[0]
            return bool(host) and hmac.compare_digest(origin_host.encode("utf-8"), host.encode("utf-8"))

        def _post_roster(self) -> None:
            plain = "text/plain; charset=utf-8"
            content_type = self.headers.get("Content-Type", "").partition(";")[0].strip().lower()
            if content_type != fleet_hub_roster_page.FORM_CONTENT_TYPE:
                self._send_html(415, "Unsupported Media Type: send a form body.\n", content_type=plain)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_html(400, "bad Content-Length\n", content_type=plain)
                return
            if length <= 0:
                self._send_html(400, "missing body\n", content_type=plain)
                return
            if length > fleet_hub_roster_page.MAX_FORM_BYTES:
                self._send_html(413, "form body too large\n", content_type=plain)
                return
            presented = self._bearer()
            if presented is not None:
                # A bearer is checked against the database (node tokens) before the
                # body is read, exactly like the JSON routes.
                try:
                    conn = open_db(Path(db_path))
                except (FleetHubError, sqlite3.Error) as exc:
                    self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                    return
                try:
                    if self._authorized():
                        is_admin = True
                    else:
                        node_id, _revoked = lookup_node_token(conn, presented)
                        is_admin = False
                        if node_id is None:
                            self._send_html(401, "Unauthorized.\n", content_type=plain)
                            return
                finally:
                    conn.close()
                if not is_admin:
                    self._send_html(403, "the admin token is required to edit the roster\n", content_type=plain)
                    return
            elif self._cookie_authorized() or self._tailscale_identity_authorized():
                self._send_html(403, "read-only: an admin bearer header is required to edit\n", content_type=plain)
                return
            else:
                self._send_html(401, "Unauthorized.\n", content_type=plain)
                return
            if not self._same_origin():
                self._send_html(403, "cross-origin form post refused\n", content_type=plain)
                return
            raw = self.rfile.read(length)
            try:
                submission = fleet_hub_roster_page.parse_form(raw)
            except fleet_hub_roster_page.FormError as exc:
                self._send_html(400, f"{exc}\n", content_type=plain)
                return
            expected = fleet_hub_roster_page.csrf_value(token)
            if not hmac.compare_digest(submission.csrf.encode("utf-8"), expected.encode("utf-8")):
                self._send_html(403, "form token mismatch: reload the page\n", content_type=plain)
                return
            try:
                conn = open_db(Path(db_path))
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            try:
                result = fleet_hub_roster_page.apply(
                    conn, frozen_deck, submission, activation=fleet_policy_page.probe_activation(conn)
                )
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            finally:
                conn.close()
            if result.status == "saved":
                self._send_html(
                    303, "", content_type=plain, extra_headers={"Location": f"/deck/roster?saved={result.revision}"}
                )
                return
            if result.status == "conflict":
                self._render_roster(status=409, editable=True, error=result.message)
                return
            self._render_roster(status=422, editable=True, error=result.message, submission=submission)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            path, _, query = self.path.partition("?")
            if self._refuse_token_query(query):
                return
            if path.startswith("/deck/actions/"):
                from . import fleet_action_http

                fleet_action_http.route(self, Path(db_path), token)
                return
            if path == "/enroll":
                self._serve_enrollment(query)
                return
            if path == "/dashboard/sessions":
                self._dashboard_api(path, query)
                return
            if path in _ASSET_ROUTES:
                self._serve_asset(path)
                return
            if path == "/health":
                self._send_json(200, {"ok": True, "service": "brigade-fleet-hub"})
                return
            if path == "/deck/roster":
                self._serve_roster(query)
                return
            if path == "/deck/policy":
                self._serve_policy_page(query)
                return
            if path.startswith("/deck/observed/"):
                self._serve_observed_work(path, query)
                return
            if path == "/deck/work":
                self._serve_work(query)
                return
            if path == "/deck/stats":
                fleet_admin_stats_http.serve(self, Path(db_path), query)
                return
            if path == "/deck/owners":
                self._serve_owners_page(query)
                return
            if path == "/" or path in ("/deck", "/deck/repos") or path.startswith("/deck/"):
                self._serve_deck(path, query)
                return
            if path.startswith(_DASHBOARD_PREFIX):
                self._serve_dashboard(path, query)
                return
            if path in (
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
            ):
                if self._bearer() is None:
                    if path in {"/policy", "/policy/status", "/policy/inventory"}:
                        self._send_json(401, {"error": {"code": "auth-failed", "message": "unauthorized"}})
                    else:
                        self._send_json(401, {"error": "unauthorized"})
                    return
                include_all = parse_qs(query).get("all", [""])[0].lower() in ("1", "true", "yes")
                try:
                    conn = open_db(Path(db_path))
                except FleetHubUnsupportedSchema:
                    self._send_json(503, {"error": FleetHubUnsupportedSchema.public_message})
                    return
                except (FleetHubError, sqlite3.Error) as exc:
                    self._send_json(500, {"error": f"hub database error: {exc}"})
                    return
                payload: dict[str, Any]
                try:
                    caller = self._caller(conn)
                    if caller is None:
                        return
                    is_admin, _node = caller
                    presented = self._bearer()
                    if path == "/nodes":
                        if not is_admin:
                            self._send_json(403, {"error": "the admin token is required to manage nodes"})
                            return
                        payload = {"nodes": list_nodes(conn)}
                    elif path == "/status":
                        payload = {
                            "claim_fencing": claim_fencing_status(conn),
                            "runs": latest_status(
                                conn,
                                include_all=include_all,
                                stale_history_after_seconds=frozen_deck.stale_history_after_seconds,
                            ),
                        }
                    elif path == "/cloud":
                        if parse_qs(query).get("view", [""])[0] == "capabilities":
                            # Fixed-size preflight, independent of leases and provider policy rows.
                            payload = {"schema": "brigade.fleet_cloud.v1"}
                        else:
                            payload = cloud_snapshot(
                                conn, frozen_deck, include_all=include_all, include_grokbot=is_admin
                            )
                    elif path == "/models":
                        payload = fleet_hub_model_roster.project_roster(
                            conn,
                            audience_node_id=None if is_admin else _node,
                            raw_node_bearer=None if is_admin else presented,
                        )
                    elif path == "/preference":
                        pref = get_run_preference(conn)
                        payload = {"preference": {key: value for key, value in pref.items() if value is not None}}
                    elif path == "/sessions":
                        payload = {"sessions": list_sessions(conn, include_all=include_all)}
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
                            self._send_json(
                                400,
                                fleet_hub_policy_api.FleetPolicyApiError(
                                    "invalid-request", "limit must be an integer"
                                ).payload(),
                            )
                            return
                        payload = fleet_hub_policy_api.read_policy(conn, history=history, limit=limit)
                    else:
                        payload = {"claims": fleet_claim_display.list_display_claims(conn, include_all=include_all)}
                except ClaimFenceError as exc:
                    self._send_json(503, {"error": str(exc), "reason": exc.reason})
                    return
                except sqlite3.Error as exc:
                    self._send_json(500, {"error": f"hub database error: {exc}"})
                    return
                finally:
                    conn.close()
                self._send_json(200, payload)
                return
            if self._handle_worklore():
                return
            self._send_json(404, {"error": "not found"})

        def do_PUT(self) -> None:  # noqa: N802 - stdlib handler API
            path = self.path.partition("?")[0]
            if path != "/preference":
                self._send_json(404, {"error": "not found"})
                return
            if self._bearer() is None:
                self._send_json(401, {"error": "unauthorized"})
                return
            try:
                conn = open_db(Path(db_path))
            except FleetHubUnsupportedSchema:
                self._send_json(503, {"error": FleetHubUnsupportedSchema.public_message})
                return
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_json(500, {"error": f"hub database error: {exc}"})
                return
            try:
                caller = self._caller(conn)
                if caller is None:
                    return
                is_admin, _node = caller
                if not is_admin:
                    self._send_json(403, {"error": "the admin token is required to set run preference"})
                    return
                parsed = self._read_json_body(MAX_BODY_BYTES)
                if parsed is None:
                    return
                body = parsed.get("preference") if isinstance(parsed, dict) and "preference" in parsed else parsed
                stored = set_run_preference(conn, body, updated_by="admin")
                payload = {"preference": {key: value for key, value in stored.items() if value is not None}}
            except FleetHubError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except sqlite3.Error as exc:
                self._send_json(500, {"error": f"hub database error: {exc}"})
                return
            finally:
                conn.close()
            self._send_json(200, payload)

        def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler API
            path = self.path.partition("?")[0]
            if not path.startswith("/work/"):
                self._send_json(404, {"error": "not found"})
                return
            self._handle_worklore()

        def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
            path = self.path.partition("?")[0]
            if not path.startswith("/work/"):
                self._send_json(404, {"error": "not found"})
                return
            self._handle_worklore()

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            path, _, query = self.path.partition("?")
            if self._refuse_token_query(query):
                return
            if path.startswith("/deck/actions/") or path == "/actions":
                from . import fleet_action_http

                fleet_action_http.route(self, Path(db_path), token)
                return
            if path == "/enroll":
                self._post_enrollment(query)
                return
            if path in ("/dashboard/enrollment", "/dashboard/sessions"):
                self._dashboard_api(path, query)
                return
            if path == "/deck/roster":
                self._post_roster()
                return
            if path == "/deck/policy":
                self._post_policy_page()
                return
            if path == "/stats":
                fleet_admin_stats_http.post(self, Path(db_path), query)
                return
            if path.startswith("/work/"):
                self._handle_worklore()
                return
            if path not in (
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
            ):
                self._send_json(404, {"error": "not found"})
                return
            if self._bearer() is None:
                if path == "/policy":
                    self._send_json(401, {"error": {"code": "auth-failed", "message": "unauthorized"}})
                else:
                    self._send_json(401, {"error": "unauthorized"})
                return
            # The database is opened before the body is read: a node token
            # is resolved against it, and an unauthenticated peer must not
            # get the hub to read its (up to 8 MiB) body first. This is a
            # non-migrating connection (#1161): the schema was created once
            # at server startup.
            try:
                conn = open_db(Path(db_path))
            except FleetHubUnsupportedSchema:
                self._send_json(503, {"error": FleetHubUnsupportedSchema.public_message})
                return
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_json(500, {"error": f"hub database error: {exc}"})
                return
            body_payload: dict[str, Any]
            try:
                caller = self._caller(conn)
                if caller is None:
                    return
                is_admin, caller_node = caller
                if path == "/nodes":
                    if not is_admin:
                        self._send_json(403, {"error": "the admin token is required to manage nodes"})
                        return
                elif path == "/message-principals":
                    if not is_admin:
                        self._send_json(403, {"error": "the admin token is required to enroll message principals"})
                        return
                elif path == "/policy" and is_admin and not allow_admin_writes:
                    # Document writes stay admin. Resolve/prepare/ack need a node token.
                    pass
                elif path in ("/events", "/claims", "/sessions", "/messages") and is_admin and not allow_admin_writes:
                    self._send_json(
                        403,
                        {
                            "error": "the admin token may not post events or claims: enroll this node with "
                            "'brigade fleet nodes add' and configure its node token, or start the hub with "
                            "--allow-admin-writes"
                        },
                    )
                    return
                parsed = self._read_json_body(MAX_BODY_BYTES)
                if parsed is None:
                    return
                if path == "/events":
                    status, body_payload = 200, dict(store_events(conn, parsed, caller_node=caller_node))
                elif path == "/claims":
                    status, body_payload = handle_claim(conn, parsed, caller_node=caller_node)
                elif path == "/sessions":
                    status, body_payload = handle_session(conn, parsed, caller_node=caller_node)
                elif path == "/messages":
                    status, body_payload = fleet_hub_messages.handle(
                        conn,
                        parsed,
                        caller=caller_node,
                        open_conn=lambda: open_db(Path(db_path)),
                        source_ip=self.client_address[0],
                    )
                elif path == "/message-principals":
                    status, body_payload = fleet_hub_messages.handle_principal_admin(conn, parsed)
                elif path == "/cloud":
                    if (
                        is_admin
                        and not allow_admin_writes
                        and (not isinstance(parsed, dict) or parsed.get("action") != "policy")
                    ):
                        self._send_json(
                            403,
                            {
                                "error": "the admin token may not admit, bind, renew, or release cloud leases: enroll this "
                                "node with 'brigade fleet nodes add' and configure its node token, or start the hub with "
                                "--allow-admin-writes"
                            },
                        )
                        return
                    status, body_payload = handle_cloud(conn, parsed, caller_node=caller_node, config=frozen_deck)
                elif path == "/models":
                    status, body_payload = handle_model_policy(conn, parsed, caller_node=caller_node)
                elif path == "/policy":
                    if not isinstance(parsed, dict):
                        self._send_json(
                            400, {"error": {"code": "invalid-request", "message": "body is not valid JSON"}}
                        )
                        return
                    action = parsed.get("action")
                    if action in fleet_hub_policy_api.ADMIN_ACTIONS and not is_admin:
                        self._send_json(
                            403,
                            {
                                "error": {
                                    "code": "auth-failed",
                                    "message": "the admin token is required for policy document writes",
                                }
                            },
                        )
                        return
                    if action in fleet_hub_policy_api.NODE_ACTIONS and is_admin and not allow_admin_writes:
                        self._send_json(
                            403,
                            {
                                "error": {
                                    "code": "auth-failed",
                                    "message": "the admin token may not resolve or prepare policy: enroll this node with "
                                    "'brigade fleet nodes add' and configure its node token, or start the hub with "
                                    "--allow-admin-writes",
                                }
                            },
                        )
                        return
                    status, body_payload = fleet_hub_policy_api.handle_policy(
                        conn, parsed, caller_node=caller_node, is_admin=is_admin
                    )
                elif path == "/grokbot":
                    if (
                        is_admin
                        and not allow_admin_writes
                        and (
                            not isinstance(parsed, dict)
                            or parsed.get("action") not in {"list", "status", "report-metadata", "enroll-actor"}
                        )
                    ):
                        self._send_json(
                            403,
                            {
                                "error": "the admin token may not mutate the Grok Bot queue: enroll this "
                                "node with 'brigade fleet nodes add' and configure its node token, or start the hub with "
                                "--allow-admin-writes"
                            },
                        )
                        return
                    status, body_payload = handle_grokbot(conn, parsed, caller_node=caller_node, config=frozen_deck)
                else:
                    status, body_payload = handle_node_request(conn, parsed)
            except FleetHubUnsupportedSchema:
                self._send_json(503, {"error": FleetHubUnsupportedSchema.public_message})
                return
            except ClaimCardinalityError as exc:
                self._send_json(409, exc.payload())
                return
            except ClaimTargetError as exc:
                self._send_json(exc.status, exc.payload())
                return
            except ClaimFenceError as exc:
                self._send_json(409, {"error": str(exc), "reason": exc.reason, "owner": {}})
                return
            except fleet_hub_policy_api.FleetPolicyApiError as exc:
                self._send_json(exc.status, exc.payload())
                return
            except fleet_hub_messages.MessageConflict as exc:
                payload = {"error": str(exc)}
                if exc.code is not None:
                    payload["code"] = exc.code
                self._send_json(409, payload)
                return
            except fleet_hub_messages.MessageQuotaError as exc:
                payload = {"error": str(exc), "code": exc.code}
                if exc.recipient is not None:
                    payload["recipient"] = exc.recipient
                self._send_json(429, payload)
                return
            except fleet_hub_messages.MessageRecipientInactive as exc:
                self._send_json(
                    400,
                    {"error": str(exc), "code": "message_recipient_inactive", "recipient": exc.recipient},
                )
                return
            except fleet_hub_messages.MessageAuthError as exc:
                self._send_json(403, {"error": str(exc), "code": "message_sender_inactive"})
                return
            except fleet_hub_messages.MessageFutureTimestamp as exc:
                self._send_json(400, {"error": str(exc), "code": "message_timestamp_future"})
                return
            except fleet_hub_messages.MessageForbidden as exc:
                self._send_json(403, {"error": str(exc), "code": exc.code})
                return
            except fleet_hub_messages.MessageError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except FleetHubForbidden as exc:
                self._send_json(403, {"error": str(exc)})
                return
            except FleetHubConflict as exc:
                self._send_json(409, {"error": str(exc)})
                return
            except FleetHubUnprocessable as exc:
                self._send_json(422, {"error": str(exc)})
                return
            except FleetHubError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            except sqlite3.Error as exc:
                self._send_json(500, {"error": f"hub database error: {exc}"})
                return
            finally:
                conn.close()
            self._send_json(status, body_payload)

    return _Handler
