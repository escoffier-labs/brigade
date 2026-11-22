"""Read-only probes for the local Orca route."""

from __future__ import annotations

import os
import re
import time
from dataclasses import asdict
from pathlib import Path

from . import native_orca_host, proc
from .native_child_host import NativeChildError
from .native_orca_dispatch import CLI
from .roster import Roster
from .roster_readiness import SOURCE_ORCA_LOCAL_PROBE, Observation, ReadinessRequest

PROBE_TIMEOUT_SECONDS = 12.0
_VERSION = re.compile(r"v?(\d+\.\d+\.\d+(?:-[\w.-]+)?(?:\+[\w.-]+)?)", re.ASCII)
# Orca 1.4.269 usage with both required selector groups and optional flags.
_WORKER_START_USAGE = re.compile(
    r"^Usage: orca-ide orchestration worker-start "
    r"\(--task <task_id> \| --spec <text> \| --spec-file <path\|->\)"
    r"(?: \[--[^\]\r\n]+\])* \(--agent <agent> \| --terminal <handle>\)"
    r"(?: \[--[^\]\r\n]+\])*[ \t]*\r?$",
    re.MULTILINE,
)
_OPTIONS = re.compile(r"^Options:[ \t]*\r?\n(.*?)(?=^\S[^\r\n]*:[^\S\r\n]*\r?$|\Z)", re.MULTILINE | re.DOTALL)
_RETRY_REQUEST_OPTION = re.compile(r"^[ \t]+--retry-request(?=[ \t=\r\n]|$)", re.MULTILINE)
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
    version_match = _VERSION.fullmatch(stdout.strip())
    if version_match is None:
        checks["orca-version"] = observed("unknown", "orca-version-unrecognized")
        return checks
    version = version_match[1]
    if version not in native_orca_host.SUPPORTED_VERSIONS:
        checks["orca-version"] = observed("unsupported", "orca-version-unsupported", detail=version)
        return checks
    checks["orca-version"] = observed("passed", "orca-version-supported", detail=version)

    stdout, error = _probe([command, orchestration, "worker-start", "--help"])
    if error is not None:
        checks["orca-retry-request"] = observed("unknown", f"orca-help-probe-{error}")
        return checks
    usage = _WORKER_START_USAGE.search(stdout)
    options = _OPTIONS.search(stdout, usage.end()) if usage is not None else None
    if options is None:
        checks["orca-retry-request"] = observed("unknown", "orca-worker-start-help-unrecognized")
        return checks
    if not _RETRY_REQUEST_OPTION.search(options[1]):
        checks["orca-retry-request"] = observed("unsupported", "orca-retry-request-unsupported")
        return checks
    checks["orca-retry-request"] = observed("passed", "orca-retry-request-supported")

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
        else:
            agents.append(observed("passed", "orca-agent-enabled", detail=target.agent, seat=seat))
    checks["orca-agent"] = tuple(agents)
    return checks
