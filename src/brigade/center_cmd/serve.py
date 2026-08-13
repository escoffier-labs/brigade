"""Local operator dashboard server skeleton."""

from __future__ import annotations

import hmac
import ipaddress
import inspect
import secrets
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence
from urllib.parse import parse_qs, urlparse

from brigade.center_cmd.dashboard import render
from brigade.center_cmd.dashboard import timing as center_timing
from brigade.center_cmd.dashboard.snapshot import SnapshotCache
from brigade.center_cmd.dashboard.views import all_views, render_nav, view_by_name

_DEFAULT_PORT = 8765
_VIEW_PREFIX = "/view/"
_LEGACY_VIEW_REDIRECTS = {
    "graph": "/view/work#ready",
    "waves": "/view/work#ready",
    "claims": "/view/work#claims",
    "activity": "/view/work#recent",
}


def _view_cache_key(view_name: str, query: dict[str, str]) -> str:
    if not query:
        return view_name
    encoded = "&".join(f"{key}={value}" for key, value in sorted(query.items()))
    return f"{view_name}?{encoded}"


def _has_port(host: str) -> bool:
    """Return True if a Host header value carries an explicit ``:port``."""
    if host.startswith("["):
        return "]:" in host
    return host.count(":") == 1


def _is_loopback(host: str) -> bool:
    """Return True if *host* resolves only to loopback addresses."""
    lowered = host.strip().lower()
    if lowered == "localhost":
        return True
    try:
        addr = ipaddress.ip_address(lowered)
        return addr.is_loopback
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(lowered, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for family, _, _, _, sockaddr in infos:
        ip_str = sockaddr[0]
        if family == socket.AF_INET6 and "%" in ip_str:
            ip_str = ip_str.split("%", 1)[0]
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if not addr.is_loopback:
            return False
    return True


def validate_bind_security(
    host: str,
    token: str | None,
    allowed_hosts: Sequence[str] | None,
) -> None:
    """Refuse to bind to a non-loopback address without a token and allowed hosts."""
    if _is_loopback(host):
        return
    if not token:
        raise ValueError(
            f"Refusing to bind to non-loopback host {host!r}: set --token and one or more --allowed-host values."
        )
    if not allowed_hosts:
        raise ValueError(
            f"Refusing to bind to non-loopback host {host!r}: --allowed-host is required when a token is set."
        )


class _DashboardHandler(BaseHTTPRequestHandler):
    _token: str | None = None
    _allowed_exact: set[str] = set()
    _allowed_bare: set[str] = set()
    _target: Path = Path(".")
    _snapshots: SnapshotCache | None = None
    _snapshot_wait_ms: float = 2000.0

    def _write_response(
        self,
        status: int,
        body: bytes | str = b"",
        content_type: str = "text/plain; charset=utf-8",
        nonce: str | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        if nonce is None:
            nonce = secrets.token_urlsafe(16)
        csp = (
            f"default-src 'none'; "
            f"script-src 'nonce-{nonce}'; "
            "script-src-attr 'none'; "
            f"style-src 'nonce-{nonce}'; "
            "img-src 'none'; "
            "connect-src 'self'; "
            "base-uri 'none'; "
            "form-action 'none'; "
            "frame-ancestors 'none'"
        )
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Security-Policy", csp)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        timing_header = getattr(self, "_timing_header", None)
        if isinstance(timing_header, str) and timing_header:
            self.send_header("X-Brigade-Center-Timing", timing_header)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _guard(self) -> bool:
        host_header = self.headers.get("Host")
        if host_header is None:
            self._write_response(403, "Forbidden: missing Host header.\n")
            return False
        host = host_header.strip().lower()
        host_bare = host.rsplit(":", 1)[0] if _has_port(host) else host
        if host not in self._allowed_exact and host_bare not in self._allowed_bare:
            self._write_response(403, "Forbidden: invalid Host header.\n")
            return False
        if self._token is not None:
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                self._write_response(401, "Unauthorized.\n")
                return False
            presented = auth[7:].encode("utf-8", "surrogatepass")
            expected = self._token.encode("utf-8", "surrogatepass")
            if not hmac.compare_digest(presented, expected):
                self._write_response(401, "Unauthorized.\n")
                return False
        return True

    def handle_one_request(self) -> None:
        """Parse one request, then guard it before any handler can run.

        The Host allowlist and bearer check live here rather than in each
        ``do_*`` method so that no verb - including ones this class does not
        implement, like OPTIONS or TRACE - can reach a response path that
        skips them or that omits the security headers.
        """
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
            if not self._guard():
                self.wfile.flush()
                return
            handler_name = f"do_{self.command}"
            handler = getattr(self, handler_name, None)
            if handler is None:
                self._write_response(405, "Method not allowed.\n")
                self.wfile.flush()
                return
            handler()
            self.wfile.flush()
        except TimeoutError as exc:
            self.log_error("Request timed out: %r", exc)
            self.close_connection = True

    def _parse_view_name(self, path: str) -> str | None:
        parsed = urlparse(path)
        route = parsed.path
        if route == "/":
            views = all_views()
            return views[0].NAME if views else None
        if not route.startswith(_VIEW_PREFIX):
            return None
        slug = route[len(_VIEW_PREFIX) :]
        if not slug or "/" in slug:
            return None
        return slug

    def _parse_query(self) -> dict[str, str]:
        parsed = urlparse(self.path)
        raw = parse_qs(parsed.query, keep_blank_values=False)
        return {key: values[0] for key, values in raw.items() if values}

    def _render_view(self, view_name: str, nonce: str) -> bytes:
        module = view_by_name(view_name)
        if module is None:
            raise KeyError(view_name)
        cache = self._snapshots if self._snapshots is not None else SnapshotCache()
        # A view that raises must degrade to an inline panel, not a 500. The
        # default error path would answer without the security headers and
        # would put a traceback on the page.
        with center_timing.request_timer(view_name) as timer:
            query = self._parse_query()
            cache_key = _view_cache_key(view_name, query)

            def loader() -> dict:
                fetch = module.fetch
                if "query" in inspect.signature(fetch).parameters:
                    return fetch(self._target, query=query)
                return fetch(self._target)

            with center_timing.phase("fetch"):
                snapshot = cache.get(cache_key, loader, wait_ms=self._snapshot_wait_ms)
            with center_timing.phase("render"):
                try:
                    if snapshot.payload.get("_center_fetch_failed"):
                        fragment = render.error_panel(module.TITLE, "This view failed to render.")
                    elif snapshot.status == "loading":
                        fragment = render.loading_panel(module.TITLE)
                    else:
                        fragment = module.render(snapshot.payload, nonce)
                except Exception:  # noqa: BLE001 - one broken panel must not take the page down
                    fragment = render.error_panel(module.TITLE, "This view failed to render.")
            banner = render.freshness_banner(snapshot, f"/view/{view_name}")
            title = render.esc(f"{module.TITLE} - Brigade Center")
            heading = f'<h1 class="page-title">{render.esc(module.TITLE)}</h1>'
            body = f"{heading}{banner}{fragment}"
            nav = render_nav(view_name)
            reload_ms = 1500 if snapshot.status == "loading" else 30000
            page = render.page(title, nonce, nav, body, reload_ms=reload_ms)
            self._timing_header = timer.header_value()
            return page.encode("utf-8")

    def _legacy_redirect(self, view_name: str | None) -> bool:
        if view_name is None:
            return False
        location = _LEGACY_VIEW_REDIRECTS.get(view_name)
        if location is None:
            return False
        self._write_response(301, extra_headers={"Location": location})
        return True

    def do_GET(self) -> None:
        view_name = self._parse_view_name(self.path)
        if view_name is None:
            self._write_response(404, "Not found.\n")
            return
        if self._legacy_redirect(view_name):
            return
        if view_by_name(view_name) is None:
            self._write_response(404, "Not found.\n")
            return
        nonce = secrets.token_urlsafe(16)
        body = self._render_view(view_name, nonce)
        self._write_response(200, body, "text/html; charset=utf-8", nonce)

    def do_HEAD(self) -> None:
        view_name = self._parse_view_name(self.path)
        if self._legacy_redirect(view_name):
            return
        if view_name is None or view_by_name(view_name) is None:
            self._write_response(404, b"")
            return
        self._write_response(200, b"", "text/html; charset=utf-8")

    def log_message(self, format: str, *args: object) -> None:
        pass


def _make_server(
    *,
    host: str = "127.0.0.1",
    port: int = _DEFAULT_PORT,
    token: str | None = None,
    allowed_hosts: Sequence[str] | None = None,
    target: Path | None = None,
) -> ThreadingHTTPServer:
    """Build and bind the server without starting it."""
    validate_bind_security(host, token, allowed_hosts)

    allowed_exact: set[str] = set()
    allowed_bare: set[str] = set()
    for value in allowed_hosts or []:
        value_lc = value.strip().lower()
        if _has_port(value_lc):
            allowed_exact.add(value_lc)
        else:
            # An operator-supplied name with no port matches any port, which is
            # what a reverse-proxied deployment needs.
            allowed_bare.add(value_lc)

    class _BoundHandler(_DashboardHandler):
        _token = token
        _target = target if target is not None else Path(".")
        _snapshots = SnapshotCache()
        _snapshot_wait_ms = 2000.0

    host_lc = host.strip().lower()
    server = ThreadingHTTPServer((host, port), _BoundHandler)
    server.daemon_threads = True
    actual_host, actual_port = server.server_address[0], server.server_address[1]
    actual_host_lc = str(actual_host).lower()

    # Names the operator can legitimately type for THIS bind. Each is pinned to
    # the bound port: a bare entry would let `Host: 127.0.0.1:1` through, and the
    # bound address is not something an attacker can point a DNS name at.
    names = {host_lc, actual_host_lc}
    if _is_loopback(actual_host_lc):
        names |= {"127.0.0.1", "localhost", "[::1]", "::1"}
    for name in names:
        allowed_exact.add(f"{name}:{actual_port}")
        if actual_port == 80:
            allowed_bare.add(name)

    _BoundHandler._allowed_exact = allowed_exact
    _BoundHandler._allowed_bare = allowed_bare
    return server


def serve(
    *,
    target: Path,
    host: str = "127.0.0.1",
    port: int = _DEFAULT_PORT,
    token: str | None = None,
    allowed_hosts: Sequence[str] | None = None,
) -> int:
    """Start the local operator dashboard server.

    Prints the bound URL and runs until interrupted, then returns 0.
    """
    try:
        server = _make_server(host=host, port=port, token=token, allowed_hosts=allowed_hosts, target=target)
    except ValueError as exc:
        print(f"brigade center serve: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"brigade center serve: cannot bind {host}:{port}: {exc}", file=sys.stderr)
        return 2

    actual_host, actual_port = server.server_address[0], server.server_address[1]
    print(f"http://{actual_host}:{actual_port}/", flush=True)
    print("Read-only dashboard. Press ctrl-c to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
