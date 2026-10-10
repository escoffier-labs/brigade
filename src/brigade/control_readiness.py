"""Claim readiness observation contract for `brigade evidence controls`.

Internal module.  An assessment records what an evaluator actually observed for
one evidence claim: how deep processing went (``validation_level``), a stable
``outcome`` with bounded reason codes, six independent dimensions and bounded
population counts.  The legacy four-value ``state`` is a conservative
projection of the outcome.

Readiness is an evidence index.  It is not a score, an enforcement gate, an
audit conclusion or a certification claim.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

READINESS_CONTRACT = "brigade.claim_readiness.v1"

# Depth of processing actually performed.  Observations, not a trust ladder.
LEVELS = ("none", "discovered", "structure_observed", "claim_validated")
_LEVEL_RANK = {level: rank for rank, level in enumerate(LEVELS)}

OUTCOMES = frozenset(
    {
        "absent",
        "discovered_only",
        "structure_observed",
        "validated",
        "failed",
        "rejected",
        "invalid",
        "unavailable",
        "incomplete",
        "not_applicable",
    }
)
# Most severe first.  Claim aggregation picks the most severe in-scope outcome.
# Positive outcomes rank weakest-depth first, so one validated artifact never
# lifts a structure-only or discovered-only sibling to ``validated``.
OUTCOME_SEVERITY = (
    "rejected",
    "invalid",
    "failed",
    "unavailable",
    "incomplete",
    "absent",
    "discovered_only",
    "structure_observed",
    "validated",
    "not_applicable",
)
_SEVERITY_RANK = {outcome: rank for rank, outcome in enumerate(OUTCOME_SEVERITY)}

DIMENSIONS = ("integrity", "signature", "authorization", "subject", "freshness", "population")
DIMENSION_STATUSES = frozenset({"passed", "failed", "unknown", "unavailable", "not_checked", "not_applicable"})
_DIMENSION_SEVERITY = ("failed", "unavailable", "unknown", "not_checked", "passed", "not_applicable")
_DIMENSION_RANK = {status: rank for rank, status in enumerate(_DIMENSION_SEVERITY)}

ARTIFACT_SCOPES = frozenset({"in_scope", "wrong_run", "unbound", "out_of_period"})

# Fixed, bounded reason vocabulary.  Reasons never carry artifact content or
# absolute host paths.
REASON_CODES = frozenset(
    {
        "approval_denied",
        "approval_expired",
        "approval_held",
        "approval_invalid",
        "approval_stale",
        "artifact_not_run_bound",
        "command_not_terminal",
        "discovery_unreadable",
        "dsse_signatures_missing",
        "entries_digest_mismatch",
        "entry_missing",
        "entry_path_unsafe",
        "evidence_missing",
        "exit_code_nonzero",
        "empty_commands",
        "guard_blocked",
        "invalid_json",
        "invalid_jsonl_line",
        "journal_chain_error",
        "journal_empty",
        "journal_partial_tail",
        "media_type_mismatch",
        "no_artifacts",
        "no_explicit_verdict",
        "no_records",
        "not_git_repository",
        "only_out_of_scope",
        "period_not_supplied",
        "planned_commands_mismatch",
        "planned_commands_missing",
        "population_truncated",
        "read_limit_exceeded",
        "run_binding_mismatch",
        "schema_missing",
        "signature_mismatch",
        "signature_unverifiable",
        "sod_failed",
        "sod_indeterminate",
        "status_canceled",
        "status_failed",
        "status_not_terminal",
        "status_rejected",
        "subject_mismatch",
        "symlink_refused",
        "timestamp_missing",
        "trailer_digest_mismatch",
        "untrusted_key",
        "verifier_error",
        "verifier_not_wired",
        "verifier_tool_unavailable",
    }
)

MAX_DISCOVERED_PER_CLAIM = 200
MAX_REPORTED_ARTIFACTS = 20


class ReadinessContractError(ValueError):
    """Raised when an observation violates the readiness contract."""


def dims(**statuses: str | tuple[str, str]) -> dict[str, dict[str, str | None]]:
    """Build a full dimension map; unspecified dimensions are ``not_checked``."""
    result: dict[str, dict[str, str | None]] = {}
    for name in DIMENSIONS:
        value = statuses.pop(name, "not_checked")
        status, reason = value if isinstance(value, tuple) else (value, None)
        if status not in DIMENSION_STATUSES:
            raise ReadinessContractError(f"invalid dimension status {status!r}")
        if reason is not None and reason not in REASON_CODES:
            raise ReadinessContractError(f"unknown reason code {reason!r}")
        result[name] = {"status": status, "reason": reason}
    if statuses:
        raise ReadinessContractError(f"unknown dimensions: {sorted(statuses)}")
    return result


@dataclass
class ArtifactObservation:
    """What one evaluator observed for one artifact.

    ``proposed`` is the evaluator's outcome before contract invariants apply.
    ``outcome`` is filled by :func:`finalize_artifact`.
    """

    relpath: str
    level: str
    proposed: str
    dimensions: dict[str, dict[str, str | None]]
    reasons: list[str] = field(default_factory=list)
    scope: str = "in_scope"
    run_binding: str | None = None
    timestamp: datetime | None = None
    outcome: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "relpath": self.relpath,
            "scope": self.scope,
            "run_binding": self.run_binding,
            "validation_level": self.level,
            "outcome": self.outcome,
            "reason_codes": sorted(set(self.reasons)),
            "dimensions": {name: dict(self.dimensions[name]) for name in DIMENSIONS},
        }


def validate_observation(obs: object) -> ArtifactObservation:
    """Check an observation against the fixed level, outcome, scope, dimension and reason vocabulary.

    This is the boundary for verifier adapters: anything outside the contract
    raises :class:`ReadinessContractError` instead of reaching aggregation.
    """
    if not isinstance(obs, ArtifactObservation):
        raise ReadinessContractError("observation is not an ArtifactObservation")
    if not isinstance(obs.relpath, str) or not obs.relpath:
        raise ReadinessContractError("observation relpath is missing")
    if obs.level not in _LEVEL_RANK:
        raise ReadinessContractError(f"invalid level {obs.level!r}")
    if obs.proposed not in OUTCOMES:
        raise ReadinessContractError(f"invalid outcome {obs.proposed!r}")
    if obs.scope not in ARTIFACT_SCOPES:
        raise ReadinessContractError(f"invalid scope {obs.scope!r}")
    if not isinstance(obs.reasons, list) or any(reason not in REASON_CODES for reason in obs.reasons):
        raise ReadinessContractError("unknown reason code")
    if not isinstance(obs.dimensions, dict) or set(obs.dimensions) != set(DIMENSIONS):
        raise ReadinessContractError("observation must carry exactly the fixed dimensions")
    for name in DIMENSIONS:
        value = obs.dimensions[name]
        if not isinstance(value, dict) or set(value) != {"status", "reason"}:
            raise ReadinessContractError(f"invalid dimension {name!r}")
        if value["status"] not in DIMENSION_STATUSES:
            raise ReadinessContractError(f"invalid dimension status {value['status']!r}")
        if value["reason"] is not None and value["reason"] not in REASON_CODES:
            raise ReadinessContractError(f"unknown reason code {value['reason']!r}")
    return obs


def _depth_capped(outcome: str, level: str) -> str:
    """Cap a positive outcome at what the processing depth supports."""
    if outcome == "validated" and level != "claim_validated":
        outcome = "structure_observed"
    if outcome == "structure_observed" and _LEVEL_RANK[level] < _LEVEL_RANK["structure_observed"]:
        outcome = "discovered_only"
    return outcome


def finalize_artifact(obs: ArtifactObservation, required: frozenset[str]) -> ArtifactObservation:
    """Apply contract invariants to an evaluator's proposed outcome.

    Dimensions are independent: a passed dimension never offsets a failed,
    unavailable or unknown required dimension.  ``validated`` additionally needs
    ``claim_validated`` depth and every required dimension passed or
    not applicable.
    """
    validate_observation(obs)
    statuses = {str(obs.dimensions[name]["status"]) for name in required}
    outcome = obs.proposed
    # A recorded denial or negative SoD verdict is an operational failure,
    # not malformed evidence. Other required failures still reject a failed
    # operation, including a receipt that names the wrong run.
    failed = {
        name
        for name in required
        if obs.dimensions[name]["status"] == "failed"
        and not (
            outcome == "failed"
            and name == "authorization"
            and obs.dimensions[name]["reason"] in {"approval_denied", "sod_failed"}
        )
    }
    if failed and outcome not in {"rejected", "invalid"}:
        outcome = "rejected"
    elif "unavailable" in statuses and _SEVERITY_RANK[outcome] > _SEVERITY_RANK["unavailable"]:
        outcome = "unavailable"
    elif "unknown" in statuses and _SEVERITY_RANK[outcome] > _SEVERITY_RANK["incomplete"]:
        outcome = "incomplete"
    if outcome == "validated" and "not_checked" in statuses:
        outcome = "structure_observed"
    outcome = _depth_capped(outcome, obs.level)
    for name in sorted(required):
        dim_reason = obs.dimensions[name]["reason"]
        if dim_reason is not None and obs.dimensions[name]["status"] not in {"passed", "not_applicable"}:
            obs.reasons.append(dim_reason)
    obs.outcome = outcome
    return obs


@dataclass
class ClaimReadiness:
    """Aggregated readiness for one claim."""

    claim_id: str
    evaluator: dict[str, Any]
    verifier: dict[str, Any]
    required: frozenset[str]
    level: str
    outcome: str
    reason: str | None
    reasons: list[str]
    dimensions: dict[str, dict[str, str | None]]
    population: dict[str, Any]
    artifacts: list[ArtifactObservation]

    def legacy_state(self) -> str:
        """Conservative projection onto the four legacy state strings."""
        if self.outcome == "validated":
            return "evidenced_passed"
        if self.outcome in {"failed", "rejected"}:
            return "evidenced_failed"
        if self.outcome == "not_applicable":
            return "not_applicable"
        return "untested"

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": READINESS_CONTRACT,
            "claim_id": self.claim_id,
            "evaluator": dict(self.evaluator),
            "verifier": dict(self.verifier),
            "validation_level": self.level,
            "outcome": self.outcome,
            "reason": self.reason,
            "reason_codes": list(self.reasons),
            "required_dimensions": sorted(self.required),
            "dimensions": {name: dict(self.dimensions[name]) for name in DIMENSIONS},
            "population": dict(self.population),
            "artifacts": [obs.to_dict() for obs in self.artifacts[:MAX_REPORTED_ARTIFACTS]],
            "artifacts_reported_truncated": len(self.artifacts) > MAX_REPORTED_ARTIFACTS,
            "legacy_state": self.legacy_state(),
        }


def aggregate(
    *,
    claim_id: str,
    evaluator: dict[str, Any],
    verifier: dict[str, Any],
    required: frozenset[str],
    observations: list[ArtifactObservation],
    truncated: bool,
    scan_limit: int,
    run_scoped: bool,
    period_supplied: bool,
    not_applicable_reason: str | None = None,
    window: dict[str, Any] | None = None,
) -> ClaimReadiness:
    """Combine artifact observations into one claim assessment.

    The most severe in-scope outcome wins and every count stays visible, so a
    validated artifact cannot hide a rejected, invalid, failed, unavailable,
    incomplete, structure-only or discovered-only sibling.  Wrong-run and
    out-of-period artifacts are excluded and counted.  Unbound artifacts under a
    run scope are never assumed to belong to the run; they keep the claim
    ``incomplete``.  A truncated population can never validate.  ``window``
    describes a claim's defined population bound (such as a fixed number of
    recent commits); it is reported, not treated as truncation.
    """
    observations = sorted(observations, key=lambda o: o.relpath)
    for obs in observations:
        finalize_artifact(obs, required)
    counts = {key: 0 for key in ("wrong_run", "out_of_period", "unbound", "in_scope")}
    outcome_counts = {outcome: 0 for outcome in sorted(OUTCOMES - {"not_applicable", "absent"})}
    for obs in observations:
        counts[obs.scope] += 1
        if obs.scope == "in_scope" and obs.outcome in outcome_counts:
            outcome_counts[obs.outcome] += 1
    in_scope = [obs for obs in observations if obs.scope == "in_scope"]
    reasons: set[str] = set()
    for obs in observations:
        if obs.scope == "in_scope":
            reasons.update(obs.reasons)
    primary: str | None = None

    if not_applicable_reason is not None:
        outcome = "not_applicable"
        primary = not_applicable_reason
    elif in_scope:
        outcome = min((obs.outcome for obs in in_scope), key=_SEVERITY_RANK.__getitem__)
        lead = next(obs for obs in in_scope if obs.outcome == outcome)
        primary = sorted(set(lead.reasons))[0] if lead.reasons else None
    elif observations:
        outcome = "absent"
        primary = "only_out_of_scope" if not counts["unbound"] else "artifact_not_run_bound"
    else:
        outcome = "absent"
        primary = "no_artifacts"

    gate_rank = _SEVERITY_RANK["incomplete"]
    if outcome != "not_applicable":
        if run_scoped and counts["unbound"] and _SEVERITY_RANK[outcome] > gate_rank:
            outcome = "incomplete"
            primary = "artifact_not_run_bound"
        if truncated and _SEVERITY_RANK[outcome] > gate_rank:
            outcome = "incomplete"
            primary = "population_truncated"
    if counts["unbound"] and run_scoped:
        reasons.add("artifact_not_run_bound")
    if truncated:
        reasons.add("population_truncated")
    if primary is not None:
        reasons.add(primary)
    if not period_supplied:
        reasons.add("period_not_supplied")

    if in_scope:
        level = min((obs.level for obs in in_scope), key=_LEVEL_RANK.__getitem__)
    elif observations:
        level = "discovered"
    else:
        level = "none"

    claim_dims: dict[str, dict[str, str | None]] = {}
    for name in DIMENSIONS:
        if not in_scope:
            claim_dims[name] = {"status": "not_checked", "reason": primary if name == "population" else None}
            continue
        worst = min(in_scope, key=lambda o: _DIMENSION_RANK[str(o.dimensions[name]["status"])])
        claim_dims[name] = dict(worst.dimensions[name])
    # Claim-level basis for population: discovery enumerated the defined
    # population without truncation or unbound artifacts.  Only then does an
    # artifact-level ``not_applicable`` population become ``passed``.
    enumerated = not truncated and not (run_scoped and counts["unbound"])
    if in_scope and enumerated and claim_dims["population"]["status"] == "not_applicable":
        claim_dims["population"] = {"status": "passed", "reason": None}
    if not period_supplied:
        claim_dims["freshness"] = {"status": "not_applicable", "reason": "period_not_supplied"}
    if outcome != "not_applicable" and (truncated or (run_scoped and counts["unbound"])):
        claim_dims["population"] = {
            "status": "unknown",
            "reason": "population_truncated" if truncated else "artifact_not_run_bound",
        }

    # Reconcile the whole assessed population: a validated claim needs
    # claim-validated depth and every required claim dimension passed or not
    # applicable, whichever artifact set the outcome.
    if outcome == "validated" and any(
        claim_dims[name]["status"] not in {"passed", "not_applicable"} for name in required
    ):
        outcome = "structure_observed"
    if outcome in {"validated", "structure_observed"}:
        outcome = _depth_capped(outcome, level)

    population: dict[str, Any] = {
        "discovered": len(observations),
        "in_scope": counts["in_scope"],
        "wrong_run": counts["wrong_run"],
        "out_of_period": counts["out_of_period"],
        "unbound": counts["unbound"],
        "scan_limit": scan_limit,
        "truncated": truncated,
    }
    population.update(outcome_counts)
    if window is not None:
        population["window"] = dict(window)
    return ClaimReadiness(
        claim_id=claim_id,
        evaluator=evaluator,
        verifier=verifier,
        required=required,
        level=level,
        outcome=outcome,
        # ``None`` when no reason code applies (for example a validated claim);
        # never an empty string outside the reason vocabulary.
        reason=primary,
        reasons=sorted(reasons),
        dimensions=claim_dims,
        population=population,
        artifacts=observations,
    )
