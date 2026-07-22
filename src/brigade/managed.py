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
from dataclasses import dataclass, replace
from typing import Any, Callable, List, Optional, Tuple

from . import component_bins, managed_snapshot, proc
from .doctor import OK, WARN, FAIL, MANUAL
from .station import CheckResult, DoctorContext


@dataclass(frozen=True)
class MachineSurface:
    kind: str  # doctor-json | brief-markdown | summary-json | verify-exit
    command: Tuple[str, ...]
    read_only: bool = True
    timeout_seconds: Optional[float] = None
    max_chars: Optional[int] = None
    probe: Tuple[str, ...] = ()
    probe_contains: Tuple[str, ...] = ()


@dataclass(frozen=True)
class ManagedTool:
    name: str  # e.g. "memory-doctor"
    station: str  # "memory" | "guard" | "tokens"
    command: str  # the binary name to detect on PATH
    summary: str
    install_args: List[str]  # argv to install (pipx/npm/pip)
    wire: Callable[[DoctorContext], List[CheckResult]]  # lay config; returns notes
    doctor: Callable[[DoctorContext], List[CheckResult]]  # health via proc
    surfaces: Tuple[MachineSurface, ...] = ()

    def detect(self) -> bool:
        return component_bins.resolve(self.command) is not None


# ---- adapters -------------------------------------------------------------


def _noop_wire(ctx: DoctorContext) -> List[CheckResult]:
    return []


def _surface(
    kind: str,
    command: Tuple[str, ...],
    *,
    timeout_seconds: Optional[float] = None,
    max_chars: Optional[int] = None,
    read_only: bool = True,
    probe: Tuple[str, ...] = (),
    probe_contains: Tuple[str, ...] = (),
) -> MachineSurface:
    return MachineSurface(
        kind=kind,
        command=command,
        read_only=read_only,
        timeout_seconds=timeout_seconds,
        max_chars=max_chars,
        probe=probe,
        probe_contains=probe_contains,
    )


def _surface_from_snapshot(raw: dict[str, Any]) -> MachineSurface:
    return MachineSurface(
        kind=str(raw["kind"]),
        command=tuple(str(part) for part in raw.get("command", [])),
        read_only=bool(raw.get("read_only", True)),
        timeout_seconds=float(raw["timeout_seconds"]) if raw.get("timeout_seconds") is not None else None,
        max_chars=int(raw["max_chars"]) if raw.get("max_chars") is not None else None,
        probe=tuple(str(part) for part in raw.get("probe", [])),
        probe_contains=tuple(str(part) for part in raw.get("probe_contains", [])),
    )


def _apply_snapshot(tool: ManagedTool, contract: dict[str, Any]) -> ManagedTool:
    install = contract.get("install")
    surfaces = contract.get("surfaces")
    if not isinstance(install, list) or not isinstance(surfaces, list):
        raise ValueError(f"managed snapshot contract is incomplete: {tool.name}")
    return replace(
        tool,
        station=str(contract["station"]),
        command=str(contract["command"]),
        summary=str(contract.get("summary") or ""),
        install_args=[str(part) for part in install],
        surfaces=tuple(_surface_from_snapshot(surface) for surface in surfaces if isinstance(surface, dict)),
    )


# memory-doctor and bootstrap-doctor inspect the operator's canonical memory and
# bootstrap files (host-global), not a per-target workspace, so their findings are
# advisory: labeled operator-scoped and never FAIL a workspace doctor run.


def _bootstrap_doctor_doctor(ctx: DoctorContext) -> List[CheckResult]:
    name = "bootstrap-doctor (operator files)"
    r = proc.run(["bootstrap-doctor", "status", "--json"])
    data = r.json()
    if data is None:
        return [(WARN, name, f"installed but unwired or errored (exit {r.code})")]
    if not isinstance(data, dict):
        return [(WARN, name, f"unexpected status output (exit {r.code})")]
    rows_value = data.get("rows", [])
    rows = rows_value if isinstance(rows_value, list) else []
    bad = [row for row in rows if isinstance(row, dict) and row.get("severity") in ("hard", "missing", "unreadable")]
    soft = [row for row in rows if isinstance(row, dict) and row.get("severity") == "soft"]
    if bad:
        return [(WARN, name, f"{len(bad)} file(s) over hard limit / missing (advisory)")]
    if soft:
        return [(WARN, name, f"{len(soft)} file(s) in soft band")]
    return [(OK, name, f"{len(rows)} bootstrap file(s) within limits")]


def _token_glace_doctor(ctx: DoctorContext) -> List[CheckResult]:
    r = proc.run(["token-glace", "doctor", "hooks", "--format", "json"])
    data = r.json()
    if data is None:
        return [(WARN, "token-glace", f"installed but doctor output unreadable (exit {r.code})")]
    if not isinstance(data, dict):
        return [(WARN, "token-glace", f"installed but doctor output unreadable (exit {r.code})")]
    status = data.get("status", "unknown")
    mapping = {"ok": OK, "warn": WARN, "disabled": MANUAL, "broken": FAIL}
    return [(mapping.get(status, WARN), "token-glace", f"hook status: {status}")]


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
    r = proc.run(["agentpantry", "doctor", "--json", "--no-net"])
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
    if not isinstance(data, dict):
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
    channels_value = health.get("selected_channels")
    channels = channels_value if isinstance(channels_value, list) else []
    profile = health.get("profile") or "none"
    if not health.get("configured"):
        top_issue = health.get("top_issue")
        top = top_issue if isinstance(top_issue, dict) else {}
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


def _graphtrail_doctor(ctx: DoctorContext) -> List[CheckResult]:
    """Health-check GraphTrail for the target workspace (optional station)."""
    name = "graphtrail (code graph)"
    binary = component_bins.resolve("graphtrail")
    if not binary:
        return [
            (
                MANUAL,
                name,
                "not installed; run `brigade setup` (direct Cargo install is one-release compatibility only)",
            )
        ]
    db = ctx.target / ".graphtrail" / "graphtrail.db"
    if not db.is_file():
        return [(WARN, name, "installed; run `graphtrail sync` to build .graphtrail/graphtrail.db")]
    r = proc.run([binary, "doctor", "--json"], timeout=30.0)
    if r.code != 0:
        return [(WARN, name, f"doctor exit {r.code}; graph may need re-sync")]
    return [(OK, name, f"db present at {db}")]


def _usage_tracker_doctor(ctx: DoctorContext) -> List[CheckResult]:
    """Advisory usage export health (host-global spend visibility under tokens)."""
    name = "usage-tracker (spend export)"
    if not proc.which("usage-tracker"):
        return [(MANUAL, name, "not installed; run `brigade add tokens` or `brigade add usage-tracker`")]
    argv = ["usage-tracker", "export", "--since", "30d", "--summary-json", "--no-write"]
    r = proc.run(argv, timeout=30.0)
    data = r.json()
    if r.code != 0:
        return [(WARN, name, f"installed but export failed (exit {r.code})")]
    if isinstance(data, dict):
        spend = data.get("total_cost_usd") or data.get("api_spend_usd") or data.get("totalCostUsd")
        detail = "export --since 30d --summary-json --no-write ok"
        if spend is not None:
            detail = f"{detail}, cost={spend}"
        return [(OK, name, detail)]
    return [(OK, name, "export --since 30d --summary-json --no-write ok")]


def _plating_doctor(ctx: DoctorContext) -> List[CheckResult]:
    """Optional publish helper: demo render + leak scan under the guard station."""
    name = "plating (publish demos)"
    if not proc.which("plating"):
        return [(MANUAL, name, "not installed; run `brigade add plating` for optional demo/scrub helpers")]
    r = proc.run(["plating", "--version"], timeout=10.0)
    if r.code != 0:
        return [(WARN, name, f"installed but not runnable (exit {r.code})")]
    version = (r.stdout or r.stderr or "").strip().splitlines()
    detail = version[0][:80] if version else "installed"
    return [(OK, name, detail)]


# miseledger (which absorbed the stationtrail/sourceharvest exporters in v0.3.0)
# operates on the operator's host-global evidence archive and local session logs,
# not a per-target workspace. Like the memory and pantry satellites, its findings
# are advisory: they never FAIL a workspace doctor run.
def _miseledger_doctor(ctx: DoctorContext) -> List[CheckResult]:
    name = "miseledger (evidence archive)"
    binary = component_bins.resolve("miseledger")
    if not binary:
        return [(MANUAL, name, "not installed; run `brigade setup`")]
    r = proc.run([binary, "doctor", "--json"], timeout=120.0)
    if r.code == 124:
        return [
            (
                WARN,
                name,
                "doctor check timed out after 120s (large archive, not an error); run `miseledger doctor` manually",
            )
        ]
    data = r.json()
    if data is None:
        return [(WARN, name, f"installed but unwired or errored (exit {r.code})")]
    if not isinstance(data, dict):
        return [(WARN, name, f"unexpected status output (exit {r.code})")]
    checks_value = data.get("checks", [])
    checks = checks_value if isinstance(checks_value, list) else []
    failed = [check for check in checks if isinstance(check, dict) and check.get("ok") is False]
    status = OK if data.get("ok") is True and not failed and r.code == 0 else WARN
    if failed:
        top = failed[0]
        return [(status, name, f"{len(failed)} check(s) failed; {top.get('name')}: {top.get('detail')}")]
    return [(status, name, f"{len(checks)} check(s) passed")]


def _token_glace_wire(ctx: DoctorContext) -> List[CheckResult]:
    # Wiring installs a host hook; which host depends on the workspace's harnesses.
    hosts = [h for h in ctx.harnesses if h in ("claude", "codex", "cursor")]
    if not hosts:
        return [
            (MANUAL, "token-glace: wire", "no hookable harness selected; run `token-glace install <host>` manually")
        ]
    notes: List[CheckResult] = []
    for h in hosts:
        host = "claude-code" if h == "claude" else h
        r = proc.run(["token-glace", "install", host])
        notes.append(
            (OK if r.code == 0 else WARN, f"token-glace: install {host}", r.stderr.strip()[:80] or "installed")
        )
    return notes


# ---- registry -------------------------------------------------------------

_TOOLS: Tuple[ManagedTool, ...] = (
    ManagedTool(
        name="bootstrap-doctor",
        station="memory",
        command="bootstrap-doctor",
        summary="bootstrap-file size/limit audit",
        install_args=["pipx", "install", "git+https://github.com/escoffier-labs/bootstrap-doctor"],
        wire=_noop_wire,
        doctor=_bootstrap_doctor_doctor,
        surfaces=(
            _surface("doctor-json", ("bootstrap-doctor", "status", "--json"), timeout_seconds=30.0),
            _surface("verify-exit", ("bootstrap-doctor", "status", "--json"), timeout_seconds=30.0),
        ),
    ),
    ManagedTool(
        name="token-glace",
        station="tokens",
        command="token-glace",
        summary="output compaction via host hooks",
        # No npm registry package exists under this name, and a git spec does
        # not build (pnpm-native repo, no prepare hook); the GitHub release
        # tarball is the reviewed installable artifact.
        install_args=[
            "npm",
            "install",
            "-g",
            "https://github.com/escoffier-labs/token-glace/releases/download/v0.8.3/token-glace-v0.8.3.tar.gz",
        ],
        wire=_token_glace_wire,
        doctor=_token_glace_doctor,
        surfaces=(
            _surface("doctor-json", ("token-glace", "doctor", "hooks", "--format", "json"), timeout_seconds=30.0),
            _surface(
                "summary-json",
                ("token-glace", "stats", "--format", "json", "--timezone", "utc"),
                timeout_seconds=30.0,
                max_chars=4000,
                probe=("token-glace", "--help"),
                probe_contains=("--format", "--timezone"),
            ),
            _surface("verify-exit", ("token-glace", "verify"), timeout_seconds=60.0),
        ),
    ),
    ManagedTool(
        name="code-search-api",
        station="search",
        command="code-search-api",
        summary="local semantic code search service with SQLite and Ollama embeddings",
        install_args=["pipx", "install", "git+https://github.com/escoffier-labs/code-search-api"],
        wire=_noop_wire,
        doctor=_code_search_api_doctor,
        surfaces=(_surface("verify-exit", ("code-search-api", "--version"), timeout_seconds=10.0),),
    ),
    ManagedTool(
        name="code-search-mcp",
        station="search",
        command="code-search-mcp",
        summary="compatibility key for the MCP bridge maintained in code-search-api/mcp",
        install_args=["npm", "install", "-g", "@solomonneas/code-search-mcp"],
        wire=_noop_wire,
        doctor=_code_search_mcp_doctor,
        surfaces=(
            _surface("doctor-json", ("code-search", "health", "--json"), timeout_seconds=10.0),
            _surface("verify-exit", ("code-search", "--version"), timeout_seconds=10.0),
        ),
    ),
    ManagedTool(
        name="agentpantry",
        station="pantry",
        command="agentpantry",
        summary="browser session auth sync (source -> sink); process-boundary Go binary",
        install_args=["go", "install", "github.com/escoffier-labs/agentpantry/cmd/agentpantry@latest"],
        wire=_noop_wire,
        doctor=_agentpantry_doctor,
        surfaces=(
            _surface(
                "doctor-json",
                ("agentpantry", "doctor", "--json", "--no-net"),
                read_only=False,
                timeout_seconds=10.0,
                probe=("agentpantry", "doctor", "--help"),
                probe_contains=("-json", "-no-net"),
            ),
            _surface(
                "summary-json",
                ("agentpantry", "inventory", "--json"),
                timeout_seconds=10.0,
                max_chars=4000,
                probe=("agentpantry", "inventory", "--help"),
                probe_contains=("-json",),
            ),
            _surface("verify-exit", ("agentpantry", "version", "--json"), timeout_seconds=10.0),
        ),
    ),
    ManagedTool(
        name="agent-notify",
        station="notifications",
        command="agent-notify",
        summary="private operator notifications for agent events",
        install_args=["go", "install", "github.com/escoffier-labs/agent-notify/cmd/agent-notify@latest"],
        wire=_agent_notify_wire,
        doctor=_agent_notify_doctor,
        surfaces=(
            _surface(
                "doctor-json",
                ("agent-notify", "doctor", "--json", "--skip-network"),
                timeout_seconds=10.0,
                probe=("agent-notify", "doctor", "--help"),
                probe_contains=("--json", "--skip-network"),
            ),
            _surface("verify-exit", ("agent-notify", "version", "--json"), timeout_seconds=10.0),
        ),
    ),
    ManagedTool(
        name="miseledger",
        station="evidence",
        command="miseledger",
        summary="local-first evidence ledger: imports adapter JSONL, FTS search, evidence bundles",
        install_args=["brigade", "setup"],
        wire=_noop_wire,
        doctor=_miseledger_doctor,
        surfaces=(
            _surface(
                "doctor-json",
                ("miseledger", "doctor", "--json"),
                read_only=False,
                timeout_seconds=120.0,
                probe=("miseledger", "doctor", "--help"),
                probe_contains=("--json", "--mcp", "--archive"),
            ),
            _surface(
                "brief-markdown",
                ("miseledger", "evidence", "<task>", "--markdown", "--limit", "5"),
                read_only=False,
                timeout_seconds=10.0,
                max_chars=4000,
                probe=("miseledger", "evidence", "--help"),
                probe_contains=("--markdown", "--limit"),
            ),
            _surface("verify-exit", ("miseledger", "version"), timeout_seconds=10.0),
        ),
    ),
    # GraphTrail closes the other half of the receipts-to-context loop: code-graph
    # briefs and deltas on verify/run receipts. Install is the managed engine
    # binary from `brigade setup`; doctor is fail-open (MANUAL when absent) like
    # every other optional sidecar.
    ManagedTool(
        name="graphtrail",
        station="search",
        command="graphtrail",
        summary="local code-graph CLI: callers, callees, impact, context briefs, and structural diffs",
        install_args=["brigade", "setup"],
        wire=_noop_wire,
        doctor=_graphtrail_doctor,
        surfaces=(
            _surface(
                "brief-markdown",
                ("graphtrail", "context", "<task>", "--markdown"),
                timeout_seconds=10.0,
                max_chars=4000,
                probe=("graphtrail", "context", "--help"),
                probe_contains=("--markdown",),
            ),
            _surface("verify-exit", ("graphtrail", "--version"), timeout_seconds=10.0),
            _surface(
                "doctor-json",
                ("graphtrail", "doctor", "--json"),
                timeout_seconds=30.0,
                probe=("graphtrail", "doctor", "--help"),
                probe_contains=("--json",),
            ),
        ),
    ),
    # usage-tracker is optional spend visibility under the tokens station. The
    # binary name is usage-tracker; station.json in that repo is the external
    # catalog source of truth for install/surfaces.
    ManagedTool(
        name="usage-tracker",
        station="tokens",
        command="usage-tracker",
        summary="local model usage export with spend and Token Glace savings summaries",
        install_args=["pipx", "install", "git+https://github.com/escoffier-labs/usage-tracker"],
        wire=_noop_wire,
        doctor=_usage_tracker_doctor,
        surfaces=(
            _surface(
                "summary-json",
                ("usage-tracker", "export", "--since", "30d", "--summary-json", "--no-write"),
                timeout_seconds=30.0,
                max_chars=4000,
                probe=("usage-tracker", "export", "--help"),
                probe_contains=("--since", "--summary-json", "--no-write"),
            ),
        ),
    ),
    # plating is an optional publish helper under guard: render demos, scan for
    # leaks, and verify recorded CLI output. Process-boundary; never required.
    ManagedTool(
        name="plating",
        station="guard",
        command="plating",
        summary="demo rendering, leak scanning, and recorded-output drift verification",
        install_args=["pipx", "install", "git+https://github.com/escoffier-labs/plating"],
        wire=_noop_wire,
        doctor=_plating_doctor,
        surfaces=(
            _surface("verify-exit", ("plating", "--version"), timeout_seconds=10.0),
            _surface("verify-exit", ("plating", "scan", "--help"), timeout_seconds=10.0),
        ),
    ),
)

_SNAPSHOT_CONTRACTS = managed_snapshot.executable_contracts()
_TOOLS = tuple(
    _apply_snapshot(tool, _SNAPSHOT_CONTRACTS[tool.name]) if tool.name in _SNAPSHOT_CONTRACTS else tool
    for tool in _TOOLS
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
