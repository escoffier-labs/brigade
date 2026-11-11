"""Bounded host resolution for the fleet hub watchdog."""

from __future__ import annotations

import ipaddress
import socket
import threading


def resolve_host(host: str) -> frozenset[str]:
    """The numeric addresses the configured `--host` resolves to.

    brigade-hub binds the first address `getaddrinfo` gives for that name,
    with no family preference, so it reports one of these. Resolved once at
    startup. An unresolvable name gives the empty set, and then only loopback
    and wildcard reports are accepted.
    """
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return frozenset()
    resolved = set()
    for *_, sockaddr in infos:
        try:
            resolved.add(ipaddress.ip_address(str(sockaddr[0]).split("%", 1)[0]).compressed)
        except ValueError:
            continue
    return frozenset(resolved)


RESOLVE_TIMEOUT = 3.0


def resolve_with_deadline(host: str) -> tuple[frozenset[str], str | None]:
    result: list[frozenset[str]] = []

    def lookup() -> None:
        try:
            result.append(resolve_host(host))
        except Exception:  # Resolver failures must not start the child.
            result.append(frozenset())

    worker = threading.Thread(target=lookup, daemon=True)
    worker.start()
    worker.join(RESOLVE_TIMEOUT)
    if worker.is_alive():
        return frozenset(), "timed out"
    if not result or not result[0]:
        return frozenset(), "failed"
    return result[0], None
