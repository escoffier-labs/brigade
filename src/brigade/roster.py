"""Load and validate a Brigade aboyeur roster."""

from __future__ import annotations

import fnmatch
import json
import re
import statistics
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol

from . import agents as agent_adapters
from . import toml_compat

SANDBOX_CHOICES = ("read-only", "workspace-write", "danger-full-access")
CODEX_TRANSPORT_CHOICES = ("exec", "app-server")
AGENT_TRANSPORT_CHOICES = ("direct", "acpx")
ACPX_TRANSPORT_VERSION = "0.12.0"
RosterSource = Literal["explicit", "workspace", "user"]


@dataclass(frozen=True)
class RosterResolution:
    path: Path
    source: RosterSource
    shadowed: tuple[Path, ...] = ()


@dataclass(frozen=True)
class Agent:
    name: str
    cli: str | None
    role: str
    timeout_seconds: float | None = None
    endpoint: str | None = None
    model: str | None = None
    headers: dict | None = None
    reasoning: str | None = None
    transport: str = "direct"
    transport_version: str | None = None
    env: dict[str, str] | None = None
    invalid_final_fallback: str | None = None
    read_only_capable: bool = True
    purpose: str | None = None
    requires: dict[str, str] | None = None
    fallback: tuple[str, ...] = ()
    stats: dict[str, str] | None = None
    caveats: tuple[str, ...] = ()


@dataclass(frozen=True)
class Roster:
    orchestrator: str
    agents: dict[str, Agent]
    max_workers: int = 4
    allow_models: tuple[str, ...] = ()
    timeout_seconds: float = 600.0
    sandbox: str | None = None
    scheduler: str | None = None
    codex_transport: str = "exec"
    resolution: RosterResolution | None = None

    def find_role(self, role: str) -> Agent | None:
        return next((a for a in self.agents.values() if a.role == role), None)


@dataclass(frozen=True)
class Capability:
    installed: bool
    authenticated: bool | None = None
    detail: str = ""
    auth_detail: str = ""


class CapabilityProbe(Protocol):
    def lookup(self, cli_ref: str) -> Capability: ...


@dataclass(frozen=True)
class SeatResolution:
    requested: str
    resolved: str | None
    outcome: Literal["self", "fallback", "dropped"]
    reason: str


@dataclass(frozen=True)
class RosterCapabilityResolution:
    roster: Roster
    report: tuple[SeatResolution, ...]

    @property
    def usable(self) -> bool:
        return self.roster.orchestrator in self.roster.agents


@dataclass(frozen=True)
class SeatReceiptStats:
    sample_count: int
    median_duration_seconds: float
    failure_rate: float


@dataclass(frozen=True)
class HostCapabilityProbe:
    def lookup(self, cli_ref: str) -> Capability:
        binary = agent_adapters.command_for(cli_ref)
        installed = agent_adapters.detect(cli_ref)
        authenticated: bool | None = None
        detail = ""
        auth_detail = ""
        if cli_ref == "cursor" and installed:
            from . import acpx_adapter

            auth = acpx_adapter.cursor_auth_status()
            detail = auth.detail
            auth_detail = auth.detail
            if auth.state == "authenticated":
                authenticated = True
            elif auth.state == "unauthenticated":
                authenticated = False
        elif installed:
            detail = f"{cli_ref} via {binary}"
        else:
            detail = f"{cli_ref} needs `{binary}` on PATH"
        return Capability(
            installed=installed,
            authenticated=authenticated,
            detail=detail,
            auth_detail=auth_detail,
        )


def _as_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _as_positive_number(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive number")
    return float(value)


def _as_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _as_optional_str(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _as_str(value, field)


def _as_requires(value: object, agent_name: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"agents.{agent_name}.requires must be a TOML table")
    allowed = frozenset({"cli", "auth"})
    parsed: dict[str, str] = {}
    for key, raw in value.items():
        if key not in allowed:
            raise ValueError(f"agents.{agent_name}.requires keys must be cli and/or auth")
        parsed[key] = _as_str(raw, f"agents.{agent_name}.requires.{key}")
    if "auth" in parsed and parsed["auth"] != "logged-in":
        raise ValueError(f'agents.{agent_name}.requires.auth must be "logged-in"')
    return parsed or None


def _as_stats(value: object, agent_name: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"agents.{agent_name}.stats must be a TOML table")
    parsed: dict[str, str] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"agents.{agent_name}.stats keys must be non-empty strings")
        parsed[key.strip()] = _as_str(raw, f"agents.{agent_name}.stats.{key}")
    return parsed or None


def _as_string_list(value: object, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{field} must be a list of strings")
    return tuple(_as_str(item, field) for item in value)


def _as_sandbox(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in SANDBOX_CHOICES:
        choices = ", ".join(SANDBOX_CHOICES)
        raise ValueError(f"limits.sandbox must be one of: {choices}")
    return value


_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# Prefix hints match the start of any underscore-separated segment; exact
# hints must equal a whole segment (so PAT flags GH_PAT but not PATH).
_SECRET_PREFIX_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "AUTH", "CRED", "COOKIE", "SESSION", "BEARER")
_SECRET_EXACT_HINTS = ("PAT",)


def _looks_like_secret_name(key: str) -> bool:
    segments = key.split("_")
    if any(seg in _SECRET_EXACT_HINTS for seg in segments):
        return True
    return any(seg.startswith(hint) for seg in segments for hint in _SECRET_PREFIX_HINTS)


_SECRET_VALUE_PREFIXES = ("sk-", "xoxb-", "ghp_", "github_pat_", "Bearer ", "sk_live_", "AKIA")


def _as_env(value: object, agent_name: str) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"agents.{agent_name}.env must be a TOML table")
    parsed: dict[str, str] = {}
    targets: dict[str, str] = {}
    for key, raw in value.items():
        if not isinstance(raw, str):
            raise ValueError(f"agents.{agent_name}.env.{key} must be a string")
        if not _ENV_NAME_RE.match(key):
            raise ValueError(f"agents.{agent_name}.env.{key} is not a valid environment variable name")
        if key.endswith("_REF"):
            target = key[: -len("_REF")]
            if agent_adapters.is_env_file_reference(raw):
                if not agent_adapters.ENV_FILE_REF_RE.match(raw):
                    raise ValueError(f"agents.{agent_name}.env.{key} must use env-file:/absolute/path#VARIABLE")
            elif not _ENV_NAME_RE.match(raw):
                raise ValueError(
                    f"agents.{agent_name}.env.{key} must name an environment variable to read the value from"
                )
        else:
            target = key
            if _looks_like_secret_name(key):
                raise ValueError(
                    f"agents.{agent_name}.env.{key} looks like a secret; pass it by reference with a _REF suffix "
                    f"naming an environment variable instead of an inline value"
                )
            if raw.startswith(_SECRET_VALUE_PREFIXES):
                raise ValueError(
                    f"agents.{agent_name}.env.{key} looks like a secret value; pass it by reference with a "
                    f"_REF suffix naming an environment variable instead"
                )
        if target in targets:
            raise ValueError(f"agents.{agent_name}.env.{key} collides with {targets[target]}: both resolve to {target}")
        targets[target] = key
        parsed[key] = raw
    return parsed or None


def is_cli_allowed(cli_ref: str, roster: Roster) -> bool:
    return _allowed(cli_ref, roster.allow_models)


def timeout_for(agent: Agent, roster: Roster) -> float:
    return agent.timeout_seconds if agent.timeout_seconds is not None else roster.timeout_seconds


def _allowed(cli_ref: str, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return True
    return any(fnmatch.fnmatchcase(cli_ref, pattern) for pattern in patterns)


def resolve_roster(target: Path, explicit: Path | None = None) -> RosterResolution:
    if explicit is not None:
        path = explicit.expanduser().resolve()
        if path.exists():
            return RosterResolution(path=path, source="explicit")
        raise FileNotFoundError(f"roster not found: {path}")

    workspace_path = (target.expanduser() / ".brigade" / "roster.toml").resolve()
    user_path = (Path.home() / ".brigade" / "roster.toml").expanduser().resolve()
    if workspace_path.exists():
        shadowed = (user_path,) if user_path != workspace_path and user_path.exists() else ()
        return RosterResolution(path=workspace_path, source="workspace", shadowed=shadowed)
    if user_path.exists():
        return RosterResolution(path=user_path, source="user")
    raise FileNotFoundError(f"roster not found: checked {workspace_path} and {user_path}")


def resolve_roster_path(target: Path, explicit: Path | None = None) -> Path:
    """Return only the selected path for callers that do not need provenance."""

    return resolve_roster(target, explicit).path


def load_roster(path: Path, *, resolution: RosterResolution | None = None) -> Roster:
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"roster not found: {path}")

    data = toml_compat.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("roster must be a TOML table")

    orchestrator = _as_str(data.get("orchestrator"), "orchestrator")
    raw_agents = data.get("agents")
    if not isinstance(raw_agents, dict) or not raw_agents:
        raise ValueError("roster needs an [agents] table")

    limits = data.get("limits", {})
    if limits is None:
        limits = {}
    if not isinstance(limits, dict):
        raise ValueError("[limits] must be a TOML table")

    max_workers = limits.get("max_workers", 4)
    if not isinstance(max_workers, int) or max_workers < 1:
        raise ValueError("limits.max_workers must be a positive integer")
    timeout_seconds = _as_positive_number(limits.get("timeout_seconds", 600.0), "limits.timeout_seconds")
    sandbox = _as_sandbox(limits.get("sandbox"))

    scheduler = limits.get("scheduler")
    if scheduler is not None and scheduler not in ("waves", "dag"):
        raise ValueError("limits.scheduler must be one of: waves, dag")

    codex_transport = data.get("codex_transport", "exec")
    if codex_transport not in CODEX_TRANSPORT_CHOICES:
        choices = ", ".join(CODEX_TRANSPORT_CHOICES)
        raise ValueError(f"codex_transport must be one of: {choices}")

    raw_allow_models = limits.get("allow_models", [])
    if raw_allow_models is None:
        raw_allow_models = []
    if not isinstance(raw_allow_models, list) or not all(isinstance(x, str) for x in raw_allow_models):
        raise ValueError("limits.allow_models must be a list of strings")
    allow_models = tuple(raw_allow_models)

    parsed_agents: dict[str, Agent] = {}
    for name, raw_agent in raw_agents.items():
        if not isinstance(raw_agent, dict):
            raise ValueError(f"agents.{name} must be a TOML table")
        agent_name = _as_str(name, "agent name")
        role = _as_str(raw_agent.get("role"), f"agents.{agent_name}.role")
        agent_timeout = raw_agent.get("timeout_seconds")
        timeout_seconds_for_agent = (
            None
            if agent_timeout is None
            else _as_positive_number(agent_timeout, f"agents.{agent_name}.timeout_seconds")
        )

        endpoint_raw = raw_agent.get("endpoint")
        model_raw = raw_agent.get("model")
        endpoint = _as_str(endpoint_raw, f"agents.{agent_name}.endpoint") if endpoint_raw is not None else None
        model = _as_str(model_raw, f"agents.{agent_name}.model") if model_raw is not None else None
        reasoning_raw = raw_agent.get("reasoning")
        reasoning = _as_str(reasoning_raw, f"agents.{agent_name}.reasoning") if reasoning_raw is not None else None
        transport_raw = raw_agent.get("transport", "direct")
        if not isinstance(transport_raw, str) or transport_raw not in AGENT_TRANSPORT_CHOICES:
            choices = ", ".join(AGENT_TRANSPORT_CHOICES)
            raise ValueError(f"agents.{agent_name}.transport must be one of: {choices}")
        transport_version_raw = raw_agent.get("transport_version")
        transport_version = (
            _as_str(transport_version_raw, f"agents.{agent_name}.transport_version")
            if transport_version_raw is not None
            else None
        )

        headers_raw = raw_agent.get("headers")
        if headers_raw is not None and not isinstance(headers_raw, dict):
            raise ValueError(f"agents.{agent_name}.headers must be a TOML table")
        headers = dict(headers_raw) if headers_raw is not None else None

        env = _as_env(raw_agent.get("env"), agent_name)
        fallback_raw = raw_agent.get("invalid_final_fallback")
        invalid_final_fallback = (
            _as_str(fallback_raw, f"agents.{agent_name}.invalid_final_fallback") if fallback_raw is not None else None
        )
        read_only_capable = _as_bool(
            raw_agent.get("read_only_capable", True),
            f"agents.{agent_name}.read_only_capable",
        )
        purpose = _as_optional_str(raw_agent.get("purpose"), f"agents.{agent_name}.purpose")
        requires = _as_requires(raw_agent.get("requires"), agent_name)
        fallback = _as_string_list(raw_agent.get("fallback"), f"agents.{agent_name}.fallback")
        stats = _as_stats(raw_agent.get("stats"), agent_name)
        caveats = _as_string_list(raw_agent.get("caveats"), f"agents.{agent_name}.caveats")

        cli_raw = raw_agent.get("cli")
        has_endpoint = endpoint is not None and model is not None
        if cli_raw is None and has_endpoint:
            cli = None
            if reasoning is not None:
                raise ValueError(f"agents.{agent_name}.reasoning requires a CLI adapter")
        else:
            cli = _as_str(cli_raw, f"agents.{agent_name}.cli")
            if not agent_adapters.is_known(cli):
                raise ValueError(f"agents.{agent_name}.cli is unknown: {cli!r}")
            if not _allowed(cli, allow_models):
                raise ValueError(f"agents.{agent_name}.cli is not allowed by limits.allow_models: {cli!r}")

        if env is not None and (transport_raw != "direct" or cli is None or cli.startswith("codex-cloud:")):
            raise ValueError(
                f"agents.{agent_name}.env overrides support direct CLI seats only; "
                f"acpx, codex-cloud, and endpoint seats manage their own environment"
            )
        if env is not None and cli == "codex" and codex_transport == "app-server":
            raise ValueError(
                f'agents.{agent_name}.env on a codex seat requires codex_transport = "exec"; '
                f"app-server sessions cannot apply per-seat env overrides"
            )

        if transport_raw == "acpx":
            if cli != "cursor":
                raise ValueError(f'agents.{agent_name}.transport acpx currently supports cli = "cursor" only')
            if model is None:
                raise ValueError(f"agents.{agent_name}.transport acpx requires model")
            if transport_version is None:
                raise ValueError(f"agents.{agent_name}.transport acpx requires transport_version")
            if transport_version != ACPX_TRANSPORT_VERSION:
                raise ValueError(
                    f"agents.{agent_name}.transport acpx reviewed version is {ACPX_TRANSPORT_VERSION}; "
                    f"got {transport_version}"
                )
            if agent_name == orchestrator:
                raise ValueError("acpx Cursor seats are workers only")
        elif transport_version is not None:
            raise ValueError(f'agents.{agent_name}.transport_version requires transport = "acpx"')

        parsed_agents[agent_name] = Agent(
            name=agent_name,
            cli=cli,
            role=role,
            timeout_seconds=timeout_seconds_for_agent,
            endpoint=endpoint,
            model=model,
            headers=headers,
            reasoning=reasoning,
            transport=transport_raw,
            transport_version=transport_version,
            env=env,
            invalid_final_fallback=invalid_final_fallback,
            read_only_capable=read_only_capable,
            purpose=purpose,
            requires=requires,
            fallback=fallback,
            stats=stats,
            caveats=caveats,
        )

    if orchestrator not in parsed_agents:
        raise ValueError(f"orchestrator {orchestrator!r} is not defined in [agents]")

    for agent_name, agent in parsed_agents.items():
        for fallback_name in agent.fallback:
            if fallback_name not in parsed_agents:
                raise ValueError(f"agents.{agent_name}.fallback references {fallback_name!r}, which is not defined")
            fallback_agent = parsed_agents[fallback_name]
            if fallback_agent.fallback:
                raise ValueError(
                    f"agents.{agent_name}.fallback target {fallback_name!r} must not define its own fallback"
                )
            if agent_name == orchestrator and fallback_agent.transport == "acpx":
                raise ValueError(f"agents.{agent_name}.fallback cannot alias an acpx worker into the orchestrator slot")
            if agent_name == orchestrator and (
                fallback_agent.cli is None or fallback_agent.cli.startswith("codex-cloud:")
            ):
                raise ValueError(
                    f"agents.{agent_name}.fallback cannot alias worker-only seat {fallback_name!r} "
                    "into the orchestrator slot"
                )
            if agent_name != orchestrator and fallback_name == orchestrator:
                raise ValueError(f"agents.{agent_name}.fallback cannot name the orchestrator {orchestrator!r}")

    for agent_name, agent in parsed_agents.items():
        if agent.requires is not None and "cli" in agent.requires:
            _validate_requires_cli(agent_name, agent)

    for agent_name, agent in parsed_agents.items():
        invalid_final_name = agent.invalid_final_fallback
        if invalid_final_name is None:
            continue
        if agent.cli != "grok" or agent.transport != "direct":
            raise ValueError(f"agents.{agent_name}.invalid_final_fallback requires a direct grok seat")
        invalid_final_agent = parsed_agents.get(invalid_final_name)
        if invalid_final_agent is None:
            raise ValueError(
                f"agents.{agent_name}.invalid_final_fallback references {invalid_final_name!r}, which is not defined"
            )
        if (
            invalid_final_name == orchestrator
            or invalid_final_agent.cli != "cursor"
            or invalid_final_agent.transport != "acpx"
        ):
            raise ValueError(f"agents.{agent_name}.invalid_final_fallback must name a reviewed cursor-grok acpx seat")
        if invalid_final_agent.model is None or not invalid_final_agent.model.lower().startswith("grok-"):
            raise ValueError(f"agents.{agent_name}.invalid_final_fallback target must use a grok model")
        if invalid_final_agent.transport_version != ACPX_TRANSPORT_VERSION:
            raise ValueError(
                f"agents.{agent_name}.invalid_final_fallback target requires reviewed acpx version "
                f"{ACPX_TRANSPORT_VERSION}"
            )

    return Roster(
        orchestrator=orchestrator,
        agents=parsed_agents,
        max_workers=max_workers,
        allow_models=allow_models,
        timeout_seconds=timeout_seconds,
        sandbox=sandbox,
        scheduler=scheduler,
        codex_transport=codex_transport,
        resolution=resolution,
    )


def workers(roster: Roster) -> list[Agent]:
    return [agent for name, agent in roster.agents.items() if name != roster.orchestrator]


def read_only_capability_error(agent: Agent) -> str | None:
    if agent.read_only_capable:
        return None
    return f"worker {agent.name!r} cannot run in read-only mode: agents.{agent.name}.read_only_capable is false"


def _seat_adapter_ref(agent: Agent) -> str | None:
    if agent.cli is None:
        return None
    if agent.cli.startswith("ollama:"):
        return "ollama"
    if agent.cli.startswith("codex-cloud:"):
        return "codex"
    return agent.cli


def _validate_requires_cli(agent_name: str, agent: Agent) -> None:
    required = agent.requires["cli"] if agent.requires is not None else None
    if required is None:
        return
    if agent.cli is None:
        raise ValueError(f"agents.{agent_name}.requires.cli is not supported on endpoint seats")
    adapter = _seat_adapter_ref(agent)
    if adapter != required:
        raise ValueError(f"agents.{agent_name}.requires.cli must match the seat adapter {adapter!r}, got {required!r}")


def _referenced_fallback_names(agents: dict[str, Agent]) -> frozenset[str]:
    referenced: set[str] = set()
    for agent in agents.values():
        referenced.update(agent.fallback)
    return frozenset(referenced)


def _requested_roots(roster: Roster) -> tuple[str, ...]:
    referenced = _referenced_fallback_names(roster.agents)
    return tuple(name for name in roster.agents if name == roster.orchestrator or name not in referenced)


def _probe_lookup_ref(agent: Agent) -> str | None:
    if agent.requires is None:
        return None
    if "cli" in agent.requires:
        return agent.requires["cli"]
    if agent.requires.get("auth") == "logged-in":
        return agent.cli
    return None


def _requirements_satisfied(agent: Agent, probe: CapabilityProbe) -> tuple[bool, str]:
    if agent.requires is None:
        return True, ""
    lookup_ref = _probe_lookup_ref(agent)
    if lookup_ref is None:
        return False, "logged-in auth requires a cli seat"
    capability = probe.lookup(lookup_ref)
    reasons: list[str] = []
    if "cli" in agent.requires:
        if not capability.installed:
            reasons.append(capability.detail or f"{lookup_ref} is not installed")
    if agent.requires.get("auth") == "logged-in" and not reasons:
        if capability.authenticated is not True:
            if capability.authenticated is False:
                reasons.append(capability.auth_detail or capability.detail or f"{lookup_ref} is not authenticated")
            elif capability.auth_detail:
                reasons.append(capability.auth_detail)
            else:
                reasons.append(f"{lookup_ref} authentication is unknown")
    return (not reasons, "; ".join(reasons))


def _resolved_self_agent(agent: Agent) -> Agent:
    return replace(agent, fallback=())


def _resolved_fallback_agent(requested_agent: Agent, fallback_agent: Agent, requested_name: str) -> Agent:
    return replace(
        fallback_agent,
        name=requested_name,
        role=requested_agent.role,
        purpose=requested_agent.purpose,
        fallback=(),
    )


def _is_reviewed_invalid_final_fallback(agent: Agent) -> bool:
    return (
        agent.cli == "cursor"
        and agent.transport == "acpx"
        and agent.model is not None
        and agent.model.lower().startswith("grok-")
        and agent.transport_version == ACPX_TRANSPORT_VERSION
    )


def _ensure_invalid_final_fallback_deps(
    roster: Roster,
    resolved_agents: dict[str, Agent],
    probe: CapabilityProbe,
) -> dict[str, Agent]:
    updated = dict(resolved_agents)
    for name, agent in list(updated.items()):
        dep_name = agent.invalid_final_fallback
        if dep_name is None:
            continue
        if dep_name in updated:
            if not _is_reviewed_invalid_final_fallback(updated[dep_name]):
                updated[name] = replace(agent, invalid_final_fallback=None)
            continue
        dep_agent = roster.agents.get(dep_name)
        if dep_agent is None:
            continue
        dep_ok, _ = _requirements_satisfied(dep_agent, probe)
        if dep_ok:
            updated[dep_name] = _resolved_self_agent(dep_agent)
        else:
            updated[name] = replace(agent, invalid_final_fallback=None)
    return updated


def resolve_capabilities(
    roster: Roster,
    probe: CapabilityProbe | None = None,
) -> RosterCapabilityResolution:
    active_probe = probe if probe is not None else HostCapabilityProbe()
    resolved_agents: dict[str, Agent] = {}
    report: list[SeatResolution] = []

    for requested_name in _requested_roots(roster):
        requested_agent = roster.agents[requested_name]
        satisfied, self_reason = _requirements_satisfied(requested_agent, active_probe)
        if satisfied:
            resolved_agents[requested_name] = _resolved_self_agent(requested_agent)
            report.append(
                SeatResolution(
                    requested=requested_name,
                    resolved=requested_name,
                    outcome="self",
                    reason="requirements satisfied",
                )
            )
            continue

        fallback_reasons: list[str] = []
        selected_fallback: str | None = None
        for fallback_name in requested_agent.fallback:
            fallback_agent = roster.agents.get(fallback_name)
            if fallback_agent is None:
                fallback_reasons.append(f"{fallback_name} is not defined")
                continue
            fallback_ok, fallback_reason = _requirements_satisfied(fallback_agent, active_probe)
            if fallback_ok:
                selected_fallback = fallback_name
                resolved_agents[requested_name] = _resolved_fallback_agent(
                    requested_agent,
                    fallback_agent,
                    requested_name,
                )
                reason_parts = [self_reason] if self_reason else []
                reason_parts.extend(fallback_reasons)
                reason_parts.append(f"selected fallback {fallback_name}")
                report.append(
                    SeatResolution(
                        requested=requested_name,
                        resolved=fallback_name,
                        outcome="fallback",
                        reason="; ".join(part for part in reason_parts if part),
                    )
                )
                break
            fallback_reasons.append(f"{fallback_name}: {fallback_reason}")

        if selected_fallback is None:
            reason_parts = [self_reason] if self_reason else []
            reason_parts.extend(fallback_reasons)
            report.append(
                SeatResolution(
                    requested=requested_name,
                    resolved=None,
                    outcome="dropped",
                    reason="; ".join(part for part in reason_parts if part),
                )
            )

    resolved_agents = _ensure_invalid_final_fallback_deps(roster, resolved_agents, active_probe)

    return RosterCapabilityResolution(
        roster=replace(roster, agents=resolved_agents),
        report=tuple(report),
    )


def _worker_result_failed(row: dict[str, object]) -> bool:
    ok = row.get("ok")
    if ok is False:
        return True
    if ok is True:
        return False
    status = row.get("status")
    if isinstance(status, str):
        normalized = status.strip().lower()
        if normalized in {"failed", "error", "fail", "failure"}:
            return True
        if normalized in {"ok", "success", "passed", "complete", "completed"}:
            return False
    exit_code = row.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
        return True
    return False


def collect_seat_receipt_stats(runs_root: Path) -> dict[str, SeatReceiptStats]:
    """Aggregate per-seat worker receipt durations and failure rates from local runs."""

    runs_root = runs_root.expanduser()
    if not runs_root.is_dir():
        return {}

    durations: dict[str, list[float]] = {}
    samples: dict[str, int] = {}
    failures: dict[str, int] = {}

    for run_dir in sorted(runs_root.iterdir()):
        if not run_dir.is_dir():
            continue
        path = run_dir / "worker-results.json"
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        results = payload.get("results")
        if not isinstance(results, list):
            continue
        for row in results:
            if not isinstance(row, dict):
                continue
            seat = row.get("worker")
            if not isinstance(seat, str) or not seat.strip():
                continue
            seat_name = seat.strip()
            samples[seat_name] = samples.get(seat_name, 0) + 1
            if _worker_result_failed(row):
                failures[seat_name] = failures.get(seat_name, 0) + 1
            duration = row.get("duration_seconds")
            if not isinstance(duration, (int, float)) or isinstance(duration, bool):
                continue
            durations.setdefault(seat_name, []).append(float(duration))

    stats: dict[str, SeatReceiptStats] = {}
    for seat_name, seat_durations in durations.items():
        sample_count = samples[seat_name]
        if sample_count <= 0:
            continue
        stats[seat_name] = SeatReceiptStats(
            sample_count=sample_count,
            median_duration_seconds=float(statistics.median(seat_durations)),
            failure_rate=failures.get(seat_name, 0) / sample_count,
        )
    return stats
