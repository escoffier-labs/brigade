"""Parsing and loading for Dot's owner-only runtime configuration."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit
from uuid import UUID

from .contracts import DotError
from .safe_state import private_directory, private_file, require_local_platform

CLOUD_AGENT_RUNTIME_SCHEMA = "brigade.cloud-agent.runtime.v1"
RUNTIME_SCHEMA = "brigade.dot.runtime.v1"
DUET_RUNTIME_SCHEMA = "brigade.duet.runtime.v1"
DUET_PRINCIPAL = "agent:duet"
GROKBOT_MEMORY_RUNTIME_SCHEMA = "brigade.grokbot-memory.runtime.v1"
GROKBOT_MEMORY_PRINCIPAL = "agent:grokbot-memory"
GROKBOT_INBOX_RUNTIME_SCHEMA = "brigade.grokbot-inbox.runtime.v1"
GROKBOT_INBOX_PRINCIPAL = "agent:grokbot-inbox"
# The alias every connector keeps for the conductor, so an agent can start a message to it.
CONDUCTOR_ALIAS = "conductor"
# What a connector-computed assignment permits. Nothing else is ever authorized by an inbox message.
ASSIGNMENT_ACTIONS = ("create_branch", "commit", "push_branch", "open_pull_request")
MAX_ASSIGNMENT_REPOSITORIES = 64
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
    mode: str = "access"
    token_file: Path | None = field(default=None, repr=False)


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
    # `assignments.repositories` from the runtime file. Empty means no inbox message is ever an assignment.
    assignment_repositories: frozenset[str] = frozenset()


@dataclass(frozen=True)
class OwnerAuthorization:
    version: str
    text: str = field(repr=False)


@dataclass(frozen=True)
class DispatchLimits:
    repositories: frozenset[str] | None = None
    max_open: int = 3
    daily_limit: int = 20
    prompt_max_bytes: int = 32768


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
    # Opt-out: false stops inbox_list and fleet_digest from carrying memory hits.
    memory_push: bool = True
    dispatch_limits: DispatchLimits | None = None


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


def _with_assignments(hub: HubConfig, root: Mapping[str, object]) -> HubConfig:
    """Attach the optional `assignments.repositories` allowlist of exact `owner/repo` names."""
    if "assignments" not in root:
        return hub
    data = _object(root["assignments"], {"repositories"})
    raw = data["repositories"]
    if not isinstance(raw, list) or len(raw) > MAX_ASSIGNMENT_REPOSITORIES:
        raise _invalid()
    names = [_parse_repo(item) for item in raw]
    if len(set(names)) != len(names):
        raise _invalid()
    return replace(hub, assignment_repositories=frozenset(names))


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
    if schema == CLOUD_AGENT_RUNTIME_SCHEMA:
        return _parse_cloud_agent_runtime(payload)
    if schema == DUET_RUNTIME_SCHEMA:
        return _parse_duet_runtime(payload)
    if schema == GROKBOT_MEMORY_RUNTIME_SCHEMA:
        return _parse_grokbot_memory_runtime(payload)
    if schema == GROKBOT_INBOX_RUNTIME_SCHEMA:
        return _parse_grokbot_inbox_runtime(payload)
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
        optional={"notify_command", "messaging_enabled", "memory_push", "assignments"},
    )
    if root["schema"] != RUNTIME_SCHEMA:
        raise _invalid()

    auth = _parse_auth(root["auth"])

    paths = _parse_dispatch_paths(root["paths"])

    hub = _with_assignments(_parse_hub(root["hub"]), root)

    dispatch_enabled = _boolean(root["dispatch_enabled"])
    events_enabled = _boolean(root["events_enabled"])
    messaging_enabled = _boolean(root["messaging_enabled"]) if "messaging_enabled" in root else None
    digest_pr_repo = _parse_repo(root["digest_pr_repo"])
    callback_data = root["callback_hosts"]
    if not isinstance(callback_data, list) or not callback_data:
        raise _invalid()
    callback_hosts = tuple(_hostname(host) for host in callback_data)

    notify_command = _parse_notify(root)

    owner_authorization = _parse_owner_authorization(root["owner_authorization"])

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
        memory_push=_boolean(root["memory_push"]) if "memory_push" in root else True,
    )


def _parse_dispatch_paths(value: object) -> PathsConfig:
    paths_data = _object(value, {"state_dir", "workspace", "fleet_config", "bin"})
    bin_data = _object(paths_data["bin"], {"t3_fleet", "brigade", "git", "gh"}, optional={"octopool"})
    bins = BinPaths(
        t3_fleet=str(_absolute_path(bin_data["t3_fleet"])),
        brigade=str(_absolute_path(bin_data["brigade"])),
        git=str(_absolute_path(bin_data["git"])),
        gh=str(_absolute_path(bin_data["gh"])),
        octopool=str(_absolute_path(bin_data["octopool"])) if "octopool" in bin_data else None,
    )
    return PathsConfig(
        state_dir=_absolute_path(paths_data["state_dir"]),
        workspace=_absolute_path(paths_data["workspace"]),
        fleet_config=_absolute_path(paths_data["fleet_config"]),
        bin=bins,
    )


def _parse_owner_authorization(value: object) -> OwnerAuthorization:
    owner_data = _object(value, {"version", "text"})
    version = _string(owner_data["version"])
    if len(version) > 64 or any(not " " <= char <= "~" for char in version):
        raise _invalid()
    authorization_text = _string(owner_data["text"])
    try:
        if len(authorization_text.encode("utf-8")) > 4096:
            raise _invalid()
    except UnicodeError:
        raise _invalid() from None
    return OwnerAuthorization(version, authorization_text)


def _parse_hosted_paths(value: object) -> PathsConfig:
    """Hosted profiles have no T3 fleet client or fleet config."""
    paths_data = _object(value, {"state_dir", "workspace", "bin"})
    bin_data = _object(paths_data["bin"], {"brigade", "git", "gh"}, optional={"octopool"})
    return PathsConfig(
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


def _parse_dispatch_limits(value: object) -> DispatchLimits:
    data = _object(value, set(), optional={"repositories", "max_open", "daily_limit", "prompt_max_bytes"})
    defaults = DispatchLimits()
    numbers = {}
    for key, maximum in (("max_open", 20), ("daily_limit", 200), ("prompt_max_bytes", 32768)):
        number = data.get(key, getattr(defaults, key))
        if type(number) is not int or not 1 <= number <= maximum:
            raise _invalid()
        numbers[key] = number
    repositories = None
    if "repositories" in data:
        raw = data["repositories"]
        if not isinstance(raw, list) or len(raw) > 256:
            raise _invalid()
        if any(not isinstance(item, str) or _ALIAS_RE.fullmatch(item) is None for item in raw):
            raise _invalid()
        repositories = frozenset(raw)
    return DispatchLimits(repositories=repositories, **numbers)


def _parse_duet_runtime(payload: object) -> Runtime:
    """Duet dispatch requires an explicit complete opt-in; events remain unavailable."""
    root = _object(
        payload,
        {"schema", "auth", "paths", "hub", "messaging_enabled", "digest_pr_repo"},
        optional={
            "notify_command",
            "skills_deny",
            "memory_push",
            "dispatch_enabled",
            "owner_authorization",
            "dispatch_limits",
            "assignments",
        },
    )
    if root["schema"] != DUET_RUNTIME_SCHEMA:
        raise _invalid()
    auth = _parse_auth(root["auth"])
    enabled = _boolean(root.get("dispatch_enabled", False))
    limits = None
    owner = None
    if enabled:
        paths = _parse_dispatch_paths(root["paths"])
        owner = _parse_owner_authorization(root.get("owner_authorization"))
        limits = _parse_dispatch_limits(root.get("dispatch_limits", {}))
    else:
        if "owner_authorization" in root or "dispatch_limits" in root:
            raise _invalid()
        paths = _parse_hosted_paths(root["paths"])
    hub = _with_assignments(_parse_hub(root["hub"]), root)
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
        dispatch_enabled=enabled,
        events_enabled=False,
        digest_pr_repo=_parse_repo(root["digest_pr_repo"]),
        callback_hosts=(),
        notify_command=_parse_notify(root),
        owner_authorization=owner,
        messaging_enabled=_boolean(root["messaging_enabled"]),
        skills_deny=skills_deny,
        schema=DUET_RUNTIME_SCHEMA,
        dispatch_limits=limits,
        memory_push=_boolean(root["memory_push"]) if "memory_push" in root else True,
    )


def _parse_grokbot_memory_runtime(payload: object) -> Runtime:
    """The Grok Bot memory profile reads cards and submits handoffs for review. Nothing here enables more.

    There is no messaging, dispatch, digest, skill or event key. The hub section names a
    dedicated principal, and that principal's own credential and memory grants are what the
    profile reads and submits with, so the listener stays isolated from Dot.
    """
    root = _object(payload, {"schema", "auth", "paths", "hub"}, optional={"notify_command"})
    if root["schema"] != GROKBOT_MEMORY_RUNTIME_SCHEMA:
        raise _invalid()
    auth = _parse_auth(root["auth"])
    paths = _parse_hosted_paths(root["paths"])
    hub = _parse_hub(root["hub"])
    if hub.principal != GROKBOT_MEMORY_PRINCIPAL:
        raise _invalid()
    return Runtime(
        auth=auth,
        paths=paths,
        hub=hub,
        dispatch_enabled=False,
        events_enabled=False,
        digest_pr_repo="",
        callback_hosts=(),
        notify_command=_parse_notify(root),
        owner_authorization=None,
        messaging_enabled=False,
        schema=GROKBOT_MEMORY_RUNTIME_SCHEMA,
        memory_push=False,
    )


def _parse_grokbot_inbox_runtime(payload: object) -> Runtime:
    """The Grok Bot inbox profile sends and reads hub messages under its own principal.

    There is no dispatch, digest, skill, event or memory key. `hub.recipients` names the aliases the
    tool accepts, and the hub's send grants (`may_send_to`) for the principal are the allowlist behind them.
    """
    root = _object(payload, {"schema", "auth", "paths", "hub", "messaging_enabled"}, optional={"notify_command"})
    if root["schema"] != GROKBOT_INBOX_RUNTIME_SCHEMA:
        raise _invalid()
    auth = _parse_auth(root["auth"])
    paths = _parse_hosted_paths(root["paths"])
    hub = _parse_hub(root["hub"])
    if hub.principal != GROKBOT_INBOX_PRINCIPAL:
        raise _invalid()
    return Runtime(
        auth=auth,
        paths=paths,
        hub=hub,
        dispatch_enabled=False,
        events_enabled=False,
        digest_pr_repo="",
        callback_hosts=(),
        notify_command=_parse_notify(root),
        owner_authorization=None,
        messaging_enabled=_boolean(root["messaging_enabled"]),
        schema=GROKBOT_INBOX_RUNTIME_SCHEMA,
        memory_push=False,
    )


def load_runtime(path: Path, *, schema: str = RUNTIME_SCHEMA, planned_token: bool = False) -> Runtime:
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
    if planned_token and schema == CLOUD_AGENT_RUNTIME_SCHEMA:
        from .cloud_runtime import parse_cloud_runtime

        return parse_cloud_runtime(payload, planned_token=True)
    return parse_runtime(payload, schema=schema)


def validate_runtime_reference(value: object, *, schema: str = RUNTIME_SCHEMA) -> str:
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise _invalid()
    load_runtime(Path(value), schema=schema)
    return value


def runtime_template(schema: str = RUNTIME_SCHEMA) -> dict[str, object]:
    """A fresh Dot or Duet runtime document with placeholder values and the default recipient aliases.

    Every value is a placeholder that parses, never a real identity: replace each one, including the
    `conductor` recipient, with the deployment's own before writing the owner-only runtime file.
    """
    if schema not in (RUNTIME_SCHEMA, DUET_RUNTIME_SCHEMA):
        raise _invalid()
    duet = schema == DUET_RUNTIME_SCHEMA
    name = "duet" if duet else "dot"
    binaries: dict[str, object] = {
        "brigade": "/usr/local/bin/brigade",
        "git": "/usr/bin/git",
        "gh": "/usr/bin/gh",
    }
    paths: dict[str, object] = {
        "state_dir": f"/var/lib/brigade-{name}/state",
        "workspace": f"/var/lib/brigade-{name}/workspace",
        "bin": binaries,
    }
    document: dict[str, object] = {
        "schema": schema,
        "auth": {
            "issuer": "https://team.example.com",
            "team_domain": "team.example.com",
            "audience": "0" * 64,
            "approved_email": "owner@example.com",
            "resource": f"https://{name}.example.com/mcp",
        },
        "paths": paths,
        "hub": {
            "principal": DUET_PRINCIPAL if duet else "agent:example",
            "node_id": "00000000-0000-4000-8000-000000000001",
            "message_token_file": f"/var/lib/brigade-{name}/message-token",
            "poll_seconds": 30,
            "recipients": {CONDUCTOR_ALIAS: "agent:conductor", "operator": "agent:operator"},
        },
        "digest_pr_repo": "example-org/example-project",
        # Empty on purpose: no inbox message is an assignment until the owner lists a repository here.
        "assignments": {"repositories": []},
    }
    if duet:
        document["messaging_enabled"] = True
        return document
    binaries["t3_fleet"] = "/usr/local/bin/t3-fleet"
    paths["fleet_config"] = "/var/lib/brigade-dot/fleet.toml"
    document.update(
        dispatch_enabled=False,
        events_enabled=False,
        messaging_enabled=True,
        callback_hosts=["callback.example.com"],
        owner_authorization={"version": "1", "text": "Owner-authorized bounded work on the configured repositories."},
    )
    return document


def _parse_cloud_agent_runtime(payload: object) -> Runtime:
    from .cloud_runtime import parse_cloud_runtime

    return parse_cloud_runtime(payload)
