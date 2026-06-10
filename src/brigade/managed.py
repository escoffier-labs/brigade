"""Managed tools: external CLIs Brigade can install, wire, and health-check.

Each tool attaches to a station. The core never imports these tools; it shells
out via brigade.proc. Absent tools are reported as MANUAL (a hint to install),
never as a hard failure.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

from . import proc
from .doctor import OK, WARN, FAIL, MANUAL
from .station import CheckResult, DoctorContext


@dataclass(frozen=True)
class ManagedTool:
    name: str  # e.g. "memory-doctor"
    station: str  # "memory" | "guard" | "tokens"
    command: str  # the binary name to detect on PATH
    summary: str
    install_args: List[str]  # argv to install (pipx/npm/pip)
    wire: Callable[[DoctorContext], List[CheckResult]]  # lay config; returns notes
    doctor: Callable[[DoctorContext], List[CheckResult]]  # health via proc

    def detect(self) -> bool:
        return proc.which(self.command) is not None


# ---- adapters -------------------------------------------------------------


def _noop_wire(ctx: DoctorContext) -> List[CheckResult]:
    return []


# memory-doctor and bootstrap-doctor inspect the operator's canonical memory and
# bootstrap files (host-global), not a per-target workspace, so their findings are
# advisory: labeled operator-scoped and never FAIL a workspace doctor run.
def _memory_doctor_doctor(ctx: DoctorContext) -> List[CheckResult]:
    name = "memory-doctor (operator memory)"
    r = proc.run(["memory-doctor", "status", "--json"])
    if r.code == 2:
        return [(WARN, name, "installed but unwired (memory/handoffs dir missing)")]
    data = r.json()
    if data is None:
        return [(WARN, name, f"unexpected output (exit {r.code})")]
    dead = data.get("dead_links", 0)
    status = WARN if dead else OK
    return [(status, name, f"cards={data.get('cards')}, dead_links={dead}, pending={data.get('pending_handoffs')}")]


def _bootstrap_doctor_doctor(ctx: DoctorContext) -> List[CheckResult]:
    name = "bootstrap-doctor (operator files)"
    r = proc.run(["bootstrap-doctor", "status", "--json"])
    data = r.json()
    if data is None:
        return [(WARN, name, f"installed but unwired or errored (exit {r.code})")]
    rows = data.get("rows", [])
    bad = [row for row in rows if row.get("severity") in ("hard", "missing", "unreadable")]
    soft = [row for row in rows if row.get("severity") == "soft"]
    if bad:
        return [(WARN, name, f"{len(bad)} file(s) over hard limit / missing (advisory)")]
    if soft:
        return [(WARN, name, f"{len(soft)} file(s) in soft band")]
    return [(OK, name, f"{len(rows)} bootstrap file(s) within limits")]


def _content_guard_doctor(ctx: DoctorContext) -> List[CheckResult]:
    # A "tool present + policy loads" check: scan this plan's own clean string.
    r = proc.run(["content-guard", "scan", "--policy", "public-repo", "--json"], env=None)
    data = r.json()
    if data is None and r.code not in (0, 1):
        return [(WARN, "content-guard", f"installed but not runnable (exit {r.code})")]
    return [(OK, "content-guard", "installed; public-repo policy loads")]


def _content_guard_wire(ctx: DoctorContext) -> List[CheckResult]:
    # content-guard ships bundled policies; nothing to lay down for the default.
    return [(OK, "content-guard: policy", "using bundled public-repo policy")]


def _tokenjuice_doctor(ctx: DoctorContext) -> List[CheckResult]:
    r = proc.run(["tokenjuice", "doctor", "hooks", "--format", "json"])
    data = r.json()
    if data is None:
        return [(WARN, "tokenjuice", f"installed but doctor output unreadable (exit {r.code})")]
    status = data.get("status", "unknown")
    mapping = {"ok": OK, "warn": WARN, "disabled": MANUAL, "broken": FAIL}
    return [(mapping.get(status, WARN), "tokenjuice", f"hook status: {status}")]


def _code_search_url() -> str:
    return os.environ.get("CODE_SEARCH_API_URL", "http://localhost:5204").rstrip("/")


def _code_search_api_doctor(ctx: DoctorContext) -> List[CheckResult]:
    name = "code-search-api (local search service)"
    url = f"{_code_search_url()}/api/health"
    request = urllib.request.Request(url)
    api_key = os.environ.get("CODE_SEARCH_API_KEY")
    if api_key:
        request.add_header("X-API-Key", api_key)
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except (OSError, urllib.error.URLError) as exc:
        return [(WARN, name, f"installed; service health unavailable at {url} ({exc})")]
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return [(WARN, name, "installed; service returned non-JSON health")]
    if not isinstance(data, dict):
        return [(WARN, name, "installed; service returned unexpected health payload")]
    status = OK if data.get("status") == "ok" else WARN
    chunks = data.get("chunks", 0)
    embedded = data.get("embedded", 0)
    summarized = data.get("summarized", 0)
    version = data.get("version") or "?"
    return [(status, name, f"version={version}, chunks={chunks}, embedded={embedded}, summarized={summarized}")]


def _code_search_mcp_doctor(ctx: DoctorContext) -> List[CheckResult]:
    return [
        (
            OK,
            "code-search-mcp (MCP bridge)",
            "installed; configure MCP clients with CODE_SEARCH_API_URL and optional CODE_SEARCH_API_KEY",
        )
    ]


# agentpantry keeps the agent's machine authenticated by syncing browser sessions
# from a daily-driver (source) to the agent host (sink). Like the memory satellites
# it inspects host-global state, so its findings are advisory and never FAIL a
# workspace doctor run.
def _agentpantry_doctor(ctx: DoctorContext) -> List[CheckResult]:
    name = "agentpantry (session auth sync)"
    r = proc.run(["agentpantry", "doctor", "--json"])
    data = r.json()
    if data is not None:
        if isinstance(data, dict) and data.get("configured") is False:
            return [(WARN, name, "installed but unwired (no config)")]
        if not isinstance(data, dict):
            return [(WARN, name, f"unexpected doctor output (exit {r.code})")]
        role = data.get("role") or "?"
        peer = data.get("peer") or "?"
        surfaces = data.get("surfaces") or []
        fail_count = int(data.get("fail_count") or 0)
        warn_count = int(data.get("warn_count") or 0)
        status = WARN if fail_count or warn_count else OK
        parts = [
            f"role={role}",
            f"peer={peer}",
            f"surfaces={','.join(str(s) for s in surfaces) or 'none'}",
            f"checks={fail_count} fail/{warn_count} warn",
        ]
        checks = data.get("checks")
        if isinstance(checks, list):
            top = next((row for row in checks if isinstance(row, dict) and row.get("status") == "FAIL"), None)
            if top is None:
                top = next((row for row in checks if isinstance(row, dict) and row.get("status") == "WARN"), None)
            if top is not None:
                parts.append(f"top={top.get('name')}: {str(top.get('detail') or '')[:80]}")
        return [(status, name, ", ".join(parts))]

    # Older agentpantry builds only expose `status --json`; keep the original
    # shallow advisory path so Brigade remains compatible with those installs.
    r = proc.run(["agentpantry", "status", "--json"])
    if r.code == 2:
        return [(WARN, name, "installed but unwired (no config)")]
    data = r.json()
    if data is None:
        return [(WARN, name, f"unexpected output (exit {r.code})")]
    role = data.get("role") or "?"
    peer = data.get("peer") or "?"
    surfaces = data.get("surfaces") or []
    key_present = bool(data.get("key_present"))
    status = OK if key_present else WARN
    detail = (
        f"role={role}, peer={peer}, "
        f"surfaces={','.join(str(s) for s in surfaces) or 'none'}, "
        f"key={'present' if key_present else 'MISSING'}"
    )
    return [(status, name, detail)]


def _agent_notify_doctor(ctx: DoctorContext) -> List[CheckResult]:
    from . import notifications_cmd

    name = "agent-notify (operator notifications)"
    health = notifications_cmd.health(ctx.target)
    channels = health.get("selected_channels") if isinstance(health.get("selected_channels"), list) else []
    profile = health.get("profile") or "none"
    if not health.get("configured"):
        top = health.get("top_issue") if isinstance(health.get("top_issue"), dict) else {}
        detail = top.get("detail") or "installed but unwired (no configured notification channels)"
        return [(WARN, name, str(detail))]
    status = OK if health.get("status") == "ok" else WARN
    return [(status, name, f"profile={profile}, channels={len(channels)}, sends=false")]


def _agent_notify_wire(ctx: DoctorContext) -> List[CheckResult]:
    return [
        (
            MANUAL,
            "agent-notify: wire",
            "write ~/.config/agent-notify/config.toml with env-var names, set channel env vars, then `brigade notifications setup plan`",
        )
    ]


# The miseledger family (miseledger + its stationtrail/sourceharvest exporters)
# operates on the operator's host-global evidence archive and local session logs,
# not a per-target workspace. Like the memory and pantry satellites, their findings
# are advisory: they never FAIL a workspace doctor run.
def _miseledger_doctor(ctx: DoctorContext) -> List[CheckResult]:
    name = "miseledger (evidence archive)"
    # `status --json` opens (and migrates) the local archive and reports counts.
    r = proc.run(["miseledger", "status", "--json"])
    data = r.json()
    if data is None:
        return [(WARN, name, f"installed but unwired or errored (exit {r.code})")]
    if not isinstance(data, dict):
        return [(WARN, name, f"unexpected status output (exit {r.code})")]
    items = data.get("items", 0)
    sources = data.get("sources", 0)
    schema = data.get("schema_version", "?")
    fts = data.get("fts") or "?"
    status = WARN if fts != "ok" else OK
    return [(status, name, f"schema={schema}, items={items}, sources={sources}, fts={fts}")]


def _stationtrail_doctor(ctx: DoctorContext) -> List[CheckResult]:
    name = "stationtrail (session exporter)"
    # `doctor --json` discovers local harness session roots; ok=false means a
    # source could not be read, which is advisory (operator local state).
    r = proc.run(["stationtrail", "doctor", "--json"])
    data = r.json()
    if data is None:
        return [(WARN, name, f"installed but doctor output unreadable (exit {r.code})")]
    if not isinstance(data, dict):
        return [(WARN, name, f"unexpected doctor output (exit {r.code})")]
    sources = data.get("sources") if isinstance(data.get("sources"), list) else []
    ready = [s for s in sources if isinstance(s, dict) and s.get("status") == "ready"]
    warnings = data.get("warnings") if isinstance(data.get("warnings"), list) else []
    status = WARN if (data.get("ok") is False or warnings) else OK
    return [(status, name, f"sources={len(sources)}, ready={len(ready)}, warnings={len(warnings)}")]


def _sourceharvest_doctor(ctx: DoctorContext) -> List[CheckResult]:
    name = "sourceharvest (source exporter)"
    # sourceharvest is a stateless adapter emitter with no archive to inspect;
    # presence + a runnable `version` is the most we can advisively assert.
    r = proc.run(["sourceharvest", "version"])
    if r.code != 0:
        return [(WARN, name, f"installed but not runnable (exit {r.code})")]
    return [(OK, name, f"installed; {r.stdout.strip() or 'version ok'}")]


def _tokenjuice_wire(ctx: DoctorContext) -> List[CheckResult]:
    # Wiring installs a host hook; which host depends on the workspace's harnesses.
    hosts = [h for h in ctx.harnesses if h in ("claude", "codex", "cursor")]
    if not hosts:
        return [(MANUAL, "tokenjuice: wire", "no hookable harness selected; run `tokenjuice install <host>` manually")]
    notes: List[CheckResult] = []
    for h in hosts:
        host = "claude-code" if h == "claude" else h
        r = proc.run(["tokenjuice", "install", host])
        notes.append((OK if r.code == 0 else WARN, f"tokenjuice: install {host}", r.stderr.strip()[:80] or "installed"))
    return notes


# ---- registry -------------------------------------------------------------

_TOOLS: Tuple[ManagedTool, ...] = (
    ManagedTool(
        name="memory-doctor",
        station="memory",
        command="memory-doctor",
        summary="memory index health, dead-link lint, handoff counts",
        install_args=["pipx", "install", "git+https://github.com/escoffier-labs/memory-doctor"],
        wire=_noop_wire,
        doctor=_memory_doctor_doctor,
    ),
    ManagedTool(
        name="bootstrap-doctor",
        station="memory",
        command="bootstrap-doctor",
        summary="bootstrap-file size/limit audit",
        install_args=["pipx", "install", "git+https://github.com/escoffier-labs/bootstrap-doctor"],
        wire=_noop_wire,
        doctor=_bootstrap_doctor_doctor,
    ),
    ManagedTool(
        name="content-guard",
        station="guard",
        command="content-guard",
        summary="policy-driven content scanning",
        install_args=["pipx", "install", "git+https://github.com/escoffier-labs/content-guard"],
        wire=_content_guard_wire,
        doctor=_content_guard_doctor,
    ),
    ManagedTool(
        name="tokenjuice",
        station="tokens",
        command="tokenjuice",
        summary="output compaction via host hooks",
        install_args=["npm", "install", "-g", "tokenjuice"],
        wire=_tokenjuice_wire,
        doctor=_tokenjuice_doctor,
    ),
    ManagedTool(
        name="code-search-api",
        station="search",
        command="code-search-api",
        summary="local semantic code search service with SQLite and Ollama embeddings",
        install_args=["pipx", "install", "git+https://github.com/escoffier-labs/code-search-api"],
        wire=_noop_wire,
        doctor=_code_search_api_doctor,
    ),
    ManagedTool(
        name="code-search-mcp",
        station="search",
        command="code-search-mcp",
        summary="read-only MCP bridge for a running code-search-api service",
        install_args=["npm", "install", "-g", "@solomonneas/code-search-mcp"],
        wire=_noop_wire,
        doctor=_code_search_mcp_doctor,
    ),
    ManagedTool(
        name="agentpantry",
        station="pantry",
        command="agentpantry",
        summary="browser session auth sync (source -> sink)",
        install_args=["go", "install", "github.com/escoffier-labs/agentpantry/cmd/agentpantry@latest"],
        wire=_noop_wire,
        doctor=_agentpantry_doctor,
    ),
    ManagedTool(
        name="agent-notify",
        station="notifications",
        command="agent-notify",
        summary="private operator notifications for agent events",
        install_args=["go", "install", "github.com/escoffier-labs/agent-notify/cmd/agent-notify@latest"],
        wire=_agent_notify_wire,
        doctor=_agent_notify_doctor,
    ),
    ManagedTool(
        name="miseledger",
        station="evidence",
        command="miseledger",
        summary="local-first evidence ledger: imports adapter JSONL, FTS search, evidence bundles",
        install_args=["go", "install", "github.com/escoffier-labs/miseledger/cmd/miseledger@latest"],
        wire=_noop_wire,
        doctor=_miseledger_doctor,
    ),
    ManagedTool(
        name="stationtrail",
        station="evidence",
        command="stationtrail",
        summary="agent-session log exporter to miseledger.adapter.v1 JSONL",
        install_args=["go", "install", "github.com/escoffier-labs/stationtrail/cmd/stationtrail@latest"],
        wire=_noop_wire,
        doctor=_stationtrail_doctor,
    ),
    ManagedTool(
        name="sourceharvest",
        station="evidence",
        command="sourceharvest",
        summary="source-system record exporter to miseledger.adapter.v1 JSONL",
        install_args=["go", "install", "github.com/escoffier-labs/sourceharvest/cmd/sourceharvest@latest"],
        wire=_noop_wire,
        doctor=_sourceharvest_doctor,
    ),
)


def all_tools() -> Tuple[ManagedTool, ...]:
    return _TOOLS


def for_station(station: str) -> Tuple[ManagedTool, ...]:
    return tuple(t for t in _TOOLS if t.station == station)


def resolve(name: str) -> Optional[ManagedTool]:
    for t in _TOOLS:
        if t.name == name:
            return t
    return None
