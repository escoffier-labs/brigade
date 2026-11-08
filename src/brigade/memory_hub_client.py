"""Client for hub memory, `POST /memory` (S14).

A workspace opts in through a `[hub]` table in `.brigade/memory-care.toml`:

    [hub]
    mode = "read"                       # off (default), read or sync
    scope = "repo:me/app"               # where `sync` publishes; fleet, repo:<id>, collection:<name> or principal:<id>
    read_scopes = ["repo:me/app"]       # what search, card reads and the replica may use; default [scope]
    replica_max_mb = 50                 # cap on the replica's heads.json

A workspace file never chooses where a credential goes, so `[hub]` refuses
`hub_url` and `node_token_file`. Memory can still use its own hub, set per user
in `~/.brigade/fleet.toml` (the file `[fleet]` lives in, under `BRIGADE_HOME`
when that is set):

    [memory_hub]
    hub_url = "https://hub.example.test:3774"
    node_token_file = "~/.brigade/memory-node-token"

Which hub memory talks to, from highest precedence:

1. `BRIGADE_MEMORY_HUB_URL` and `BRIGADE_MEMORY_HUB_TOKEN_FILE`, set together.
   The token file path must be absolute.
2. `[memory_hub] hub_url` and `node_token_file`, set together. A leading `~`
   expands, and the path must then be absolute.
3. The fleet settings: `[fleet] hub_url` (or `BRIGADE_FLEET_HUB_URL`) and this
   machine's node token (`BRIGADE_FLEET_NODE_TOKEN` or `[fleet] node_token_file`).

A memory override replaces both fleet values for hub memory only, so fleet
coordination can stay on one hub while memory lives on another. `brigade fleet`
commands never read it. The override's token file must be a private, owned
regular file holding 32 to 512 URL-safe characters (the check the message
credentials use), and it must not be a fleet credential: a token equal to the
fleet node or admin token, or a file that is `[fleet] node_token_file` or
`token_file`, is refused. The URL gets the fleet hub URL's scheme rule (https,
or http on loopback) and may not carry a user name or password. The replica is
keyed by hub identity, so pointing memory at another hub discards the old
replica and rebuilds it. `HubSettings.source` says which of the three won
(`env`, `[memory_hub]` or `fleet`), and `memory hub status` reports it.

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
# A connector that reads as its own principal sets this, so a replica pulled by any other credential never answers.
ENV_REPLICA_PRINCIPAL = "BRIGADE_MEMORY_REPLICA_PRINCIPAL"
SOURCE_ENV, SOURCE_TABLE, SOURCE_FLEET = "env", "[memory_hub]", "fleet"
MEMORY_HUB_TABLE = "memory_hub"
_OVERRIDE_KEYS = ("hub_url", "node_token_file")
READ_SCOPES_MAX = 64
# Answers that say "not now" rather than "no": they spool and use the replica.
_UNAVAILABLE_STATUSES = {408, 429}
# Coded proposal quota answers are hub decisions, so callers can stop proposing.
QUOTA_REFUSALS = {"memory_proposal_principal_quota", "memory_proposal_scope_quota"}
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
    source: str = SOURCE_FLEET  # where hub_url and token came from: env, [memory_hub] or fleet

    @property
    def enabled(self) -> bool:
        return self.mode != "off"


ADMIN_CREDENTIAL_REFUSAL = (
    "the hub says this credential is its admin token; hub memory runs only as a node or principal "
    "credential, so set BRIGADE_FLEET_NODE_TOKEN or [fleet] node_token_file (or the memory hub override, "
    "[memory_hub] node_token_file or BRIGADE_MEMORY_HUB_TOKEN_FILE) to this machine's node token"
)


def refuse_admin(status_body: dict[str, Any]) -> dict[str, Any]:
    """`status_body` as is, unless the hub reports the admin credential: then HubConfigError."""
    if status_body.get("credential") == "admin":
        raise HubConfigError(ADMIN_CREDENTIAL_REFUSAL)
    return status_body


def valid_scope(scope: object) -> bool:
    """`fleet`, `repo:<identity>`, `collection:<name>` or `principal:<id>`, as the hub accepts them.

    A collection name follows the repository identity rule. Node tokens get no
    default access to a collection: only a grant naming it opens it.
    """
    if not isinstance(scope, str):
        return False
    if scope == "fleet":
        return True
    for prefix in ("repo:", "collection:", "principal:"):
        if scope.startswith(prefix):
            rest = scope[len(prefix) :]
            return 0 < len(rest) <= 200 and all(33 <= ord(c) <= 126 and c not in '*"\\' for c in rest)
    return False


def valid_card_id(card_id: object) -> bool:
    return isinstance(card_id, str) and _CARD_ID.fullmatch(card_id) is not None


def replica_principal_allowed(recorded: object) -> bool:
    """False when a pinned principal is set and the replica was pulled by a different one.

    Unset means no pin, the behavior of every ordinary memory command.
    """
    expected = os.environ.get(ENV_REPLICA_PRINCIPAL, "").strip()
    return not expected or recorded == expected


def read_config_text(target: Path) -> str | None:
    """The memory-care config text; None when absent. Raises OSError or UnicodeDecodeError as is."""
    try:
        return (target / CONFIG_REL).read_text(encoding="utf-8-sig")
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
            f"[hub] read_scopes must be a list of 1 to {READ_SCOPES_MAX} scopes: "
            "fleet, repo:<identity>, collection:<name> or principal:<id>"
        )
    return tuple(dict.fromkeys(raw))


def display_url(url: str) -> str:
    """`url` without a user name or password, for anything that prints it."""
    parts = urllib.parse.urlsplit(url)
    if parts.username is None and parts.password is None:
        return url
    host = parts.netloc.rpartition("@")[2]
    return urllib.parse.urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def _user_config() -> dict[str, Any]:
    """`~/.brigade/fleet.toml` (under `BRIGADE_HOME` when set), or {} when absent."""
    from . import fleet_client

    path = fleet_client.brigade_home() / fleet_client.FLEET_CONFIG_REL_PATH.name
    try:
        if not path.is_file():
            return {}
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise HubConfigError(f"{path} could not be read") from None
    return payload if isinstance(payload, dict) else {}


def _table_override(config: dict[str, Any]) -> tuple[str, str] | None:
    """`[memory_hub] hub_url` and `node_token_file` as given, or None when the table sets neither.

    The table names one hub. If a workspace ever needs to choose among hubs, the
    workspace could name one of several user-level tables; it must never carry
    a URL or a token path itself.
    """
    table = config.get(MEMORY_HUB_TABLE)
    if table is None:
        return None
    if not isinstance(table, dict):
        raise HubConfigError(f"[{MEMORY_HUB_TABLE}] must be a table")
    unknown = sorted(set(table) - set(_OVERRIDE_KEYS))
    if unknown:
        raise HubConfigError(f"[{MEMORY_HUB_TABLE}] has unknown keys: {', '.join(unknown)}")
    values: dict[str, str] = {}
    for key in _OVERRIDE_KEYS:
        if key not in table:
            continue
        raw = table[key]
        if not isinstance(raw, str) or not raw.strip():
            raise HubConfigError(f"[{MEMORY_HUB_TABLE}] {key} must be a non-empty string")
        values[key] = raw.strip()
    if not values:
        return None
    for key, other in (("hub_url", "node_token_file"), ("node_token_file", "hub_url")):
        if key in values and other not in values:
            raise HubConfigError(
                f"[{MEMORY_HUB_TABLE}] {other} is missing: hub_url and node_token_file must be set together"
            )
    return values["hub_url"], values["node_token_file"]


def _same_file(first: Path, raw_second: str) -> bool:
    """Whether `raw_second` (a `[fleet]` token path, as `brigade fleet` reads it) names the file `first`."""
    candidates = [raw_second.strip()]
    if "\\" in raw_second:
        candidates.append(raw_second.strip().replace("\\", "/"))
    for candidate in candidates:
        second = Path(candidate).expanduser()
        try:
            if os.path.samefile(first, second):
                return True
        except (OSError, ValueError):
            # samefile needs both files to exist; compare the resolved paths instead.
            try:
                if os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second)):
                    return True
            except (OSError, ValueError):
                continue
    return False


def _refuse_fleet_credential(
    token: str, path: Path, file_name: str, config: dict[str, Any], fleet: dict[str, str] | None
) -> None:
    """Refuse an override that would send a fleet credential to the memory hub. Never prints either token."""
    from . import fleet_client

    values = dict(fleet) if fleet is not None else dict(fleet_client.load_fleet_settings())
    refusal = (
        f"{file_name} {path}: the memory hub override must not use a fleet credential; "
        "give memory its own node token on its own hub"
    )
    for key in ("node_token", "admin_token"):
        other = str(values.get(key) or "")
        if other and secrets.compare_digest(token.encode(), other.encode()):
            raise HubConfigError(refusal)
    section = config.get("fleet")
    if isinstance(section, dict):
        for key in ("node_token_file", "token_file"):
            raw = section.get(key)
            if isinstance(raw, str) and raw.strip() and _same_file(path, raw):
                raise HubConfigError(refusal)


def _memory_hub_override(
    fleet: dict[str, str] | None, *, credential_pair: tuple[str, Path] | None = None
) -> tuple[str, str, str] | None:
    """`(hub_url, token, source)` when memory has its own hub, else None to use the fleet settings.

    The environment pair outranks `[memory_hub]`. Each pair is all or nothing,
    and an empty environment value counts as unset, as for the fleet variables.
    """
    from . import fleet_client
    from .fleet_client_message_credentials import FleetMessageHubError, read_message_token_file

    config = _user_config()
    env_url = os.environ.get(ENV_HUB_URL, "").strip()
    env_file = os.environ.get(ENV_HUB_TOKEN_FILE, "").strip()
    if credential_pair is not None:
        url, path = credential_pair
        source, url_name, file_name = "cli-file", "--hub-url", "--token-file"
    elif env_url or env_file:
        for name, other, value in (
            (ENV_HUB_URL, ENV_HUB_TOKEN_FILE, env_url),
            (ENV_HUB_TOKEN_FILE, ENV_HUB_URL, env_file),
        ):
            if not value:
                raise HubConfigError(f"{other} is set but {name} is missing: set both or neither")
        url, path, source, url_name, file_name = env_url, Path(env_file), SOURCE_ENV, ENV_HUB_URL, ENV_HUB_TOKEN_FILE
    else:
        pair = _table_override(config)
        if pair is None:
            return None
        url, path = pair[0], Path(pair[1]).expanduser()
        source = SOURCE_TABLE
        url_name, file_name = f"[{MEMORY_HUB_TABLE}] hub_url", f"[{MEMORY_HUB_TABLE}] node_token_file"
    try:
        fleet_client._require_encrypted_or_loopback_hub(url)
    except fleet_client.FleetClientError as exc:
        raise HubConfigError(f"{url_name}: {exc}") from exc
    parts = urllib.parse.urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise HubConfigError(f"{url_name} must not carry a user name or password")
    if not parts.hostname:
        raise HubConfigError(f"{url_name} names no host")
    if not path.is_absolute():
        raise HubConfigError(f"{file_name} must be an absolute path")
    try:
        token = read_message_token_file(str(path), label="memory hub node")
    except FleetMessageHubError as exc:
        raise HubConfigError(f"{file_name} {path}: {exc}") from exc
    _refuse_fleet_credential(token, path, file_name, config, fleet)
    return url.rstrip("/"), token, source


def load_settings(
    target: Path, *, fleet: dict[str, str] | None = None, credential_pair: tuple[str, Path] | None = None
) -> HubSettings:
    """Settings for `target`. `fleet` stands in for the fleet settings: `hub_url`, `node_token`, `admin_token`.

    A memory override (see the module docstring) replaces the fleet values. In
    `off` mode neither the environment pair nor the user-level file is read.
    """
    table = read_hub_table(target)
    moved = [key for key in _OVERRIDE_KEYS if key in table]
    if moved:
        raise HubConfigError(
            f"[hub] {moved[0]} is not read from the workspace: set hub_url and node_token_file in "
            f"[{MEMORY_HUB_TABLE}] in ~/.brigade/fleet.toml, or set {ENV_HUB_URL} and {ENV_HUB_TOKEN_FILE}"
        )
    unknown = sorted(set(table) - {"mode", "scope", "read_scopes", "replica_max_mb"})
    if unknown:
        raise HubConfigError(f"[hub] has unknown keys: {', '.join(unknown)}")
    mode = hub_mode(target)
    scope = table.get("scope")
    if scope is not None and not valid_scope(scope):
        raise HubConfigError("[hub] scope must be fleet, repo:<identity>, collection:<name> or principal:<id>")
    if mode == "sync" and scope is None:
        raise HubConfigError('[hub] mode = "sync" needs a scope to publish into')
    read_scopes = _read_scopes(table, scope)
    if mode != "off" and not read_scopes:
        raise HubConfigError(f'[hub] mode = "{mode}" needs a scope or read_scopes to read from')
    max_mb = table.get("replica_max_mb", DEFAULT_REPLICA_MAX_MB)
    if not isinstance(max_mb, int) or isinstance(max_mb, bool) or max_mb < 1:
        raise HubConfigError("[hub] replica_max_mb must be a positive integer")
    if mode == "off":
        return HubSettings(mode=mode, scope=scope, read_scopes=read_scopes, replica_max_bytes=max_mb * 1024 * 1024)
    override = (
        _memory_hub_override(fleet, credential_pair=credential_pair)
        if credential_pair is not None
        else _memory_hub_override(fleet)
    )
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
        return event_id_at(ms, secrets.randbits(80))
    with _LAST_EVENT_LOCK:
        value = (int(time.time() * 1000) << 80) | secrets.randbits(80)
        if value <= _LAST_EVENT[0]:
            value = _LAST_EVENT[0] + 1
        _LAST_EVENT[0] = value
    return _crockford(value)


def _crockford(value: int) -> str:
    return "".join(CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))


def event_id_at(ms: int, tail: int) -> str:
    """The event id for Unix milliseconds `ms` and the 80-bit `tail`: the same pair always gives the same id."""
    return _crockford((ms << 80) | (tail & ((1 << 80) - 1)))


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
            if action == "rescan" and status != 200:
                raise HubRefusal(status, {"code": "memory_rescan_status", "error": "rescan requires HTTP 200"})
            return payload
        code = payload.get("code")
        if status == 429 and isinstance(code, str) and code in QUOTA_REFUSALS:
            raise HubRefusal(status, payload)
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
