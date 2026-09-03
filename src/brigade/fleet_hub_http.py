"""HTTP adapter for the Fleet Hub domain service.

Keeping request parsing, authentication, and HTML rendering outside the
SQLite domain module makes the hub's security boundary easier to inspect.
"""

import hmac
import importlib.resources
import json
import re
import secrets
import sqlite3
from datetime import datetime, timezone
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode

from . import fleet_command_deck, fleet_dashboard, fleet_hub_grokbot, fleet_hub_sessions, worklore_http
from . import fleet_hub as _hub
from . import fleet_hub_model_roster
from . import fleet_hub_roster_page
from . import fleet_hub_status
from .fleet_hub import (
    DASHBOARD_COOKIE,
    DASHBOARD_COOKIE_MAX_AGE,
    MAX_BODY_BYTES,
    FleetHubConflict,
    FleetHubError,
    FleetHubForbidden,
    FleetHubUnprocessable,
    _ASSET_CACHE_CONTROL,
    _ASSET_CONTENT_TYPES,
    _ASSET_ROUTES,
    _DASHBOARD_PREFIX,
    _TAILSCALE_IDENTITY_HEADER,
    _TAILSCALE_IDENTITY_MAX_LEN,
    dashboard_cookie_value,
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


def _board_more_href(path: str, params: dict[str, list[str]], *, next_offset: int) -> str:
    next_params = {key: list(values) for key, values in params.items()}
    next_params["offset"] = [str(next_offset)]
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


def make_handler(
    token: str,
    db_path: Path,
    *,
    allow_admin_writes: bool = False,
    deck_config: fleet_command_deck.DeckConfig | None = None,
    trust_tailscale_identity: bool = False,
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
                self._send_json(401, {"error": "unauthorized"})
                return None
            if revoked:
                self._send_json(401, {"error": "unauthorized: node token revoked"})
                return None
            return False, node_id

        def _cookie_authorized(self) -> bool:
            header = self.headers.get("Cookie", "")
            if not header:
                return False
            jar: SimpleCookie = SimpleCookie()
            try:
                jar.load(header)
            except CookieError:
                return False
            morsel = jar.get(DASHBOARD_COOKIE)
            if morsel is None:
                return False
            return hmac.compare_digest(morsel.value.encode("utf-8"), dashboard_cookie_value(token).encode("utf-8"))

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
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json(400, {"error": "bad Content-Length"})
                return None
            limit = 1048576 if path == "/work/imports" else 262144
            if self.command == "DELETE" and length == 0:
                return {}
            if length <= 0 or length > limit:
                self._send_json(400, {"error": "missing or oversized body"})
                return None
            raw = self.rfile.read(length)
            try:
                parsed = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self._send_json(400, {"error": "body is not valid JSON"})
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

        def _send_json(self, status: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
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
            self.send_header("Referrer-Policy", "no-referrer")
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
            presented = params.pop("token", [""])[0]
            if presented:
                if not hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8")):
                    self._send_html(401, "Unauthorized.\n", content_type=plain)
                    return
                # Redirect to the same page without the token so it does not
                # linger in the address bar; the view is validated above, so
                # the Location is always one of our own relative routes.
                cookie = (
                    f"{DASHBOARD_COOKIE}={dashboard_cookie_value(token)}; Path=/; HttpOnly; "
                    f"SameSite=Strict; Max-Age={DASHBOARD_COOKIE_MAX_AGE}"
                )
                rest = urlencode(params, doseq=True)
                location = path + (f"?{rest}" if rest else "")
                self._send_html(303, "", content_type=plain, extra_headers={"Location": location, "Set-Cookie": cookie})
                return
            if not (self._authorized() or self._cookie_authorized() or self._tailscale_identity_authorized()):
                self._send_html(
                    401,
                    "Unauthorized: send the fleet bearer token, or open this page once with "
                    "?token=<fleet token> to set the read-only dashboard cookie.\n",
                    content_type=plain,
                )
                return
            try:
                # Non-migrating connection (#1161): the schema exists because
                # server startup created it; this open is read/write only.
                conn = open_db(Path(db_path))
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            try:
                include_all = _board_include_all(params)
                offset = fleet_hub_status.clamp_board_offset(params.get("offset", ["0"])[0])
                board = board_status(
                    conn,
                    include_all=include_all,
                    offset=offset,
                    history_window_seconds=frozen_deck.stale_history_after_seconds,
                    stale_history_after_seconds=frozen_deck.stale_history_after_seconds,
                )
                claims = list_claims(conn)
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
            except sqlite3.Error as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            finally:
                conn.close()
            more_href = _board_more_href(path, params, next_offset=board.offset + board.limit) if board.more else None
            nonce = secrets.token_urlsafe(16)
            page = fleet_dashboard.render_page(
                view=view,
                query_string=urlencode(params, doseq=True),
                runs=board.runs,
                claims=claims,
                nodes=nodes,
                started_at=started_at,
                nonce=nonce,
                more_href=more_href,
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
            """Command Deck HTML (/, /deck, /deck/repos): the same enrollment,
            redirect, bearer-or-cookie authorization, and security headers as
            ``_serve_dashboard``; non-token query parameters are ignored,
            never reflected. Renders from the startup-frozen deck config."""
            plain = "text/plain; charset=utf-8"
            if path in ("/", "/deck"):
                render = fleet_command_deck.render_deck
            elif path == "/deck/repos":
                render = fleet_command_deck.render_repos
            else:
                self._send_html(404, "Not found.\n", content_type=plain)
                return
            params = parse_qs(query, keep_blank_values=False)
            presented = params.pop("token", [""])[0]
            if presented:
                if not hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8")):
                    self._send_html(401, "Unauthorized.\n", content_type=plain)
                    return
                cookie = (
                    f"{DASHBOARD_COOKIE}={dashboard_cookie_value(token)}; Path=/; HttpOnly; "
                    f"SameSite=Strict; Max-Age={DASHBOARD_COOKIE_MAX_AGE}"
                )
                self._send_html(303, "", content_type=plain, extra_headers={"Location": path, "Set-Cookie": cookie})
                return
            if not (self._authorized() or self._cookie_authorized() or self._tailscale_identity_authorized()):
                self._send_html(
                    401,
                    "Unauthorized: send the fleet bearer token, or open this page once with "
                    "?token=<fleet token> to set the read-only dashboard cookie.\n",
                    content_type=plain,
                )
                return
            try:
                conn = open_db(Path(db_path))
            except (FleetHubError, sqlite3.Error) as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            try:
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
            except sqlite3.Error as exc:
                self._send_html(500, f"hub database error: {exc}\n", content_type=plain)
                return
            finally:
                conn.close()
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
            )
            nonce = secrets.token_urlsafe(16)
            page = render(view, nonce=nonce, now=now)
            self._send_html(200, page, nonce=nonce)

        def _roster_auth(self) -> tuple[bool, bool]:
            """``(authorized, editable)``: bearer/cookie edit; Tailscale identity reads."""
            if self._authorized() or self._cookie_authorized():
                return True, True
            if self._tailscale_identity_authorized():
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
                view = fleet_hub_roster_page.load_view(conn, frozen_deck)
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
                csrf=fleet_hub_roster_page.csrf_value(token),
                editable=editable,
                banner=banner,
                error=error,
                submission=submission,
            )
            self._send_html(status, page, nonce=nonce)

        def _serve_roster(self, query: str) -> None:
            plain = "text/plain; charset=utf-8"
            params = parse_qs(query, keep_blank_values=False)
            presented = params.pop("token", [""])[0]
            if presented:
                if not hmac.compare_digest(presented.encode("utf-8"), token.encode("utf-8")):
                    self._send_html(401, "Unauthorized.\n", content_type=plain)
                    return
                cookie = (
                    f"{DASHBOARD_COOKIE}={dashboard_cookie_value(token)}; Path=/; HttpOnly; "
                    f"SameSite=Strict; Max-Age={DASHBOARD_COOKIE_MAX_AGE}"
                )
                self._send_html(
                    303, "", content_type=plain, extra_headers={"Location": "/deck/roster", "Set-Cookie": cookie}
                )
                return
            authorized, editable = self._roster_auth()
            if not authorized:
                self._send_html(
                    401,
                    "Unauthorized: send the fleet bearer token, or open this page once with "
                    "?token=<fleet token> to set the dashboard cookie.\n",
                    content_type=plain,
                )
                return
            saved = params.get("saved", [""])[0]
            saved_revision = int(saved) if saved.isdigit() and len(saved) <= 12 else None
            self._render_roster(status=200, editable=editable, saved_revision=saved_revision)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            path, _, query = self.path.partition("?")
            if path in _ASSET_ROUTES:
                self._serve_asset(path)
                return
            if path == "/health":
                self._send_json(200, {"ok": True, "service": "brigade-fleet-hub"})
                return
            if path == "/deck/roster":
                self._serve_roster(query)
                return
            if path == "/" or path in ("/deck", "/deck/repos") or path.startswith("/deck/"):
                self._serve_deck(path, query)
                return
            if path.startswith(_DASHBOARD_PREFIX):
                self._serve_dashboard(path, query)
                return
            if path in ("/status", "/claims", "/nodes", "/cloud", "/models", "/preference", "/sessions"):
                if self._bearer() is None:
                    self._send_json(401, {"error": "unauthorized"})
                    return
                include_all = parse_qs(query).get("all", [""])[0].lower() in ("1", "true", "yes")
                try:
                    conn = open_db(Path(db_path))
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
                            "runs": latest_status(
                                conn,
                                include_all=include_all,
                                stale_history_after_seconds=frozen_deck.stale_history_after_seconds,
                            )
                        }
                    elif path == "/cloud":
                        payload = cloud_snapshot(conn, frozen_deck, include_all=include_all, include_grokbot=is_admin)
                    elif path == "/models":
                        payload = fleet_hub_model_roster.project_roster(
                            conn,
                            audience_node_id=None if is_admin else _node,
                            raw_node_bearer=None if is_admin else presented,
                        )
                    elif path == "/preference":
                        payload = {"preference": get_run_preference(conn)}
                    elif path == "/sessions":
                        payload = {"sessions": list_sessions(conn, include_all=include_all)}
                    else:
                        payload = {"claims": list_claims(conn, include_all=include_all)}
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
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._send_json(400, {"error": "bad Content-Length"})
                    return
                if length <= 0 or length > MAX_BODY_BYTES:
                    self._send_json(400, {"error": "missing or oversized body"})
                    return
                raw = self.rfile.read(length)
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send_json(400, {"error": "body is not valid JSON"})
                    return
                body = parsed.get("preference") if isinstance(parsed, dict) and "preference" in parsed else parsed
                payload = {"preference": set_run_preference(conn, body, updated_by="admin")}
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
            path = self.path.partition("?")[0]
            if path.startswith("/work/"):
                self._handle_worklore()
                return
            if path not in ("/events", "/claims", "/nodes", "/cloud", "/models", "/grokbot", "/sessions"):
                self._send_json(404, {"error": "not found"})
                return
            if self._bearer() is None:
                self._send_json(401, {"error": "unauthorized"})
                return
            # The database is opened before the body is read: a node token
            # is resolved against it, and an unauthenticated peer must not
            # get the hub to read its (up to 8 MiB) body first. This is a
            # non-migrating connection (#1161): the schema was created once
            # at server startup.
            try:
                conn = open_db(Path(db_path))
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
                elif path in ("/events", "/claims", "/sessions") and is_admin and not allow_admin_writes:
                    self._send_json(
                        403,
                        {
                            "error": "the admin token may not post events or claims: enroll this node with "
                            "'brigade fleet nodes add' and configure its node token, or start the hub with "
                            "--allow-admin-writes"
                        },
                    )
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self._send_json(400, {"error": "bad Content-Length"})
                    return
                if length <= 0 or length > MAX_BODY_BYTES:
                    self._send_json(400, {"error": "missing or oversized body"})
                    return
                raw = self.rfile.read(length)
                try:
                    parsed = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    self._send_json(400, {"error": "body is not valid JSON"})
                    return
                if path == "/events":
                    status, body_payload = 200, dict(store_events(conn, parsed, caller_node=caller_node))
                elif path == "/claims":
                    status, body_payload = handle_claim(conn, parsed, caller_node=caller_node)
                elif path == "/sessions":
                    status, body_payload = handle_session(conn, parsed, caller_node=caller_node)
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
