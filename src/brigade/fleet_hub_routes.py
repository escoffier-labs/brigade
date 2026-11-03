"""Route registration seam for the Fleet Hub HTTP handler.

``fleet_hub_http.make_handler`` builds one :class:`RouteTable` per handler
class. Route modules add their routes through a ``register_routes(routes, hub)``
function, so a new route lives in its own module instead of growing the
handler's dispatch chain.

Lookup is order independent: an exact path wins, then the longest matching
prefix. Registering the same method and path (or prefix) twice raises, so two
modules cannot silently claim one route. The handler keeps everything that
applies before lookup (the token-in-query refusal) and the shared
``{"error": "not found"}`` answer when nothing matches.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .fleet_command_deck import DeckConfig
    from .fleet_hub_sealed import StoragePolicy
    from .fleet_policy_page import Inventory

# ``handler(request, path, query)``: ``request`` is the live
# ``BaseHTTPRequestHandler``, ``path`` and ``query`` are its split request target.
RouteHandler = Callable[[Any, str, str], None]


@dataclass(frozen=True)
class HubContext:
    """The startup-frozen settings a route needs, captured by ``make_handler``.

    ``open_db`` resolves ``fleet_hub_http.open_db`` at call time, so a test
    that replaces that name still observes every connection a route opens.
    """

    token: str
    db_path: Path
    allow_admin_writes: bool
    deck: DeckConfig
    storage_policy: StoragePolicy
    policy_inventory: Callable[[], Inventory] | None
    open_db: Callable[[Path], sqlite3.Connection]


class RouteTable:
    """Exact and prefix routes keyed by HTTP method."""

    def __init__(self) -> None:
        self._exact: dict[tuple[str, str], RouteHandler] = {}
        self._prefixes: dict[str, list[tuple[str, RouteHandler]]] = {}

    def add(
        self,
        methods: str | Iterable[str],
        handler: RouteHandler,
        *,
        paths: Iterable[str] = (),
        prefixes: Iterable[str] = (),
    ) -> None:
        """Register ``handler`` for each method on exact ``paths`` and ``prefixes``."""
        for method in (methods,) if isinstance(methods, str) else tuple(methods):
            for path in paths:
                if (method, path) in self._exact:
                    raise ValueError(f"route already registered: {method} {path}")
                self._exact[(method, path)] = handler
            entries = self._prefixes.setdefault(method, [])
            for prefix in prefixes:
                if not prefix or any(existing == prefix for existing, _ in entries):
                    raise ValueError(f"route prefix already registered or empty: {method} {prefix!r}")
                entries.append((prefix, handler))
            entries.sort(key=lambda entry: len(entry[0]), reverse=True)

    def find(self, method: str, path: str) -> RouteHandler | None:
        """The handler for ``method`` and ``path``, or ``None`` when nothing matches."""
        handler = self._exact.get((method, path))
        if handler is not None:
            return handler
        for prefix, prefixed in self._prefixes.get(method, ()):
            if path.startswith(prefix):
                return prefixed
        return None

    def dispatch(self, request: Any, method: str, path: str, query: str) -> bool:
        """Run the matching route and return ``True``, or return ``False`` untouched."""
        handler = self.find(method, path)
        if handler is None:
            return False
        handler(request, path, query)
        return True

    def registered(self) -> list[tuple[str, str]]:
        """``(method, route)`` pairs, a prefix written with a trailing ``*``."""
        exact = [(method, path) for method, path in self._exact]
        prefixed = [(method, prefix + "*") for method, entries in self._prefixes.items() for prefix, _ in entries]
        return sorted(exact + prefixed)
