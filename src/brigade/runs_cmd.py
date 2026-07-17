"""Inspect Brigade run artifact directories."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

_NONTERMINAL_STATUSES = frozenset(
    {"started", "planning", "dispatching", "synthesizing", "artifact-collection", "running"}
)
_SUCCESS_STATUSES = frozenset({"ok", "dry-run"})


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


def _runs_root(cwd: Path, runs_dir: Path | None) -> tuple[Path | None, str | None]:
    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        return None, f"error: --cwd is not a directory: {cwd}"
    root = runs_dir.expanduser() if runs_dir is not None else cwd / ".brigade" / "runs"
    if not root.is_dir():
        return None, f"error: runs directory not found: {root}"
    return root, None


def _resolve_run_dir(run: str | Path, *, cwd: Path, runs_dir: Path | None = None) -> tuple[Path | None, str | None]:
    raw = Path(run).expanduser()
    if raw.is_absolute() or len(raw.parts) > 1 or raw.exists():
        run_dir = raw.resolve()
        if not run_dir.is_dir():
            return None, f"error: run directory not found: {run_dir}"
        return run_dir, None

    root, error = _runs_root(cwd, runs_dir)
    if error is not None:
        return None, error
    assert root is not None
    if str(run) == "latest":
        runs, skipped = _collect_runs(root)
        if skipped:
            print(f"skipped {skipped} invalid run director{'y' if skipped == 1 else 'ies'}", file=sys.stderr)
        if not runs:
            return None, f"error: no runs found in {root}"
        return runs[0][0].resolve(), None
    run_dir = (root / raw).resolve()
    if not run_dir.is_dir():
        return None, f"error: run directory not found: {run_dir}"
    return run_dir, None


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


def _print_ground_truth(worker_results: dict[str, Any] | None) -> None:
    ground_truth = worker_results.get("ground_truth") if worker_results else None
    if not isinstance(ground_truth, dict):
        return
    if ground_truth.get("available") is not True:
        reason = ground_truth.get("reason")
        suffix = f" ({reason})" if isinstance(reason, str) and reason else ""
        print(f"ground truth: unavailable{suffix}")
        return
    print("ground truth:")
    changed = [item for item in ground_truth.get("changed_files") or [] if isinstance(item, str)]
    untracked = [item for item in ground_truth.get("untracked_files") or [] if isinstance(item, str)]
    print(f"  changed_files: {len(changed)}" + (f" ({', '.join(changed[:8])})" if changed else ""))
    if untracked:
        print(f"  untracked_files: {len(untracked)} ({', '.join(untracked[:8])})")
    diffstat = ground_truth.get("diffstat")
    if isinstance(diffstat, str) and diffstat.strip():
        for line in diffstat.strip().splitlines():
            print(f"  {line.strip()}")
    patch_ref = ground_truth.get("patch_ref")
    if isinstance(patch_ref, str) and patch_ref:
        print(f"  patch_ref: {patch_ref}")
    for receipt in ground_truth.get("verify_receipts") or []:
        if not isinstance(receipt, dict):
            continue
        print(f"  verify: {receipt.get('run_id', 'unknown')} {receipt.get('status', 'unknown')}")
        for command in receipt.get("commands") or []:
            if isinstance(command, dict):
                print(f"    - {command.get('command', '?')} exit={command.get('exit_code')}")


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


def _duration_text(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "unknown duration"
    return f"{value:g}s"


def _artifact_signature(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _is_terminal(meta: dict[str, Any]) -> bool:
    status = meta.get("status")
    if meta.get("finished_at"):
        return True
    return isinstance(status, str) and status not in _NONTERMINAL_STATUSES


def _failure_fields(meta: dict[str, Any]) -> tuple[object, object, object]:
    failure = meta.get("failure")
    failure_payload = failure if isinstance(failure, dict) else {}
    return (
        meta.get("failure_phase") or failure_payload.get("phase"),
        failure_payload.get("kind"),
        failure_payload.get("detail"),
    )


def _print_failure(meta: dict[str, Any]) -> None:
    phase, kind, detail = _failure_fields(meta)
    _line("failure phase", phase)
    _line("failure kind", kind)
    _line("failure detail", detail)


def _watch_return_code(status: object) -> int:
    if status in _SUCCESS_STATUSES:
        return 0
    return 1


def _emit_json(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True))


def _emit_run(meta: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        phase, kind, detail = _failure_fields(meta)
        payload = {
            "type": "run",
            "status": meta.get("status"),
            "task": meta.get("task"),
            "started_at": meta.get("started_at"),
            "finished_at": meta.get("finished_at"),
            "duration_seconds": meta.get("duration_seconds"),
            "failure_phase": phase,
            "failure_kind": kind,
            "failure_detail": detail,
        }
        _emit_json({key: value for key, value in payload.items() if value is not None})
        return
    _line("status", meta.get("status"))
    _line("task", meta.get("task"))
    _print_failure(meta)


def _emit_plan(plan_payload: dict[str, Any], *, json_output: bool) -> None:
    assignments = plan_payload.get("assignments")
    if not isinstance(assignments, list):
        return
    if json_output:
        _emit_json({"type": "plan", "assignments": assignments})
        return
    print("plan:")
    if not assignments:
        print("  (no worker assignments)")
        return
    for assignment in assignments:
        if not isinstance(assignment, dict):
            continue
        stage = assignment.get("stage", 1)
        print(f"  stage {stage} -> {assignment.get('worker', 'unknown')}: {assignment.get('task', '')}")


def _event_item_type(event: dict[str, Any]) -> str:
    params = event.get("params")
    if not isinstance(params, dict):
        return ""
    item = params.get("item")
    if isinstance(item, dict) and isinstance(item.get("type"), str):
        return item["type"]
    turn = params.get("turn")
    if isinstance(turn, dict):
        return "turn"
    return ""


def _emit_event(worker: str, event: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        _emit_json({"type": "event", "worker": worker, "event": event})
        return
    method = event.get("method", "unknown")
    item_type = _event_item_type(event)
    suffix = f" {item_type}" if item_type else ""
    print(f"event: {worker} {method}{suffix}")


def _emit_workers(worker_results: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        _emit_json({"type": "workers", "results": worker_results.get("results") or []})
        return
    _print_workers(worker_results)


def _emit_synthesis(synthesis: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        _emit_json(
            {
                "type": "synthesis",
                "orchestrator": synthesis.get("orchestrator"),
                "result": synthesis.get("result"),
            }
        )
        return
    _print_synthesis(synthesis)


def _emit_final(final_text: str, *, json_output: bool) -> None:
    if json_output:
        _emit_json({"type": "final", "text": final_text})
        return
    _print_final(final_text)


def _emit_summary(run_dir: Path, meta: dict[str, Any], *, json_output: bool) -> None:
    status = str(meta.get("status") or "unknown")
    duration = meta.get("duration_seconds")
    if json_output:
        payload: dict[str, object] = {"type": "summary", "run": str(run_dir), "status": status}
        if isinstance(duration, (int, float)):
            payload["duration_seconds"] = duration
        phase, _, _ = _failure_fields(meta)
        if phase == "stale-lock-recovery":
            recovery_status = _lock_recovery_status(run_dir, meta)
            payload["failure_phase"] = phase
            payload["inspect_command"] = f"brigade runs show {run_dir}"
            payload["recover_status"] = recovery_status
            payload["resume_available"] = _resume_available(run_dir)
        _emit_json(payload)
        return
    print(f"summary: {status} in {_duration_text(duration)}")
    _print_terminal_guidance(run_dir, meta)


def _tail_events(run_dir: Path, offsets: dict[Path, int], *, json_output: bool) -> None:
    events_dir = run_dir / "events"
    if not events_dir.is_dir():
        return
    for path in sorted(events_dir.glob("*.jsonl")):
        try:
            size = path.stat().st_size
        except OSError:
            continue
        offset = offsets.get(path, 0)
        if size < offset:
            offset = 0
        try:
            with path.open() as fh:
                fh.seek(offset)
                for line in fh:
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        event = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(event, dict):
                        _emit_event(path.stem, event, json_output=json_output)
                offsets[path] = fh.tell()
        except OSError:
            continue


def _poll_watch_artifacts(
    run_dir: Path,
    signatures: dict[str, str],
    event_offsets: dict[Path, int],
    *,
    json_output: bool,
) -> tuple[dict[str, Any] | None, int | None]:
    try:
        run_meta = _read_json(run_dir / "run.json")
        if run_meta is None:
            print(f"error: run.json not found in {run_dir}", file=sys.stderr)
            return None, 2
        run_sig = _artifact_signature(run_meta)
        if signatures.get("run") != run_sig:
            _emit_run(run_meta, json_output=json_output)
            signatures["run"] = run_sig

        plan = _read_json(run_dir / "plan.json")
        if plan is not None:
            plan_sig = _artifact_signature(plan)
            if signatures.get("plan") != plan_sig:
                _emit_plan(plan, json_output=json_output)
                signatures["plan"] = plan_sig

        _tail_events(run_dir, event_offsets, json_output=json_output)

        worker_results = _read_json(run_dir / "worker-results.json")
        if worker_results is not None:
            workers_sig = _artifact_signature(worker_results)
            if signatures.get("workers") != workers_sig:
                _emit_workers(worker_results, json_output=json_output)
                signatures["workers"] = workers_sig

        synthesis = _read_json(run_dir / "synthesis.json")
        if synthesis is not None:
            synthesis_sig = _artifact_signature(synthesis)
            if signatures.get("synthesis") != synthesis_sig:
                _emit_synthesis(synthesis, json_output=json_output)
                signatures["synthesis"] = synthesis_sig

        final_text = _read_text(run_dir / "final.txt")
        if final_text is not None and signatures.get("final") != final_text:
            _emit_final(final_text, json_output=json_output)
            signatures["final"] = final_text
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return None, 2
    return run_meta, None


def _short(text: object, limit: int = 72) -> str:
    rendered = " ".join(str(text or "").split())
    if len(rendered) <= limit:
        return rendered
    return rendered[: limit - 3].rstrip() + "..."


def _run_sort_key(item: tuple[Path, dict[str, Any]]) -> str:
    path, meta = item
    value = meta.get("started_at")
    return str(value) if value else path.name


def _collect_runs(root: Path) -> tuple[list[tuple[Path, dict[str, Any]]], int]:
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
    return runs, skipped


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

    runs, skipped = _collect_runs(root)
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


def show_latest(*, cwd: Path, runs_dir: Path | None = None) -> int:
    cwd = cwd.expanduser().resolve()
    if not cwd.is_dir():
        print(f"error: --cwd is not a directory: {cwd}", file=sys.stderr)
        return 2
    root = runs_dir.expanduser() if runs_dir is not None else cwd / ".brigade" / "runs"
    if not root.is_dir():
        print(f"error: runs directory not found: {root}", file=sys.stderr)
        return 2

    runs, skipped = _collect_runs(root)
    if skipped:
        print(f"skipped {skipped} invalid run director{'y' if skipped == 1 else 'ies'}", file=sys.stderr)
    if not runs:
        print(f"error: no runs found in {root}", file=sys.stderr)
        return 1
    return show(runs[0][0])


def _resume_available(run_dir: Path) -> bool:
    try:
        worker_results = _read_json(run_dir / "worker-results.json")
    except ValueError:
        return False
    results = worker_results.get("results") if worker_results else None
    if not isinstance(results, list):
        return False
    return any(
        isinstance(result, dict)
        and isinstance(result.get("thread_id"), str)
        and bool(result["thread_id"])
        and not result.get("ok")
        and result.get("status") in {"interrupted", "failed"}
        for result in results
    )


def _print_recovery_guidance(run_dir: Path) -> None:
    if _resume_available(run_dir):
        print(f"resume: brigade runs resume {run_dir}")
    else:
        print("resume: unavailable (no resumable app-server worker thread)")


def _lock_workspace(run_dir: Path, run_meta: dict[str, Any], *, fallback: Path | None = None) -> Path | None:
    from . import runguard

    return runguard.resolve_run_lock_workspace(run_meta, run_dir, fallback=fallback)


def _lock_recovery_status(run_dir: Path, run_meta: dict[str, Any]) -> str:
    workspace = _lock_workspace(run_dir, run_meta)
    if workspace is None:
        return "unknown"
    from . import runguard

    return runguard.run_recovery_status(workspace, run_dir)


def _print_terminal_guidance(run_dir: Path, run_meta: dict[str, Any]) -> None:
    phase, _, _ = _failure_fields(run_meta)
    if phase != "stale-lock-recovery":
        return
    print(f"inspect: brigade runs show {run_dir}")
    recovery_status = _lock_recovery_status(run_dir, run_meta)
    if recovery_status == "cleared":
        print("recover: completed (stale lock cleared)")
    elif recovery_status == "required":
        print("recover: required (stale lock remains)")
    else:
        print("recover: unknown (workspace or lock metadata unavailable)")
    _print_recovery_guidance(run_dir)


def recover(run: str | Path, *, cwd: Path, runs_dir: Path | None = None) -> int:
    from . import runguard

    run_dir, error = _resolve_run_dir(run, cwd=cwd, runs_dir=runs_dir)
    if error is not None:
        print(error, file=sys.stderr)
        return 2
    assert run_dir is not None
    recovered_unreadable_artifact = False
    read_error: str | None = None
    try:
        run_meta = _read_json(run_dir / "run.json")
    except ValueError as exc:
        run_meta = None
        read_error = str(exc)
    else:
        read_error = f"run.json not found in {run_dir}" if run_meta is None else None
    if run_meta is None:
        workspace = _lock_workspace(run_dir, {}, fallback=cwd)
        assert workspace is not None
        try:
            runguard.recover_stale_run(workspace, run_dir)
        except runguard.RunLockError as exc:
            detail = read_error if str(exc).startswith("run lock not found for run:") else str(exc)
            print(f"error: {detail}", file=sys.stderr)
            return 2
        try:
            run_meta = _read_json(run_dir / "run.json")
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if run_meta is None:
            print(f"error: run.json not found in {run_dir} after recovery", file=sys.stderr)
            return 2
        recovered_unreadable_artifact = True
    if recovered_unreadable_artifact:
        print(f"recovered: {run_dir}")
        _print_recovery_guidance(run_dir)
        return 0
    if _is_terminal(run_meta):
        phase, _, _ = _failure_fields(run_meta)
        if phase == "stale-lock-recovery":
            workspace = _lock_workspace(run_dir, run_meta, fallback=cwd)
            if workspace is None:
                print(f"error: recovered run artifact has no workspace cwd: {run_dir}", file=sys.stderr)
                return 2
            try:
                runguard.recover_stale_run(workspace, run_dir, required=False)
            except runguard.RunLockError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        print(f"already terminal: {run_dir} [{run_meta.get('status', 'unknown')}]")
        _print_recovery_guidance(run_dir)
        return 0
    workspace = _lock_workspace(run_dir, run_meta, fallback=cwd)
    if workspace is None:
        print(f"error: run artifact has no workspace cwd: {run_dir}", file=sys.stderr)
        return 2
    try:
        runguard.recover_stale_run(workspace, run_dir)
    except runguard.RunLockError as exc:
        if str(exc).startswith("run lock not found for run:"):
            try:
                concurrent_meta = _read_json(run_dir / "run.json")
            except ValueError:
                concurrent_meta = None
            if concurrent_meta is not None and _is_terminal(concurrent_meta):
                print(f"already terminal: {run_dir} [{concurrent_meta.get('status', 'unknown')}]")
                _print_recovery_guidance(run_dir)
                return 0
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"recovered: {run_dir}")
    _print_recovery_guidance(run_dir)
    return 0


def watch(
    run: str | Path,
    *,
    cwd: Path,
    runs_dir: Path | None = None,
    json_output: bool = False,
    interval: float = 1.0,
) -> int:
    if interval < 0:
        print("error: --interval must be non-negative", file=sys.stderr)
        return 2
    run_dir, error = _resolve_run_dir(run, cwd=cwd, runs_dir=runs_dir)
    if error is not None:
        print(error, file=sys.stderr)
        return 2
    assert run_dir is not None
    if json_output:
        _emit_json({"type": "watch", "run": str(run_dir)})
    else:
        print(f"watching: {run_dir}")

    signatures: dict[str, str] = {}
    event_offsets: dict[Path, int] = {}
    summary_emitted = False
    while True:
        run_meta, rc = _poll_watch_artifacts(
            run_dir,
            signatures,
            event_offsets,
            json_output=json_output,
        )
        if rc is not None:
            return rc
        assert run_meta is not None
        if run_meta.get("status") == "artifact-collection":
            workspace = _lock_workspace(run_dir, run_meta, fallback=cwd)
            if workspace is not None:
                from . import runguard

                try:
                    if runguard.recover_stale_run(workspace, run_dir, required=False):
                        continue
                    refreshed = _read_json(run_dir / "run.json")
                    if refreshed is not None and _is_terminal(refreshed):
                        continue
                    print(
                        "error: artifact-collection run has no matching recoverable lock",
                        file=sys.stderr,
                    )
                    return 2
                except runguard.RunLockError as exc:
                    detail = str(exc)
                    if "owner process is still active" not in detail and "recovery is still active" not in detail:
                        print(f"error: artifact-collection recovery failed: {detail}", file=sys.stderr)
                        return 2
        if _is_terminal(run_meta):
            if not summary_emitted:
                _emit_summary(run_dir, run_meta, json_output=json_output)
                summary_emitted = True
            return _watch_return_code(run_meta.get("status"))
        time.sleep(interval)


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
    _print_failure(run_meta)
    if run_meta.get("suspected_noop") is True:
        print("warning: suspected no-op run; ok workers produced no non-.brigade file changes.")

    _print_roster(roster)
    _print_plan(plan)
    _print_workers(worker_results)
    _print_ground_truth(worker_results)
    _print_synthesis(synthesis)
    _print_final(_read_text(run_dir / "final.txt"))
    _print_terminal_guidance(run_dir, run_meta)
    if _is_terminal(run_meta):
        return _watch_return_code(run_meta.get("status"))
    return 0
