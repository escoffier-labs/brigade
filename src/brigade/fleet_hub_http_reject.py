"""HTTP handler hooks for bounded cleanup after rejected uploads."""

import socket
import ssl
import time
from http.server import BaseHTTPRequestHandler
from io import BufferedIOBase
from typing import Any, cast


# Rejected uploads may finish within these bounds during handler cleanup.
# Neither a huge declaration nor a trickling peer may retain a handler forever.
_REJECT_DRAIN_BYTES = 1024 * 1024
_REJECT_DRAIN_SECONDS = 0.25


class _RequestBodyReader:
    """Count body reads while preserving the buffered stream's other operations."""

    def __init__(self, stream: BufferedIOBase) -> None:
        self.stream = stream
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size)
        self.bytes_read += len(data)
        return data

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


class RejectedUploadMixin(BaseHTTPRequestHandler):
    """Preserve rejection responses while discarding bounded late body bytes."""

    def parse_request(self) -> bool:
        if not super().parse_request():
            return False
        # Header parsing is complete. All Hub body readers use read(),
        # including the delegated JSON and action adapters.
        self.rfile = cast(BufferedIOBase, _RequestBodyReader(self.rfile))
        return True

    def _has_unread_body(self) -> bool:
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get_all("Transfer-Encoding", []) or len(lengths) != 1:
            return True
        try:
            length = int(lengths[0])
        except ValueError:
            return True
        return length < 0 or length > getattr(self.rfile, "bytes_read", 0)

    def send_response(self, code: int, message: str | None = None) -> None:
        super().send_response(code, message)
        headers = getattr(self, "headers", None)
        self._rejected_body = (
            code >= 400
            and headers is not None
            and bool(headers.get_all("Content-Length", []) or headers.get_all("Transfer-Encoding", []))
        )
        self._drain_on_close = self._rejected_body and self._has_unread_body()
        if self._rejected_body:
            self.close_connection = True

    def end_headers(self) -> None:
        # send_error supplies its own Connection header after send_response.
        if getattr(self, "_rejected_body", False) and not any(
            line.lower().startswith(b"connection:") for line in getattr(self, "_headers_buffer", [])
        ):
            self.send_header("Connection", "close")
        super().end_headers()

    def finish(self) -> None:
        try:
            if getattr(self, "_drain_on_close", False):
                self._drain_rejected_body()
        finally:
            super().finish()

    def _drain_rejected_body(self) -> None:
        """Flush the rejection, then discard bounded late upload bytes.

        A full close with unread socket data can reset the connection while
        the client is still sending, hiding the HTTP rejection. This shared
        cleanup also covers auth, framing and delegated-route refusals.
        Bytes already buffered by rfile are out of the kernel receive queue,
        so read directly from the socket without waiting for a declared size
        that may be invalid, ambiguous, enormous, or already partly read.
        """
        previous_timeout = self.connection.gettimeout()
        try:
            self.wfile.flush()
            # A TCP half-close on SSLSocket bypasses TLS close_notify and
            # can hide the rejection behind SSLEOFError during an upload.
            # TLS drains first and uses the server's normal close path.
            # socketserver.shutdown_request still calls SHUT_WR at close.
            if not isinstance(self.connection, ssl.SSLSocket):
                self.connection.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + _REJECT_DRAIN_SECONDS
            remaining = _REJECT_DRAIN_BYTES
            while remaining > 0:
                seconds = deadline - time.monotonic()
                if seconds <= 0:
                    break
                self.connection.settimeout(seconds)
                chunk = self.connection.recv(min(65536, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
        except OSError:
            # Timeouts and peers that have already closed need no cleanup.
            pass
        finally:
            self.connection.settimeout(previous_timeout)
