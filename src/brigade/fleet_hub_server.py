"""Bound Fleet Hub connections before starting unauthenticated handler threads."""

from http.server import ThreadingHTTPServer
from socket import socket
from socketserver import BaseRequestHandler
import threading
from typing import Any

MAX_HANDLER_THREADS = 32
MAX_HANDLERS_PER_SOURCE = 8


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Close excess connections without blocking the accept loop.

    Source quotas use the TCP peer address, never caller-supplied headers.
    A reverse proxy therefore shares one source quota across its clients.
    Counts include connections waiting for their first request bytes and
    remain reserved through handler execution and socket cleanup.
    """

    def __init__(
        self,
        server_address: tuple[str, int],
        RequestHandlerClass: type[BaseRequestHandler],
        bind_and_activate: bool = True,
    ) -> None:
        self._admission_lock = threading.Lock()
        self._active_requests: dict[socket | tuple[bytes, socket], str] = {}
        self._source_counts: dict[str, int] = {}
        super().__init__(server_address, RequestHandlerClass, bind_and_activate)

    def process_request(self, request: socket | tuple[bytes, socket], client_address: Any) -> None:
        source = client_address[0]
        with self._admission_lock:
            source_count = self._source_counts.get(source, 0)
            admitted = len(self._active_requests) < MAX_HANDLER_THREADS and source_count < MAX_HANDLERS_PER_SOURCE
            if admitted:
                self._active_requests[request] = source
                self._source_counts[source] = source_count + 1
        if not admitted:
            # No HTTP response: request parsing/authentication has not run.
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            # The accept loop owns socket cleanup when thread creation fails.
            self._release(request)
            raise

    def process_request_thread(self, request: socket | tuple[bytes, socket], client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(request)

    def _release(self, request: socket | tuple[bytes, socket]) -> None:
        with self._admission_lock:
            # A late startup interrupt may race a handler that already exited.
            # Pop the reservation once so both cleanup paths cannot decrement it.
            source = self._active_requests.pop(request, None)
            if source is None:
                return
            remaining = self._source_counts[source] - 1
            if remaining:
                self._source_counts[source] = remaining
            else:
                del self._source_counts[source]
