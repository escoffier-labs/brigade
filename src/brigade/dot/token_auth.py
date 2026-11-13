"""Protected bearer authentication for the generic connector only."""

import hmac
import os
import secrets
import re
from pathlib import Path
from typing import Any, Mapping

from .access_auth import AuthError, bounded_headers
from .contracts import DotError
from .runtime_config import AuthConfig


def read_token(path: Path) -> bytes:
    from brigade.fleet_client_message_credentials import read_message_token_file

    try:
        value = read_message_token_file(str(path)).encode("ascii")
        if not re.fullmatch(rb"[A-Za-z0-9_-]{43,512}", value) or any(
            value == (value[:n] * (len(value) // n)) for n in range(1, 9)
        ):
            raise ValueError
        return value
    except Exception:
        raise DotError("invalid_config") from None


def ensure_listener_token(path: Path) -> bool:
    """Create a private credential without overwriting an existing file."""
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(secrets.token_urlsafe(48))
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        raise DotError("invalid_config") from None
    return True


def require_credential_isolation(
    listener: Path, hub_token_file: Path | None = None, *, value: bytes | None = None, planned: bool = False
) -> None:
    """Refuse shared authority without returning credential material."""
    from brigade import fleet_client, fleet_home_guard

    try:
        if value is None and not planned:
            value = read_token(listener)
        settings = fleet_client.load_fleet_settings(strict=True)
        config_path, section = fleet_home_guard.user_fleet_config(strict=True)
        paths = [hub_token_file] if hub_token_file is not None else []
        if config_path is not None:
            for key in ("node_token_file", "token_file"):
                raw = section.get(key)
                if isinstance(raw, str) and raw.strip():
                    path = fleet_home_guard.token_path(raw, config_path)
                    if path is None:
                        raise ValueError
                    paths.append(path)
        for path in paths:
            if listener.resolve() == path.resolve() or (
                path.exists() and listener.exists() and listener.samefile(path)
            ):
                raise ValueError
            if path.exists():
                with path.open("rb") as stream:
                    other = stream.read(4097)
                if len(other) > 4096 or (value is not None and hmac.compare_digest(value, other.strip())):
                    raise ValueError
        for key in ("node_token", "admin_token"):
            if value is not None and settings.get(key) and hmac.compare_digest(value, settings[key].encode("utf-8")):
                raise ValueError
    except Exception:
        raise DotError("invalid_config") from None


class TokenVerifier:
    """No network fallback, no stored token value and fixed refusal diagnostics."""

    def __init__(self, auth: AuthConfig, *, hub_token_file: Path | None = None):
        if auth.mode != "token" or auth.token_file is None:
            raise DotError("invalid_config")
        read_token(auth.token_file)
        self.config = auth
        self.hub_token_file = hub_token_file
        self.validated = True
        self.metadata_url = self.resource = self.authorization_server = ""

    async def verify(self, scope: Mapping[str, Any]) -> str:
        try:
            values = [v for k, v in bounded_headers(scope) if k.lower() == b"authorization"]
            if len(values) != 1 or not re.fullmatch(rb"Bearer [A-Za-z0-9._~+/-]+=*", values[0]):
                raise ValueError
            assert self.config.token_file is not None
            value = read_token(self.config.token_file)
            require_credential_isolation(self.config.token_file, self.hub_token_file, value=value)
            if not hmac.compare_digest(values[0][7:], value):
                raise ValueError
            return self.config.approved_email
        except Exception:
            raise AuthError() from None
