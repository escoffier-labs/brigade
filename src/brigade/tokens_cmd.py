"""Tokens station commands for Token Glace + usage-tracker integration.

Plans and reports only. Does not install host hooks or export usage unless the
operator runs the printed sidecar commands.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import proc
from . import station_health as health


DOCS = {
    "token-glace": {
        "product": "https://brigade.tools/token-glace",
        "repo": "https://github.com/escoffier-labs/token-glace",
    },
    "usage-tracker": {
        "product": "https://brigade.tools/usage-tracker",
        "repo": "https://github.com/escoffier-labs/usage-tracker",
    },
}

BOUNDARIES = [
    "Token Glace and usage-tracker stay process-boundary tools; Brigade only installs, plans, and health-checks.",
    "Brigade does not rewrite host hooks or export private usage data from these commands.",
    "Tokens station tools are optional and fail-open for workspace doctor.",
]


def status_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    glace = proc.which("token-glace")
    tracker = proc.which("usage-tracker")

    tools: dict[str, Any] = {
        "token-glace": {"installed": glace is not None, "binary": glace},
        "usage-tracker": {"installed": tracker is not None, "binary": tracker},
    }

    installed_any = glace is not None or tracker is not None
    payload = health.base_payload(
        target=target,
        station="tokens",
        summary="tokens tools not installed; run `brigade add tokens`",
        health="missing",
        installed=installed_any,
        next_commands=[
            "brigade add tokens",
            "brigade tokens wire plan",
            "brigade tokens doctor",
        ],
        docs={
            "token_glace": DOCS["token-glace"]["product"],
            "usage_tracker": DOCS["usage-tracker"]["product"],
        },
        boundaries=BOUNDARIES,
        tools=tools,
        pipeline=[
            "token-glace install <host>",
            "host output compaction hooks",
            "optional usage-tracker export --summary-json",
        ],
    )
    if not installed_any:
        return payload

    glace_health = "missing"
    glace_summary = "token-glace not installed"
    if glace:
        result = health.run_json([glace, "doctor", "hooks", "--format", "json"], timeout=30.0)
        tools["token-glace"]["doctor"] = result
        data = result.get("stdout_json") if isinstance(result.get("stdout_json"), dict) else {}
        status = str(data.get("status") or "unknown")
        mapping = {"ok": "ok", "warn": "warn", "disabled": "unwired", "broken": "fail"}
        glace_health = mapping.get(status, "incomplete" if result.get("exit_code") not in (0, None) else "warn")
        glace_summary = f"token-glace hook status: {status}"
        tools["token-glace"]["health"] = glace_health
        tools["token-glace"]["summary"] = glace_summary

    tracker_health = "missing"
    tracker_summary = "usage-tracker not installed"
    if tracker:
        # Prefer compact summary surface when present; fall back to --help probe.
        summary = health.run_json([tracker, "export", "--summary-json"], timeout=30.0)
        tools["usage-tracker"]["summary_probe"] = summary
        if summary.get("exit_code") == 0 and isinstance(summary.get("stdout_json"), dict):
            data = summary["stdout_json"]
            tracker_health = "ok"
            spend = data.get("total_cost_usd") or data.get("api_spend_usd") or data.get("totalCostUsd")
            tracker_summary = "usage-tracker export ok" + (f", cost={spend}" if spend is not None else "")
        elif summary.get("exit_code") == 0:
            tracker_health = "ok"
            tracker_summary = "usage-tracker export ok (non-JSON summary)"
        else:
            help_probe = proc.run([tracker, "export", "--help"], timeout=10.0)
            if help_probe.code == 0:
                tracker_health = "warn"
                tracker_summary = "usage-tracker installed; export --summary-json failed (see summary_probe)"
            else:
                tracker_health = "incomplete"
                tracker_summary = f"usage-tracker not runnable (exit {help_probe.code})"
        tools["usage-tracker"]["health"] = tracker_health
        tools["usage-tracker"]["summary"] = tracker_summary

    levels = []
    for key in ("token-glace", "usage-tracker"):
        if tools[key].get("installed"):
            levels.append(tools[key].get("health") or "incomplete")
    rank = {"fail": 5, "timeout": 4, "incomplete": 3, "unwired": 2, "warn": 1, "ok": 0, "missing": 0}
    overall = max(levels, key=lambda h: rank.get(str(h), 0)) if levels else "missing"
    payload["health"] = overall
    parts = []
    if glace:
        parts.append(glace_summary)
    if tracker:
        parts.append(tracker_summary)
    payload["summary"] = "; ".join(parts) if parts else payload["summary"]

    payload["next_commands"] = [
        "brigade tokens wire plan",
        "brigade tokens doctor",
        "token-glace doctor hooks --format json",
    ]
    if overall in ("fail", "unwired", "incomplete"):
        payload["next_commands"] = [
            "brigade tokens wire plan",
            "token-glace doctor hooks --format json",
            "brigade tokens doctor",
        ]
    return payload


def status(*, target: Path, json_output: bool = False) -> int:
    payload = status_payload(target)
    if json_output:
        health.json_print(payload)
        return 0
    print(f"tokens: {payload['summary']}")
    print(f"health: {payload.get('health') or 'unknown'} (advisory; never fails workspace doctor)")
    tools = payload.get("tools") or {}
    for name, row in tools.items():
        if not isinstance(row, dict):
            continue
        marker = "installed" if row.get("installed") else "missing"
        detail = row.get("summary") or ""
        print(f"- {name}: {marker}" + (f" ({detail})" if detail else ""))
    health.print_next(payload)
    return 0


def doctor(*, target: Path, json_output: bool = False) -> int:
    payload = status_payload(target)
    payload["command"] = "tokens doctor"
    if json_output:
        health.json_print(payload)
    else:
        print(f"tokens doctor: {payload['summary']}")
        print(f"health: {payload.get('health') or 'unknown'}")
        tools = payload.get("tools") or {}
        for name, row in tools.items():
            if isinstance(row, dict) and row.get("installed"):
                print(f"- {name}: {row.get('health') or '?'} - {row.get('summary') or ''}")
        health.print_next(payload)
        print("note: tokens checks are advisory for workspace doctor; this command exits 1 on fail/incomplete/timeout")
    return health.doctor_exit(str(payload.get("health") or "missing"))


def wire_plan_payload(*, target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    return {
        "target": str(target),
        "station": "tokens",
        "kind": "wire",
        "title": "tokens wire plan",
        "created_at": health.now_iso(),
        "installed": {
            "token-glace": proc.which("token-glace") is not None,
            "usage-tracker": proc.which("usage-tracker") is not None,
        },
        "commands": [
            ["token-glace", "install", "claude-code"],
            ["token-glace", "install", "codex"],
            ["token-glace", "doctor", "hooks", "--format", "json"],
            ["usage-tracker", "export", "--summary-json"],
        ],
        "manual_steps": [
            "Install Token Glace hooks only for harnesses you actually use.",
            "Tell agents what the wrapper means so they do not fight compacted output.",
            "usage-tracker export is optional spend visibility; it never blocks work.",
        ],
        "boundaries": BOUNDARIES,
        "next_commands": [
            "Review the commands below, then run them yourself.",
            "brigade tokens doctor",
            "brigade add tokens",
        ],
        "docs": {
            "token_glace": DOCS["token-glace"]["product"],
            "usage_tracker": DOCS["usage-tracker"]["product"],
        },
        "pipeline": [
            "token-glace install",
            "host hooks",
            "optional usage-tracker export",
        ],
    }


def wire_plan(*, target: Path, write: bool = False, json_output: bool = False) -> int:
    payload = wire_plan_payload(target=target)
    if write:
        payload = health.write_plan(target, "tokens", payload)
    if json_output:
        health.json_print(payload)
        return 0
    if write:
        print(f"wrote tokens wire plan: {payload['plan_path']}")
    else:
        print(health.render_plan_md("tokens wire plan", payload), end="")
    return 0
