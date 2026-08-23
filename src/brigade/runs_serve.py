"""Foreground loopback Run View over the versioned runs JSON contracts.

The HTTP layer calls ``runs_cmd`` serializers only. It does not read run
artifacts itself. Bind is loopback-only; the process stays in the
foreground and is never installed as a service.
"""

from __future__ import annotations

import ipaddress
import json
import re
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from . import runs_cmd

_LOOPBACK_HOST = "127.0.0.1"
_DEFAULT_LIST_LIMIT = 10
_MAX_LIST_LIMIT = 200
_SERVED_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_API_RUNS_PREFIX = "/api/runs"
_CSP_NONCE_PLACEHOLDER = "__BRIGADE_CSP_NONCE__"
_INDEX_PATH = Path(__file__).with_name("runs_view.html")
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT"})

INDEX_HTML = _INDEX_PATH.read_text(encoding="utf-8")


def _bound_address(server: ThreadingHTTPServer) -> tuple[str, int]:
    host, port = server.server_address[0], server.server_address[1]
    if isinstance(host, (bytes, bytearray)):
        host = host.decode("ascii")
    return str(host), int(port)


def _is_loopback_host(host: str) -> bool:
    lowered = host.strip().lower()
    if lowered in {"localhost", "127.0.0.1", "::1", "[::1]"}:
        return True
    try:
        return ipaddress.ip_address(lowered.strip("[]")).is_loopback
    except ValueError:
        return False


def resolve_served_run_id(run_id: str, runs_root: Path) -> Path | None:
    """Resolve *run_id* strictly beneath *runs_root*.

    Rejects absolute paths, parent traversal, symlink escape, alternate
    roots, and malformed identifiers. This is a location check only; the
    caller must still load the run through ``runs_cmd`` serializers.
    """
    if not isinstance(run_id, str) or not _SERVED_RUN_ID.fullmatch(run_id):
        return None
    try:
        root = runs_root.resolve()
    except OSError:
        return None
    if not root.is_dir():
        return None
    from . import node as node_mod
    from . import run_id as run_id_mod

    workspace = node_mod.infer_workspace_from_runs_dir(root)
    for name in run_id_mod.lookup_names_for_workspace(run_id, workspace):
        if not _SERVED_RUN_ID.fullmatch(name):
            continue
        resolved = _resolve_contained_run_dir(root, name)
        if resolved is not None:
            return resolved
    return None


def _resolve_contained_run_dir(root: Path, run_id: str) -> Path | None:
    candidate = root / run_id
    try:
        if candidate.is_symlink():
            return None
    except OSError:
        return None
    try:
        resolved = candidate.resolve()
    except OSError:
        return None
    if resolved.name != run_id:
        return None
    try:
        resolved.relative_to(root)
    except ValueError:
        return None
    if resolved.parent != root:
        return None
    if not resolved.is_dir():
        return None
    return resolved


def _runs_root_for_serve(cwd: Path, runs_dir: Path | None) -> Path:
    cwd = cwd.expanduser().resolve()
    if runs_dir is not None:
        return runs_dir.expanduser()
    return cwd / ".brigade" / "runs"


class _RunViewHandler(BaseHTTPRequestHandler):
    _cwd: Path = Path(".")
    _runs_dir: Path | None = None
    _allowed_hosts: set[str] = set()
    _bound_origin: str = "http://127.0.0.1"
    protocol_version = "HTTP/1.1"

    def _security_headers(self, nonce: str | None = None) -> tuple[dict[str, str], str]:
        if nonce is None:
            nonce = secrets.token_urlsafe(16)
        return {
            "Content-Security-Policy": (
                f"default-src 'none'; "
                f"script-src 'nonce-{nonce}'; "
                "script-src-attr 'none'; "
                f"style-src 'nonce-{nonce}'; "
                "img-src 'none'; "
                "font-src 'none'; "
                "object-src 'none'; "
                "connect-src 'self'; "
                "base-uri 'none'; "
                "form-action 'none'; "
                "frame-ancestors 'none'"
            ),
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Cache-Control": "no-store",
            "Permissions-Policy": "clipboard-read=(), clipboard-write=()",
        }, nonce

    def _write_response(
        self,
        status: int,
        body: bytes | str = b"",
        content_type: str = "text/plain; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
        nonce: str | None = None,
    ) -> None:
        headers, used_nonce = self._security_headers(nonce)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for key, value in headers.items():
            self.send_header(key, value)
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _write_json(
        self,
        status: int,
        payload: dict[str, object],
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, indent=2, sort_keys=True)
        self._write_response(status, body, "application/json; charset=utf-8", extra_headers)

    def _host_allowed(self) -> bool:
        host_header = self.headers.get("Host")
        if host_header is None:
            self._write_response(403, "Forbidden: missing Host header.\n")
            return False
        host = host_header.strip().lower()
        if host not in self._allowed_hosts:
            self._write_response(403, "Forbidden: invalid Host header.\n")
            return False
        return True

    def _origin_allowed(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        normalized = origin.strip().rstrip("/").lower()
        if normalized != self._bound_origin.lower():
            self._write_response(403, "Forbidden: unexpected Origin.\n")
            return False
        return True

    def handle_one_request(self) -> None:
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ""
                self.request_version = ""
                self.command = ""
                self.send_error(414)
                return
            if not self.raw_requestline:
                self.close_connection = True
                return
            if not self.parse_request():
                return
            if not self._host_allowed():
                self.wfile.flush()
                return
            if self.command in _MUTATING_METHODS:
                self._write_response(405, "Method not allowed.\n", extra_headers={"Allow": "GET, HEAD"})
                self.wfile.flush()
                return
            handler_name = f"do_{self.command}"
            handler = getattr(self, handler_name, None)
            if handler is None:
                self._write_response(405, "Method not allowed.\n", extra_headers={"Allow": "GET, HEAD"})
                self.wfile.flush()
                return
            handler()
            self.wfile.flush()
        except TimeoutError as exc:
            self.log_error("Request timed out: %r", exc)
            self.close_connection = True

    def _route(self) -> tuple[str, str | None, str | None]:
        parsed = urlparse(self.path)
        route = unquote(parsed.path)
        if route != "/" and route.endswith("/"):
            route = route.rstrip("/")
        if route == "/":
            return "app", None, None
        if route == _API_RUNS_PREFIX:
            return "list", None, None
        if not route.startswith(_API_RUNS_PREFIX + "/"):
            return "missing", None, None
        rest = route[len(_API_RUNS_PREFIX) + 1 :]
        if rest.endswith("/events"):
            run_id = rest[: -len("/events")]
            return "events", run_id, None
        if "/" in rest:
            return "missing", None, None
        return "detail", rest, None

    def _serve_app(self) -> None:
        nonce = secrets.token_urlsafe(16)
        body = INDEX_HTML.replace(_CSP_NONCE_PLACEHOLDER, nonce)
        self._write_response(200, body, "text/html; charset=utf-8", nonce=nonce)

    def _serve_list(self) -> None:
        parsed = urlparse(self.path)
        raw_limit = parse_qs(parsed.query).get("limit", [str(_DEFAULT_LIST_LIMIT)])
        try:
            limit = int(raw_limit[0])
        except (TypeError, ValueError, IndexError):
            self._write_response(400, "error: limit must be a positive integer\n")
            return
        if limit < 1 or limit > _MAX_LIST_LIMIT:
            self._write_response(400, "error: limit must be a positive integer\n")
            return
        payload, diagnostic, _rc = runs_cmd.runs_list_contract(
            cwd=self._cwd,
            runs_dir=self._runs_dir,
            limit=limit,
        )
        if payload is None:
            self._write_response(400, (diagnostic or "error: invalid list request") + "\n")
            return
        extra = None
        if diagnostic:
            cleaned = runs_cmd._clean_str(diagnostic, 400)
            if cleaned:
                extra = {"X-Brigade-Diagnostic": cleaned}
        self._write_json(200, payload, extra)

    def _resolved_run(self, run_id: str | None) -> Path | None:
        if run_id is None:
            return None
        root = _runs_root_for_serve(self._cwd, self._runs_dir)
        return resolve_served_run_id(run_id, root)

    def _serve_detail(self, run_id: str | None) -> None:
        run_dir = self._resolved_run(run_id)
        if run_dir is None:
            self._write_response(404, "error: run not found\n")
            return
        payload, diagnostic, _rc = runs_cmd.run_detail_contract(run_dir)
        if payload is None:
            extra = None
            if diagnostic:
                cleaned = runs_cmd._clean_str(diagnostic, 400)
                if cleaned:
                    extra = {"X-Brigade-Diagnostic": cleaned}
            self._write_response(502, (diagnostic or "error: run unreadable") + "\n", extra_headers=extra)
            return
        self._write_json(200, payload)

    def _serve_events(self, run_id: str | None) -> None:
        if not self._origin_allowed():
            return
        run_dir = self._resolved_run(run_id)
        if run_dir is None:
            self._write_response(404, "error: run not found\n")
            return
        headers, _nonce = self._security_headers()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            self.connection.settimeout(None)
        except OSError:
            pass
        closed = False

        def _should_stop() -> bool:
            return closed

        try:
            for record in runs_cmd.iter_watch_records(run_dir, interval=0.25, should_stop=_should_stop):
                line = f"data: {json.dumps(record, sort_keys=True)}\n\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            closed = True
            self.close_connection = True

    def do_GET(self) -> None:
        kind, run_id, _ = self._route()
        if kind == "app":
            self._serve_app()
            return
        if kind == "list":
            self._serve_list()
            return
        if kind == "detail":
            self._serve_detail(run_id)
            return
        if kind == "events":
            self._serve_events(run_id)
            return
        self._write_response(404, "Not found.\n")

    def do_HEAD(self) -> None:
        kind, run_id, _ = self._route()
        if kind == "app":
            nonce = secrets.token_urlsafe(16)
            self._write_response(200, b"", "text/html; charset=utf-8", nonce=nonce)
            return
        if kind in {"list", "detail", "events"}:
            self._write_response(200, b"", "application/json; charset=utf-8")
            return
        self._write_response(404, b"")

    def log_message(self, format: str, *args: object) -> None:
        return


def make_server(
    *,
    cwd: Path,
    runs_dir: Path | None = None,
    host: str = _LOOPBACK_HOST,
    port: int = 0,
) -> ThreadingHTTPServer:
    """Bind the Run View server without starting it."""
    if not _is_loopback_host(host):
        raise ValueError(f"runs serve binds to loopback only: {host}")
    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        raise ValueError(f"--cwd is not a directory: {cwd}")

    class _BoundHandler(_RunViewHandler):
        _cwd = cwd
        _runs_dir = runs_dir

    server = ThreadingHTTPServer((host, port), _BoundHandler)
    server.daemon_threads = True
    actual_host, actual_port = _bound_address(server)
    allowed = {
        f"{actual_host.lower()}:{actual_port}",
        f"{_LOOPBACK_HOST}:{actual_port}",
        f"localhost:{actual_port}",
    }
    _BoundHandler._allowed_hosts = allowed
    _BoundHandler._bound_origin = f"http://{actual_host}:{actual_port}"
    return server


def serve(
    *,
    cwd: Path,
    runs_dir: Path | None = None,
    host: str = _LOOPBACK_HOST,
    port: int = 0,
    open_browser: bool = True,
    open_url: Callable[[str], None] | None = None,
) -> int:
    """Print the bound URL and serve until Ctrl+C. Never backgrounds."""
    try:
        server = make_server(cwd=cwd, runs_dir=runs_dir, host=host, port=port)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"error: cannot bind {host}:{port}: {exc}", file=sys.stderr)
        return 2

    actual_host, actual_port = _bound_address(server)
    url = f"http://{actual_host}:{actual_port}/"
    print(url, flush=True)
    print("Read-only Run View. Press Ctrl+C to stop.", flush=True)
    if open_browser:
        try:
            if open_url is not None:
                open_url(url)
            else:
                import webbrowser

                webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001 - browser launch must not kill the view
            print(f"warning: could not open browser: {exc}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
