"""Tokens station commands for TokenJuice + usage-tracker integration.

Plans and reports only. Does not install host hooks or export usage unless the
operator runs the printed sidecar commands.
"""

from __future__ import annotations

from pathlib import Path
import shlex
from typing import Any

from . import proc, managed
from . import station_health as health


DOCS = {
    "tokenjuice": {
        "product": "https://github.com/vincentkoc/tokenjuice",
        "repo": "https://github.com/vincentkoc/tokenjuice",
    },
    "usage-tracker": {
        "product": "https://brigade.tools/usage-tracker",
        "repo": "https://github.com/escoffier-labs/usage-tracker",
    },
}

CUTOVER_HINT = "Review Host cutover in memory/cards/token-glace-output-compaction.md before installation."

BOUNDARIES = [
    "TokenJuice and usage-tracker stay process-boundary tools; Brigade only installs, plans, and health-checks.",
    "Brigade does not rewrite host hooks or export private usage data from these commands.",
    "TokenJuice starts a new stats epoch; preserve legacy Glace sidecars for recovery and rollback.",
    "Select one compactor per host; OpenClaw remains disabled. Review hooks before explicit activation.",
    "Tokens station tools are optional and fail-open for workspace doctor.",
]


def status_payload(target: Path) -> dict[str, Any]:
    target = target.expanduser().resolve()
    glace = managed.tokenjuice_binary()
    tracker = proc.which("usage-tracker")
    candidate = proc.which("tokenjuice")
    legacy = proc.which("token-glace")
    occupied = bool(candidate or legacy) and glace is None

    tools: dict[str, Any] = {
        "tokenjuice": {
            "installed": glace is not None,
            "binary": glace,
            "candidate": candidate,
            "identity": "package-metadata-match" if glace else "unverified" if candidate else "missing",
            "provenance": "unverified",
        },
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
            "tokenjuice": DOCS["tokenjuice"]["product"],
            "usage_tracker": DOCS["usage-tracker"]["product"],
        },
        boundaries=BOUNDARIES,
        tools=tools,
        pipeline=[
            "tokenjuice install <host>",
            "host output compaction hooks",
            "optional usage-tracker export --summary-json",
        ],
    )
    payload["legacy"] = {"token-glace": {"binary": legacy, "migration": "manual; preserve hooks and artifacts"}}
    if occupied:
        tools["tokenjuice"]["summary"] = "executable ownership unverified; Glace aliases are not upstream TokenJuice"
        payload["health"] = "warn"
        payload["summary"] = "TokenJuice ownership unverified; review the existing executable before installation"
        payload["next_commands"] = [CUTOVER_HINT, "brigade tokens wire plan", "brigade tokens doctor"]
    if not installed_any:
        return payload

    glace_health = "missing"
    glace_summary = "tokenjuice not installed"
    if glace:
        result = health.run_json([glace, "doctor", "hooks", "--format", "json"], timeout=30.0)
        tools["tokenjuice"]["doctor"] = result
        stdout_json = result.get("stdout_json")
        data: dict[str, Any] = stdout_json if isinstance(stdout_json, dict) else {}
        status = str(data.get("status") or "unknown")
        mapping = {"ok": "ok", "warn": "warn", "disabled": "unwired", "broken": "fail"}
        glace_health = mapping.get(status, "incomplete" if result.get("exit_code") not in (0, None) else "warn")
        glace_summary = f"tokenjuice hook status: {status}"
        tools["tokenjuice"]["health"] = glace_health
        tools["tokenjuice"]["summary"] = glace_summary

    tracker_health = "missing"
    tracker_summary = "usage-tracker not installed"
    if tracker:
        # Prefer compact summary surface when present; fall back to --help probe.
        summary = health.run_json(
            [tracker, "export", "--since", "30d", "--summary-json", "--no-write"],
            timeout=30.0,
        )
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

    levels = ["warn"] if occupied else []
    for key in ("tokenjuice", "usage-tracker"):
        if tools[key].get("installed"):
            levels.append(tools[key].get("health") or "incomplete")
    rank = {"fail": 5, "timeout": 4, "incomplete": 3, "unwired": 2, "warn": 1, "ok": 0, "missing": 0}
    overall = max(levels, key=lambda h: rank.get(str(h), 0)) if levels else "missing"
    payload["health"] = overall
    parts = ["TokenJuice ownership unverified"] if occupied else []
    if glace:
        parts.append(glace_summary)
    if tracker:
        parts.append(tracker_summary)
    payload["summary"] = "; ".join(parts) if parts else payload["summary"]

    qualified_doctor = [shlex.join([glace, "doctor", "hooks", "--format", "json"])] if glace else []
    payload["next_commands"] = [
        "brigade tokens wire plan",
        "brigade tokens doctor",
        *qualified_doctor,
    ]
    if overall in ("fail", "unwired", "incomplete"):
        payload["next_commands"] = [
            "brigade tokens wire plan",
            *qualified_doctor,
            "brigade tokens doctor",
        ]
    if occupied:
        payload["next_commands"].insert(0, CUTOVER_HINT)
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
    verified = managed.tokenjuice_binary()
    occupied = not verified and bool(proc.which("tokenjuice") or proc.which("token-glace"))
    commands = (
        [
            [verified, "install", "claude-code"],
            [verified, "install", "codex"],
            [verified, "doctor", "hooks", "--format", "json"],
        ]
        if verified
        else []
    )
    qualification_steps = (
        []
        if verified
        else [
            "No qualified TokenJuice executable is available. Review the prior owner and its hooks, "
            "complete the manual migration, then regenerate this plan before activation."
        ]
    )
    return {
        "target": str(target),
        "station": "tokens",
        "kind": "wire",
        "title": "tokens wire plan",
        "created_at": health.now_iso(),
        "installed": {
            "tokenjuice": verified is not None,
            "usage-tracker": proc.which("usage-tracker") is not None,
        },
        "commands": [*commands, ["usage-tracker", "export", "--summary-json"]],
        "manual_steps": [
            *qualification_steps,
            "Install TokenJuice hooks only for harnesses you actually use.",
            "Preserve unrelated hooks and legacy artifacts. Remove only the previous compactor owner through its installer.",
            "Use an absolute upstream tokenjuice path in Codex hooks. Verify actual hook events and tool-name matching before cutover.",
            "Qualify the selected release on the actual host route. Codex structured JSON does not use the text-wrap footer; footer acceptance is not a global activation gate.",
            "usage-tracker export is optional spend visibility; it never blocks work.",
        ],
        "boundaries": BOUNDARIES,
        "next_commands": [
            "Review the commands below, then run them yourself.",
            "brigade tokens doctor",
            *([CUTOVER_HINT] if occupied else ["brigade add tokens"]),
        ],
        "docs": {
            "tokenjuice": DOCS["tokenjuice"]["product"],
            "usage_tracker": DOCS["usage-tracker"]["product"],
        },
        "pipeline": [
            "tokenjuice install",
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
