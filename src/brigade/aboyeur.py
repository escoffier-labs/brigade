"""Bounded cross-model orchestration for `brigade run`."""

from __future__ import annotations

import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from json import JSONDecoder
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import agents
from . import codex_appserver
from . import graphtrail_delta
from . import proc, runguard
from . import run_control
from .roster import Agent, Roster, is_cli_allowed, timeout_for, workers

CODE_GRAPH_HEADING = "## Code graph context (GraphTrail, read-only)"
CODE_GRAPH_LIMIT = 4000
DRIFT_IMPACT_HEADING = "## Upstream drift impact (Upstream Drift + GraphTrail, read-only)"
DRIFT_IMPACT_LIMIT = 4000
BRIEF_BUDGET_BYTES = 6000


@dataclass(frozen=True)
class Assignment:
    worker: str
    task: str
    stage: int = 1


@dataclass(frozen=True)
class WorkerResult:
    worker: str
    task: str
    text: str
    ok: bool
    detail: str = ""
    thread_id: str | None = None
    status: str = ""


@dataclass(frozen=True)
class CodeGraphBrief:
    attached: bool
    text: str = ""
    bytes: int = 0


@dataclass(frozen=True)
class DriftImpactBrief:
    attached: bool
    text: str = ""
    bytes: int = 0
    pending_count: int = 0


@dataclass(frozen=True)
class BriefSet:
    code_graph: CodeGraphBrief
    drift_impact: DriftImpactBrief
    budget_bytes: int
    attached: tuple[dict[str, object], ...]


def _brief_bytes(text: str) -> int:
    return len(text.encode())


def _truncate_brief_text(text: str, limit: int, label: str) -> str:
    if _brief_bytes(text) <= limit:
        return text
    note = f"\n\n[{label} brief truncated to fit the run brief budget.]\n"
    room = max(0, limit - _brief_bytes(note))
    clipped = text.encode()[:room].decode(errors="ignore")
    boundary = clipped.rfind("\n")
    if boundary > 0:
        clipped = clipped[:boundary]
    else:
        clipped = clipped.rstrip()
    return clipped.rstrip() + note


def _brief_order(task: str) -> tuple[str, ...]:
    lowered = task.lower()
    if any(word in lowered for word in ("release", "changelog", "publish", "version")):
        return ("drift_impact", "code_graph")
    if any(word in lowered for word in ("doc", "readme", "handoff", "memory", "evidence")):
        return ("drift_impact", "code_graph")
    return ("code_graph", "drift_impact")


def arbitrate_briefs(
    task: str,
    *,
    code_graph: CodeGraphBrief,
    drift_impact: DriftImpactBrief,
    budget_bytes: int = BRIEF_BUDGET_BYTES,
) -> BriefSet:
    briefs: dict[str, CodeGraphBrief | DriftImpactBrief] = {
        "code_graph": code_graph,
        "drift_impact": drift_impact,
    }
    kept_code_graph = CodeGraphBrief(attached=False)
    kept_drift = DriftImpactBrief(attached=False)
    used = 0
    attached: list[dict[str, object]] = []
    for name in _brief_order(task):
        brief = briefs[name]
        if not brief.attached or not brief.text:
            continue
        remaining = budget_bytes - used
        if remaining <= 0:
            continue
        text = brief.text
        truncated = False
        if _brief_bytes(text) > remaining:
            if remaining < 500:
                continue
            text = _truncate_brief_text(text, remaining, name.replace("_", " "))
            truncated = True
        size = _brief_bytes(text)
        used += size
        attached.append({"name": name, "bytes": size, "truncated": truncated})
        if name == "code_graph":
            kept_code_graph = CodeGraphBrief(attached=True, text=text, bytes=size)
        else:
            kept_drift = DriftImpactBrief(
                attached=True,
                text=text,
                bytes=size,
                pending_count=getattr(brief, "pending_count", 0),
            )
    return BriefSet(
        code_graph=kept_code_graph,
        drift_impact=kept_drift,
        budget_bytes=budget_bytes,
        attached=tuple(attached),
    )


def _prepend_brief(prompt: str, *, heading: str, text: str) -> str:
    if not text:
        return prompt
    if heading in prompt:
        return prompt
    return f"{text}\n{prompt}"


def _prepend_optional_briefs(
    prompt: str,
    *,
    code_graph: CodeGraphBrief | None = None,
    drift_impact: DriftImpactBrief | None = None,
) -> str:
    if code_graph is not None and code_graph.attached and code_graph.text:
        prompt = _prepend_brief(prompt, heading=CODE_GRAPH_HEADING, text=code_graph.text)
    if drift_impact is not None and drift_impact.attached and drift_impact.text:
        prompt = _prepend_brief(prompt, heading=DRIFT_IMPACT_HEADING, text=drift_impact.text)
    return prompt


def _prepend_code_graph(prompt: str, code_graph: CodeGraphBrief | None) -> str:
    if CODE_GRAPH_HEADING in prompt:
        return prompt
    return _prepend_optional_briefs(prompt, code_graph=code_graph)


def _truncate_on_line_boundary(text: str, limit: int = CODE_GRAPH_LIMIT) -> str:
    if len(text) <= limit:
        return text
    note = f"\n\n[GraphTrail context truncated to {limit} chars.]\n"
    room = max(0, limit - len(note))
    clipped = text[:room]
    boundary = clipped.rfind("\n")
    if boundary > 0:
        clipped = clipped[:boundary]
    else:
        clipped = clipped.rstrip()
    return clipped.rstrip() + note


def _graphtrail_bin() -> str | None:
    from . import context_cmd

    return context_cmd._graphtrail_bin()


def code_graph_brief(cwd: Path | None, task: str) -> CodeGraphBrief:
    if cwd is None:
        return CodeGraphBrief(attached=False)
    db_path = cwd / ".graphtrail" / "graphtrail.db"
    if not db_path.is_file():
        return CodeGraphBrief(attached=False)
    binary = _graphtrail_bin()
    if binary is None:
        return CodeGraphBrief(attached=False)
    result = proc.run(
        [binary, "--db", str(db_path), "context", task, "--markdown", "--limit", "8"],
        timeout=10.0,
        cwd=cwd,
    )
    if result.code != 0:
        return CodeGraphBrief(attached=False)
    body = result.stdout.strip()
    if not body:
        return CodeGraphBrief(attached=False)
    text = _truncate_on_line_boundary(f"{CODE_GRAPH_HEADING}\n\n{body}\n")
    return CodeGraphBrief(attached=True, text=text, bytes=len(text.encode()))


def _upstream_drift_state_path() -> Path:
    return Path(os.environ.get("UPSTREAM_DRIFT_STATE_PATH", Path.home() / ".config/upstream-drift/state.json"))


def _upstream_drift_reports_dir() -> Path:
    return Path(os.environ.get("UPSTREAM_DRIFT_REPORTS_DIR", Path.home() / "repos/upstream-drift/reports"))


def _read_json_dict(path: Path) -> dict[str, object] | None:
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _latest_drift_report(reports_dir: Path, watch: str) -> str:
    if not _safe_watch_name(watch):
        return ""
    root = reports_dir / watch
    if not root.is_dir():
        return ""
    reports = sorted(root.glob("*.md"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not reports:
        return ""
    try:
        return reports[0].read_text()
    except OSError:
        return ""


def _safe_watch_name(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9._-]+", value))


def _drift_symbol_candidates(watch: str, report: str) -> list[str]:
    candidates: list[str] = []
    for value in [watch, *re.findall(r"`([A-Za-z_][A-Za-z0-9_.:-]{2,80})`", report)]:
        for part in re.split(r"[^A-Za-z0-9_.:]+", value):
            cleaned = part.strip("._:")
            if len(cleaned) < 3:
                continue
            if cleaned not in candidates:
                candidates.append(cleaned)
            if len(candidates) >= 4:
                return candidates
    return candidates


def _drift_report_excerpt(report: str, limit: int = 700) -> str:
    lines = []
    for line in report.splitlines():
        stripped = line.strip()
        if not stripped or stripped == "---" or stripped.startswith(("watch:", "date:")):
            continue
        lines.append(stripped)
        if len(" ".join(lines)) >= limit:
            break
    text = "\n".join(lines)
    return _truncate_on_line_boundary(text, limit)


def _pending_drift_entries() -> list[dict[str, object]]:
    state = _read_json_dict(_upstream_drift_state_path())
    if state is None:
        return []
    entries: list[dict[str, object]] = []
    for name, raw in sorted(state.items()):
        if not isinstance(name, str) or not _safe_watch_name(name) or not isinstance(raw, dict):
            continue
        failures = raw.get("consecutiveFailures")
        if not isinstance(failures, int) or failures < 3:
            continue
        entries.append(
            {
                "name": name,
                "consecutive_failures": failures,
                "last_run_at": raw.get("lastRunAt") if isinstance(raw.get("lastRunAt"), str) else None,
            }
        )
    return entries


def drift_impact_brief(cwd: Path | None) -> DriftImpactBrief:
    if cwd is None:
        return DriftImpactBrief(attached=False)
    db_path = cwd / ".graphtrail" / "graphtrail.db"
    binary = _graphtrail_bin()
    if not db_path.is_file() or binary is None:
        return DriftImpactBrief(attached=False)
    pending = _pending_drift_entries()
    if not pending:
        return DriftImpactBrief(attached=False)

    reports_dir = _upstream_drift_reports_dir()
    sections = [DRIFT_IMPACT_HEADING, ""]
    for entry in pending[:3]:
        watch = str(entry["name"])
        report = _latest_drift_report(reports_dir, watch)
        sections.append(
            f"### {watch}\n"
            f"- consecutive failures: {entry['consecutive_failures']}\n"
            f"- last run: {entry.get('last_run_at') or 'unknown'}"
        )
        excerpt = _drift_report_excerpt(report)
        if excerpt:
            sections.append("Drift report excerpt:\n" + excerpt)
        for candidate in _drift_symbol_candidates(watch, report):
            result = proc.run(
                [binary, "--db", str(db_path), "impact", candidate, "--depth", "2"],
                timeout=5.0,
                cwd=cwd,
            )
            body = result.stdout.strip()
            if result.code == 0 and body:
                sections.append(f"GraphTrail impact for `{candidate}`:\n{body}")
                break

    text = _truncate_on_line_boundary("\n\n".join(sections).strip() + "\n", DRIFT_IMPACT_LIMIT)
    return DriftImpactBrief(
        attached=True,
        text=text,
        bytes=len(text.encode()),
        pending_count=len(pending),
    )


def build_plan_prompt(
    task: str,
    roster: Roster,
    corrective_note: str | None = None,
    read_only: bool = False,
    code_graph: CodeGraphBrief | None = None,
    drift_impact: DriftImpactBrief | None = None,
) -> str:
    worker_lines = "\n".join(f"- {agent.name}: cli={agent.cli}; role={agent.role}" for agent in workers(roster))
    if not worker_lines:
        worker_lines = "- no workers configured"

    note = f"\nCorrection needed: {corrective_note}\n" if corrective_note else ""
    policy = f"\n\n{_read_only_rules()}\n" if read_only else ""
    prompt = (
        "You are the Brigade aboyeur. Split the user's task across the available workers.\n"
        "Return exactly one JSON object, with no prose outside JSON:\n"
        '{"assignments":[{"stage":1,"worker":"<worker-name>","task":"<specific sub-task>"}]}\n'
        f"{note}\n"
        f"User task:\n{task}\n\n"
        f"Available workers, excluding you:\n{worker_lines}\n\n"
        f"Rules:\n- Use at most {roster.max_workers} assignments per stage.\n"
        "- Stage must be a positive integer starting at stage 1.\n"
        "- Assignments in the same stage run in parallel; later stages receive earlier-stage worker results.\n"
        "- Omit stage only for backwards-compatible stage 1 assignments.\n"
        "- Assign only listed workers.\n"
        "- Use zero assignments only if no worker is useful."
        f"{policy}"
    )
    return _prepend_optional_briefs(prompt, code_graph=code_graph, drift_impact=drift_impact)


def _extract_json(text: str) -> object:
    stripped = text.strip()
    fenced = _extract_fenced_json(stripped)
    if fenced is not None:
        return json.loads(fenced)
    return _loads_first_json_object(stripped)


def _extract_fenced_json(text: str) -> str | None:
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.strip().startswith("```"):
            start = index + 1
            break
    if start is None:
        return None

    for end in range(start, len(lines)):
        if lines[end].strip().startswith("```"):
            return "\n".join(lines[start:end]).strip()
    return None


def _loads_first_json_object(text: str) -> object:
    decoder = JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return value
    return json.loads(text)


def make_run_dir(base: Path, now: datetime | None = None) -> Path:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    return base / f"{stamp}-{uuid4().hex[:8]}"


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _slug(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:48] or "brigade-run"


def _safe_document_content(text: str) -> str:
    # The ingester treats `##` as handoff section boundaries, so keep routed
    # document content at ### or below.
    return re.sub(r"(?m)^##(?!#)", "###", text).strip()


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def write_run_handoff(
    inbox: Path,
    *,
    task: str,
    cwd: Path | None,
    output_dir: Path | None,
    assignments: list[Assignment],
    worker_results: list[WorkerResult],
    final_text: str,
    read_only: bool = False,
    now: datetime | None = None,
) -> Path:
    timestamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%d-%H%M")
    safe_task = _one_line(task)
    path = inbox / f"{timestamp}-brigade-run-{_slug(safe_task)}.md"
    worker_summary = (
        "\n".join(
            f"- {result.worker}: {'ok' if result.ok else 'failed'}"
            + (f" ({_one_line(result.detail)})" if result.detail else "")
            for result in worker_results
        )
        or "- no workers dispatched"
    )
    assignment_summary = (
        "\n".join(
            f"- stage {assignment.stage} -> {assignment.worker}: {_one_line(assignment.task)}"
            for assignment in assignments
        )
        or "- no worker assignments"
    )
    artifact_line = f"- artifacts: `{output_dir}`" if output_dir is not None else "- artifacts: none"
    cwd_line = f"- cwd: `{cwd}`" if cwd is not None else "- cwd: not set"
    mode_line = "- mode: read-only" if read_only else "- mode: normal"
    document_content = _safe_document_content(
        f"""### Brigade run: {_slug(safe_task)}
- task: {safe_task}
{artifact_line}
{cwd_line}
{mode_line}

Final answer:
{final_text}
"""
    )
    body = f"""# Memory Handoff

## Type

project-context

## Title

Brigade run completed: {_slug(safe_task)}

## Summary

Brigade completed a bounded plan-dispatch-synthesize run and produced a final answer. This handoff captures the task, assignments, worker status, artifact path, and final result for memory ingestion.

## Durable facts

- task: {safe_task}
{cwd_line}
{artifact_line}
{mode_line}
- orchestrated assignments:
{assignment_summary}
- worker status:
{worker_summary}

## Evidence

{artifact_line}
- final answer captured in this handoff

## Recommended memory action

no-card

## Target document

.learnings/LEARNINGS.md

## Suggested document content

{document_content}
"""
    inbox.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def _read_only_rules() -> str:
    return (
        "READ-ONLY MODE:\n"
        "- Do not modify files.\n"
        "- Do not install packages, change configuration, commit, push, or call external write APIs.\n"
        "- You may inspect, reason, summarize, and recommend exact next steps.\n"
        "- If a task appears to require changes, describe the proposed changes instead of making them."
    )


def parse_plan(text: str, roster: Roster) -> list[Assignment]:
    try:
        payload = _extract_json(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"plan is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError("plan JSON must be an object")
    raw_assignments = payload.get("assignments")
    if not isinstance(raw_assignments, list):
        raise ValueError("plan JSON needs an assignments list")

    assignments: list[Assignment] = []
    seen: set[tuple[int, str, str]] = set()
    stage_counts: dict[int, int] = {}
    for item in raw_assignments:
        if not isinstance(item, dict):
            raise ValueError("each assignment must be an object")
        stage = item.get("stage", 1)
        if isinstance(stage, bool) or not isinstance(stage, int) or stage < 1:
            raise ValueError("assignment.stage must be a positive integer")
        raw_worker = item.get("worker")
        subtask = item.get("task")
        if not isinstance(raw_worker, str) or not raw_worker.strip():
            raise ValueError("assignment.worker must be a non-empty string")
        worker = raw_worker.strip()
        if worker not in roster.agents:
            raise ValueError(f"assignment references unknown worker: {worker!r}")
        if worker == roster.orchestrator:
            raise ValueError("assignment cannot target the orchestrator")
        if not isinstance(subtask, str) or not subtask.strip():
            raise ValueError("assignment.task must be a non-empty string")
        assignment = Assignment(worker=worker, task=subtask.strip(), stage=stage)
        key = (assignment.stage, assignment.worker, assignment.task)
        if key not in seen:
            assignments.append(assignment)
            seen.add(key)
            stage_counts[assignment.stage] = stage_counts.get(assignment.stage, 0) + 1

    for stage, count in stage_counts.items():
        if count > roster.max_workers:
            raise ValueError(f"plan has {count} assignments in stage {stage}, limit is {roster.max_workers}")
    return sorted(assignments, key=lambda assignment: assignment.stage)


def _record_plan_attempt(
    attempts: list[dict[str, object]] | None,
    *,
    stage: str,
    result: agents.AgentResult,
    parsed: bool = False,
    parse_error: str | None = None,
) -> None:
    if attempts is None:
        return
    payload: dict[str, object] = {
        "stage": stage,
        "ok": result.ok,
        "parsed": parsed,
        "detail": result.detail,
        "text": result.text,
    }
    if parse_error is not None:
        payload["parse_error"] = parse_error
    attempts.append(payload)


def _run_orchestrator(
    roster: Roster,
    prompt: str,
    cwd: Path | None = None,
    read_only: bool = False,
    sandbox_read_only: bool | None = None,
    sandbox: str | None = None,
) -> agents.AgentResult:
    orchestrator = roster.agents[roster.orchestrator]
    if not is_cli_allowed(orchestrator.cli, roster):
        return agents.AgentResult(
            text="",
            ok=False,
            detail=f"{orchestrator.cli} is not allowed by limits.allow_models",
        )
    kwargs: dict[str, object] = {
        "timeout": timeout_for(orchestrator, roster),
        "cwd": cwd,
        "read_only": read_only if sandbox_read_only is None else sandbox_read_only,
    }
    if sandbox is not None:
        kwargs["sandbox"] = sandbox
    if orchestrator.model is not None:
        kwargs["model"] = orchestrator.model
    return agents.run_agent(orchestrator.cli, prompt, **kwargs)


def plan(
    task: str,
    roster: Roster,
    cwd: Path | None = None,
    read_only: bool = False,
    sandbox_read_only: bool | None = None,
    sandbox: str | None = None,
    attempts: list[dict[str, object]] | None = None,
    code_graph: CodeGraphBrief | None = None,
    drift_impact: DriftImpactBrief | None = None,
) -> list[Assignment]:
    first = _run_orchestrator(
        roster,
        build_plan_prompt(task, roster, read_only=read_only, code_graph=code_graph, drift_impact=drift_impact),
        cwd=cwd,
        read_only=read_only,
        sandbox_read_only=sandbox_read_only,
        sandbox=sandbox,
    )
    if not first.ok:
        _record_plan_attempt(attempts, stage="initial", result=first)
        raise RuntimeError(f"orchestrator failed during plan: {first.detail}")
    try:
        assignments = parse_plan(first.text, roster)
        _record_plan_attempt(attempts, stage="initial", result=first, parsed=True)
        return assignments
    except ValueError as exc:
        _record_plan_attempt(attempts, stage="initial", result=first, parse_error=str(exc))
        second = _run_orchestrator(
            roster,
            build_plan_prompt(
                task,
                roster,
                corrective_note=str(exc),
                read_only=read_only,
                code_graph=code_graph,
                drift_impact=drift_impact,
            ),
            cwd=cwd,
            read_only=read_only,
            sandbox_read_only=sandbox_read_only,
            sandbox=sandbox,
        )
        if not second.ok:
            _record_plan_attempt(attempts, stage="correction", result=second)
            raise RuntimeError(f"orchestrator failed during plan correction: {second.detail}") from exc
        try:
            assignments = parse_plan(second.text, roster)
            _record_plan_attempt(attempts, stage="correction", result=second, parsed=True)
            return assignments
        except ValueError as second_exc:
            _record_plan_attempt(
                attempts,
                stage="correction",
                result=second,
                parse_error=str(second_exc),
            )
            raise RuntimeError(f"orchestrator returned an invalid plan: {second_exc}") from second_exc


def _render_prior_results(results: list[WorkerResult]) -> str:
    return "\n\n".join(
        "\n".join(
            [
                f"Worker: {result.worker}",
                f"Sub-task: {result.task}",
                f"Status: {'ok' if result.ok else 'failed'}",
                f"Detail: {result.detail}" if result.detail else "Detail:",
                "Output:",
                result.text or "(no output)",
            ]
        )
        for result in results
    )


def _worker_prompt(
    agent: Agent,
    assignment: Assignment,
    *,
    prior_results: list[WorkerResult] | None = None,
    read_only: bool = False,
    code_graph: CodeGraphBrief | None = None,
    drift_impact: DriftImpactBrief | None = None,
) -> str:
    prior_context = ""
    if prior_results:
        prior_context = f"\n\nEarlier-stage context:\n{_render_prior_results(prior_results)}"
    policy = f"\n\n{_read_only_rules()}" if read_only else ""
    prompt = (
        f"You are Brigade worker {agent.name}.\n"
        f"Role:\n{agent.role}\n\n"
        f"Sub-task:\n{assignment.task}\n\n"
        "Return a concise, complete result for the orchestrator to synthesize."
        f"{prior_context}"
        f"{policy}"
    )
    return _prepend_optional_briefs(prompt, code_graph=code_graph, drift_impact=drift_impact)


def _worker_event_writer(events_dir: Path | None, worker: str, *, verbose: bool = False):
    """Append lifecycle notifications to events/<worker>.jsonl; optionally narrate."""
    if events_dir is None and not verbose:
        return None
    path = None
    if events_dir is not None:
        events_dir.mkdir(parents=True, exist_ok=True)
        path = events_dir / f"{_slug(worker)}.jsonl"

    def on_event(msg: dict) -> None:
        if path is not None:
            with path.open("a") as fh:
                fh.write(json.dumps(msg) + "\n")
        if verbose and msg.get("method") == "item/completed":
            item = (msg.get("params") or {}).get("item") or {}
            print(f"worker {worker}: {item.get('type', 'item')} completed", file=sys.stderr)

    return on_event


def _run_codex_appserver_worker(
    appserver,
    agent: Agent,
    worker: str,
    prompt: str,
    *,
    timeout: float,
    cwd: Path | None,
    read_only: bool,
    sandbox: str | None,
    registry: run_control.LiveTurnRegistry | None,
    on_event=None,
) -> agents.AgentResult:
    effective_sandbox = sandbox if sandbox is not None else ("read-only" if read_only else None)
    active_turn_id: str | None = None
    try:
        thread = appserver.start_thread(cwd=cwd, model=agent.model, sandbox=effective_sandbox)

        def on_turn_start(turn_id: str) -> None:
            nonlocal active_turn_id
            active_turn_id = turn_id
            if registry is not None:
                registry.register(worker, thread, turn_id)

        try:
            turn = thread.run_turn(prompt, timeout=timeout, on_event=on_event, on_turn_start=on_turn_start)
        except TypeError as exc:
            if "on_turn_start" not in str(exc):
                raise
            turn = thread.run_turn(prompt, timeout=timeout, on_event=on_event)
    except codex_appserver.AppServerError as exc:
        return agents.AgentResult(text="", ok=False, detail=str(exc)[:200], status="failed")
    finally:
        if registry is not None and active_turn_id is not None:
            registry.unregister(worker, active_turn_id)
    text = turn.text.strip()
    if not turn.ok:
        return agents.AgentResult(
            text=text,
            ok=False,
            detail=(turn.detail or f"turn {turn.status}")[:200],
            thread_id=turn.thread_id,
            status=turn.status,
        )
    if not text:
        return agents.AgentResult(
            text="", ok=False, detail="empty output", thread_id=turn.thread_id, status=turn.status
        )
    return agents.AgentResult(text=text, ok=True, thread_id=turn.thread_id, status=turn.status)


def dispatch(
    assignments: list[Assignment],
    roster: Roster,
    cwd: Path | None = None,
    read_only: bool = False,
    sandbox_read_only: bool | None = None,
    sandbox: str | None = None,
    code_graph: CodeGraphBrief | None = None,
    drift_impact: DriftImpactBrief | None = None,
    appserver=None,
    control_registry: run_control.LiveTurnRegistry | None = None,
    events_dir: Path | None = None,
    verbose: bool = False,
) -> list[WorkerResult]:
    def run_one(assignment: Assignment, prior_results: list[WorkerResult]) -> WorkerResult:
        agent = roster.agents[assignment.worker]
        if not is_cli_allowed(agent.cli, roster):
            return WorkerResult(
                worker=assignment.worker,
                task=assignment.task,
                text="",
                ok=False,
                detail=f"{agent.cli} is not allowed by limits.allow_models",
            )
        prompt = _worker_prompt(
            agent,
            assignment,
            prior_results=prior_results,
            read_only=read_only,
            code_graph=code_graph,
            drift_impact=drift_impact,
        )
        if agent.cli == "codex" and appserver is not None:
            on_event = _worker_event_writer(events_dir, assignment.worker, verbose=verbose)
            result = _run_codex_appserver_worker(
                appserver,
                agent,
                assignment.worker,
                prompt,
                timeout=timeout_for(agent, roster),
                cwd=cwd,
                read_only=read_only if sandbox_read_only is None else sandbox_read_only,
                sandbox=sandbox,
                registry=control_registry,
                on_event=on_event,
            )
        else:
            kwargs: dict[str, object] = {
                "timeout": timeout_for(agent, roster),
                "cwd": cwd,
                "read_only": read_only if sandbox_read_only is None else sandbox_read_only,
            }
            if sandbox is not None:
                kwargs["sandbox"] = sandbox
            if agent.model is not None:
                kwargs["model"] = agent.model
            result = agents.run_agent(agent.cli, prompt, **kwargs)
        return WorkerResult(
            worker=assignment.worker,
            task=assignment.task,
            text=result.text,
            ok=result.ok,
            detail=result.detail,
            thread_id=result.thread_id,
            status=result.status,
        )

    if not assignments:
        return []

    all_results: list[WorkerResult] = []
    stages = sorted({assignment.stage for assignment in assignments})
    for stage in stages:
        stage_assignments = [assignment for assignment in assignments if assignment.stage == stage]
        stage_results_by_index: dict[int, WorkerResult] = {}
        prior_results = list(all_results)
        max_workers = min(roster.max_workers, len(stage_assignments))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_to_index = {
                executor.submit(run_one, assignment, prior_results): index
                for index, assignment in enumerate(stage_assignments)
            }
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    stage_results_by_index[index] = future.result()
                except Exception as exc:  # pragma: no cover - defensive boundary
                    assignment = stage_assignments[index]
                    stage_results_by_index[index] = WorkerResult(
                        worker=assignment.worker,
                        task=assignment.task,
                        text="",
                        ok=False,
                        detail=str(exc)[:200],
                    )
        all_results.extend(stage_results_by_index[index] for index in range(len(stage_assignments)))

    return all_results


def build_synth_prompt(
    task: str,
    results: list[WorkerResult],
    read_only: bool = False,
    ground_truth: dict[str, object] | None = None,
    code_graph: CodeGraphBrief | None = None,
    drift_impact: DriftImpactBrief | None = None,
) -> str:
    if results:
        rendered = "\n\n".join(
            "\n".join(
                [
                    f"Worker: {result.worker}",
                    f"Sub-task: {result.task}",
                    f"Status: {'ok' if result.ok else 'failed'}",
                    f"Detail: {result.detail}" if result.detail else "Detail:",
                    "Output:",
                    result.text or "(no output)",
                ]
            )
            for result in results
        )
    else:
        rendered = "(No workers were assigned.)"

    policy = f"\n\n{_read_only_rules()}" if read_only else ""
    facts = _ground_truth_facts(ground_truth)
    facts_block = f"\n\n{facts}" if facts else ""
    prompt = (
        "You are the Brigade orchestrator. Synthesize the final answer for the user.\n"
        "Account for worker failures if any are present. Do not include implementation chatter."
        f"{facts_block}\n\n"
        f"Original task:\n{task}\n\n"
        f"Worker results:\n{rendered}\n"
        f"{policy}"
    )
    return _prepend_optional_briefs(prompt, code_graph=code_graph, drift_impact=drift_impact)


def _print_plan(assignments: list[Assignment]) -> None:
    print("plan:")
    if not assignments:
        print("  (no worker assignments)")
        return
    stages = sorted({assignment.stage for assignment in assignments})
    if len(stages) == 1:
        for assignment in assignments:
            print(f"  -> {assignment.worker}: {assignment.task}")
        return
    for stage in stages:
        print(f"  stage {stage}:")
        for assignment in assignments:
            if assignment.stage == stage:
                print(f"    -> {assignment.worker}: {assignment.task}")


def _print_worker_status(results: list[WorkerResult]) -> None:
    print("workers:")
    if not results:
        print("  (none)")
        return
    for result in results:
        marker = "ok" if result.ok else "failed"
        detail = f": {result.detail}" if result.detail else ""
        print(f"  [{marker}] {result.worker}{detail}")


def _assignment_payload(assignments: list[Assignment]) -> list[dict[str, object]]:
    return [
        {"stage": assignment.stage, "worker": assignment.worker, "task": assignment.task} for assignment in assignments
    ]


def _worker_payload(results: list[WorkerResult]) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for result in results:
        entry: dict[str, object] = {
            "worker": result.worker,
            "task": result.task,
            "ok": result.ok,
            "detail": result.detail,
            "text": result.text,
        }
        if result.thread_id is not None:
            entry["thread_id"] = result.thread_id
            entry["status"] = result.status
        payload.append(entry)
    return payload


def _agent_result_payload(result: agents.AgentResult) -> dict[str, object]:
    return {
        "ok": result.ok,
        "detail": result.detail,
        "text": result.text,
    }


def _code_graph_delta_skip(status: str) -> dict[str, object]:
    reasons = {
        "disabled": "disabled",
        "skipped_read_only": "read-only run",
        "skipped_dry_run": "dry run",
        "unavailable": "cwd not set",
    }
    reason = reasons.get(status, status.replace("_", " "))
    return {
        "status": status,
        "ok": False,
        "summary": f"code graph delta skipped: {reason}",
        "raw_counts": {},
        "edge_churn": 0,
        "changed_symbols": [],
        "changed_symbol_count": 0,
    }


def _initial_code_graph_delta(
    *,
    code_graph_enabled: bool,
    dry_run: bool,
    read_only: bool,
    cwd: Path | None,
) -> dict[str, object] | None:
    if not code_graph_enabled:
        return _code_graph_delta_skip("disabled")
    if read_only:
        return _code_graph_delta_skip("skipped_read_only")
    if dry_run:
        return _code_graph_delta_skip("skipped_dry_run")
    if cwd is None:
        return _code_graph_delta_skip("unavailable")
    return None


def _parse_iso_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _git_stdout(cwd: Path, *args: str) -> tuple[str, str | None]:
    result = proc.run(["git", *args], cwd=cwd)
    if result.code == 0:
        return result.stdout, None
    detail = result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed"
    return "", detail


def _receipt_command_payload(command: object) -> dict[str, object] | None:
    if not isinstance(command, dict):
        return None
    raw_command = command.get("command")
    if not isinstance(raw_command, str):
        return None
    payload: dict[str, object] = {"command": raw_command}
    status = command.get("status")
    if isinstance(status, str):
        payload["status"] = status
    exit_code = command.get("exit_code")
    if isinstance(exit_code, int) and not isinstance(exit_code, bool):
        payload["exit_code"] = exit_code
    elif exit_code is None:
        payload["exit_code"] = None
    return payload


def _verify_receipt_payload(data: dict[str, Any]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key in ("run_id", "status", "started_at", "completed_at"):
        value = data.get(key)
        if isinstance(value, str):
            payload[key] = value
    commands = data.get("commands")
    if isinstance(commands, list):
        payload["commands"] = [
            command_payload for item in commands if (command_payload := _receipt_command_payload(item)) is not None
        ]
    else:
        payload["commands"] = []
    return payload


def _verify_receipts_since(cwd: Path, started_at: datetime) -> list[dict[str, object]]:
    root = cwd / ".brigade" / "work" / "verify-runs"
    if not root.is_dir():
        return []
    receipts: list[dict[str, object]] = []
    for receipt_path in sorted(root.glob("*/receipt.json")):
        try:
            data = json.loads(receipt_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        receipt_started_at = _parse_iso_datetime(data.get("started_at"))
        if receipt_started_at is None or receipt_started_at < started_at:
            continue
        receipts.append(_verify_receipt_payload(data))

    def _sort_key(item: dict[str, object]) -> tuple[datetime, str]:
        # Sort by parsed UTC time: lexical started_at ordering misorders
        # receipts with mixed timezone offsets.
        parsed = _parse_iso_datetime(item.get("started_at"))
        return (parsed or datetime.min.replace(tzinfo=timezone.utc), str(item.get("run_id") or ""))

    receipts.sort(key=_sort_key, reverse=True)
    return receipts


def build_ground_truth(cwd: Path | None, started_at: datetime) -> dict[str, object]:
    verify_receipts = _verify_receipts_since(cwd, started_at) if cwd is not None else []
    payload: dict[str, object] = {
        "available": False,
        "cwd": str(cwd) if cwd is not None else None,
        "diffstat": "",
        "changed_files": [],
        "untracked_files": [],
        "patch_ref": None,
        "verify_receipts": verify_receipts,
        "latest_verify": verify_receipts[0] if verify_receipts else None,
    }
    if cwd is None:
        payload["reason"] = "cwd not set"
        return payload

    inside, error = _git_stdout(cwd, "rev-parse", "--is-inside-work-tree")
    if error is not None or inside.strip() != "true":
        payload["reason"] = error or "not a git worktree"
        return payload

    diffstat, error = _git_stdout(cwd, "diff", "--stat", "HEAD")
    if error is not None:
        payload["reason"] = error
        return payload
    changed_names, error = _git_stdout(cwd, "diff", "--name-only", "HEAD")
    if error is not None:
        payload["reason"] = error
        return payload
    try:
        untracked_files = runguard._untracked_files(cwd)
    except runguard.RunGuardError as exc:
        payload["reason"] = str(exc)
        return payload

    payload.update(
        {
            "available": True,
            "diffstat": diffstat.strip(),
            "changed_files": [line for line in changed_names.splitlines() if line.strip()],
            "untracked_files": untracked_files,
        }
    )
    return payload


def _ground_truth_str_list(ground_truth: dict[str, object], key: str) -> list[str]:
    value = ground_truth.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]


def _ground_truth_facts(ground_truth: dict[str, object] | None) -> str:
    if ground_truth is None:
        return ""
    lines = ["Brigade-computed facts:"]
    if ground_truth.get("available") is not True:
        reason = ground_truth.get("reason")
        detail = f" ({_one_line(str(reason))})" if reason else ""
        lines.append(f"- ground_truth: unavailable{detail}")
    else:
        changed_files = _ground_truth_str_list(ground_truth, "changed_files")
        untracked_files = _ground_truth_str_list(ground_truth, "untracked_files")
        diffstat = _one_line(str(ground_truth.get("diffstat") or "none"))
        if len(diffstat) > 240:
            diffstat = diffstat[:237] + "..."
        lines.append(
            f"- changed_files: {len(changed_files)}" + (f" ({', '.join(changed_files[:6])})" if changed_files else "")
        )
        lines.append(
            f"- untracked_files: {len(untracked_files)}"
            + (f" ({', '.join(untracked_files[:6])})" if untracked_files else "")
        )
        lines.append(f"- diffstat: {diffstat}")
    patch_ref = ground_truth.get("patch_ref")
    if isinstance(patch_ref, str) and patch_ref:
        lines.append(f"- patch_ref: {patch_ref}")
    verify_receipts = ground_truth.get("verify_receipts")
    if isinstance(verify_receipts, list) and verify_receipts:
        latest = verify_receipts[0] if isinstance(verify_receipts[0], dict) else {}
        latest_status = latest.get("status") if isinstance(latest.get("status"), str) else "unknown"
        latest_run = latest.get("run_id") if isinstance(latest.get("run_id"), str) else "unknown"
        lines.append(f"- verify_receipts: {len(verify_receipts)} latest={latest_run} status={latest_status}")
    else:
        lines.append("- verify_receipts: 0")
    code_graph_delta = ground_truth.get("code_graph_delta")
    if isinstance(code_graph_delta, dict):
        summary = _one_line(str(code_graph_delta.get("summary") or code_graph_delta.get("status") or "unknown"))
        if len(summary) > 240:
            summary = summary[:237] + "..."
        lines.append(f"- code_graph_delta: {summary}")
    return "\n".join(lines)


def _with_patch_ref(ground_truth: object, patch_ref: str) -> object:
    if not isinstance(ground_truth, dict):
        return ground_truth
    updated = dict(ground_truth)
    updated["patch_ref"] = patch_ref
    return updated


def set_artifact_patch_ref(output_dir: Path, patch_ref: str = "changes.patch") -> None:
    for filename in ("worker-results.json", "synthesis.json"):
        path = output_dir / filename
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or "ground_truth" not in payload:
            continue
        payload["ground_truth"] = _with_patch_ref(payload.get("ground_truth"), patch_ref)
        _write_json(path, payload)


def _roster_payload(roster: Roster) -> dict[str, object]:
    return {
        "orchestrator": roster.orchestrator,
        "max_workers": roster.max_workers,
        "timeout_seconds": roster.timeout_seconds,
        "allow_models": list(roster.allow_models),
        "sandbox": roster.sandbox,
        "agents": {
            name: {
                "cli": agent.cli,
                "model": agent.model,
                "role": agent.role,
                "timeout_seconds": agent.timeout_seconds,
            }
            for name, agent in roster.agents.items()
        },
    }


def _run_payload(
    *,
    task: str,
    cwd: Path | None,
    roster: Roster,
    dry_run: bool,
    read_only: bool,
    status: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    output_dir: Path | None = None,
    handoff_path: Path | None = None,
    error: str | None = None,
    code_graph: CodeGraphBrief | None = None,
    drift_impact: DriftImpactBrief | None = None,
    brief_set: BriefSet | None = None,
    codex_transport: str | None = None,
    control_socket: Path | None = None,
    code_graph_delta: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "task": task,
        "cwd": str(cwd) if cwd is not None else None,
        "orchestrator": roster.orchestrator,
        "dry_run": dry_run,
        "read_only": read_only,
        "status": status,
        "started_at": _utc_iso(started_at),
        "code_graph_brief": {
            "attached": bool(code_graph.attached) if code_graph is not None else False,
            "bytes": code_graph.bytes if code_graph is not None else 0,
        },
        "drift_impact_brief": {
            "attached": bool(drift_impact.attached) if drift_impact is not None else False,
            "bytes": drift_impact.bytes if drift_impact is not None else 0,
            "pending_count": drift_impact.pending_count if drift_impact is not None else 0,
        },
        "brief_budget": {
            "bytes": brief_set.budget_bytes if brief_set is not None else BRIEF_BUDGET_BYTES,
            "attached": list(brief_set.attached) if brief_set is not None else [],
        },
    }
    if code_graph_delta is not None:
        payload["code_graph_delta"] = code_graph_delta
    if finished_at is not None:
        payload["finished_at"] = _utc_iso(finished_at)
        payload["duration_seconds"] = max(0.0, round((finished_at - started_at).total_seconds(), 3))
    if output_dir is not None:
        payload["artifacts"] = str(output_dir)
    if handoff_path is not None:
        payload["handoff"] = str(handoff_path)
    if error is not None:
        payload["error"] = error
    if codex_transport is not None:
        payload["codex_transport"] = codex_transport
    if control_socket is not None:
        payload["control_socket"] = str(control_socket)
    return payload


def run(
    task: str,
    roster: Roster,
    *,
    dry_run: bool = False,
    show_plan: bool = False,
    verbose: bool = False,
    cwd: Path | None = None,
    output_dir: Path | None = None,
    handoff_inbox: Path | None = None,
    read_only: bool = False,
    sandbox_read_only: bool | None = None,
    sandbox: str | None = None,
    code_graph_enabled: bool = True,
    code_graph: CodeGraphBrief | None = None,
    drift_impact: DriftImpactBrief | None = None,
    codex_transport: str | None = None,
) -> int:
    started_at = datetime.now(timezone.utc)
    transport_for_payload = codex_transport or roster.codex_transport
    cwd = cwd.expanduser().resolve() if cwd is not None else None
    output_dir = output_dir.expanduser() if output_dir is not None else None
    handoff_inbox = handoff_inbox.expanduser() if handoff_inbox is not None else None
    if code_graph is None:
        code_graph = code_graph_brief(cwd, task) if code_graph_enabled else CodeGraphBrief(attached=False)
    if drift_impact is None:
        drift_impact = drift_impact_brief(cwd) if code_graph_enabled else DriftImpactBrief(attached=False)
    brief_set = arbitrate_briefs(task, code_graph=code_graph, drift_impact=drift_impact)
    code_graph = brief_set.code_graph
    drift_impact = brief_set.drift_impact
    code_graph_delta = _initial_code_graph_delta(
        code_graph_enabled=code_graph_enabled,
        dry_run=dry_run,
        read_only=read_only,
        cwd=cwd,
    )
    code_graph_delta_before: dict[str, object] | None = None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        if code_graph_delta is None and cwd is not None:
            code_graph_delta_before = graphtrail_delta.capture_before(cwd, output_dir)
            code_graph_delta = code_graph_delta_before
        _write_json(output_dir / "roster.json", _roster_payload(roster))
        _write_json(
            output_dir / "run.json",
            _run_payload(
                task=task,
                cwd=cwd,
                roster=roster,
                dry_run=dry_run,
                read_only=read_only,
                status="started",
                started_at=started_at,
                output_dir=output_dir,
                code_graph=code_graph,
                drift_impact=drift_impact,
                brief_set=brief_set,
                codex_transport=transport_for_payload,
                code_graph_delta=code_graph_delta,
            ),
        )

    control_socket = None
    plan_attempts: list[dict[str, object]] | None = [] if output_dir is not None else None
    try:
        assignments = plan(
            task,
            roster,
            cwd=cwd,
            read_only=read_only,
            sandbox_read_only=sandbox_read_only,
            sandbox=sandbox,
            attempts=plan_attempts,
            code_graph=code_graph,
            drift_impact=drift_impact,
        )
    except RuntimeError as exc:
        if output_dir is not None:
            finished_at = datetime.now(timezone.utc)
            _write_json(output_dir / "plan-attempts.json", {"attempts": plan_attempts or []})
            _write_json(
                output_dir / "run.json",
                _run_payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="failed",
                    started_at=started_at,
                    finished_at=finished_at,
                    output_dir=output_dir,
                    error=str(exc),
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    brief_set=brief_set,
                    codex_transport=transport_for_payload,
                    control_socket=control_socket,
                    code_graph_delta=code_graph_delta,
                ),
            )
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if output_dir is not None:
        _write_json(output_dir / "plan-attempts.json", {"attempts": plan_attempts or []})
        _write_json(output_dir / "plan.json", {"assignments": _assignment_payload(assignments)})

    if dry_run:
        payload = {"assignments": _assignment_payload(assignments)}
        if output_dir is not None:
            finished_at = datetime.now(timezone.utc)
            _write_json(
                output_dir / "run.json",
                _run_payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="dry-run",
                    started_at=started_at,
                    finished_at=finished_at,
                    output_dir=output_dir,
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    brief_set=brief_set,
                    codex_transport=transport_for_payload,
                    code_graph_delta=code_graph_delta,
                ),
            )
        print(json.dumps(payload, indent=2))
        return 0

    if show_plan or verbose:
        _print_plan(assignments)

    effective_transport = codex_transport or roster.codex_transport
    has_codex_workers = any(
        (roster.agents.get(a.worker) is not None and roster.agents[a.worker].cli == "codex") for a in assignments
    )
    appserver = None
    control_registry = None
    control_server = None
    if effective_transport == "app-server" and has_codex_workers:
        try:
            appserver = codex_appserver.AppServer(cwd=cwd)
            appserver.start()
        except codex_appserver.AppServerError as exc:
            print(f"warning: codex app-server unavailable ({exc}); falling back to exec", file=sys.stderr)
            appserver = None
            effective_transport = "exec"
        if appserver is not None and output_dir is not None:
            control_registry = run_control.LiveTurnRegistry()
            control_socket = output_dir / "control.sock"
            control_server = run_control.ControlServer(control_socket, control_registry)
            try:
                control_server.start()
            except run_control.ControlError as exc:
                print(f"warning: run control unavailable ({exc})", file=sys.stderr)
                control_registry = None
                control_server = None
                control_socket = None
    elif effective_transport == "app-server":
        effective_transport = "exec"
    transport_for_payload = effective_transport
    if output_dir is not None and control_socket is not None:
        _write_json(
            output_dir / "run.json",
            _run_payload(
                task=task,
                cwd=cwd,
                roster=roster,
                dry_run=dry_run,
                read_only=read_only,
                status="dispatching",
                started_at=started_at,
                output_dir=output_dir,
                code_graph=code_graph,
                drift_impact=drift_impact,
                brief_set=brief_set,
                codex_transport=transport_for_payload,
                control_socket=control_socket,
                code_graph_delta=code_graph_delta,
            ),
        )

    try:
        worker_results = dispatch(
            assignments,
            roster,
            cwd=cwd,
            read_only=read_only,
            sandbox_read_only=sandbox_read_only,
            sandbox=sandbox,
            code_graph=code_graph,
            drift_impact=drift_impact,
            appserver=appserver,
            control_registry=control_registry,
            events_dir=(output_dir / "events") if (output_dir is not None and appserver is not None) else None,
            verbose=verbose,
        )
    finally:
        if control_server is not None:
            control_server.close()
        if appserver is not None:
            appserver.close()
    if output_dir is not None and code_graph_delta_before is not None and cwd is not None:
        code_graph_delta = graphtrail_delta.capture_after_and_diff(cwd, output_dir, code_graph_delta_before)
    ground_truth = build_ground_truth(cwd, started_at)
    if code_graph_delta is not None:
        ground_truth["code_graph_delta"] = code_graph_delta
    if output_dir is not None:
        _write_json(
            output_dir / "worker-results.json",
            {"results": _worker_payload(worker_results), "ground_truth": ground_truth},
        )
    if verbose:
        _print_worker_status(worker_results)
        print("synthesis:")
        print(f"  -> {roster.orchestrator}")

    final = _run_orchestrator(
        roster,
        build_synth_prompt(
            task,
            worker_results,
            read_only=read_only,
            ground_truth=ground_truth,
            code_graph=code_graph,
            drift_impact=drift_impact,
        ),
        cwd=cwd,
        read_only=read_only,
        sandbox_read_only=sandbox_read_only,
        sandbox=sandbox,
    )
    if output_dir is not None:
        _write_json(
            output_dir / "synthesis.json",
            {
                "orchestrator": roster.orchestrator,
                "result": _agent_result_payload(final),
                "ground_truth": ground_truth,
            },
        )
    if not final.ok:
        if output_dir is not None:
            finished_at = datetime.now(timezone.utc)
            _write_json(
                output_dir / "run.json",
                _run_payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="failed",
                    started_at=started_at,
                    finished_at=finished_at,
                    output_dir=output_dir,
                    error=final.detail,
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    brief_set=brief_set,
                    codex_transport=transport_for_payload,
                    code_graph_delta=code_graph_delta,
                ),
            )
        print(f"error: orchestrator failed during synthesis: {final.detail}", file=sys.stderr)
        return 2
    if output_dir is not None:
        finished_at = datetime.now(timezone.utc)
        (output_dir / "final.txt").write_text(final.text + "\n")
        _write_json(
            output_dir / "run.json",
            _run_payload(
                task=task,
                cwd=cwd,
                roster=roster,
                dry_run=dry_run,
                read_only=read_only,
                status="ok",
                started_at=started_at,
                finished_at=finished_at,
                output_dir=output_dir,
                code_graph=code_graph,
                drift_impact=drift_impact,
                brief_set=brief_set,
                codex_transport=transport_for_payload,
                control_socket=control_socket,
                code_graph_delta=code_graph_delta,
            ),
        )
    if handoff_inbox is not None:
        try:
            handoff = write_run_handoff(
                handoff_inbox,
                task=task,
                cwd=cwd,
                output_dir=output_dir,
                assignments=assignments,
                worker_results=worker_results,
                final_text=final.text,
                read_only=read_only,
            )
        except OSError as exc:
            detail = f"handoff failed: {exc}"
            if output_dir is not None:
                finished_at = datetime.now(timezone.utc)
                _write_json(
                    output_dir / "run.json",
                    _run_payload(
                        task=task,
                        cwd=cwd,
                        roster=roster,
                        dry_run=dry_run,
                        read_only=read_only,
                        status="handoff-failed",
                        started_at=started_at,
                        finished_at=finished_at,
                        output_dir=output_dir,
                        error=detail,
                        code_graph=code_graph,
                        drift_impact=drift_impact,
                        brief_set=brief_set,
                        codex_transport=transport_for_payload,
                        control_socket=control_socket,
                        code_graph_delta=code_graph_delta,
                    ),
                )
            print(f"error: {detail}", file=sys.stderr)
            print(final.text)
            return 2
        print(f"handoff: {handoff}", file=sys.stderr)
        if output_dir is not None:
            finished_at = datetime.now(timezone.utc)
            _write_json(
                output_dir / "run.json",
                _run_payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="ok",
                    started_at=started_at,
                    finished_at=finished_at,
                    output_dir=output_dir,
                    handoff_path=handoff,
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    brief_set=brief_set,
                    codex_transport=transport_for_payload,
                    control_socket=control_socket,
                    code_graph_delta=code_graph_delta,
                ),
            )
    print(final.text)
    return 0
