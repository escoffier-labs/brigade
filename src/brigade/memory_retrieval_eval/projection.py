"""Projection eval fixtures and report section for #845 V1.

V1 owns checked-in scenarios, schema coverage, nullable external-contract
fields (#495 explanation, #498 redaction), scope annotations, cost metrics, and
diffable failure artifacts. Engine/facade adapter wiring lands in V2 (#843/#844).
"""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .corpus import CardDoc, repo_root

PROJECTION_SCHEMA = "memory-projection-eval.v1"
PROJECTION_ISSUE = 845
SCOPE_DIMENSIONS = ("repository", "task", "operator", "branch", "worktree")
SCAN_COUNT_FIELDS = ("created", "updated", "unchanged", "removed", "skipped", "failed")
CATEGORIES = (
    "identity_drift",
    "stale_partial_scan",
    "superseded_leakage",
    "duplicate_live",
    "scope_leakage",
    "provenance_completeness",
    "instruction_like_trusted_path",
    "cost",
)
IDENTITY_OPERATIONS = ("create", "edit", "rename", "move", "removal")

EXPLANATION_495_REASON = "selection explanation contract owned by #495"
REDACTION_498_REASON = "canonical Markdown projection does not apply origin-scoped redaction (#498)"


@dataclass(frozen=True)
class ProjectionScenario:
    scenario_id: str
    category: str
    scope_annotation: dict[str, str | None]
    payload: dict[str, Any]


def default_projection_root() -> Path:
    return repo_root() / "evals" / "memory-retrieval" / "projection"


def load_manifest(projection_root: Path | None = None) -> dict[str, Any]:
    root = (projection_root or default_projection_root()).resolve()
    path = root / "manifest.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != PROJECTION_SCHEMA:
        raise ValueError(f"projection manifest schema must be {PROJECTION_SCHEMA!r}")
    return payload


def load_scenarios(projection_root: Path | None = None) -> list[ProjectionScenario]:
    root = (projection_root or default_projection_root()).resolve()
    path = root / "scenarios.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    raw = payload.get("scenarios")
    if not isinstance(raw, list) or not raw:
        raise ValueError("projection scenarios file must contain a non-empty scenarios list")
    scenarios: list[ProjectionScenario] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each projection scenario must be an object")
        scenario_id = str(item.get("id") or "")
        category = str(item.get("category") or "")
        if not scenario_id:
            raise ValueError("each projection scenario must include id")
        if category not in CATEGORIES:
            raise ValueError(f"scenario {scenario_id!r} has unknown category {category!r}")
        scope = item.get("scope_annotation")
        problems = validate_scope_annotation(scope, scenario_id=scenario_id)
        if problems:
            raise ValueError("; ".join(problems))
        assert isinstance(scope, dict)
        scenarios.append(
            ProjectionScenario(
                scenario_id=scenario_id,
                category=category,
                scope_annotation={key: scope.get(key) for key in SCOPE_DIMENSIONS},
                payload=item,
            )
        )
    return scenarios


def validate_scope_annotation(
    scope: Any,
    *,
    scenario_id: str = "<unknown>",
) -> list[str]:
    """Return problems when scope annotation violates the eval-only contract."""
    problems: list[str] = []
    if not isinstance(scope, dict):
        return [f"{scenario_id}: scope_annotation must be an object"]
    extra = sorted(set(scope) - set(SCOPE_DIMENSIONS))
    missing = [dim for dim in SCOPE_DIMENSIONS if dim not in scope]
    if extra:
        problems.append(f"{scenario_id}: scope_annotation has unexpected keys: {', '.join(extra)}")
    if missing:
        problems.append(f"{scenario_id}: scope_annotation missing keys: {', '.join(missing)}")
    for dim in SCOPE_DIMENSIONS:
        value = scope.get(dim)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            problems.append(f"{scenario_id}: scope {dim!r} must be null or a non-empty opaque string")
    return problems


def external_contract_fields() -> dict[str, Any]:
    return {
        "explanation_495": {
            "available": False,
            "reason": EXPLANATION_495_REASON,
            "selection": None,
            "candidate_ids": None,
            "retrieval_arm": None,
            "per_arm_rank": None,
            "fusion_rule_id": None,
            "final_rank": None,
            "named_scores": None,
            "trust_provenance_labels": None,
            "unavailable_arms": None,
            "omitted_candidates": None,
        },
        "redaction_498": {
            "available": False,
            "status": "not_applicable",
            "reason": REDACTION_498_REASON,
            "policy_version": None,
            "scanner_present": False,
        },
    }


def _scan_block(scenario: ProjectionScenario) -> dict[str, Any]:
    scan = dict(scenario.payload.get("scan") or {})
    counts = {field: int(scan.get(field, 0)) for field in SCAN_COUNT_FIELDS}
    return {
        "status": str(scan.get("status") or "unknown"),
        **counts,
        "stale": bool(scan.get("stale", False)),
        "partial": bool(scan.get("partial", False)),
    }


def _health_block(scenario: ProjectionScenario) -> dict[str, Any]:
    scan = scenario.payload.get("scan") or {}
    projection = scenario.payload.get("projection") or {}
    rows = projection.get("rows")
    if isinstance(rows, list):
        live_count = sum(1 for row in rows if isinstance(row, dict) and row.get("live"))
    else:
        live_count = 1 if projection.get("live") else 0
    canonical = scenario.payload.get("canonical") or {}
    canonical_count = 1 if canonical else 0
    return {
        "stale": bool(scan.get("stale", False)),
        "partial": bool(scan.get("partial", False)),
        "canonical_count": canonical_count,
        "live_count": live_count,
        "hash_divergence": _hash_divergence(scenario),
        "status": str(scan.get("status") or "unknown"),
    }


def _hash_divergence(scenario: ProjectionScenario) -> bool:
    canonical = scenario.payload.get("canonical") or {}
    projection = scenario.payload.get("projection") or {}
    if not canonical or not projection:
        return False
    c_hash = canonical.get("content_hash")
    p_hash = projection.get("content_hash")
    if isinstance(c_hash, str) and isinstance(p_hash, str):
        return c_hash != p_hash
    return False


def _ranked_for_scenario(
    scenario: ProjectionScenario,
    *,
    search: Callable[[str, int], list[tuple[str, float]]] | None,
) -> list[str]:
    query = scenario.payload.get("query")
    if search is None or not isinstance(query, str) or not query.strip():
        projection = scenario.payload.get("projection") or {}
        rows = projection.get("rows")
        if isinstance(rows, list):
            return [str(row.get("external_id")) for row in rows if isinstance(row, dict) and row.get("external_id")]
        external_id = projection.get("external_id")
        return [str(external_id)] if external_id else []
    ranked_pairs = search(query, 10)
    return [card_id for card_id, _score in ranked_pairs]


def failure_artifact(
    scenario: ProjectionScenario,
    *,
    violation: str,
    search: Callable[[str, int], list[tuple[str, float]]] | None = None,
) -> dict[str, Any]:
    return {
        "scenario_id": scenario.scenario_id,
        "category": scenario.category,
        "expected_category": scenario.category,
        "scope_annotation": dict(scenario.scope_annotation),
        "scan": _scan_block(scenario),
        "health": _health_block(scenario),
        "ranked": _ranked_for_scenario(scenario, search=search),
        "violation": violation,
    }


def _evaluate_identity_drift(scenario: ProjectionScenario) -> str | None:
    operation = str(scenario.payload.get("operation") or "")
    if operation not in IDENTITY_OPERATIONS:
        return f"unknown identity operation {operation!r}"
    expected = scenario.payload.get("expected") or {}
    projection = scenario.payload.get("projection") or {}
    if expected.get("tombstoned") and projection.get("live"):
        return "removed card still live in projection"
    if expected.get("hash_divergence") and not _hash_divergence(scenario):
        return "expected canonical/projection hash divergence missing"
    if expected.get("hash_divergence") is False and _hash_divergence(scenario):
        return "unexpected hash divergence"
    if expected.get("identity_changed") and not projection.get("prior_external_id"):
        return "rename scenario missing prior_external_id"
    if expected.get("path_changed") and not projection.get("prior_path"):
        return "move scenario missing prior_path"
    live_expected = expected.get("live_count")
    if live_expected is not None:
        health = _health_block(scenario)
        if health["live_count"] != int(live_expected):
            return f"live_count expected {live_expected}, got {health['live_count']}"
    return None


def _evaluate_stale_partial(scenario: ProjectionScenario) -> str | None:
    expected = scenario.payload.get("expected") or {}
    scan = scenario.payload.get("scan") or {}
    if expected.get("six_counts_present"):
        for field in SCAN_COUNT_FIELDS:
            if field not in scan:
                return f"scan missing count field {field!r}"
    if expected.get("health_stale") and not scan.get("stale"):
        return "expected stale scan health"
    if expected.get("stale") and not scan.get("stale"):
        return "expected stale snapshot"
    if expected.get("partial") and not scan.get("partial"):
        return "expected partial scan"
    if expected.get("marked_partial") and not scenario.payload.get("projection", {}).get("partial"):
        return "projection not marked partial"
    if expected.get("visible_in_default_results") is False:
        if scenario.payload.get("projection", {}).get("from_stale_snapshot") and scan.get("stale") is not True:
            return "stale snapshot card should not be default-visible"
    return None


def _evaluate_superseded(scenario: ProjectionScenario) -> str | None:
    expected = scenario.payload.get("expected") or {}
    projection = scenario.payload.get("projection") or {}
    rows = projection.get("rows")
    if not isinstance(rows, list) or len(rows) < 2:
        return "superseded scenario requires at least two projection rows"
    if expected.get("superseded_must_not_outrank_current"):
        superseded = next((r for r in rows if r.get("superseded")), None)
        current = next((r for r in rows if not r.get("superseded")), None)
        if not superseded or not current:
            return "superseded/current row pair missing"
    if expected.get("harmful_recall_without_signal"):
        if not any(r.get("contradicts") for r in rows if isinstance(r, dict)):
            return "contradiction row missing"
    return None


def _evaluate_duplicate(scenario: ProjectionScenario) -> str | None:
    expected = scenario.payload.get("expected") or {}
    projection = scenario.payload.get("projection") or {}
    rows = projection.get("rows")
    if not isinstance(rows, list):
        return "duplicate scenario requires projection.rows"
    live_by_id: dict[str, int] = {}
    hashes: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        ext_id = str(row.get("external_id") or "")
        if row.get("live"):
            live_by_id[ext_id] = live_by_id.get(ext_id, 0) + 1
        content_hash = row.get("content_hash")
        if isinstance(content_hash, str):
            hashes.add(content_hash)
    if expected.get("one_live_per_external_id") is False:
        if not any(count > 1 for count in live_by_id.values()):
            return "expected duplicate live rows"
    if expected.get("one_live_per_external_id") is True:
        if any(count > 1 for count in live_by_id.values()):
            return "more than one live row per external_id"
    if expected.get("historical_hash_retained") and len(hashes) < 2:
        return "expected multiple historical content hashes"
    return None


def _evaluate_scope(scenario: ProjectionScenario) -> str | None:
    expected = scenario.payload.get("expected") or {}
    foreign = scenario.payload.get("foreign_scope_annotation")
    if expected.get("must_not_leak_to_foreign_scope"):
        if not isinstance(foreign, dict):
            return "cross-namespace scenario missing foreign_scope_annotation"
        problems = validate_scope_annotation(foreign, scenario_id=scenario.scenario_id)
        if problems:
            return problems[0]
        for dim in SCOPE_DIMENSIONS:
            home = scenario.scope_annotation.get(dim)
            away = foreign.get(dim)
            if home and away and home == away:
                return f"scope dimension {dim!r} must differ between namespaces"
    if expected.get("report_level_failure"):
        dimension = scenario.payload.get("dimension")
        if dimension not in SCOPE_DIMENSIONS:
            return "scope boundary scenario missing dimension"
    return None


def _evaluate_provenance(scenario: ProjectionScenario) -> str | None:
    expected = scenario.payload.get("expected") or {}
    projection = scenario.payload.get("projection") or {}
    if expected.get("source_identity_required"):
        if not projection.get("source_kind") or not projection.get("source_path"):
            return "source identity incomplete"
    if expected.get("trust_label_required"):
        trust = projection.get("trust") or {}
        if not trust.get("label"):
            return "trust label missing"
    if expected.get("injection_state_required"):
        injection = (projection.get("trust") or {}).get("injection") or {}
        if not injection.get("status"):
            return "injection status missing"
    if expected.get("scan_id_required") and not projection.get("scan_id"):
        return "scan_id missing"
    if expected.get("declared_relation_receipt_required"):
        relations = projection.get("relations")
        if not isinstance(relations, list) or not relations:
            return "declared relations missing"
        if not any(isinstance(rel, dict) and rel.get("receipt") for rel in relations):
            return "relation receipt missing"
    return None


def _trusted_path_exclusions() -> set[str]:
    return {"proj-card-quarantined", "proj-card-injection-pending"}


def evaluate_scenario(
    scenario: ProjectionScenario,
    *,
    cost_observed: Mapping[str, Any] | None = None,
) -> str | None:
    """Return a violation when a checked-in fixture is internally inconsistent."""
    category = scenario.category
    if category == "identity_drift":
        return _evaluate_identity_drift(scenario)
    if category == "stale_partial_scan":
        return _evaluate_stale_partial(scenario)
    if category == "superseded_leakage":
        return _evaluate_superseded(scenario)
    if category == "duplicate_live":
        return _evaluate_duplicate(scenario)
    if category == "scope_leakage":
        return _evaluate_scope(scenario)
    if category == "provenance_completeness":
        return _evaluate_provenance(scenario)
    if category == "instruction_like_trusted_path":
        return _evaluate_instruction_fixture(scenario)
    if category == "cost":
        return _evaluate_cost(scenario.payload.get("expected") or {}, cost_observed or {})
    return f"unknown category {category!r}"


def _evaluate_instruction_fixture(scenario: ProjectionScenario) -> str | None:
    expected = scenario.payload.get("expected") or {}
    card_id = str(scenario.payload.get("card_id") or scenario.payload.get("projection", {}).get("external_id") or "")
    if expected.get("excluded_from_trusted_brief"):
        if card_id not in _trusted_path_exclusions():
            return f"card {card_id!r} not in trusted-path exclusion set"
    return None


def adapter_projection_violation(
    scenario: ProjectionScenario,
    *,
    search: Callable[[str, int], list[tuple[str, float]]],
) -> str | None:
    """Return a violation when a lexical adapter leaks projection-sensitive content."""
    if scenario.category != "instruction_like_trusted_path":
        return None
    expected = scenario.payload.get("expected") or {}
    card_id = str(scenario.payload.get("card_id") or scenario.payload.get("projection", {}).get("external_id") or "")
    if not expected.get("excluded_from_trusted_brief"):
        return None
    query = str(scenario.payload.get("query") or "")
    ranked = [cid for cid, _ in search(query, 20)]
    if card_id in ranked:
        return f"quarantined/injection-pending card {card_id!r} appeared in trusted-path search"
    return None


def _evaluate_cost(expected: Mapping[str, Any], observed: Mapping[str, Any]) -> str | None:
    for key in ("index_build_time_ms", "query_p50_ms", "query_p95_ms", "index_size_bytes"):
        min_key = f"{key}_min"
        if min_key in expected and observed.get(key, -1) < int(expected[min_key]):
            return f"cost field {key!r} below minimum"
    return None


def validate_all_fixtures(scenarios: Sequence[ProjectionScenario]) -> list[str]:
    """Validate checked-in projection fixtures; return human-readable problems."""
    problems: list[str] = []
    for scenario in scenarios:
        if scenario.category == "cost":
            continue
        violation = evaluate_scenario(scenario)
        if violation:
            problems.append(f"{scenario.scenario_id}: {violation}")
    return problems


def measure_cost(
    *,
    cards: Sequence[CardDoc],
    queries: Sequence[str],
    search: Callable[[str, int], list[tuple[str, float]]],
) -> dict[str, Any]:
    start = time.perf_counter()
    corpus_bytes = sum(len(card.searchable_text.encode("utf-8")) for card in cards)
    _ = list(cards)
    index_build_time_ms = (time.perf_counter() - start) * 1000.0

    latencies: list[float] = []
    for query in queries:
        t0 = time.perf_counter()
        _ = search(query, 5)
        latencies.append((time.perf_counter() - t0) * 1000.0)
    if latencies:
        ordered = sorted(latencies)
        p50 = statistics.median(ordered)
        idx = max(0, min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1)))))
        p95 = ordered[idx]
    else:
        p50 = 0.0
        p95 = 0.0
    return {
        "index_build_time_ms": round(index_build_time_ms, 3),
        "query_p50_ms": round(p50, 3),
        "query_p95_ms": round(p95, 3),
        "index_size_bytes": corpus_bytes,
        "query_samples": len(latencies),
    }


def scope_enforcement_report(*, adapter: str) -> dict[str, Any]:
    """Report-level failures for adapters without a production scope contract."""
    failures = [
        {
            "adapter": adapter,
            "dimension": dim,
            "reason": f"{adapter} adapter has no production scope contract for {dim}",
        }
        for dim in SCOPE_DIMENSIONS
    ]
    return {
        "available": False,
        "reason": "production scope filter not implemented for lexical adapters",
        "report_level_failures": failures,
    }


def category_coverage(scenarios: Sequence[ProjectionScenario]) -> dict[str, int]:
    counts = {category: 0 for category in CATEGORIES}
    for scenario in scenarios:
        counts[scenario.category] = counts.get(scenario.category, 0) + 1
    return counts


def build_projection_section(
    *,
    projection_root: Path | None = None,
    cards: Sequence[CardDoc] | None = None,
    query_texts: Sequence[str] | None = None,
    search: Callable[[str, int], list[tuple[str, float]]] | None = None,
    adapter: str = "fixture-reference",
) -> dict[str, Any]:
    """Build the versioned projection section for the #722 report envelope."""
    root = (projection_root or default_projection_root()).resolve()
    manifest = load_manifest(root)
    scenarios = load_scenarios(root)
    fixture_problems = validate_all_fixtures(scenarios)
    if fixture_problems:
        raise ValueError("projection fixture validation failed:\n- " + "\n- ".join(fixture_problems))

    cost_observed: dict[str, Any] = {}
    if cards is not None and search is not None and query_texts is not None:
        cost_observed = measure_cost(cards=cards, queries=query_texts, search=search)
        cost_violation = evaluate_scenario(
            next(s for s in scenarios if s.category == "cost"),
            cost_observed=cost_observed,
        )
        if cost_violation:
            raise ValueError(f"projection cost scenario failed: {cost_violation}")

    failures: list[dict[str, Any]] = []
    passes: list[str] = []
    by_category: dict[str, dict[str, Any]] = {}

    for scenario in scenarios:
        bucket = by_category.setdefault(
            scenario.category,
            {"scenario_count": 0, "passed": 0, "failed": 0, "failures": []},
        )
        bucket["scenario_count"] += 1
        violation = None
        if search is not None:
            violation = adapter_projection_violation(scenario, search=search)
        if violation:
            bucket["failed"] += 1
            artifact = failure_artifact(scenario, violation=violation, search=search)
            bucket["failures"].append(artifact)
            failures.append(artifact)
        else:
            bucket["passed"] += 1
            passes.append(scenario.scenario_id)

    report_level_failures = scope_enforcement_report(adapter=adapter)["report_level_failures"]

    return {
        "schema": PROJECTION_SCHEMA,
        "issue": PROJECTION_ISSUE,
        "manifest": {
            "schema": manifest.get("schema"),
            "categories": list(manifest.get("categories") or []),
            "scope_dimensions": list(manifest.get("scope_dimensions") or []),
        },
        "coverage": category_coverage(scenarios),
        "scope_enforcement": scope_enforcement_report(adapter=adapter),
        "external_contracts": external_contract_fields(),
        "cost": cost_observed,
        "by_category": by_category,
        "summary": {
            "scenario_count": len(scenarios),
            "passed": len(passes),
            "failed": len(failures),
            "report_level_failure_count": len(report_level_failures),
        },
        "failures": failures,
        "report_level_failures": report_level_failures,
    }


def projection_fixture_card_ids(projection_root: Path | None = None) -> list[str]:
    root = (projection_root or default_projection_root()).resolve()
    cards_dir = root / "cards"
    if not cards_dir.is_dir():
        return []
    return sorted(path.stem for path in cards_dir.glob("*.md"))


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
