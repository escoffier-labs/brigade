"""CandidateSetGate: least-privilege tool sets per plan step (#504).

Before a worker seat runs, filter ``.brigade/tools.toml`` by the step's declared
domain, capability, and max risk class. Record the admissible set and scores in
``candidate-set.json``. An empty admissible set under active requirements is a
typed failure for bounded replanning — the model must not improvise tools.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence, TypedDict

from . import localio
from .run_transport import Assignment
from .tools_cmd import constants as tools_constants
from .tools_cmd.config import _load_config

CANDIDATE_SET_SCHEMA = "brigade.candidate-set.v1"
CANDIDATE_SET_SCHEMA_VERSION = 1
FAILURE_KIND = "no-admissible-tool"
FAILURE_PHASE = "planning"
GATE_VERSION = "candidate-set-gate.v1"

RISK_CLASSES = tools_constants.RISK_CLASSES
_RISK_RANK = {name: index for index, name in enumerate(RISK_CLASSES)}


class ToolScoreArtifact(TypedDict):
    tool_id: str
    score: float
    domain: str | None
    capability: list[str]
    risk_class: str | None
    reasons: list[str]


class StepCandidateArtifact(TypedDict, total=False):
    stage: int
    worker: str
    task: str
    requirements: dict[str, Any]
    enforcement: bool
    admissible: list[ToolScoreArtifact]
    rejected: list[ToolScoreArtifact]
    empty: bool


class CandidateSetArtifact(TypedDict, total=False):
    schema: str
    schema_version: int
    gate_version: str
    run_id: str
    decided_at: str
    catalog_errors: list[str]
    tool_count: int
    steps: list[StepCandidateArtifact]
    empty_required_steps: list[dict[str, Any]]


@dataclass(frozen=True)
class ToolRequirements:
    domain: str | None = None
    capabilities: tuple[str, ...] = ()
    max_risk_class: str | None = None

    @property
    def active(self) -> bool:
        return bool(self.domain or self.capabilities or self.max_risk_class)

    def payload(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.domain:
            out["domain"] = self.domain
        if self.capabilities:
            out["capabilities"] = list(self.capabilities)
        if self.max_risk_class:
            out["max_risk_class"] = self.max_risk_class
        return out


@dataclass(frozen=True)
class ToolScore:
    tool_id: str
    score: float
    admitted: bool
    domain: str | None
    capability: tuple[str, ...]
    risk_class: str | None
    reasons: tuple[str, ...]

    def payload(self) -> ToolScoreArtifact:
        return {
            "tool_id": self.tool_id,
            "score": self.score,
            "domain": self.domain,
            "capability": list(self.capability),
            "risk_class": self.risk_class,
            "reasons": list(self.reasons),
        }


@dataclass
class StepCandidateDecision:
    stage: int
    worker: str
    task: str
    requirements: ToolRequirements
    admissible: list[ToolScore] = field(default_factory=list)
    rejected: list[ToolScore] = field(default_factory=list)

    @property
    def enforcement(self) -> bool:
        return self.requirements.active

    @property
    def empty(self) -> bool:
        return not self.admissible

    @property
    def empty_required(self) -> bool:
        return self.enforcement and self.empty

    def payload(self) -> StepCandidateArtifact:
        return {
            "stage": self.stage,
            "worker": self.worker,
            "task": self.task,
            "requirements": self.requirements.payload(),
            "enforcement": self.enforcement,
            "admissible": [score.payload() for score in self.admissible],
            "rejected": [score.payload() for score in self.rejected],
            "empty": self.empty,
        }


@dataclass
class CandidateSetDecision:
    run_id: str
    decided_at: datetime
    steps: list[StepCandidateDecision] = field(default_factory=list)
    catalog_errors: list[str] = field(default_factory=list)
    tool_count: int = 0

    @property
    def empty_required_steps(self) -> list[StepCandidateDecision]:
        return [step for step in self.steps if step.empty_required]

    @property
    def has_empty_required_steps(self) -> bool:
        return bool(self.empty_required_steps)

    def admissible_tool_ids(self, assignment: Assignment) -> tuple[str, ...]:
        for step in self.steps:
            if step.stage == assignment.stage and step.worker == assignment.worker and step.task == assignment.task:
                return tuple(score.tool_id for score in step.admissible)
        return ()

    def payload(self) -> CandidateSetArtifact:
        empty_required = [
            {
                "stage": step.stage,
                "worker": step.worker,
                "task": step.task,
                "requirements": step.requirements.payload(),
            }
            for step in self.empty_required_steps
        ]
        payload: CandidateSetArtifact = {
            "schema": CANDIDATE_SET_SCHEMA,
            "schema_version": CANDIDATE_SET_SCHEMA_VERSION,
            "gate_version": GATE_VERSION,
            "run_id": self.run_id,
            "decided_at": _utc_iso(self.decided_at),
            "tool_count": self.tool_count,
            "steps": [step.payload() for step in self.steps],
        }
        if self.catalog_errors:
            payload["catalog_errors"] = list(self.catalog_errors)
        if empty_required:
            payload["empty_required_steps"] = empty_required
        return payload


def requirements_from_assignment(assignment: Assignment) -> ToolRequirements:
    return ToolRequirements(
        domain=assignment.domain,
        capabilities=tuple(assignment.capabilities),
        max_risk_class=assignment.max_risk_class,
    )


def risk_rank(value: str | None) -> int | None:
    if value is None:
        return None
    return _RISK_RANK.get(value)


def score_tool(tool: dict[str, Any], requirements: ToolRequirements) -> ToolScore:
    tool_id = str(tool.get("id") or "")
    domain = tool.get("domain") if isinstance(tool.get("domain"), str) else None
    raw_caps = tool.get("capability") or []
    capability = tuple(str(item) for item in raw_caps if isinstance(item, str) and item.strip())
    risk_class = tool.get("risk_class") if isinstance(tool.get("risk_class"), str) else None
    reasons: list[str] = []
    score = 0.0
    admitted = True

    if tool.get("enabled") is False:
        return ToolScore(
            tool_id=tool_id,
            score=0.0,
            admitted=False,
            domain=domain,
            capability=capability,
            risk_class=risk_class,
            reasons=("disabled",),
        )

    if not requirements.active:
        # Observation mode: every enabled tool is admissible; prefer labeled tools.
        if domain:
            score += 0.25
        if capability:
            score += 0.25
        if risk_class:
            score += 0.25
        score += 0.25
        reasons.append("no-step-requirements")
        return ToolScore(
            tool_id=tool_id,
            score=round(score, 4),
            admitted=True,
            domain=domain,
            capability=capability,
            risk_class=risk_class,
            reasons=tuple(reasons),
        )

    if requirements.domain:
        if domain is None:
            admitted = False
            reasons.append("missing-domain")
        elif domain != requirements.domain:
            admitted = False
            reasons.append(f"domain-mismatch:{domain}")
        else:
            score += 1.0
            reasons.append("domain-match")

    if requirements.capabilities:
        required = set(requirements.capabilities)
        have = set(capability)
        missing = sorted(required - have)
        if missing:
            admitted = False
            reasons.append(f"missing-capability:{','.join(missing)}")
        else:
            score += 1.0
            reasons.append("capability-match")

    if requirements.max_risk_class:
        max_rank = risk_rank(requirements.max_risk_class)
        tool_rank = risk_rank(risk_class)
        if max_rank is None:
            admitted = False
            reasons.append(f"invalid-max-risk-class:{requirements.max_risk_class}")
        elif tool_rank is None:
            admitted = False
            reasons.append("missing-risk-class")
        elif tool_rank > max_rank:
            admitted = False
            reasons.append(f"risk-too-high:{risk_class}")
        else:
            # Prefer lower risk within the ceiling.
            proximity = 1.0 - (tool_rank / max(1, len(RISK_CLASSES) - 1))
            score += proximity
            reasons.append("risk-within-ceiling")

    return ToolScore(
        tool_id=tool_id,
        score=round(score, 4),
        admitted=admitted,
        domain=domain,
        capability=capability,
        risk_class=risk_class,
        reasons=tuple(reasons),
    )


def filter_candidate_set(
    tools: Sequence[dict[str, Any]],
    requirements: ToolRequirements,
) -> tuple[list[ToolScore], list[ToolScore]]:
    admissible: list[ToolScore] = []
    rejected: list[ToolScore] = []
    for tool in tools:
        scored = score_tool(tool, requirements)
        if scored.admitted:
            admissible.append(scored)
        else:
            rejected.append(scored)
    admissible.sort(key=lambda item: (-item.score, item.tool_id))
    rejected.sort(key=lambda item: (item.tool_id,))
    return admissible, rejected


def evaluate_assignments(
    target: Path,
    assignments: Sequence[Assignment],
    *,
    run_id: str,
    now: datetime | None = None,
) -> CandidateSetDecision:
    tools, errors = _load_config(target)
    decided_at = now or datetime.now(timezone.utc)
    decision = CandidateSetDecision(
        run_id=run_id,
        decided_at=decided_at,
        catalog_errors=list(errors),
        tool_count=len(tools),
    )
    for assignment in assignments:
        requirements = requirements_from_assignment(assignment)
        admissible, rejected = filter_candidate_set(tools, requirements)
        decision.steps.append(
            StepCandidateDecision(
                stage=assignment.stage,
                worker=assignment.worker,
                task=assignment.task,
                requirements=requirements,
                admissible=admissible,
                rejected=rejected,
            )
        )
    return decision


def apply_admissible_tools(
    assignments: Sequence[Assignment],
    decision: CandidateSetDecision,
) -> list[Assignment]:
    updated: list[Assignment] = []
    for assignment in assignments:
        tool_ids = decision.admissible_tool_ids(assignment)
        updated.append(replace(assignment, admissible_tool_ids=tool_ids))
    return updated


def empty_required_failure_detail(decision: CandidateSetDecision) -> str:
    parts: list[str] = []
    for step in decision.empty_required_steps:
        req = step.requirements.payload()
        req_bits = ", ".join(f"{key}={value!r}" for key, value in req.items())
        parts.append(f"{step.worker}@stage{step.stage} ({req_bits or 'requirements'})")
    joined = "; ".join(parts) if parts else "plan step"
    return f"no admissible tool for {joined}"


def write_candidate_set_receipt(output_dir: Path, decision: CandidateSetDecision) -> Path:
    path = output_dir / "candidate-set.json"
    payload = decision.payload()
    ordered: dict[str, object] = {}
    for key in (
        "schema",
        "schema_version",
        "gate_version",
        "run_id",
        "decided_at",
        "tool_count",
        "catalog_errors",
        "steps",
        "empty_required_steps",
    ):
        if key in payload:
            ordered[key] = payload[key]
    localio.write_text_atomic(path, json.dumps(ordered, indent=2) + "\n")
    return path


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
