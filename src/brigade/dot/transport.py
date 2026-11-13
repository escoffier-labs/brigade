"""Shared connector transport validation and protected token reading."""

from __future__ import annotations
import ipaddress
import os
import re
import stat
import urllib.request
from pathlib import Path
from typing import Any

SDK_LOOPBACK_ALLOWED_HOSTS = ("localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "::1", "[::1]:*")


_DIRECT_QUEUE_HUB_TOKEN_MAX_BYTES = 4098


class ConfigurationError(ValueError):
    """Configuration was unsafe or incomplete. Details never reach clients."""


class OptionalDependencyError(RuntimeError):
    """The listener's optional MCP runtime is not installed."""


def parse_bind(value: str) -> tuple[str, int]:
    """Parse a deliberately small host:port surface without IPv6 ambiguity."""
    if not isinstance(value, str) or value.count(":") != 1:
        raise ConfigurationError("invalid")
    host, port_text = value.rsplit(":", 1)
    if not host or not port_text.isdecimal():
        raise ConfigurationError("invalid")
    port = int(port_text)
    if not 1 <= port <= 65_535:
        raise ConfigurationError("invalid")
    return host, port


def load_hub_token_file(path: Path) -> str:
    """Load one configured Fleet Hub listener token from a safe file."""
    return _read_descriptor_safe_token_file(path)


def _read_descriptor_safe_token_file(path: Path) -> str:
    """Read one owner-private token through a no-follow descriptor."""
    no_follow = getattr(os, "O_NOFOLLOW", None)
    owner = getattr(os, "getuid", None)
    if not path.is_absolute() or not isinstance(no_follow, int) or no_follow == 0 or not callable(owner):
        raise ConfigurationError("invalid")

    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0),
        )
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != owner()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_size > _DIRECT_QUEUE_HUB_TOKEN_MAX_BYTES
        ):
            raise ConfigurationError("invalid")
        chunks: list[bytes] = []
        remaining = _DIRECT_QUEUE_HUB_TOKEN_MAX_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        if len(data) > _DIRECT_QUEUE_HUB_TOKEN_MAX_BYTES:
            raise ConfigurationError("invalid")
        return _validate_bearer(data.decode("utf-8").rstrip("\r\n"))
    except ConfigurationError:
        raise
    except (OSError, UnicodeDecodeError, ValueError):
        raise ConfigurationError("invalid") from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                raise ConfigurationError("invalid") from None


def _validate_bearer(value: str) -> str:
    if not value or len(value) > 4096 or "\x00" in value or not value.isascii():
        raise ConfigurationError("invalid")
    return value


def _load_mcp() -> tuple[Any, Any, Any, Any]:
    try:
        from mcp.server import MCPServer
        from mcp.server.transport_security import TransportSecuritySettings
        from starlette.requests import Request
        from starlette.responses import JSONResponse
    except ImportError as exc:
        raise OptionalDependencyError() from exc
    return MCPServer, JSONResponse, Request, TransportSecuritySettings


def _valid_host(value: str) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 253 and all(part for part in value.split("."))


def _valid_origin(value: str) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"https?://[A-Za-z0-9.-]+(?::[1-9][0-9]{0,4})?", value))


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Turn every redirect response into a bounded request failure."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _open_http(request: urllib.request.Request, timeout: int) -> Any:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    return opener.open(request, timeout=timeout)


MAX_CLIENT_ID_LENGTH = 64
INSTANCES = frozenset({"operator", "repository-scout", "implementation-worker"})


def validate_client_id(client_id: object) -> None:
    """Accept a bounded lowercase identifier, never a path or caller actor override."""
    if client_id is not None and (
        not isinstance(client_id, str)
        or len(client_id) > MAX_CLIENT_ID_LENGTH
        or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", client_id) is None
    ):
        raise ConfigurationError("invalid")


def deployment_name(instance: str, client_id: str | None = None) -> str:
    if instance not in INSTANCES:
        raise ConfigurationError("invalid")
    validate_client_id(client_id)
    return instance if client_id is None else f"{instance}-{client_id}"
