"""Tool-native auth that Brigade carries through a rewrite but never catalogs.

Some tools authenticate a remote MCP server through a key Brigade does not model.
Claude Code's ``headersHelper`` is a command the tool runs to print a header map. The
command is the user's own configuration on that machine, so the catalog does not hold
it, the ownership sidecar does not hold it, and no hash of it is stored anywhere.
Instead a rewrite of the SAME live entry keeps the key when the entry still points at
the same endpoint, and refuses to write when it would not.

Three rules keep that safe:

* Origin binding, fail closed. The helper is generated credentials for one host. It is
  kept only when the entry stays remote and both URLs are strictly canonical and name
  the same origin. A URL that Python and a WHATWG parser (Claude Code runs on Node)
  could read differently has no origin here (``url_origin`` returns None), so it never
  compares equal to anything. Otherwise the projection is blocked for that server
  (``NativeAuthEndpointChanged``), which no flag overrides, rather than sending the
  credentials somewhere new or writing an entry with no auth.
* One credential source. When the live entry holds a helper and the projection would
  also carry catalog headers, the projection is blocked
  (``NativeAuthSourceCollision``) instead of writing both with no signal.
* Fingerprints ignore preserved keys. Ownership fingerprints are computed over the
  entry minus these keys, so a live entry that differs from the projection only by a
  preserved key reads ``current`` and the sidecar carries nothing derived from it.
  An entry with no preserved key hashes exactly as before.

This module also merges Codex native auth into an existing catalog entry on import.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from . import localio
from .mcp_adapters import CanonicalServer

# Allowlist, not "every unknown key": only keys with a known auth meaning are kept, so a
# rewrite still drops stale or unrelated native settings. Only Claude Code defines
# ``headersHelper``; the other JSON adapters have no equivalent key.
PRESERVED_NATIVE_KEYS: dict[str, tuple[str, ...]] = {
    "claude": ("headersHelper",),
    "claude-user": ("headersHelper",),
}

_DEFAULT_PORTS = {"http": 80, "https": 443}
_NATIVE_AUTH_FIELDS = ("http_headers", "env_http_headers", "bearer_token_env_var")
# Anything on the existing catalog entry that can authenticate the server for the merge check.
_EXISTING_AUTH_FIELDS = ("headers", *_NATIVE_AUTH_FIELDS)
_SCHEME_RE = re.compile(r"(https?)://", re.IGNORECASE)
_HOST_RE = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9._-]*[A-Za-z0-9_.-])?")
_V6_RE = re.compile(r"\[([0-9A-Fa-f:.]+)\](?::([0-9]+))?")


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


class NativeAuthSourceCollision(NativeAuthConflict):
    """The live entry authenticates natively and the catalog entry also supplies headers."""

    def __init__(self) -> None:
        ValueError.__init__(
            self,
            "the live entry authenticates through a native helper, but the catalog entry also supplies "
            "headers; two credential sources are ambiguous; nothing was written for this server; remove "
            "the catalog headers or the live helper to continue",
        )
        self.fields = {"reason": "native_auth_source_collision"}


def preserved_keys(harness: str) -> tuple[str, ...]:
    return PRESERVED_NATIVE_KEYS.get(harness, ())


def _split(url: object) -> tuple[str, str, str, int, str] | None:
    """Strictly canonical http(s) URL -> (scheme, authority, host, port, rest); else None.

    ``authority`` is the raw text lowercased with the default port made explicit-free, so two
    URLs name one origin only when that text is identical. No parser-level normalization is
    trusted: Python's ``urlsplit`` and the WHATWG parser disagree on ``\\``, tabs, userinfo,
    percent-encoded hosts and more, and a disagreement would send retained credentials to a
    host the user never approved. Rejected (None): non-str, any whitespace or control or
    backslash anywhere, a non-ASCII or percent-encoded or userinfo-bearing authority, an empty
    or malformed host, a port that is empty, zero, above 65535, signed, padded or non-decimal,
    and a ``?`` or ``#`` right after the authority. Non-ASCII hosts are rejected, never IDNA
    mapped; a trailing dot is kept literally, so ``h.`` and ``h`` are different origins.
    """
    if not isinstance(url, str) or any(ord(c) <= 0x20 or ord(c) == 0x7F or c == "\\" for c in url):
        return None
    match = _SCHEME_RE.match(url)
    if match is None:
        return None
    scheme, tail = match.group(1).lower(), url[match.end() :]
    cut = next((i for i, c in enumerate(tail) if c in "/?#"), len(tail))
    authority, rest = tail[:cut], tail[cut:]
    if rest and rest[0] != "/":
        return None
    if not authority.isascii() or "@" in authority or "%" in authority:
        return None
    v6 = _V6_RE.fullmatch(authority)
    port_text: str
    if v6 is not None:
        try:
            ipaddress.IPv6Address(v6.group(1))
        except ValueError:
            return None
        host, port_text = f"[{v6.group(1).lower()}]", v6.group(2) or ""
    else:
        host, sep, port_text = authority.partition(":")
        if (sep and not port_text) or ":" in port_text or "[" in authority or "]" in authority:
            return None
        if _HOST_RE.fullmatch(host) is None or ".." in host:
            return None
        host = host.lower()
    if port_text and not port_text.isdigit():
        return None
    port = int(port_text) if port_text else _DEFAULT_PORTS[scheme]
    if port_text and (port_text != str(port) or not 0 < port <= 65535):
        return None
    try:  # An independent parser must agree on the pieces; any disagreement is a refusal.
        parts = urlsplit(url)
        if (parts.hostname or "") != host.strip("[]") or (parts.port or _DEFAULT_PORTS[scheme]) != port:
            return None
    except ValueError:
        return None
    shown = host if port == _DEFAULT_PORTS[scheme] else f"{host}:{port}"
    return scheme, shown, host, port, rest


def url_origin(url: object) -> tuple[str, str, int] | None:
    """Scheme, host and port of a strictly canonical http(s) URL; None means "no provable origin"."""
    parts = _split(url)
    return None if parts is None else (parts[0], parts[2], parts[3])


def same_origin(old: object, new: object) -> bool:
    """True only when both URLs are canonical and their raw authority text is identical."""
    a, b = _split(old), _split(new)
    return a is not None and b is not None and a[:4] == b[:4] and a[1] == b[1]


def normalized_url(url: object) -> str | None:
    """Exact-match form: case-folded origin, default port dropped, fragment ignored; None if unsafe."""
    parts = _split(url)
    if parts is None:
        return None
    scheme, authority, _, _, rest = parts
    path, _, query = rest.split("#", 1)[0].partition("?")
    return f"{scheme}://{authority}{path or '/'}" + (f"?{query}" if query else "")


def carry_native_keys(harness: str, projected: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    """Return ``projected`` plus the live entry's preserved keys, or raise when unsafe."""
    kept = {key: live[key] for key in preserved_keys(harness) if live.get(key)}
    if not kept:
        return projected
    if "url" not in projected:
        raise NativeAuthEndpointChanged("native_auth_transport_changed")
    if not same_origin(live.get("url"), projected["url"]):
        raise NativeAuthEndpointChanged("native_auth_origin_changed")
    if projected.get("headers"):
        raise NativeAuthSourceCollision()
    return {**projected, **kept}


def entry_fingerprint(harness: str, entry: dict[str, Any]) -> str:
    """Hash an entry for ownership, ignoring preserved native keys (see module docs)."""
    keys = preserved_keys(harness)
    return localio.stable_hash({k: v for k, v in entry.items() if k not in keys} if keys else entry)


def _has_auth(server: CanonicalServer) -> bool:
    """Any header or native auth setting counts: a header name pattern is not proof of "no auth"."""
    return any(getattr(server, key) for key in _EXISTING_AUTH_FIELDS)


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
