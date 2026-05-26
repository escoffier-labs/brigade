"""Load and validate a Brigade aboyeur roster."""
from __future__ import annotations

import fnmatch
import ast
from dataclasses import dataclass
from pathlib import Path

from . import agents as agent_adapters

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on Python 3.10
    tomllib = None


@dataclass(frozen=True)
class Agent:
    name: str
    cli: str
    role: str
    timeout_seconds: float | None = None


@dataclass(frozen=True)
class Roster:
    orchestrator: str
    agents: dict[str, Agent]
    max_workers: int = 4
    allow_models: tuple[str, ...] = ()
    timeout_seconds: float = 600.0


def _as_str(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _as_positive_number(value: object, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{field} must be a positive number")
    return float(value)


def is_cli_allowed(cli_ref: str, roster: Roster) -> bool:
    return _allowed(cli_ref, roster.allow_models)


def timeout_for(agent: Agent, roster: Roster) -> float:
    return agent.timeout_seconds if agent.timeout_seconds is not None else roster.timeout_seconds


def _allowed(cli_ref: str, patterns: tuple[str, ...]) -> bool:
    if not patterns:
        return True
    return any(fnmatch.fnmatchcase(cli_ref, pattern) for pattern in patterns)


def _fallback_toml_loads(text: str) -> dict[str, object]:
    """Parse the small TOML subset used by rosters on Python 3.10."""
    data: dict[str, object] = {}
    current: dict[str, object] = data

    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("[") and line.endswith("]"):
            path = [part.strip() for part in line[1:-1].split(".") if part.strip()]
            if not path:
                raise ValueError(f"invalid TOML table on line {line_number}")
            current = data
            for part in path:
                next_table = current.setdefault(part, {})
                if not isinstance(next_table, dict):
                    raise ValueError(f"invalid TOML table on line {line_number}")
                current = next_table
            continue
        if "=" not in line:
            raise ValueError(f"invalid TOML assignment on line {line_number}")
        key, raw_value = line.split("=", 1)
        key = key.strip()
        raw_value = raw_value.strip()
        if not key:
            raise ValueError(f"invalid TOML key on line {line_number}")
        try:
            current[key] = ast.literal_eval(raw_value)
        except (SyntaxError, ValueError) as exc:
            raise ValueError(f"unsupported TOML value on line {line_number}") from exc

    return data


def _loads_toml(text: str) -> dict[str, object]:
    if tomllib is not None:
        return tomllib.loads(text)
    return _fallback_toml_loads(text)


def load_roster(path: Path) -> Roster:
    if not path.exists():
        raise FileNotFoundError(f"roster not found: {path}")

    data = _loads_toml(path.read_text())
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
        cli = _as_str(raw_agent.get("cli"), f"agents.{agent_name}.cli")
        role = _as_str(raw_agent.get("role"), f"agents.{agent_name}.role")
        agent_timeout = raw_agent.get("timeout_seconds")
        timeout_seconds_for_agent = (
            None
            if agent_timeout is None
            else _as_positive_number(agent_timeout, f"agents.{agent_name}.timeout_seconds")
        )
        if not agent_adapters.is_known(cli):
            raise ValueError(f"agents.{agent_name}.cli is unknown: {cli!r}")
        if not _allowed(cli, allow_models):
            raise ValueError(f"agents.{agent_name}.cli is not allowed by limits.allow_models: {cli!r}")
        parsed_agents[agent_name] = Agent(
            name=agent_name,
            cli=cli,
            role=role,
            timeout_seconds=timeout_seconds_for_agent,
        )

    if orchestrator not in parsed_agents:
        raise ValueError(f"orchestrator {orchestrator!r} is not defined in [agents]")

    return Roster(
        orchestrator=orchestrator,
        agents=parsed_agents,
        max_workers=max_workers,
        allow_models=allow_models,
        timeout_seconds=timeout_seconds,
    )


def workers(roster: Roster) -> list[Agent]:
    return [agent for name, agent in roster.agents.items() if name != roster.orchestrator]
