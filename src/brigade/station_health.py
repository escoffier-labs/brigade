"""Shared helpers for first-class station status/doctor/plan surfaces.

Station CLIs (pantry, evidence, search, tokens, ...) share a small health
schema so agents and operators get the same fields: installed, health,
summary, next_commands, docs, and boundaries. Sidecars stay process-boundary
binaries; these helpers only format and shell out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from . import proc
from .localio import utc_now_iso as now_iso

# Shared advisory health levels used by station CLIs (never FAIL workspace doctor).
HEALTH_LEVELS = ("ok", "warn", "fail", "missing", "unwired", "incomplete", "timeout")


def json_print(payload: Mapping[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def run_json(args: Sequence[str], *, timeout: float = 30.0) -> dict[str, Any]:
    result = proc.run(list(args), timeout=timeout)
    data = result.json()
    return {
        "command": list(args),
        "exit_code": result.code,
        "stdout_json": data if isinstance(data, dict) else None,
        "stdout_unparsed": None if isinstance(data, dict) else (result.stdout or "")[:500],
        "stderr": (result.stderr or "")[:500],
    }


def print_next(payload: Mapping[str, Any]) -> None:
    next_commands = payload.get("next_commands") or []
    if not next_commands:
        return
    print("next:")
    for command in next_commands:
        print(f"  {command}")


def health_from_counts(*, fail_count: int = 0, warn_count: int = 0, default: str = "ok") -> str:
    if fail_count:
        return "fail"
    if warn_count:
        return "warn"
    return default


def base_payload(
    *,
    target: Path,
    station: str,
    summary: str,
    health: str = "missing",
    installed: bool = False,
    next_commands: Sequence[str] | None = None,
    docs: Mapping[str, str] | None = None,
    boundaries: Sequence[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "target": str(target),
        "station": station,
        "installed": installed,
        "health": health,
        "summary": summary,
        "advisory": True,
        "next_commands": list(next_commands or []),
        "docs": dict(docs or {}),
        "boundaries": list(boundaries or []),
        "created_at": now_iso(),
    }
    payload.update(extra)
    return payload


def render_plan_md(title: str, payload: Mapping[str, Any]) -> str:
    lines = [f"# {title}", "", f"- target: {payload.get('target')}"]
    if payload.get("station"):
        lines.append(f"- station: {payload.get('station')}")
    docs = payload.get("docs") if isinstance(payload.get("docs"), dict) else {}
    if docs.get("product"):
        lines.append(f"- product: {docs['product']}")
    if docs.get("repo"):
        lines.append(f"- repo: {docs['repo']}")
    if payload.get("pipeline"):
        lines.append(f"- pipeline: {' -> '.join(str(p) for p in payload['pipeline'])}")
    lines.extend(["", "## Commands", ""])
    for command in payload.get("commands") or []:
        lines.append("```sh")
        if isinstance(command, (list, tuple)):
            lines.append(" ".join(str(part) for part in command))
        else:
            lines.append(str(command))
        lines.append("```")
        lines.append("")
    manual = payload.get("manual_steps") or []
    if manual:
        lines.extend(["## Manual Steps", ""])
        for step in manual:
            lines.append(f"- {step}")
        lines.append("")
    boundaries = payload.get("boundaries") or []
    if boundaries:
        lines.extend(["## Boundaries", ""])
        for boundary in boundaries:
            lines.append(f"- {boundary}")
        lines.append("")
    next_commands = payload.get("next_commands") or []
    if next_commands:
        lines.extend(["## Next", ""])
        for command in next_commands:
            lines.append(f"- {command}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_plan(target: Path, station: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    created = str(payload.get("created_at") or now_iso())
    stamp = created.replace(":", "").replace("+", "Z").replace(".", "-")
    kind = str(payload.get("kind") or "plan")
    plan_dir = target / ".brigade" / station / "plans" / f"{stamp}-{kind}"
    plan_dir.mkdir(parents=True, exist_ok=True)
    json_path = plan_dir / "plan.json"
    md_path = plan_dir / "PLAN.md"
    out = dict(payload)
    out["plan_id"] = plan_dir.name
    out["plan_path"] = str(md_path)
    out["receipt_path"] = str(json_path)
    json_path.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")
    title = str(payload.get("title") or f"{station} {kind} plan")
    md_path.write_text(render_plan_md(title, out))
    return out


def doctor_exit(health: str, *, fail_levels: Iterable[str] = ("fail", "incomplete", "timeout")) -> int:
    return 1 if health in set(fail_levels) else 0
