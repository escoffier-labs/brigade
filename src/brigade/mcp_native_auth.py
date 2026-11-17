"""Tool-native auth that Brigade carries through a rewrite but never catalogs.

Some tools authenticate a remote MCP server through a key Brigade does not model.
Claude Code's ``headersHelper`` is a command the tool runs to print a header map. The
command is the user's own configuration on that machine, so the catalog does not hold
it, the ownership sidecar does not hold it, and no hash of it is stored anywhere.
Instead a rewrite of the SAME live entry keeps the key when the entry still points at
the same endpoint, and refuses to write when it would not.

Two rules keep that safe:

* Origin binding. The helper is generated credentials for one host. It is kept only
  when the entry stays remote and its URL origin (scheme, host, port) is unchanged.
  Otherwise the projection is blocked for that server (``NativeAuthEndpointChanged``),
  which no flag overrides, rather than sending the credentials somewhere new or
  writing an entry with no auth.
* Fingerprints ignore preserved keys. Ownership fingerprints are computed over the
  entry minus these keys, so a live entry that differs from the projection only by a
  preserved key reads ``current`` and the sidecar carries nothing derived from it.
  An entry with no preserved key hashes exactly as before.

This module also merges Codex native auth into an existing catalog entry on import.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from . import localio
from .mcp_adapters import CanonicalServer, _sensitive_http_header

# Allowlist, not "every unknown key": only keys with a known auth meaning are kept, so a
# rewrite still drops stale or unrelated native settings. Only Claude Code defines
# ``headersHelper``; the other JSON adapters have no equivalent key.
PRESERVED_NATIVE_KEYS: dict[str, tuple[str, ...]] = {
    "claude": ("headersHelper",),
    "claude-user": ("headersHelper",),
}

_DEFAULT_PORTS = {"http": 80, "https": 443}
_NATIVE_AUTH_FIELDS = ("http_headers", "env_http_headers", "bearer_token_env_var")


class NativeAuthConflict(ValueError):
    """A later generic credential cannot silently shadow retained native auth."""

    def __init__(self, headers: set[str]):
        super().__init__(
            "generic headers collide with retained native authentication; live config unchanged; "
            "use explicit native canonical auth to resolve the collision"
        )
        self.fields: dict[str, Any] = {"reason": "native_auth_collision", "headers": sorted(headers)}


class NativeAuthEndpointChanged(NativeAuthConflict):
    """Retained native auth is bound to an endpoint the projection no longer targets."""

    def __init__(self, reason: str):
        ValueError.__init__(
            self,
            "native auth on the live entry is bound to its current endpoint, but the catalog entry "
            "targets a different one; nothing was written for this server; remove the live entry or "
            "restore its auth by hand to continue",
        )
        self.fields = {"reason": reason}


def preserved_keys(harness: str) -> tuple[str, ...]:
    return PRESERVED_NATIVE_KEYS.get(harness, ())


def url_origin(url: object) -> tuple[str, str, int] | None:
    """Scheme, host and port of an http(s) URL; None when it has no usable origin."""
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url.strip())
        scheme, host, port = parts.scheme.lower(), parts.hostname, parts.port
    except ValueError:
        return None
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    return scheme, host.lower(), port or _DEFAULT_PORTS[scheme]


def normalized_url(url: object) -> str | None:
    """Exact-match form: case-folded origin, default port dropped, fragment ignored."""
    origin = url_origin(url)
    if origin is None:
        return None
    parts = urlsplit(str(url).strip())
    scheme, host, port = origin
    netloc = host if port == _DEFAULT_PORTS[scheme] else f"{host}:{port}"
    return f"{scheme}://{netloc}{parts.path or '/'}" + (f"?{parts.query}" if parts.query else "")


def carry_native_keys(harness: str, projected: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """Return ``projected`` plus the live entry's preserved keys, or raise when unsafe."""
    kept = {key: live[key] for key in preserved_keys(harness) if live.get(key)}
    if not kept:
        return projected
    if "url" not in projected:
        raise NativeAuthEndpointChanged("native_auth_transport_changed")
    old, new = url_origin(live.get("url")), url_origin(projected["url"])
    if old is None or new is None or old != new:
        raise NativeAuthEndpointChanged("native_auth_origin_changed")
    return {**projected, **kept}


def entry_fingerprint(harness: str, entry: dict[str, Any]) -> str:
    """Hash an entry for ownership, ignoring preserved native keys (see module docs)."""
    keys = preserved_keys(harness)
    return localio.stable_hash({k: v for k, v in entry.items() if k not in keys} if keys else entry)


def _has_auth(server: CanonicalServer) -> bool:
    return any(getattr(server, key) for key in _NATIVE_AUTH_FIELDS) or any(
        _sensitive_http_header(name) for name in server.headers
    )


def merge_imported_native_auth(existing: CanonicalServer, imported: CanonicalServer) -> CanonicalServer | None:
    """Give ``existing`` the native auth ``imported`` carries, only when provably the same server.

    Same name is the caller's key. Both must be remote on the exact same normalized URL,
    and ``existing`` must hold no auth of its own: never match on URL alone, never
    overwrite. Returns the updated server, or None when nothing should change.
    """
    if not (existing.is_remote and imported.is_remote) or _has_auth(existing):
        return None
    url = normalized_url(existing.url)
    if url is None or url != normalized_url(imported.url):
        return None
    auth = {key: getattr(imported, key) for key in _NATIVE_AUTH_FIELDS if getattr(imported, key)}
    return replace(existing, **auth) if auth else None
