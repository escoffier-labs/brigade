"""Bounded cross-model orchestration for `brigade run`."""

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

from . import agents
from . import codex_appserver
from . import context_eval
from . import evidence_brief as evidence_brief_mod
from . import fleet_client
from . import graphtrail_delta
from . import causal_receipt
from . import localio
from . import message_envelope
from . import proc, receipt_schema, runguard
from . import run_control
from . import run_checkpoint
from . import run_budget
from . import run_events
from . import run_journal
from . import run_lifecycle
from . import run_projector
from . import run_shadow
from . import seat_health
from . import seat_health_policy
from . import verification_contract
from .result_integrity import validate_final_output
from .run_receipts import (
    agent_result_from_worker as _agent_result_from_worker,
    agent_result_payload as _agent_result_payload,
    assignment_payload as _assignment_payload,
    worker_payload as _worker_payload,
    write_agent_logs as _write_agent_logs,
    write_worker_logs as _write_worker_logs,
)
from .run_transport import Assignment, WorkerResult, dag_cycle_members
from .roster import Agent, Roster, is_cli_allowed, read_only_capability_error, timeout_for, workers
from .route_catalog import RouteBrief, route_brief, uncovered_stages, unknown_covers
from .route_policy import (
    RoutePolicyDecision,
    direct_worker_skill_ids,
    planner_skill_policy_section,
    validate_plan_skill_bindings,
    worker_skill_policy_constraint,
)

CODE_GRAPH_HEADING = "## Code graph context (GraphTrail, read-only)"
CODE_GRAPH_LIMIT = 4000
DRIFT_IMPACT_HEADING = "## Upstream drift impact (Upstream Drift + GraphTrail, read-only)"
DRIFT_IMPACT_LIMIT = 4000
BRIEF_BUDGET_BYTES = 6000
NOOP_DETAIL = "no-op"

# Journal authority is the default for every new run (issue #568 slice 11).
# Enrollment stays durable and per-run: existing runs are classified only from
# their stored run.json fields, so legacy snapshot-only runs are never migrated
# by a later Brigade release or environment change.
_AUTHORITY_REQUEST_FIELD = "run_journal_authority_requested"


# A plan-mode seat has no write tool, so any file it tries to create fails, and a
# failed write is what invites harness hooks to hijack the seat's final message
# (#518). Say the quiet part in the prompt: the plan lives in the reply, nowhere else.
NO_PLAN_FILE_RULE = (
    "- Do not write, create, or edit any file, including a plan, design, or context file. "
    "This seat runs with every write tool hidden: the write fails, and the failure can "
    "replace your plan with tool or hook commentary. The plan belongs in this reply only."
)

# The corrective turn is the last chance before the run dies on an unparsable
# plan, so it restates the output contract instead of only naming the parse error.
PLAN_JSON_ONLY_RULE = (
    "Reply with the JSON plan object and nothing else: no prose, no preamble, no explanation, "
    "no tool-failure or hook commentary, nothing before or after the object. "
    'If no worker is useful, reply with exactly {"assignments": []}.'
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


@dataclass(frozen=True)
class FleetModelPolicyResolution:
    roster: Roster
    receipt: dict[str, object]
    error: str | None = None


def _model_lease_lifecycle(function: Callable[..., int]) -> Callable[..., int]:
    """Acquire all admitted seat capacities from one startup snapshot, then release them."""

    def wrapped(task: str, roster: Roster, *args: Any, **kwargs: Any) -> int:
        worker = kwargs.get("worker")
        model_override = kwargs.get("model_override")
        snapshot = fleet_client.load_model_policy_snapshot()
        resolution = resolve_fleet_model_policy(roster, worker=worker, model_override=model_override, snapshot=snapshot)
        leases: list[fleet_client.ModelLeaseDecision] = []
        lease_error: str | None = None
        if resolution.error is None and snapshot.get("state") == "authoritative":
            for seat, agent in resolution.roster.agents.items():
                decision = fleet_client.acquire_model_lease(
                    seat,
                    agents.model_policy_provider(agent.cli or ""),
                    agents.model_policy_model(agent.cli or "", agent.model),
                )
                if not decision.granted:
                    lease_error = f"fleet model policy denied seat {seat!r}: {decision.reason}"
                    break
                leases.append(decision)
        kwargs["model_policy_snapshot"] = snapshot
        kwargs["model_lease_error"] = lease_error
        try:
            return function(task, roster, *args, **kwargs)
        finally:
            for decision in leases:
                if decision.lease_id is not None and decision.holder is not None:
                    fleet_client.release_model_lease(decision.lease_id, holder=decision.holder)

    return wrapped


def resolve_fleet_model_policy(
    roster: Roster,
    *,
    worker: str | None = None,
    model_override: str | None = None,
    snapshot: Mapping[str, Any] | None = None,
) -> FleetModelPolicyResolution:
    """Resolve one immutable Fleet Hub policy snapshot into an effective roster."""
    effective = roster
    if model_override is not None:
        cleaned_override = model_override.strip()
        if worker is None:
            return FleetModelPolicyResolution(
                roster=roster,
                receipt={"state": "invalid", "authoritative": False, "models": [], "decisions": []},
                error="--model requires --worker so the overridden seat is unambiguous",
            )
        agent = roster.agents.get(worker)
        if agent is None:
            return FleetModelPolicyResolution(
                roster=roster,
                receipt={"state": "invalid", "authoritative": False, "models": [], "decisions": []},
                error=f"unknown worker: {worker}",
            )
        if not cleaned_override:
            return FleetModelPolicyResolution(
                roster=roster,
                receipt={"state": "invalid", "authoritative": False, "models": [], "decisions": []},
                error="--model must be a non-empty model identifier",
            )
        if agent.cli is None or not agents.supports_model_pinning(agent.cli):
            cli = agent.cli or "unconfigured"
            return FleetModelPolicyResolution(
                roster=roster,
                receipt={"state": "invalid", "authoritative": False, "models": [], "decisions": []},
                error=f"seat {worker!r} uses {cli!r}, which does not support a per-run model override",
            )
        updated_agents = dict(roster.agents)
        updated_agents[worker] = replace(agent, model=cleaned_override)
        effective = replace(roster, agents=updated_agents)

    raw_snapshot = dict(snapshot) if snapshot is not None else fleet_client.load_model_policy_snapshot()
    state = raw_snapshot.get("state") if isinstance(raw_snapshot.get("state"), str) else "unavailable"
    raw_models = raw_snapshot.get("models")
    models = [dict(row) for row in raw_models if isinstance(row, Mapping)] if isinstance(raw_models, list) else []
    receipt: dict[str, object] = {
        "schema": "brigade.fleet_model_policy.v1",
        "state": state,
        "authoritative": state == "authoritative",
        "models": models,
        "decisions": [],
    }
    if model_override is not None:
        receipt["model_override"] = model_override.strip()
        receipt["model_override_seat"] = worker
    if state == "unconfigured":
        return FleetModelPolicyResolution(roster=effective, receipt=receipt)
    if state == "unavailable":
        return FleetModelPolicyResolution(
            roster=effective,
            receipt=receipt,
            error="fleet model policy hub is unavailable; refusing new dispatch",
        )
    if state == "auth-failed":
        return FleetModelPolicyResolution(
            roster=effective,
            receipt=receipt,
            error="fleet model policy could not authenticate this node; refusing new dispatch",
        )
    if state != "authoritative":
        return FleetModelPolicyResolution(roster=effective, receipt=receipt)

    seat_rows = {row.get("seat"): row for row in models if isinstance(row.get("seat"), str) and row.get("seat")}
    disabled_providers = {
        row.get("provider")
        for row in models
        if row.get("seat") is None and row.get("enabled") is False and isinstance(row.get("provider"), str)
    }
    kept_agents = dict(effective.agents)
    decisions: list[dict[str, object]] = []
    denied: dict[str, str] = {}
    for seat, agent in effective.agents.items():
        provider = agents.model_policy_provider(agent.cli or "")
        model = agents.model_policy_model(agent.cli or "", agent.model)
        row = seat_rows.get(seat)
        decision: dict[str, object] = {
            "kind": "fleet-model-policy",
            "seat": seat,
            "requested_provider": provider,
            "requested_model": model,
        }
        if provider in disabled_providers:
            outcome = "provider-disabled"
            detail = f"provider {provider!r} is disabled"
        elif row is None:
            outcome = "omitted"
            detail = "seat is absent from the authoritative registry"
        else:
            policy_provider = row.get("provider")
            policy_model = row.get("model")
            decision["policy_provider"] = policy_provider
            decision["policy_model"] = policy_model
            decision["policy_enabled"] = row.get("enabled") is True
            if policy_provider != provider or policy_model != model:
                outcome = "mismatch"
                detail = f"requested {provider}/{model}, registry allows {policy_provider}/{policy_model}"
            elif row.get("enabled") is not True:
                outcome = "disabled"
                detail = "registry entry is disabled"
            else:
                outcome = "enabled"
                detail = "exact seat/provider/model entry is enabled"
        decision["outcome"] = outcome
        decision["detail"] = detail
        decisions.append(decision)
        if outcome != "enabled":
            denied[seat] = detail
            if seat != effective.orchestrator:
                kept_agents.pop(seat, None)

    receipt["decisions"] = decisions
    required_seat = worker or effective.orchestrator
    if required_seat in denied:
        detail = denied[required_seat]
        return FleetModelPolicyResolution(
            roster=replace(effective, agents=kept_agents),
            receipt=receipt,
            error=f"fleet model policy denied seat {required_seat!r}: {detail}",
        )
    routing = tuple(
        {
            "requested_seat": decision["seat"],
            "effective_seat": decision["seat"] if decision["outcome"] == "enabled" else None,
            "outcome": decision["outcome"],
            "typed_cause": "fleet-model-policy",
            "requested_provider": decision["requested_provider"],
            "requested_model": decision["requested_model"],
        }
        for decision in decisions
    )
    return FleetModelPolicyResolution(
        roster=replace(effective, agents=kept_agents, seat_routing=(*effective.seat_routing, *routing)),
        receipt=receipt,
    )


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
    evidence: EvidenceBrief | None = None,
    route: RouteBrief | None = None,
    no_file_writes: bool = False,
    skill_policy: RoutePolicyDecision | None = None,
) -> str:
    worker_lines = "\n".join(
        f"- {agent.name}: cli={agent.cli}; "
        + (f"read_only_capable={str(agent.read_only_capable).lower()}; " if read_only else "")
        + f"role={agent.role}"
        for agent in workers(roster)
    )
    if not worker_lines:
        worker_lines = "- no workers configured"

    note = f"\nCorrection needed: {corrective_note}\n" if corrective_note else ""
    policy = f"\n\n{_read_only_rules()}\n" if read_only else ""
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


def make_run_dir(base: Path, now: datetime | None = None, *, workspace: Path | None = None) -> Path:
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%d-%H%M%S")
    local_id = f"{stamp}-{uuid4().hex[:8]}"
    from . import node as node_mod
    from . import run_id as run_id_mod

    inferred = workspace if workspace is not None else node_mod.infer_workspace_from_runs_dir(base)
    try:
        identity = node_mod.ensure_identity(inferred)
    except node_mod.NodeIdentityError:
        return base / local_id
    return base / run_id_mod.format_run_id(identity.short_id, local_id)


def _resolve_authority_state(run_dir: Path) -> str:
    """Return one of 'legacy', 'authority-requested', 'authoritative'.

    'legacy' only when no durable authority request is present
    (run_journal_authority_requested is not true on run.json). 'authority-
    requested' only when the durable authority request is true and NONE of the
    four projection metadata fields is present on run.json (projector_version,
    journal_present, journal_last_sequence, journal_last_event_digest). Once
    ANY of the four projection metadata fields is present, the run is past the
    not-yet-authoritative fallback: incomplete fields, a stale or wrong
    projector version, a bounded read failure, a chain failure, a cursor
    failure, or a digest failure all raise a bounded LifecycleJournalError and
    never downgrade. 'authoritative' only when run.json carries all four
    journal-metadata fields with projector_version == run_projector.
    PROJECTOR_VERSION and its saved journal_last_sequence /
    journal_last_event_digest verifies against the event at that sequence in a
    bounded, chain-valid journal prefix. A bound failure or chain error on a
    run with NO projection metadata fields returns 'authority-requested' (the
    not-yet-authoritative fallback); it never authorizes projection.
    """
    run_json = run_dir / "run.json"
    try:
        raw = run_json.read_bytes()
    except FileNotFoundError:
        return "legacy"
    try:
        meta = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        meta = None
    if not isinstance(meta, dict) or meta.get(_AUTHORITY_REQUEST_FIELD) is not True:
        return "legacy"
    metadata_fields = (
        "projector_version",
        "journal_present",
        "journal_last_sequence",
        "journal_last_event_digest",
    )
    present = [name for name in metadata_fields if name in meta]
    if not present:
        return "authority-requested"
    # Past the not-yet-authoritative fallback: no downgrade is allowed.
    missing = [name for name in metadata_fields if name not in meta]
    if missing:
        raise run_lifecycle.LifecycleJournalError(
            run_events._bound(f"authority run missing projection metadata: {sorted(missing)}")
        )
    if meta.get("projector_version") != run_projector.PROJECTOR_VERSION:
        raise run_lifecycle.LifecycleJournalError(run_events._bound("authority run projector version is not current"))
    # Finding 1: once any projection metadata exists, journal_present must be
    # exactly True. A false or wrong-typed value is invalid authoritative
    # metadata and must fail closed before the checkpoint/lifecycle append.
    if meta.get("journal_present") is not True:
        raise run_lifecycle.LifecycleJournalError(run_events._bound("authority run journal_present is not true"))
    saved_seq = meta.get("journal_last_sequence")
    saved_digest = meta.get("journal_last_event_digest")
    if isinstance(saved_seq, bool) or not isinstance(saved_seq, int) or saved_seq < 1:
        raise run_lifecycle.LifecycleJournalError(
            run_events._bound("authority run saved journal_last_sequence is invalid")
        )
    if not isinstance(saved_digest, str):
        raise run_lifecycle.LifecycleJournalError(
            run_events._bound("authority run saved journal_last_event_digest is invalid")
        )
    journal_path = run_lifecycle._journal_path(run_dir)
    try:
        report = run_journal.read_journal_bounded(journal_path)
    except (OSError, run_journal.RunJournalError) as exc:
        raise run_lifecycle._bound_journal_failure(exc) from exc
    if report.partial_tail is not None or report.chain_errors:
        raise run_lifecycle.LifecycleJournalError(run_events._bound("authority run journal is not chain-valid"))
    events = report.events
    # Finding 2: the bounded verified prefix must reject ANY event whose run_id
    # differs from run_dir.name, not only the event at the saved cursor. A
    # chain-valid journal that mixes in a foreign-run-id event is not
    # exclusively this run's and must fail closed.
    for event in events:
        if event.run_id != run_dir.name:
            raise run_lifecycle.LifecycleJournalError(
                run_events._bound("authority run journal contains an event with a foreign run_id")
            )
    if saved_seq > len(events):
        raise run_lifecycle.LifecycleJournalError(
            run_events._bound("authority run saved sequence is beyond the journal tail")
        )
    event = events[saved_seq - 1]
    if event.event_digest != saved_digest:
        raise run_lifecycle.LifecycleJournalError(
            run_events._bound("authority run saved cursor does not verify against the journal")
        )
    return "authoritative"


_PROJECTION_METADATA_FIELDS = (
    "projector_version",
    "journal_present",
    "journal_last_sequence",
    "journal_last_event_digest",
)


def _payload_requests_authority(payload: dict[str, object]) -> bool:
    """Cheap payload classification: does the incoming run.json payload carry
    any authority signal that requires filesystem authority resolution?

    Returns True when the payload carries the durable authority request field
    with ANY value (present, including an explicit ``False`` which is a forged
    downgrade attempt) OR any of the four projection metadata fields
    (projector_version, journal_present, journal_last_sequence,
    journal_last_event_digest). Only legacy and lifecycle-only writes (the
    authority request field ABSENT and all four projection metadata fields
    ABSENT) return False so ``_write_json`` can skip the existing-run.json
    authority read entirely. No-downgrade is preserved: any incoming durable
    authority request (true or false) or projected metadata still resolves
    and validates authority through ``_resolve_authority_state``.
    """
    if _AUTHORITY_REQUEST_FIELD in payload:
        return True
    return any(field in payload for field in _PROJECTION_METADATA_FIELDS)


def _genuine_journal_ahead(run_dir: Path) -> bool:
    """Return True only for one verified checkpoint/event pair ahead.

    ``check_projection_readiness`` reports ``REASON_JOURNAL_AHEAD`` whenever
    the artifact's ``last_compared_sequence``/``last_compared_event_digest``
    does not match the journal tail, which conflates a real committed
    checkpoint/event pair whose shadow step has not run yet with a forged,
    stale, or multi-pair cursor. Catch-up is safe only when the artifact cursor
    verifies against an actual event in a clean journal and the tail is exactly
    one structurally covered pair ahead.
    """
    artifact_path = run_shadow.shadow_artifact_path(run_dir)
    try:
        data = json.loads(artifact_path.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    baseline = run_shadow._verify_stale_baseline(
        run_dir,
        run_dir.name,
        data.get("last_compared_sequence"),
        data.get("last_compared_event_digest"),
    )
    if baseline is None:
        return False
    try:
        journal_report = run_journal.read_journal_bounded(run_lifecycle._journal_path(run_dir))
    except (OSError, run_journal.RunJournalError):
        return False
    if journal_report.partial_tail is not None or journal_report.chain_errors:
        return False
    if not journal_report.events:
        return False
    tail_seq = journal_report.events[-1].sequence
    return run_shadow._checkpoint_status_write_gap(run_dir, prior_seq=baseline[0], tail_seq=tail_seq)


def _authoritative_prior_decision(run_dir: Path, prior_snapshot: dict[str, object]) -> str:
    """Resolve the prior authority gate for an authoritative run.

    A ready prior returns immediately. A prior that is not ready may catch up
    exactly one verified checkpoint/event pair by recording parity against the
    persisted pre-write snapshot and then requiring readiness to become fully
    green. Forged cursors, multi-pair gaps, mismatches, errors, and a catch-up
    that does not restore readiness all raise before a new pair is appended.
    """
    report = run_shadow.check_projection_readiness(run_dir)
    if report.ready:
        return "ready"
    if report.reasons == (run_shadow.REASON_JOURNAL_AHEAD,) and _genuine_journal_ahead(run_dir):
        run_shadow.record_shadow_comparison(run_dir, prior_snapshot)
        caught_up = run_shadow.check_projection_readiness(run_dir)
        if caught_up.ready:
            artifact_path = run_shadow.shadow_artifact_path(run_dir)
            try:
                artifact = json.loads(artifact_path.read_text())
            except (OSError, ValueError) as exc:
                raise run_lifecycle.LifecycleJournalError(
                    run_events._bound("authoritative run prior catch-up evidence is unreadable")
                ) from exc
            mismatches = artifact.get("mismatches") if isinstance(artifact, dict) else None
            errors = artifact.get("errors") if isinstance(artifact, dict) else None
            if (
                not isinstance(artifact, dict)
                or artifact.get("last_outcome") != run_shadow.OUTCOME_MATCH
                or isinstance(mismatches, bool)
                or not isinstance(mismatches, int)
                or mismatches != 0
                or isinstance(errors, bool)
                or not isinstance(errors, int)
                or errors != 0
                or artifact.get("last_error_category") is not None
            ):
                raise run_lifecycle.LifecycleJournalError(
                    run_events._bound("authoritative run prior catch-up evidence is not a clean match")
                )
            return "ready"
        raise run_lifecycle.LifecycleJournalError(
            run_events._bound("authoritative run prior catch-up not ready: " + ",".join(sorted(caught_up.reasons)))
        )
    raise run_lifecycle.LifecycleJournalError(
        run_events._bound("authoritative run prior gate not ready: " + ",".join(sorted(report.reasons)))
    )


def _project_authority_candidate(path: Path, run_dir: Path, candidate: dict[str, object]) -> None:
    """Re-read the bounded journal, verify a clean chain, project the
    candidate, and atomically replace run.json with the projected bytes."""
    try:
        journal_report = run_journal.read_journal_bounded(run_lifecycle._journal_path(run_dir))
    except (OSError, run_journal.RunJournalError) as exc:
        raise run_lifecycle._bound_journal_failure(exc) from exc
    if journal_report.partial_tail is not None or journal_report.chain_errors:
        raise run_lifecycle.LifecycleJournalError(run_events._bound("authority write journal is not chain-valid"))
    try:
        projection = run_projector.project_run_snapshot(candidate, journal_report.events, journal_present=True)
    except run_projector.ProjectionError as exc:
        raise run_lifecycle.LifecycleJournalError(run_events._bound(exc.diagnostic)) from exc
    localio.write_text_atomic(path, projection.to_bytes().decode("utf-8"))


def _write_json_inner(path: Path, payload: object) -> None:
    # run.json is polled by `brigade runs watch/steer/interrupt` while the run
    # rewrites it, so the write must be atomic or a concurrent reader can
    # observe a truncated file.
    if path.name == "run.json" and isinstance(payload, dict):
        status = payload.get("status")
        if isinstance(status, str) and status:
            transition_status = status
            prior_status: str | None = None
            prior_kind: str | None = None
            if path.is_file():
                try:
                    prior_snapshot = json.loads(path.read_bytes())
                except (OSError, ValueError, UnicodeDecodeError, RecursionError):
                    prior_snapshot = None
                if isinstance(prior_snapshot, dict):
                    raw_prior_status = prior_snapshot.get("status")
                    if isinstance(raw_prior_status, str) and raw_prior_status:
                        prior_status = raw_prior_status
                    raw_prior_kind = prior_snapshot.get("kind")
                    if isinstance(raw_prior_kind, str) and raw_prior_kind:
                        prior_kind = raw_prior_kind
            kind = payload.get("kind") if isinstance(payload.get("kind"), str) else prior_kind
            approval_reference = payload.get("approval_reference")
            if status == "running" and isinstance(approval_reference, Mapping):
                decision_state = approval_reference.get("decision_state")
                if decision_state == "pending":
                    # Keep the compatibility snapshot on a status understood
                    # by the previous reader while journaling the intentional
                    # pause.
                    transition_status = "paused"
                elif decision_state in {"approved", "rejected", "held", "consumed"}:
                    # Approval facts own these state changes. Treat their
                    # compatibility snapshot refreshes as neutral so a
                    # same-status write cannot leave a checkpoint promising a
                    # run.resumed event that record_lifecycle_transition
                    # correctly suppresses.
                    transition_status = "approval-state-refresh"
            elif status == "running" and kind == "research":
                # Research reopen must not emit approval-gated run.resumed.
                # Only a true terminal→running recovery is recorded; same-
                # status research running refreshes stay status-neutral.
                _research_terminal = {
                    "failed",
                    "cancelled",
                    "canceled",
                    "completed",
                    "ok",
                    "timeout",
                    "incomplete",
                    "dry-run",
                }
                if prior_status in _research_terminal:
                    transition_status = "research-reopened"
                else:
                    transition_status = "research-state-refresh"
            run_dir = path.parent
            workspace = runguard.resolve_run_lock_workspace(payload, run_dir)
            # Cheap payload classification BEFORE the filesystem authority
            # resolution. Legacy and lifecycle-only status writes (the durable
            # authority request field ABSENT and all four projection metadata
            # fields ABSENT in the incoming payload) never read/parse the
            # existing run.json only to resolve authority, so an unreadable
            # legacy run.json cannot make a status write fail. No-downgrade is
            # preserved: an incoming durable authority request of ANY value
            # (including an explicit False, which is a forged downgrade
            # attempt) or any projection metadata field still resolves and
            # validates authority through _resolve_authority_state.
            if _payload_requests_authority(payload):
                authority_state = _resolve_authority_state(run_dir)
            else:
                authority_state = "legacy"
            # Construct the canonical legacy candidate and the projection base
            # exactly once. The base strips the four journal-derived metadata
            # fields; the same object is the base-stripped checkpoint body
            # whenever the resolved state is authority-requested or
            # authoritative.
            candidate = copy.deepcopy(payload)
            for derived in (
                "projector_version",
                "journal_present",
                "journal_last_sequence",
                "journal_last_event_digest",
            ):
                candidate.pop(derived, None)
            encoded_candidate = json.dumps(candidate, indent=2, sort_keys=True) + "\n"
            base_bytes = encoded_candidate.encode("utf-8")
            body_kind = (
                run_checkpoint._BODY_KIND_BASE_STRIPPED
                if authority_state in {"authority-requested", "authoritative"}
                else None
            )
            # Prior authority gate (authoritative only): fail closed BEFORE
            # the checkpoint/lifecycle append when the prior committed state
            # is not ready and not exactly one recoverable checkpoint/event
            # pair ahead. The catch-up uses the persisted pre-write snapshot,
            # never the incoming next status. Legacy and authority-requested
            # runs skip this gate.
            if authority_state == "authoritative":
                try:
                    prior_snapshot = json.loads(path.read_bytes())
                except (OSError, ValueError, UnicodeDecodeError) as exc:
                    raise run_lifecycle.LifecycleJournalError(
                        run_events._bound("authoritative prior snapshot is unreadable")
                    ) from exc
                if not isinstance(prior_snapshot, dict):
                    raise run_lifecycle.LifecycleJournalError(
                        run_events._bound("authoritative prior snapshot is not an object")
                    )
                _authoritative_prior_decision(run_dir, prior_snapshot)
            # Exact order: activate the journal, publish the recovery
            # checkpoint, append the lifecycle status transition, record the
            # shadow parity, consult the post-parity readiness veto, then
            # atomically replace run.json. A CheckpointError from
            # write_checkpoint fails BEFORE the lifecycle append and BEFORE
            # run.json replacement. The prior gate above raises BEFORE any
            # append for an authoritative run with a real prior defect.
            run_lifecycle.prepare_lifecycle_journal(
                run_dir,
                workspace=workspace,
                incoming_snapshot=payload,
            )
            run_checkpoint.write_checkpoint(
                run_dir,
                base_bytes,
                workspace=workspace,
                paired_event_type=run_lifecycle.STATUS_EVENT_TYPE.get(transition_status),
                body_kind=body_kind,
            )
            run_lifecycle.record_lifecycle_transition(
                run_dir,
                status=transition_status,
                # The receipt payload identifies the lock workspace
                # (lock_workspace or cwd); the run directory layout does not.
                workspace=workspace,
                incoming_snapshot=payload,
            )
            run_shadow.record_shadow_comparison(run_dir, payload)
            if authority_state == "legacy":
                # Legacy runs skip the veto and write the legacy body (slice 5).
                localio.write_text_atomic(path, encoded_candidate)
                return
            readiness = run_shadow.check_projection_readiness(run_dir)
            if authority_state == "authority-requested":
                # Authority-requested projection uses the POST-parity readiness
                # report (a ready first comparison projects that same write),
                # never the pre-parity decision. A not-ready gate falls back to
                # the legacy body until a ready first comparison catches up.
                if readiness.ready:
                    _project_authority_candidate(path, run_dir, candidate)
                else:
                    localio.write_text_atomic(path, encoded_candidate)
                return
            # Authoritative writes require both the prior gate and this
            # post-parity gate to be fully ready. No comparison-gap override
            # survives: the only recoverable lag was consumed before append.
            if readiness.ready:
                _project_authority_candidate(path, run_dir, candidate)
                return
            raise run_lifecycle.LifecycleJournalError(
                run_events._bound("authoritative run gate not ready: " + ",".join(sorted(readiness.reasons)))
            )
    localio.write_text_atomic(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    if path.name == "run.json" and isinstance(payload, dict):
        run_shadow.record_shadow_comparison(path.parent, payload)


def _write_json(path: Path, payload: object) -> None:
    """Write JSON, serializing every run.json checkpoint/event transaction."""
    if (
        path.name == "run.json"
        and isinstance(payload, dict)
        and isinstance(payload.get("status"), str)
        and payload["status"]
    ):
        with run_lifecycle.checkpoint_event_pair():
            _write_json_inner(path, payload)
        return
    _write_json_inner(path, payload)


def _revision_contains(revisions_dir: Path, projection: bytes) -> bool:
    """Return whether a preserved sidecar revision matches ``projection``."""
    if not revisions_dir.is_dir():
        return False
    for revision_path in revisions_dir.glob("*.json"):
        try:
            if revision_path.read_bytes() == projection:
                return True
        except OSError:
            continue
    return False


def _supports_directory_fsync() -> bool:
    return os.name == "posix"


def _fsync_directory(path: Path) -> None:
    """Persist directory entries where the platform supports directory fsync."""
    if not _supports_directory_fsync():
        return
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _ensure_directory_durable(path: Path) -> None:
    """Create missing directory levels and persist each new parent entry."""
    missing: list[Path] = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir()
        except FileExistsError:
            if not directory.is_dir():
                raise
        else:
            _fsync_directory(directory.parent)


def _write_new_revision(revisions_dir: Path, encoded: bytes) -> None:
    """Exclusively create the next immutable sidecar revision."""
    _ensure_directory_durable(revisions_dir)
    sequence = max(
        (int(path.stem) for path in revisions_dir.glob("*.json") if path.stem.isascii() and path.stem.isdecimal()),
        default=0,
    )
    while True:
        sequence += 1
        revision_path = revisions_dir / f"{sequence:06d}.json"
        writer_acquired = False
        try:
            fd = os.open(revision_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            writer_acquired = True
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            _fsync_directory(revisions_dir)
        except FileExistsError:
            continue
        except BaseException:
            if writer_acquired:
                revision_path.unlink(missing_ok=True)
            raise
        return


def write_sidecar_revision(run_dir: Path, filename: str, payload: object) -> None:
    """Append an immutable sidecar revision, then update its compatibility file."""
    projection_path = run_dir / filename
    revisions_dir = run_dir / "revisions" / Path(filename).stem
    if projection_path.exists():
        legacy_projection = projection_path.read_bytes()
        json.loads(legacy_projection)
        if not _revision_contains(revisions_dir, legacy_projection):
            _write_new_revision(revisions_dir, legacy_projection)
    _write_new_revision(revisions_dir, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    _write_json(projection_path, payload)


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
    outcome_id: str | None = None,
    outcome_digest: str | None = None,
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
    if output_dir is not None or outcome_id:
        parent_kind = "outcome" if outcome_id else "run"
        parent_id = outcome_id if outcome_id else output_dir.name
        localio.write_json(
            causal_receipt.handoff_sidecar_path(path),
            causal_receipt.recorded_handoff(
                handoff_id=path.stem,
                parent_kind=parent_kind,
                parent_id=parent_id,
                parent_digest=outcome_digest,
            ),
        )
    return path


def _read_only_rules() -> str:
    return (
        "READ-ONLY MODE:\n"
        "- Do not modify files.\n"
        "- Do not install packages, change configuration, commit, push, or call external write APIs.\n"
        "- You may inspect, reason, summarize, and recommend exact next steps.\n"
        "- If a task appears to require changes, describe the proposed changes instead of making them."
    )


def parse_plan(
    text: str,
    roster: Roster,
    *,
    read_only: bool = False,
    skill_policy: RoutePolicyDecision | None = None,
) -> list[Assignment]:
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
        if read_only:
            capability_error = read_only_capability_error(roster.agents[worker])
            if capability_error is not None:
                raise ValueError(capability_error)
        if not isinstance(subtask, str) or not subtask.strip():
            raise ValueError("assignment.task must be a non-empty string")
        raw_covers = item.get("covers", [])
        if not isinstance(raw_covers, list) or any(not isinstance(c, str) or not c.strip() for c in raw_covers):
            raise ValueError("assignment.covers must be a list of non-empty strings")
        covers = tuple(dict.fromkeys(c.strip() for c in raw_covers))
        raw_selected = item.get("selected_skill_ids", [])
        if raw_selected is None:
            raw_selected = []
        if not isinstance(raw_selected, list) or any(
            not isinstance(skill_id, str) or not skill_id.strip() for skill_id in raw_selected
        ):
            raise ValueError("assignment.selected_skill_ids must be a list of non-empty strings when set")
        selected_skill_ids = tuple(dict.fromkeys(skill_id.strip() for skill_id in raw_selected))
        raw_domain = item.get("domain")
        domain: str | None = None
        if raw_domain is not None:
            if not isinstance(raw_domain, str) or not raw_domain.strip():
                raise ValueError("assignment.domain must be a non-empty string when set")
            domain = raw_domain.strip()
        raw_capabilities = item.get("capabilities", [])
        if raw_capabilities is None:
            raw_capabilities = []
        if not isinstance(raw_capabilities, list) or any(
            not isinstance(capability, str) or not capability.strip() for capability in raw_capabilities
        ):
            raise ValueError("assignment.capabilities must be a list of non-empty strings when set")
        capabilities = tuple(dict.fromkeys(capability.strip() for capability in raw_capabilities))
        raw_max_risk = item.get("max_risk_class")
        max_risk_class: str | None = None
        if raw_max_risk is not None:
            if not isinstance(raw_max_risk, str) or not raw_max_risk.strip():
                raise ValueError("assignment.max_risk_class must be a non-empty string when set")
            max_risk_class = raw_max_risk.strip()
            from .tools_cmd.constants import RISK_CLASSES

            if max_risk_class not in RISK_CLASSES:
                raise ValueError("assignment.max_risk_class must be one of: " + ", ".join(RISK_CLASSES))
        assignment = Assignment(
            worker=worker,
            task=subtask.strip(),
            stage=stage,
            covers=covers,
            selected_skill_ids=selected_skill_ids,
            domain=domain,
            capabilities=capabilities,
            max_risk_class=max_risk_class,
        )
        key = (assignment.stage, assignment.worker, assignment.task)
        if key not in seen:
            assignments.append(assignment)
            seen.add(key)
            stage_counts[assignment.stage] = stage_counts.get(assignment.stage, 0) + 1
        elif covers or selected_skill_ids or domain or capabilities or max_risk_class:
            # Duplicates merge their covers instead of dropping them, so a plan
            # that tags the same assignment twice still counts as covering both.
            for index, existing in enumerate(assignments):
                if (existing.stage, existing.worker, existing.task) == key:
                    merged = tuple(dict.fromkeys(existing.covers + covers))
                    merged_skills = tuple(dict.fromkeys(existing.selected_skill_ids + selected_skill_ids))
                    merged_caps = tuple(dict.fromkeys(existing.capabilities + capabilities))
                    merged_domain = domain or existing.domain
                    merged_risk = max_risk_class or existing.max_risk_class
                    assignments[index] = replace(
                        existing,
                        covers=merged,
                        selected_skill_ids=merged_skills,
                        domain=merged_domain,
                        capabilities=merged_caps,
                        max_risk_class=merged_risk,
                    )
                    break

    for stage, count in stage_counts.items():
        if count > roster.max_workers:
            raise ValueError(f"plan has {count} assignments in stage {stage}, limit is {roster.max_workers}")
    validate_plan_skill_bindings(assignments, skill_policy)
    return sorted(assignments, key=lambda assignment: assignment.stage)


def _record_plan_attempt(
    attempts: list[dict[str, object]] | None,
    *,
    stage: str,
    result: agents.AgentResult,
    parsed: bool = False,
    parse_error: str | None = None,
    coverage_missing: list[str] | None = None,
    unknown_covers: list[str] | None = None,
) -> None:
    if attempts is None:
        return
    payload: dict[str, object] = {
        "stage": stage,
        "ok": result.ok,
        "parsed": parsed,
        "detail": result.detail,
        "text": result.text,
        "timed_out": result.timed_out,
        "status": result.status,
    }
    if result.failure_phase is not None:
        payload["failure_phase"] = result.failure_phase
    if result.failure_kind is not None:
        payload["failure_kind"] = result.failure_kind
    if parse_error is not None:
        payload["parse_error"] = parse_error
    if coverage_missing:
        payload["coverage_missing"] = list(coverage_missing)
    if unknown_covers:
        payload["unknown_covers"] = list(unknown_covers)
    attempts.append(payload)


def _orchestrator_failure_detail(
    result: agents.AgentResult,
    *,
    seat: str,
    cli: str | None,
    transport: str,
) -> str:
    if cli != "codex":
        return result.detail or ""
    # Prefer stderr: it still carries the raw codex banner after run_agent maps detail.
    source = result.stderr or result.detail or ""
    mapped = agents.codex_stdin_hang_detail(source, seat=seat, transport=transport)
    if mapped is not None:
        return mapped
    if "blocked waiting for stdin" in (result.detail or "").lower():
        return (
            agents.codex_stdin_hang_detail(
                "Reading additional input from stdin",
                seat=seat,
                transport=transport,
            )
            or result.detail
        )
    return result.detail or ""


def _run_orchestrator(
    roster: Roster,
    prompt: str,
    cwd: Path | None = None,
    read_only: bool = False,
    sandbox_read_only: bool | None = None,
    sandbox: str | None = None,
    codex_transport: str | None = None,
    process_registry: proc.ProcessRegistry | None = None,
) -> agents.AgentResult:
    orchestrator = roster.agents[roster.orchestrator]
    transport = codex_transport or roster.codex_transport
    if not is_cli_allowed(orchestrator.cli, roster):
        return agents.AgentResult(
            text="",
            ok=False,
            detail=f"{orchestrator.cli} is not allowed by limits.allow_models",
        )
    if orchestrator.cli.startswith("codex-cloud:"):
        # A cloud task returns a status report plus diff, not a plan or a
        # synthesis; the orchestrator seat must be a conversational CLI.
        return agents.AgentResult(
            text="",
            ok=False,
            detail="codex-cloud seats are workers only; pick a local CLI for the orchestrator",
        )
    cloudflare_detail = agents.cloudflare_ai_gateway_preflight_detail(orchestrator.model)
    if cloudflare_detail is not None:
        return agents.AgentResult(
            text="",
            ok=False,
            detail=cloudflare_detail,
            failure_phase="preflight",
            failure_kind="provider-config",
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
    if orchestrator.reasoning is not None:
        kwargs["reasoning"] = orchestrator.reasoning
    if orchestrator.env is not None:
        kwargs["env"] = dict(orchestrator.env)
    if orchestrator.command is not None:
        kwargs["command"] = orchestrator.command
    result = _call_with_process_registry(
        agents.run_agent,
        orchestrator.cli,
        prompt,
        process_registry=process_registry,
        **kwargs,
    )
    if not result.ok and orchestrator.cli == "codex":
        detail = _orchestrator_failure_detail(
            result,
            seat=roster.orchestrator,
            cli=orchestrator.cli,
            transport=transport,
        )
        if detail != result.detail:
            return agents.AgentResult(
                text=result.text,
                ok=False,
                detail=detail,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                requested_model=result.requested_model,
                reasoning=result.reasoning,
                transport=result.transport,
            )
    return result


def _call_with_process_registry(
    function: Callable[..., Any],
    *args: Any,
    process_registry: proc.ProcessRegistry | None,
    **kwargs: Any,
) -> Any:
    parameters = inspect.signature(function).parameters.values()
    accepts_registry = any(
        parameter.name == "process_registry" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )
    if accepts_registry:
        kwargs["process_registry"] = process_registry
    return function(*args, **kwargs)


def _coverage_missing(route: RouteBrief | None, assignments: list[Assignment]) -> list[str]:
    if route is None or not route.attached or not route.route:
        return []
    return uncovered_stages(route, assignments)


def _unknown_covers(route: RouteBrief | None, assignments: list[Assignment]) -> list[str]:
    if route is None or not route.attached or not route.route:
        return []
    return unknown_covers(route, assignments)


def _validate_plan_dependencies(route: RouteBrief | None, assignments: list[Assignment]) -> None:
    if route is None or not route.attached:
        return
    members = dag_cycle_members(assignments, route.dependencies)
    if members:
        raise ValueError(f"plan has a dependency cycle involving: {', '.join(members)}")


def _orchestrator_hides_write_tools(
    roster: Roster,
    *,
    read_only: bool,
    sandbox_read_only: bool | None,
    sandbox: str | None,
) -> bool:
    """True when the orchestrator seat launches without any file-write tool."""
    orchestrator = roster.agents.get(roster.orchestrator)
    if orchestrator is None:
        return False
    # _run_orchestrator resolves read-only the same way before building argv.
    effective_read_only = read_only if sandbox_read_only is None else sandbox_read_only
    return agents.hides_write_tools(orchestrator.cli, read_only=effective_read_only, sandbox=sandbox)


def _orchestrator_harness(roster: Roster) -> str | None:
    agent = roster.agents.get(roster.orchestrator)
    return agent.cli if agent is not None else None


def _emit_plan_request(
    prompt: str,
    roster: Roster,
    *,
    output_dir: Path | None,
) -> str:
    delivery = message_envelope.emit(
        prompt,
        kind="plan-request",
        producer="aboyeur.build_plan_prompt",
        from_seat=message_envelope.BRIGADE_SEAT,
        to_seat=roster.orchestrator,
        run_dir=output_dir,
        run_id=output_dir.name if output_dir is not None else None,
        assignment_id=message_envelope.default_assignment_id("plan-request"),
        session_harness=_orchestrator_harness(roster),
    )
    if not delivery.delivered:
        raise RuntimeError(delivery.reason or "plan-request rejected by provenance gate")
    return prompt


def _parse_gated_plan(
    text: str,
    roster: Roster,
    *,
    output_dir: Path | None,
    read_only: bool = False,
    skill_policy: RoutePolicyDecision | None = None,
) -> list[Assignment]:
    delivery = message_envelope.emit(
        text,
        kind="plan-result",
        producer="aboyeur.plan",
        from_seat=roster.orchestrator,
        to_seat=message_envelope.BRIGADE_SEAT,
        run_dir=output_dir,
        run_id=output_dir.name if output_dir is not None else None,
        assignment_id=message_envelope.default_assignment_id("plan-result"),
        session_harness=_orchestrator_harness(roster),
    )
    if not delivery.delivered:
        raise ValueError(delivery.reason or "plan-result rejected by provenance gate")
    return parse_plan(text, roster, read_only=read_only, skill_policy=skill_policy)


def _result_from_seat(result: WorkerResult) -> str:
    """Seat that actually produced the terminal text (fallback-aware)."""

    for attempt in result.attempts:
        if attempt.selected and attempt.worker:
            return attempt.worker
    return result.worker


def _admitted_result_output(
    result: WorkerResult,
    *,
    run_id: str | None = None,
    to_seat: str | None = None,
) -> str:
    env = result.provenance
    if not isinstance(env, dict):
        _legacy, display = message_envelope.synthesize_legacy_message_provenance()
        return display
    binding = message_envelope.message_binding(env)
    expected_message_id = binding["message_id"] if binding is not None else env.get("item_id")
    admission = message_envelope.admit_message(
        result.text,
        env,
        kind="worker-result",
        producer="run_transport.dispatch",
        run_id=run_id or message_envelope.IN_MEMORY_RUN_ID,
        message_id=expected_message_id if isinstance(expected_message_id, str) else None,
        assignment_id=message_envelope.assignment_id_for(result.worker, result.task),
        from_seat=_result_from_seat(result),
        to_seat=to_seat,
    )
    if not admission.delivered:
        return admission.display or admission.reason or message_envelope.LEGACY_MESSAGE_DISPLAY
    return message_envelope.wrap_message_body(result.text, env)


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
    evidence: EvidenceBrief | None = None,
    route: RouteBrief | None = None,
    skill_policy: RoutePolicyDecision | None = None,
    codex_transport: str | None = None,
    process_registry: proc.ProcessRegistry | None = None,
    output_dir: Path | None = None,
) -> list[Assignment]:
    transport = codex_transport or roster.codex_transport
    no_file_writes = _orchestrator_hides_write_tools(
        roster,
        read_only=read_only,
        sandbox_read_only=sandbox_read_only,
        sandbox=sandbox,
    )
    first = _call_with_process_registry(
        _run_orchestrator,
        roster,
        _emit_plan_request(
            build_plan_prompt(
                task,
                roster,
                read_only=read_only,
                code_graph=code_graph,
                drift_impact=drift_impact,
                evidence=evidence,
                route=route,
                no_file_writes=no_file_writes,
                skill_policy=skill_policy,
            ),
            roster,
            output_dir=output_dir,
        ),
        cwd=cwd,
        read_only=read_only,
        sandbox_read_only=sandbox_read_only,
        sandbox=sandbox,
        codex_transport=transport,
        process_registry=process_registry,
    )
    if not first.ok:
        _record_plan_attempt(attempts, stage="initial", result=first)
        raise RuntimeError(f"orchestrator failed during plan: {first.detail}")
    try:
        assignments = _parse_gated_plan(
            first.text,
            roster,
            output_dir=output_dir,
            read_only=read_only,
            skill_policy=skill_policy,
        )
        _validate_plan_dependencies(route, assignments)
        _record_plan_attempt(
            attempts,
            stage="initial",
            result=first,
            parsed=True,
            coverage_missing=_coverage_missing(route, assignments),
            unknown_covers=_unknown_covers(route, assignments),
        )
    except ValueError as exc:
        _record_plan_attempt(attempts, stage="initial", result=first, parse_error=str(exc))
        # Schema-force the retry: the parse error alone left a seat whose final
        # message had been hijacked by hooks with nothing to correct toward (#518).
        second = _call_with_process_registry(
            _run_orchestrator,
            roster,
            _emit_plan_request(
                build_plan_prompt(
                    task,
                    roster,
                    corrective_note=f"{exc} {PLAN_JSON_ONLY_RULE}",
                    read_only=read_only,
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    evidence=evidence,
                    route=route,
                    no_file_writes=no_file_writes,
                    skill_policy=skill_policy,
                ),
                roster,
                output_dir=output_dir,
            ),
            cwd=cwd,
            read_only=read_only,
            sandbox_read_only=sandbox_read_only,
            sandbox=sandbox,
            codex_transport=transport,
            process_registry=process_registry,
        )
        if not second.ok:
            _record_plan_attempt(attempts, stage="correction", result=second)
            raise RuntimeError(f"orchestrator failed during plan correction: {second.detail}") from exc
        try:
            assignments = _parse_gated_plan(
                second.text,
                roster,
                output_dir=output_dir,
                read_only=read_only,
                skill_policy=skill_policy,
            )
            _validate_plan_dependencies(route, assignments)
            _record_plan_attempt(
                attempts,
                stage="correction",
                result=second,
                parsed=True,
                coverage_missing=_coverage_missing(route, assignments),
                unknown_covers=_unknown_covers(route, assignments),
            )
        except ValueError as second_exc:
            _record_plan_attempt(
                attempts,
                stage="correction",
                result=second,
                parse_error=str(second_exc),
            )
            raise RuntimeError(f"orchestrator returned an invalid plan: {second_exc}") from second_exc

    missing = _coverage_missing(route, assignments)
    if not missing:
        return assignments
    # The plan parses but skips required route stages: one corrective retry, then
    # keep whichever plan covers more. Advisory, never fatal - a deterministic
    # constraint must not brick a run the orchestrator can finish.
    revised_result = _call_with_process_registry(
        _run_orchestrator,
        roster,
        _emit_plan_request(
            build_plan_prompt(
                task,
                roster,
                corrective_note=(
                    "the plan does not cover required route stages: "
                    + ", ".join(missing)
                    + '. Add or tag assignments with "covers" so every required stage is covered.'
                ),
                read_only=read_only,
                code_graph=code_graph,
                drift_impact=drift_impact,
                evidence=evidence,
                route=route,
                no_file_writes=no_file_writes,
                skill_policy=skill_policy,
            ),
            roster,
            output_dir=output_dir,
        ),
        cwd=cwd,
        read_only=read_only,
        sandbox_read_only=sandbox_read_only,
        sandbox=sandbox,
        codex_transport=transport,
        process_registry=process_registry,
    )
    if not revised_result.ok:
        _record_plan_attempt(attempts, stage="coverage-correction", result=revised_result)
        return assignments
    try:
        revised = _parse_gated_plan(
            revised_result.text,
            roster,
            output_dir=output_dir,
            read_only=read_only,
            skill_policy=skill_policy,
        )
        _validate_plan_dependencies(route, revised)
    except ValueError as exc:
        _record_plan_attempt(attempts, stage="coverage-correction", result=revised_result, parse_error=str(exc))
        return assignments
    revised_missing = _coverage_missing(route, revised)
    _record_plan_attempt(
        attempts,
        stage="coverage-correction",
        result=revised_result,
        parsed=True,
        coverage_missing=revised_missing,
        unknown_covers=_unknown_covers(route, revised),
    )
    # A revision that covers strictly more wins; anything else (including an
    # empty plan) keeps the original. Never trade assignments away for tags.
    if revised and len(revised_missing) < len(missing):
        return revised
    return assignments


def _render_prior_results(
    results: list[WorkerResult],
    *,
    run_id: str | None = None,
    to_seat: str | None = None,
) -> str:
    return "\n\n".join(
        "\n".join(
            [
                f"Worker: {result.worker}",
                f"Sub-task: {result.task}",
                f"Status: {'ok' if result.ok else 'failed'}",
                f"Detail: {result.detail}" if result.detail else "Detail:",
                "Output:",
                _admitted_result_output(result, run_id=run_id, to_seat=to_seat) or "(no output)",
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
    direct: bool = False,
    code_graph: CodeGraphBrief | None = None,
    drift_impact: DriftImpactBrief | None = None,
    evidence: EvidenceBrief | None = None,
    skill_policy: RoutePolicyDecision | None = None,
    run_id: str | None = None,
    to_seat: str | None = None,
) -> str:
    prior_context = ""
    if prior_results:
        prior_context = (
            f"\n\nEarlier-stage context:\n{_render_prior_results(prior_results, run_id=run_id, to_seat=to_seat)}"
        )
    policy = f"\n\n{_read_only_rules()}" if read_only else ""
    scope_policy = worker_skill_policy_constraint(skill_policy, assignment)
    tool_gate = ""
    if assignment.admissible_tool_ids:
        tool_ids = ", ".join(assignment.admissible_tool_ids)
        tool_gate = (
            "\n\nAdmissible portable tools for this step (CandidateSetGate): "
            f"{tool_ids}. Use only these catalog tool ids; do not improvise tools "
            "outside the admissible set."
        )
    elif assignment.domain or assignment.capabilities or assignment.max_risk_class:
        tool_gate = (
            "\n\nCandidateSetGate found no admissible portable tools for this step's "
            "declared domain/capability/risk requirements. Do not invent tools."
        )
    return_instruction = (
        "Return a concise, complete final user-visible result."
        if direct
        else "Return a concise, complete result for the orchestrator to synthesize."
    )
    prompt = (
        f"You are Brigade worker {agent.name}.\n"
        f"Role:\n{agent.role}\n\n"
        f"Sub-task:\n{assignment.task}\n\n"
        f"{return_instruction}"
        f"{prior_context}"
        f"{scope_policy}"
        f"{tool_gate}"
        f"{policy}"
    )
    return _prepend_optional_briefs(prompt, code_graph=code_graph, drift_impact=drift_impact, evidence=evidence)


def _contains_event_marker(value: Any, marker: str) -> bool:
    if isinstance(value, str):
        return marker in value
    if isinstance(value, list):
        return any(_contains_event_marker(item, marker) for item in value)
    if isinstance(value, dict):
        return any(_contains_event_marker(item, marker) for item in value.values())
    return False


def _redact_event_marker(value: Any, marker: str) -> Any:
    if isinstance(value, str):
        return value.replace(marker, "[redacted]")
    if isinstance(value, list):
        return [_redact_event_marker(item, marker) for item in value]
    if isinstance(value, dict):
        return {key: _redact_event_marker(item, marker) for key, item in value.items()}
    return value


def _worker_event_writer(
    events_dir: Path | None,
    worker: str,
    *,
    verbose: bool = False,
    workspace: Path | None = None,
    correlation_marker: str | None = None,
):
    """Append lifecycle notifications to events/<worker>.jsonl; optionally narrate."""
    if events_dir is None and not verbose:
        return None
    path = None
    if events_dir is not None:
        events_dir.mkdir(parents=True, exist_ok=True)
        path = events_dir / f"{_slug(worker)}.jsonl"

    def on_event(msg: dict) -> None:
        safe_msg = msg
        if correlation_marker is not None:
            params = msg.get("params") if isinstance(msg, dict) else None
            item = params.get("item") if isinstance(params, dict) else None
            if (
                msg.get("method") == "item/completed"
                and isinstance(item, dict)
                and item.get("type") == "commandExecution"
                and _contains_event_marker(msg, correlation_marker)
                and workspace is not None
            ):
                thread_id = params.get("threadId")
                turn_id = params.get("turnId")
                if isinstance(thread_id, str) and isinstance(turn_id, str):
                    from . import runs_cmd

                    try:
                        runs_cmd._bind_approval_pause_request(
                            workspace,
                            correlation_marker=correlation_marker,
                            worker=worker,
                            thread_id=thread_id,
                            turn_id=turn_id,
                        )
                    except runs_cmd.ApprovalResumeError:
                        pass
            safe_msg = _redact_event_marker(msg, correlation_marker)
        if path is not None:
            with path.open("a") as fh:
                fh.write(proc.bound_text(json.dumps(safe_msg, default=str), proc.MAX_CAPTURE_BYTES) + "\n")
        if verbose and safe_msg.get("method") == "item/completed":
            item = (safe_msg.get("params") or {}).get("item") or {}
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
            turn_kwargs = {"timeout": timeout, "on_event": on_event, "on_turn_start": on_turn_start}
            if agent.reasoning is not None:
                turn_kwargs["effort"] = agent.reasoning
            turn = thread.run_turn(prompt, **turn_kwargs)
        except TypeError as exc:
            if "on_turn_start" not in str(exc):
                raise
            fallback_kwargs = {"timeout": timeout, "on_event": on_event}
            if agent.reasoning is not None:
                fallback_kwargs["effort"] = agent.reasoning
            turn = thread.run_turn(prompt, **fallback_kwargs)
    except codex_appserver.AppServerError as exc:
        return agents.AgentResult(
            text="",
            ok=False,
            detail=str(exc)[:200],
            status="failed",
            transport="codex-app-server",
            requested_model=agent.model,
            reasoning=agent.reasoning,
        )
    finally:
        if registry is not None and active_turn_id is not None:
            registry.unregister(worker, active_turn_id)
    text = turn.text.strip()
    if getattr(turn, "output_limit_exceeded", False) or len(text.encode("utf-8")) > proc.MAX_CAPTURE_BYTES:
        return agents.AgentResult(
            text=proc.bound_text(text),
            ok=False,
            detail=f"combined output exceeded {proc.MAX_CAPTURE_BYTES} byte limit"[:200],
            failure_phase="harness",
            failure_kind="output-limit",
            thread_id=turn.thread_id,
            status=turn.status,
            transport="codex-app-server",
            requested_model=agent.model,
            reasoning=agent.reasoning,
        )
    if not turn.ok:
        timed_out = bool(getattr(turn, "timed_out", False))
        failure_kind = "timeout" if timed_out else "interrupted" if turn.status == "interrupted" else None
        return agents.AgentResult(
            text=text,
            ok=False,
            detail=(turn.detail or f"turn {turn.status}")[:200],
            failure_kind=failure_kind,
            thread_id=turn.thread_id,
            status=turn.status,
            timed_out=timed_out,
            transport="codex-app-server",
            requested_model=agent.model,
            reasoning=agent.reasoning,
        )
    if not text:
        return agents.AgentResult(
            text="",
            ok=False,
            detail="empty output",
            thread_id=turn.thread_id,
            status=turn.status,
            transport="codex-app-server",
            requested_model=agent.model,
            reasoning=agent.reasoning,
        )
    output_failure = validate_final_output(text)
    if output_failure is not None:
        return agents.AgentResult(
            text=text,
            ok=False,
            detail=output_failure.detail,
            failure_phase="output-validation",
            failure_kind=output_failure.kind,
            thread_id=turn.thread_id,
            status=turn.status,
            transport="codex-app-server",
            requested_model=agent.model,
            reasoning=agent.reasoning,
        )
    return agents.AgentResult(
        text=text,
        ok=True,
        thread_id=turn.thread_id,
        status=turn.status,
        transport="codex-app-server",
        requested_model=agent.model,
        reasoning=agent.reasoning,
    )


def _candidate_set_replan_note(decision: Any) -> str:
    from . import candidate_set as candidate_set_mod

    detail = candidate_set_mod.empty_required_failure_detail(decision)
    return (
        f"{detail}. Revise assignment domain, capabilities, and/or max_risk_class "
        "so at least one .brigade/tools.toml entry is admissible, or drop tool "
        "requirements when no portable tool is needed."
    )


def _apply_candidate_set_gate(
    *,
    cwd: Path,
    output_dir: Path,
    assignments: list[Assignment],
    roster: Roster,
    task: str,
    read_only: bool,
    sandbox_read_only: bool | None,
    sandbox: str | None,
    code_graph: Any,
    drift_impact: Any,
    evidence: Any,
    route: RouteBrief | None,
    skill_policy: Any,
    transport_for_payload: str | None,
    process_registry: Any,
    plan_attempts: list[dict[str, object]] | None,
    allow_replan: bool,
) -> tuple[list[Assignment], Any, str | None]:
    """Filter tools per assignment, write receipt, optionally replan once on empty sets.

    Returns ``(assignments, decision, failure_detail)``. ``failure_detail`` is set when
    a required step still has no admissible tool after the bounded replan.
    """
    from . import candidate_set as candidate_set_mod

    decision = candidate_set_mod.evaluate_assignments(
        cwd,
        assignments,
        run_id=output_dir.name,
    )
    candidate_set_mod.write_candidate_set_receipt(output_dir, decision)
    if not decision.has_empty_required_steps:
        return candidate_set_mod.apply_admissible_tools(assignments, decision), decision, None

    if allow_replan:
        note = _candidate_set_replan_note(decision)
        if plan_attempts is not None:
            plan_attempts.append(
                {
                    "stage": "candidate-set-gate",
                    "ok": False,
                    "parsed": True,
                    "detail": note,
                    "failure_phase": candidate_set_mod.FAILURE_PHASE,
                    "failure_kind": candidate_set_mod.FAILURE_KIND,
                    "empty_required_steps": [
                        {
                            "stage": step.stage,
                            "worker": step.worker,
                            "task": step.task,
                            "requirements": step.requirements.payload(),
                        }
                        for step in decision.empty_required_steps
                    ],
                }
            )
        no_file_writes = _orchestrator_hides_write_tools(
            roster,
            read_only=read_only,
            sandbox_read_only=sandbox_read_only,
            sandbox=sandbox,
        )
        revised_result = _call_with_process_registry(
            _run_orchestrator,
            roster,
            _emit_plan_request(
                build_plan_prompt(
                    task,
                    roster,
                    corrective_note=note,
                    read_only=read_only,
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    evidence=evidence,
                    route=route,
                    no_file_writes=no_file_writes,
                    skill_policy=skill_policy,
                ),
                roster,
                output_dir=output_dir,
            ),
            cwd=cwd,
            read_only=read_only,
            sandbox_read_only=sandbox_read_only,
            sandbox=sandbox,
            codex_transport=transport_for_payload,
            process_registry=process_registry,
        )
        if revised_result.ok:
            try:
                revised = _parse_gated_plan(
                    revised_result.text,
                    roster,
                    output_dir=output_dir,
                    read_only=read_only,
                    skill_policy=skill_policy,
                )
                _validate_plan_dependencies(route, revised)
                _record_plan_attempt(
                    plan_attempts,
                    stage="candidate-set-correction",
                    result=revised_result,
                    parsed=True,
                    coverage_missing=_coverage_missing(route, revised),
                    unknown_covers=_unknown_covers(route, revised),
                )
                assignments = revised
                decision = candidate_set_mod.evaluate_assignments(
                    cwd,
                    assignments,
                    run_id=output_dir.name,
                )
                candidate_set_mod.write_candidate_set_receipt(output_dir, decision)
                _write_json(
                    output_dir / "plan.json",
                    receipt_schema.run_plan_document(_assignment_payload(assignments)),
                )
            except ValueError as exc:
                _record_plan_attempt(
                    plan_attempts,
                    stage="candidate-set-correction",
                    result=revised_result,
                    parse_error=str(exc),
                )
        else:
            _record_plan_attempt(plan_attempts, stage="candidate-set-correction", result=revised_result)

    if decision.has_empty_required_steps:
        return (
            candidate_set_mod.apply_admissible_tools(assignments, decision),
            decision,
            candidate_set_mod.empty_required_failure_detail(decision),
        )
    return candidate_set_mod.apply_admissible_tools(assignments, decision), decision, None


def dispatch(
    assignments: list[Assignment],
    roster: Roster,
    cwd: Path | None = None,
    read_only: bool = False,
    sandbox_read_only: bool | None = None,
    sandbox: str | None = None,
    direct: bool = False,
    code_graph: CodeGraphBrief | None = None,
    drift_impact: DriftImpactBrief | None = None,
    evidence: EvidenceBrief | None = None,
    appserver=None,
    control_registry: run_control.LiveTurnRegistry | None = None,
    events_dir: Path | None = None,
    verbose: bool = False,
    authorized_writable_worktree: bool = False,
    fail_fast: bool = True,
    scheduler: str = "waves",
    route_dependencies: dict[str, tuple[str, ...]] | None = None,
    route_held: dict[str, list[str]] | None = None,
    on_stage_start: Callable[[int, tuple[str, ...]], None] | None = None,
    on_interrupt: Callable[[], None] | None = None,
    on_scheduler_resolved: Callable[[str, str | None], None] | None = None,
    on_dispatch_requested: Callable[[Agent], int | None] | None = None,
    on_dispatch_observed: Callable[[Agent, int], None] | None = None,
    on_dispatch_completed: Callable[[Agent, int], None] | None = None,
    on_dispatch_failed: Callable[[Agent, int], None] | None = None,
    process_registry: proc.ProcessRegistry | None = None,
    build_prompt: Callable[..., str] | None = None,
    quarantine_state: seat_health_policy.SeatQuarantineState | None = None,
    reprobe_seat: Callable[[Agent], bool] | None = None,
    on_failed_attempt_persisted: Callable[[WorkerResult], None] | None = None,
    run_id: str | None = None,
    output_dir: Path | None = None,
) -> list[WorkerResult]:
    from . import run_transport

    bound_prompt = build_prompt
    if bound_prompt is None:
        bound_prompt = partial(
            _worker_prompt,
            run_id=run_id or (output_dir.name if output_dir is not None else message_envelope.IN_MEMORY_RUN_ID),
            to_seat=roster.orchestrator,
        )
    return run_transport.dispatch(
        assignments,
        roster,
        build_prompt=bound_prompt,
        run_appserver_worker=_run_codex_appserver_worker,
        event_writer=_worker_event_writer,
        cwd=cwd,
        read_only=read_only,
        sandbox_read_only=sandbox_read_only,
        sandbox=sandbox,
        direct=direct,
        code_graph=code_graph,
        drift_impact=drift_impact,
        evidence=evidence,
        appserver=appserver,
        control_registry=control_registry,
        events_dir=events_dir,
        verbose=verbose,
        authorized_writable_worktree=authorized_writable_worktree,
        fail_fast=fail_fast,
        scheduler=scheduler,
        route_dependencies=route_dependencies,
        route_held=route_held,
        on_stage_start=on_stage_start,
        on_interrupt=on_interrupt,
        on_scheduler_resolved=on_scheduler_resolved,
        on_dispatch_requested=on_dispatch_requested,
        on_dispatch_observed=on_dispatch_observed,
        on_dispatch_completed=on_dispatch_completed,
        on_dispatch_failed=on_dispatch_failed,
        process_registry=process_registry,
        quarantine_state=quarantine_state,
        reprobe_seat=reprobe_seat,
        on_failed_attempt_persisted=on_failed_attempt_persisted,
        run_id=run_id,
        output_dir=output_dir,
    )


def build_synth_prompt(
    task: str,
    results: list[WorkerResult],
    read_only: bool = False,
    ground_truth: dict[str, object] | None = None,
    code_graph: CodeGraphBrief | None = None,
    drift_impact: DriftImpactBrief | None = None,
    evidence: EvidenceBrief | None = None,
    run_id: str | None = None,
    to_seat: str | None = None,
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
                    _admitted_result_output(result, run_id=run_id, to_seat=to_seat) or "(no output)",
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
    return _prepend_optional_briefs(prompt, code_graph=code_graph, drift_impact=drift_impact, evidence=evidence)


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


def _is_brigade_path(value: str) -> bool:
    normalized = value.replace("\\", "/").strip("/")
    return normalized == ".brigade" or normalized.startswith(".brigade/")


def _non_brigade_paths(paths: object) -> list[str]:
    if not isinstance(paths, list):
        return []
    return [item for item in paths if isinstance(item, str) and item.strip() and not _is_brigade_path(item)]


def _suspected_noop(
    *,
    ground_truth: dict[str, object],
    worker_results: list[WorkerResult],
    dry_run: bool,
    read_only: bool,
    sandbox_read_only: bool | None,
    sandbox: str | None,
) -> bool:
    if dry_run or read_only or sandbox_read_only is True or sandbox == "read-only":
        return False
    if ground_truth.get("available") is not True or not worker_results:
        return False
    if not all(result.ok for result in worker_results):
        return False
    changed = _non_brigade_paths(ground_truth.get("changed_files"))
    untracked = _non_brigade_paths(ground_truth.get("untracked_files"))
    return not changed and not untracked


def _mark_noop_worker_results(worker_results: list[WorkerResult], suspected_noop: bool) -> list[WorkerResult]:
    if not suspected_noop:
        return worker_results
    return [replace(result, detail=NOOP_DETAIL) if result.ok else result for result in worker_results]


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


_ROUTE_CHANGED_PATHS_CAP = 200


def _route_changed_paths(cwd: Path | None) -> tuple[str, ...]:
    """Tracked-modified plus untracked paths, for route surface derivation.
    Best-effort per source: a repo with no commits still yields its untracked
    files, and any git failure means fewer path signals, never a failed run."""
    if cwd is None:
        return ()
    paths: list[str] = []
    try:
        changed, error = _git_stdout(cwd, "diff", "--name-only", "HEAD")
        if error is None:
            paths.extend(line.strip() for line in changed.splitlines() if line.strip())
    except Exception:
        pass
    try:
        paths.extend(runguard._untracked_files(cwd))
    except Exception:
        pass
    return tuple(paths[:_ROUTE_CHANGED_PATHS_CAP])


def _receipt_git_snapshot(cwd: Path | None) -> dict[str, object] | None:
    if cwd is None:
        return None
    try:
        head, head_error = _git_stdout(cwd, "rev-parse", "HEAD")
        branch, branch_error = _git_stdout(cwd, "rev-parse", "--abbrev-ref", "HEAD")
        status, status_error = _git_stdout(cwd, "status", "--porcelain")
    except Exception:
        return None
    if head_error or branch_error or status_error:
        return None
    return {"head": head.strip(), "branch": branch.strip(), "dirty_files": len(status.splitlines())}


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


def build_ground_truth(
    cwd: Path | None,
    started_at: datetime,
    pre_run_snapshot: runguard.PreRunSnapshot | None = None,
) -> dict[str, object]:
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

    # When a pre-run snapshot exists, attribute only state changes relative to
    # it so pre-existing dirty or untracked files (possible only in a linked
    # worktree; the primary checkout rejects --allow-dirty) are not charged to
    # the worker. For a clean baseline the full HEAD diffstat equals the worker
    # delta and is preserved. For a dirty baseline the full HEAD diffstat is
    # contaminated by pre-existing dirt, so it is never reused as the run delta:
    # only content-changed paths relative to baseline are reported and line
    # counts are left unavailable (they cannot be computed without baseline
    # contamination).
    if pre_run_snapshot is not None:
        try:
            changed_names_list, untracked_files = runguard.changes_relative_to_snapshot(cwd, pre_run_snapshot)
        except runguard.RunGuardError as exc:
            # Fail closed: a final git query failure must not become an
            # available clean result. Surface unavailable ground truth with a
            # precise reason instead of charging (or clearing) the worker.
            payload["reason"] = str(exc)
            return payload
        changed_names = "\n".join(changed_names_list)
        baseline_dirty = bool(pre_run_snapshot.tracked_dirty_paths) or bool(pre_run_snapshot.untracked_paths)
        if baseline_dirty:
            diffstat = ""
            payload["diffstat_unavailable"] = True
        else:
            diffstat, error = _git_stdout(cwd, "diff", "--stat", "HEAD")
            if error is not None:
                payload["reason"] = error
                return payload
    else:
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
            # Changes are observed during the run; the author is unknown. Brigade
            # does not assert the worker authored them (a concurrent commit,
            # branch switch, or pre-existing edit could also be the source).
            "change_provenance": "observed-during-run-author-unknown",
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
        # Ground truth describes changes *observed during the run*; the author is
        # unknown. Brigade does not assert the worker authored them (a concurrent
        # commit, branch switch, or pre-existing edit could also have produced
        # them). Drift checks narrow this, but the provenance stays "author
        # unknown" so synthesis never claims the worker wrote concurrent edits.
        lines.append(
            "- changed_files: observed during the run, author unknown "
            f"({len(changed_files)})" + (f": {', '.join(changed_files[:6])}" if changed_files else "")
        )
        lines.append(
            "- untracked_files: observed during the run, author unknown "
            f"({len(untracked_files)})" + (f": {', '.join(untracked_files[:6])}" if untracked_files else "")
        )
        if ground_truth.get("diffstat_unavailable") is True:
            lines.append("- diffstat: unavailable (dirty baseline; line counts would include pre-existing changes)")
        else:
            diffstat = _one_line(str(ground_truth.get("diffstat") or "none"))
            if len(diffstat) > 240:
                diffstat = diffstat[:237] + "..."
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
    context_eval_payload = ground_truth.get("context_eval")
    if isinstance(context_eval_payload, dict):
        summary = _context_eval_fact(context_eval_payload)
        if summary:
            lines.append(f"- context eval: {summary}")
    return "\n".join(lines)


def _context_eval_fact(payload: dict[str, object]) -> str:
    try:
        counts = payload.get("counts")
        if not isinstance(counts, dict):
            return ""
        rate = payload.get("brief_hit_rate")
        hits = counts.get("hits")
        delta_files = counts.get("delta_files")
        missed = counts.get("missed")
        if (
            isinstance(rate, bool)
            or not isinstance(rate, (int, float))
            or isinstance(hits, bool)
            or not isinstance(hits, int)
            or isinstance(delta_files, bool)
            or not isinstance(delta_files, int)
            or isinstance(missed, bool)
            or not isinstance(missed, int)
        ):
            return ""
        return f"brief hit rate {rate:.2f} ({hits}/{delta_files} files, {missed} missed)"
    except Exception:
        return ""


def _with_patch_ref(ground_truth: object, patch_ref: str) -> object:
    if not isinstance(ground_truth, dict):
        return ground_truth
    updated = dict(ground_truth)
    updated["patch_ref"] = patch_ref
    return updated


def _context_eval_for_run(
    code_graph: CodeGraphBrief | None,
    code_graph_delta: dict[str, object] | None,
) -> dict[str, object] | None:
    try:
        if code_graph is None or not code_graph.attached or not code_graph.text:
            return None
        if not isinstance(code_graph_delta, dict) or code_graph_delta.get("ok") is not True:
            return None
        if code_graph_delta.get("stale_graph_used") is True:
            return None
        sidecar_path = code_graph_delta.get("sidecar_path")
        if not isinstance(sidecar_path, str) or not sidecar_path:
            return None
        delta_files = context_eval.extract_delta_files(sidecar_path)
        if not delta_files:
            return None
        brief_files = context_eval.extract_brief_files(code_graph.text)
        return context_eval.evaluate(brief_files, delta_files)
    except Exception:
        return None


def set_artifact_patch_ref(output_dir: Path, patch_ref: str = "changes.patch") -> None:
    for filename in ("worker-results.json", "synthesis.json"):
        path = output_dir / filename
        try:
            payload = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise runguard.RunGuardError(
                f"failed to read {filename} while recording artifact patch reference: {exc}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise runguard.RunGuardError(
                f"failed to parse {filename} while recording artifact patch reference: {exc}"
            ) from exc
        if not isinstance(payload, dict) or "ground_truth" not in payload:
            raise runguard.RunGuardError(f"{filename} is missing ground_truth while recording artifact patch reference")
        payload["ground_truth"] = _with_patch_ref(payload.get("ground_truth"), patch_ref)
        if filename == "worker-results.json":
            receipt_schema.stamp_worker_results_document(payload)
        else:
            receipt_schema.stamp_synthesis_document(payload)
        try:
            write_sidecar_revision(output_dir, filename, payload)
        except OSError as exc:
            raise runguard.RunGuardError(f"failed to record artifact patch reference in {filename}: {exc}") from exc


def record_artifact_collection(
    output_dir: Path,
    *,
    status: str,
    patch_ref: str | None = None,
    changed: bool | None = None,
    tracked_count: int | None = None,
    untracked_count: int | None = None,
    worktree: Path | None = None,
    failure_phase: str | None = None,
    failure_kind: str | None = None,
    detail: str | None = None,
) -> None:
    run_path = output_dir / "run.json"
    try:
        payload = json.loads(run_path.read_text())
    except OSError as exc:
        raise runguard.RetainRunLockError(f"failed to read run receipt during artifact collection: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise runguard.RetainRunLockError(f"run receipt is invalid during artifact collection: {exc}") from exc
    if not isinstance(payload, dict):
        raise runguard.RetainRunLockError("run receipt must contain an object during artifact collection")

    collection: dict[str, object] = {"status": status}
    if patch_ref is not None:
        collection["patch_ref"] = patch_ref
    if changed is not None:
        collection["changed"] = changed
    if tracked_count is not None:
        collection["tracked_count"] = tracked_count
    if untracked_count is not None:
        collection["untracked_count"] = untracked_count
    if worktree is not None:
        collection["worktree"] = str(worktree)

    if status == "failed":
        bounded_detail = _one_line(detail or "artifact collection failed")[:2000]
        phase = failure_phase or "artifact-collection"
        artifact_failure = {
            "phase": phase,
            "kind": failure_kind or "unknown",
            "detail": bounded_detail,
        }
        collection["failure"] = artifact_failure
        primary_status = payload.get("status")
        primary_terminal = isinstance(primary_status, str) and primary_status not in {
            "started",
            "planning",
            "dispatching",
            "result-processing",
            "synthesizing",
            "handoff",
            "artifact-collection",
            "running",
        }
        if not primary_terminal:
            payload["status"] = "failed"
            payload["error"] = bounded_detail
            payload["failure_phase"] = phase
            payload["failure"] = artifact_failure
            finished_at = datetime.now(timezone.utc)
            payload["status_started_at"] = _utc_iso(finished_at)
            payload["finished_at"] = _utc_iso(finished_at)
            started_at = payload.get("started_at")
            if isinstance(started_at, str):
                started = _parse_iso_datetime(started_at)
                if started is not None:
                    payload["duration_seconds"] = max(
                        0.0,
                        round((finished_at - started).total_seconds(), 3),
                    )
    elif status == "ok" and payload.get("status") == "artifact-collection":
        finished_at = datetime.now(timezone.utc)
        payload["status"] = "ok"
        payload["status_started_at"] = _utc_iso(finished_at)
        payload["finished_at"] = _utc_iso(finished_at)
        started_at = payload.get("started_at")
        if isinstance(started_at, str):
            started = _parse_iso_datetime(started_at)
            if started is not None:
                payload["duration_seconds"] = max(
                    0.0,
                    round((finished_at - started).total_seconds(), 3),
                )
    payload["artifact_collection"] = collection
    try:
        _write_json(run_path, receipt_schema.stamp_run_receipt(payload))
    except (OSError, run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError) as exc:
        raise runguard.RetainRunLockError(f"failed to update run receipt after artifact collection: {exc}") from exc


def record_run_termination(
    output_dir: Path,
    *,
    status: str,
    failure_phase: str | None,
    failure_kind: str,
    detail: str,
    seat: str | None = None,
    active_seats: tuple[str, ...] = (),
) -> None:
    """Terminalize a run that escaped its normal result path."""

    run_path = output_dir / "run.json"
    try:
        payload = json.loads(run_path.read_text())
    except OSError as exc:
        raise runguard.RetainRunLockError(f"failed to read run receipt during termination: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise runguard.RetainRunLockError(f"run receipt is invalid during termination: {exc}") from exc
    if not isinstance(payload, dict):
        raise runguard.RetainRunLockError("run receipt must contain an object during termination")
    if payload.get("finished_at"):
        return

    stored_status = payload.get("status")
    phase_owner = payload.get("phase_owner")
    if stored_status == "result-processing" and isinstance(phase_owner, str) and phase_owner:
        seat = phase_owner
        active_seats = ()
    phase = failure_phase or {
        "started": "startup",
        "planning": "planning",
        "dispatching": "dispatch",
        "result-processing": "result-processing",
        "synthesizing": "synthesis",
        "handoff": "handoff",
        "artifact-collection": "artifact-collection",
    }.get(stored_status, "run")
    bounded_detail = _one_line(detail or failure_kind)[:2000]
    failure: dict[str, object] = {
        "phase": phase,
        "kind": failure_kind,
        "detail": bounded_detail,
    }
    if seat:
        failure["seat"] = seat
    elif active_seats:
        failure["seats"] = list(active_seats)
    finished_at = datetime.now(timezone.utc)
    payload.update(
        {
            "status": status,
            "status_started_at": _utc_iso(finished_at),
            "error": bounded_detail,
            "failure_phase": phase,
            "failure": failure,
            "finished_at": _utc_iso(finished_at),
        }
    )
    started_at = payload.get("started_at")
    if isinstance(started_at, str):
        started = _parse_iso_datetime(started_at)
        if started is not None:
            payload["duration_seconds"] = max(
                0.0,
                round((finished_at - started).total_seconds(), 3),
            )
    try:
        _write_json(run_path, receipt_schema.stamp_run_receipt(payload))
    except (OSError, run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError) as exc:
        raise runguard.RetainRunLockError(f"failed to write terminal run receipt: {exc}") from exc


def _initial_verification_budget(container: Mapping[str, Any]) -> tuple[bool, Mapping[str, Any] | None]:
    """Return a present, validated verification budget from an initial run artifact."""
    if "verification_contract" not in container:
        return False, None
    contract = container["verification_contract"]
    if not isinstance(contract, Mapping):
        raise run_budget.BudgetCompatibilityError(
            "verification_contract must be an object",
            code="schema_incompatible",
        )
    schema = contract.get("schema")
    if schema is not None and schema != verification_contract.VERIFICATION_CONTRACT_SCHEMA:
        raise run_budget.BudgetCompatibilityError(
            run_events._bound(f"unsupported verification_contract schema {schema!r}"),
            code="schema_incompatible",
        )
    version = contract.get("schema_version")
    if version is not None and (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != verification_contract.VERIFICATION_CONTRACT_SCHEMA_VERSION
    ):
        raise run_budget.BudgetCompatibilityError(
            run_events._bound(f"unsupported verification_contract schema_version {version!r}"),
            code="schema_incompatible",
        )
    if "budget" not in contract:
        return False, None
    budget = contract["budget"]
    if not isinstance(budget, Mapping):
        raise run_budget.BudgetCompatibilityError(
            "verification_contract budget must be an object",
            code="schema_incompatible",
        )
    return True, budget


def _build_budget_coordinator(
    output_dir: Path,
    *,
    started_at: datetime,
    append_event: Callable[[str, Mapping[str, Any], str], Any],
) -> run_budget.BudgetCoordinator | None:
    """Build a budget coordinator when the run journal is active.

    Declaration sources (first match wins):
    1. ``run.json`` ``run_budget`` object
    2. ``run.json`` / plan ``verification_contract.budget`` (#500 bridge)
    3. Empty declaration (no enforceable ceilings; observed reconcile still
       works). Ordinary undeclared ``brigade run`` / dogfood / model-trial
       paths stay unbounded for backward compatibility.

    Returns ``None`` when the lifecycle journal is not enrolled so budget events
    are not appended into a missing journal.
    """
    run_path = output_dir / "run.json"
    try:
        raw = json.loads(run_path.read_text())
    except (OSError, json.JSONDecodeError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    if raw.get("lifecycle_journal_requested") is not True and not (output_dir / "events" / "lifecycle.jsonl").is_file():
        return None

    declaration: run_budget.RunBudgetDeclaration
    if "run_budget" in raw:
        declaration = run_budget.declaration_from_persisted_artifact(raw["run_budget"])
    else:
        has_budget, budget = _initial_verification_budget(raw)
        if not has_budget:
            # Also accept a sibling plan.json contract when present.
            plan_path = output_dir / "plan.json"
            try:
                plan_raw = json.loads(plan_path.read_text())
            except (OSError, json.JSONDecodeError):
                plan_raw = None
            if isinstance(plan_raw, dict):
                has_budget, budget = _initial_verification_budget(plan_raw)
        declaration = run_budget.declaration_from_verification_budget(budget)

    coordinator = run_budget.BudgetCoordinator(
        declaration=declaration,
        append_event=append_event,
    )
    coordinator.set_started_at(started_at)
    journal_path = output_dir / "events" / "lifecycle.jsonl"
    if journal_path.is_file():
        try:
            report = run_journal.read_journal_bounded(journal_path)
            if report.partial_tail is None and not report.chain_errors:
                coordinator.reload(report.events)
        except (OSError, run_journal.RunJournalError, run_events.CanonicalizationError):
            # Leave empty projection; append path will fail closed if the journal is broken.
            pass
    return coordinator


def _observed_budget_cancellation(
    process_registry: proc.ProcessRegistry,
    control_registry: run_control.LiveTurnRegistry | None,
    active_seats: tuple[str, ...],
) -> run_budget.CancellationReport:
    """Cancel both transports and preserve only observed per-seat state."""
    outcomes: list[run_budget.CancelOutcome] = []
    active: list[str] = []
    represented: set[str] = set()
    if control_registry is not None:
        interrupted, still_active = control_registry.interrupt_observed(active_seats)
        outcomes.extend(run_budget.CancelOutcome(seat, "interrupt", result) for seat, result in interrupted)
        represented.update(seat for seat, _result in interrupted)
        represented.update(still_active)
        active.extend(still_active)

    try:
        observed_processes = process_registry.cancel()
    except Exception:
        unobserved = tuple(seat for seat in active_seats if seat not in represented)
        outcomes.extend(run_budget.CancelOutcome(seat, "process_cancel", "error") for seat in unobserved)
        active.extend(unobserved)
        return run_budget.CancellationReport(
            outcomes=tuple(outcomes),
            active_seats=tuple(dict.fromkeys(active)),
        )

    process_results: dict[str, str] = {}
    priority = {"interrupted": 0, "still_active": 1, "error": 2}
    for process in observed_processes:
        seat = process.seat or "process"
        existing = process_results.get(seat)
        if existing is None or priority[process.result] > priority[existing]:
            process_results[seat] = process.result
    for seat, result in process_results.items():
        outcomes.append(run_budget.CancelOutcome(seat, "process_cancel", result))
        represented.add(seat)
        if result in {"still_active", "error"}:
            active.append(seat)

    for seat in active_seats:
        if seat not in represented:
            outcomes.append(run_budget.CancelOutcome(seat, "none", "unsupported"))
    return run_budget.CancellationReport(
        outcomes=tuple(outcomes),
        active_seats=tuple(dict.fromkeys(active)),
    )


def record_approval_pause(output_dir: Path, approval_reference: Mapping[str, Any]) -> None:
    """Commit an approval wait and refresh its compatibility snapshot.

    The caller must hold the matching normal run lock. Successful return is
    intentionally non-exceptional so the surrounding ``run_lock`` context
    releases ownership and the process can exit cleanly.
    """
    run_dir = output_dir.expanduser().resolve()
    run_path = run_dir / "run.json"
    try:
        raw = json.loads(run_path.read_text())
    except OSError as exc:
        raise runguard.RetainRunLockError(f"failed to read run receipt before approval pause: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise runguard.RetainRunLockError(f"run receipt is invalid before approval pause: {exc}") from exc
    if not isinstance(raw, dict):
        raise runguard.RetainRunLockError("run receipt must contain an object before approval pause")
    try:
        reference = run_lifecycle.normalize_approval_reference(approval_reference)
        workspace = runguard.resolve_run_lock_workspace(raw, run_dir)
        if workspace is None or not runguard.is_active_run_owner(workspace, run_dir):
            raise run_lifecycle.LifecycleJournalError("approval pause requires the active run lock for this run")
        run_lifecycle.prepare_lifecycle_journal(
            run_dir,
            workspace=workspace,
            incoming_snapshot=raw,
        )
        run_lifecycle.record_lifecycle_event(
            run_dir,
            event_type="approval.requested",
            payload={
                "approval_id": reference["approval_id"],
                "source": reference["source"],
                "contract_fingerprint": reference["contract_fingerprint"],
            },
            idempotency_key=run_lifecycle.approval_idempotency_key(reference["approval_id"], "requested"),
            workspace=workspace,
        )
        raw["status"] = "running"
        raw["status_started_at"] = _utc_iso(datetime.now(timezone.utc))
        raw["approval_reference"] = {
            **reference,
            "decision_state": "pending",
        }
        raw.pop("finished_at", None)
        raw.pop("duration_seconds", None)
        _write_json(run_path, receipt_schema.stamp_run_receipt(raw))
    except (OSError, run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError) as exc:
        raise runguard.RetainRunLockError(f"failed to record approval pause: {exc}") from exc


def write_approval_resume_handoff(
    output_dir: Path,
    worker_results: list[WorkerResult],
    *,
    requester_worker: str,
    requester_thread_id: str,
) -> None:
    """Persist only the closed worker coordinates needed by approval resume."""
    matching = [
        result
        for result in worker_results
        if isinstance(result.thread_id, str)
        and bool(result.thread_id)
        and result.worker == requester_worker
        and result.thread_id == requester_thread_id
    ]
    if len(matching) != 1:
        raise runguard.RetainRunLockError("approval pause requester did not match exactly one worker thread")
    requester = matching[0]
    resumable = [
        {
            "worker": requester.worker,
            "task": requester.task,
            "ok": False,
            "thread_id": requester.thread_id,
            "status": "interrupted",
        }
    ]
    try:
        write_sidecar_revision(
            output_dir,
            "worker-results.json",
            receipt_schema.worker_results_document(resumable),
        )
    except (OSError, TypeError, ValueError) as exc:
        raise runguard.RetainRunLockError("failed to persist approval resume handoff") from exc


def record_dispatch_stage(output_dir: Path, *, stage: int, seats: tuple[str, ...]) -> None:
    """Record the currently executing dispatch stage without terminalizing it."""

    run_path = output_dir / "run.json"
    try:
        payload = json.loads(run_path.read_text())
    except OSError as exc:
        raise runguard.RetainRunLockError(f"failed to read run receipt during stage transition: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise runguard.RetainRunLockError(f"run receipt is invalid during stage transition: {exc}") from exc
    if not isinstance(payload, dict):
        raise runguard.RetainRunLockError("run receipt must contain an object during stage transition")
    if payload.get("finished_at"):
        return
    payload.update(
        {
            "status": "dispatching",
            "status_started_at": _utc_iso(datetime.now(timezone.utc)),
            "active_stage": stage,
            "active_seats": list(seats),
        }
    )
    try:
        _write_json(run_path, receipt_schema.stamp_run_receipt(payload))
    except (OSError, run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError) as exc:
        raise runguard.RetainRunLockError(f"failed to write dispatch stage receipt: {exc}") from exc


def record_result_processing(output_dir: Path, *, seat: str | None = None) -> None:
    """Record aggregate post-dispatch result processing and clear worker ownership."""

    run_path = output_dir / "run.json"
    try:
        payload = json.loads(run_path.read_text())
    except OSError as exc:
        raise runguard.RetainRunLockError(f"failed to read run receipt during result processing: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise runguard.RetainRunLockError(f"run receipt is invalid during result processing: {exc}") from exc
    if not isinstance(payload, dict):
        raise runguard.RetainRunLockError("run receipt must contain an object during result processing")
    if payload.get("finished_at"):
        return
    payload.update({"status": "result-processing", "status_started_at": _utc_iso(datetime.now(timezone.utc))})
    if seat is None:
        payload.pop("phase_owner", None)
    else:
        payload["phase_owner"] = seat
    payload.pop("active_stage", None)
    payload.pop("active_seats", None)
    try:
        _write_json(run_path, receipt_schema.stamp_run_receipt(payload))
    except (OSError, run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError) as exc:
        raise runguard.RetainRunLockError(f"failed to record result-processing phase: {exc}") from exc


def _roster_resolution_payload(roster: Roster) -> dict[str, object] | None:
    if roster.resolution is None:
        return None
    return {
        "path": str(roster.resolution.path),
        "source": roster.resolution.source,
        "shadowed": [str(path) for path in roster.resolution.shadowed],
    }


def _roster_payload(roster: Roster) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": receipt_schema.ROSTER_SNAPSHOT_SCHEMA,
        "schema_version": receipt_schema.ROSTER_SNAPSHOT_SCHEMA_VERSION,
        "orchestrator": roster.orchestrator,
        "max_workers": roster.max_workers,
        "timeout_seconds": roster.timeout_seconds,
        "allow_models": list(roster.allow_models),
        "sandbox": roster.sandbox,
        "agents": {
            name: {
                "cli": agent.cli,
                "command": list(agent.command) if agent.command is not None else None,
                "model": agent.model,
                "reasoning": agent.reasoning,
                "transport": agent.transport,
                "transport_version": agent.transport_version,
                "role": agent.role,
                "timeout_seconds": agent.timeout_seconds,
                "invalid_final_fallback": agent.invalid_final_fallback,
                "read_only_capable": agent.read_only_capable,
                "cloud_safe_mode": agent.cloud_safe_mode,
                # env tables hold names and references only, never secret
                # values (enforced at roster load), so persisting them for
                # resume is safe.
                "env": dict(agent.env) if agent.env is not None else None,
                "capabilities": list(agent.capabilities),
            }
            for name, agent in roster.agents.items()
        },
    }
    if resolution := _roster_resolution_payload(roster):
        payload["resolution"] = resolution
    return payload


def _run_payload(
    *,
    task: str,
    cwd: Path | None,
    lock_workspace: Path | None,
    roster: Roster,
    dry_run: bool,
    read_only: bool,
    status: str,
    started_at: datetime,
    finished_at: datetime | None = None,
    output_dir: Path | None = None,
    handoff_path: Path | None = None,
    error: str | None = None,
    failure_phase: str | None = None,
    failure_kind: str | None = None,
    failure_seat: str | None = None,
    transport_warning: dict[str, object] | None = None,
    code_graph: CodeGraphBrief | None = None,
    drift_impact: DriftImpactBrief | None = None,
    evidence: EvidenceBrief | None = None,
    brief_set: BriefSet | None = None,
    codex_transport: str | None = None,
    control_socket: Path | None = None,
    control_transport: run_control.ControlTransport | None = None,
    code_graph_delta: dict[str, object] | None = None,
    context_eval_payload: dict[str, object] | None = None,
    suspected_noop: bool = False,
    route: RouteBrief | None = None,
    skill_route_policy: RoutePolicyDecision | None = None,
    worker: str | None = None,
    include_git: bool = True,
    pre_run_snapshot: dict[str, object] | None = None,
    scheduler: dict[str, object] | None = None,
    lifecycle_journal_requested: bool | None = None,
    run_journal_authority_requested: bool | None = None,
    health: dict[str, object] | None = None,
    worker_failure_summary: dict[str, object] | None = None,
    transport_routing: dict[str, object] | None = None,
    retry_decisions: Sequence[Mapping[str, Any]] | None = None,
    verification_contract_payload: Mapping[str, Any] | None = None,
    run_budget_payload: Mapping[str, Any] | None = None,
    kind: str = "work",
    causal_receipt_payload: Mapping[str, Any] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema": receipt_schema.RUN_RECEIPT_SCHEMA,
        "schema_version": receipt_schema.RUN_RECEIPT_SCHEMA_VERSION,
        "kind": kind,
        "task": task,
        "orchestrator": roster.orchestrator,
        "dry_run": dry_run,
        "read_only": read_only,
        "status": status,
        "started_at": _utc_iso(started_at),
        "status_started_at": _utc_iso(started_at if status == "started" else datetime.now(timezone.utc)),
        "suspected_noop": suspected_noop,
        "code_graph_brief": {
            "attached": bool(code_graph.attached) if code_graph is not None else False,
            "bytes": code_graph.bytes if code_graph is not None else 0,
        },
        "drift_impact_brief": {
            "attached": bool(drift_impact.attached) if drift_impact is not None else False,
            "bytes": drift_impact.bytes if drift_impact is not None else 0,
            "pending_count": drift_impact.pending_count if drift_impact is not None else 0,
        },
        "evidence_brief": {
            "attached": bool(evidence.attached) if evidence is not None else False,
            "bytes": evidence.bytes if evidence is not None else 0,
        },
        "brief_budget": {
            "bytes": brief_set.budget_bytes if brief_set is not None else BRIEF_BUDGET_BYTES,
            "attached": list(brief_set.attached) if brief_set is not None else [],
        },
    }
    if cwd is not None:
        payload["cwd"] = str(cwd)
    if scheduler is not None:
        payload["scheduler"] = scheduler
    if lifecycle_journal_requested:
        payload["lifecycle_journal_requested"] = True
    if run_journal_authority_requested:
        payload["run_journal_authority_requested"] = True
    if resolution := _roster_resolution_payload(roster):
        payload["roster"] = resolution
    if roster.seat_routing:
        payload["seat_routing"] = [dict(decision) for decision in roster.seat_routing]
    if retry_decisions:
        payload["retry_decisions"] = [dict(entry) for entry in retry_decisions]
    if lock_workspace is not None:
        payload["lock_workspace"] = str(lock_workspace)
    if route is not None:
        payload["route"] = route.payload()
    if skill_route_policy is not None and skill_route_policy.policy_applied:
        from .route_policy import route_policy_extensions_from_decision

        payload["skill_route_policy"] = route_policy_extensions_from_decision(skill_route_policy)
    if worker is not None:
        payload["worker"] = worker
    if include_git:
        git = _receipt_git_snapshot(cwd)
        if git is not None:
            payload["git"] = git
    if pre_run_snapshot is not None:
        payload["pre_run_snapshot"] = pre_run_snapshot
    if code_graph_delta is not None:
        payload["code_graph_delta"] = code_graph_delta
    if context_eval_payload is not None:
        payload["context_eval"] = context_eval_payload
    if finished_at is not None:
        payload["finished_at"] = _utc_iso(finished_at)
        payload["duration_seconds"] = max(0.0, round((finished_at - started_at).total_seconds(), 3))
    if output_dir is not None:
        payload["artifacts"] = str(output_dir)
    if handoff_path is not None:
        payload["handoff"] = str(handoff_path)
    if error is not None:
        payload["error"] = error
        if failure_phase is not None or failure_kind is not None:
            payload["failure_phase"] = failure_phase or "unknown"
            if failure_kind is not None:
                payload["failure_kind"] = failure_kind
            failure_payload: dict[str, object] = {
                "phase": failure_phase or "unknown",
                "kind": failure_kind or "unknown",
                "detail": error,
            }
            if failure_seat is not None:
                failure_payload["seat"] = failure_seat
            payload["failure"] = failure_payload
    if transport_warning is not None:
        payload["transport_warning"] = dict(transport_warning)
    if codex_transport is not None:
        payload["codex_transport"] = codex_transport
    if control_transport is not None:
        payload["control_transport"] = control_transport.to_metadata()
        if control_transport.kind == "unix" and control_transport.path:
            payload["control_socket"] = control_transport.path
    elif control_socket is not None:
        payload["control_socket"] = str(control_socket)
    if verification_contract_payload is not None:
        payload["verification_contract"] = dict(verification_contract_payload)
    if run_budget_payload is not None:
        payload["run_budget"] = dict(run_budget_payload)
    if isinstance(causal_receipt_payload, Mapping):
        payload["causal_receipt"] = dict(causal_receipt_payload)
    return seat_health_policy.apply_health_fields(
        payload,
        health=health,
        worker_failures=worker_failure_summary,
        transport_routing=transport_routing,
    )


def _direct_worker_error(worker: str, roster: Roster, *, read_only: bool = False) -> str | None:
    agent = roster.agents.get(worker)
    if agent is None:
        return f"unknown worker: {worker}"
    if worker == roster.orchestrator:
        return f"--worker cannot target orchestrator seat: {worker}"
    if read_only:
        capability_error = read_only_capability_error(agent)
        if capability_error is not None:
            return capability_error
    if agent.cli is None:
        return f"worker has no CLI adapter: {worker}"
    if not is_cli_allowed(agent.cli, roster):
        return f"{agent.cli} is not allowed by limits.allow_models"
    return None


def record_run_start(
    output_dir: Path,
    *,
    task: str,
    cwd: Path | None,
    roster: Roster,
    read_only: bool,
    worker: str | None = None,
    dry_run: bool = False,
    lock_workspace: Path | None = None,
    codex_transport: str | None = None,
    started_at: datetime | None = None,
    scheduler: str | None = None,
    verification_contract_payload: Mapping[str, Any] | None = None,
    run_budget_payload: Mapping[str, Any] | None = None,
    kind: str = "work",
) -> bool:
    """Write the minimal typed receipt needed before optional or blocking work.

    The requested scheduler is recorded here so a run that dies before dispatch
    still says what it was asked to do; ``used`` stays None until dispatch
    resolves it. ``record_run_termination`` merges into this payload, so the
    field survives a startup failure.
    """

    if run_budget_payload is not None:
        run_budget_payload = run_budget.validate_explicit_declaration(run_budget_payload)
    output_dir = output_dir.expanduser().resolve()
    started_at = started_at or datetime.now(timezone.utc)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_json = output_dir / "run.json"
    run_json_exists = run_json.is_file()
    existing_lifecycle_requested = False
    existing_authority_requested = False
    existing_verification_contract: dict[str, Any] | None = None
    existing_run_budget: dict[str, Any] | None = None
    existing_kind: str | None = None
    existing_causal_receipt: dict[str, Any] | None = None
    existing_retry_decisions: list[dict[str, Any]] | None = None
    if run_json_exists:
        try:
            existing = json.loads(run_json.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise runguard.RetainRunLockError(
                "existing run.json is unreadable as JSON; refusing to overwrite unknown durable enrollment state"
            ) from exc
        except OSError as exc:
            raise runguard.RetainRunLockError(
                "failed to read existing run.json; refusing to overwrite unknown durable enrollment state"
            ) from exc
        if not isinstance(existing, dict):
            raise runguard.RetainRunLockError(
                "existing run.json is not a JSON object; refusing to overwrite unknown durable enrollment state"
            )
        if existing.get("lifecycle_journal_requested") is True:
            existing_lifecycle_requested = True
        if existing.get(_AUTHORITY_REQUEST_FIELD) is True:
            existing_authority_requested = True
        if isinstance(existing.get("verification_contract"), dict):
            existing_verification_contract = dict(existing["verification_contract"])
        if isinstance(existing.get("run_budget"), dict):
            existing_run_budget = dict(existing["run_budget"])
        if isinstance(existing.get("kind"), str) and existing["kind"].strip():
            existing_kind = existing["kind"].strip()
        if isinstance(existing.get("causal_receipt"), dict):
            existing_causal_receipt = dict(existing["causal_receipt"])
        existing_retry = existing.get("retry_decisions")
        if isinstance(existing_retry, list):
            existing_retry_decisions = [dict(entry) for entry in existing_retry if isinstance(entry, dict)]
    # Every new run carries both durable request fields. Existing runs enroll
    # only from their stored run.json fields, so legacy snapshot-only runs stay
    # untouched. An authority request implies lifecycle journaling even when an
    # older or repaired receipt retained only the authority field.
    new_run = not run_json_exists
    lifecycle_requested = existing_lifecycle_requested or existing_authority_requested or new_run
    authority_requested = existing_authority_requested or new_run
    contract_payload = (
        dict(verification_contract_payload)
        if isinstance(verification_contract_payload, Mapping)
        else existing_verification_contract
    )
    budget_payload = dict(run_budget_payload) if isinstance(run_budget_payload, Mapping) else existing_run_budget
    receipt_kind = kind if new_run or kind != "work" else (existing_kind or kind)
    # The first run.json write activates the lifecycle journal and publishes a
    # recovery checkpoint BEFORE the atomic run.json replacement. If that final
    # replacement fails, durable journal/checkpoint state already exists without
    # a run.json, so ``_terminalize_run_lifecycle.terminate_existing`` cannot
    # terminalize (run.json is absent) and the lock must be retained for
    # ``brigade runs recover``. Translate the bounded lifecycle write failures
    # to ``RetainRunLockError`` so ``runguard.run_lock`` keeps the lock instead
    # of releasing it and orphaning the recovery state.
    try:
        _write_json(
            output_dir / "run.json",
            _run_payload(
                task=task,
                cwd=cwd,
                lock_workspace=lock_workspace if lock_workspace is not None else cwd,
                roster=roster,
                dry_run=dry_run,
                read_only=read_only,
                status="started",
                started_at=started_at,
                output_dir=output_dir,
                code_graph=CodeGraphBrief(attached=False),
                drift_impact=DriftImpactBrief(attached=False),
                evidence=EvidenceBrief(attached=False),
                codex_transport=codex_transport or roster.codex_transport,
                worker=worker,
                include_git=False,
                scheduler=(
                    {"requested": scheduler, "used": None, "fallback_reason": None} if scheduler is not None else None
                ),
                lifecycle_journal_requested=True if lifecycle_requested else None,
                run_journal_authority_requested=True if authority_requested else None,
                verification_contract_payload=contract_payload,
                run_budget_payload=budget_payload,
                kind=receipt_kind,
                causal_receipt_payload=(
                    existing_causal_receipt
                    if existing_causal_receipt is not None
                    else (causal_receipt.recorded_run(run_id=output_dir.name) if new_run else None)
                ),
                retry_decisions=existing_retry_decisions,
            ),
        )
    except (OSError, run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError) as exc:
        raise runguard.RetainRunLockError(f"failed to write initial run receipt: {exc}") from exc
    _write_json(output_dir / "roster.json", _roster_payload(roster))
    return lifecycle_requested or authority_requested


def update_run_receipt(output_dir: Path, **fields: object) -> dict[str, object]:
    path = output_dir.expanduser().resolve() / "run.json"
    current = _read_json_dict(path)
    if current is None:
        raise FileNotFoundError(path)
    current.update(fields)
    _write_json(path, current)
    return current


@contextmanager
def terminal_sigterm_handler(
    output_dir: Path | None,
    *,
    seat: str | None,
    before_record: Callable[[], None] | None = None,
    should_record: Callable[[], bool] | None = None,
) -> Iterator[None]:
    """Terminalize SIGTERM while preserving the conventional 128+signal exit."""

    if output_dir is None or threading.current_thread() is not threading.main_thread():
        yield
        return

    output_dir = output_dir.expanduser()
    previous = signal.getsignal(signal.SIGTERM)

    def handle_sigterm(signum: int, frame: object) -> None:  # noqa: ARG001
        if should_record is not None and not should_record():
            raise SystemExit(128 + signum)
        if before_record is not None:
            before_record()
        run_path = output_dir / "run.json"
        if run_path.is_file():
            active_seats: tuple[str, ...] = ()
            active_seat = seat
            try:
                payload = json.loads(run_path.read_text())
            except (OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                stored_active = payload.get("active_seats")
                if isinstance(stored_active, list):
                    active_seats = tuple(value for value in stored_active if isinstance(value, str) and value)
                    if len(active_seats) == 1:
                        active_seat = active_seats[0]
                    elif active_seats:
                        active_seat = None
                if not active_seats:
                    phase_owner = payload.get("phase_owner")
                    if isinstance(phase_owner, str) and phase_owner:
                        active_seat = phase_owner
                    else:
                        stored_worker = payload.get("worker")
                        if isinstance(stored_worker, str) and stored_worker:
                            active_seat = stored_worker
            record_run_termination(
                output_dir,
                status="canceled",
                failure_phase=None,
                failure_kind="signal",
                detail="run terminated by SIGTERM",
                seat=active_seat,
                active_seats=active_seats,
            )
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handle_sigterm)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _terminalize_run_lifecycle(function: Callable[..., int]) -> Callable[..., int]:
    """Ensure every artifact-producing run leaves a terminal receipt on escape."""

    @wraps(function)
    def wrapped(*args: Any, **kwargs: Any) -> int:
        raw_output_dir = kwargs.get("output_dir")
        output_dir = raw_output_dir.expanduser() if isinstance(raw_output_dir, Path) else None
        raw_roster = args[1] if len(args) > 1 else kwargs.get("roster")
        raw_worker = kwargs.get("worker")
        seat = (
            raw_worker
            if isinstance(raw_worker, str) and raw_worker
            else raw_roster.orchestrator
            if isinstance(raw_roster, Roster)
            else None
        )

        def terminate_existing(
            *, status: str, failure_kind: str, detail: str, failure_phase: str | None = None
        ) -> None:
            if output_dir is None or not (output_dir / "run.json").is_file():
                return
            record_run_termination(
                output_dir,
                status=status,
                failure_phase=failure_phase,
                failure_kind=failure_kind,
                detail=detail,
                seat=seat,
            )

        try:
            with terminal_sigterm_handler(output_dir, seat=seat):
                return function(*args, **kwargs)
        except runguard.RetainRunLockError:
            raise
        except KeyboardInterrupt:
            terminate_existing(
                status="canceled",
                failure_kind="keyboard-interrupt",
                detail="run canceled by user",
            )
            raise
        except TimeoutError as exc:
            terminate_existing(
                status="timeout",
                failure_kind="timeout",
                detail=_one_line(str(exc)) or "run timed out",
            )
            raise
        except Exception as exc:
            terminate_existing(
                status="failed",
                failure_kind="unexpected-error",
                detail=f"{type(exc).__name__}: {_one_line(str(exc)) or 'unexpected run failure'}",
            )
            raise

    return wrapped


@dataclass(frozen=True)
class OrchestratorHealthRoutingDecision:
    roster: Roster
    warning: str | None = None
    abort_detail: str | None = None
    abort_failure_phase: str = "preflight"
    abort_failure_kind: str = "unclassified"
    abort_seat: str | None = None
    receipt: dict[str, object] | None = None


def _seat_health_result_for(
    results: tuple[seat_health.SeatHealthResult, ...] | None,
    seat: str,
) -> seat_health.SeatHealthResult | None:
    if results is None:
        return None
    return next((result for result in results if result.seat == seat), None)


def _seat_health_typed_cause(result: seat_health.SeatHealthResult) -> str:
    if result.failure is not None:
        return result.failure.failure_class.value
    failed = next((check for check in result.checks if check.status == "failed"), None)
    if failed is not None and failed.cause_code:
        return failed.cause_code
    return "unclassified"


def resolve_orchestrator_health_routing(
    roster: Roster,
    results: tuple[seat_health.SeatHealthResult, ...] | None,
    *,
    pinned_seats: frozenset[str] = frozenset(),
    worker_seat: str | None = None,
) -> OrchestratorHealthRoutingDecision:
    """Route unhealthy orchestrator and worker seats to declared fallbacks.

    ``worker_seat`` marks a direct-worker run (``--worker``): only the selected
    worker and its declared fallbacks are health-gated, so an unused
    orchestrator can neither reroute nor abort the run.
    """
    if results is None:
        return OrchestratorHealthRoutingDecision(roster=roster)

    referenced = {name for agent in roster.agents.values() for name in agent.fallback}
    # Roots are the orchestrator plus seats not referenced as fallbacks by any other role.
    roots = [name for name in roster.agents if name == roster.orchestrator or name not in referenced]
    direct_worker = worker_seat is not None
    if direct_worker and worker_seat in roster.agents:
        # A pinned worker never dispatches through the orchestrator, so only the
        # worker seat chain is health-gated.
        roots = [worker_seat]
    effective_agents = dict(roster.agents)
    effective_orchestrator = roster.orchestrator
    decisions: list[dict[str, object]] = []
    warnings: list[str] = []
    abort: tuple[str, seat_health.SeatHealthResult, list[dict[str, str]]] | None = None

    for requested in roots:
        health_result = _seat_health_result_for(results, requested)
        if health_result is None or health_result.status != "unhealthy":
            continue
        typed_cause = _seat_health_typed_cause(health_result)
        rejected: list[dict[str, str]] = []
        selected: str | None = None
        for fallback_name in roster.agents[requested].fallback:
            fallback_result = _seat_health_result_for(results, fallback_name)
            if fallback_result is not None and fallback_result.status == "healthy":
                selected = fallback_name
                break
            status = fallback_result.status if fallback_result is not None else "missing"
            cause = _seat_health_typed_cause(fallback_result) if fallback_result is not None else "missing"
            rejected.append({"seat": fallback_name, "status": status, "cause": cause})

        decision: dict[str, object] = {
            "requested_seat": requested,
            "effective_seat": selected,
            "outcome": "fallback" if selected is not None else "skip",
            "typed_cause": typed_cause,
            "rejected_fallbacks": rejected,
        }
        decisions.append(decision)
        if selected is not None:
            if requested == roster.orchestrator:
                effective_orchestrator = selected
                warnings.append(
                    f"warning: orchestrator {requested} is unhealthy [{typed_cause}]; "
                    f"using declared fallback {selected}"
                )
            else:
                fallback = roster.agents[selected]
                requested_agent = roster.agents[requested]
                effective_agents[requested] = replace(
                    fallback,
                    name=requested,
                    role=requested_agent.role,
                    purpose=requested_agent.purpose,
                    fallback=(),
                )
                warnings.append(
                    f"warning: seat {requested} is unhealthy [{typed_cause}]; using declared fallback {selected}"
                )
        elif requested == roster.orchestrator or requested in pinned_seats:
            abort = (requested, health_result, rejected)
            decision["outcome"] = "abort"
        else:
            effective_agents.pop(requested, None)
            warnings.append(f"warning: skipping unhealthy seat {requested} [{typed_cause}]; no healthy fallback")

    if not decisions:
        return OrchestratorHealthRoutingDecision(roster=roster)

    effective = replace(
        roster,
        agents=effective_agents,
        orchestrator=effective_orchestrator,
        seat_routing=(*roster.seat_routing, *decisions),
    )
    receipt: dict[str, object] = {"schema": "brigade.seat_routing.v1", "decisions": decisions}
    if direct_worker:
        receipt["run_mode"] = "direct-worker"
    orchestrator_decision = next(
        (decision for decision in decisions if decision["requested_seat"] == roster.orchestrator), None
    )
    if orchestrator_decision is not None:
        # Preserve the original v1 summary for existing artifact consumers.
        receipt.update(
            {
                "requested_orchestrator": roster.orchestrator,
                "effective_orchestrator": orchestrator_decision["effective_seat"],
                "outcome": orchestrator_decision["outcome"],
                "typed_cause": orchestrator_decision["typed_cause"],
                "rejected_fallbacks": orchestrator_decision["rejected_fallbacks"],
            }
        )

    if abort is None:
        return OrchestratorHealthRoutingDecision(
            roster=effective,
            warning="\n".join(warnings) or None,
            receipt=receipt,
        )

    requested, unhealthy_result, rejected = abort
    typed_cause = _seat_health_typed_cause(unhealthy_result)
    if rejected:
        rejected_detail = "; rejected fallbacks: " + ", ".join(
            f"{entry['seat']} ({entry['status']}[{entry['cause']}])" for entry in rejected
        )
    else:
        rejected_detail = "; no declared fallbacks"
    if requested == roster.orchestrator:
        detail = f"orchestrator {requested} is unhealthy [{typed_cause}]{rejected_detail}"
    else:
        detail = f"seat {requested} is unhealthy [{typed_cause}]{rejected_detail}"
    failure = unhealthy_result.failure
    return OrchestratorHealthRoutingDecision(
        roster=effective,
        warning="\n".join(warnings) or None,
        abort_detail=detail,
        abort_failure_phase=failure.phase.value if failure is not None else "preflight",
        abort_failure_kind=failure.failure_class.value if failure is not None else "unclassified",
        abort_seat=requested,
        receipt=receipt,
    )


def _write_seat_routing_receipt(output_dir: Path, receipt: dict[str, object]) -> None:
    try:
        _write_json(output_dir / "seat-routing.json", receipt)
    except OSError as exc:
        print(f"error: seat routing receipt failed: {exc}", file=sys.stderr)


def _write_run_seat_health_receipt(
    output_dir: Path,
    roster: Roster,
    *,
    cwd: Path | None = None,
    probe: seat_health.SeatHealthProbe | None = None,
    require_hard_isolation: bool = False,
    sandbox: str | None = None,
) -> tuple[seat_health.SeatHealthResult, ...] | None:
    """Probe declared seats and write seat-health.json beside run.json.

    Returns probe results on success, including when the receipt write fails.
    Returns ``None`` when the probe itself raised so routing treats the run as
    observation-only.  ``allow_model_smoke`` stays off: paying for a model call
    per seat on every run is a routing decision that does not belong to
    admission.
    """
    active_probe = probe or seat_health.SeatHealthProbe(collect_executable_version=False)
    probe_failed = False
    try:
        results = active_probe.probe_roster(
            roster,
            workspace=cwd,
            allow_model_smoke=False,
            require_hard_isolation=require_hard_isolation,
            sandbox=sandbox,
        )
    except Exception as exc:
        probe_failed = True
        results = seat_health.exception_results_for_probe_failure(roster, exc)
    try:
        seat_health.write_seat_health_receipt(
            output_dir / "seat-health.json",
            results,
            run_id=output_dir.name,
        )
    except OSError as exc:
        print(f"error: seat health receipt failed: {exc}", file=sys.stderr)
    if probe_failed:
        return None
    return results


@_terminalize_run_lifecycle
@_model_lease_lifecycle
def run(
    task: str,
    roster: Roster,
    *,
    dry_run: bool = False,
    show_plan: bool = False,
    verbose: bool = False,
    cwd: Path | None = None,
    lock_workspace: Path | None = None,
    output_dir: Path | None = None,
    handoff_inbox: Path | None = None,
    read_only: bool = False,
    sandbox_read_only: bool | None = None,
    sandbox: str | None = None,
    code_graph_enabled: bool = True,
    evidence_enabled: bool = True,
    code_graph: CodeGraphBrief | None = None,
    drift_impact: DriftImpactBrief | None = None,
    evidence: EvidenceBrief | None = None,
    codex_transport: str | None = None,
    route_enabled: bool = True,
    route_approvals: tuple[str, ...] = (),
    route_template: str | None = None,
    route_overrides: tuple[str, ...] = (),
    worker: str | None = None,
    model_override: str | None = None,
    allow_shadow: bool = False,
    authorized_writable_worktree: bool = False,
    fail_fast: bool = True,
    scheduler: str = "waves",
    defer_artifact_collection: bool = False,
    verification_contract_payload: Mapping[str, Any] | None = None,
    run_budget_payload: Mapping[str, Any] | None = None,
    model_policy_snapshot: Mapping[str, Any] | None = None,
    model_lease_error: str | None = None,
) -> int:
    if run_budget_payload is not None:
        run_budget_payload = run_budget.validate_explicit_declaration(run_budget_payload)
    started_at = datetime.now(timezone.utc)
    process_registry = proc.ProcessRegistry()
    transport_for_payload = codex_transport or roster.codex_transport
    cwd = cwd.expanduser().resolve() if cwd is not None else None
    lock_workspace = lock_workspace.expanduser().resolve() if lock_workspace is not None else cwd
    output_dir = output_dir.expanduser() if output_dir is not None else None
    handoff_inbox = handoff_inbox.expanduser() if handoff_inbox is not None else None
    direct_worker = worker is not None
    # The command-line sandbox override is part of the effective isolation input,
    # so seat-health preflight must judge it rather than the static declaration.
    effective_sandbox = sandbox if sandbox is not None else ("read-only" if read_only else None)
    durable_enrollment_expected = False

    # What the roster/flag asked for vs what dispatch actually ran. `used` stays
    # None until dispatch resolves it, so a run that dies before dispatch reads
    # as "requested dag, never got there" rather than falsely claiming a mode.
    scheduler_resolution: dict[str, object] = {
        "requested": scheduler,
        "used": None,
        "fallback_reason": None,
    }
    health_summary_payload: dict[str, object] | None = None
    transport_routing_payload: dict[str, object] | None = None
    quarantine_state = seat_health_policy.SeatQuarantineState()
    active_health_probe = seat_health.SeatHealthProbe(collect_executable_version=False)

    model_policy = resolve_fleet_model_policy(
        roster,
        worker=worker,
        model_override=model_override,
        snapshot=model_policy_snapshot,
    )
    roster = model_policy.roster
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        _write_json(output_dir / "model-policy.json", model_policy.receipt)
        run_path = output_dir / "run.json"
        if roster.seat_routing and run_path.is_file():
            update_run_receipt(
                output_dir,
                seat_routing=[dict(decision) for decision in roster.seat_routing],
            )
        _write_json(output_dir / "roster.json", _roster_payload(roster))
    if model_policy.error is not None or model_lease_error is not None:
        policy_error = model_policy.error or model_lease_error
        if output_dir is not None and not (output_dir / "run.json").is_file():
            record_run_start(
                output_dir,
                task=task,
                cwd=cwd,
                roster=roster,
                read_only=read_only,
                worker=worker,
                dry_run=dry_run,
                lock_workspace=lock_workspace,
                codex_transport=transport_for_payload,
                started_at=started_at,
                scheduler=scheduler,
                verification_contract_payload=verification_contract_payload,
                run_budget_payload=run_budget_payload,
            )
        if output_dir is not None and (output_dir / "run.json").is_file():
            record_run_termination(
                output_dir,
                status="failed",
                failure_phase="preflight",
                failure_kind="fleet-model-policy",
                detail=policy_error,
                seat=worker or roster.orchestrator,
            )
        print(f"error: {policy_error}", file=sys.stderr)
        return 2

    def scheduler_resolved(used: str, fallback_reason: str | None) -> None:
        scheduler_resolution["used"] = used
        scheduler_resolution["fallback_reason"] = fallback_reason

    def _payload(**kwargs: Any) -> dict[str, object]:
        if "skill_route_policy" not in kwargs and skill_policy is not None:
            kwargs["skill_route_policy"] = skill_policy
        if "retry_decisions" not in kwargs and quarantine_state.retry_decisions:
            kwargs["retry_decisions"] = [dict(entry) for entry in quarantine_state.retry_decisions]
        if health_summary_payload is not None and "health" not in kwargs:
            kwargs["health"] = health_summary_payload
        if transport_routing_payload is not None and "transport_routing" not in kwargs:
            kwargs["transport_routing"] = transport_routing_payload
        if output_dir is not None and (
            "lifecycle_journal_requested" not in kwargs or "run_journal_authority_requested" not in kwargs
        ):
            run_path = output_dir / "run.json"
            try:
                run_info = os.lstat(run_path)
            except FileNotFoundError as exc:
                if durable_enrollment_expected:
                    raise runguard.RetainRunLockError("refusing to overwrite unknown durable enrollment state") from exc
                run_info = None
            except OSError as exc:
                if durable_enrollment_expected:
                    raise runguard.RetainRunLockError("refusing to overwrite unknown durable enrollment state") from exc
                run_info = None
            if run_info is not None and (not os.path.isfile(run_path) or os.path.islink(run_path)):
                if durable_enrollment_expected:
                    raise runguard.RetainRunLockError("refusing to overwrite unknown durable enrollment state")
            elif run_info is not None:
                try:
                    existing = json.loads(run_path.read_text())
                except (OSError, ValueError, RecursionError) as exc:
                    if durable_enrollment_expected:
                        raise runguard.RetainRunLockError(
                            "refusing to overwrite unknown durable enrollment state"
                        ) from exc
                    existing = None
                if not isinstance(existing, dict) and durable_enrollment_expected:
                    raise runguard.RetainRunLockError("refusing to overwrite unknown durable enrollment state")
                if isinstance(existing, dict):
                    if (
                        "lifecycle_journal_requested" not in kwargs
                        and existing.get("lifecycle_journal_requested") is True
                    ):
                        kwargs["lifecycle_journal_requested"] = True
                    if (
                        "run_journal_authority_requested" not in kwargs
                        and existing.get("run_journal_authority_requested") is True
                    ):
                        kwargs["run_journal_authority_requested"] = True
                    if "health" not in kwargs and isinstance(existing.get("health"), dict):
                        kwargs["health"] = dict(existing["health"])
                    if "transport_routing" not in kwargs and isinstance(existing.get("transport_routing"), dict):
                        kwargs["transport_routing"] = dict(existing["transport_routing"])
                    if "verification_contract_payload" not in kwargs and isinstance(
                        existing.get("verification_contract"), dict
                    ):
                        kwargs["verification_contract_payload"] = dict(existing["verification_contract"])
                    if "run_budget_payload" not in kwargs and isinstance(existing.get("run_budget"), dict):
                        kwargs["run_budget_payload"] = dict(existing["run_budget"])
                    if "kind" not in kwargs and isinstance(existing.get("kind"), str) and existing["kind"].strip():
                        kwargs["kind"] = existing["kind"].strip()
                    if "causal_receipt_payload" not in kwargs and isinstance(existing.get("causal_receipt"), dict):
                        kwargs["causal_receipt_payload"] = dict(existing["causal_receipt"])
                    existing_retry = existing.get("retry_decisions")
                    if isinstance(existing_retry, list):
                        prior = [dict(entry) for entry in existing_retry if isinstance(entry, dict)]
                        fresh = [
                            dict(entry) for entry in (kwargs.get("retry_decisions") or []) if isinstance(entry, dict)
                        ]
                        seen = {json.dumps(entry, sort_keys=True) for entry in prior}
                        kwargs["retry_decisions"] = prior + [
                            entry for entry in fresh if json.dumps(entry, sort_keys=True) not in seen
                        ]
        return _run_payload(
            lock_workspace=lock_workspace,
            pre_run_snapshot=pre_run_snapshot_payload,
            scheduler=dict(scheduler_resolution),
            **kwargs,
        )

    skill_policy: RoutePolicyDecision | None = None

    # Capture pre-run git state before any worker touches the tree so ground
    # truth can attribute only the worker's changes and a drift check can fail
    # the run if branch or HEAD moves out from under it. The in-memory snapshot
    # is captured now (before dispatch); the persisted copy is written once
    # output_dir exists. A git work tree whose state cannot be read is a
    # preflight failure (not a silent downgrade): the run refuses to start
    # rather than running without isolation attribution.
    try:
        pre_run_snapshot = runguard.capture_pre_run_snapshot(cwd)
        # Branch/HEAD drift is checked against the resolved lock workspace, which
        # may be a separate canonical checkout from the assigned cwd worktree.
        # The cwd snapshot stays the ground-truth baseline for worker change
        # attribution; a separate drift snapshot is captured from lock_workspace
        # before dispatch when it differs, so movement of the canonical checkout
        # is detected even though the assigned worktree never moved. When they
        # are the same path, reuse the cwd snapshot to preserve explicit
        # canonical-write behavior.
        if lock_workspace != cwd:
            drift_snapshot = runguard.capture_pre_run_snapshot(lock_workspace)
        else:
            drift_snapshot = pre_run_snapshot
    except runguard.RunGuardError as exc:
        print(f"error: pre-run snapshot failed: {exc}", file=sys.stderr)
        return 2
    pre_run_snapshot_payload = runguard.snapshot_payload(pre_run_snapshot)

    def _drift_failure_rc() -> int | None:
        """Centralized branch/HEAD drift check across every return path.

        Returns 2 (and records a run-isolation failure) when branch or HEAD
        moved since the drift snapshot taken from the lock workspace, else
        None so the caller can proceed. Call this after planning, before any
        dry-run return, after worker dispatch, after synthesis, and
        immediately before finalization.
        """
        detail = runguard.detect_branch_head_drift(lock_workspace, drift_snapshot)
        if detail is None:
            return None
        if output_dir is not None:
            record_run_termination(
                output_dir,
                status="failed",
                failure_phase="run-isolation",
                failure_kind="branch-head-drift",
                detail=detail,
                seat=roster.orchestrator,
            )
        print(f"error: {detail}", file=sys.stderr)
        return 2

    def _run_isolation_check() -> str | None:
        """Detect canonical-checkout escapes after dispatch without per-worker attribution."""

        if lock_workspace == cwd or drift_snapshot is None:
            return None
        # Preserve the existing branch/HEAD drift classification. Working-tree
        # mutations are what this post-dispatch check adds; committed or
        # branch-moving interference remains caught by the run-level drift
        # checkpoints.
        if runguard.detect_branch_head_drift(lock_workspace, drift_snapshot) is not None:
            return None
        try:
            changed, untracked = runguard.changes_relative_to_snapshot(lock_workspace, drift_snapshot)
        except runguard.RunGuardError as exc:
            return f"isolation breach: could not verify canonical checkout: {exc}"
        escaped = sorted(set(changed + untracked))
        if not escaped:
            return None
        shown = ", ".join(escaped[:10])
        if len(escaped) > 10:
            shown += f", and {len(escaped) - 10} more"
        return f"isolation breach: modified files outside assigned worktree: {shown}"

    if worker is not None:
        worker_error = _direct_worker_error(worker, roster, read_only=read_only)
        if worker_error is not None:
            print(f"error: {worker_error}", file=sys.stderr)
            return 2
    if output_dir is not None:
        durable_enrollment_expected = record_run_start(
            output_dir,
            task=task,
            cwd=cwd,
            roster=roster,
            read_only=read_only,
            worker=worker,
            dry_run=dry_run,
            lock_workspace=lock_workspace,
            codex_transport=transport_for_payload,
            started_at=started_at,
            scheduler=scheduler,
            verification_contract_payload=verification_contract_payload,
            run_budget_payload=run_budget_payload,
        )
    if code_graph is None:
        code_graph = code_graph_brief(cwd, task) if code_graph_enabled else CodeGraphBrief(attached=False)
    if drift_impact is None:
        drift_impact = drift_impact_brief(cwd) if code_graph_enabled else DriftImpactBrief(attached=False)
    if evidence is None:
        evidence = (
            evidence_brief_mod.evidence_brief(cwd, task)
            if code_graph_enabled and evidence_enabled
            else EvidenceBrief(attached=False)
        )
    brief_set = arbitrate_briefs(task, code_graph=code_graph, drift_impact=drift_impact, evidence=evidence)
    code_graph = brief_set.code_graph
    drift_impact = brief_set.drift_impact
    evidence = brief_set.evidence
    route = (
        route_brief(
            task,
            template=route_template,
            changed_paths=_route_changed_paths(cwd),
            approvals=route_approvals,
            overrides=route_overrides,
        )
        if route_enabled
        else None
    )
    code_graph_delta = _initial_code_graph_delta(
        code_graph_enabled=code_graph_enabled,
        dry_run=dry_run,
        read_only=read_only,
        cwd=cwd,
    )
    code_graph_delta_before: dict[str, object] | None = None
    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        if pre_run_snapshot_payload is not None:
            _write_json(output_dir / "pre-run-snapshot.json", pre_run_snapshot_payload)
        if code_graph_delta is None and cwd is not None:
            code_graph_delta_before = graphtrail_delta.capture_before(cwd, output_dir)
            code_graph_delta = code_graph_delta_before
        _write_json(output_dir / "roster.json", _roster_payload(roster))
        _write_json(
            output_dir / "run.json",
            _payload(
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
                evidence=evidence,
                brief_set=brief_set,
                codex_transport=transport_for_payload,
                route=route,
                code_graph_delta=code_graph_delta,
                worker=worker,
            ),
        )
        # After the initial run receipt, before planning: a probe failure stays receipted.
        health_results = _write_run_seat_health_receipt(
            output_dir,
            roster,
            cwd=cwd,
            probe=active_health_probe,
            require_hard_isolation=read_only,
            sandbox=effective_sandbox,
        )
        routing = resolve_orchestrator_health_routing(
            roster,
            health_results,
            pinned_seats=frozenset({worker}) if worker is not None else frozenset(),
            worker_seat=worker,
        )
        roster = routing.roster
        health_summary_payload = seat_health_policy.seat_health_summary(
            health_results,
            routing_decisions=roster.seat_routing,
        )
        # Record the effective sandbox the preflight judged. The run receipt's
        # field inventory is closed, so the override lives under the preserved
        # health payload rather than as a new top-level run.json key.
        if isinstance(health_summary_payload, dict):
            health_summary_payload["effective_sandbox"] = effective_sandbox
        if routing.warning is not None:
            print(routing.warning, file=sys.stderr)
        if worker is not None and roster.seat_routing:
            for decision in roster.seat_routing:
                if decision.get("requested_seat") != worker or decision.get("outcome") != "fallback":
                    continue
                effective = decision.get("effective_seat")
                cause = decision.get("typed_cause") or "unclassified"
                if isinstance(effective, str):
                    print(
                        seat_health_policy.format_worker_seat_resolution(
                            requested=worker,
                            effective=effective,
                            typed_cause=str(cause),
                        ),
                        file=sys.stderr,
                    )
                break
        if routing.receipt is not None:
            routing_receipt = dict(routing.receipt)
            routing_receipt["run_id"] = output_dir.name
            _write_seat_routing_receipt(output_dir, routing_receipt)
        if routing.abort_detail is not None:
            if roster.seat_routing or health_summary_payload is not None:
                run_path = output_dir / "run.json"
                try:
                    run_payload = json.loads(run_path.read_text())
                except (OSError, json.JSONDecodeError) as exc:
                    raise runguard.RetainRunLockError(
                        f"failed to read run receipt before seat-routing abort: {exc}"
                    ) from exc
                if isinstance(run_payload, dict):
                    if roster.seat_routing:
                        run_payload["seat_routing"] = [dict(decision) for decision in roster.seat_routing]
                    if health_summary_payload is not None:
                        run_payload["health"] = dict(health_summary_payload)
                    _write_json(run_path, receipt_schema.stamp_run_receipt(run_payload))
            record_run_termination(
                output_dir,
                status="failed",
                failure_phase=routing.abort_failure_phase,
                failure_kind=routing.abort_failure_kind,
                detail=routing.abort_detail,
                seat=routing.abort_seat or roster.orchestrator,
            )
            print(f"error: {routing.abort_detail}", file=sys.stderr)
            return 2

    if cwd is not None and route is not None and route.attached:
        from .route_policy import decide_route_skills, route_policy_extensions_from_decision
        from .route_receipts import write_route_decision

        skill_policy = decide_route_skills(
            cwd,
            route_brief=route,
            runs_dir=output_dir.parent if output_dir is not None else None,
            now=started_at,
            allow_shadow=allow_shadow if direct_worker else True,
        )
        if output_dir is not None and skill_policy.policy_applied:
            write_route_decision(
                output_dir,
                roster,
                runs_dir=output_dir.parent,
                policy_extensions=route_policy_extensions_from_decision(skill_policy),
            )
            _write_json(
                output_dir / "run.json",
                _payload(
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
                    evidence=evidence,
                    brief_set=brief_set,
                    codex_transport=transport_for_payload,
                    route=route,
                    code_graph_delta=code_graph_delta,
                    worker=worker,
                ),
            )

    control_socket = None
    control_transport = None
    plan_attempts: list[dict[str, object]] | None = [] if output_dir is not None else None
    if worker is not None:
        selected_skill_ids, bind_error = direct_worker_skill_ids(
            skill_policy,
            allow_shadow=allow_shadow,
        )
        if bind_error is not None:
            print(f"error: {bind_error}", file=sys.stderr)
            return 2
        assignments = [
            Assignment(
                worker=worker,
                task=task,
                stage=1,
                selected_skill_ids=selected_skill_ids,
            )
        ]
    else:
        if output_dir is not None:
            _write_json(
                output_dir / "run.json",
                _payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="planning",
                    started_at=started_at,
                    output_dir=output_dir,
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    evidence=evidence,
                    brief_set=brief_set,
                    codex_transport=transport_for_payload,
                    route=route,
                    code_graph_delta=code_graph_delta,
                    worker=worker,
                ),
            )
        try:
            assignments = _call_with_process_registry(
                plan,
                task,
                roster,
                cwd=cwd,
                read_only=read_only,
                sandbox_read_only=sandbox_read_only,
                sandbox=sandbox,
                attempts=plan_attempts,
                code_graph=code_graph,
                drift_impact=drift_impact,
                evidence=evidence,
                route=route,
                skill_policy=skill_policy,
                codex_transport=transport_for_payload,
                process_registry=process_registry,
                output_dir=output_dir,
            )
        except RuntimeError as exc:
            final_attempt = plan_attempts[-1] if plan_attempts else None
            timed_out = isinstance(final_attempt, dict) and final_attempt.get("timed_out") is True
            if isinstance(final_attempt, dict) and isinstance(final_attempt.get("failure_kind"), str):
                failure_kind = final_attempt["failure_kind"]
            elif isinstance(final_attempt, dict) and isinstance(final_attempt.get("parse_error"), str):
                failure_kind = "invalid-plan"
            else:
                failure_kind = "orchestrator-error"
            if timed_out:
                failure_kind = "timeout"
            if output_dir is not None:
                finished_at = datetime.now(timezone.utc)
                _write_json(output_dir / "plan-attempts.json", {"attempts": plan_attempts or []})
                failure_phase = "planning"
                if isinstance(final_attempt, dict):
                    attempt_phase = final_attempt.get("failure_phase")
                    attempt_kind = final_attempt.get("failure_kind")
                    if attempt_phase == "preflight" and attempt_kind == "provider-config":
                        failure_phase = "preflight"
                _write_json(
                    output_dir / "run.json",
                    _payload(
                        task=task,
                        cwd=cwd,
                        roster=roster,
                        dry_run=dry_run,
                        read_only=read_only,
                        status="timeout" if timed_out else "failed",
                        started_at=started_at,
                        finished_at=finished_at,
                        output_dir=output_dir,
                        error=str(exc),
                        failure_phase=failure_phase,
                        failure_kind=failure_kind,
                        failure_seat=roster.orchestrator,
                        code_graph=code_graph,
                        drift_impact=drift_impact,
                        evidence=evidence,
                        brief_set=brief_set,
                        codex_transport=transport_for_payload,
                        route=route,
                        control_socket=control_socket,
                        control_transport=control_transport,
                        code_graph_delta=code_graph_delta,
                        worker=worker,
                    ),
                )
            print(f"error: {exc}", file=sys.stderr)
            return 2

    # Drift checkpoint: after planning (and before any dry-run return). A
    # concurrent commit or branch switch during planning must fail the run
    # instead of letting a dry-run return a plan built on stale state.
    drift_rc = _drift_failure_rc()
    if drift_rc is not None:
        return drift_rc

    if output_dir is not None:
        attempts_payload: dict[str, object] = {"attempts": plan_attempts or []}
        if direct_worker:
            attempts_payload["mode"] = "direct-worker"
        _write_json(output_dir / "plan-attempts.json", attempts_payload)
        plan_doc = receipt_schema.run_plan_document(
            _assignment_payload(assignments),
            run_id=output_dir.name,
        )
        contract_for_plan = verification_contract_payload
        if contract_for_plan is None:
            try:
                run_raw = json.loads((output_dir / "run.json").read_text())
            except (OSError, json.JSONDecodeError):
                run_raw = None
            if isinstance(run_raw, dict) and isinstance(run_raw.get("verification_contract"), dict):
                contract_for_plan = dict(run_raw["verification_contract"])
        if isinstance(contract_for_plan, Mapping):
            plan_doc["verification_contract"] = dict(contract_for_plan)
        budget_for_plan = run_budget_payload
        if budget_for_plan is None:
            try:
                run_raw = json.loads((output_dir / "run.json").read_text())
            except (OSError, json.JSONDecodeError):
                run_raw = None
            if isinstance(run_raw, dict) and isinstance(run_raw.get("run_budget"), dict):
                budget_for_plan = dict(run_raw["run_budget"])
        if isinstance(budget_for_plan, Mapping):
            plan_doc["run_budget"] = dict(budget_for_plan)
        _write_json(output_dir / "plan.json", plan_doc)

    if cwd is not None and output_dir is not None:
        from . import candidate_set as candidate_set_mod

        assignments, _candidate_decision, candidate_failure = _apply_candidate_set_gate(
            cwd=cwd,
            output_dir=output_dir,
            assignments=assignments,
            roster=roster,
            task=task,
            read_only=read_only,
            sandbox_read_only=sandbox_read_only,
            sandbox=sandbox,
            code_graph=code_graph,
            drift_impact=drift_impact,
            evidence=evidence,
            route=route,
            skill_policy=skill_policy,
            transport_for_payload=transport_for_payload,
            process_registry=process_registry,
            plan_attempts=plan_attempts,
            allow_replan=not dry_run and not direct_worker,
        )
        plan_doc = receipt_schema.run_plan_document(
            _assignment_payload(assignments),
            run_id=output_dir.name,
        )
        try:
            prior_plan = json.loads((output_dir / "plan.json").read_text())
        except (OSError, json.JSONDecodeError):
            prior_plan = None
        if isinstance(prior_plan, dict) and isinstance(prior_plan.get("verification_contract"), dict):
            plan_doc["verification_contract"] = dict(prior_plan["verification_contract"])
        elif isinstance(verification_contract_payload, Mapping):
            plan_doc["verification_contract"] = dict(verification_contract_payload)
        if isinstance(prior_plan, dict) and isinstance(prior_plan.get("run_budget"), dict):
            plan_doc["run_budget"] = dict(prior_plan["run_budget"])
        elif isinstance(run_budget_payload, Mapping):
            plan_doc["run_budget"] = dict(run_budget_payload)
        _write_json(output_dir / "plan.json", plan_doc)
        if plan_attempts is not None:
            attempts_payload = {"attempts": plan_attempts or []}
            if direct_worker:
                attempts_payload["mode"] = "direct-worker"
            _write_json(output_dir / "plan-attempts.json", attempts_payload)
        if candidate_failure is not None and not dry_run:
            record_run_termination(
                output_dir,
                status="failed",
                failure_phase=candidate_set_mod.FAILURE_PHASE,
                failure_kind=candidate_set_mod.FAILURE_KIND,
                detail=candidate_failure,
                seat=roster.orchestrator,
            )
            print(f"error: {candidate_failure}", file=sys.stderr)
            return 2

    if dry_run:
        payload = {"assignments": _assignment_payload(assignments)}
        if output_dir is not None:
            finished_at = datetime.now(timezone.utc)
            _write_json(
                output_dir / "run.json",
                _payload(
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
                    evidence=evidence,
                    brief_set=brief_set,
                    codex_transport=transport_for_payload,
                    route=route,
                    code_graph_delta=code_graph_delta,
                    worker=worker,
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
    if effective_transport == "app-server" and not has_codex_workers:
        effective_transport = "exec"
    elif effective_transport == "app-server" and output_dir is not None:
        control_socket = output_dir / "control.sock"
    transport_for_payload = effective_transport
    if output_dir is not None:
        _write_json(
            output_dir / "run.json",
            _payload(
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
                evidence=evidence,
                brief_set=brief_set,
                codex_transport=transport_for_payload,
                route=route,
                control_socket=control_socket,
                control_transport=control_transport,
                code_graph_delta=code_graph_delta,
                worker=worker,
            ),
        )
    appserver = None
    control_registry = None
    control_server = None

    def close_appserver() -> None:
        nonlocal appserver
        server = appserver
        appserver = None
        close = getattr(server, "close", None)
        if callable(close):
            close()

    def close_control_server() -> None:
        nonlocal control_server
        server = control_server
        control_server = None
        close = getattr(server, "close", None)
        if callable(close):
            close()

    def close_server_resources() -> None:
        try:
            close_control_server()
        finally:
            close_appserver()

    try:
        if effective_transport == "app-server":
            try:
                # BRIGADE_RUN_ID is run-scoped process identity, not per-seat env.
                # AppServer already accepts process env; seat.env stays forbidden
                # on the shared app-server session (roster + dispatch force direct).
                appserver_kwargs: dict[str, Any] = {"cwd": cwd}
                if output_dir is not None:
                    appserver_params = inspect.signature(codex_appserver.AppServer).parameters.values()
                    accepts_env = any(
                        parameter.name == "env" or parameter.kind is inspect.Parameter.VAR_KEYWORD
                        for parameter in appserver_params
                    )
                    if accepts_env:
                        appserver_kwargs["env"] = {receipt_schema.BRIGADE_RUN_ID_ENV: output_dir.name}
                appserver = _call_with_process_registry(
                    codex_appserver.AppServer,
                    process_registry=process_registry,
                    **appserver_kwargs,
                )
                appserver.start()
            except codex_appserver.AppServerError as exc:
                close_appserver()
                transport_routing_payload = seat_health_policy.transport_fallback_decision(
                    requested_transport="app-server",
                    effective_transport="exec",
                    cause="transport-unavailable",
                    detail=str(exc),
                )
                if output_dir is not None:
                    try:
                        _write_json(output_dir / "transport-routing.json", dict(transport_routing_payload))
                    except OSError as write_exc:
                        print(f"error: transport routing receipt failed: {write_exc}", file=sys.stderr)
                print(
                    f"warning: codex app-server unavailable ({exc}); falling back to exec [transport-unavailable]",
                    file=sys.stderr,
                )
                effective_transport = "exec"
                control_socket = None
            if appserver is not None and output_dir is not None:
                control_registry = run_control.LiveTurnRegistry()
                control_server = run_control.ControlServer(control_socket, control_registry)
                try:
                    control_transport = control_server.start()
                except run_control.ControlError as exc:
                    close_control_server()
                    print(f"warning: run control unavailable ({exc})", file=sys.stderr)
                    control_registry = None
                    control_transport = None
                    control_socket = None
        transport_for_payload = effective_transport
        if output_dir is not None:
            _write_json(
                output_dir / "run.json",
                _payload(
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
                    evidence=evidence,
                    brief_set=brief_set,
                    codex_transport=transport_for_payload,
                    route=route,
                    control_socket=control_socket,
                    control_transport=control_transport,
                    code_graph_delta=code_graph_delta,
                    worker=worker,
                ),
            )

        active_stage = min(assignment.stage for assignment in assignments) if assignments else 1
        active_seats = tuple(assignment.worker for assignment in assignments if assignment.stage == active_stage)
    except BaseException:
        close_server_resources()
        raise

    budget_coordinator: run_budget.BudgetCoordinator | None = None

    def _budget_cancel_fn() -> run_budget.CancellationReport:
        """Record observed per-seat interruption state without raw transport data."""
        return _observed_budget_cancellation(process_registry, control_registry, active_seats)

    def _append_budget_event(event_type: str, payload: Mapping[str, Any], idempotency_key: str) -> Any:
        assert output_dir is not None
        return run_lifecycle.record_lifecycle_event(
            output_dir,
            event_type=event_type,
            payload=dict(payload),
            idempotency_key=idempotency_key,
            workspace=lock_workspace,
        )

    if output_dir is not None and lock_workspace is not None:
        try:
            budget_coordinator = _build_budget_coordinator(
                output_dir,
                started_at=started_at,
                append_event=_append_budget_event,
            )
        except run_budget.BudgetCompatibilityError as exc:
            # Unknown schema/dimension must stop recovery/dispatch with a bounded diagnostic.
            active_seat = active_seats[0] if len(active_seats) == 1 else None
            record_run_termination(
                output_dir,
                status="failed",
                failure_phase="dispatch",
                failure_kind="unexpected-error",
                detail=f"run budget compatibility error: {exc.diagnostic}",
                seat=active_seat,
                active_seats=active_seats,
            )
            close_server_resources()
            return 1

    def stage_started(stage: int, seats: tuple[str, ...]) -> None:
        nonlocal active_stage, active_seats
        active_stage = stage
        active_seats = seats
        if output_dir is not None:
            record_dispatch_stage(output_dir, stage=stage, seats=seats)

    def dispatch_interrupted() -> None:
        if output_dir is None:
            return
        active_seat = active_seats[0] if len(active_seats) == 1 else None
        # Live operator cancellation is a policy terminal (#593), not a worker
        # FailureClass and not infrastructure-neutral under #580.
        if budget_coordinator is not None:
            try:
                budget_coordinator.request_cancel(
                    request_id="opcancel:live-ctrl-c",
                    reason_class="operator_cancel",
                    transport_capability="mixed",
                    dimension="wall_clock_seconds",
                    cancel_fn=_budget_cancel_fn,
                )
            except (run_budget.BudgetError, run_lifecycle.LifecycleJournalError, OSError) as exc:
                print(f"warning: budget cancel receipt failed: {exc}", file=sys.stderr)
        record_run_termination(
            output_dir,
            status="canceled",
            failure_phase="dispatch",
            failure_kind=run_budget.POLICY_KIND_OPERATOR_CANCELLED,
            detail="run canceled by operator",
            seat=active_seat,
            active_seats=active_seats,
        )

    def dispatch_fact(event_type: str, agent: Agent, attempt: int | None = None) -> int | None:
        if output_dir is None:
            return None
        try:
            event = run_lifecycle.record_dispatch_fact(
                output_dir,
                workspace=lock_workspace,
                event_type=event_type,
                seat=agent.name,
                attempt=attempt,
            )
        except (OSError, run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError) as exc:
            raise runguard.RetainRunLockError(f"failed to record dispatch lifecycle fact: {exc}") from exc
        if event is None:
            return None
        recorded_attempt = event.payload.get("attempt")
        return recorded_attempt if isinstance(recorded_attempt, int) else None

    def dispatch_requested(agent: Agent) -> int | None:
        # Enforce wall-clock / worker-dispatch ceilings before new work starts (#593).
        # Attempt allocation and reservation share one lock; retries reclaim an
        # open pending identity while concurrent same-seat callers mint anew.
        if budget_coordinator is None or output_dir is None:
            return dispatch_fact("run.dispatch.requested", agent)
        try:
            return budget_coordinator.reserve_and_record_dispatch(
                seat=agent.name,
                allocate_attempt=lambda: run_lifecycle.allocate_next_dispatch_attempt(output_dir, agent.name),
                record_requested=lambda attempt: dispatch_fact("run.dispatch.requested", agent, attempt),
            )
        except run_budget.BudgetPolicyError as exc:
            if exc.exhausted:
                try:
                    budget_coordinator.request_cancel(
                        request_id=f"budgetcancel:{exc.dimension}",
                        reason_class="budget_cancel",
                        transport_capability="mixed",
                        dimension=exc.dimension,
                        cancel_fn=_budget_cancel_fn,
                    )
                except (run_budget.BudgetError, run_lifecycle.LifecycleJournalError, OSError) as cancel_exc:
                    print(f"warning: budget cancel receipt failed: {cancel_exc}", file=sys.stderr)
            raise
        except (OSError, run_lifecycle.LifecycleJournalError, run_checkpoint.CheckpointError) as exc:
            # Mirror dispatch_fact fail-closed translation so enrollment gaps
            # before the durable requested write retain the run lock.
            raise runguard.RetainRunLockError(f"failed to record dispatch lifecycle fact: {exc}") from exc

    def dispatch_observed(agent: Agent, attempt: int) -> None:
        dispatch_fact("run.dispatch.observed", agent, attempt)

    def dispatch_completed(agent: Agent, attempt: int) -> None:
        dispatch_fact("run.dispatch.completed", agent, attempt)

    def dispatch_failed(agent: Agent, attempt: int) -> None:
        dispatch_fact("run.dispatch.failed", agent, attempt)

    def reprobe_seat_for_retry(agent: Agent) -> bool:
        active_health_probe.invalidate(seat=agent.name)
        try:
            result = active_health_probe.probe(
                agent,
                roster,
                workspace=cwd,
                allow_model_smoke=False,
                require_hard_isolation=read_only,
            )
        except Exception:
            return False
        if output_dir is not None:
            try:
                seat_health.write_seat_health_receipt(
                    output_dir / "seat-health.json",
                    (result,),
                    run_id=output_dir.name,
                )
            except OSError as exc:
                print(f"error: seat health receipt failed: {exc}", file=sys.stderr)
        return result.status == "healthy"

    def persist_failed_attempt(result: WorkerResult) -> None:
        if output_dir is None:
            return
        try:
            write_sidecar_revision(
                output_dir,
                "worker-results.json",
                receipt_schema.worker_results_document(_worker_payload([result])),
            )
        except OSError as exc:
            print(f"error: worker attempt receipt failed: {exc}", file=sys.stderr)

    active_seat = active_seats[0] if len(active_seats) == 1 else None
    worker_prompt_builder = partial(
        _worker_prompt,
        skill_policy=skill_policy,
        run_id=output_dir.name if output_dir is not None else message_envelope.IN_MEMORY_RUN_ID,
        to_seat=roster.orchestrator,
    )
    try:
        try:
            worker_results = _call_with_process_registry(
                dispatch,
                assignments,
                roster,
                cwd=cwd,
                read_only=read_only,
                sandbox_read_only=sandbox_read_only,
                sandbox=sandbox,
                direct=direct_worker,
                code_graph=code_graph,
                drift_impact=drift_impact,
                evidence=evidence,
                appserver=appserver,
                control_registry=control_registry,
                events_dir=(output_dir / "events") if (output_dir is not None and appserver is not None) else None,
                verbose=verbose,
                authorized_writable_worktree=authorized_writable_worktree,
                fail_fast=fail_fast,
                scheduler=scheduler,
                route_dependencies=dict(route.dependencies) if route is not None and route.attached else None,
                route_held=dict(route.held) if route is not None and route.attached else None,
                on_stage_start=stage_started,
                on_interrupt=dispatch_interrupted,
                on_scheduler_resolved=scheduler_resolved,
                on_dispatch_requested=dispatch_requested,
                on_dispatch_observed=dispatch_observed,
                on_dispatch_completed=dispatch_completed,
                on_dispatch_failed=dispatch_failed,
                process_registry=process_registry,
                build_prompt=worker_prompt_builder,
                quarantine_state=quarantine_state,
                reprobe_seat=reprobe_seat_for_retry,
                on_failed_attempt_persisted=persist_failed_attempt,
                run_id=output_dir.name if output_dir is not None else None,
                output_dir=output_dir,
            )
        except runguard.RetainRunLockError:
            raise
        except run_budget.BudgetPolicyError as exc:
            active_seat = active_seats[0] if len(active_seats) == 1 else None
            status, kind = run_budget.terminal_status_for_policy("budget_exhausted")
            # Reservation denial without full exhaustion still terminalizes the
            # run: new work cannot start under the declared ceiling.
            if output_dir is not None:
                record_run_termination(
                    output_dir,
                    status=status,
                    failure_phase="dispatch",
                    failure_kind=kind,
                    detail=_one_line(str(exc)) or "run budget reservation denied",
                    seat=active_seat,
                    active_seats=active_seats,
                )
            return 1
        except KeyboardInterrupt:
            active_seat = active_seats[0] if len(active_seats) == 1 else None
            if output_dir is not None:
                record_run_termination(
                    output_dir,
                    status="canceled",
                    failure_phase="dispatch",
                    failure_kind=run_budget.POLICY_KIND_OPERATOR_CANCELLED,
                    detail="run canceled by operator",
                    seat=active_seat,
                    active_seats=active_seats,
                )
            raise
        except TimeoutError as exc:
            active_seat = active_seats[0] if len(active_seats) == 1 else None
            detail = _one_line(str(exc)) or "worker dispatch timed out"
            if output_dir is not None:
                record_run_termination(
                    output_dir,
                    status="timeout",
                    failure_phase="dispatch",
                    failure_kind="timeout",
                    detail=detail,
                    seat=active_seat,
                    active_seats=active_seats,
                )
            raise
        except Exception as exc:
            active_seat = active_seats[0] if len(active_seats) == 1 else None
            detail = f"{type(exc).__name__}: {_one_line(str(exc)) or 'unexpected dispatch failure'}"
            if output_dir is not None:
                record_run_termination(
                    output_dir,
                    status="failed",
                    failure_phase="dispatch",
                    failure_kind="unexpected-error",
                    detail=detail,
                    seat=active_seat,
                    active_seats=active_seats,
                )
            raise
    finally:
        close_server_resources()
    if (
        output_dir is not None
        and lock_workspace is not None
        and runguard.is_active_run_owner(lock_workspace, output_dir)
    ):
        from . import runs_cmd

        approval_reference = runs_cmd._approval_pause_reference_for_owned_run(
            lock_workspace,
            output_dir,
        )
        if approval_reference is not None:
            write_approval_resume_handoff(
                output_dir,
                worker_results,
                requester_worker=approval_reference.requester_worker,
                requester_thread_id=approval_reference.requester_thread_id,
            )
            record_approval_pause(output_dir, approval_reference)
            return 0
    # Drift checkpoint: after worker dispatch. A concurrent commit or branch
    # switch during dispatch must fail the run before ground truth attributes
    # the foreign state to the worker.
    drift_rc = _drift_failure_rc()
    if drift_rc is not None:
        return drift_rc
    if output_dir is not None:
        record_result_processing(output_dir)
    if output_dir is not None and code_graph_delta_before is not None and cwd is not None:
        code_graph_delta = graphtrail_delta.capture_after_and_diff(cwd, output_dir, code_graph_delta_before)
    context_eval_payload = _context_eval_for_run(code_graph, code_graph_delta)
    ground_truth = build_ground_truth(cwd, started_at, pre_run_snapshot=pre_run_snapshot)
    if code_graph_delta is not None:
        ground_truth["code_graph_delta"] = code_graph_delta
    if context_eval_payload is not None:
        ground_truth["context_eval"] = context_eval_payload
    suspected_noop = _suspected_noop(
        ground_truth=ground_truth,
        worker_results=worker_results,
        dry_run=dry_run,
        read_only=read_only,
        sandbox_read_only=sandbox_read_only,
        sandbox=sandbox,
    )
    ground_truth["suspected_noop"] = suspected_noop
    worker_results = _mark_noop_worker_results(worker_results, suspected_noop)
    if output_dir is not None:
        worker_results = _write_worker_logs(output_dir, worker_results)
        write_sidecar_revision(
            output_dir,
            "worker-results.json",
            receipt_schema.worker_results_document(
                _worker_payload(worker_results),
                ground_truth=ground_truth,
            ),
        )
    isolation_detail = _run_isolation_check()
    if isolation_detail is not None:
        if output_dir is not None:
            record_run_termination(
                output_dir,
                status="failed",
                failure_phase="run-isolation",
                failure_kind="isolation-breach",
                detail=isolation_detail,
                seat=roster.orchestrator,
            )
        print(f"error: {isolation_detail}", file=sys.stderr)
        return 2
    if verbose:
        _print_worker_status(worker_results)
        if not direct_worker:
            print("synthesis:")
            print(f"  -> {roster.orchestrator}")

    synth_captured = None
    if direct_worker:
        direct_result = (
            worker_results[0]
            if worker_results
            else WorkerResult(
                worker=worker or "",
                task=task,
                text="",
                ok=False,
                detail="direct worker produced no result",
            )
        )
        final = _agent_result_from_worker(direct_result)
    else:
        if output_dir is not None:
            _write_json(
                output_dir / "run.json",
                _payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="synthesizing",
                    started_at=started_at,
                    output_dir=output_dir,
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    evidence=evidence,
                    brief_set=brief_set,
                    codex_transport=transport_for_payload,
                    route=route,
                    control_socket=control_socket,
                    control_transport=control_transport,
                    code_graph_delta=code_graph_delta,
                    worker=worker,
                ),
            )
        synthesis_started = time.monotonic()
        synth_prompt = build_synth_prompt(
            task,
            worker_results,
            read_only=read_only,
            ground_truth=ground_truth,
            code_graph=code_graph,
            drift_impact=drift_impact,
            evidence=evidence,
            run_id=output_dir.name if output_dir is not None else message_envelope.IN_MEMORY_RUN_ID,
            to_seat=roster.orchestrator,
        )
        synth_request = message_envelope.emit(
            synth_prompt,
            kind="synthesis-request",
            producer="aboyeur.build_synth_prompt",
            from_seat=message_envelope.BRIGADE_SEAT,
            to_seat=roster.orchestrator,
            run_dir=output_dir,
            run_id=output_dir.name if output_dir is not None else None,
            assignment_id=message_envelope.default_assignment_id("synthesis-request"),
            session_harness=_orchestrator_harness(roster),
        )
        synth_captured = None
        if not synth_request.delivered:
            final = agents.AgentResult(
                text="",
                ok=False,
                detail=synth_request.reason or "synthesis-request rejected by provenance gate",
                failure_phase="synthesis",
                failure_kind="unclassified",
            )
        else:
            final = _call_with_process_registry(
                _run_orchestrator,
                roster,
                synth_prompt,
                cwd=cwd,
                read_only=read_only,
                sandbox_read_only=sandbox_read_only,
                sandbox=sandbox,
                codex_transport=transport_for_payload,
                process_registry=process_registry,
            )
            synth_captured = message_envelope.emit(
                final.text,
                kind="synthesis-result",
                producer="aboyeur.run",
                from_seat=roster.orchestrator,
                to_seat=message_envelope.BRIGADE_SEAT,
                run_dir=output_dir,
                run_id=output_dir.name if output_dir is not None else None,
                assignment_id=message_envelope.default_assignment_id("synthesis-result"),
                session_harness=_orchestrator_harness(roster),
            )
            if not synth_captured.delivered:
                final = replace(
                    final,
                    text="",
                    ok=False,
                    detail=synth_captured.reason or "synthesis-result rejected by provenance gate",
                    failure_phase=final.failure_phase or "synthesis",
                    failure_kind=final.failure_kind or "unclassified",
                )
        final = replace(final, duration_seconds=max(0.0, round(time.monotonic() - synthesis_started, 3)))
    # Drift checkpoint: after synthesis. A concurrent commit or branch switch
    # during synthesis must fail the run before the synthesized answer is
    # finalized on top of drifted state.
    drift_rc = _drift_failure_rc()
    if drift_rc is not None:
        return drift_rc
    if output_dir is not None:
        if not direct_worker:
            final = _write_agent_logs(output_dir, "synthesis", final)
        synthesis_payload = (
            receipt_schema.synthesis_document(
                mode="direct-worker",
                worker=worker,
                result=_agent_result_payload(final),
                ground_truth=ground_truth,
                run_id=output_dir.name,
                worker_results=_worker_payload(worker_results),
            )
            if direct_worker
            else receipt_schema.synthesis_document(
                orchestrator=roster.orchestrator,
                result=_agent_result_payload(final),
                ground_truth=ground_truth,
                run_id=output_dir.name,
                worker_results=_worker_payload(worker_results),
            )
        )
        if synth_captured is not None:
            synthesis_payload["provenance"] = synth_captured.envelope
        causal_receipt.write_synthesis_lineage_artifacts(
            output_dir,
            synthesis_payload,
            worker_results=_worker_payload(worker_results),
            write=write_sidecar_revision,
        )
    if not final.ok:
        if output_dir is not None:
            finished_at = datetime.now(timezone.utc)
            interrupted = final.status == "interrupted"
            if direct_worker:
                (output_dir / "final.txt").write_text(final.text + "\n")
            _write_json(
                output_dir / "run.json",
                _payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="timeout" if final.timed_out else "canceled" if interrupted else "failed",
                    started_at=started_at,
                    finished_at=finished_at,
                    output_dir=output_dir,
                    error=final.detail,
                    failure_phase=(
                        final.failure_phase
                        or ("inference" if final.timed_out else "dispatch" if direct_worker else "synthesis")
                    ),
                    failure_kind=(
                        "timeout"
                        if final.timed_out
                        else final.failure_kind or ("interrupted" if interrupted else "agent-error")
                    ),
                    failure_seat=worker if direct_worker else roster.orchestrator,
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    evidence=evidence,
                    brief_set=brief_set,
                    codex_transport=transport_for_payload,
                    route=route,
                    code_graph_delta=code_graph_delta,
                    context_eval_payload=context_eval_payload,
                    suspected_noop=suspected_noop,
                    worker=worker,
                    transport_warning=final.transport_warning,
                ),
            )
        if direct_worker:
            print(f"error: worker failed: {final.detail}", file=sys.stderr)
            if final.text:
                print(final.text)
        else:
            print(f"error: orchestrator failed during synthesis: {final.detail}", file=sys.stderr)
        return 2
    # Drift checkpoint: immediately before finalization. The final ok receipt
    # must not be written on top of state a concurrent commit moved out from
    # under the run.
    drift_rc = _drift_failure_rc()
    if drift_rc is not None:
        return drift_rc
    workers_ok = direct_worker or all(result.ok for result in worker_results)
    failed_seats = [result.worker for result in worker_results if not result.ok]
    if output_dir is not None:
        pending_handoff = handoff_inbox is not None
        if not workers_ok:
            final_status = "incomplete"
            finished_at = datetime.now(timezone.utc)
            run_status = "incomplete"
        else:
            final_status = "artifact-collection" if defer_artifact_collection else "ok"
            finished_at = None if defer_artifact_collection or pending_handoff else datetime.now(timezone.utc)
            run_status = "handoff" if pending_handoff else final_status
        (output_dir / "final.txt").write_text(final.text + "\n")
        _write_json(
            output_dir / "run.json",
            _payload(
                task=task,
                cwd=cwd,
                roster=roster,
                dry_run=dry_run,
                read_only=read_only,
                status=run_status,
                started_at=started_at,
                finished_at=finished_at,
                output_dir=output_dir,
                error=(
                    f"{len(failed_seats)} worker(s) failed or were skipped: {', '.join(failed_seats)}"
                    if not workers_ok
                    else None
                ),
                failure_phase="workers" if not workers_ok else None,
                failure_kind="worker-failure" if not workers_ok else None,
                failure_seat=",".join(failed_seats) if not workers_ok else None,
                code_graph=code_graph,
                drift_impact=drift_impact,
                evidence=evidence,
                brief_set=brief_set,
                codex_transport=transport_for_payload,
                route=route,
                control_socket=control_socket,
                control_transport=control_transport,
                code_graph_delta=code_graph_delta,
                context_eval_payload=context_eval_payload,
                suspected_noop=suspected_noop,
                worker=worker,
                transport_warning=direct_result.transport_warning if direct_worker else None,
                worker_failure_summary=(
                    seat_health_policy.worker_failure_summary(worker_results) if not workers_ok else None
                ),
            ),
        )
    if handoff_inbox is not None and workers_ok:
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
                    _payload(
                        task=task,
                        cwd=cwd,
                        roster=roster,
                        dry_run=dry_run,
                        read_only=read_only,
                        status="failed",
                        started_at=started_at,
                        finished_at=finished_at,
                        output_dir=output_dir,
                        error=detail,
                        failure_phase="handoff",
                        failure_kind="handoff-write-error",
                        failure_seat=roster.orchestrator,
                        code_graph=code_graph,
                        drift_impact=drift_impact,
                        evidence=evidence,
                        brief_set=brief_set,
                        codex_transport=transport_for_payload,
                        route=route,
                        control_socket=control_socket,
                        control_transport=control_transport,
                        code_graph_delta=code_graph_delta,
                        context_eval_payload=context_eval_payload,
                        suspected_noop=suspected_noop,
                        worker=worker,
                    ),
                )
            print(f"error: {detail}", file=sys.stderr)
            print(final.text)
            return 2
        print(f"handoff: {handoff}", file=sys.stderr)
        if output_dir is not None:
            finished_at = None if defer_artifact_collection else datetime.now(timezone.utc)
            _write_json(
                output_dir / "run.json",
                _payload(
                    task=task,
                    cwd=cwd,
                    roster=roster,
                    dry_run=dry_run,
                    read_only=read_only,
                    status="artifact-collection" if defer_artifact_collection else "ok",
                    started_at=started_at,
                    finished_at=finished_at,
                    output_dir=output_dir,
                    handoff_path=handoff,
                    code_graph=code_graph,
                    drift_impact=drift_impact,
                    evidence=evidence,
                    brief_set=brief_set,
                    codex_transport=transport_for_payload,
                    route=route,
                    control_socket=control_socket,
                    control_transport=control_transport,
                    code_graph_delta=code_graph_delta,
                    context_eval_payload=context_eval_payload,
                    suspected_noop=suspected_noop,
                    worker=worker,
                ),
            )
    reroute_summary = seat_health_policy.format_reroute_summary(roster.seat_routing)
    if reroute_summary is not None:
        print(reroute_summary, file=sys.stderr)
    if not workers_ok:
        print(
            seat_health_policy.format_incomplete_warning(
                worker_results,
                routing_decisions=roster.seat_routing,
            ),
            file=sys.stderr,
        )
        print(final.text)
        return 3
    print(final.text)
    return 0
