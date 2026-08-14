"""Lane-specific research doctor with redacted health projections."""

from __future__ import annotations

import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path

from .. import agents
from .. import proc
from .. import roster as roster_mod
from ..roster import Agent, Roster
from . import config as rconfig
from .llm import NoResearcherError, resolve_lane

SCHEMA = "brigade.research.doctor.v1"
SCHEMA_VERSION = 1
_VERSION_PROBE_TIMEOUT = 2.0
_VERSION_PROBE_MAX_LEN = 120

FIXED_CAPABILITIES: tuple[str, ...] = (
    "research.plan",
    "research.extract",
    "research.synthesize",
    "research.review",
    "research.browser-discover",
)

REQUIRED_CAPABILITIES: frozenset[str] = frozenset(
    {
        "research.plan",
        "research.extract",
        "research.synthesize",
        "research.review",
    }
)

_CAPABILITY_PROFILE_FIELD: dict[str, str] = {
    "research.plan": "planner",
    "research.extract": "extractor",
    "research.synthesize": "synthesizer",
    "research.review": "reviewer",
}

_HOME_PATH_RE = re.compile(r"/(?:home|Users)/[^\s\"']+")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_COOKIE_ASSIGN_RE = re.compile(r"(?i)\bcookie\s*[=:]\s*[^\s,;]+")
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(profile|cookie|token|authorization|password|api[_-]?key|secret|header|env)\s*[=:]\s*[^\s,;]+"
)
_SECRET_KEY_RE = re.compile(
    r"(?i)(secret|header|env|cookie|token|authorization|password|api[_-]?key|browser[_-]?profile)"
)
_SECRETISH_RE = re.compile(r"(?i)\b[\w-]*(?:cookie|secret|token)[\w-]*\b")


def _redact_text(value: object) -> str:
    text = str(value or "")
    text = _HOME_PATH_RE.sub("[redacted]", text)
    text = _BEARER_RE.sub("[redacted]", text)
    text = _COOKIE_ASSIGN_RE.sub("cookie=[redacted]", text)
    text = _ASSIGNMENT_RE.sub(lambda m: f"{m.group(1)}=[redacted]", text)
    text = _SECRETISH_RE.sub("[redacted]", text)
    return text


def _is_secret_key(key: object) -> bool:
    return bool(_SECRET_KEY_RE.search(str(key)))


def _redact_value(value: object) -> object:
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for key, item in value.items():
            key_s = str(key)
            if _is_secret_key(key_s):
                out[key_s] = "[redacted]"
            else:
                out[key_s] = _redact_value(item)
        return out
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _first_concise_line(text: str) -> str | None:
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if len(line) > _VERSION_PROBE_MAX_LEN:
            line = line[:_VERSION_PROBE_MAX_LEN].rstrip()
        return line or None
    return None


def _probe_executable_version(executable: str) -> str | None:
    """Bounded read-only ``--version`` probe. Never raises; never hangs."""
    try:
        result = proc.run([executable, "--version"], timeout=_VERSION_PROBE_TIMEOUT)
    except Exception:
        return None
    if result.code != 0:
        return None
    raw = _first_concise_line(result.stdout) or _first_concise_line(result.stderr)
    if raw is None:
        return None
    redacted = _redact_text(raw).strip()
    return redacted or None


def probe_agent(agent: Agent) -> dict[str, object]:
    """Read-only executable and adapter health probe for one seat."""
    cli_ref = agent.cli or ""
    command = agents.command_for(cli_ref) if cli_ref else ""
    executable = shutil.which(command) if command else None
    version: str | None = None
    auth_status = "unknown"
    detail = ""
    failure_kind: str | None = None

    if not cli_ref:
        auth_status = "unconfigured"
        detail = "seat has no cli"
    elif not executable:
        auth_status = "missing"
        detail = f"{cli_ref} executable not found on PATH"
    else:
        version = _probe_executable_version(executable)
        capability = roster_mod.HostCapabilityProbe().lookup(cli_ref)
        detail = capability.detail or capability.auth_detail or f"{cli_ref} via {command}"
        if capability.authenticated is True:
            auth_status = "authenticated"
        elif capability.authenticated is False:
            auth_status = "unauthenticated"
            if cli_ref == "oracle":
                failure_kind = "browser-auth"
        else:
            auth_status = "installed"

    payload: dict[str, object] = {
        "auth_status": auth_status,
        "detail": detail,
        "version": version,
        "executable": executable,
    }
    if failure_kind is not None:
        payload["failure_kind"] = failure_kind
    return payload


def _lane_concurrency(capability: str, agent: Agent | None) -> int | None:
    if capability == "research.browser-discover":
        return 1
    if agent is not None and agent.cli == "oracle":
        return 1
    return None


def _probe_healthy(probe_result: dict[str, object], *, executable: str | None) -> bool:
    auth_status = str(probe_result.get("auth_status") or "unknown")
    failure_kind = probe_result.get("failure_kind")
    if failure_kind == "browser-auth":
        return False
    if auth_status in {"unauthenticated", "missing", "unconfigured"}:
        return False
    if not executable:
        return False
    return True


def _normalize_probe(raw: object) -> dict[str, object]:
    redacted = _redact_value(raw) if isinstance(raw, dict) else {}
    if not isinstance(redacted, dict):
        return {}
    return {str(k): v for k, v in redacted.items()}


def _detail_text(value: object, *, configured: bool, capability: str) -> str:
    if isinstance(value, (dict, list, tuple)):
        return _redact_text(json.dumps(value, sort_keys=True, default=str))
    if value is None or value == "":
        return "" if configured else f"no seat for {capability}"
    return _redact_text(value)


def _build_lane(
    *,
    capability: str,
    agent: Agent | None,
    probe_result: dict[str, object],
    status: str,
    detail: str,
    failure_kind: str | None = None,
    fallback_seat: str | None = None,
    fallback_status: str | None = None,
) -> dict[str, object]:
    cli_ref = agent.cli if agent is not None else None
    command = agents.command_for(cli_ref) if cli_ref else None
    executable = probe_result.get("executable")
    if executable is None and command:
        found = shutil.which(command)
        executable = _redact_text(found) if found else None
    elif isinstance(executable, str):
        executable = _redact_text(executable)
    else:
        executable = None

    auth_status = str(probe_result.get("auth_status") or ("unconfigured" if agent is None else "unknown"))
    if failure_kind is None and agent is not None and agent.cli == "oracle" and auth_status == "unauthenticated":
        failure_kind = "browser-auth"
    if isinstance(failure_kind, str):
        failure_kind = _redact_text(failure_kind)
    else:
        failure_kind = None

    lane: dict[str, object] = {
        "capability": capability,
        "configured": agent is not None,
        "seat": agent.name if agent is not None else None,
        "cli": cli_ref,
        "executable": executable if isinstance(executable, str) else None,
        "version": probe_result.get("version"),
        "auth_status": auth_status,
        "requested_model": agent.model if agent is not None else None,
        "model_attestation": "unverified",
        "read_only": True if agent is None else bool(agent.read_only_capable),
        "timeout_seconds": agent.timeout_seconds if agent is not None else None,
        "concurrency": _lane_concurrency(capability, agent),
        "last_live_smoke": None,
        "status": status,
        "detail": detail,
    }
    if failure_kind:
        lane["failure_kind"] = failure_kind
    if fallback_seat is not None:
        lane["fallback_seat"] = fallback_seat
    if fallback_status is not None:
        lane["fallback_status"] = fallback_status
    return lane


def doctor_payload(
    target: Path,
    *,
    roster: Roster | None = None,
    profile_name: str | None = None,
    probe: Callable[[Agent], dict[str, object]] = probe_agent,
) -> dict[str, object]:
    target = target.expanduser().resolve()
    if roster is None:
        roster = roster_mod.load_roster(roster_mod.resolve_roster_path(target))
    cfg = rconfig.load(target)
    profile = cfg.profile(profile_name)

    lanes: list[dict[str, object]] = []
    required_failures = False
    optional_warnings = False
    probe_cache: dict[str, dict[str, object]] = {}

    def cached_probe(agent: Agent) -> dict[str, object]:
        cached = probe_cache.get(agent.name)
        if cached is not None:
            return cached
        normalized = _normalize_probe(probe(agent))
        probe_cache[agent.name] = normalized
        return normalized

    browser_required = bool(profile.browser_ai_research) or "browser-ai" in profile.discovery

    for capability in FIXED_CAPABILITIES:
        if capability == "research.browser-discover":
            agents_for_cap = roster.find_capability(capability)
            agent = agents_for_cap[0] if agents_for_cap else None
            if agent is None:
                status = "fail" if browser_required else "warn"
                detail = f"no seat for {capability}"
                lane = _build_lane(
                    capability=capability,
                    agent=None,
                    probe_result={},
                    status=status,
                    detail=detail,
                )
                if status == "fail":
                    required_failures = True
                else:
                    optional_warnings = True
                lanes.append(lane)
                continue

            probe_result = cached_probe(agent)
            executable = probe_result.get("executable")
            if not isinstance(executable, str):
                command = agents.command_for(agent.cli) if agent.cli else None
                found = shutil.which(command) if command else None
                executable = _redact_text(found) if found else None
                if executable:
                    probe_result = {**probe_result, "executable": executable}
            healthy = _probe_healthy(
                probe_result,
                executable=executable if isinstance(executable, str) else None,
            )
            failure_kind = probe_result.get("failure_kind")
            if not isinstance(failure_kind, str):
                failure_kind = None
            if healthy:
                status = "ok"
            else:
                status = "fail" if browser_required else "warn"
            if status == "fail":
                required_failures = True
            elif status == "warn":
                optional_warnings = True
            lanes.append(
                _build_lane(
                    capability=capability,
                    agent=agent,
                    probe_result=probe_result,
                    status=status,
                    detail=_detail_text(
                        probe_result.get("detail"),
                        configured=True,
                        capability=capability,
                    ),
                    failure_kind=failure_kind,
                )
            )
            continue

        field = _CAPABILITY_PROFILE_FIELD[capability]
        candidates = getattr(profile, field)
        try:
            resolved = resolve_lane(roster, phase=capability, candidates=candidates)
        except NoResearcherError:
            required_failures = True
            lanes.append(
                _build_lane(
                    capability=capability,
                    agent=None,
                    probe_result={},
                    status="fail",
                    detail=f"no seat for {capability}",
                )
            )
            continue

        primary = roster.agents[resolved.primary]
        primary_probe = cached_probe(primary)
        primary_executable = primary_probe.get("executable")
        if not isinstance(primary_executable, str):
            command = agents.command_for(primary.cli) if primary.cli else None
            found = shutil.which(command) if command else None
            primary_executable = _redact_text(found) if found else None
            if primary_executable:
                primary_probe = {**primary_probe, "executable": primary_executable}
        primary_healthy = _probe_healthy(
            primary_probe,
            executable=primary_executable if isinstance(primary_executable, str) else None,
        )

        fallback_seat: str | None = None
        fallback_status: str | None = None
        allowed_fallback_healthy = False
        allow_fallback = True
        if capability == "research.synthesize":
            allow_fallback = bool(profile.allow_synthesis_fallback)

        if allow_fallback:
            for name in resolved.fallbacks:
                fallback_agent = roster.agents[name]
                fallback_probe = cached_probe(fallback_agent)
                fallback_executable = fallback_probe.get("executable")
                if not isinstance(fallback_executable, str):
                    command = agents.command_for(fallback_agent.cli) if fallback_agent.cli else None
                    found = shutil.which(command) if command else None
                    fallback_executable = _redact_text(found) if found else None
                    if fallback_executable:
                        fallback_probe = {**fallback_probe, "executable": fallback_executable}
                healthy = _probe_healthy(
                    fallback_probe,
                    executable=fallback_executable if isinstance(fallback_executable, str) else None,
                )
                if fallback_seat is None:
                    fallback_seat = name
                    fallback_status = "ok" if healthy else "fail"
                if healthy:
                    allowed_fallback_healthy = True
                    fallback_seat = name
                    fallback_status = "ok"
                    break

        if primary_healthy:
            # Optional configured fallback still counts: unhealthy fallback warns
            # even when the required primary is healthy. No fallback declared on the
            # resolved lane stays ok.
            if allow_fallback and fallback_seat is not None and fallback_status != "ok":
                status = "warn"
                optional_warnings = True
            else:
                status = "ok"
        elif allowed_fallback_healthy:
            status = "warn"
            optional_warnings = True
        else:
            status = "fail"
            required_failures = True

        failure_kind = primary_probe.get("failure_kind")
        if not isinstance(failure_kind, str):
            failure_kind = None

        lanes.append(
            _build_lane(
                capability=capability,
                agent=primary,
                probe_result=primary_probe,
                status=status,
                detail=_detail_text(
                    primary_probe.get("detail"),
                    configured=True,
                    capability=capability,
                ),
                failure_kind=failure_kind,
                fallback_seat=fallback_seat,
                fallback_status=fallback_status,
            )
        )

    if not profile.discovery:
        required_failures = True

    if required_failures:
        overall = "fail"
    elif optional_warnings:
        overall = "warn"
    else:
        overall = "ok"

    return {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "status": overall,
        "profile": profile.name,
        "lanes": lanes,
    }
