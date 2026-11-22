"""Read-only probes for the local Orca route."""

from __future__ import annotations

import re
import time
from pathlib import Path

from . import native_orca_host, proc
from .roster import Roster
from .roster_readiness import SOURCE_ORCA_LOCAL_PROBE, Observation, ReadinessRequest

PROBE_TIMEOUT_SECONDS = 5.0
_VERSION = re.compile(r"v?(\d+\.\d+\.\d+(?:-[\w.-]+)?(?:\+[\w.-]+)?)", re.ASCII)
_WORKER_START_USAGE = re.compile(r"^Usage:[^\r\n]+\bworker-start\b", re.MULTILINE)
_RETRY_REQUEST_OPTION = re.compile(r"^[ \t]+--retry-request(?=[ \t=\r\n]|$)", re.MULTILINE)


def _probe(argv: list[str]) -> tuple[str, str | None]:
    try:
        result = proc.run(argv, timeout=PROBE_TIMEOUT_SECONDS, stdin=b"", supervise_group=True)
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
    try:
        executable = proc.resolve_executable(roster.orca_command)
    except (OSError, ValueError):
        checks["orca-binary"] = observed("unknown", "orca-binary-resolution-failed")
        return checks
    if executable.path is None:
        checks["orca-binary"] = observed(
            "failed", "orca-binary-missing", detail="Make the configured Orca command available."
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

    stdout, error = _probe([command, "orchestration", "worker-start", "--help"])
    if error is not None:
        checks["orca-retry-request"] = observed("unknown", f"orca-help-probe-{error}")
        return checks
    if not _WORKER_START_USAGE.search(stdout):
        checks["orca-retry-request"] = observed("unknown", "orca-worker-start-help-unrecognized")
        return checks
    if not _RETRY_REQUEST_OPTION.search(stdout):
        checks["orca-retry-request"] = observed("unsupported", "orca-retry-request-unsupported")
        return checks
    checks["orca-retry-request"] = observed("passed", "orca-retry-request-supported")

    agents = []
    for seat in request.workers:
        agent = roster.orca_targets.get(seat, {}).get("agent")
        if isinstance(agent, str) and agent in native_orca_host.AGENT_EFFORTS:
            agents.append(observed("passed", "orca-agent-enabled", detail=agent, seat=seat))
        else:
            agents.append(
                observed(
                    "unsupported",
                    "orca-agent-disabled",
                    detail="Declare an agent supported by Brigade's Orca adapter for this seat.",
                    seat=seat,
                )
            )
    checks["orca-agent"] = tuple(agents)
    return checks
