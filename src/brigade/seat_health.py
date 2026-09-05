"""Shared, redacted seat-health probes.

This module deliberately has no run-admission policy.  It answers whether a
declared seat is healthy now and writes a receipt that later routing work can
consume.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import time
from concurrent.futures import TimeoutError as FuturesTimeoutError
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Protocol
from urllib.parse import urlsplit

from . import acpx_adapter, agents, model_inventory, proc
from .worker_failure import (
    FAILURE_CLASS_SPECS,
    FailureClass,
    FailurePhase,
    WorkerFailure,
    safe_detail,
)

SCHEMA = "brigade.seat_health.v1"
PER_SEAT_TIMEOUT_SECONDS = 30.0
OVERALL_TIMEOUT_SECONDS = 35.0
MODEL_REACHABILITY_TIMEOUT_SECONDS = 15.0
HEALTHY_CACHE_TTL_SECONDS = 5 * 60.0
UNHEALTHY_CACHE_TTL_SECONDS = 30.0
DETAIL_LIMIT = 240
# agy 1.1.25 was installed when #1418 added these prompt-free probes. Keep
# that reviewed version as the floor because earlier releases were not checked
# to support both `agy models` and `agy --version` in automation.
_ANTIGRAVITY_MIN_VERSION = (1, 1, 25)
_ANTIGRAVITY_PROBE_TIMEOUT_SECONDS = 2.0
_MODEL_SMOKE_MARKER = "BRIGADE_SEAT_HEALTH_EXACT_MARKER"
_MODEL_SMOKE_PROMPT = (
    "Return exactly BRIGADE_SEAT_HEALTH_EXACT_MARKER and nothing else. Do not use tools. Do not write files."
)

CheckName = Literal[
    "declaration",
    "executable-identity",
    "authentication-entitlement",
    "transport-liveness",
    "version-gates",
    "model-reachability",
    "isolation-compatibility",
]


def _resolve_agent_executable(seat: Any) -> proc.ExecutableIdentity:
    command = getattr(seat, "command", None)
    if command is None:
        return agents.resolve_agent_executable(seat.cli)
    return agents.resolve_agent_executable(seat.cli, command=command)


CheckStatus = Literal["passed", "degraded", "failed"]
HealthStatus = Literal["healthy", "degraded", "unhealthy"]

CHECK_NAMES: tuple[CheckName, ...] = (
    "declaration",
    "executable-identity",
    "authentication-entitlement",
    "transport-liveness",
    "version-gates",
    "model-reachability",
    "isolation-compatibility",
)

# Explicitly public, rather than inferred from the roster, so new adapters
# cannot silently skip health checks.
ADAPTER_CHECK_MATRIX: dict[str, tuple[CheckName, ...]] = {
    "direct": CHECK_NAMES,
    "endpoint": ("declaration", "authentication-entitlement", "transport-liveness", "model-reachability"),
    "codex-exec": CHECK_NAMES,
    "codex-app-server": CHECK_NAMES,
    "cursor-acpx": CHECK_NAMES,
}


@dataclass(frozen=True)
class SeatHealthCheck:
    name: CheckName
    status: CheckStatus
    detail: str = ""
    duration_seconds: float = 0.0
    cause_code: str | None = None

    def payload(self) -> dict[str, object]:
        result: dict[str, object] = {
            "name": self.name,
            "status": self.status,
            "duration_seconds": round(max(self.duration_seconds, 0.0), 3),
        }
        if self.detail:
            result["detail"] = safe_detail(self.detail)[:DETAIL_LIMIT]
        if self.cause_code:
            result["cause_code"] = self.cause_code[:80]
        return result


@dataclass(frozen=True)
class SeatHealthResult:
    probe_id: str
    seat: str
    fingerprint: str
    status: HealthStatus
    requested: dict[str, str | None]
    checks: tuple[SeatHealthCheck, ...]
    started_at: float
    finished_at: float
    started_wall_at: float
    finished_wall_at: float
    cached: bool = False
    cache_age_seconds: float | None = None
    failure: WorkerFailure | None = None
    resolution: dict[str, str] | None = None

    def payload(self) -> dict[str, object]:
        result: dict[str, object] = {
            "probe_id": self.probe_id,
            "seat": self.seat,
            "fingerprint": self.fingerprint,
            "status": self.status,
            "started_at": _timestamp(self.started_wall_at),
            "finished_at": _timestamp(self.finished_wall_at),
            "duration_seconds": round(max(self.finished_at - self.started_at, 0.0), 3),
            "cached": self.cached,
            "requested": {key: value for key, value in self.requested.items() if value is not None},
            "checks": [check.payload() for check in self.checks],
        }
        if self.cache_age_seconds is not None:
            result["cache_age_seconds"] = round(max(self.cache_age_seconds, 0.0), 3)
        if self.failure is not None:
            failure = self.failure.payload()
            failure["probe_id"] = self.probe_id
            result["failure"] = failure
        if self.resolution is not None:
            result["resolution"] = dict(self.resolution)
        return result


class AdapterChecks(Protocol):
    """Adapter-owned checks, injectable so probe tests never require a live seat."""

    def check(
        self,
        name: CheckName,
        *,
        seat: Any,
        roster: Any,
        workspace: Path | None,
        timeout_seconds: float,
    ) -> SeatHealthCheck: ...


@dataclass(frozen=True)
class _CacheEntry:
    result: SeatHealthResult
    stored_at: float


class SeatHealthProbe:
    """Probe declared roots and fallbacks with bounded parallel work.

    ``adapter`` is normally omitted, which uses the existing CLI, inventory,
    ACPX, and app-server primitives.  Fixture adapters supply all checks in
    tests, preventing credentials and network calls.
    """

    def __init__(
        self,
        *,
        adapter: AdapterChecks | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        model_smoke: Callable[[Any, Any, Path, float], SeatHealthCheck] | None = None,
        collect_executable_version: bool = True,
    ) -> None:
        self._adapter = adapter
        self._clock = clock
        self._wall_clock = wall_clock
        self._model_smoke = model_smoke
        self._collect_executable_version = collect_executable_version
        self._cache: dict[str, _CacheEntry] = {}
        self._cache_lock = threading.Lock()

    def probe(
        self,
        seat: Any,
        roster: Any,
        *,
        workspace: Path | None = None,
        allow_model_smoke: bool = True,
        require_hard_isolation: bool = False,
        sandbox: str | None = None,
    ) -> SeatHealthResult:
        """Probe one seat.  The caller decides whether to serialize the result."""
        started = self._clock()
        started_wall_at = self._wall_clock()
        requested = _requested(seat, roster)
        fingerprint = seat_fingerprint(seat, roster, executable_version=self._executable_version(seat))
        cached = self._cached(fingerprint, started)
        if cached is not None:
            # Workspace and isolation state are intentionally recomputed.
            retained = tuple(check for check in cached.checks if check.name != "isolation-compatibility")
            isolation = self._run_check(
                "isolation-compatibility",
                seat,
                roster,
                workspace,
                PER_SEAT_TIMEOUT_SECONDS,
                allow_model_smoke,
                require_hard_isolation=require_hard_isolation,
                sandbox=sandbox,
            )
            cached_checks = (*retained, isolation)
            return _result(
                cached.probe_id,
                seat.name,
                fingerprint,
                requested,
                cached_checks,
                started,
                self._clock(),
                started_wall_at,
                self._wall_clock(),
                cached=True,
                cache_age_seconds=started - cached.started_at,
            )

        deadline = started + PER_SEAT_TIMEOUT_SECONDS
        checks: list[SeatHealthCheck] = []
        for name in ADAPTER_CHECK_MATRIX[_adapter_kind(seat, roster)]:
            remaining = deadline - self._clock()
            if remaining <= 0:
                checks.append(_timeout_check(name))
                break
            budget = min(remaining, MODEL_REACHABILITY_TIMEOUT_SECONDS) if name == "model-reachability" else remaining
            checks.append(
                self._run_check(
                    name,
                    seat,
                    roster,
                    workspace,
                    budget,
                    allow_model_smoke,
                    require_hard_isolation=require_hard_isolation,
                    sandbox=sandbox,
                )
            )
        result = _result(
            _probe_id(seat.name, started),
            seat.name,
            fingerprint,
            requested,
            tuple(checks),
            started,
            self._clock(),
            started_wall_at,
            self._wall_clock(),
        )
        with self._cache_lock:
            self._cache[fingerprint] = _CacheEntry(result, self._clock())
        return result

    def probe_roster(
        self,
        roster: Any,
        *,
        workspace: Path | None = None,
        allow_model_smoke: bool = True,
        require_hard_isolation: bool = False,
        sandbox: str | None = None,
    ) -> tuple[SeatHealthResult, ...]:
        """Probe every requested root and every declared fallback in parallel."""
        names = _seat_chain_names(roster)
        deadline = self._clock() + OVERALL_TIMEOUT_SECONDS
        results: dict[str, SeatHealthResult] = {}
        executor = ThreadPoolExecutor(max_workers=max(1, len(names)), thread_name_prefix="seat-health")
        try:
            futures = {
                executor.submit(
                    self.probe,
                    roster.agents[name],
                    roster,
                    workspace=workspace,
                    allow_model_smoke=allow_model_smoke,
                    require_hard_isolation=require_hard_isolation,
                    sandbox=sandbox,
                ): name
                for name in names
            }
            try:
                for future in as_completed(futures, timeout=max(deadline - self._clock(), 0.0)):
                    name = futures[future]
                    try:
                        results[name] = future.result()
                    except Exception as exc:  # adapter bugs must be receipted, not crash doctor
                        results[name] = _exception_result(name, roster, self._clock(), self._wall_clock(), exc)
            except FuturesTimeoutError:
                pass
            for future, name in futures.items():
                if name not in results:
                    future.cancel()
                    results[name] = _overall_timeout_result(name, roster, self._clock(), self._wall_clock())
        finally:
            # A hung provider must not turn the 35-second admission deadline
            # into an unbounded executor shutdown.  Running checks are bounded
            # independently and their results are discarded after this phase.
            executor.shutdown(wait=False, cancel_futures=True)
        return tuple(results[name] for name in names)

    def invalidate(
        self,
        *,
        fingerprint: str | None = None,
        seat: str | None = None,
    ) -> int:
        """Drop cached probe rows after fingerprint expiry, restart, or resume.

        Pass ``fingerprint`` to clear one exact entry, ``seat`` to clear every
        cached row for that seat name, or neither to clear the whole cache.
        Returns the number of entries removed.
        """
        with self._cache_lock:
            if fingerprint is not None:
                return 1 if self._cache.pop(fingerprint, None) is not None else 0
            if seat is None:
                count = len(self._cache)
                self._cache.clear()
                return count
            victims = [key for key, entry in self._cache.items() if entry.result.seat == seat]
            for key in victims:
                del self._cache[key]
            return len(victims)

    def _cached(self, fingerprint: str, now: float) -> SeatHealthResult | None:
        with self._cache_lock:
            entry = self._cache.get(fingerprint)
        if entry is None:
            return None
        ttl = HEALTHY_CACHE_TTL_SECONDS if entry.result.status == "healthy" else UNHEALTHY_CACHE_TTL_SECONDS
        return entry.result if now - entry.stored_at <= ttl else None

    def _executable_version(self, seat: Any) -> str | None:
        if not self._collect_executable_version or self._adapter is not None or getattr(seat, "cli", None) is None:
            return None
        identity = _resolve_agent_executable(seat)
        if not identity.runnable:
            return None
        try:
            result = proc.run([identity.command, "--version"], timeout=2.0)
        except OSError:
            return None
        match = re.search(r"\b\d+(?:\.\d+){1,3}\b", f"{result.stdout}\n{result.stderr}")
        return match.group(0) if result.code == 0 and match is not None else None

    def _run_check(
        self,
        name: CheckName,
        seat: Any,
        roster: Any,
        workspace: Path | None,
        timeout_seconds: float,
        allow_model_smoke: bool,
        *,
        require_hard_isolation: bool = False,
        sandbox: str | None = None,
    ) -> SeatHealthCheck:
        started = self._clock()
        try:
            if self._adapter is not None:
                check = self._adapter.check(
                    name, seat=seat, roster=roster, workspace=workspace, timeout_seconds=timeout_seconds
                )
            else:
                check = self._default_check(
                    name,
                    seat,
                    roster,
                    workspace,
                    timeout_seconds,
                    allow_model_smoke,
                    require_hard_isolation=require_hard_isolation,
                    sandbox=sandbox,
                )
        except TimeoutError:
            return _timeout_check(name, self._clock() - started)
        except Exception as exc:  # provider APIs are not allowed to escape the health boundary
            return SeatHealthCheck(name, "failed", safe_detail(str(exc)), self._clock() - started, "check-exception")
        return replace(check, duration_seconds=max(check.duration_seconds, self._clock() - started))

    def _default_check(
        self,
        name: CheckName,
        seat: Any,
        roster: Any,
        workspace: Path | None,
        timeout_seconds: float,
        allow_model_smoke: bool,
        *,
        require_hard_isolation: bool = False,
        sandbox: str | None = None,
    ) -> SeatHealthCheck:
        if name == "declaration":
            return SeatHealthCheck(name, "passed", "roster declaration validated")
        if name == "executable-identity":
            if seat.cli is None:
                return SeatHealthCheck(name, "degraded", "endpoint seat has no local executable")
            identity = _resolve_agent_executable(seat)
            if not identity.runnable:
                return SeatHealthCheck(name, "failed", identity.detail, cause_code="missing-executable")
            return SeatHealthCheck(name, "passed", f"{identity.command} ({identity.kind})")
        if name == "authentication-entitlement":
            if seat.transport == "acpx":
                auth = acpx_adapter.cursor_auth_status()
                if auth.state == "authenticated":
                    return SeatHealthCheck(name, "passed", auth.detail)
                if auth.state == "unauthenticated":
                    return SeatHealthCheck(name, "failed", auth.detail, cause_code="auth-required")
                return SeatHealthCheck(name, "degraded", auth.detail, cause_code="auth-status-unavailable")
            if seat.cli == "antigravity":
                return self._antigravity_auth_check(seat, timeout_seconds)
            return SeatHealthCheck(
                name,
                "degraded",
                "adapter has no prompt-free authentication status check",
                cause_code="probe-incomplete",
            )
        if name == "transport-liveness":
            return self._transport_check(seat, roster, workspace, timeout_seconds)
        if name == "version-gates":
            if seat.transport == "acpx":
                version, detail = acpx_adapter.installed_version()
                if version == seat.transport_version:
                    return SeatHealthCheck(name, "passed", f"acpx version {version}")
                return SeatHealthCheck(
                    name,
                    "failed",
                    f"requires {seat.transport_version}; found {version or detail}",
                    cause_code="version-mismatch",
                )
            if seat.cli == "antigravity":
                return self._antigravity_version_check(seat, timeout_seconds)
            return SeatHealthCheck(
                name, "degraded", "adapter has no reviewed version gate", cause_code="probe-incomplete"
            )
        if name == "model-reachability":
            return self._model_check(seat, roster, workspace, timeout_seconds, allow_model_smoke)
        if name == "isolation-compatibility":
            effective_sandbox = sandbox if sandbox is not None else roster.sandbox
            enforcement = (
                "endpoint"
                if seat.cli is None
                else agents.read_only_enforcement(seat.cli, sandbox=effective_sandbox, transport=seat.transport)
            )
            if enforcement == "hard":
                return SeatHealthCheck(name, "passed", "declared read-only enforcement is hard")
            if require_hard_isolation and enforcement in {"soft", "none"}:
                return SeatHealthCheck(
                    name,
                    "failed",
                    f"read-only run requires hard isolation; declared enforcement is {enforcement}",
                    cause_code="unsafe-isolation",
                )
            return SeatHealthCheck(
                name,
                "degraded",
                f"declared read-only enforcement is {enforcement}; postflight is deferred",
                cause_code="probe-incomplete",
            )
        raise AssertionError(name)

    def _transport_check(
        self, seat: Any, roster: Any, workspace: Path | None, timeout_seconds: float
    ) -> SeatHealthCheck:
        if seat.transport == "acpx":
            if proc.which("acpx") is None:
                return SeatHealthCheck(
                    "transport-liveness", "failed", "acpx is not installed", cause_code="missing-transport"
                )
            return SeatHealthCheck("transport-liveness", "passed", "acpx transport executable is available")
        if seat.cli == "codex" and roster.codex_transport == "app-server":
            # A start-thread request is still a provider transport smoke. It
            # must never point the app-server at the caller's worktree.
            root = _temporary_git_repo()
            try:
                with _app_server(root) as server:
                    server.start_thread(cwd=root, model=seat.model, sandbox=roster.sandbox)
                return SeatHealthCheck("transport-liveness", "passed", "app-server initialized and accepted a thread")
            except Exception as exc:  # AppServerError has no stable public subclasses
                detail = safe_detail(str(exc))
                code = "initialize-no-progress" if "timed out" in detail else "app-server-unavailable"
                return SeatHealthCheck("transport-liveness", "failed", detail, cause_code=code)
            finally:
                _remove_temp_repo(root)
        if seat.cli is None:
            return SeatHealthCheck(
                "transport-liveness",
                "degraded",
                "endpoint liveness requires a provider-safe status operation",
                cause_code="probe-incomplete",
            )
        identity = _resolve_agent_executable(seat)
        if not identity.runnable:
            return SeatHealthCheck("transport-liveness", "failed", identity.detail, cause_code="missing-executable")
        return SeatHealthCheck("transport-liveness", "passed", "direct executable is runnable")

    def _model_check(
        self, seat: Any, roster: Any, workspace: Path | None, timeout_seconds: float, allow_model_smoke: bool
    ) -> SeatHealthCheck:
        if isinstance(seat.cli, str) and seat.cli.startswith("codex-cloud:"):
            return SeatHealthCheck(
                "model-reachability",
                "degraded",
                "cloud seats validate via `brigade run cloud canary`; model smoke is not provider-safe",
            )
        if seat.model is None:
            return SeatHealthCheck("model-reachability", "degraded", "seat has no exact model declaration")
        if seat.cli == "antigravity":
            return self._antigravity_model_check(seat, timeout_seconds)
        # Roster doctor retains its established inventory rendering below.  It
        # asks the shared coordinator for all other facts without duplicating a
        # provider inventory call, and it deliberately never sends a smoke
        # request while merely inspecting a workspace.
        if not allow_model_smoke:
            return SeatHealthCheck(
                "model-reachability",
                "degraded",
                "model check is owned by roster doctor inventory",
                cause_code="probe-incomplete",
            )
        if seat.transport == "direct" and seat.cli is not None:
            inspected = model_inventory.ModelInventoryInspector().inspect(
                seat.cli, seat.model, getattr(seat, "command", None)
            )
            if inspected is not None:
                if inspected.state == "exact":
                    return SeatHealthCheck("model-reachability", "passed", inspected.detail)
                if inspected.state == "missing":
                    return SeatHealthCheck(
                        "model-reachability", "failed", inspected.detail, cause_code="model-unavailable"
                    )
                return SeatHealthCheck("model-reachability", "degraded", inspected.detail, cause_code=inspected.state)
        root = _temporary_git_repo()
        try:
            if self._model_smoke is not None:
                return self._model_smoke(seat, roster, root, timeout_seconds)
            if seat.cli is None:
                return SeatHealthCheck(
                    "model-reachability", "degraded", "endpoint has no provider-safe exact model smoke implementation"
                )
            if seat.transport == "acpx":
                result = acpx_adapter.run_cursor(
                    _MODEL_SMOKE_PROMPT,
                    cwd=root,
                    timeout=timeout_seconds,
                    model=seat.model,
                    version=seat.transport_version,
                    read_only=True,
                )
            elif seat.cli == "codex" and roster.codex_transport == "app-server":
                return self._app_server_model_smoke(seat, roster, root, timeout_seconds)
            else:
                result = agents.run_agent(
                    seat.cli,
                    _MODEL_SMOKE_PROMPT,
                    timeout=timeout_seconds,
                    cwd=root,
                    read_only=True,
                    sandbox="read-only",
                    model=seat.model,
                    reasoning=seat.reasoning,
                    env=seat.env,
                    command=getattr(seat, "command", None),
                )
            return _model_smoke_result(result.ok, result.text, result.detail, result.failure_kind, result.timed_out)
        finally:
            _remove_temp_repo(root)

    def _antigravity_auth_check(self, seat: Any, timeout_seconds: float) -> SeatHealthCheck:
        result, detail = _antigravity_models(seat, timeout_seconds)
        if result is None:
            return SeatHealthCheck("authentication-entitlement", "failed", detail, cause_code="models-command-failed")
        output = f"{result.stdout}\n{result.stderr}"
        if _antigravity_auth_error(output):
            return SeatHealthCheck(
                "authentication-entitlement",
                "failed",
                "agy models reported authentication is required",
                cause_code="auth-required",
            )
        if result.code != 0:
            return SeatHealthCheck(
                "authentication-entitlement",
                "failed",
                f"agy models exited {result.code}",
                cause_code="models-command-failed",
            )
        model_ids = _antigravity_model_ids(output)
        if not model_ids:
            return SeatHealthCheck(
                "authentication-entitlement",
                "failed",
                "agy models returned no model lines",
                cause_code="model-list-empty",
            )
        return SeatHealthCheck("authentication-entitlement", "passed", f"agy models listed {len(model_ids)} model(s)")

    def _antigravity_version_check(self, seat: Any, timeout_seconds: float) -> SeatHealthCheck:
        argv, env, env_error = _antigravity_probe_invocation(seat, "--version")
        if env_error:
            return SeatHealthCheck("version-gates", "failed", env_error, cause_code="version-unavailable")
        try:
            result = proc.run(argv, timeout=min(timeout_seconds, _ANTIGRAVITY_PROBE_TIMEOUT_SECONDS), env=env)
        except OSError as exc:
            return SeatHealthCheck("version-gates", "failed", safe_detail(str(exc)), cause_code="version-unavailable")
        output = f"{result.stdout}\n{result.stderr}"
        match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", output)
        if result.code != 0:
            return SeatHealthCheck(
                "version-gates", "failed", f"agy --version exited {result.code}", cause_code="version-unavailable"
            )
        if match is None:
            return SeatHealthCheck(
                "version-gates",
                "failed",
                "agy --version returned no semantic version",
                cause_code="version-unavailable",
            )
        version = tuple(int(part) for part in match.groups())
        if version < _ANTIGRAVITY_MIN_VERSION:
            required = ".".join(str(part) for part in _ANTIGRAVITY_MIN_VERSION)
            found = ".".join(str(part) for part in version)
            return SeatHealthCheck(
                "version-gates", "failed", f"requires agy {required}; found {found}", cause_code="version-mismatch"
            )
        return SeatHealthCheck("version-gates", "passed", f"agy version {'.'.join(match.groups())}")

    def _antigravity_model_check(self, seat: Any, timeout_seconds: float) -> SeatHealthCheck:
        result, detail = _antigravity_models(seat, timeout_seconds)
        if result is None:
            return SeatHealthCheck("model-reachability", "failed", detail, cause_code="models-command-failed")
        output = f"{result.stdout}\n{result.stderr}"
        if _antigravity_auth_error(output):
            return SeatHealthCheck(
                "model-reachability",
                "failed",
                "agy models reported authentication is required",
                cause_code="auth-required",
            )
        if result.code != 0:
            return SeatHealthCheck(
                "model-reachability", "failed", f"agy models exited {result.code}", cause_code="models-command-failed"
            )
        model = _canonical_antigravity_model_id(seat.model)
        if _model_id_is_listed(model, output):
            return SeatHealthCheck("model-reachability", "passed", f"agy models lists requested model {model}")
        return SeatHealthCheck(
            "model-reachability",
            "failed",
            f"requested model {model} is not listed by agy models",
            cause_code="model-not-listed",
        )

    def _app_server_model_smoke(self, seat: Any, roster: Any, root: Path, timeout_seconds: float) -> SeatHealthCheck:
        try:
            with _app_server(root) as server:
                thread = server.start_thread(cwd=root, model=seat.model, sandbox="read-only")
                result = thread.run_turn(_MODEL_SMOKE_PROMPT, timeout=timeout_seconds, effort=seat.reasoning)
        except Exception as exc:  # app-server owns its protocol detail
            return SeatHealthCheck(
                "model-reachability", "failed", safe_detail(str(exc)), cause_code="app-server-unavailable"
            )
        cause = "timeout" if result.timed_out else "model-unavailable"
        return _model_smoke_result(result.ok, result.text, result.detail, cause, result.timed_out)


def _antigravity_models(seat: Any, timeout_seconds: float) -> tuple[proc.Result | None, str]:
    argv, env, env_error = _antigravity_probe_invocation(seat, "models")
    if env_error:
        return None, env_error
    try:
        return (
            proc.run(argv, timeout=min(timeout_seconds, _ANTIGRAVITY_PROBE_TIMEOUT_SECONDS), env=env),
            "",
        )
    except OSError as exc:
        return None, safe_detail(str(exc))


def _antigravity_auth_error(output: str) -> bool:
    lowered = output.lower()
    return any(marker in lowered for marker in ("authentication required", "not authenticated", "not logged in"))


def _antigravity_model_ids(output: str) -> tuple[str, ...]:
    ignored = frozenset({"available", "model", "models", "no", "none"})
    model_ids: list[str] = []
    for line in output.splitlines():
        token = line.strip().split(maxsplit=1)[0] if line.strip() else ""
        if token.lower() not in ignored and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", token):
            model_ids.append(token)
    return tuple(model_ids)


def _model_id_is_listed(model: str, output: str) -> bool:
    token = re.compile(rf"(?<!\S){re.escape(model)}(?=\s|$|\()")
    return any(token.search(line) is not None for line in output.splitlines())


def _canonical_antigravity_model_id(model: str) -> str:
    normalized = re.sub(r"[()]+", "", model.strip().lower())
    normalized = re.sub(r"[\s_-]+", "-", normalized).strip("-")
    return agents._antigravity_model_pin(normalized)


def _antigravity_probe_invocation(seat: Any, subcommand: str) -> tuple[list[str], dict[str, str] | None, str]:
    seat_env = getattr(seat, "env", None)
    child_env: dict[str, str] | None = None
    if seat_env is not None:
        overrides, env_error = agents.resolve_env_overrides(seat_env)
        if overrides is None:
            return [], None, env_error
        child_env = dict(os.environ)
        child_env.update(overrides)
    identity = _resolve_agent_executable(seat)
    command = getattr(seat, "command", None) or ()
    return [identity.command, *command[1:], subcommand], child_env, ""


def seat_fingerprint(seat: Any, roster: Any, *, executable_version: str | None = None) -> str:
    """Hash only stable, redacted probe inputs.  Never include executable paths or secrets."""
    cli = getattr(seat, "cli", None)
    identity = _resolve_agent_executable(seat) if cli else None
    env = getattr(seat, "env", None) or {}
    env_presence = {str(key): bool(os.environ.get(_env_target(str(key)))) for key in env}
    endpoint = _redacted_endpoint(getattr(seat, "endpoint", None))
    data = {
        "executable": None
        if identity is None
        else {
            "command": identity.command,
            "kind": identity.kind,
            "runnable": identity.runnable,
            "version": executable_version,
        },
        "adapter": cli,
        "transport": _requested(seat, roster)["transport"],
        "transport_version": getattr(seat, "transport_version", None),
        "model": getattr(seat, "model", None),
        "reasoning": getattr(seat, "reasoning", None),
        "env_present": env_presence,
        "sandbox": getattr(roster, "sandbox", None),
        "workspace_mode": "detached" if getattr(seat, "transport", "direct") == "acpx" else "workspace",
        "endpoint": endpoint,
    }
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def write_seat_health_receipt(
    path: Path, results: tuple[SeatHealthResult, ...] | list[SeatHealthResult], *, run_id: str | None = None
) -> None:
    """Atomically write the additive seat-health receipt for a caller-owned run directory."""
    started = min((result.started_wall_at for result in results), default=time.time())
    finished = max((result.finished_wall_at for result in results), default=started)
    payload: dict[str, object] = {
        "schema": SCHEMA,
        "started_at": _timestamp(started),
        "finished_at": _timestamp(finished),
        "results": [result.payload() for result in results],
    }
    if run_id is not None:
        payload["run_id"] = run_id
    path = path.expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    ) as handle:
        temporary = Path(handle.name)
        handle.write(json.dumps(payload, sort_keys=True, indent=2) + "\n")
    os.replace(temporary, path)


def _requested(seat: Any, roster: Any) -> dict[str, str | None]:
    cli = getattr(seat, "cli", None)
    if cli is None:
        adapter = "endpoint"
        transport = "endpoint"
    elif cli == "codex":
        adapter = "codex"
        transport = getattr(roster, "codex_transport", "exec")
    else:
        adapter = cli
        transport = getattr(seat, "transport", "direct")
    return {
        "adapter": adapter,
        "transport": transport,
        "model": getattr(seat, "model", None),
        "reasoning": getattr(seat, "reasoning", None),
    }


def _adapter_kind(seat: Any, roster: Any) -> str:
    if getattr(seat, "cli", None) is None:
        return "endpoint"
    if seat.cli == "codex":
        return "codex-app-server" if roster.codex_transport == "app-server" else "codex-exec"
    return "cursor-acpx" if seat.transport == "acpx" else "direct"


def _result(
    probe_id: str,
    seat: str,
    fingerprint: str,
    requested: dict[str, str | None],
    checks: tuple[SeatHealthCheck, ...],
    started: float,
    finished: float,
    started_wall_at: float,
    finished_wall_at: float,
    *,
    cached: bool = False,
    cache_age_seconds: float | None = None,
) -> SeatHealthResult:
    failed = next((check for check in checks if check.status == "failed"), None)
    status: HealthStatus = (
        "unhealthy" if failed else ("degraded" if any(check.status == "degraded" for check in checks) else "healthy")
    )
    failure = _failure_for(failed) if failed is not None else None
    return SeatHealthResult(
        probe_id,
        seat,
        fingerprint,
        status,
        requested,
        checks,
        started,
        finished,
        started_wall_at,
        finished_wall_at,
        cached,
        cache_age_seconds,
        failure,
    )


def _failure_for(check: SeatHealthCheck) -> WorkerFailure:
    cause = check.cause_code or "health-check-failed"
    mapping = {
        "missing-executable": FailureClass.EXECUTABLE_UNAVAILABLE,
        "auth-required": FailureClass.AUTH_REQUIRED,
        "entitlement-denied": FailureClass.ENTITLEMENT_DENIED,
        "version-mismatch": FailureClass.VERSION_GATE,
        "model-unavailable": FailureClass.MODEL_UNAVAILABLE,
        "model-not-listed": FailureClass.MODEL_UNAVAILABLE,
        "missing-transport": FailureClass.TRANSPORT_UNAVAILABLE,
        "app-server-unavailable": FailureClass.TRANSPORT_UNAVAILABLE,
        "initialize-no-progress": FailureClass.TRANSPORT_HANG,
        "timeout": FailureClass.TIMEOUT,
        "unsafe-isolation": FailureClass.CONFIGURATION_INVALID,
    }
    failure_class = mapping.get(cause, FailureClass.UNCLASSIFIED)
    spec = FAILURE_CLASS_SPECS[failure_class]
    phase = FailurePhase.PREFLIGHT if FailurePhase.PREFLIGHT in spec.phases else spec.default_phase
    return WorkerFailure(failure_class, phase, spec.default_retry, f"seat-health:{check.name}", check.detail, cause)


def _model_smoke_result(ok: bool, text: str, detail: str, failure_kind: str | None, timed_out: bool) -> SeatHealthCheck:
    if ok and text.strip() == _MODEL_SMOKE_MARKER:
        return SeatHealthCheck("model-reachability", "passed", "exact marker returned")
    cause = "timeout" if timed_out else _smoke_cause(failure_kind)
    safe = safe_detail(detail or "exact marker was not returned")
    return SeatHealthCheck("model-reachability", "failed", safe, cause_code=cause)


def _smoke_cause(failure_kind: str | None) -> str:
    known = {
        "authentication-error": "auth-required",
        "command-not-found": "missing-executable",
        "provider-auth": "auth-required",
        "provider-setting-error": "model-unavailable",
        "provider-startup": "app-server-unavailable",
        "timeout": "timeout",
        "version-mismatch": "version-mismatch",
    }
    return known.get(failure_kind or "", "model-unavailable")


def _timeout_check(name: CheckName, duration: float = 0.0) -> SeatHealthCheck:
    return SeatHealthCheck(name, "failed", "health check timed out", duration, "timeout")


def _seat_chain_names(roster: Any) -> tuple[str, ...]:
    referenced = {fallback for seat in roster.agents.values() for fallback in seat.fallback}
    roots = [name for name in roster.agents if name == roster.orchestrator or name not in referenced]
    names: list[str] = []
    for root in roots:
        if root not in names:
            names.append(root)
        for fallback in roster.agents[root].fallback:
            # Fleet model admission can remove a denied fallback while keeping
            # its enabled root. Health probes must use the effective roster,
            # not reintroduce an unavailable seat from its declaration.
            if fallback in roster.agents and fallback not in names:
                names.append(fallback)
    return tuple(names)


def exception_results_for_probe_failure(roster: Any, exc: Exception) -> tuple[SeatHealthResult, ...]:
    """Return one failed result per declared seat when the probe itself blew up.

    ``probe_roster`` normally covers the whole seat chain, so a caller that recorded
    only the orchestrator would leave every other seat with no row at all. A reader
    cannot tell a missing row from a healthy one, so cover the same chain here and
    mark each seat failed with the probe's own cause.
    """
    now = time.monotonic()
    wall_now = time.time()
    return tuple(_exception_result(name, roster, now, wall_now, exc) for name in _seat_chain_names(roster))


def _exception_result(name: str, roster: Any, now: float, wall_now: float, exc: Exception) -> SeatHealthResult:
    seat = roster.agents[name]
    check = SeatHealthCheck("declaration", "failed", safe_detail(str(exc)), cause_code="probe-exception")
    return _result(
        _probe_id(name, now),
        name,
        seat_fingerprint(seat, roster),
        _requested(seat, roster),
        (check,),
        now,
        now,
        wall_now,
        wall_now,
    )


def _overall_timeout_result(name: str, roster: Any, now: float, wall_now: float) -> SeatHealthResult:
    seat = roster.agents[name]
    return _result(
        _probe_id(name, now),
        name,
        seat_fingerprint(seat, roster),
        _requested(seat, roster),
        (_timeout_check("declaration"),),
        now,
        now,
        wall_now,
        wall_now,
    )


def _probe_id(name: str, started: float) -> str:
    digest = hashlib.sha256(f"{name}:{started:.9f}".encode()).hexdigest()[:10]
    return f"probe-{digest}"


def _timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _env_target(key: str) -> str:
    return key[:-4] if key.endswith("_REF") else key


def _redacted_endpoint(value: str | None) -> str | None:
    if not value:
        return None
    parsed = urlsplit(value)
    return parsed.hostname or "endpoint"


def _temporary_git_repo() -> Path:
    root = Path(tempfile.mkdtemp(prefix="brigade-seat-health-"))
    subprocess.run(["git", "init", "--quiet", str(root)], check=True, capture_output=True, text=True)
    return root


def _remove_temp_repo(root: Path) -> None:
    # The directory contains only probe-created files.  Avoid a broad recursive shell command.
    for child in sorted(root.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink(missing_ok=True)
        elif child.is_dir():
            child.rmdir()
    root.rmdir()


def _app_server(root: Path):
    from .codex_appserver import AppServer

    return AppServer(cwd=root)
