"""Deterministic, resumable model trials over Brigade direct-worker runs."""

from __future__ import annotations

import hashlib
import json
import re
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import aboyeur, localio, runguard
from .roster import Roster

MANIFEST_SCHEMA = "brigade.eval_manifest.v1"
CELL_SCHEMA = "brigade.eval_cell.v1"
SUMMARY_SCHEMA = "brigade.eval_summary.v1"
GRADER_SCHEMA = "brigade.grader_result.v1"
TERMINAL_STATES = frozenset({"accepted", "rejected", "unscored", "execution_error", "adapter_error", "grader_error"})
REGRADEABLE_STATES = frozenset({"grader_error"})
MEASUREMENT_STATES = frozenset({"adapter_error", "grader_error"})


@dataclass(frozen=True)
class CellSpec:
    cell_id: str
    coordinate: str
    case_id: str
    prompt: str
    seat: str
    trial: int
    graders: tuple[dict[str, Any], ...]
    execution_mode: str

    def payload(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "coordinate": self.coordinate,
            "case_id": self.case_id,
            "prompt": self.prompt,
            "seat": self.seat,
            "trial": self.trial,
            "graders": list(self.graders),
            "execution_mode": self.execution_mode,
        }


def _canonical_digest(value: object) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def load_manifest(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    try:
        data = json.loads(path.read_text())
    except OSError as exc:
        raise ValueError(f"manifest unreadable: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    if data.get("schema") != MANIFEST_SCHEMA:
        raise ValueError(f"manifest schema must be {MANIFEST_SCHEMA}")
    if not isinstance(data.get("name"), str) or not data["name"].strip():
        raise ValueError("manifest name must be a non-empty string")
    if not isinstance(data.get("cases"), list) or not data["cases"]:
        raise ValueError("manifest cases must be a non-empty list")
    if not isinstance(data.get("seats"), list) or not data["seats"]:
        raise ValueError("manifest seats must be a non-empty list")
    return data


def _normalize_prompt(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _case_prompt(case: dict[str, Any], base_dir: Path) -> str:
    prompt = case.get("prompt")
    prompt_file = case.get("prompt_file")
    if isinstance(prompt, str) and prompt:
        return _normalize_prompt(prompt)
    if isinstance(prompt_file, str) and prompt_file:
        candidate = (base_dir / prompt_file).resolve()
        if base_dir.resolve() not in candidate.parents and candidate != base_dir.resolve():
            raise ValueError(f"case {case.get('id')!r} prompt_file escapes the manifest directory")
        try:
            return _normalize_prompt(candidate.read_text())
        except OSError as exc:
            raise ValueError(f"case {case.get('id')!r} prompt_file unreadable: {exc}") from exc
    raise ValueError(f"case {case.get('id')!r} needs prompt or prompt_file")


def expand_cells(manifest: dict[str, Any], roster: Roster, *, base_dir: Path = Path(".")) -> list[CellSpec]:
    defaults = manifest.get("graders", [])
    if not isinstance(defaults, list) or not all(isinstance(item, dict) for item in defaults):
        raise ValueError("manifest graders must be a list of objects")
    default_trials = manifest.get("trials", 1)
    if not isinstance(default_trials, int) or isinstance(default_trials, bool) or default_trials < 1:
        raise ValueError("manifest trials must be a positive integer")
    execution = manifest.get("execution", {"mode": "read-only"})
    if not isinstance(execution, dict):
        raise ValueError("manifest execution must be an object")
    execution_mode = execution.get("mode", "read-only")
    if execution_mode not in {"read-only", "writable-worktree"}:
        raise ValueError("manifest execution.mode must be read-only or writable-worktree")
    cells: list[CellSpec] = []
    seen_cases: set[str] = set()
    for raw_case in manifest["cases"]:
        if not isinstance(raw_case, dict):
            raise ValueError("each case must be an object")
        case_id = raw_case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError("each case needs a non-empty id")
        if case_id in seen_cases:
            raise ValueError(f"duplicate case id: {case_id}")
        seen_cases.add(case_id)
        prompt = _case_prompt(raw_case, base_dir)
        trials = raw_case.get("trials", default_trials)
        if not isinstance(trials, int) or isinstance(trials, bool) or trials < 1:
            raise ValueError(f"case {case_id!r} trials must be a positive integer")
        graders = raw_case.get("graders", defaults)
        if not isinstance(graders, list) or not all(isinstance(item, dict) for item in graders):
            raise ValueError(f"case {case_id!r} graders must be a list of objects")
        for seat in manifest["seats"]:
            if not isinstance(seat, str) or seat not in roster.agents:
                raise ValueError(f"unknown roster seat: {seat!r}")
            agent = roster.agents[seat]
            if seat == roster.orchestrator or agent.cli is None:
                raise ValueError(f"trial seat must be a CLI worker, not orchestrator or endpoint: {seat}")
            seat_spec = {
                "seat": seat,
                "cli": agent.cli,
                "model": agent.model,
                "reasoning": agent.reasoning,
                "transport": getattr(agent, "transport", None),
                "transport_version": getattr(agent, "transport_version", None),
                "env": dict(agent.env) if agent.env else None,
                "codex_transport": roster.codex_transport if agent.cli == "codex" else None,
            }
            for trial in range(1, trials + 1):
                coordinate = f"{case_id}:{seat}:{trial}"
                identity = {
                    "schema": CELL_SCHEMA,
                    "case": {"id": case_id, "prompt": prompt},
                    "seat": seat_spec,
                    "trial": trial,
                    "graders": graders,
                    "execution_mode": execution_mode,
                }
                cells.append(
                    CellSpec(
                        cell_id=_canonical_digest(identity),
                        coordinate=coordinate,
                        case_id=case_id,
                        prompt=prompt,
                        seat=seat,
                        trial=trial,
                        graders=tuple(dict(item) for item in graders),
                        execution_mode=execution_mode,
                    )
                )
    return cells


def _grader_result(kind: str, index: int, started: float, *, status: str, score: float | None, detail: str) -> dict:
    return {
        "schema": GRADER_SCHEMA,
        "grader_id": f"{kind}:{index}",
        "grader_type": kind,
        "version": 1,
        "status": status,
        "score": score,
        "score_min": 0.0,
        "score_max": 1.0,
        "detail": detail,
        "component_checks": ([{"name": kind, "passed": score == 1.0, "detail": detail}] if status == "scored" else []),
        "duration_seconds": max(0.0, round(time.monotonic() - started, 6)),
    }


def _json_field(value: Any, path: str) -> Any:
    current = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise KeyError(path)
        current = current[part]
    return current


def _changed_files(run_dir: Path) -> list[str]:
    try:
        data = json.loads((run_dir / "worker-results.json").read_text())
    except (OSError, json.JSONDecodeError):
        return []
    ground_truth = data.get("ground_truth") if isinstance(data, dict) else None
    if not isinstance(ground_truth, dict):
        return []
    values = [ground_truth.get("changed_files"), ground_truth.get("untracked_files")]
    return sorted({item for group in values if isinstance(group, list) for item in group if isinstance(item, str)})


def grade_output(
    *,
    graders: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    text: str,
    exit_code: int,
    workspace: Path,
    run_dir: Path,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, grader in enumerate(graders, start=1):
        started = time.monotonic()
        kind = grader.get("type")
        if not isinstance(kind, str):
            results.append(
                _grader_result(
                    "unknown", index, started, status="grader_error", score=None, detail="missing grader type"
                )
            )
            continue
        try:
            passed = False
            detail = ""
            if kind == "exit_status":
                expected = grader.get("expected", 0)
                if not isinstance(expected, int):
                    raise ValueError("expected must be an integer")
                passed = exit_code == expected
                detail = f"exit {exit_code}, expected {expected}"
            elif kind == "exact_output":
                expected = grader.get("expected")
                if not isinstance(expected, str):
                    raise ValueError("expected must be a string")
                passed = text.rstrip("\n") == expected.rstrip("\n")
                detail = "output matched" if passed else "output did not match"
            elif kind == "regex_output":
                pattern = grader.get("pattern")
                if not isinstance(pattern, str):
                    raise ValueError("pattern must be a string")
                passed = re.search(pattern, text, re.MULTILINE) is not None
                detail = "pattern matched" if passed else "pattern did not match"
            elif kind == "json_field":
                path = grader.get("path")
                if not isinstance(path, str):
                    raise ValueError("path must be a string")
                actual = _json_field(json.loads(text), path)
                expected = grader.get("expected")
                passed = actual == expected
                detail = f"{path} matched" if passed else f"{path} did not match"
            elif kind == "file_exists":
                rel = grader.get("path")
                if not isinstance(rel, str) or Path(rel).is_absolute() or ".." in Path(rel).parts:
                    raise ValueError("path must be workspace-relative")
                passed = (workspace / rel).exists()
                detail = f"{rel} exists" if passed else f"{rel} missing"
            elif kind == "diff_constraints":
                changed = _changed_files(run_dir)
                forbidden = grader.get("forbidden", [])
                allowed = grader.get("allowed", [])
                if not isinstance(forbidden, list) or not isinstance(allowed, list):
                    raise ValueError("allowed and forbidden must be lists")
                forbidden_hits = [path for path in changed if any(re.fullmatch(str(p), path) for p in forbidden)]
                outside = [path for path in changed if allowed and not any(re.fullmatch(str(p), path) for p in allowed)]
                passed = not forbidden_hits and not outside
                detail = f"changed={changed}; forbidden={forbidden_hits}; outside_allowed={outside}"
            elif kind == "verify_receipt":
                rel = grader.get("path")
                if not isinstance(rel, str) or Path(rel).is_absolute() or ".." in Path(rel).parts:
                    raise ValueError("path must be workspace-relative")
                receipt = json.loads((workspace / rel).read_text())
                passed = isinstance(receipt, dict) and receipt.get("status") == "completed"
                detail = "verification completed" if passed else "verification not completed"
            else:
                raise ValueError(f"unknown grader type: {kind}")
            results.append(
                _grader_result(kind, index, started, status="scored", score=1.0 if passed else 0.0, detail=detail)
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError, re.error) as exc:
            results.append(
                _grader_result(kind, index, started, status="grader_error", score=None, detail=str(exc)[:200])
            )
    return results


def _worker_result(run_dir: Path) -> dict[str, Any] | None:
    payload = _load_json(run_dir / "worker-results.json")
    if payload is None:
        return None
    results = payload.get("results")
    if not isinstance(results, list) or not results or not isinstance(results[0], dict):
        return None
    return results[0]


def _state(
    exit_code: int,
    graders: list[dict[str, Any]],
    worker_result: dict[str, Any] | None = None,
    *,
    run_meta: dict[str, Any] | None = None,
) -> str:
    if exit_code != 0:
        if isinstance(run_meta, dict) and run_meta.get("failure_kind") == "timeout":
            return "execution_error"
        if worker_result is not None and worker_result.get("timed_out") is True:
            return "execution_error"
        if worker_result is not None and worker_result.get("ok") is False:
            adapter_exit = worker_result.get("exit_code")
            if adapter_exit is None or adapter_exit == 0:
                return "adapter_error"
        return "execution_error"
    if not graders:
        return "unscored"
    if any(item["status"] == "grader_error" for item in graders):
        return "grader_error"
    return "accepted" if all(item["score"] == item["score_max"] for item in graders) else "rejected"


def _resume_skip_state(state: str) -> bool:
    return state in TERMINAL_STATES and state not in REGRADEABLE_STATES


def _output_digest(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode()).hexdigest()}"


def _failure_reason(
    *,
    run_meta: dict[str, Any] | None,
    worker_result: dict[str, Any] | None,
    state: str,
) -> str | None:
    if isinstance(run_meta, dict) and run_meta.get("failure_kind") == "timeout":
        return "timeout"
    if isinstance(worker_result, dict) and worker_result.get("timed_out") is True:
        return "timeout"
    if state != "adapter_error":
        return None
    if not isinstance(worker_result, dict):
        return "transport_drop"
    failure_kind = str(worker_result.get("failure_kind") or "").lower()
    detail = str(worker_result.get("detail") or "").lower()
    if "5xx" in failure_kind or "5xx" in detail or "server error" in detail:
        return "provider_5xx"
    return "transport_drop"


def _cell_is_measurement_failure(cell: dict[str, Any]) -> bool:
    state = cell.get("state")
    if state in MEASUREMENT_STATES:
        return True
    return state == "execution_error" and cell.get("failure_reason") == "timeout"


def _attach_grader_output_refs(graders: list[dict[str, Any]], *, cell_id: str, output_digest: str) -> None:
    for grader in graders:
        grader["cell_id"] = cell_id
        grader["output_digest"] = output_digest
        grader["output_refs"] = [{"path": "run/final.txt", "sha256": output_digest}]


def _finalize_cell_payload(
    cell: CellSpec,
    *,
    plan: dict[str, Any],
    attempt: int,
    started_at: str,
    exit_code: int,
    run_dir: Path,
    graders: list[dict[str, Any]],
    text: str,
    failure_reason: str | None,
) -> dict[str, Any]:
    output_digest = _output_digest(text)
    _attach_grader_output_refs(graders, cell_id=cell.cell_id, output_digest=output_digest)
    run_meta = _load_json(run_dir / "run.json") or {}
    worker_result = _worker_result(run_dir)
    state = _state(exit_code, graders, worker_result, run_meta=run_meta)
    if failure_reason is None:
        failure_reason = _failure_reason(run_meta=run_meta, worker_result=worker_result, state=state)
    payload: dict[str, Any] = {
        "schema": CELL_SCHEMA,
        **cell.payload(),
        "state": state,
        "attempt": attempt,
        "started_at": started_at,
        "manifest_digest": plan["manifest_digest"],
        "exit_code": exit_code,
        "duration_seconds": run_meta.get("duration_seconds"),
        "run_dir": str(run_dir),
        "graders": graders,
        "acceptance": {"state": "accepted" if state == "accepted" else "not-accepted"},
    }
    if failure_reason is not None:
        payload["failure_reason"] = failure_reason
    return payload


def _write_cell_payload(cell_dir: Path, run_parent: Path, payload: dict[str, Any]) -> None:
    cell_dir.mkdir(parents=True, exist_ok=True)
    localio.write_json(cell_dir / "cell.json", payload)
    localio.write_json(run_parent / "cell.json", payload)


def _expected_output_digest(cell: dict[str, Any]) -> str | None:
    graders = cell.get("graders")
    if not isinstance(graders, list):
        return None
    for grader in graders:
        if isinstance(grader, dict) and isinstance(grader.get("output_digest"), str):
            return grader["output_digest"]
    return None


def _regrade_cell_from_disk(cell_dir: Path, cell: CellSpec, plan: dict[str, Any]) -> bool:
    current = _load_json(cell_dir / "cell.json")
    if current is None:
        return False
    run_dir_value = current.get("run_dir")
    if not isinstance(run_dir_value, str) or not run_dir_value:
        return False
    run_dir = Path(run_dir_value)
    try:
        text = (run_dir / "final.txt").read_text()
    except OSError:
        return False
    output_digest = _output_digest(text)
    expected = _expected_output_digest(current)
    if expected is not None and expected != output_digest:
        raise ValueError(f"cell {cell.cell_id}: stored output digest mismatch for {run_dir / 'final.txt'}")
    exit_code = current.get("exit_code")
    if not isinstance(exit_code, int) or isinstance(exit_code, bool):
        exit_code = 0
    run_meta = _load_json(run_dir / "run.json") or {}
    cwd_value = run_meta.get("cwd")
    workspace = Path(cwd_value).expanduser().resolve() if isinstance(cwd_value, str) else run_dir
    graders = grade_output(
        graders=cell.graders,
        text=text,
        exit_code=exit_code,
        workspace=workspace,
        run_dir=run_dir,
    )
    attempt = current.get("attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
        attempt = 1
    started_at = current.get("started_at")
    if not isinstance(started_at, str):
        started_at = datetime.now(timezone.utc).isoformat()
    payload = _finalize_cell_payload(
        cell,
        plan=plan,
        attempt=attempt,
        started_at=started_at,
        exit_code=exit_code,
        run_dir=run_dir,
        graders=graders,
        text=text,
        failure_reason=None,
    )
    _write_cell_payload(cell_dir, run_dir.parent, payload)
    return True


def regrade(output_dir: Path) -> int:
    output_dir = output_dir.expanduser().resolve()
    plan = _load_json(output_dir / "plan.json")
    if plan is None:
        print("error: no trial plan found", file=sys.stderr)
        return 2
    current_ids = {
        item["cell_id"]
        for item in plan.get("cells", [])
        if isinstance(item, dict) and isinstance(item.get("cell_id"), str)
    }
    cells_by_id = {cell.cell_id: cell for cell in _cells_from_plan(plan)}
    regraded = 0
    for cell_id in sorted(current_ids):
        cell = cells_by_id.get(cell_id)
        if cell is None:
            continue
        cell_dir = output_dir / "cells" / cell_id
        try:
            if _regrade_cell_from_disk(cell_dir, cell, plan):
                regraded += 1
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    localio.write_json(output_dir / "summary.json", summarize(output_dir))
    return process_exit(summarize(output_dir))


def _cells_from_plan(plan: dict[str, Any]) -> list[CellSpec]:
    cells: list[CellSpec] = []
    for raw in plan.get("cells", []):
        if not isinstance(raw, dict):
            continue
        graders = raw.get("graders", [])
        if not isinstance(graders, list):
            graders = []
        cells.append(
            CellSpec(
                cell_id=str(raw["cell_id"]),
                coordinate=str(raw.get("coordinate", "")),
                case_id=str(raw.get("case_id", "")),
                prompt=str(raw.get("prompt", "")),
                seat=str(raw.get("seat", "")),
                trial=int(raw.get("trial", 1)),
                graders=tuple(dict(item) for item in graders if isinstance(item, dict)),
                execution_mode=str(raw.get("execution_mode", "read-only")),
            )
        )
    return cells


def project_cell(cell: dict[str, Any], *, base_dir: Path) -> dict[str, Any]:
    projected = dict(cell)
    prompt = projected.pop("prompt", None)
    if isinstance(prompt, str):
        projected["prompt_digest"] = _output_digest(prompt)
    case = projected.get("case")
    if isinstance(case, dict) and isinstance(case.get("prompt"), str):
        case = dict(case)
        case["prompt_digest"] = _output_digest(case["prompt"])
        case.pop("prompt", None)
        projected["case"] = case
    run_dir = projected.get("run_dir")
    if isinstance(run_dir, str):
        try:
            projected["run_dir"] = str(Path(run_dir).resolve().relative_to(base_dir.resolve()))
        except ValueError:
            projected["run_dir"] = Path(run_dir).name
    return projected


def process_exit(summary: dict[str, Any]) -> int:
    if int(summary.get("measurement_failures", 0) or 0) > 0:
        return 3
    counts = summary.get("counts")
    if not isinstance(counts, dict):
        return 0
    if counts.get("rejected", 0) > 0 or counts.get("execution_error", 0) > 0:
        return 1
    return 0


def _trial_worktree_path(
    workspace: Path,
    output_dir: Path,
    cell: CellSpec,
    attempt: int,
) -> Path:
    experiment = hashlib.sha256(str(output_dir).encode()).hexdigest()[:12]
    return (
        Path.home()
        / ".cache"
        / "brigade"
        / "worktrees"
        / f"eval-{workspace.name}-{experiment}-{cell.cell_id[:12]}-{attempt:03d}"
    )


_ATTEMPT_DIR = re.compile(r"attempt-(\d+)")


def _attempt_number(cell_dir: Path) -> int:
    attempts = cell_dir / "attempts"
    numbers = (
        [
            int(match.group(1))
            for entry in attempts.iterdir()
            if entry.is_dir() and (match := _ATTEMPT_DIR.fullmatch(entry.name)) is not None
        ]
        if attempts.is_dir()
        else []
    )
    recorded = _load_json(cell_dir / "cell.json") or {}
    prior = recorded.get("attempt")
    if isinstance(prior, int) and not isinstance(prior, bool) and prior > 0:
        numbers.append(prior)
    return max(numbers, default=0) + 1


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def build_plan(manifest_path: Path, roster: Roster, output_dir: Path) -> tuple[dict[str, Any], list[CellSpec]]:
    manifest = load_manifest(manifest_path)
    cells = expand_cells(manifest, roster, base_dir=manifest_path.expanduser().resolve().parent)
    prior = _load_json(output_dir / "plan.json") or {}
    prior_by_coordinate = {
        item.get("coordinate"): item.get("cell_id")
        for item in prior.get("cells", [])
        if isinstance(item, dict) and isinstance(item.get("coordinate"), str)
    }
    stale = [
        {
            "coordinate": cell.coordinate,
            "previous_cell_id": prior_by_coordinate[cell.coordinate],
            "cell_id": cell.cell_id,
        }
        for cell in cells
        if cell.coordinate in prior_by_coordinate and prior_by_coordinate[cell.coordinate] != cell.cell_id
    ]
    payload = {
        "schema": MANIFEST_SCHEMA,
        "name": manifest["name"],
        "manifest_digest": _canonical_digest(manifest),
        "cells": [cell.payload() for cell in cells],
        "stale_cells": stale,
    }
    return payload, cells


def execute(
    manifest_path: Path,
    roster: Roster,
    *,
    workspace: Path,
    output_dir: Path,
    resume: bool,
) -> int:
    workspace = workspace.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        plan, cells = build_plan(manifest_path, roster, output_dir)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    writable = any(cell.execution_mode == "writable-worktree" for cell in cells)
    if writable and not runguard.is_git_worktree(workspace):
        print("error: writable-worktree trials require a git worktree target", file=sys.stderr)
        return 2
    localio.write_json(output_dir / "plan.json", plan)
    if resume and plan["stale_cells"]:
        print(
            f"note: {len(plan['stale_cells'])} stale cell(s) from the previous plan kept and counted in summary",
            file=sys.stderr,
        )
    for cell in cells:
        cell_dir = output_dir / "cells" / cell.cell_id
        current = _load_json(cell_dir / "cell.json")
        if resume and current is not None and _resume_skip_state(str(current.get("state", ""))):
            continue
        if resume and current is not None and current.get("state") in REGRADEABLE_STATES:
            try:
                _regrade_cell_from_disk(cell_dir, cell, plan)
            except ValueError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            continue
        attempt = _attempt_number(cell_dir)
        if isinstance(current, dict) and isinstance(current.get("started_at"), str):
            started_at = current["started_at"]
        else:
            started_at = datetime.now(timezone.utc).isoformat()
        cell_dir.mkdir(parents=True, exist_ok=True)
        localio.write_json(
            cell_dir / "cell.json",
            {
                "schema": CELL_SCHEMA,
                **cell.payload(),
                "state": "running",
                "attempt": attempt,
                "started_at": started_at,
                "manifest_digest": plan["manifest_digest"],
            },
        )
        run_dir = cell_dir / "attempts" / f"attempt-{attempt:03d}" / "run"
        cell_workspace = workspace
        worktree_path: Path | None = None
        if cell.execution_mode == "writable-worktree":
            worktree_path = _trial_worktree_path(workspace, output_dir, cell, attempt)
            try:
                cell_workspace = runguard.create_detached_worktree(workspace, worktree_path)
            except runguard.RunGuardError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
        try:
            rc = aboyeur.run(
                cell.prompt,
                roster,
                worker=cell.seat,
                cwd=cell_workspace,
                output_dir=run_dir,
                route_enabled=False,
                read_only=cell.execution_mode == "read-only",
                authorized_writable_worktree=cell.execution_mode == "writable-worktree",
            )
            try:
                text = (run_dir / "final.txt").read_text()
            except OSError:
                text = ""
            graders = grade_output(
                graders=cell.graders,
                text=text,
                exit_code=rc,
                workspace=cell_workspace,
                run_dir=run_dir,
            )
        finally:
            if worktree_path is not None:
                runguard.remove_worktree(workspace, worktree_path)
        payload = _finalize_cell_payload(
            cell,
            plan=plan,
            attempt=attempt,
            started_at=started_at,
            exit_code=rc,
            run_dir=run_dir,
            graders=graders,
            text=text,
            failure_reason=None,
        )
        _write_cell_payload(cell_dir, run_dir.parent, payload)
    summary = summarize(output_dir)
    localio.write_json(output_dir / "summary.json", summary)
    return process_exit(summary)


def _stats(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"values": [], "count": 0, "mean": None, "median": None, "min": None, "max": None, "stdev": None}
    return {
        "values": values,
        "count": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "stdev": statistics.pstdev(values),
    }


def summarize(output_dir: Path) -> dict[str, Any]:
    counts: dict[str, int] = {}
    stale_counts: dict[str, int] = {}
    scores: list[float] = []
    partial_scores: list[float] = []
    durations: list[float] = []
    measurement_failures = 0
    cells_dir = output_dir / "cells"
    paths = sorted(cells_dir.glob("*/cell.json")) if cells_dir.is_dir() else []
    plan = _load_json(output_dir / "plan.json")
    current_ids = {
        item["cell_id"]
        for item in (plan or {}).get("cells", [])
        if isinstance(item, dict) and isinstance(item.get("cell_id"), str)
    }
    for path in paths:
        cell = _load_json(path)
        if cell is None:
            continue
        state = str(cell.get("state", "unknown"))
        cell_id = cell.get("cell_id")
        if current_ids and cell_id not in current_ids:
            stale_counts[state] = stale_counts.get(state, 0) + 1
            continue
        counts[state] = counts.get(state, 0) + 1
        if _cell_is_measurement_failure(cell):
            measurement_failures += 1
        if isinstance(cell.get("duration_seconds"), (int, float)):
            durations.append(float(cell["duration_seconds"]))
        cell_state = cell.get("state")
        for grader in cell.get("graders", []):
            if not isinstance(grader, dict) or not isinstance(grader.get("score"), (int, float)):
                continue
            if grader.get("status") != "scored":
                continue
            value = float(grader["score"])
            if cell_state == "grader_error":
                partial_scores.append(value)
            else:
                scores.append(value)
    return {
        "schema": SUMMARY_SCHEMA,
        "counts": dict(sorted(counts.items())),
        "stale_counts": dict(sorted(stale_counts.items())),
        "measurement_failures": measurement_failures,
        "scores": _stats(scores),
        "partial_scores": _stats(partial_scores),
        "durations": _stats(durations),
    }


def show(output_dir: Path, *, json_output: bool = False) -> int:
    plan = _load_json(output_dir / "plan.json")
    if plan is None:
        print(f"error: no trial plan at {output_dir}", file=sys.stderr)
        return 2
    summary = summarize(output_dir)
    if json_output:
        print(json.dumps({"plan": plan, "summary": summary}, indent=2, sort_keys=True))
    else:
        print(f"trial: {plan.get('name')}")
        print(f"cells: {len(plan.get('cells', []))}")
        print(f"stale: {len(plan.get('stale_cells', []))}")
        for state, count in summary["counts"].items():
            print(f"{state}: {count}")
    return 0
