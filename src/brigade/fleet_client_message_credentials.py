"""Explicit, protected credentials for message-only external principals."""

from __future__ import annotations

import hmac
import os
import re
import stat
import sys

from . import fleet_client

ENV_TOKEN_FILE = "BRIGADE_FLEET_MESSAGE_TOKEN_FILE"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,512}")


class FleetMessageHubError(fleet_client.FleetClientError):
    """A message operation failure with structured status and code."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def read_message_token_file(path: str) -> str:
    """Read a bounded private regular file without following its final symlink."""
    fd = None
    try:
        # NONBLOCK prevents a hostile FIFO from blocking before fstat rejects it.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise ValueError
        fd = os.open(path, flags)
        info = os.fstat(fd)
        # Windows synthesizes POSIX mode bits (0666 for a writable file) that chmod
        # cannot clear, and has no euid; keep only the platform-neutral checks there.
        posix = sys.platform != "win32"
        geteuid = getattr(os, "geteuid", None) if posix else None
        if (
            not stat.S_ISREG(info.st_mode)
            or (geteuid is not None and info.st_uid != geteuid())
            or (posix and info.st_mode & 0o077)
            or info.st_size > 4096
        ):
            raise ValueError
        raw = os.read(fd, 4097)
        if len(raw) > 4096:
            raise ValueError
        token = raw.decode("ascii")
        if _TOKEN_RE.fullmatch(token) is None:
            raise ValueError
        return token
    except (OSError, ValueError, UnicodeError):
        raise FleetMessageHubError(
            "message token file must be a private, owned regular file containing a valid token",
            code="token_file_invalid",
        ) from None
    finally:
        if fd is not None:
            os.close(fd)


def message_settings(token_file: str | None) -> tuple[str, str, str]:
    settings = fleet_client.load_fleet_settings()
    hub = settings.get("hub_url", "")
    if not hub:
        raise fleet_client.FleetClientError("no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
    path = token_file if token_file is not None else os.environ.get(ENV_TOKEN_FILE)
    if path is not None:
        token = read_message_token_file(path)
    else:
        token = settings.get("node_token", "")
        if not token:
            raise fleet_client.FleetClientError("no fleet node token configured")
    admin = settings.get("admin_token", "")
    if admin and hmac.compare_digest(token.encode("utf-8"), admin.encode("utf-8")):
        raise FleetMessageHubError(
            "admin credentials cannot be used for message operations", code="admin_token_refused"
        )
    try:
        scope = fleet_client.resolve_node_id()
    except fleet_client.FleetClientError:
        if path is None:
            raise
        scope = "message-principal"
    if path is not None and scope == "unknown":
        scope = "message-principal"
    return hub, token, scope
