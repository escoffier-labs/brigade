"""Loopback-only HTTP server for the read-only Brigade Run View."""

from __future__ import annotations

import json
import re
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from . import runs_cmd

_MAX_LIST_LIMIT = 100
_MAX_RESPONSE_BYTES = 1_000_000
_MAX_SSE_RECORD_BYTES = 262_144
_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_HTTP_METHOD = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
_WATCH_SCHEMA = "brigade.run-watch.v1"


class RunViewHTTPServer(ThreadingHTTPServer):
    """A foreground server bound only to one loopback listener."""

    daemon_threads = True

    def __init__(self, workspace: Path) -> None:
        workspace = workspace.expanduser().resolve()
        if not workspace.is_dir():
            raise ValueError(f"--cwd is not a directory: {workspace}")
        self.workspace = workspace
        super().__init__(("127.0.0.1", 0), RunViewRequestHandler)

    @property
    def authority(self) -> str:
        return f"127.0.0.1:{self.server_port}"

    def validated_runs_root(self) -> Path | None:
        """Return a regular runs directory, or its safe missing path."""
        brigade_dir = self.workspace / ".brigade"
        runs_dir = brigade_dir / "runs"
        try:
            if brigade_dir.is_symlink():
                return None
            if brigade_dir.exists():
                if not brigade_dir.is_dir():
                    return None
                brigade_root = brigade_dir.resolve(strict=True)
                if brigade_root.parent != self.workspace:
                    return None
            else:
                return runs_dir

            if runs_dir.is_symlink():
                return None
            if not runs_dir.exists():
                return runs_dir
            if not runs_dir.is_dir():
                return None
            resolved_runs_dir = runs_dir.resolve(strict=True)
        except OSError:
            return None
        return resolved_runs_dir if resolved_runs_dir.parent == brigade_root else None


class RunViewRequestHandler(BaseHTTPRequestHandler):
    """Serve only the Run View's document and read-only command contracts."""

    server: RunViewHTTPServer
    protocol_version = "HTTP/1.1"

    def __getattr__(self, name: str) -> Any:
        """Reject every valid HTTP method without a dedicated handler."""
        if name.startswith("do_") and _HTTP_METHOD.fullmatch(name.removeprefix("do_")):
            return self._method_not_allowed
        raise AttributeError(name)

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        nonce = secrets.token_urlsafe(16)
        if not self._valid_host():
            self._error(400, "request is unavailable", nonce)
            return

        try:
            parsed = urlsplit(self.path)
        except ValueError:
            self._error(400, "request is unavailable", nonce)
            return
        if parsed.scheme or parsed.netloc:
            self._error(400, "request is unavailable", nonce)
            return
        if parsed.path == "/":
            if parsed.query:
                self._error(404, "not found", nonce)
                return
            self._document(nonce)
            return
        if parsed.path == "/api/runs":
            self._runs_list(parsed.query, nonce)
            return
        if parsed.path.startswith("/api/runs/"):
            self._run_route(parsed.path, parsed.query, nonce)
            return
        self._error(404, "not found", nonce)

    def _valid_host(self) -> bool:
        return self.headers.get_all("Host") == [self.server.authority]

    def _valid_origin(self) -> bool:
        return self.headers.get_all("Origin") in (None, [f"http://{self.server.authority}"])

    def _document(self, nonce: str) -> None:
        try:
            from .run_view_app import render_document

            document = render_document(nonce)
        except Exception:
            self._error(503, "Run View is unavailable", nonce)
            return
        if not isinstance(document, str):
            self._error(503, "Run View is unavailable", nonce)
            return
        self._send(200, document.encode("utf-8"), "text/html; charset=utf-8", nonce)

    def _runs_list(self, query: str, nonce: str) -> None:
        limit = self._limit(query)
        if limit is None:
            self._error(400, "invalid limit", nonce)
            return
        runs_root = self.server.validated_runs_root()
        if runs_root is None:
            self._error(503, "runs are unavailable", nonce)
            return
        try:
            payload = runs_cmd.runs_list_payload(
                cwd=self.server.workspace,
                runs_dir=runs_root,
                limit=limit,
                missing_root_as_empty=True,
            )
        except (OSError, ValueError):
            self._error(503, "runs are unavailable", nonce)
            return
        self._json(200, payload, nonce)

    def _run_route(self, path: str, query: str, nonce: str) -> None:
        if query:
            self._error(404, "not found", nonce)
            return
        suffix = path.removeprefix("/api/runs/")
        parts = suffix.split("/")
        if len(parts) == 2 and parts[1] == "events":
            self._events(parts[0], nonce)
            return
        if len(parts) == 1:
            self._run_detail(parts[0], nonce)
            return
        self._error(404, "not found", nonce)

    def _run_detail(self, run_id: str, nonce: str) -> None:
        run = self._run_dir(run_id)
        if run is None:
            self._error(404, "run is unavailable", nonce)
            return
        try:
            payload = runs_cmd.run_detail_payload(run[0])
        except (OSError, ValueError):
            self._error(404, "run is unavailable", nonce)
            return
        self._json(200, payload, nonce)

    def _events(self, run_id: str, nonce: str) -> None:
        if not self._valid_origin():
            self._error(403, "request is unavailable", nonce)
            return
        run = self._run_dir(run_id)
        if run is None:
            self._error(404, "run is unavailable", nonce)
            return
        self.send_response(200)
        self._security_headers(nonce)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Connection", "close")
        self.end_headers()

        def record_sink(record: dict[str, object]) -> None:
            self._send_sse_record(record)

        try:
            runs_cmd.watch(
                run_id,
                cwd=self.server.workspace,
                runs_dir=run[1],
                json_output=True,
                record_sink=record_sink,
                recover_artifact_collection=False,
            )
        except (BrokenPipeError, ConnectionResetError):
            return
        except (OSError, ValueError):
            self._send_sse_record({"schema": _WATCH_SCHEMA, "type": "error"})

    def _run_dir(self, run_id: str) -> tuple[Path, Path] | None:
        if "%" in run_id or not _RUN_ID.fullmatch(run_id):
            return None
        root = self.server.validated_runs_root()
        if root is None:
            return None
        try:
            if not root.is_dir():
                return None
            candidate = root / run_id
            if candidate.is_symlink() or not candidate.is_dir():
                return None
            resolved = candidate.resolve(strict=True)
        except OSError:
            return None
        if resolved.parent != root:
            return None
        return resolved, root

    @staticmethod
    def _limit(query: str) -> int | None:
        if not query:
            return 10
        parsed = parse_qs(query, keep_blank_values=True)
        if set(parsed) != {"limit"} or len(parsed["limit"]) != 1:
            return None
        value = parsed["limit"][0]
        if not re.fullmatch(r"[1-9][0-9]{0,2}", value):
            return None
        limit = int(value)
        return limit if limit <= _MAX_LIST_LIMIT else None

    def _method_not_allowed(self) -> None:
        nonce = secrets.token_urlsafe(16)
        if not self._valid_host():
            self._error(400, "request is unavailable", nonce)
            return
        self._send(405, b'{"error":"method not allowed"}', "application/json; charset=utf-8", nonce, allow="GET")

    def _json(self, status: int, payload: dict[str, object], nonce: str) -> None:
        try:
            body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        except (TypeError, ValueError):
            self._error(503, "response is unavailable", nonce)
            return
        self._send(status, body, "application/json; charset=utf-8", nonce)

    def _error(self, status: int, message: str, nonce: str) -> None:
        body = json.dumps({"error": message}, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", nonce)

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        nonce: str,
        *,
        allow: str | None = None,
    ) -> None:
        if len(body) > _MAX_RESPONSE_BYTES:
            body = b'{"error":"response is unavailable"}'
            status = 503
            content_type = "application/json; charset=utf-8"
        self.send_response(status)
        self._security_headers(nonce)
        if allow is not None:
            self.send_header("Allow", allow)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _security_headers(self, nonce: str) -> None:
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; "
            f"script-src 'nonce-{nonce}'; "
            f"style-src 'nonce-{nonce}'; "
            "connect-src 'self'; img-src 'self'; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")

    def _send_sse_record(self, record: dict[str, object]) -> None:
        try:
            encoded = json.dumps(record, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        except (TypeError, ValueError):
            return
        if len(encoded) > _MAX_SSE_RECORD_BYTES:
            return
        self.wfile.write(b"data: " + encoded + b"\n\n")
        self.wfile.flush()

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the foreground command output limited to its bound URL."""


def create_server(cwd: Path) -> RunViewHTTPServer:
    """Bind a Run View server to a random loopback port."""
    return RunViewHTTPServer(cwd)


def serve(cwd: Path, *, no_open: bool = False) -> int:
    """Run the foreground server until Ctrl+C closes its listener."""
    try:
        server = create_server(cwd)
    except (OSError, ValueError) as exc:
        print(f"error: unable to bind Run View: {exc}", file=sys.stderr)
        return 2
    url = f"http://{server.authority}/"
    print(url, flush=True)
    if not no_open:
        try:
            webbrowser.open(url)
        except OSError:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
