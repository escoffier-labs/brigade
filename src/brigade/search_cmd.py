"""Search station commands for GraphTrail + code-search integration.

GraphTrail and code-search remain process-boundary binaries. Explicit
``brigade code sync|context|impact`` commands, plus their ``brigade search``
compatibility aliases, execute GraphTrail. ``brigade search sync plan`` stays
review-only. Brigade never starts code-search-api, and workspace doctor remains
fail-open when optional search tools are absent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import proc
from . import station_health as health


DOCS = {
    "graphtrail": {
        "product": "https://brigade.tools/graphtrail",
        "repo": "https://github.com/escoffier-labs/graphtrail",
    },
    "code-search": {
        "product": "https://brigade.tools/code-search",
        "repo": "https://github.com/escoffier-labs/code-search-api",
    },
}

BOUNDARIES = [
    "GraphTrail and code-search stay process-boundary binaries.",
    "Explicit `brigade code sync|context|impact` commands execute GraphTrail across a process boundary; `brigade search sync|context|impact` are compatibility aliases.",
    "`brigade search sync plan` is review-only and never executes GraphTrail.",
    "Brigade never starts code-search-api.",
    "Search station tools are optional and fail-open for workspace doctor.",
    "The code-search-mcp compatibility key is maintained by code-search-api/mcp.",
]


def status_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    graphtrail_bin = proc.which("graphtrail")
    api_bin = proc.which("code-search-api")
    mcp_bin = proc.which("code-search-mcp")
    db = target / ".graphtrail" / "graphtrail.db"

    tools: dict[str, Any] = {
        "graphtrail": {
            "installed": graphtrail_bin is not None,
            "binary": graphtrail_bin,
            "db_present": db.is_file(),
            "db_path": str(db),
        },
        "code-search-api": {
            "installed": api_bin is not None,
            "binary": api_bin,
        },
        "code-search-mcp": {
            "installed": mcp_bin is not None,
            "binary": mcp_bin,
            "owner": "code-search-api/mcp",
            "compatibility_key": "code-search-mcp",
        },
    }

    installed_any = any(row["installed"] for row in tools.values())
    payload = health.base_payload(
        target=target,
        station="search",
        summary="search tools not installed; run `brigade setup`",
        health="missing",
        installed=installed_any,
        next_commands=[
            "brigade setup",
            "brigade search sync plan",
            "brigade search doctor",
        ],
        docs={
            "graphtrail": DOCS["graphtrail"]["product"],
            "code_search": DOCS["code-search"]["product"],
        },
        boundaries=BOUNDARIES,
        tools=tools,
        pipeline=[
            "graphtrail sync",
            ".graphtrail/graphtrail.db",
            "graphtrail context / receipt code_graph_delta",
            "optional code-search-api serve + code-search-api/mcp bridge",
        ],
    )

    if not installed_any:
        return payload

    # GraphTrail health
    graph_health = "missing"
    graph_summary = "graphtrail not installed"
    graph_doctor: dict[str, Any] | None = None
    if graphtrail_bin:
        if not db.is_file():
            graph_health = "unwired"
            graph_summary = "graphtrail installed; run `graphtrail sync` to build .graphtrail/graphtrail.db"
            graph_doctor = health.run_json([graphtrail_bin, "doctor", "--json"], timeout=30.0)
        else:
            graph_doctor = health.run_json([graphtrail_bin, "doctor", "--json"], timeout=30.0)
            tools["graphtrail"]["doctor"] = graph_doctor
            exit_code = int(graph_doctor.get("exit_code") or 0)
            if exit_code == 124:
                graph_health = "timeout"
                graph_summary = "graphtrail doctor timed out"
            elif exit_code != 0:
                graph_health = "fail"
                graph_summary = f"graphtrail doctor exit {exit_code}"
            else:
                graph_health = "ok"
                graph_summary = f"graphtrail db present at {db}"
        tools["graphtrail"]["health"] = graph_health
        tools["graphtrail"]["summary"] = graph_summary
        tools["graphtrail"]["doctor"] = graph_doctor

    # code-search-api health (HTTP probe via managed doctor adapter)
    api_health = "missing"
    api_summary = "code-search-api not installed"
    if api_bin:
        from . import managed
        from .doctor import build_context

        ctx_results = managed._code_search_api_doctor(build_context(target))
        status, _name, detail = ctx_results[0]
        api_summary = detail
        if status == "OK":
            api_health = "ok"
        elif status == "WARN":
            api_health = "warn"
        else:
            api_health = "incomplete"
        tools["code-search-api"]["health"] = api_health
        tools["code-search-api"]["summary"] = api_summary
        tools["code-search-api"]["doctor_status"] = status

    if mcp_bin:
        tools["code-search-mcp"]["health"] = "ok"
        tools["code-search-mcp"]["summary"] = "installed; wire CODE_SEARCH_API_URL into MCP clients"

    # Aggregate health: worst of present tools
    levels = []
    for key in ("graphtrail", "code-search-api", "code-search-mcp"):
        if tools[key].get("installed"):
            levels.append(tools[key].get("health") or "incomplete")
    rank = {"fail": 5, "timeout": 4, "incomplete": 3, "unwired": 2, "warn": 1, "ok": 0, "missing": 0}
    overall = max(levels, key=lambda h: rank.get(str(h), 0)) if levels else "missing"
    payload["health"] = overall

    parts = []
    if graphtrail_bin:
        parts.append(graph_summary)
    if api_bin:
        parts.append(f"code-search-api={api_health}")
    if mcp_bin:
        parts.append("code-search-mcp=installed (owner=code-search-api/mcp)")
    payload["summary"] = "; ".join(parts) if parts else payload["summary"]

    payload["next_commands"] = [
        "brigade search sync plan",
        "brigade search doctor",
        "brigade operator checkup --target .",
    ]
    if overall in ("fail", "unwired", "incomplete", "timeout"):
        payload["next_commands"] = [
            "brigade search sync plan",
            "graphtrail sync",
            "brigade search doctor",
        ]
    elif not graphtrail_bin:
        payload["next_commands"] = ["brigade setup", "brigade search sync plan"]
    return payload


def status(*, target: Path, json_output: bool = False) -> int:
    payload = status_payload(target)
    if json_output:
        health.json_print(payload)
        return 0
    print(f"search: {payload['summary']}")
    print(f"health: {payload.get('health') or 'unknown'} (advisory; never fails workspace doctor)")
    tools = payload.get("tools") or {}
    for name, row in tools.items():
        if not isinstance(row, dict):
            continue
        marker = "installed" if row.get("installed") else "missing"
        print(f"- {name}: {marker}" + (f" ({row.get('summary')})" if row.get("summary") else ""))
    if payload.get("pipeline"):
        print("pipeline: " + " -> ".join(payload["pipeline"]))
    health.print_next(payload)
    return 0


def doctor(*, target: Path, json_output: bool = False) -> int:
    payload = status_payload(target)
    payload["command"] = "search doctor"
    if json_output:
        health.json_print(payload)
    else:
        print(f"search doctor: {payload['summary']}")
        print(f"health: {payload.get('health') or 'unknown'}")
        tools = payload.get("tools") or {}
        for name, row in tools.items():
            if isinstance(row, dict) and row.get("installed"):
                print(f"- {name}: {row.get('health') or '?'} - {row.get('summary') or ''}")
        health.print_next(payload)
        print("note: search checks are advisory for workspace doctor; this command exits 1 on fail/incomplete/timeout")
    return health.doctor_exit(str(payload.get("health") or "missing"))


def sync_plan_payload(*, target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    return {
        "target": str(target),
        "station": "search",
        "kind": "sync",
        "title": "search sync plan",
        "created_at": health.now_iso(),
        "installed": {
            "graphtrail": proc.which("graphtrail") is not None,
            "code-search-api": proc.which("code-search-api") is not None,
            "code-search-mcp": proc.which("code-search-mcp") is not None,
        },
        "compatibility": {"code-search-mcp": {"owner": "code-search-api/mcp"}},
        "commands": [
            ["graphtrail", "sync", str(target)],
            ["graphtrail", "doctor", "--json"],
            ["graphtrail", "stats", "--json"],
            ["code-search-api", "index"],
            ["code-search-api", "serve"],
        ],
        "manual_steps": [
            "Run graphtrail sync in each repo that should contribute code-graph deltas.",
            "Start code-search-api only when you want local semantic search (optional).",
            "Configure MCP clients with CODE_SEARCH_API_URL after the API is listening.",
        ],
        "boundaries": BOUNDARIES,
        "next_commands": [
            "Review the commands below, then run them yourself.",
            "brigade search doctor",
            "brigade operator checkup --target .",
        ],
        "docs": {
            "graphtrail": DOCS["graphtrail"]["product"],
            "code_search": DOCS["code-search"]["product"],
        },
        "pipeline": [
            "graphtrail sync",
            "code-search-api index/serve (optional)",
            "receipt code_graph_delta / context briefs",
        ],
    }


def sync_plan(*, target: Path, write: bool = False, json_output: bool = False) -> int:
    payload = sync_plan_payload(target=target)
    if write:
        payload = health.write_plan(target, "search", payload)
    if json_output:
        health.json_print(payload)
        return 0
    if write:
        print(f"wrote search sync plan: {payload['plan_path']}")
    else:
        print(health.render_plan_md("search sync plan", payload), end="")
    return 0
