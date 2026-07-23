"""Grounded deliberation mode for ``brigade run``.

v1 schema (``brigade.deliberation.v1``)::

    {
      "schema": "brigade.deliberation.v1",
      "decision": "<original task>",
      "perspectives": [
        {
          "worker": "<seat>",
          "stage": 1,
          "evidence_scope": {
            "kind": "graphtrail-callers",
            "reference": "<symbol or query>",
            "query": "<command query>",
            "grounded": true,
            "status": "valid"
          },
          "position": "...",
          "assumptions": ["..."],
          "evidence_references": ["..."],
          "agreements": ["..."],
          "conflicts": ["..."],
          "raw_output": "..."
        }
      ],
      "challenger": {
        "worker": "<seat>",
        "stage": 2,
        "attacks": ["..."],
        "minority_report": "...",
        "recommendation": "...",
        "confidence": "low|medium|high",
        "unresolved_conflicts": ["..."],
        "agreements": ["..."],
        "raw_output": "..."
      },
      "agreements": ["..."],
      "unresolved_conflicts": ["..."],
      "assumptions": ["..."],
      "evidence_references": ["..."],
      "minority_report": "...",
      "recommendation": "...",
      "confidence": "low|medium|high",
      "invalid_lenses": [
        {"worker": "...", "reason": "duplicate|ungrounded", "evidence_scope": {...}}
      ]
    }

Evidence scope ``status`` values: ``valid``, ``invalid``, ``duplicate``.
Prompt-only role labels without a recorded evidence trace are ``ungrounded`` (``grounded: false``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from json import JSONDecoder
from pathlib import Path
from typing import Any, Callable

from . import evidence_brief as evidence_brief_mod
from . import proc
from .roster import Agent, Roster, workers
from .run_transport import Assignment, WorkerResult

SCHEMA = "brigade.deliberation.v1"
PERSPECTIVE_STAGE = 1
CHALLENGER_STAGE = 2
MIN_PERSPECTIVES = 2
MAX_PERSPECTIVES = 3
SCOPE_HEADING = "## Deliberation evidence scope (read-only)"
_PERSPECTIVE_JSON_HINT = (
    'Return a single JSON object with keys: "position", "assumptions", '
    '"evidence_references", "agreements", "conflicts".'
)
_CHALLENGER_JSON_HINT = (
    'Return a single JSON object with keys: "attacks", "minority_report", '
    '"recommendation", "confidence", "unresolved_conflicts", "agreements".'
)
_CONFIDENCE_VALUES = frozenset({"low", "medium", "high"})


@dataclass(frozen=True)
class EvidenceScope:
    kind: str
    reference: str
    query: str
    text: str = ""
    grounded: bool = False
    status: str = "valid"

    def fingerprint(self) -> tuple[str, str]:
        return (self.kind, self.reference)

    def payload(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "reference": self.reference,
            "query": self.query,
            "grounded": self.grounded,
            "status": self.status,
        }


@dataclass(frozen=True)
class DeliberationLens:
    worker: str
    stage: int
    role: str
    scope: EvidenceScope
    task: str

    @property
    def assignment_key(self) -> tuple[str, str, int]:
        return (self.worker, self.task, self.stage)


@dataclass(frozen=True)
class DeliberationPlan:
    decision: str
    assignments: tuple[Assignment, ...]
    lenses: tuple[DeliberationLens, ...]
    invalid_lenses: tuple[DeliberationLens, ...]

    def lens_for(self, assignment: Assignment) -> DeliberationLens | None:
        for lens in (*self.lenses, *self.invalid_lenses):
            if lens.worker == assignment.worker and lens.stage == assignment.stage and lens.task == assignment.task:
                return lens
        return None


def _graphtrail_bin() -> str | None:
    from . import context_cmd

    return context_cmd._graphtrail_bin()


def _truncate_scope_text(text: str, limit: int = 3500) -> str:
    if len(text) <= limit:
        return text
    note = f"\n\n[Evidence scope truncated to {limit} chars.]\n"
    room = max(0, limit - len(note))
    clipped = text[:room]
    boundary = clipped.rfind("\n")
    if boundary > 0:
        clipped = clipped[:boundary]
    return clipped.rstrip() + note


def _graphtrail_markdown(cwd: Path, db_path: Path, args: list[str]) -> str:
    binary = _graphtrail_bin()
    if binary is None:
        return ""
    result = proc.run(
        [binary, "--db", str(db_path), *args, "--markdown", "--limit", "8"],
        timeout=10.0,
        cwd=cwd,
    )
    if result.code != 0:
        return ""
    body = result.stdout.strip()
    if not body:
        return ""
    return _truncate_scope_text(f"{SCOPE_HEADING}\n\n{body}\n")


def _graphtrail_context_json(cwd: Path, db_path: Path, task: str) -> dict[str, Any] | None:
    binary = _graphtrail_bin()
    if binary is None:
        return None
    result = proc.run(
        [binary, "--db", str(db_path), "context", task, "--json", "--limit", "8"],
        timeout=10.0,
        cwd=cwd,
    )
    if result.code != 0:
        return None
    data = result.json()
    return data if isinstance(data, dict) else None


def _entry_point_symbol(context: dict[str, Any]) -> str | None:
    entry_points = context.get("entry_points")
    if not isinstance(entry_points, list):
        return None
    for item in entry_points:
        if not isinstance(item, dict):
            continue
        name = item.get("qualified_name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    return None


def _miseledger_scope(cwd: Path, query: str) -> EvidenceScope:
    bundle = evidence_brief_mod.fetch_evidence_bundle(cwd, query, limit=3)
    text = evidence_brief_mod.render_evidence_bundle(bundle, limit=3) if bundle else ""
    grounded = bool(text)
    return EvidenceScope(
        kind="miseledger-query",
        reference=query,
        query=query,
        text=text,
        grounded=grounded,
        status="valid" if grounded else "invalid",
    )


def _role_label_scope(agent: Agent) -> EvidenceScope:
    return EvidenceScope(
        kind="role-label",
        reference=agent.role,
        query=agent.role,
        text="",
        grounded=False,
        status="invalid",
    )


def _candidate_graphtrail_scopes(cwd: Path, task: str) -> list[EvidenceScope]:
    db_path = cwd / ".graphtrail" / "graphtrail.db"
    if not db_path.is_file():
        return []
    candidates: list[EvidenceScope] = []
    context_text = _graphtrail_markdown(cwd, db_path, ["context", task])
    if context_text:
        candidates.append(
            EvidenceScope(
                kind="graphtrail-context",
                reference=task,
                query=task,
                text=context_text,
                grounded=True,
                status="valid",
            )
        )
    context_json = _graphtrail_context_json(cwd, db_path, task)
    symbol = _entry_point_symbol(context_json) if context_json else None
    if symbol:
        for kind, command in (
            ("graphtrail-callers", ["callers", symbol]),
            ("graphtrail-callees", ["callees", symbol]),
            ("graphtrail-impact", ["impact", symbol]),
        ):
            text = _graphtrail_markdown(cwd, db_path, command)
            if not text:
                continue
            candidates.append(
                EvidenceScope(
                    kind=kind,
                    reference=symbol,
                    query=" ".join(command),
                    text=text,
                    grounded=True,
                    status="valid",
                )
            )
    return candidates


def _task_query_variants(cwd: Path, task: str) -> list[str]:
    words: list[str] = []
    for raw in re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]*", task.lower()):
        if len(raw) < 3 or raw in evidence_brief_mod._STOPWORDS or raw in words:
            continue
        words.append(raw)
        if len(words) >= 6:
            break
    if not words:
        return [cwd.name]
    variants = [f"{cwd.name} {word}" for word in words[:3]]
    if len(words) >= 2:
        variants.append(f"{cwd.name} {' '.join(words[:2])}")
    return variants


def derive_evidence_scopes(cwd: Path | None, task: str, *, count: int = MAX_PERSPECTIVES) -> list[EvidenceScope]:
    """Derive up to ``count`` distinct evidence scopes for stage-one perspectives."""
    if cwd is None:
        return []
    count = max(MIN_PERSPECTIVES, min(count, MAX_PERSPECTIVES))
    scopes: list[EvidenceScope] = []
    seen: set[tuple[str, str]] = set()

    def add(scope: EvidenceScope) -> None:
        key = scope.fingerprint()
        if key in seen:
            return
        seen.add(key)
        scopes.append(scope)

    for scope in _candidate_graphtrail_scopes(cwd, task):
        add(scope)
        if len(scopes) >= count:
            return scopes[:count]

    for query in _task_query_variants(cwd, task):
        scope = _miseledger_scope(cwd, query)
        if scope.grounded:
            add(scope)
        if len(scopes) >= count:
            return scopes[:count]
    return scopes


def mark_duplicate_scopes(scopes: list[EvidenceScope]) -> list[EvidenceScope]:
    seen: set[tuple[str, str]] = set()
    marked: list[EvidenceScope] = []
    for scope in scopes:
        key = scope.fingerprint()
        if key in seen:
            marked.append(
                EvidenceScope(
                    kind=scope.kind,
                    reference=scope.reference,
                    query=scope.query,
                    text=scope.text,
                    grounded=scope.grounded,
                    status="duplicate",
                )
            )
        else:
            seen.add(key)
            marked.append(scope)
    return marked


def _select_perspective_workers(roster: Roster, count: int) -> list[Agent]:
    available = workers(roster)
    if len(available) < MIN_PERSPECTIVES + 1:
        raise ValueError(
            f"deliberation requires at least {MIN_PERSPECTIVES + 1} non-orchestrator workers; "
            f"roster has {len(available)}"
        )
    return available[: min(count, len(available) - 1)]


def _select_challenger_worker(roster: Roster, perspective_workers: list[Agent]) -> Agent:
    perspective_names = {agent.name for agent in perspective_workers}
    remaining = [agent for agent in workers(roster) if agent.name not in perspective_names]
    if remaining:
        reviewer = next((agent for agent in remaining if "review" in agent.role.lower()), None)
        return reviewer or remaining[0]
    return perspective_workers[-1]


def _perspective_task(decision: str) -> str:
    return (
        "Examine this decision from your assigned evidence scope only. "
        "Do not rely on prompt-only role labels as evidence.\n\n"
        f"Decision:\n{decision}\n\n"
        f"{_PERSPECTIVE_JSON_HINT}"
    )


def _challenger_task(decision: str) -> str:
    return (
        "You are the deliberation challenger. Independent perspectives have already been collected. "
        "Attack the strongest apparent consensus and surface what the majority may be missing.\n\n"
        f"Decision:\n{decision}\n\n"
        f"{_CHALLENGER_JSON_HINT}"
    )


def build_plan(
    roster: Roster,
    decision: str,
    *,
    cwd: Path | None,
    perspective_count: int | None = None,
) -> DeliberationPlan:
    """Build staged deliberation assignments with recorded evidence scopes."""
    count = perspective_count or MAX_PERSPECTIVES
    count = max(MIN_PERSPECTIVES, min(count, MAX_PERSPECTIVES))
    perspective_agents = _select_perspective_workers(roster, count)
    scopes = derive_evidence_scopes(cwd, decision, count=len(perspective_agents))
    challenger = _select_challenger_worker(roster, perspective_agents)

    lenses: list[DeliberationLens] = []
    invalid_lenses: list[DeliberationLens] = []
    assignments: list[Assignment] = []
    used_fingerprints: set[tuple[str, str]] = set()
    task = _perspective_task(decision)

    for index, agent in enumerate(perspective_agents):
        if index < len(scopes):
            scope = scopes[index]
            if scope.fingerprint() in used_fingerprints:
                scope = EvidenceScope(
                    kind=scope.kind,
                    reference=scope.reference,
                    query=scope.query,
                    text=scope.text,
                    grounded=scope.grounded,
                    status="duplicate",
                )
            elif not scope.grounded or scope.status != "valid":
                scope = EvidenceScope(
                    kind=scope.kind,
                    reference=scope.reference,
                    query=scope.query,
                    text=scope.text,
                    grounded=False,
                    status="invalid",
                )
            else:
                used_fingerprints.add(scope.fingerprint())
        else:
            scope = _role_label_scope(agent)

        lens = DeliberationLens(worker=agent.name, stage=PERSPECTIVE_STAGE, role="perspective", scope=scope, task=task)
        if scope.status in {"invalid", "duplicate"} or not scope.grounded:
            invalid_lenses.append(lens)
        else:
            lenses.append(lens)
            assignments.append(Assignment(worker=agent.name, task=task, stage=PERSPECTIVE_STAGE))

    if len(lenses) < MIN_PERSPECTIVES:
        raise ValueError(
            "deliberation could not assemble enough grounded, distinct evidence scopes; "
            "need GraphTrail dependency traces or MiseLedger evidence"
        )

    challenger_task = _challenger_task(decision)
    challenger_lens = DeliberationLens(
        worker=challenger.name,
        stage=CHALLENGER_STAGE,
        role="challenger",
        scope=EvidenceScope(
            kind="deliberation-challenger",
            reference="consensus-attack",
            query=challenger_task,
            text="",
            grounded=True,
            status="valid",
        ),
        task=challenger_task,
    )
    lenses.append(challenger_lens)
    assignments.append(Assignment(worker=challenger.name, task=challenger_task, stage=CHALLENGER_STAGE))
    return DeliberationPlan(
        decision=decision,
        assignments=tuple(assignments),
        lenses=tuple(lenses),
        invalid_lenses=tuple(invalid_lenses),
    )


def _render_prior_perspectives(prior_results: list[WorkerResult]) -> str:
    blocks: list[str] = []
    for result in prior_results:
        blocks.append(
            "\n".join(
                [
                    f"Worker: {result.worker}",
                    f"Status: {'ok' if result.ok else 'failed'}",
                    "Output:",
                    result.text or "(no output)",
                ]
            )
        )
    return "\n\n".join(blocks)


def build_worker_prompt(
    agent: Agent,
    assignment: Assignment,
    *,
    plan: DeliberationPlan,
    prior_results: list[WorkerResult] | None,
    read_only: bool,
    read_only_policy: str = "",
) -> str:
    lens = plan.lens_for(assignment)
    if lens is None:
        raise ValueError(f"missing deliberation lens for {assignment.worker!r} stage {assignment.stage}")
    prior_context = ""
    if prior_results:
        prior_context = f"\n\nIndependent perspectives:\n{_render_prior_perspectives(prior_results)}"
    policy = f"\n\n{read_only_policy}" if read_only and read_only_policy else ""
    scope_block = f"\n\n{lens.scope.text}" if lens.scope.text else ""
    return (
        f"You are Brigade worker {agent.name} in deliberation mode ({lens.role}).\n"
        f"Role:\n{agent.role}\n\n"
        f"Sub-task:\n{assignment.task}"
        f"{scope_block}"
        f"{prior_context}"
        f"{policy}"
    )


def make_prompt_builder(
    plan: DeliberationPlan,
    *,
    read_only_policy: str = "",
) -> Callable[..., str]:
    def build_prompt(
        agent: Agent,
        assignment: Assignment,
        *,
        prior_results: list[WorkerResult] | None = None,
        read_only: bool = False,
        direct: bool = False,
        code_graph: object | None = None,
        drift_impact: object | None = None,
        evidence: object | None = None,
    ) -> str:
        _ = (direct, code_graph, drift_impact, evidence)
        return build_worker_prompt(
            agent,
            assignment,
            plan=plan,
            prior_results=prior_results,
            read_only=read_only,
            read_only_policy=read_only_policy,
        )

    return build_prompt


def _extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError:
            value = None
        else:
            return value if isinstance(value, dict) else None
    decoder = JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _one_line(value: object) -> str:
    return " ".join(str(value or "").split())


def _normalize_confidence(value: object) -> str:
    rendered = _one_line(value).lower()
    if rendered in _CONFIDENCE_VALUES:
        return rendered
    return "medium"


def _parse_perspective_output(text: str) -> dict[str, Any]:
    payload = _extract_json_object(text) or {}
    return {
        "position": _one_line(payload.get("position") or text),
        "assumptions": _string_list(payload.get("assumptions")),
        "evidence_references": _string_list(payload.get("evidence_references")),
        "agreements": _string_list(payload.get("agreements")),
        "conflicts": _string_list(payload.get("conflicts")),
        "raw_output": text,
    }


def _parse_challenger_output(text: str) -> dict[str, Any]:
    payload = _extract_json_object(text) or {}
    return {
        "attacks": _string_list(payload.get("attacks")),
        "minority_report": _one_line(payload.get("minority_report") or text),
        "recommendation": _one_line(payload.get("recommendation")),
        "confidence": _normalize_confidence(payload.get("confidence")),
        "unresolved_conflicts": _string_list(payload.get("unresolved_conflicts")),
        "agreements": _string_list(payload.get("agreements")),
        "raw_output": text,
    }


def assemble_artifact(
    plan: DeliberationPlan,
    worker_results: list[WorkerResult],
) -> dict[str, object]:
    """Build the deliberation.json payload from dispatch results."""
    by_worker_stage = {(result.worker, result.task): result for result in worker_results}
    perspectives: list[dict[str, object]] = []
    challenger_payload: dict[str, object] | None = None
    invalid_entries: list[dict[str, object]] = []

    for lens in plan.invalid_lenses:
        invalid_entries.append(
            {
                "worker": lens.worker,
                "reason": lens.scope.status if lens.scope.status != "valid" else "ungrounded",
                "evidence_scope": lens.scope.payload(),
            }
        )

    for lens in plan.lenses:
        if lens.role != "perspective":
            continue
        result = by_worker_stage.get((lens.worker, lens.task))
        parsed = _parse_perspective_output(result.text if result is not None else "")
        perspectives.append(
            {
                "worker": lens.worker,
                "stage": lens.stage,
                "evidence_scope": lens.scope.payload(),
                **parsed,
            }
        )

    challenger_lens = next((lens for lens in plan.lenses if lens.role == "challenger"), None)
    if challenger_lens is not None:
        result = by_worker_stage.get((challenger_lens.worker, challenger_lens.task))
        parsed = _parse_challenger_output(result.text if result is not None else "")
        challenger_payload = {
            "worker": challenger_lens.worker,
            "stage": challenger_lens.stage,
            **parsed,
        }

    agreements: list[str] = []
    conflicts: list[str] = []
    assumptions: list[str] = []
    evidence_refs: list[str] = []
    for item in perspectives:
        agreements.extend(_string_list(item.get("agreements")))
        conflicts.extend(_string_list(item.get("conflicts")))
        assumptions.extend(_string_list(item.get("assumptions")))
        evidence_refs.extend(_string_list(item.get("evidence_references")))

    minority_report = ""
    recommendation = ""
    confidence = "medium"
    if challenger_payload is not None:
        agreements.extend(_string_list(challenger_payload.get("agreements")))
        conflicts.extend(_string_list(challenger_payload.get("unresolved_conflicts")))
        minority_report = _one_line(challenger_payload.get("minority_report"))
        recommendation = _one_line(challenger_payload.get("recommendation"))
        confidence = _normalize_confidence(challenger_payload.get("confidence"))

    def dedupe(items: list[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for item in items:
            if item in seen:
                continue
            seen.add(item)
            ordered.append(item)
        return ordered

    payload: dict[str, object] = {
        "schema": SCHEMA,
        "decision": plan.decision,
        "perspectives": perspectives,
        "agreements": dedupe(agreements),
        "unresolved_conflicts": dedupe(conflicts),
        "assumptions": dedupe(assumptions),
        "evidence_references": dedupe(evidence_refs),
        "minority_report": minority_report,
        "recommendation": recommendation,
        "confidence": confidence,
        "invalid_lenses": invalid_entries,
    }
    if challenger_payload is not None:
        payload["challenger"] = challenger_payload
    validate_schema(payload)
    return payload


def validate_schema(payload: dict[str, object]) -> None:
    """Validate a deliberation artifact against the v1 schema."""
    if payload.get("schema") != SCHEMA:
        raise ValueError(f"schema must be {SCHEMA!r}")
    if not isinstance(payload.get("decision"), str) or not str(payload["decision"]).strip():
        raise ValueError("decision must be a non-empty string")
    perspectives = payload.get("perspectives")
    if not isinstance(perspectives, list) or not perspectives:
        raise ValueError("perspectives must be a non-empty list")
    for item in perspectives:
        if not isinstance(item, dict):
            raise ValueError("each perspective must be an object")
        scope = item.get("evidence_scope")
        if not isinstance(scope, dict):
            raise ValueError("each perspective must include evidence_scope")
        for key in ("kind", "reference", "query", "grounded", "status"):
            if key not in scope:
                raise ValueError(f"evidence_scope missing {key!r}")
        if "position" not in item:
            raise ValueError("each perspective must include position")
    challenger = payload.get("challenger")
    if not isinstance(challenger, dict):
        raise ValueError("challenger must be an object")
    for key in ("worker", "stage", "minority_report", "recommendation", "confidence"):
        if key not in challenger:
            raise ValueError(f"challenger missing {key!r}")
    if payload.get("confidence") not in _CONFIDENCE_VALUES:
        raise ValueError("confidence must be low, medium, or high")
    if "minority_report" not in payload:
        raise ValueError("minority_report is required")
    if "recommendation" not in payload:
        raise ValueError("recommendation is required")
    invalid_lenses = payload.get("invalid_lenses")
    if not isinstance(invalid_lenses, list):
        raise ValueError("invalid_lenses must be a list")


def plan_payload(plan: DeliberationPlan) -> dict[str, object]:
    from .run_receipts import assignment_payload

    return {
        "schema": "brigade.run_plan.v1",
        "mode": "deliberation",
        "assignments": assignment_payload(list(plan.assignments)),
        "evidence_scopes": [
            {
                "worker": lens.worker,
                "stage": lens.stage,
                "role": lens.role,
                **lens.scope.payload(),
            }
            for lens in (*plan.lenses, *plan.invalid_lenses)
            if lens.role == "perspective"
        ],
        "invalid_lenses": [
            {
                "worker": lens.worker,
                "reason": lens.scope.status if lens.scope.status != "valid" else "ungrounded",
                **lens.scope.payload(),
            }
            for lens in plan.invalid_lenses
        ],
    }


def synthesis_context(artifact: dict[str, object]) -> str:
    """Render deliberation results for the orchestrator synthesis prompt."""
    lines = [
        "## Deliberation artifact (grounded perspectives + challenger)",
        f"Recommendation: {artifact.get('recommendation', '')}",
        f"Confidence: {artifact.get('confidence', 'medium')}",
        f"Minority report: {artifact.get('minority_report', '')}",
    ]
    unresolved = artifact.get("unresolved_conflicts")
    if isinstance(unresolved, list) and unresolved:
        lines.append("Unresolved conflicts:")
        lines.extend(f"- {item}" for item in unresolved if isinstance(item, str))
    perspectives = artifact.get("perspectives")
    if isinstance(perspectives, list):
        lines.append("Perspectives:")
        for item in perspectives:
            if not isinstance(item, dict):
                continue
            scope = item.get("evidence_scope")
            scope_kind = scope.get("kind") if isinstance(scope, dict) else "unknown"
            lines.append(f"- {item.get('worker', 'unknown')} ({scope_kind}): {item.get('position', '')}")
    challenger = artifact.get("challenger")
    if isinstance(challenger, dict):
        lines.append(f"Challenger ({challenger.get('worker', 'unknown')}): {challenger.get('minority_report', '')}")
    return "\n".join(lines)
