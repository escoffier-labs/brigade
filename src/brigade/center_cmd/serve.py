"""Local operator dashboard server skeleton."""

from __future__ import annotations

import hmac
import html
import ipaddress
import secrets
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Sequence

_DEFAULT_PORT = 8765

_INDEX_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style nonce="{nonce}">
body {{
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  line-height: 1.5;
  margin: 2rem;
  color: #111;
  background: #fff;
}}
h1 {{
  font-size: 1.5rem;
  color: #0066cc;
}}
button {{
  background: #0066cc;
  color: #fff;
  border: none;
  padding: 0.5rem 1rem;
  border-radius: 0.25rem;
  cursor: pointer;
}}
</style>
</head>
<body>
<h1>{title}</h1>
<p>Dashboard views will land in a follow-up.</p>
<button type="button" data-action="demo">Show greeting</button>
<script nonce="{nonce}">
document.addEventListener("DOMContentLoaded", function() {{
  document.body.addEventListener("click", function(e) {{
    var target = e.target.closest('[data-action="demo"]');
    if (!target) return;
    target.textContent = "Hello from the dashboard skeleton";
  }});
}});
</script>
</body>
</html>
"""


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

    def _write_response(
        self,
        status: int,
        body: bytes | str = b"",
        content_type: str = "text/plain; charset=utf-8",
        nonce: str | None = None,
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
        self.send_header("Content-Length", str(len(data)))
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

    def _render_index(self, nonce: str) -> bytes:
        title = html.escape("Brigade Center", quote=True)
        nonce_escaped = html.escape(nonce, quote=True)
        page = _INDEX_TEMPLATE.format(title=title, nonce=nonce_escaped)
        return page.encode("utf-8")

    def do_GET(self) -> None:
        if self.path != "/":
            self._write_response(404, "Not found.\n")
            return
        nonce = secrets.token_urlsafe(16)
        body = self._render_index(nonce)
        self._write_response(200, body, "text/html; charset=utf-8", nonce)

    def do_HEAD(self) -> None:
        if self.path != "/":
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
        server = _make_server(host=host, port=port, token=token, allowed_hosts=allowed_hosts)
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
