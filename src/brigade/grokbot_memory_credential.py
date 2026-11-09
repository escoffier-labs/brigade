"""The Grok Bot memory principal's credential for job ``memory_refs`` (#2416).

Grok Bot job references are searched with the Grok Bot memory principal's own
credential (S25), so a job never names a card that principal cannot read. Only
the user-level fleet configuration names that credential::

    # ~/.brigade/fleet.toml (BRIGADE_HOME/fleet.toml when BRIGADE_HOME is set)
    [grokbot_memory]
    token_file = "/absolute/path/to/grokbot-memory-token"

A workspace file never chooses where this credential is read from or where it
is sent: nothing here reads a workspace ``.brigade/`` file, a pack instance
file or a runtime file. The token goes only to the fleet hub that
``[fleet] hub_url`` (or ``BRIGADE_FLEET_HUB_URL``) names.

The token file gets the checks every message credential gets
(``read_message_token_file``): an absolute path, not a symlink, a regular file
owned by this user, no group or world access on POSIX, at most 4 KiB, holding
one valid token. A token equal to this host's node or admin credential is
refused, because references computed with it would follow that credential's
grants instead of the bot's.

When the key is absent or anything is wrong, references stay off and
``resolve`` says why in one fixed sentence. A reason never carries the token,
the file contents or the path. Nothing here logs. Dot, Duet and interactive
memory search do not read this section and keep their own principals.
"""

from __future__ import annotations

import hmac
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

SECTION = "grokbot_memory"
KEY = "token_file"
CONFIG_NAME = "fleet.toml"
_CONFIG_MAX_BYTES = 1 << 20

ON = "on: Grok Bot job references use the grokbot memory principal's credential"
OFF_NOT_CONFIGURED = "off: no [grokbot_memory] token_file in the user-level ~/.brigade/fleet.toml"
OFF_CONFIG_UNREADABLE = "off: the user-level ~/.brigade/fleet.toml could not be read"
OFF_INVALID_KEY = "off: [grokbot_memory] token_file must be a non-empty string"
OFF_NOT_ABSOLUTE = "off: [grokbot_memory] token_file must be an absolute path"
OFF_TOKEN_REFUSED = (
    "off: the grokbot memory token file must be a private regular file owned by this user, "
    "not a symlink, at most 4 KiB, holding one valid token"
)
OFF_NO_HUB = "off: no fleet hub configured ([fleet] hub_url in ~/.brigade/fleet.toml)"
OFF_SHARED_CREDENTIAL = "off: the grokbot memory token must not be this host's node or admin credential"


@dataclass(frozen=True)
class Status:
    """Whether job references are on, and why. ``authority`` is ``(hub_url, token)`` when on."""

    reason: str
    # True when the operator set the key, or the file holding it, and it was refused.
    refused: bool
    authority: tuple[str, str] | None = field(default=None, repr=False, compare=False)

    @property
    def on(self) -> bool:
        return self.authority is not None


def config_path() -> Path:
    from .fleet_client import brigade_home

    return brigade_home() / CONFIG_NAME


def _section() -> tuple[object, str | None]:
    """The ``[grokbot_memory]`` table of the user-level fleet.toml, or a refusal reason."""
    path = config_path()
    try:
        if not path.exists():
            return None, None
        with path.open("rb") as handle:
            raw = handle.read(_CONFIG_MAX_BYTES + 1)
        if len(raw) > _CONFIG_MAX_BYTES:
            return None, OFF_CONFIG_UNREADABLE
        payload = tomllib.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None, OFF_CONFIG_UNREADABLE
    return payload.get(SECTION), None


def resolve() -> Status:
    """The Grok Bot memory credential from the user-level fleet.toml. Never raises, never logs."""
    try:
        return _resolve()
    except Exception:  # noqa: BLE001 - any doubt leaves references off
        return Status(OFF_TOKEN_REFUSED, refused=True)


def _resolve() -> Status:
    from . import fleet_client
    from .fleet_client_message_credentials import FleetMessageHubError, read_message_token_file

    section, problem = _section()
    if problem is not None:
        return Status(problem, refused=True)
    if section is None or (isinstance(section, dict) and KEY not in section):
        return Status(OFF_NOT_CONFIGURED, refused=False)
    value = section.get(KEY) if isinstance(section, dict) else None
    if not isinstance(value, str) or not value.strip():
        return Status(OFF_INVALID_KEY, refused=True)
    path = Path(value.strip()).expanduser()
    if not path.is_absolute():
        return Status(OFF_NOT_ABSOLUTE, refused=True)
    try:
        token = read_message_token_file(str(path), label="grokbot memory")
    except FleetMessageHubError:
        return Status(OFF_TOKEN_REFUSED, refused=True)
    settings = fleet_client.load_fleet_settings()
    hub_url = settings.get("hub_url", "")
    if not hub_url:
        return Status(OFF_NO_HUB, refused=True)
    for other in (settings.get("node_token", ""), settings.get("admin_token", "")):
        if other and hmac.compare_digest(token.encode("utf-8"), other.encode("utf-8")):
            return Status(OFF_SHARED_CREDENTIAL, refused=True)
    return Status(ON, refused=False, authority=(hub_url, token))


def doctor_check() -> dict[str, str]:
    """``memory-refs`` for ``run cloud grokbot doctor``: ok when on, skipped when unset, fail when refused."""
    status = resolve()
    state = "ok" if status.on else "fail" if status.refused else "skipped"
    return {"check": "memory-refs", "status": state, "detail": status.reason}
