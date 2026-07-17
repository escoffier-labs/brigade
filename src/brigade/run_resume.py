"""Re-attach interrupted app-server workers from a past run and re-synthesize.

Salvage path, not a throughput path: workers resume sequentially. Only codex
workers that ran over the app-server transport carry a thread_id and can be
resumed; everything else is reported as non-resumable.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import aboyeur, agents, codex_appserver, runguard
from .roster import Agent, Roster

_RESUMABLE_STATUSES = ("interrupted", "failed")
_NONTERMINAL_RUN_STATUSES = frozenset(
    {"started", "planning", "dispatching", "synthesizing", "artifact-collection", "running"}
)


def _load_json(run_dir: Path, name: str) -> dict | None:
    path = run_dir / name
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _roster_from_snapshot(snapshot: dict) -> Roster:
    agents_map = {}
    for name, raw in (snapshot.get("agents") or {}).items():
        agents_map[name] = Agent(
            name=name,
            cli=raw.get("cli"),
            role=raw.get("role") or "",
            timeout_seconds=raw.get("timeout_seconds"),
            model=raw.get("model"),
            reasoning=raw.get("reasoning"),
            transport=raw.get("transport", "direct"),
            transport_version=raw.get("transport_version"),
            env=dict(raw["env"]) if raw.get("env") else None,
        )
    return Roster(
        orchestrator=snapshot["orchestrator"],
        agents=agents_map,
        max_workers=snapshot.get("max_workers", 4),
        allow_models=tuple(snapshot.get("allow_models") or ()),
        timeout_seconds=snapshot.get("timeout_seconds", 600.0),
        sandbox=snapshot.get("sandbox"),
    )


def _continuation_prompt(task: str) -> str:
    return (
        "You were interrupted before finishing. Original sub-task:\n"
        f"{task}\n\n"
        "Finish the sub-task and return a concise, complete final result."
    )


def resume(run_dir: Path) -> int:
    run_dir = run_dir.expanduser().resolve()
    run_meta = _load_json(run_dir, "run.json")
    if run_meta is None:
        print(
            f"error: missing run artifacts in {run_dir} (need run.json, roster.json, worker-results.json)",
            file=sys.stderr,
        )
        return 2
    status = run_meta.get("status")
    if not isinstance(status, str) or status in _NONTERMINAL_RUN_STATUSES:
        print("error: run is not terminal; recover or wait for the active run before resuming", file=sys.stderr)
        return 2
    workspace = runguard.resolve_run_lock_workspace(run_meta, run_dir)
    if workspace is None:
        print("error: run artifact has no workspace cwd; cannot verify lock ownership", file=sys.stderr)
        return 2
    try:
        runguard.recover_stale_run(workspace, run_dir, required=False)
        with runguard.run_lock(workspace, run_dir=run_dir):
            return _resume_locked(run_dir)
    except runguard.RunLockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _resume_locked(run_dir: Path) -> int:
    run_dir = run_dir.expanduser().resolve()
    run_meta = _load_json(run_dir, "run.json")
    roster_snapshot = _load_json(run_dir, "roster.json")
    worker_data = _load_json(run_dir, "worker-results.json")
    if run_meta is None or roster_snapshot is None or worker_data is None:
        print(
            f"error: missing run artifacts in {run_dir} (need run.json, roster.json, worker-results.json)",
            file=sys.stderr,
        )
        return 2
    status = run_meta.get("status")
    if not isinstance(status, str) or status in _NONTERMINAL_RUN_STATUSES:
        print("error: run is not terminal; recover or wait for the active run before resuming", file=sys.stderr)
        return 2
    raw_cwd = run_meta.get("cwd")
    if not isinstance(raw_cwd, str) or not raw_cwd:
        print("error: run artifact has no workspace cwd; cannot verify lock ownership", file=sys.stderr)
        return 2
    cwd = Path(raw_cwd).expanduser().resolve()
    roster = _roster_from_snapshot(roster_snapshot)
    results = list(worker_data.get("results") or [])
    resumable = [
        r
        for r in results
        if isinstance(r.get("thread_id"), str) and not r.get("ok") and r.get("status") in _RESUMABLE_STATUSES
    ]
    stuck = [r for r in results if not r.get("ok") and r not in resumable]
    for r in stuck:
        print(
            f"non-resumable: {r.get('worker')} ({r.get('detail') or 'failed'}) - no app-server thread recorded",
            file=sys.stderr,
        )
    if not resumable:
        print("error: no resumable workers in this run", file=sys.stderr)
        return 2

    read_only = bool(run_meta.get("read_only"))
    sandbox = roster_snapshot.get("sandbox")

    server = codex_appserver.AppServer(cwd=cwd)
    try:
        server.start()
    except codex_appserver.AppServerError as exc:
        print(f"error: codex app-server unavailable: {exc}", file=sys.stderr)
        return 2
    try:
        for entry in resumable:
            worker = entry.get("worker", "")
            agent = roster.agents.get(worker)
            timeout = agent.timeout_seconds if agent and agent.timeout_seconds is not None else roster.timeout_seconds
            print(f"resuming: {worker} (thread {entry['thread_id']})", file=sys.stderr)
            try:
                thread = server.resume_thread(
                    entry["thread_id"],
                    cwd=cwd,
                    model=agent.model if agent else None,
                    sandbox=sandbox if sandbox is not None else ("read-only" if read_only else None),
                )
                if agent and agent.reasoning is not None:
                    turn = thread.run_turn(
                        _continuation_prompt(entry.get("task", "")),
                        timeout=timeout,
                        effort=agent.reasoning,
                    )
                else:
                    turn = thread.run_turn(_continuation_prompt(entry.get("task", "")), timeout=timeout)
            except codex_appserver.AppServerError as exc:
                entry["detail"] = str(exc)[:200]
                entry["status"] = "failed"
                continue
            entry["text"] = turn.text.strip()
            entry["ok"] = turn.ok and bool(turn.text.strip())
            entry["detail"] = "" if entry["ok"] else (turn.detail or f"turn {turn.status}")[:200]
            entry["status"] = turn.status
    finally:
        server.close()

    worker_results = [
        aboyeur.WorkerResult(
            worker=r.get("worker", ""),
            task=r.get("task", ""),
            text=r.get("text", ""),
            ok=bool(r.get("ok")),
            detail=r.get("detail", ""),
            thread_id=r.get("thread_id"),
            status=r.get("status", ""),
        )
        for r in results
    ]
    ground_truth = worker_data.get("ground_truth") or {}
    aboyeur._write_json(
        run_dir / "worker-results.json",
        {
            "schema": "brigade.worker_results.v1",
            "results": aboyeur._worker_payload(worker_results),
            "ground_truth": ground_truth,
        },
    )

    task = run_meta.get("task", "")
    synth_prompt = aboyeur.build_synth_prompt(task, worker_results, read_only=read_only, ground_truth=ground_truth)
    orchestrator = roster.agents[roster.orchestrator]
    if orchestrator.cli is None:
        print(
            f"error: orchestrator {roster.orchestrator!r} has no CLI in roster.json; cannot re-synthesize",
            file=sys.stderr,
        )
        return 2
    final = agents.run_agent(
        orchestrator.cli,
        synth_prompt,
        timeout=orchestrator.timeout_seconds or roster.timeout_seconds,
        cwd=cwd,
        read_only=read_only,
        model=orchestrator.model,
        reasoning=orchestrator.reasoning,
        env=dict(orchestrator.env) if orchestrator.env is not None else None,
    )
    aboyeur._write_json(
        run_dir / "synthesis.json",
        {
            "schema": "brigade.synthesis.v1",
            "orchestrator": roster.orchestrator,
            "result": {"ok": final.ok, "detail": final.detail, "text": final.text},
            "ground_truth": ground_truth,
        },
    )
    now = datetime.now(timezone.utc).isoformat()
    run_meta.setdefault("resumed_at", []).append(now)
    if not final.ok:
        run_meta["status"] = "failed"
        run_meta["error"] = final.detail
        aboyeur._write_json(run_dir / "run.json", run_meta)
        print(f"error: orchestrator failed during synthesis: {final.detail}", file=sys.stderr)
        return 2
    (run_dir / "final.txt").write_text(final.text + "\n")
    run_meta["status"] = "ok"
    run_meta.pop("error", None)
    recovered_failure = run_meta.pop("failure", None)
    run_meta.pop("failure_phase", None)
    if isinstance(recovered_failure, dict):
        history = run_meta.get("recovery_history")
        if not isinstance(history, list):
            history = []
            run_meta["recovery_history"] = history
        history.append(recovered_failure)
    aboyeur._write_json(run_dir / "run.json", run_meta)
    print(final.text)
    return 0
