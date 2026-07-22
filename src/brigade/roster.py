"""Load and validate a Brigade aboyeur roster."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from . import agents as agent_adapters
from . import toml_compat
from .templates import template_root

SANDBOX_CHOICES = ("read-only", "workspace-write", "danger-full-access")
BUNDLED_PRESET_IDS = frozenset({"minimal", "budget-open-weight", "review-heavy", "full-multi-lane"})
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
class SeatRequirements:
    cli: str | None = None
    auth: str | None = None


@dataclass(frozen=True)
class SeatStats:
    speed: str
    source: str


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
    spec: str | None = None
    requires: SeatRequirements | None = None
    fallback: tuple[str, ...] = ()
    stats: SeatStats | None = None
    caveats: tuple[str, ...] = ()


@dataclass(frozen=True)
class Roster:
    orchestrator: str
    agents: dict[str, Agent]
    max_workers: int = 4
    allow_models: tuple[str, ...] = ()
    timeout_seconds: float = 600.0
    sandbox: str | None = None
    codex_transport: str = "exec"
    resolution: RosterResolution | None = None

    def find_role(self, role: str) -> Agent | None:
        return next((a for a in self.agents.values() if a.role == role), None)


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


def _as_sandbox(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in SANDBOX_CHOICES:
        choices = ", ".join(SANDBOX_CHOICES)
        raise ValueError(f"limits.sandbox must be one of: {choices}")
    return value


def _as_optional_str(value: object, field: str) -> str | None:
    if value is None:
        return None
    return _as_str(value, field)


def _as_string_list(value: object, field: str, *, allow_empty: bool = False) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list of strings")
    if not value and not allow_empty:
        raise ValueError(f"{field} must be a non-empty list of strings")
    parsed: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{field}[{index}] must be a non-empty string")
        parsed.append(item.strip())
    return tuple(parsed)


def _as_requires(value: object, agent_name: str) -> SeatRequirements | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"agents.{agent_name}.requires must be a TOML table")
    if not value:
        return None
    unknown = set(value) - {"cli", "auth"}
    if unknown:
        keys = ", ".join(sorted(unknown))
        raise ValueError(f"agents.{agent_name}.requires has unknown keys: {keys}")
    cli = _as_optional_str(value.get("cli"), f"agents.{agent_name}.requires.cli")
    auth = _as_optional_str(value.get("auth"), f"agents.{agent_name}.requires.auth")
    if auth is not None and auth != "logged-in":
        raise ValueError(f"agents.{agent_name}.requires.auth must be 'logged-in'")
    if auth is not None and cli is None:
        raise ValueError(f"agents.{agent_name}.requires.auth requires requires.cli")
    if cli is None and auth is None:
        return None
    return SeatRequirements(cli=cli, auth=auth)


def _as_stats(value: object, agent_name: str) -> SeatStats | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"agents.{agent_name}.stats must be a TOML table")
    speed = _as_str(value.get("speed"), f"agents.{agent_name}.stats.speed")
    source = _as_str(value.get("source"), f"agents.{agent_name}.stats.source")
    unknown = set(value) - {"speed", "source"}
    if unknown:
        keys = ", ".join(sorted(unknown))
        raise ValueError(f"agents.{agent_name}.stats has unknown keys: {keys}")
    return SeatStats(speed=speed, source=source)


def _as_caveats(value: object, agent_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ValueError(f"agents.{agent_name}.caveats must be a list of strings")
    parsed: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(f"agents.{agent_name}.caveats[{index}] must be a string")
        parsed.append(item.strip())
    return tuple(parsed)


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


def _validate_preset_id(preset_id: str) -> str:
    normalized = preset_id.strip()
    if not normalized:
        raise ValueError("preset id must be a non-empty string")
    if normalized != preset_id or normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
        raise ValueError(f"unknown preset: {preset_id!r}")
    if normalized not in BUNDLED_PRESET_IDS:
        raise ValueError(f"unknown preset: {preset_id!r}")
    return normalized


def preset_path(preset_id: str) -> Path:
    """Return the packaged TOML path for a curated bundled roster preset."""

    normalized = _validate_preset_id(preset_id)
    return template_root() / "rosters" / f"{normalized}.toml"


def load_preset(preset_id: str) -> Roster:
    """Load a curated bundled roster preset by id."""

    path = preset_path(preset_id)
    if not path.is_file():
        raise FileNotFoundError(f"preset not found: {preset_id!r} (looked at {path})")
    return load_roster(path)


def list_preset_ids() -> tuple[str, ...]:
    """Return the stable bundled preset ids in catalog order."""

    return tuple(sorted(BUNDLED_PRESET_IDS))


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
        spec = _as_optional_str(raw_agent.get("spec"), f"agents.{agent_name}.spec")
        requires = _as_requires(raw_agent.get("requires"), agent_name)
        seat_fallback_raw = raw_agent.get("fallback")
        if seat_fallback_raw is None:
            fallback: tuple[str, ...] = ()
        else:
            fallback = _as_string_list(seat_fallback_raw, f"agents.{agent_name}.fallback", allow_empty=True)
        stats = _as_stats(raw_agent.get("stats"), agent_name)
        caveats = _as_caveats(raw_agent.get("caveats"), agent_name)

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
            spec=spec,
            requires=requires,
            fallback=fallback,
            stats=stats,
            caveats=caveats,
        )

    if orchestrator not in parsed_agents:
        raise ValueError(f"orchestrator {orchestrator!r} is not defined in [agents]")

    for agent_name, agent in parsed_agents.items():
        fallback_name = agent.invalid_final_fallback
        if fallback_name is None:
            continue
        if agent.cli != "grok" or agent.transport != "direct":
            raise ValueError(f"agents.{agent_name}.invalid_final_fallback requires a direct grok seat")
        fallback_agent = parsed_agents.get(fallback_name)
        if fallback_agent is None:
            raise ValueError(
                f"agents.{agent_name}.invalid_final_fallback references {fallback_name!r}, which is not defined"
            )
        if fallback_name == orchestrator or fallback_agent.cli != "cursor" or fallback_agent.transport != "acpx":
            raise ValueError(f"agents.{agent_name}.invalid_final_fallback must name a reviewed cursor-grok acpx seat")
        if fallback_agent.model is None or not fallback_agent.model.lower().startswith("grok-"):
            raise ValueError(f"agents.{agent_name}.invalid_final_fallback target must use a grok model")
        if fallback_agent.transport_version != ACPX_TRANSPORT_VERSION:
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
        codex_transport=codex_transport,
        resolution=resolution,
    )


def workers(roster: Roster) -> list[Agent]:
    return [agent for name, agent in roster.agents.items() if name != roster.orchestrator]


def read_only_capability_error(agent: Agent) -> str | None:
    if agent.read_only_capable:
        return None
    return f"worker {agent.name!r} cannot run in read-only mode: agents.{agent.name}.read_only_capable is false"
