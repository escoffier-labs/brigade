"""Parsing and loading for Dot's owner-only runtime configuration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit
from uuid import UUID

from .contracts import DotError
from .safe_state import private_directory, private_file, require_local_platform

RUNTIME_SCHEMA = "brigade.dot.runtime.v1"
DUET_RUNTIME_SCHEMA = "brigade.duet.runtime.v1"
DUET_PRINCIPAL = "agent:duet"
MAX_SKILLS_DENY = 256
MAX_RUNTIME_BYTES = 65_536

_AUDIENCE_RE = re.compile(r"^[0-9a-f]{64}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+$")
_HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*$"
)
_PRINCIPAL_RE = re.compile(r"^agent:[a-z][a-z0-9._-]{0,62}$")
_ALIAS_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_RECIPIENT_RE = re.compile(r"^(agent:[a-z][a-z0-9._-]{0,62}|[0-9a-f-]{36})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SKILL_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,63}$")


@dataclass(frozen=True)
class AuthConfig:
    issuer: str
    team_domain: str
    audience: str
    approved_email: str
    resource: str


@dataclass(frozen=True)
class BinPaths:
    t3_fleet: str | None
    brigade: str
    git: str
    gh: str
    octopool: str | None


@dataclass(frozen=True)
class PathsConfig:
    state_dir: Path
    workspace: Path
    fleet_config: Path | None
    bin: BinPaths


@dataclass(frozen=True)
class HubConfig:
    principal: str
    node_id: str
    message_token_file: Path = field(repr=False)
    poll_seconds: int
    recipients: Mapping[str, str]


@dataclass(frozen=True)
class OwnerAuthorization:
    version: str
    text: str = field(repr=False)


@dataclass(frozen=True)
class Runtime:
    auth: AuthConfig
    paths: PathsConfig
    hub: HubConfig
    dispatch_enabled: bool
    events_enabled: bool
    digest_pr_repo: str
    callback_hosts: tuple[str, ...]
    notify_command: tuple[str, ...] | None
    owner_authorization: OwnerAuthorization | None = field(repr=False)
    # None keeps Dot's original contract: message_send follows dispatch_enabled.
    messaging_enabled: bool | None = None
    skills_deny: frozenset[str] = frozenset()
    schema: str = RUNTIME_SCHEMA


def _invalid() -> DotError:
    return DotError("invalid_config")


def _object(value: object, keys: set[str], *, optional: set[str] | None = None) -> dict[str, object]:
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise _invalid()
    allowed = keys | (optional or set())
    if set(value) - allowed or keys - set(value):
        raise _invalid()
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise _invalid()
    return value


def _absolute_path(value: object) -> Path:
    raw = _string(value)
    path = Path(raw)
    if not path.is_absolute():
        raise _invalid()
    return path


def _hostname(value: object) -> str:
    host = _string(value)
    if _HOST_RE.fullmatch(host) is None:
        raise _invalid()
    return host


def _https_url(value: object) -> str:
    raw = _string(value)
    try:
        parsed = urlsplit(raw)
        valid = (
            parsed.scheme == "https" and bool(parsed.hostname) and parsed.username is None and parsed.password is None
        )
        _ = parsed.port  # Validate that a supplied port is numeric and in range.
    except ValueError:
        valid = False
    if not valid:
        raise _invalid()
    _hostname(parsed.hostname)
    return raw


def _parse_auth(value: object) -> AuthConfig:
    auth_data = _object(value, {"issuer", "team_domain", "audience", "approved_email", "resource"})
    issuer = _https_url(auth_data["issuer"])
    team_domain = _hostname(auth_data["team_domain"])
    if issuer != "https://" + team_domain:
        raise _invalid()
    audience = _string(auth_data["audience"])
    approved_email = _string(auth_data["approved_email"])
    resource = _https_url(auth_data["resource"])
    if _AUDIENCE_RE.fullmatch(audience) is None or _EMAIL_RE.fullmatch(approved_email) is None:
        raise _invalid()
    return AuthConfig(issuer, team_domain, audience, approved_email, resource)


def _parse_hub(value: object) -> HubConfig:
    hub_data = _object(value, {"principal", "node_id", "message_token_file", "poll_seconds", "recipients"})
    principal = _string(hub_data["principal"])
    node_id = _string(hub_data["node_id"])
    if _PRINCIPAL_RE.fullmatch(principal) is None:
        raise _invalid()
    try:
        if str(UUID(node_id)) != node_id or node_id.lower() != node_id:
            raise ValueError
    except ValueError:
        raise _invalid() from None
    poll_seconds = hub_data["poll_seconds"]
    if type(poll_seconds) is not int or not 10 <= poll_seconds <= 300:
        raise _invalid()
    recipients_data = hub_data["recipients"]
    if not isinstance(recipients_data, dict) or len(recipients_data) > 16:
        raise _invalid()
    recipients: dict[str, str] = {}
    for alias, recipient in recipients_data.items():
        if not isinstance(alias, str) or _ALIAS_RE.fullmatch(alias) is None:
            raise _invalid()
        recipient_text = _string(recipient)
        if _RECIPIENT_RE.fullmatch(recipient_text) is None:
            raise _invalid()
        recipients[alias] = recipient_text
    return HubConfig(
        principal=principal,
        node_id=node_id,
        message_token_file=_absolute_path(hub_data["message_token_file"]),
        poll_seconds=poll_seconds,
        recipients=MappingProxyType(recipients),
    )


def _parse_repo(value: object) -> str:
    digest_pr_repo = _string(value)
    if _REPO_RE.fullmatch(digest_pr_repo) is None:
        raise _invalid()
    return digest_pr_repo


def _parse_notify(root: Mapping[str, object]) -> tuple[str, ...] | None:
    if "notify_command" not in root:
        return None
    notify_data = root["notify_command"]
    if not isinstance(notify_data, list) or not notify_data:
        raise _invalid()
    notify_command = tuple(_string(item) for item in notify_data)
    if not Path(notify_command[0]).is_absolute():
        raise _invalid()
    return notify_command


def _boolean(value: object) -> bool:
    if type(value) is not bool:
        raise _invalid()
    return value


def parse_runtime(payload: object, *, schema: str = RUNTIME_SCHEMA) -> Runtime:
    """Parse one runtime document. The expected schema comes from the pack, never the document."""
    if schema == DUET_RUNTIME_SCHEMA:
        return _parse_duet_runtime(payload)
    if schema != RUNTIME_SCHEMA:
        raise _invalid()
    root = _object(
        payload,
        {
            "schema",
            "auth",
            "paths",
            "hub",
            "dispatch_enabled",
            "events_enabled",
            "digest_pr_repo",
            "callback_hosts",
            "owner_authorization",
        },
        optional={"notify_command", "messaging_enabled"},
    )
    if root["schema"] != RUNTIME_SCHEMA:
        raise _invalid()

    auth = _parse_auth(root["auth"])

    paths_data = _object(root["paths"], {"state_dir", "workspace", "fleet_config", "bin"})
    bin_data = _object(paths_data["bin"], {"t3_fleet", "brigade", "git", "gh"}, optional={"octopool"})
    bins = BinPaths(
        t3_fleet=str(_absolute_path(bin_data["t3_fleet"])),
        brigade=str(_absolute_path(bin_data["brigade"])),
        git=str(_absolute_path(bin_data["git"])),
        gh=str(_absolute_path(bin_data["gh"])),
        octopool=str(_absolute_path(bin_data["octopool"])) if "octopool" in bin_data else None,
    )
    paths = PathsConfig(
        state_dir=_absolute_path(paths_data["state_dir"]),
        workspace=_absolute_path(paths_data["workspace"]),
        fleet_config=_absolute_path(paths_data["fleet_config"]),
        bin=bins,
    )

    hub = _parse_hub(root["hub"])

    dispatch_enabled = _boolean(root["dispatch_enabled"])
    events_enabled = _boolean(root["events_enabled"])
    messaging_enabled = _boolean(root["messaging_enabled"]) if "messaging_enabled" in root else None
    digest_pr_repo = _parse_repo(root["digest_pr_repo"])
    callback_data = root["callback_hosts"]
    if not isinstance(callback_data, list) or not callback_data:
        raise _invalid()
    callback_hosts = tuple(_hostname(host) for host in callback_data)

    notify_command = _parse_notify(root)

    owner_data = _object(root["owner_authorization"], {"version", "text"})
    version = _string(owner_data["version"])
    if len(version) > 64 or any(not " " <= char <= "~" for char in version):
        raise _invalid()
    authorization_text = _string(owner_data["text"])
    try:
        if len(authorization_text.encode("utf-8")) > 4096:
            raise _invalid()
    except UnicodeError:
        raise _invalid() from None
    owner_authorization = OwnerAuthorization(version, authorization_text)

    return Runtime(
        auth=auth,
        paths=paths,
        hub=hub,
        dispatch_enabled=dispatch_enabled,
        events_enabled=events_enabled,
        digest_pr_repo=digest_pr_repo,
        callback_hosts=callback_hosts,
        notify_command=notify_command,
        owner_authorization=owner_authorization,
        messaging_enabled=messaging_enabled,
    )


def _parse_duet_runtime(payload: object) -> Runtime:
    """Duet has no dispatch, fleet client, owner authorization or event webhooks to configure."""
    root = _object(
        payload,
        {"schema", "auth", "paths", "hub", "messaging_enabled", "digest_pr_repo"},
        optional={"notify_command", "skills_deny"},
    )
    if root["schema"] != DUET_RUNTIME_SCHEMA:
        raise _invalid()
    auth = _parse_auth(root["auth"])
    paths_data = _object(root["paths"], {"state_dir", "workspace", "bin"})
    bin_data = _object(paths_data["bin"], {"brigade", "git", "gh"}, optional={"octopool"})
    paths = PathsConfig(
        state_dir=_absolute_path(paths_data["state_dir"]),
        workspace=_absolute_path(paths_data["workspace"]),
        fleet_config=None,
        bin=BinPaths(
            t3_fleet=None,
            brigade=str(_absolute_path(bin_data["brigade"])),
            git=str(_absolute_path(bin_data["git"])),
            gh=str(_absolute_path(bin_data["gh"])),
            octopool=str(_absolute_path(bin_data["octopool"])) if "octopool" in bin_data else None,
        ),
    )
    hub = _parse_hub(root["hub"])
    # Duet never shares another agent's Hub principal, inbox or acknowledgement state.
    if hub.principal != DUET_PRINCIPAL:
        raise _invalid()
    skills_deny: frozenset[str] = frozenset()
    if "skills_deny" in root:
        deny_data = root["skills_deny"]
        if not isinstance(deny_data, list) or len(deny_data) > MAX_SKILLS_DENY:
            raise _invalid()
        names = [_string(name) for name in deny_data]
        if any(_SKILL_RE.fullmatch(name) is None for name in names) or len(set(names)) != len(names):
            raise _invalid()
        skills_deny = frozenset(names)
    return Runtime(
        auth=auth,
        paths=paths,
        hub=hub,
        dispatch_enabled=False,
        events_enabled=False,
        digest_pr_repo=_parse_repo(root["digest_pr_repo"]),
        callback_hosts=(),
        notify_command=_parse_notify(root),
        owner_authorization=None,
        messaging_enabled=_boolean(root["messaging_enabled"]),
        skills_deny=skills_deny,
        schema=DUET_RUNTIME_SCHEMA,
    )


def load_runtime(path: Path, *, schema: str = RUNTIME_SCHEMA) -> Runtime:
    require_local_platform()
    directory = descriptor = None
    try:
        directory = private_directory(path.parent)
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        private_file(os.fstat(descriptor))
        if os.fstat(descriptor).st_size > MAX_RUNTIME_BYTES:
            raise _invalid()
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            raw = stream.read(MAX_RUNTIME_BYTES + 1)
            private_file(os.fstat(stream.fileno()))
        if len(raw) > MAX_RUNTIME_BYTES:
            raise _invalid()
        payload = json.loads(raw.decode("utf-8"))
    except (DotError, OSError, UnicodeError, ValueError):
        raise _invalid() from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if directory is not None:
            os.close(directory)
    return parse_runtime(payload, schema=schema)


def validate_runtime_reference(value: object, *, schema: str = RUNTIME_SCHEMA) -> str:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise _invalid()
    load_runtime(Path(value), schema=schema)
    return value
