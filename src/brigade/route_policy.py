"""Route-level skill exploration policy and band classification (#573)."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from . import outcome_cmd, scorecard, verify_manifest

if TYPE_CHECKING:
    from .route_catalog import RouteBrief
    from .run_transport import Assignment

ROUTE_POLICY_VERSION = "route-policy.v1"

EXPLORATION_ASSIGNMENT_CAP = 2
EXPLORATION_ASSIGNMENT_PCT = 0.10
ONE_EXPLORATORY_SKILL_PER_ROUTE = True
EXPLORATION_DECAY_DAYS = 7
EXPLORATION_HARD_CEILING_DAYS = 30
EXPLORATION_HARD_CEILING = 5

Band = Literal["unseen", "candidate", "provisional", "promoted"]
RouteAuthority = Literal["shadow", "scoped_write", "full"]
ManifestRole = Literal["production", "fixture_eval"]


@dataclass(frozen=True)
class TrustedOptInEntry:
    manifest: verify_manifest.VerifyManifest
    manifest_path: str
    card: scorecard.SubjectScorecard | None = None


@dataclass(frozen=True)
class SkillAssignment:
    artifact_id: str
    band: Band
    route_authority: RouteAuthority
    exploratory: bool
    score_inputs: dict[str, Any]
    scope_globs: tuple[str, ...] = ()
    manifest_path: str | None = None
    manifest_id: str | None = None
    subject_path: str | None = None
    verify_manifest_id: str | None = None
    reason: str | None = None


@dataclass
class RoutePolicyDecision:
    route_class: str | None
    assignments: list[SkillAssignment] = field(default_factory=list)
    eligible_assignment_count_7d: int = 0
    exploratory_assignment_count_7d: int = 0
    quota: int = 0
    accept_reject: list[dict[str, Any]] = field(default_factory=list)
    policy_applied: bool = False
    decided_at: datetime | None = None


def classify_band(
    card: scorecard.SubjectScorecard | None,
    *,
    persisted_status: str | None = None,
    policy_marker: str | None = None,
) -> Band:
    """Classify exploration band from receipt-only scorecard and scorecard route policy."""
    hurt = 0
    if card is not None:
        hurt = int(card.dimensions.get("effectiveness", {}).get("hurt", 0))
    promoted_authority = (
        persisted_status == "promoted"
        and policy_marker == scorecard.SCORECARD_POLICY_VERSION
        and hurt == 0
        and card is not None
    )
    if promoted_authority:
        return "promoted"
    if card is None:
        return "unseen"
    effectiveness = card.dimensions.get("effectiveness", {})
    helped = int(effectiveness.get("helped", 0))
    trials = int(effectiveness.get("trials", 0))
    if trials == 0 or helped == 0:
        return "unseen"
    utility = card.utility_guardrails
    required_ids = utility.get("required_check_ids") or []
    passing = int(utility.get("passing_trials", 0))
    required = int(utility.get("required_trials", scorecard._UTILITY_REQUIRED_TRIALS))
    utility_complete = not required_ids or passing >= required
    if helped >= 2 and not utility_complete:
        return "provisional"
    if helped >= 1:
        return "candidate"
    return "unseen"


def route_authority_for_band(band: Band) -> RouteAuthority:
    if band == "promoted":
        return "full"
    if band == "unseen":
        return "shadow"
    return "scoped_write"


def exploration_quota(eligible_assignment_count: int) -> int:
    eligible = max(1, eligible_assignment_count)
    return min(EXPLORATION_ASSIGNMENT_CAP, max(1, math.floor(EXPLORATION_ASSIGNMENT_PCT * eligible)))


def validate_scope_globs(globs: tuple[str, ...]) -> str | None:
    """Return a stable rejection reason when scope_globs are unsafe or unusable."""
    return verify_manifest.validate_scope_globs(globs)


def _default_runs_dir(target: Path) -> Path:
    return target / ".brigade" / "runs"


def discover_route_decision_paths(target: Path, *, runs_dir: Path | None = None) -> list[Path]:
    root = runs_dir.expanduser().resolve() if runs_dir is not None else _default_runs_dir(target)
    if not root.is_dir():
        return []
    return sorted(
        child / "route-decision.json" for child in root.iterdir() if (child / "route-decision.json").is_file()
    )


def _parse_timestamp(value: str | None) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _decision_timestamp(decision: dict[str, Any], decision_path: Path) -> datetime | None:
    for key in ("decided_at", "assigned_at"):
        parsed = _parse_timestamp(decision.get(key))
        if parsed is not None:
            return parsed
    run_path = decision_path.parent / "run.json"
    if run_path.is_file():
        try:
            run_receipt = json.loads(run_path.read_text())
        except (OSError, json.JSONDecodeError):
            run_receipt = None
        if isinstance(run_receipt, dict):
            for key in ("started_at", "finished_at"):
                parsed = _parse_timestamp(run_receipt.get(key))
                if parsed is not None:
                    return parsed
    return None


def _load_route_decision(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _route_class_from_decision(decision: dict[str, Any], decision_path: Path) -> str | None:
    exploration = decision.get("exploration")
    if isinstance(exploration, dict):
        route_class = exploration.get("route_class")
        if isinstance(route_class, str) and route_class:
            return route_class
    run_path = decision_path.parent / "run.json"
    if not run_path.is_file():
        return None
    try:
        run_receipt = json.loads(run_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(run_receipt, dict):
        return None
    manifest = outcome_cmd.route_manifest(run_receipt, run_path)
    return outcome_cmd.route_fingerprint(manifest)


def _assignments_from_decision(decision: dict[str, Any]) -> list[dict[str, Any]]:
    raw = decision.get("skill_assignments")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    return []


def _within_window(timestamp: datetime | None, *, now: datetime, days: int) -> bool:
    if timestamp is None:
        return False
    return timestamp >= now - timedelta(days=days)


def assignment_counts(
    decision_paths: list[Path],
    *,
    route_class: str,
    now: datetime,
) -> tuple[int, int, dict[str, int]]:
    """Return eligible 7d count, exploratory 7d count, and per-skill 30d exploratory counts."""
    eligible_7d = 0
    exploratory_7d = 0
    exploratory_30d_by_skill: dict[str, int] = {}
    for path in decision_paths:
        decision = _load_route_decision(path)
        if decision is None:
            continue
        decision_route_class = _route_class_from_decision(decision, path)
        if decision_route_class != route_class:
            continue
        timestamp = _decision_timestamp(decision, path)
        if not _within_window(timestamp, now=now, days=EXPLORATION_HARD_CEILING_DAYS):
            continue
        for assignment in _assignments_from_decision(decision):
            artifact_id = assignment.get("artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id:
                continue
            exploratory = bool(assignment.get("exploratory"))
            if _within_window(timestamp, now=now, days=EXPLORATION_DECAY_DAYS):
                eligible_7d += 1
                if exploratory:
                    exploratory_7d += 1
            if exploratory:
                exploratory_30d_by_skill[artifact_id] = exploratory_30d_by_skill.get(artifact_id, 0) + 1
    return eligible_7d, exploratory_7d, exploratory_30d_by_skill


def score_inputs_for_card(card: scorecard.SubjectScorecard, band: Band) -> dict[str, Any]:
    effectiveness = card.dimensions.get("effectiveness", {})
    return {
        "band": band,
        "effectiveness": {
            "helped": effectiveness.get("helped", 0),
            "hurt": effectiveness.get("hurt", 0),
            "wilson": effectiveness.get("wilson", 0.0),
            "trials": effectiveness.get("trials", 0),
        },
        "utility_guardrails": card.utility_guardrails,
    }


def route_manifest_from_brief(route: RouteBrief) -> dict[str, Any]:
    from . import router

    if not route.attached:
        return {"followed": False}
    path = next((signal for signal in route.signals if signal in router.PATHS), "unknown")
    return {
        "followed": True,
        "path": path,
        "size": route.size,
        "signals": sorted(route.signals),
    }


def _manifest_opts_into_route(
    manifest: verify_manifest.VerifyManifest,
    *,
    route_manifest: dict[str, Any],
    route_class: str,
) -> bool:
    if manifest.route_paths and verify_manifest.validate_route_paths(manifest.route_paths) is not None:
        return False
    if manifest.route_classes and verify_manifest.validate_route_classes(manifest.route_classes) is not None:
        return False
    if not manifest.route_paths and not manifest.route_classes:
        return False
    if manifest.route_paths and route_manifest.get("path") in manifest.route_paths:
        return True
    if manifest.route_classes and route_class in manifest.route_classes:
        return True
    return False


def _manifest_role(manifest: verify_manifest.VerifyManifest) -> ManifestRole:
    return "fixture_eval" if manifest.binding_mode == "fixture_eval" else "production"


def _subject_fingerprint_resolves(target: Path, manifest: verify_manifest.VerifyManifest) -> bool:
    return verify_manifest.resolve_subject_fingerprint(target, manifest) is not None


def _trusted_card_for_manifest(
    target: Path, manifest: verify_manifest.VerifyManifest
) -> scorecard.SubjectScorecard | None:
    card = scorecard.scorecard_for_artifact(
        target,
        manifest.artifact_id,
        artifact_kind=manifest.artifact_kind,
    )
    if card is None:
        return None
    fingerprint = verify_manifest.resolve_subject_fingerprint(target, manifest)
    if fingerprint is None:
        return None
    normalized = scorecard._normalize_fingerprint(fingerprint)
    if normalized is None or card.subject.content_fingerprint != normalized:
        return None
    return card


def _role_ambiguity_reason(role: ManifestRole) -> str:
    if role == "production":
        return "ambiguous_production_manifest"
    return "ambiguous_fixture_eval_manifest"


def _entry_from_selected_manifest(
    target: Path,
    manifest: verify_manifest.VerifyManifest,
    manifest_path: str,
) -> TrustedOptInEntry | None:
    if _manifest_role(manifest) == "production":
        card = _trusted_card_for_manifest(target, manifest)
        if card is None:
            return None
        return TrustedOptInEntry(manifest=manifest, manifest_path=manifest_path, card=card)
    if not _subject_fingerprint_resolves(target, manifest):
        return None
    card = _trusted_card_for_manifest(target, manifest)
    return TrustedOptInEntry(manifest=manifest, manifest_path=manifest_path, card=card)


def _select_manifest_for_band(
    *,
    band: Band,
    production: tuple[verify_manifest.VerifyManifest, str] | None,
    fixture: tuple[verify_manifest.VerifyManifest, str] | None,
) -> tuple[verify_manifest.VerifyManifest, str] | None:
    if band == "unseen":
        return fixture
    return production or fixture


def _artifact_id_from_payload(payload: dict[str, Any]) -> str | None:
    subject = payload.get("subject")
    if not isinstance(subject, dict):
        return None
    artifact_id = subject.get("artifact_id")
    return str(artifact_id) if isinstance(artifact_id, str) and artifact_id else None


def _route_opted_raw_payload(
    payload: dict[str, Any],
    *,
    route_manifest: dict[str, Any],
    route_class: str,
) -> bool:
    route_paths_raw = payload.get("route_paths", [])
    route_classes_raw = payload.get("route_classes", [])
    if not route_paths_raw and not route_classes_raw:
        return False
    route_paths = tuple(str(item) for item in route_paths_raw) if isinstance(route_paths_raw, list) else ()
    route_classes = tuple(str(item) for item in route_classes_raw) if isinstance(route_classes_raw, list) else ()
    if route_paths and verify_manifest.validate_route_paths(route_paths) is not None:
        return False
    if route_classes and verify_manifest.validate_route_classes(route_classes) is not None:
        return False
    if route_paths and route_manifest.get("path") in route_paths:
        return True
    if route_classes and route_class in route_classes:
        return True
    return False


def discover_trusted_opt_in_manifests(
    target: Path,
    *,
    route_manifest: dict[str, Any],
    route_class: str,
) -> tuple[list[TrustedOptInEntry], list[dict[str, Any]]]:
    grouped: dict[str, dict[ManifestRole, list[tuple[verify_manifest.VerifyManifest, str]]]] = {}
    rejections: list[dict[str, Any]] = []
    for path in verify_manifest._discover_manifest_files(target, tracked_only=True):
        payload = verify_manifest._load_manifest_file(path)
        if not isinstance(payload, dict):
            continue
        try:
            manifest = verify_manifest.manifest_from_payload(payload, path=path)
        except ValueError:
            artifact_id = _artifact_id_from_payload(payload)
            if artifact_id and _route_opted_raw_payload(
                payload,
                route_manifest=route_manifest,
                route_class=route_class,
            ):
                scope_globs_raw = payload.get("scope_globs", [])
                if isinstance(scope_globs_raw, list):
                    scope_reason = verify_manifest.validate_scope_globs(scope_globs_raw)
                    if scope_reason is not None:
                        rejections.append(
                            {
                                "artifact_id": artifact_id,
                                "accepted": False,
                                "reason": scope_reason,
                            }
                        )
            continue
        if not _manifest_opts_into_route(manifest, route_manifest=route_manifest, route_class=route_class):
            continue
        manifest_path = verify_manifest.manifest_source_path(target, manifest)
        if not manifest_path:
            continue
        role = _manifest_role(manifest)
        grouped.setdefault(manifest.artifact_id, {}).setdefault(role, []).append((manifest, manifest_path))

    entries: list[TrustedOptInEntry] = []
    for artifact_id in sorted(grouped):
        roles = grouped[artifact_id]
        ambiguous = False
        for role, items in roles.items():
            if len(items) > 1:
                rejections.append(
                    {
                        "artifact_id": artifact_id,
                        "accepted": False,
                        "reason": _role_ambiguity_reason(role),
                    }
                )
                ambiguous = True
        if ambiguous:
            continue
        production = roles.get("production", [None])[0] if roles.get("production") else None
        fixture = roles.get("fixture_eval", [None])[0] if roles.get("fixture_eval") else None
        card = None
        if production is not None:
            card = _trusted_card_for_manifest(target, production[0])
        elif fixture is not None:
            card = _trusted_card_for_manifest(target, fixture[0])
        band = classify_band(
            card,
            persisted_status=_persisted_status(target, artifact_id),
            policy_marker=_persisted_policy_marker(target, artifact_id),
        )
        selected = _select_manifest_for_band(band=band, production=production, fixture=fixture)
        if selected is None:
            continue
        entry = _entry_from_selected_manifest(target, selected[0], selected[1])
        if entry is not None:
            entries.append(entry)
    return sorted(entries, key=lambda item: (item.manifest.artifact_id, item.manifest_path)), rejections


def _fixture_eval_manifest_for_skill(
    target: Path,
    artifact_id: str,
) -> tuple[verify_manifest.VerifyManifest | None, str | None]:
    matches: list[tuple[verify_manifest.VerifyManifest, str]] = []
    for path in verify_manifest._discover_manifest_files(target, tracked_only=True):
        payload = verify_manifest._load_manifest_file(path)
        if not isinstance(payload, dict):
            continue
        try:
            manifest = verify_manifest.manifest_from_payload(payload, path=path)
        except ValueError:
            continue
        if manifest.binding_mode != "fixture_eval":
            continue
        if manifest.artifact_id != artifact_id:
            continue
        manifest_path = verify_manifest.manifest_source_path(target, manifest)
        if not manifest_path:
            return None, "missing_manifest_path"
        matches.append((manifest, manifest_path))
    if not matches:
        return None, "missing_fixture_eval_manifest"
    if len(matches) > 1:
        return None, "ambiguous_fixture_eval_manifest"
    return matches[0][0], matches[0][1]


def _scope_from_manifest(manifest: verify_manifest.VerifyManifest) -> tuple[tuple[str, ...] | None, str | None]:
    if not manifest.scope_globs:
        return None, "missing_scope_globs"
    reason = validate_scope_globs(manifest.scope_globs)
    if reason is not None:
        return None, reason
    return manifest.scope_globs, None


def _persisted_status(target: Path, artifact_id: str) -> str | None:
    entry = outcome_cmd.load_status(target).get(artifact_id)
    if not isinstance(entry, dict):
        return None
    status = entry.get("status")
    return str(status) if isinstance(status, str) and status else None


def _persisted_policy_marker(target: Path, artifact_id: str) -> str | None:
    entry = outcome_cmd.load_status(target).get(artifact_id)
    if not isinstance(entry, dict):
        return None
    route_policy = entry.get("route_policy")
    if not isinstance(route_policy, dict):
        return None
    marker = route_policy.get("policy_version")
    return str(marker) if isinstance(marker, str) and marker else None


@dataclass(frozen=True)
class RouteBudget:
    token_budget: int | None = None
    work_budget: int | None = None
    token_spent: int = 0
    work_spent: int = 0

    def allows_exploratory(self) -> bool:
        if self.token_budget is not None and self.token_spent >= self.token_budget:
            return False
        if self.work_budget is not None and self.work_spent >= self.work_budget:
            return False
        return True


def _exploration_block_reason(
    *,
    exploratory_7d: int,
    quota: int,
    budget: RouteBudget | None,
) -> str | None:
    if exploratory_7d >= quota:
        return "exploration_quota_exhausted"
    if budget is not None and not budget.allows_exploratory():
        return "routing_budget_exhausted"
    return None


def _reject_exploratory_pool(
    decision: RoutePolicyDecision,
    pool: list[tuple[TrustedOptInEntry, Band]],
    *,
    reason: str,
) -> None:
    for entry, _band in pool:
        decision.accept_reject.append({"artifact_id": entry.manifest.artifact_id, "accepted": False, "reason": reason})


def _select_scoped_exploratory(
    target: Path,
    decision: RoutePolicyDecision,
    entry: TrustedOptInEntry,
    band: Band,
    *,
    exploratory_30d: dict[str, int],
) -> SkillAssignment | None:
    artifact_id = entry.manifest.artifact_id
    if exploratory_30d.get(artifact_id, 0) >= EXPLORATION_HARD_CEILING:
        decision.accept_reject.append(
            {"artifact_id": artifact_id, "accepted": False, "reason": "exploration_hard_ceiling"}
        )
        return None
    globs, scope_reason = _scope_from_manifest(entry.manifest)
    if globs is None:
        decision.accept_reject.append(
            {"artifact_id": artifact_id, "accepted": False, "reason": scope_reason or "missing_scope_globs"}
        )
        return None
    score_inputs = score_inputs_for_card(entry.card, band) if entry.card is not None else {"band": band}
    decision.accept_reject.append({"artifact_id": artifact_id, "accepted": True, "reason": "exploratory_selected"})
    return SkillAssignment(
        artifact_id=artifact_id,
        band=band,
        route_authority="scoped_write",
        exploratory=True,
        score_inputs=score_inputs,
        scope_globs=globs,
        manifest_path=entry.manifest_path,
        manifest_id=entry.manifest.manifest_id,
        subject_path=entry.manifest.subject_path,
    )


def _select_shadow_exploratory(
    target: Path,
    decision: RoutePolicyDecision,
    entry: TrustedOptInEntry,
    band: Band,
    *,
    exploratory_30d: dict[str, int],
) -> SkillAssignment | None:
    artifact_id = entry.manifest.artifact_id
    if exploratory_30d.get(artifact_id, 0) >= EXPLORATION_HARD_CEILING:
        decision.accept_reject.append(
            {"artifact_id": artifact_id, "accepted": False, "reason": "exploration_hard_ceiling"}
        )
        return None
    if entry.manifest.binding_mode == "fixture_eval":
        fixture_manifest = entry.manifest
        fixture_path = entry.manifest_path
    else:
        fixture_manifest, fixture_path = _fixture_eval_manifest_for_skill(target, artifact_id)
        if fixture_manifest is None:
            decision.accept_reject.append(
                {
                    "artifact_id": artifact_id,
                    "accepted": False,
                    "reason": fixture_path or "missing_fixture_eval_manifest",
                }
            )
            return None
    decision.accept_reject.append({"artifact_id": artifact_id, "accepted": True, "reason": "shadow_with_proven_route"})
    return SkillAssignment(
        artifact_id=artifact_id,
        band=band,
        route_authority="shadow",
        exploratory=True,
        score_inputs={"band": band},
        manifest_path=fixture_path,
        manifest_id=fixture_manifest.manifest_id,
        subject_path=fixture_manifest.subject_path,
        verify_manifest_id=fixture_manifest.manifest_id,
        reason="shadow_fixture_eval",
    )


DIRECT_WORKER_REJECTS_SHADOW = "direct_worker_rejects_shadow"


def shadow_verify_invocation(manifest_id: str) -> str:
    return f"brigade work verify run --target . --manifest {manifest_id}"


def direct_worker_skill_ids(
    decision: RoutePolicyDecision | None,
    *,
    allow_shadow: bool = False,
) -> tuple[tuple[str, ...], str | None]:
    """Select skill ids for a direct-worker assignment from a pre-run policy decision."""
    if decision is None or not decision.policy_applied:
        return (), None
    exploratory = [item for item in decision.assignments if item.exploratory]
    if len(exploratory) > 1:
        return (), "direct_worker_supports_at_most_one_exploratory_skill"
    if not exploratory:
        return (), None
    item = exploratory[0]
    if item.route_authority == "shadow":
        if not allow_shadow:
            return (), DIRECT_WORKER_REJECTS_SHADOW
        return (item.artifact_id,), None
    if item.route_authority == "scoped_write":
        return (item.artifact_id,), None
    return (), f"direct_worker_cannot_bind_{item.route_authority}"


def decide_route_skills(
    target: Path,
    *,
    route_brief: RouteBrief | None = None,
    run_receipt: dict[str, Any] | None = None,
    decision_paths: list[Path] | None = None,
    now: datetime | None = None,
    budget: RouteBudget | None = None,
    runs_dir: Path | None = None,
    allow_shadow: bool = True,
) -> RoutePolicyDecision:
    """Select proven and exploratory skill assignments under exploration caps."""
    target = target.expanduser().resolve()
    now = now or datetime.now(timezone.utc)

    if route_brief is not None:
        if not route_brief.attached:
            return RoutePolicyDecision(route_class=None)
        route_manifest = route_manifest_from_brief(route_brief)
    elif isinstance(run_receipt, dict):
        route = run_receipt.get("route")
        if not isinstance(route, dict) or not route.get("attached"):
            return RoutePolicyDecision(route_class=None)
        route_manifest = outcome_cmd.route_manifest(run_receipt, None)
    else:
        return RoutePolicyDecision(route_class=None)

    route_class = outcome_cmd.route_fingerprint(route_manifest)
    if route_class is None:
        return RoutePolicyDecision(route_class=None)

    opt_ins, discovery_rejections = discover_trusted_opt_in_manifests(
        target,
        route_manifest=route_manifest,
        route_class=route_class,
    )
    if not opt_ins and not discovery_rejections:
        return RoutePolicyDecision(route_class=route_class)

    paths = decision_paths if decision_paths is not None else discover_route_decision_paths(target, runs_dir=runs_dir)
    eligible_7d, exploratory_7d, exploratory_30d = assignment_counts(paths, route_class=route_class, now=now)
    quota = exploration_quota(eligible_7d)

    classified: list[tuple[TrustedOptInEntry, Band]] = []
    for entry in opt_ins:
        artifact_id = entry.manifest.artifact_id
        band = classify_band(
            entry.card,
            persisted_status=_persisted_status(target, artifact_id),
            policy_marker=_persisted_policy_marker(target, artifact_id),
        )
        classified.append((entry, band))

    proven = sorted(
        [(entry, band) for entry, band in classified if band == "promoted"],
        key=lambda item: (
            -item[0].card.dimensions.get("effectiveness", {}).get("wilson", 0.0)  # type: ignore[union-attr]
            if item[0].card is not None
            else 0.0,
            item[0].manifest.artifact_id,
        ),
    )
    exploratory_pool = sorted(
        [(entry, band) for entry, band in classified if band in {"candidate", "provisional"}],
        key=lambda item: (
            -item[0].card.dimensions.get("effectiveness", {}).get("wilson", 0.0)  # type: ignore[union-attr]
            if item[0].card is not None
            else 0.0,
            item[0].manifest.artifact_id,
        ),
    )
    shadow_pool = sorted(
        [(entry, band) for entry, band in classified if band == "unseen"],
        key=lambda item: item[0].manifest.artifact_id,
    )

    decision = RoutePolicyDecision(
        route_class=route_class,
        eligible_assignment_count_7d=eligible_7d,
        exploratory_assignment_count_7d=exploratory_7d,
        quota=quota,
        policy_applied=True,
        decided_at=now,
        accept_reject=list(discovery_rejections),
    )

    for entry, band in proven:
        assert entry.card is not None
        decision.assignments.append(
            SkillAssignment(
                artifact_id=entry.manifest.artifact_id,
                band=band,
                route_authority="full",
                exploratory=False,
                score_inputs=score_inputs_for_card(entry.card, band),
                manifest_path=entry.manifest_path,
                manifest_id=entry.manifest.manifest_id,
                subject_path=entry.manifest.subject_path,
            )
        )
        decision.accept_reject.append(
            {"artifact_id": entry.manifest.artifact_id, "accepted": True, "reason": "promoted_priority"}
        )

    selected_exploratory: SkillAssignment | None = None
    block_reason = _exploration_block_reason(exploratory_7d=exploratory_7d, quota=quota, budget=budget)
    if block_reason is not None:
        _reject_exploratory_pool(decision, exploratory_pool, reason=block_reason)
        if proven and allow_shadow:
            _reject_exploratory_pool(decision, shadow_pool, reason=block_reason)
    else:
        for entry, band in exploratory_pool:
            selected_exploratory = _select_scoped_exploratory(
                target,
                decision,
                entry,
                band,
                exploratory_30d=exploratory_30d,
            )
            if selected_exploratory is not None:
                break
        if selected_exploratory is None and proven and allow_shadow:
            for entry, band in shadow_pool:
                selected_exploratory = _select_shadow_exploratory(
                    target,
                    decision,
                    entry,
                    band,
                    exploratory_30d=exploratory_30d,
                )
                if selected_exploratory is not None:
                    break
        elif not allow_shadow and shadow_pool:
            for entry, _band in shadow_pool:
                decision.accept_reject.append(
                    {
                        "artifact_id": entry.manifest.artifact_id,
                        "accepted": False,
                        "reason": DIRECT_WORKER_REJECTS_SHADOW,
                    }
                )
        if selected_exploratory is None:
            pending = {entry.manifest.artifact_id for entry, _band in exploratory_pool} - {
                entry["artifact_id"] for entry in decision.accept_reject
            }
            for artifact_id in sorted(pending):
                decision.accept_reject.append(
                    {"artifact_id": artifact_id, "accepted": False, "reason": "one_exploratory_per_route"}
                )
            if proven:
                pending_shadow = {entry.manifest.artifact_id for entry, _band in shadow_pool} - {
                    entry["artifact_id"] for entry in decision.accept_reject
                }
                for artifact_id in sorted(pending_shadow):
                    decision.accept_reject.append(
                        {"artifact_id": artifact_id, "accepted": False, "reason": "one_exploratory_per_route"}
                    )
            elif shadow_pool:
                for entry, _band in shadow_pool:
                    decision.accept_reject.append(
                        {
                            "artifact_id": entry.manifest.artifact_id,
                            "accepted": False,
                            "reason": "unseen_cannot_be_sole_provider",
                        }
                    )

    if selected_exploratory is not None:
        decision.assignments.append(selected_exploratory)

    return decision


def exploratory_skill_ids(decision: RoutePolicyDecision | None) -> frozenset[str]:
    if decision is None or not decision.policy_applied:
        return frozenset()
    return frozenset(item.artifact_id for item in decision.assignments if item.exploratory)


def validate_plan_skill_bindings(assignments: list[Assignment], decision: RoutePolicyDecision | None) -> None:
    """Ensure planner bindings match the pre-run exploratory skill decision."""
    expected = exploratory_skill_ids(decision)
    if not expected:
        if any(assignment.selected_skill_ids for assignment in assignments):
            raise ValueError("selected_skill_ids must be omitted when no exploratory skill was selected")
        return
    if len(expected) != 1:
        raise ValueError("route policy selected more than one exploratory skill")
    skill_id = next(iter(expected))
    bound = [assignment for assignment in assignments if skill_id in assignment.selected_skill_ids]
    if len(bound) != 1:
        raise ValueError(f"exploratory skill {skill_id!r} must be bound to exactly one assignment")
    exploratory = next(item for item in decision.assignments if item.exploratory)  # type: ignore[union-attr]
    assignment = bound[0]
    if exploratory.route_authority == "shadow":
        if "verify" not in assignment.covers:
            raise ValueError(f"shadow skill {skill_id!r} must bind to a verify-covering assignment")
        manifest_id = exploratory.verify_manifest_id or ""
        invocation = shadow_verify_invocation(manifest_id)
        if invocation not in assignment.task:
            raise ValueError(
                f"shadow skill {skill_id!r} assignment must include the exact verify invocation {invocation!r}"
            )
    elif exploratory.route_authority == "scoped_write":
        if assignment.covers == ("verify",) or (len(assignment.covers) == 1 and assignment.covers[0] == "verify"):
            raise ValueError(f"scoped-write skill {skill_id!r} must bind to a production assignment")
    extra = sorted(
        {skill for assignment in assignments for skill in assignment.selected_skill_ids if skill not in expected}
    )
    if extra:
        raise ValueError(f"unknown selected_skill_ids: {', '.join(extra)}")


def _assignment_to_dict(assignment: SkillAssignment) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "artifact_id": assignment.artifact_id,
        "band": assignment.band,
        "route_authority": assignment.route_authority,
        "exploratory": assignment.exploratory,
        "score_inputs": assignment.score_inputs,
    }
    if assignment.scope_globs:
        payload["scope_globs"] = list(assignment.scope_globs)
    if assignment.manifest_path:
        payload["manifest_path"] = assignment.manifest_path
    if assignment.manifest_id:
        payload["manifest_id"] = assignment.manifest_id
    if assignment.subject_path:
        payload["subject_path"] = assignment.subject_path
    if assignment.verify_manifest_id:
        payload["verify_manifest_id"] = assignment.verify_manifest_id
    if assignment.reason:
        payload["reason"] = assignment.reason
    return payload


def route_policy_extensions_from_decision(decision: RoutePolicyDecision) -> dict[str, Any]:
    if not decision.policy_applied or decision.decided_at is None:
        return {}
    return {
        "decided_at": _utc_iso(decision.decided_at),
        "policy_version": ROUTE_POLICY_VERSION,
        "score_inputs": {item.artifact_id: item.score_inputs for item in decision.assignments},
        "skill_assignments": [_assignment_to_dict(item) for item in decision.assignments],
        "exploration": {
            "route_class": decision.route_class,
            "eligible_assignment_count_7d": decision.eligible_assignment_count_7d,
            "exploratory_assignment_count_7d": decision.exploratory_assignment_count_7d,
            "quota": decision.quota,
            "accept_reject": decision.accept_reject,
        },
    }


def route_policy_payload(
    target: Path,
    run_receipt: dict[str, Any],
    *,
    now: datetime | None = None,
    budget: RouteBudget | None = None,
    runs_dir: Path | None = None,
    exclude_decision_path: Path | None = None,
    policy_decision: RoutePolicyDecision | None = None,
) -> dict[str, Any]:
    """Build additive route-decision fields for scorecard skill exploration."""
    if policy_decision is not None:
        return route_policy_extensions_from_decision(policy_decision)
    paths = discover_route_decision_paths(target, runs_dir=runs_dir)
    if exclude_decision_path is not None:
        resolved = exclude_decision_path.resolve()
        paths = [path for path in paths if path.resolve() != resolved]
    decision = decide_route_skills(
        target,
        run_receipt=run_receipt,
        decision_paths=paths,
        now=now,
        budget=budget,
        runs_dir=runs_dir,
    )
    return route_policy_extensions_from_decision(decision)


def planner_skill_policy_section(decision: RoutePolicyDecision | None) -> str:
    if decision is None or not decision.policy_applied or not decision.assignments:
        return ""
    lines = ["## Skill route policy (deterministic, pre-run)"]
    exploratory_ids = exploratory_skill_ids(decision)
    if exploratory_ids:
        lines.append(
            "- Bind each accepted exploratory skill to exactly one assignment via "
            '`"selected_skill_ids": ["<artifact-id>"]`. '
            "Each exploratory skill id must appear on exactly one assignment."
        )
    for assignment in decision.assignments:
        if assignment.route_authority == "full":
            lines.append(
                f"- promoted skill `{assignment.artifact_id}` (manifest `{assignment.manifest_path}`): "
                "full route authority"
            )
        elif assignment.route_authority == "scoped_write":
            globs = ", ".join(assignment.scope_globs)
            lines.append(
                f"- exploratory skill `{assignment.artifact_id}` ({assignment.band}, manifest "
                f"`{assignment.manifest_path}`): scoped write only within globs [{globs}]; "
                "bind it to one production assignment with selected_skill_ids"
            )
        elif assignment.route_authority == "shadow":
            invocation = shadow_verify_invocation(assignment.verify_manifest_id or "")
            lines.append(
                f"- shadow skill `{assignment.artifact_id}` (unseen, manifest `{assignment.manifest_path}`): "
                f"read-only fixture evaluation via verify manifest `{assignment.verify_manifest_id}`; "
                "bind it to one verify-covering assignment whose task includes the exact invocation "
                f"`{invocation}`"
            )
    return "\n".join(lines) + "\n"


def worker_skill_policy_constraint(
    decision: RoutePolicyDecision | None,
    assignment: Assignment | None = None,
) -> str:
    if decision is None or not decision.policy_applied or assignment is None:
        return ""
    selected = set(assignment.selected_skill_ids)
    if not selected:
        return ""
    lines: list[str] = []
    for item in decision.assignments:
        if item.artifact_id not in selected:
            continue
        if item.route_authority == "scoped_write" and item.scope_globs:
            globs = ", ".join(item.scope_globs)
            lines.append(
                "Scoped-write constraint: edits are limited to verifier-manifest globs "
                f"[{globs}] from `{item.manifest_path}`."
            )
        elif item.route_authority == "shadow":
            invocation = shadow_verify_invocation(item.verify_manifest_id or "")
            lines.append(
                "Shadow skill constraint: read-only fixture evaluation only via "
                f"`{invocation}`; do not perform production edits."
            )
    if not lines:
        return ""
    return "\n" + "\n".join(lines)
