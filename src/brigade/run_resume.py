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

from . import aboyeur, agents, codex_appserver, receipt_schema, run_lifecycle, runguard
from .roster import Agent, Roster, _as_bool, _as_env

_RESUMABLE_STATUSES = ("interrupted", "failed")
_NONTERMINAL_RUN_STATUSES = frozenset(
    {
        "started",
        "planning",
        "dispatching",
        "result-processing",
        "synthesizing",
        "handoff",
        "artifact-collection",
        "running",
    }
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


def _interrupted_appserver_results(run_dir: Path) -> dict | None:
    """Rebuild the minimal resume handoff left by an interrupted dispatch.

    App-server notifications are flushed as they arrive, before the aggregate
    worker-results sidecar is written.  They are therefore the durable source
    of thread coordinates when the run owner exits in the dispatch gate.
    """
    run_meta = _load_json(run_dir, "run.json")
    plan = _load_json(run_dir, "plan.json")
    if run_meta is None or plan is None or run_meta.get("codex_transport") != "app-server":
        return None
    assignments = plan.get("assignments")
    if not isinstance(assignments, list):
        return None
    active_stage = run_meta.get("active_stage")
    active_seats = run_meta.get("active_seats")
    active_seat_set = (
        frozenset(seat for seat in active_seats if isinstance(seat, str)) if isinstance(active_seats, list) else None
    )
    recovered: list[dict[str, object]] = []
    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        worker = assignment.get("worker")
        task = assignment.get("task")
        if not isinstance(worker, str) or not isinstance(task, str):
            continue
        if isinstance(active_stage, int) and assignment.get("stage", 1) != active_stage:
            continue
        if active_seat_set is not None and worker not in active_seat_set:
            continue
        event_path = run_dir / "events" / f"{aboyeur._slug(worker)}.jsonl"
        thread_id: str | None = None
        try:
            lines = event_path.read_text().splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, RecursionError):
                continue
            params = event.get("params") if isinstance(event, dict) else None
            candidate = params.get("threadId") if isinstance(params, dict) else None
            if isinstance(candidate, str) and candidate:
                thread_id = candidate
        if thread_id is not None:
            recovered.append(
                {
                    "worker": worker,
                    "task": task,
                    "ok": False,
                    "detail": "run owner exited during app-server dispatch",
                    "thread_id": thread_id,
                    "status": "interrupted",
                    "transport": "codex-app-server",
                }
            )
    if not recovered:
        return None
    return receipt_schema.worker_results_document(recovered)


def _interrupted_salvage_blocked(
    run_dir: Path,
    run_meta: dict,
    *,
    worker_data: dict | None,
    interrupted_data: dict | None,
) -> str | None:
    """Return a user-facing error when multi-stage salvage cannot re-synthesize."""
    if interrupted_data is None:
        return None
    active_stage = run_meta.get("active_stage")
    if not isinstance(active_stage, int) or active_stage <= 1:
        return None
    if worker_data is not None:
        plan = _load_json(run_dir, "plan.json")
        assignments = plan.get("assignments") if plan is not None else None
        if not isinstance(assignments, list):
            return (
                f"interrupted app-server dispatch at stage {active_stage} cannot be resumed; "
                "earlier-stage worker results were not persisted"
            )
        prior_workers = {
            assignment.get("worker")
            for assignment in assignments
            if isinstance(assignment, dict)
            and isinstance(assignment.get("worker"), str)
            and assignment.get("stage", 1) < active_stage
        }
        if not prior_workers:
            return None
        results = worker_data.get("results")
        if not isinstance(results, list):
            results = []
        represented = {entry.get("worker") for entry in results if isinstance(entry, dict)}
        if prior_workers.issubset(represented):
            return None
    return (
        f"interrupted app-server dispatch at stage {active_stage} cannot be resumed; "
        "earlier-stage worker results were not persisted"
    )


def _archive_failure(run_meta: dict) -> None:
    recovered_failure = run_meta.pop("failure", None)
    run_meta.pop("failure_phase", None)
    if not isinstance(recovered_failure, dict):
        return
    history = run_meta.get("recovery_history")
    if not isinstance(history, list):
        history = []
        run_meta["recovery_history"] = history
    history.append(recovered_failure)


def _refresh_run_timing(run_meta: dict, *, finished_at: datetime | None = None) -> None:
    finished = finished_at or datetime.now(timezone.utc)
    run_meta["status_started_at"] = aboyeur._utc_iso(finished)
    run_meta["finished_at"] = aboyeur._utc_iso(finished)
    started_at = run_meta.get("started_at")
    if isinstance(started_at, str):
        started = aboyeur._parse_iso_datetime(started_at)
        if started is not None:
            run_meta["duration_seconds"] = max(
                0.0,
                round((finished - started).total_seconds(), 3),
            )


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
            env=_as_env(raw.get("env"), name),
            invalid_final_fallback=raw.get("invalid_final_fallback"),
            read_only_capable=_as_bool(
                raw.get("read_only_capable", True),
                f"agents.{name}.read_only_capable",
            ),
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
    workspace = runguard.resolve_run_lock_workspace(run_meta, run_dir)
    if workspace is None:
        print("error: run artifact has no workspace cwd; cannot verify lock ownership", file=sys.stderr)
        return 2
    try:
        runguard.recover_stale_run(workspace, run_dir, required=False)
        with runguard.run_lock(workspace, run_dir=run_dir):
            run_meta = _load_json(run_dir, "run.json")
            status = run_meta.get("status") if run_meta is not None else None
            if run_meta is not None and isinstance(status, str) and status in _NONTERMINAL_RUN_STATUSES:
                interrupted = _interrupted_appserver_results(run_dir)
                if interrupted is None:
                    print(
                        "error: run is not terminal; no durable app-server thread coordinates are available",
                        file=sys.stderr,
                    )
                    return 2
                salvage_error = _interrupted_salvage_blocked(
                    run_dir,
                    run_meta,
                    worker_data=_load_json(run_dir, "worker-results.json"),
                    interrupted_data=interrupted,
                )
                if salvage_error is not None:
                    print(f"error: {salvage_error}", file=sys.stderr)
                    return 2
                # A live owner cannot coexist with this lock.  Reaching here
                # means an earlier owner disappeared without terminalizing the
                # snapshot (for example between a gate and its exception
                # handler).  Commit the missing terminal journal fact before
                # deciding whether app-server coordinates make it resumable.
                aboyeur.record_run_termination(
                    run_dir,
                    status="failed",
                    failure_phase=status,
                    failure_kind="owner-process-exited",
                    detail="run owner exited before recording a terminal state",
                )
            return _resume_locked(run_dir)
    except runguard.RunLockError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def _resume_locked(
    run_dir: Path,
    *,
    approval_resume: bool = False,
    approval_token: str | None = None,
) -> int:
    run_dir = run_dir.expanduser().resolve()
    run_meta = _load_json(run_dir, "run.json")
    roster_snapshot = _load_json(run_dir, "roster.json")
    worker_data_snapshot = _load_json(run_dir, "worker-results.json")
    interrupted_data = _interrupted_appserver_results(run_dir)
    salvage_error = (
        None
        if run_meta is None
        else _interrupted_salvage_blocked(
            run_dir,
            run_meta,
            worker_data=worker_data_snapshot,
            interrupted_data=interrupted_data,
        )
    )
    if salvage_error is not None:
        print(f"error: {salvage_error}", file=sys.stderr)
        return 2
    worker_data = worker_data_snapshot
    if worker_data is None:
        worker_data = interrupted_data
    elif interrupted_data is not None:
        results = worker_data.get("results")
        interrupted = interrupted_data.get("results")
        if isinstance(results, list) and isinstance(interrupted, list):
            represented = {entry.get("worker") for entry in results if isinstance(entry, dict)}
            worker_data = dict(worker_data)
            worker_data["results"] = [
                *results,
                *(entry for entry in interrupted if entry.get("worker") not in represented),
            ]
    if run_meta is None or roster_snapshot is None or worker_data is None:
        print(
            f"error: missing run artifacts in {run_dir} (need run.json, roster.json, worker-results.json)",
            file=sys.stderr,
        )
        return 2
    status = run_meta.get("status")
    approval_reference = run_meta.get("approval_reference")
    approval_reserved = (
        status == "running"
        and isinstance(approval_reference, dict)
        and approval_reference.get("decision_state") == "approved"
    )
    invalid_status = (
        not approval_reserved if approval_resume else not isinstance(status, str) or status in _NONTERMINAL_RUN_STATUSES
    )
    if invalid_status:
        print("error: run is not terminal; recover or wait for the active run before resuming", file=sys.stderr)
        return 2
    raw_cwd = run_meta.get("cwd")
    if not isinstance(raw_cwd, str) or not raw_cwd:
        print("error: run artifact has no workspace cwd; cannot verify lock ownership", file=sys.stderr)
        return 2
    cwd = Path(raw_cwd).expanduser().resolve()
    try:
        roster = _roster_from_snapshot(roster_snapshot)
    except (KeyError, TypeError, ValueError) as exc:
        print(f"error: invalid roster snapshot: {exc}", file=sys.stderr)
        return 2
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
    if approval_resume:
        from . import runs_cmd

        if approval_token is None or not runs_cmd._APPROVAL_MARKER_RE.fullmatch(approval_token):
            print("error: approval resume token is missing or invalid", file=sys.stderr)
            return 2
        server = codex_appserver.AppServer(
            cwd=cwd,
            env={runs_cmd._APPROVAL_RESUME_TOKEN_ENV: approval_token},
        )
    else:
        server = codex_appserver.AppServer(cwd=cwd)

    def redact_approval_token(value: str) -> str:
        if approval_resume and approval_token is not None:
            return value.replace(approval_token, "[redacted]")
        return value

    try:
        server.start()
    except codex_appserver.AppServerError as exc:
        print(
            f"error: codex app-server unavailable: {redact_approval_token(str(exc))}",
            file=sys.stderr,
        )
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
                entry["detail"] = redact_approval_token(str(exc))[:200]
                entry["status"] = "failed"
                continue
            entry["text"] = redact_approval_token(turn.text).strip()
            entry["ok"] = turn.ok and bool(turn.text.strip())
            entry["detail"] = "" if entry["ok"] else redact_approval_token(turn.detail or f"turn {turn.status}")[:200]
            entry["status"] = turn.status
    finally:
        server.close()

    if approval_resume:
        from . import runs_cmd

        assert isinstance(approval_reference, dict)
        reference = run_lifecycle.normalize_approval_reference(approval_reference)
        runs_cmd._finalize_redeemed_approval(run_dir, cwd, reference)

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
    aboyeur.write_sidecar_revision(
        run_dir,
        "worker-results.json",
        receipt_schema.worker_results_document(
            aboyeur._worker_payload(worker_results),
            ground_truth=ground_truth,
        ),
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
    aboyeur.write_sidecar_revision(
        run_dir,
        "synthesis.json",
        receipt_schema.synthesis_document(
            orchestrator=roster.orchestrator,
            result={"ok": final.ok, "detail": final.detail, "text": final.text},
            ground_truth=ground_truth,
        ),
    )
    now = datetime.now(timezone.utc).isoformat()
    run_meta.setdefault("resumed_at", []).append(now)
    if not final.ok:
        bounded_detail = (final.detail or "orchestrator failed during synthesis")[:2000]
        _archive_failure(run_meta)
        run_meta["status"] = "failed"
        run_meta["error"] = bounded_detail
        run_meta["failure_phase"] = "synthesis"
        run_meta["failure"] = {
            "phase": "synthesis",
            "kind": "agent-error",
            "detail": bounded_detail,
            "seat": roster.orchestrator,
        }
        _refresh_run_timing(run_meta)
        try:
            aboyeur._write_json(run_dir / "run.json", receipt_schema.stamp_run_receipt(run_meta))
        except run_lifecycle.LifecycleJournalError as exc:
            raise runguard.RetainRunLockError(f"failed to write failed-synthesis run receipt: {exc}") from exc
        print(f"error: orchestrator failed during synthesis: {final.detail}", file=sys.stderr)
        return 2
    (run_dir / "final.txt").write_text(final.text + "\n")
    run_meta["status"] = "ok"
    run_meta.pop("error", None)
    _archive_failure(run_meta)
    _refresh_run_timing(run_meta)
    try:
        aboyeur._write_json(run_dir / "run.json", receipt_schema.stamp_run_receipt(run_meta))
    except run_lifecycle.LifecycleJournalError as exc:
        raise runguard.RetainRunLockError(f"failed to write successful-synthesis run receipt: {exc}") from exc
    print(final.text)
    return 0
