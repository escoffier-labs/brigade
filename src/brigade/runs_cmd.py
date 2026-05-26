"""Inspect Brigade run artifact directories."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return payload


def _read_text(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text().strip()


def _line(label: str, value: object | None) -> None:
    if value not in (None, ""):
        print(f"{label}: {value}")


def _print_roster(roster: dict[str, Any] | None) -> None:
    if not roster:
        return
    agents = roster.get("agents")
    print("roster:")
    _line("  orchestrator", roster.get("orchestrator"))
    _line("  max_workers", roster.get("max_workers"))
    _line("  timeout_seconds", roster.get("timeout_seconds"))
    allow_models = roster.get("allow_models")
    if isinstance(allow_models, list) and allow_models:
        print(f"  allow_models: {', '.join(str(item) for item in allow_models)}")
    if isinstance(agents, dict):
        for name, agent in agents.items():
            if not isinstance(agent, dict):
                continue
            marker = " (orchestrator)" if name == roster.get("orchestrator") else ""
            timeout = agent.get("timeout_seconds")
            timeout_text = f"; timeout={timeout:g}s" if isinstance(timeout, (int, float)) else ""
            print(f"  - {name}: {agent.get('cli', 'unknown')}{marker}{timeout_text}")


def _print_plan(plan: dict[str, Any] | None) -> None:
    assignments = plan.get("assignments") if plan else None
    if not isinstance(assignments, list):
        return
    print("plan:")
    if not assignments:
        print("  (no worker assignments)")
        return
    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        print(f"  -> {assignment.get('worker', 'unknown')}: {assignment.get('task', '')}")


def _print_workers(worker_results: dict[str, Any] | None) -> None:
    results = worker_results.get("results") if worker_results else None
    if not isinstance(results, list):
        return
    print("workers:")
    if not results:
        print("  (none)")
        return
    for result in results:
        if not isinstance(result, dict):
            continue
        marker = "ok" if result.get("ok") else "failed"
        detail = f": {result.get('detail')}" if result.get("detail") else ""
        print(f"  [{marker}] {result.get('worker', 'unknown')}{detail}")


def _print_synthesis(synthesis: dict[str, Any] | None) -> None:
    if not synthesis:
        return
    result = synthesis.get("result")
    print("synthesis:")
    if isinstance(result, dict):
        marker = "ok" if result.get("ok") else "failed"
        detail = f": {result.get('detail')}" if result.get("detail") else ""
        print(f"  [{marker}] {synthesis.get('orchestrator', 'orchestrator')}{detail}")
    else:
        print(f"  {synthesis.get('orchestrator', 'orchestrator')}")


def _print_final(final_text: str | None) -> None:
    if final_text is None:
        return
    print("final:")
    if not final_text:
        print("  (empty)")
        return
    for line in final_text.splitlines():
        print(f"  {line}")


def _short(text: object, limit: int = 72) -> str:
    rendered = " ".join(str(text or "").split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _run_sort_key(item: tuple[Path, dict[str, Any]]) -> str:
    path, meta = item
    value = meta.get("started_at")
    return str(value) if value else path.name


def list_runs(*, cwd: Path, runs_dir: Path | None = None, limit: int = 10) -> int:
    if limit < 1:
        print("error: --limit must be a positive integer", file=sys.stderr)
        return 2

    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        print(f"error: --cwd is not a directory: {cwd}", file=sys.stderr)
        return 2
    root = runs_dir.expanduser() if runs_dir is not None else cwd / ".brigade" / "runs"
    if not root.is_dir():
        print(f"error: runs directory not found: {root}", file=sys.stderr)
        return 2

    runs: list[tuple[Path, dict[str, Any]]] = []
    skipped = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            meta = _read_json(child / "run.json")
        except ValueError:
            skipped += 1
            continue
        if meta is None:
            skipped += 1
            continue
        runs.append((child, meta))

    runs.sort(key=_run_sort_key, reverse=True)
    for path, meta in runs[:limit]:
        status = meta.get("status", "unknown")
        started = meta.get("started_at", path.name)
        duration = meta.get("duration_seconds")
        duration_text = f" {duration:g}s" if isinstance(duration, (int, float)) else ""
        mode = " read-only" if meta.get("read_only") else ""
        if meta.get("dry_run"):
            mode += " dry-run"
        print(f"{started} [{status}]{duration_text}{mode} {path}")
        task = _short(meta.get("task"))
        if task:
            print(f"  {task}")
    if not runs:
        print(f"no runs found in {root}")
    if skipped:
        print(f"skipped {skipped} invalid run director{'y' if skipped == 1 else 'ies'}", file=sys.stderr)
    return 0


def show(run_dir: Path) -> int:
    run_dir = run_dir.expanduser()
    if not run_dir.is_dir():
        print(f"error: run directory not found: {run_dir}", file=sys.stderr)
        return 2

    try:
        run_meta = _read_json(run_dir / "run.json")
        if run_meta is None:
            print(f"error: run.json not found in {run_dir}", file=sys.stderr)
            return 2
        roster = _read_json(run_dir / "roster.json")
        plan = _read_json(run_dir / "plan.json")
        worker_results = _read_json(run_dir / "worker-results.json")
        synthesis = _read_json(run_dir / "synthesis.json")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"run: {run_dir}")
    _line("status", run_meta.get("status"))
    _line("task", run_meta.get("task"))
    _line("cwd", run_meta.get("cwd"))
    mode = "read-only" if run_meta.get("read_only") else "normal"
    if run_meta.get("dry_run"):
        mode = f"{mode}, dry-run"
    print(f"mode: {mode}")
    _line("started", run_meta.get("started_at"))
    _line("finished", run_meta.get("finished_at"))
    duration = run_meta.get("duration_seconds")
    if isinstance(duration, (int, float)):
        print(f"duration: {duration:g}s")
    _line("artifacts", run_meta.get("artifacts"))
    _line("handoff", run_meta.get("handoff"))
    _line("error", run_meta.get("error"))

    _print_roster(roster)
    _print_plan(plan)
    _print_workers(worker_results)
    _print_synthesis(synthesis)
    _print_final(_read_text(run_dir / "final.txt"))
    return 0
