"""Stage 1 compatibility seam for GraphTrail and drift briefs."""
# ruff: noqa: F401

from __future__ import annotations

import copy
import inspect
import json
import os
import re
import signal
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from functools import partial, wraps
from json import JSONDecoder
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence
from uuid import uuid4

from .. import agents
from .. import codex_appserver
from .. import context_eval
from .. import evidence_brief as evidence_brief_mod
from .. import graphtrail_delta
from .. import causal_receipt
from .. import localio
from .. import message_envelope
from .. import proc, receipt_schema, runguard
from .. import run_control
from .. import run_checkpoint
from .. import run_budget
from .. import run_events
from .. import run_journal
from .. import run_lifecycle
from .. import run_projector
from .. import run_shadow
from .. import seat_health
from .. import seat_health_policy
from .. import verification_contract
from ..result_integrity import validate_final_output
from ..run_receipts import (
    agent_result_from_worker as _agent_result_from_worker,
    agent_result_payload as _agent_result_payload,
    assignment_payload as _assignment_payload,
    worker_payload as _worker_payload,
    write_agent_logs as _write_agent_logs,
    write_worker_logs as _write_worker_logs,
)
from ..run_transport import Assignment, WorkerResult, dag_cycle_members
from ..roster import Agent, Roster, is_cli_allowed, read_only_capability_error, timeout_for, workers
from ..route_catalog import RouteBrief, route_brief, uncovered_stages, unknown_covers
from ..route_policy import (
    RoutePolicyDecision,
    direct_worker_skill_ids,
    planner_skill_policy_section,
    validate_plan_skill_bindings,
    worker_skill_policy_constraint,
)

from . import run_io, planning, prompts, artifacts
from . import orchestrator as _orchestrator_mod


CODE_GRAPH_HEADING = "## Code graph context (GraphTrail, read-only)"
CODE_GRAPH_LIMIT = 4000
DRIFT_IMPACT_HEADING = "## Upstream drift impact (Upstream Drift + GraphTrail, read-only)"
DRIFT_IMPACT_LIMIT = 4000
BRIEF_BUDGET_BYTES = 6000


# A plan-mode seat has no write tool, so any file it tries to create fails, and a
# failed write is what invites harness hooks to hijack the seat's final message
# (#518). Say the quiet part in the prompt: the plan lives in the reply, nowhere else.
NO_PLAN_FILE_RULE = (
    "- Do not write, create, or edit any file, including a plan, design, or context file. "
    "This seat runs with every write tool hidden: the write fails, and the failure can "
    "replace your plan with tool or hook commentary. The plan belongs in this reply only."
)


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


EvidenceBrief = evidence_brief_mod.EvidenceBrief


@dataclass(frozen=True)
class BriefSet:
    code_graph: CodeGraphBrief
    drift_impact: DriftImpactBrief
    evidence: EvidenceBrief
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
        return ("drift_impact", "code_graph", "evidence")
    if any(word in lowered for word in ("doc", "readme", "handoff", "memory", "evidence")):
        return ("drift_impact", "code_graph", "evidence")
    return ("code_graph", "drift_impact", "evidence")


def arbitrate_briefs(
    task: str,
    *,
    code_graph: CodeGraphBrief,
    drift_impact: DriftImpactBrief,
    evidence: EvidenceBrief | None = None,
    budget_bytes: int = BRIEF_BUDGET_BYTES,
) -> BriefSet:
    evidence = evidence or EvidenceBrief(attached=False)
    briefs: dict[str, CodeGraphBrief | DriftImpactBrief | EvidenceBrief] = {
        "code_graph": code_graph,
        "drift_impact": drift_impact,
        "evidence": evidence,
    }
    kept_code_graph = CodeGraphBrief(attached=False)
    kept_drift = DriftImpactBrief(attached=False)
    kept_evidence = EvidenceBrief(attached=False)
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
        elif name == "drift_impact":
            kept_drift = DriftImpactBrief(
                attached=True,
                text=text,
                bytes=size,
                pending_count=getattr(brief, "pending_count", 0),
            )
        else:
            kept_evidence = EvidenceBrief(attached=True, text=text, bytes=size)
    return BriefSet(
        code_graph=kept_code_graph,
        drift_impact=kept_drift,
        evidence=kept_evidence,
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
    evidence: EvidenceBrief | None = None,
) -> str:
    if code_graph is not None and code_graph.attached and code_graph.text:
        prompt = _prepend_brief(prompt, heading=CODE_GRAPH_HEADING, text=code_graph.text)
    if drift_impact is not None and drift_impact.attached and drift_impact.text:
        prompt = _prepend_brief(prompt, heading=DRIFT_IMPACT_HEADING, text=drift_impact.text)
    if evidence is not None and evidence.attached and evidence.text:
        prompt = _prepend_brief(prompt, heading=evidence_brief_mod.HEADING, text=evidence.text)
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
    from .. import context_cmd

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


def _planner_worker_line(agent: Agent, *, read_only: bool) -> str:
    parts = [f"{agent.name}: cli={agent.cli}"]
    if agent.model:
        parts.append(f"model={agent.model}")
    if agent.purpose:
        parts.append(f"purpose={agent.purpose}")
    if agent.caveats:
        parts.append("caveats=" + ", ".join(agent.caveats))
    if agent.fallback:
        parts.append("fallback=" + ", ".join(agent.fallback))
    if read_only:
        parts.append(f"read_only_capable={str(agent.read_only_capable).lower()}")
    parts.append(f"role={agent.role}")
    return "- " + "; ".join(parts)


def build_plan_prompt(
    task: str,
    roster: Roster,
    corrective_note: str | None = None,
    read_only: bool = False,
    code_graph: CodeGraphBrief | None = None,
    drift_impact: DriftImpactBrief | None = None,
    evidence: EvidenceBrief | None = None,
    route: RouteBrief | None = None,
    no_file_writes: bool = False,
    skill_policy: RoutePolicyDecision | None = None,
) -> str:
    worker_lines = "\n".join(_planner_worker_line(agent, read_only=read_only) for agent in workers(roster))
    if not worker_lines:
        worker_lines = "- no workers configured"

    note = f"\nCorrection needed: {corrective_note}\n" if corrective_note else ""
    policy = f"\n\n{planning._read_only_rules()}\n" if read_only else ""
    capability_rule = "- Assign only workers with read_only_capable=true.\n" if read_only else ""
    no_write_rule = f"\n{NO_PLAN_FILE_RULE}" if no_file_writes else ""
    route_section = ""
    route_rule = ""
    if route is not None and route.attached and route.text:
        route_section = f"\n{route.text}"
        route_rule = (
            '\n- Tag each assignment with "covers": ["<stage>", ...] naming the route '
            "stages it satisfies; every required route stage must be covered."
        )
    skill_section = ""
    skill_text = planner_skill_policy_section(skill_policy)
    if skill_text:
        skill_section = f"\n{skill_text}"
    prompt = (
        "You are the Brigade aboyeur. Split the user's task across the available workers.\n"
        "Return exactly one JSON object, with no prose outside JSON:\n"
        '{"assignments":[{"stage":1,"worker":"<worker-name>","task":"<specific sub-task>",'
        '"covers":["<route-stage>"],"selected_skill_ids":["<artifact-id>"],'
        '"domain":"<tool-domain>","capabilities":["<capability>"],'
        '"max_risk_class":"read|local-write|network|privileged"}]}\n'
        f"{note}\n"
        f"User task:\n{task}\n\n"
        f"Available workers, excluding you:\n{worker_lines}\n"
        f"{route_section}"
        f"{skill_section}\n"
        f"Rules:\n- Use at most {roster.max_workers} assignments per stage.\n"
        "- Stage must be a positive integer starting at stage 1.\n"
        "- Assignments in the same stage run in parallel; later stages receive earlier-stage worker results.\n"
        "- Omit stage only for backwards-compatible stage 1 assignments.\n"
        "- Assign only listed workers.\n"
        f"{capability_rule}"
        "- Use zero assignments only if no worker is useful."
        f"{route_rule}"
        "\n- Optional tool gates: set domain, capabilities, and/or max_risk_class so "
        "CandidateSetGate can narrow .brigade/tools.toml before the worker runs; "
        "an empty admissible set fails the step for replanning."
        f"{no_write_rule}"
        f"{policy}"
    )
    return _prepend_optional_briefs(prompt, code_graph=code_graph, drift_impact=drift_impact, evidence=evidence)
