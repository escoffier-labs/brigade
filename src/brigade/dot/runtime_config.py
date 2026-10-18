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
from .safe_state import private_directory, private_file

RUNTIME_SCHEMA = "brigade.dot.runtime.v1"
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


@dataclass(frozen=True)
class AuthConfig:
    issuer: str
    team_domain: str
    audience: str
    approved_email: str
    resource: str


@dataclass(frozen=True)
class BinPaths:
    t3_fleet: str
    brigade: str
    git: str
    gh: str
    octopool: str | None


@dataclass(frozen=True)
class PathsConfig:
    state_dir: Path
    workspace: Path
    fleet_config: Path
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
    owner_authorization: OwnerAuthorization = field(repr=False)


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


def parse_runtime(payload: object) -> Runtime:
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
        optional={"notify_command"},
    )
    if root["schema"] != RUNTIME_SCHEMA:
        raise _invalid()

    auth_data = _object(root["auth"], {"issuer", "team_domain", "audience", "approved_email", "resource"})
    issuer = _https_url(auth_data["issuer"])
    team_domain = _hostname(auth_data["team_domain"])
    if issuer != "https://" + team_domain:
        raise _invalid()
    audience = _string(auth_data["audience"])
    approved_email = _string(auth_data["approved_email"])
    resource = _https_url(auth_data["resource"])
    if _AUDIENCE_RE.fullmatch(audience) is None or _EMAIL_RE.fullmatch(approved_email) is None:
        raise _invalid()
    auth = AuthConfig(issuer, team_domain, audience, approved_email, resource)

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

    hub_data = _object(root["hub"], {"principal", "node_id", "message_token_file", "poll_seconds", "recipients"})
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
    hub = HubConfig(
        principal=principal,
        node_id=node_id,
        message_token_file=_absolute_path(hub_data["message_token_file"]),
        poll_seconds=poll_seconds,
        recipients=MappingProxyType(recipients),
    )

    dispatch_enabled = root["dispatch_enabled"]
    events_enabled = root["events_enabled"]
    if type(dispatch_enabled) is not bool or type(events_enabled) is not bool:
        raise _invalid()
    digest_pr_repo = _string(root["digest_pr_repo"])
    if _REPO_RE.fullmatch(digest_pr_repo) is None:
        raise _invalid()
    callback_data = root["callback_hosts"]
    if not isinstance(callback_data, list) or not callback_data:
        raise _invalid()
    callback_hosts = tuple(_hostname(host) for host in callback_data)

    notify_data = root.get("notify_command")
    notify_command: tuple[str, ...] | None = None
    if "notify_command" in root:
        if not isinstance(notify_data, list) or not notify_data:
            raise _invalid()
        notify_command = tuple(_string(item) for item in notify_data)
        if not Path(notify_command[0]).is_absolute():
            raise _invalid()

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
    )


def load_runtime(path: Path) -> Runtime:
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
    return parse_runtime(payload)


def validate_runtime_reference(value: object) -> str:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise _invalid()
    load_runtime(Path(value))
    return value
