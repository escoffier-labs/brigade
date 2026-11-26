"""Read-only probes for the local Orca route."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict
from pathlib import Path
from typing import NoReturn

from . import native_orca_host, proc
from .native_child_host import NativeChildError
from .native_orca_dispatch import CLI
from .roster import Roster
from .roster_readiness import SOURCE_ORCA_LOCAL_PROBE, Observation, ReadinessRequest

PROBE_TIMEOUT_SECONDS = 12.0
# The flags used by launch_argv and native_orca_observations. Require the full
# worker-start shape, including flags used only for some target declarations.
_REQUIRED_FLAGS = {
    "worker-start": {
        "environment",
        "retry-request",
        "run",
        "task",
        "worktree",
        "base-branch",
        "agent",
        "model",
        "effort",
        "json",
        "full",
    },
    "worker-show": {"environment", "dispatch", "json", "full"},
    "worker-stop": {"environment", "dispatch", "retry-request", "json"},
    "worker-read": {"environment", "dispatch", "source", "json"},
    "inbox": {"environment", "run", "limit", "json", "full"},
}
_CREDENTIAL_ENV_NAME = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_KEY|APIKEY|PRIVATE_KEY|AUTH"
    r"|^(?:OP_|AWS_|GH_|GITHUB_|ANTHROPIC_|OPENAI_|GOOGLE_APPLICATION)"
)


def _declared_target(roster: Roster, seat: str) -> native_orca_host.NativeTarget | None:
    # Readiness only needs the declaration. Dispatch binds the enrollment revision.
    try:
        return native_orca_host.declared_target(roster, seat, source_revision="0" * 40)
    except NativeChildError:
        return None


def target_scope(roster: Roster, request: ReadinessRequest) -> dict[str, dict[str, object] | None]:
    return {
        seat: asdict(target) if (target := _declared_target(roster, seat)) is not None else None
        for seat in request.workers
    }


def _probe(argv: list[str]) -> tuple[str, str | None]:
    env = {name: value for name, value in os.environ.items() if not _CREDENTIAL_ENV_NAME.search(name.upper())}
    try:
        result = proc.run(argv, timeout=PROBE_TIMEOUT_SECONDS, env=env, stdin=b"", supervise_group=True)
    except OSError:
        return "", "failed"
    if result.code == 124:
        return "", "timeout"
    if result.code != 0 or result.decode_failed or result.output_limit_exceeded:
        return "", "failed"
    return result.stdout, None


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate registry key")
    return result


def _invalid_constant(_: str) -> NoReturn:
    raise ValueError("invalid registry constant")


def _registry_commands(stdout: str) -> dict[tuple[str, ...], set[str]]:
    payload = json.loads(stdout, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
    if not isinstance(payload, dict):
        raise ValueError("invalid registry")
    schema = payload.get("schemaVersion")
    entries = payload.get("commands")
    if type(schema) is not int or schema != 1 or not isinstance(entries, list):
        raise ValueError("unrecognized registry schema")
    commands: dict[tuple[str, ...], set[str]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid registry command")
        path, flags = entry.get("path"), entry.get("flags")
        if not isinstance(path, list) or not path or not all(isinstance(part, str) and part for part in path):
            raise ValueError("invalid command path")
        if not isinstance(flags, list) or not all(isinstance(flag, str) and flag for flag in flags):
            raise ValueError("invalid command flags")
        key = tuple(path)
        if key in commands:
            raise ValueError("duplicate command path")
        commands[key] = set(flags)
    return commands


def orca_route_observations(
    roster: Roster, request: ReadinessRequest, *, scope: str
) -> dict[str, Observation | tuple[Observation, ...]]:
    """Collect evidence until a prerequisite fails, with no native dispatch."""

    def observed(status: str, reason: str, *, detail: str = "", seat: str | None = None) -> Observation:
        return Observation(status, time.time(), scope, detail, seat=seat, reason=reason, source=SOURCE_ORCA_LOCAL_PROBE)

    checks: dict[str, Observation | tuple[Observation, ...]] = {}
    command_name, orchestration = CLI
    # Add a configurable command only when dispatch honors it too.
    try:
        executable = proc.resolve_executable(command_name)
    except (OSError, ValueError):
        checks["orca-binary"] = observed("unknown", "orca-binary-resolution-failed")
        return checks
    if executable.path is None:
        checks["orca-binary"] = observed(
            "failed", "orca-binary-missing", detail=f"Make {command_name} available on PATH."
        )
        return checks
    if not executable.runnable:
        checks["orca-binary"] = observed("unknown", "orca-binary-unrunnable")
        return checks
    command = str(Path(executable.path).absolute())
    checks["orca-binary"] = observed("passed", "orca-binary-resolved")

    stdout, error = _probe([command, "--version"])
    if error is not None:
        checks["orca-version"] = observed("unknown", f"orca-version-probe-{error}")
        return checks
    version = stdout.strip().removeprefix("v")
    if not native_orca_host.valid_version(version):
        checks["orca-version"] = observed("unknown", "orca-version-unrecognized")
        return checks
    checks["orca-version"] = observed("passed", "orca-version-parsed", detail=version)

    # Keep the existing evidence category for readers of readiness v1.
    category = "orca-retry-request"
    stdout, error = _probe([command, "agent-context", "--json"])
    if error is not None:
        checks[category] = observed("unknown", f"orca-registry-probe-{error}")
        return checks
    try:
        commands = _registry_commands(stdout)
    except (ValueError, RecursionError):
        checks[category] = observed("unknown", "orca-registry-unrecognized")
        return checks
    for verb, required in _REQUIRED_FLAGS.items():
        if not required.issubset(commands.get((orchestration, verb), ())):
            checks[category] = observed("unsupported", "orca-command-capabilities-missing", detail=verb)
            return checks
    checks[category] = observed("passed", "orca-command-capabilities-present")

    agents = []
    for seat in request.workers:
        target = _declared_target(roster, seat)
        if target is None:
            agents.append(
                observed(
                    "unsupported",
                    "orca-target-invalid",
                    detail="Declare a valid Orca target for this seat.",
                    seat=seat,
                )
            )
        elif (gap := native_orca_host.structural_gap(target)) is not None:
            agents.append(observed("unsupported", f"orca-target-{gap}", seat=seat))
        elif target.orca_version != version:
            agents.append(
                observed("unsupported", "orca-target-version-mismatch", detail=target.orca_version, seat=seat)
            )
        else:
            agents.append(observed("passed", "orca-agent-declared", detail=target.agent, seat=seat))
    checks["orca-agent"] = tuple(agents)
    return checks
