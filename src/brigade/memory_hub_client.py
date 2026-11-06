"""Client for hub memory, `POST /memory` (S14).

A workspace opts in through a `[hub]` table in `.brigade/memory-care.toml`:

    [hub]
    mode = "read"                       # off (default), read or sync
    scope = "repo:me/app"               # where `sync` publishes; fleet, repo:<id> or principal:<id>
    read_scopes = ["repo:me/app"]       # what search, card reads and the replica may use; default [scope]
    replica_max_mb = 50                 # cap on the replica's heads.json
    hub_url = "https://hub.example.test:3774"      # optional: memory's own hub, with node_token_file
    node_token_file = "~/.brigade/memory-node-token"  # optional: needs hub_url

Which hub memory talks to, from highest precedence:

1. `BRIGADE_MEMORY_HUB_URL` and `BRIGADE_MEMORY_HUB_TOKEN_FILE`, set together.
2. `[hub] hub_url` and `[hub] node_token_file`, set together, in the workspace.
3. The fleet settings: `[fleet] hub_url` (or `BRIGADE_FLEET_HUB_URL`) and this
   machine's node token (`BRIGADE_FLEET_NODE_TOKEN` or `[fleet] node_token_file`).

A memory override replaces both fleet values for hub memory only, so fleet
coordination can stay on one hub while memory lives on another. `brigade fleet`
commands never read it. The override's token file must be a private, owned
regular file (the check the message credentials use), a relative path resolves
against the workspace, and the URL gets the fleet hub URL's scheme rule: https,
or http on loopback. The replica is keyed by hub identity, so pointing memory at
another hub discards the old replica and rebuilds it. `HubSettings.source` says
which of the three won (`env`, `[hub]` or `fleet`), and `memory hub status`
reports it.

The client never reads the shared admin token (`BRIGADE_FLEET_TOKEN`): without a
node token the hub client is unavailable. The node-token slot can still hold
the admin token by mistake, and the hub then answers as its admin. `status`,
`pull`, `push`, `import`, `card` and `proposals drop` ask the hub's `status`
first and refuse when it reports `credential: "admin"`, so Dot and Duet
`memory_card`, which run `card`, refuse too. Search (`memory search`, and Dot
and Duet `memory_search`) makes no `status` call, to stay inside its latency
budget, so it cannot tell: with the admin token in the node slot it reads
every card the admin may read inside `read_scopes`, including scopes this
node holds no grant for. It writes and replicates nothing. Tokens are sent as
a bearer header and never logged, printed or stored.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import threading
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

MODES = ("off", "read", "sync")
CONFIG_REL = ".brigade/memory-care.toml"
SEARCH_BUDGET_SECONDS = 0.8
CALL_TIMEOUT_SECONDS = 10.0
DEFAULT_REPLICA_MAX_MB = 50
ENV_HUB_URL = "BRIGADE_MEMORY_HUB_URL"
ENV_HUB_TOKEN_FILE = "BRIGADE_MEMORY_HUB_TOKEN_FILE"
SOURCE_ENV, SOURCE_TABLE, SOURCE_FLEET = "env", "[hub]", "fleet"
READ_SCOPES_MAX = 64
# Answers that say "not now" rather than "no": they spool and use the replica.
_UNAVAILABLE_STATUSES = {408, 429}
CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CARD_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}")
_EVENT_ID = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}")


class HubConfigError(ValueError):
    """The `[hub]` table is malformed."""


class HubUnavailable(Exception):
    """The hub did not serve memory: transport failure, timeout, 3xx, 404, 408, 429 or 503."""

    def __init__(self, reason: str, *, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


class HubRefusal(Exception):
    """The hub answered with a memory 4xx (or a coded 5xx) refusal."""

    def __init__(self, status: int, body: dict[str, Any]) -> None:
        self.status = status
        self.body = body
        self.code = str(body.get("code") or "")
        self.error = str(body.get("error") or "")
        super().__init__(f"{status} {self.code or 'refused'}: {self.error}")

    @property
    def denies_access(self) -> bool:
        """401 or 403: the hub refused this credential, so nothing it served before may stand in."""
        return self.status in (401, 403)


@dataclass(frozen=True)
class HubSettings:
    mode: str = "off"
    scope: str | None = None
    read_scopes: tuple[str, ...] = ()
    replica_max_bytes: int = DEFAULT_REPLICA_MAX_MB * 1024 * 1024
    hub_url: str = ""
    token: str = field(default="", repr=False)
    source: str = SOURCE_FLEET  # where hub_url and token came from: env, [hub] or fleet

    @property
    def enabled(self) -> bool:
        return self.mode != "off"


ADMIN_CREDENTIAL_REFUSAL = (
    "the hub says this credential is its admin token; hub memory runs only as a node or principal "
    "credential, so set BRIGADE_FLEET_NODE_TOKEN or [fleet] node_token_file (or the memory hub override's "
    "node_token_file) to this machine's node token"
)


def refuse_admin(status_body: dict[str, Any]) -> dict[str, Any]:
    """`status_body` as is, unless the hub reports the admin credential: then HubConfigError."""
    if status_body.get("credential") == "admin":
        raise HubConfigError(ADMIN_CREDENTIAL_REFUSAL)
    return status_body


def valid_scope(scope: object) -> bool:
    if not isinstance(scope, str):
        return False
    if scope == "fleet":
        return True
    for prefix in ("repo:", "principal:"):
        if scope.startswith(prefix):
            rest = scope[len(prefix) :]
            return 0 < len(rest) <= 200 and all(33 <= ord(c) <= 126 and c not in '*"\\' for c in rest)
    return False


def valid_card_id(card_id: object) -> bool:
    return isinstance(card_id, str) and _CARD_ID.fullmatch(card_id) is not None


def read_config_text(target: Path) -> str | None:
    """The memory-care config text; None when absent. Raises OSError or UnicodeDecodeError as is."""
    try:
        return (target / CONFIG_REL).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def read_hub_table(target: Path) -> dict[str, Any]:
    try:
        raw = read_config_text(target)
    except (OSError, UnicodeDecodeError) as exc:
        raise HubConfigError(f"invalid memory-care config: {exc}") from exc
    if raw is None:
        return {}
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise HubConfigError(f"invalid memory-care config: {exc}") from exc
    table = data.get("hub", {})
    if not isinstance(table, dict):
        raise HubConfigError("[hub] must be a table")
    return table


def hub_mode(target: Path) -> str:
    """The configured mode, without touching fleet settings: `off` when unset."""
    mode = read_hub_table(target).get("mode", "off")
    if mode not in MODES:
        raise HubConfigError(f"[hub] mode must be one of: {', '.join(MODES)}")
    return str(mode)


def _read_scopes(table: dict[str, Any], scope: str | None) -> tuple[str, ...]:
    raw = table.get("read_scopes")
    if raw is None:
        return (scope,) if scope else ()
    if not isinstance(raw, list) or not 1 <= len(raw) <= READ_SCOPES_MAX or not all(valid_scope(item) for item in raw):
        raise HubConfigError(
            f"[hub] read_scopes must be a list of 1 to {READ_SCOPES_MAX} scopes: fleet, repo:<identity> or principal:<id>"
        )
    return tuple(dict.fromkeys(raw))


def _table_override(table: dict[str, Any]) -> tuple[str, str] | None:
    """`[hub] hub_url` and `node_token_file` as given, or None when neither is set. Reads no file."""
    values: dict[str, str] = {}
    for key in ("hub_url", "node_token_file"):
        if key not in table:
            continue
        raw = table[key]
        if not isinstance(raw, str) or not raw.strip():
            raise HubConfigError(f"[hub] {key} must be a non-empty string")
        values[key] = raw.strip()
    if not values:
        return None
    for key, other in (("hub_url", "node_token_file"), ("node_token_file", "hub_url")):
        if key in values and other not in values:
            raise HubConfigError(f"[hub] {other} is missing: hub_url and node_token_file must be set together")
    return values["hub_url"], values["node_token_file"]


def _memory_hub_override(table: dict[str, Any], target: Path) -> tuple[str, str, str] | None:
    """`(hub_url, token, source)` when memory has its own hub, else None to use the fleet settings.

    The environment pair outranks the `[hub]` pair. Each pair is all or nothing,
    and an empty environment value counts as unset, as for the fleet variables.
    """
    from . import fleet_client
    from .fleet_client_message_credentials import FleetMessageHubError, read_message_token_file

    env_url = os.environ.get(ENV_HUB_URL, "").strip()
    env_file = os.environ.get(ENV_HUB_TOKEN_FILE, "").strip()
    if env_url or env_file:
        for name, other, value in (
            (ENV_HUB_URL, ENV_HUB_TOKEN_FILE, env_url),
            (ENV_HUB_TOKEN_FILE, ENV_HUB_URL, env_file),
        ):
            if not value:
                raise HubConfigError(f"{other} is set but {name} is missing: set both or neither")
        url, token_file, source, url_name, file_name = env_url, env_file, SOURCE_ENV, ENV_HUB_URL, ENV_HUB_TOKEN_FILE
    else:
        pair = _table_override(table)
        if pair is None:
            return None
        url, token_file = pair
        source, url_name, file_name = SOURCE_TABLE, "[hub] hub_url", "[hub] node_token_file"
    try:
        fleet_client._require_encrypted_or_loopback_hub(url)
    except fleet_client.FleetClientError as exc:
        raise HubConfigError(f"{url_name}: {exc}") from exc
    if not urllib.parse.urlsplit(url).hostname:
        raise HubConfigError(f"{url_name} names no host")
    path = Path(token_file).expanduser()
    if not path.is_absolute():
        path = target / path
    try:
        token = read_message_token_file(str(path), label="memory hub node")
    except FleetMessageHubError as exc:
        raise HubConfigError(f"{file_name} {path}: {exc}") from exc
    return url.rstrip("/"), token, source


def load_settings(target: Path, *, fleet: dict[str, str] | None = None) -> HubSettings:
    """Settings for `target`. `fleet` stands in for the fleet settings: `hub_url` and `node_token`.

    A memory override (see the module docstring) replaces the fleet values, and
    the fleet settings are then not read at all. In `off` mode neither the
    environment pair nor a token file is consulted.
    """
    table = read_hub_table(target)
    unknown = sorted(set(table) - {"mode", "scope", "read_scopes", "replica_max_mb", "hub_url", "node_token_file"})
    if unknown:
        raise HubConfigError(f"[hub] has unknown keys: {', '.join(unknown)}")
    mode = hub_mode(target)
    scope = table.get("scope")
    if scope is not None and not valid_scope(scope):
        raise HubConfigError("[hub] scope must be fleet, repo:<identity> or principal:<id>")
    if mode == "sync" and scope is None:
        raise HubConfigError('[hub] mode = "sync" needs a scope to publish into')
    read_scopes = _read_scopes(table, scope)
    if mode != "off" and not read_scopes:
        raise HubConfigError(f'[hub] mode = "{mode}" needs a scope or read_scopes to read from')
    max_mb = table.get("replica_max_mb", DEFAULT_REPLICA_MAX_MB)
    if not isinstance(max_mb, int) or isinstance(max_mb, bool) or max_mb < 1:
        raise HubConfigError("[hub] replica_max_mb must be a positive integer")
    _table_override(table)  # a half-set pair is a config error even while memory is off
    if mode == "off":
        return HubSettings(mode=mode, scope=scope, read_scopes=read_scopes, replica_max_bytes=max_mb * 1024 * 1024)
    override = _memory_hub_override(table, target)
    if override is not None:
        hub_url, token, source = override
    else:
        if fleet is None:
            from . import fleet_client

            # The node token only: memory never falls back to the shared admin token,
            # which can read every scope.
            fleet = dict(fleet_client.load_fleet_settings())
        hub_url, token, source = (
            str(fleet.get("hub_url") or "").rstrip("/"),
            str(fleet.get("node_token") or ""),
            SOURCE_FLEET,
        )
    return HubSettings(
        mode=mode,
        scope=scope,
        read_scopes=read_scopes,
        replica_max_bytes=max_mb * 1024 * 1024,
        hub_url=hub_url,
        token=token,
        source=source,
    )


def hub_identity(hub_url: str) -> str:
    """Scheme, host and port of the hub: what a replica watermark belongs to."""
    parts = urllib.parse.urlsplit(hub_url)
    port = parts.port or {"http": 80, "https": 443}.get(parts.scheme.lower())
    return f"{parts.scheme.lower()}://{(parts.hostname or '').lower()}:{port}{parts.path.rstrip('/')}"


_LAST_EVENT = [0]
_LAST_EVENT_LOCK = threading.Lock()


def event_id(ms: int | None = None) -> str:
    """A time-ordered id: 48 bits of Unix milliseconds, 80 random bits, Crockford base32.

    Ids minted by this process without an explicit `ms` strictly increase, even
    within one millisecond: the next one adds one to the previous random part.
    """
    if ms is not None:
        value = (ms << 80) | secrets.randbits(80)
    else:
        with _LAST_EVENT_LOCK:
            value = (int(time.time() * 1000) << 80) | secrets.randbits(80)
            if value <= _LAST_EVENT[0]:
                value = _LAST_EVENT[0] + 1
            _LAST_EVENT[0] = value
    return "".join(CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))


def event_id_ms(value: str) -> int | None:
    if not isinstance(value, str) or not _EVENT_ID.fullmatch(value):
        return None
    number = 0
    for char in value:
        number = number * 32 + CROCKFORD.index(char)
    return number >> 80


def obj(value: Any) -> dict[str, Any]:
    """`value` when it is a JSON object, else an empty one."""
    return value if isinstance(value, dict) else {}


def normalize_text(text: str) -> str:
    """UTF-8, LF line endings, exactly one trailing newline, as the hub stores a card."""
    return text.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n") + "\n"


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


class Transport(Protocol):
    def __call__(self, url: str, token: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
        """POST JSON and return (status, decoded body); raise OSError or TimeoutError on transport failure."""
        ...


def urllib_transport(url: str, token: str, body: dict[str, Any], timeout: float) -> tuple[int, Any]:
    from . import fleet_client

    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
        method="POST",
    )

    def send() -> tuple[int, bytes]:
        try:
            with fleet_client._hub_open(request, timeout=timeout) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    status, raw = fleet_client._run_with_deadline(send, timeout=timeout)
    try:
        decoded = json.loads(raw.decode("utf-8")) if raw else None
    except (UnicodeDecodeError, ValueError):
        decoded = None
    return status, decoded


class MemoryHubClient:
    """One hub, one credential. `call` returns the body of a 2xx answer."""

    def __init__(self, hub_url: str, token: str, *, transport: Transport | None = None) -> None:
        if not hub_url:
            raise HubUnavailable("no-hub-configured")
        if not token:
            raise HubUnavailable("no-node-credential")
        if transport is None:
            from . import fleet_client

            try:
                fleet_client._require_encrypted_or_loopback_hub(hub_url)
            except fleet_client.FleetClientError as exc:
                raise HubConfigError(str(exc)) from exc
        self.hub_url = hub_url.rstrip("/")
        self.identity = hub_identity(self.hub_url)
        self._token = token
        self._transport = transport or urllib_transport

    def __repr__(self) -> str:  # never show the credential
        return f"MemoryHubClient({self.identity!r})"

    def call(self, action: str, *, timeout: float | None = None, **fields: Any) -> dict[str, Any]:
        body = {"action": action, **fields}
        try:
            status, payload = self._transport(
                self.hub_url + "/memory", self._token, body, timeout or CALL_TIMEOUT_SECONDS
            )
        except TimeoutError as exc:
            raise HubUnavailable("timeout") from exc
        except (OSError, urllib.error.URLError) as exc:
            raise HubUnavailable("unreachable") from exc
        payload = payload if isinstance(payload, dict) else {}
        if 200 <= status < 300:
            return payload
        if 300 <= status < 400 or status in _UNAVAILABLE_STATUSES:
            # A redirect is never followed with the credential, and 408 or 429
            # come from the hub or a proxy in front of it: try again later.
            raise HubUnavailable(f"http-{status}", status=status)
        if status == 404 and not payload.get("code"):
            raise HubUnavailable("no-hub-memory", status=404)
        if status == 503 and not payload.get("code"):
            raise HubUnavailable(str(payload.get("reason") or "hub-busy"), status=503)
        if status == 401:
            raise HubRefusal(401, {"error": str(payload.get("error") or "unauthorized"), "code": "unauthorized"})
        if 500 <= status and not payload.get("code"):
            raise HubUnavailable(f"http-{status}", status=status)
        # Coded 503s, including non-retryable memory_scan_failed, are refusals.
        # Sync turns these into proposals and removes any existing spool entry.
        raise HubRefusal(status, payload)


def client_for(settings: HubSettings, *, transport: Transport | None = None) -> MemoryHubClient:
    return MemoryHubClient(settings.hub_url, settings.token, transport=transport)
