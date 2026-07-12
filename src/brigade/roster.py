"""Load and validate a Brigade aboyeur roster."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

from . import agents as agent_adapters
from . import toml_compat

SANDBOX_CHOICES = ("read-only", "workspace-write", "danger-full-access")
CODEX_TRANSPORT_CHOICES = ("exec", "app-server")


@dataclass(frozen=True)
class Agent:
    name: str
    cli: str | None
    role: str
    timeout_seconds: float | None = None
    endpoint: str | None = None
    model: str | None = None
    headers: dict | None = None


@dataclass(frozen=True)
class Roster:
    orchestrator: str
    agents: dict[str, Agent]
    max_workers: int = 4
    allow_models: tuple[str, ...] = ()
    timeout_seconds: float = 600.0
    sandbox: str | None = None
    codex_transport: str = "exec"

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


def _as_sandbox(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in SANDBOX_CHOICES:
        choices = ", ".join(SANDBOX_CHOICES)
        raise ValueError(f"limits.sandbox must be one of: {choices}")
    return value


def is_cli_allowed(cli_ref: str, roster: Roster) -> bool:
    return _allowed(cli_ref, roster.allow_models)


def timeout_for(agent: Agent, roster: Roster) -> float:
    return agent.timeout_seconds if agent.timeout_seconds is not None else roster.timeout_seconds


def _allowed(cli_ref: str, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return True
    return any(fnmatch.fnmatchcase(cli_ref, pattern) for pattern in patterns)


def resolve_roster_path(target: Path, explicit: Path | None = None) -> Path:
    if explicit is not None:
        path = explicit.expanduser()
        if path.exists():
            return path
        raise FileNotFoundError(f"roster not found: {path}")

    workspace_path = target.expanduser() / ".brigade" / "roster.toml"
    user_path = Path.home() / ".brigade" / "roster.toml"
    if workspace_path.exists():
        return workspace_path
    if user_path.exists():
        return user_path
    raise FileNotFoundError(f"roster not found: checked {workspace_path} and {user_path}")


def load_roster(path: Path) -> Roster:
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

        headers_raw = raw_agent.get("headers")
        if headers_raw is not None and not isinstance(headers_raw, dict):
            raise ValueError(f"agents.{agent_name}.headers must be a TOML table")
        headers = dict(headers_raw) if headers_raw is not None else None

        cli_raw = raw_agent.get("cli")
        has_endpoint = endpoint is not None and model is not None
        if cli_raw is None and has_endpoint:
            cli = None
        else:
            cli = _as_str(cli_raw, f"agents.{agent_name}.cli")
            if not agent_adapters.is_known(cli):
                raise ValueError(f"agents.{agent_name}.cli is unknown: {cli!r}")
            if not _allowed(cli, allow_models):
                raise ValueError(f"agents.{agent_name}.cli is not allowed by limits.allow_models: {cli!r}")

        parsed_agents[agent_name] = Agent(
            name=agent_name,
            cli=cli,
            role=role,
            timeout_seconds=timeout_seconds_for_agent,
            endpoint=endpoint,
            model=model,
            headers=headers,
        )

    if orchestrator not in parsed_agents:
        raise ValueError(f"orchestrator {orchestrator!r} is not defined in [agents]")

    return Roster(
        orchestrator=orchestrator,
        agents=parsed_agents,
        max_workers=max_workers,
        allow_models=allow_models,
        timeout_seconds=timeout_seconds,
        sandbox=sandbox,
        codex_transport=codex_transport,
    )


def workers(roster: Roster) -> list[Agent]:
    return [agent for name, agent in roster.agents.items() if name != roster.orchestrator]
