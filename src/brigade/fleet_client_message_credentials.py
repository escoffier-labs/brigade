"""Explicit, protected credentials for message-only external principals."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import hmac
import os
import re
import stat
import sys

from . import fleet_client
from .mcp_events import EVENT_ERROR_CODES

ENV_TOKEN_FILE = "BRIGADE_FLEET_MESSAGE_TOKEN_FILE"
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{32,512}")


MESSAGE_CODES = (
    frozenset(
        {
            "message_recipient_not_permitted",
            "message_sender_recipient_quota",
            "message_recipient_quota",
            "message_sender_quota",
            "message_wait_capped",
            "paging_snapshot_capacity",
            "paging_continuation_unsupported",
            "paging_cursor_expired",
            "question_station_busy",
            "question_operator_invalid",
            "question_request_conflict",
            "question_not_owned",
            "message_recipient_inactive",
            "message_recipient_unknown",
            "message_sender_inactive",
            "message_identity_peers_unsupported",
            "message_authorization_unavailable",
            "message_not_found",
            "message_timestamp_future",
            "token_file_invalid",
            "admin_token_refused",
            "token_scope_changed",
            "spool_conflict",
            "client_id_invalid",
            "invalid_argument",
            "hub_unavailable",
            "principal_enrollment_changed",
            "principal_enrollment_revoked",
            "principal_enrollment_grants_changed",
        }
    )
    | EVENT_ERROR_CODES
)


class FleetMessageHubError(fleet_client.FleetClientError):
    """A message operation failure with structured status and code."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code if isinstance(code, str) and code in MESSAGE_CODES else None


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


def message_settings(token_file: str | None, *, node_credential: bool = False) -> tuple[str, str, str]:
    settings = fleet_client.load_fleet_settings()
    hub = settings.get("hub_url", "")
    if not hub:
        raise MessageNotConfiguredError("no fleet hub configured (~/.brigade/fleet.toml [fleet] hub_url)")
    path = None if node_credential else token_file if token_file is not None else os.environ.get(ENV_TOKEN_FILE)
    if path is not None:
        token = read_message_token_file(path)
    else:
        token = settings.get("node_token", "")
        if not token:
            raise MessageNotConfiguredError("no fleet node token configured")
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


def message_scope(hub: str, token: str) -> str:
    """Opaque identity of a (Hub endpoint, credential) pair, framed so no two pairs collide."""
    framed = b"brigade-message-scope-v1"
    # The endpoint is normalized exactly as the transport builds its request URL.
    for part in (hub.rstrip("/"), token):
        raw = part.encode("utf-8")
        framed += len(raw).to_bytes(4, "big") + raw
    return hashlib.sha256(framed).hexdigest()


class MessageAuthorityError(FleetMessageHubError):
    """A closed credential failure, never an outage eligible for queue-success."""


class MessageNotConfiguredError(MessageAuthorityError):
    """An unconfigured node flush is a no-op, without touching its old spool."""


@dataclass(frozen=True)
class MessageAuthority:
    """One operation's immutable transport authority; never put it in persistent data."""

    hub: str = field(repr=False)
    token: str = field(repr=False)
    node_id: str = field(repr=False)
    token_file: str | None = field(default=None, repr=False)
    node_only: bool = field(default=False, repr=False)
    node_credential: bool = field(default=False, repr=False)
    companions: tuple[MessageAuthority, ...] = field(default=(), repr=False)

    @property
    def scope(self) -> str:
        return message_scope(self.hub, self.token)

    @classmethod
    def capture(
        cls, token_file: str | None = None, *, node_only: bool = False, node_credential: bool = False
    ) -> MessageAuthority:
        if node_only and ENV_TOKEN_FILE in os.environ:
            raise MessageAuthorityError("message credential invalid", code="token_file_invalid")
        try:
            hub, token, node_id = (
                message_settings(None, node_credential=True)
                if node_only or node_credential
                else message_settings(token_file)
            )
        except MessageNotConfiguredError:
            raise
        except fleet_client.FleetClientError as error:
            raise MessageAuthorityError(
                "message credential invalid", code=getattr(error, "code", None) or "token_file_invalid"
            ) from None
        return cls(hub, token, node_id, token_file, node_only, node_only or node_credential)

    def validate(self) -> None:
        """Read current credentials only to compare, never to select an operation's authority."""
        if self.node_only and ENV_TOKEN_FILE in os.environ:
            raise MessageAuthorityError("message credential invalid", code="token_file_invalid")
        try:
            hub, token, node_id = (
                message_settings(None, node_credential=True)
                if self.node_credential
                else message_settings(self.token_file)
            )
        except fleet_client.FleetClientError as error:
            raise MessageAuthorityError(
                "message credential invalid", code=getattr(error, "code", None) or "token_file_invalid"
            ) from None
        if not hmac.compare_digest(message_scope(hub, token), self.scope) or node_id != self.node_id:
            raise MessageAuthorityError("message credential changed", code="token_scope_changed")
        for companion in self.companions:
            companion.validate()
