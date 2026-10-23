"""Operation readiness protects evidence boundaries, not configured seat health alone."""

from dataclasses import replace

import pytest

from brigade import roster, roster_readiness as readiness, seat_health

NOW = 1000.0


@pytest.fixture
def declared():
    return roster.Roster(
        orchestrator="chef",
        agents={
            "chef": roster.Agent("chef", "codex", "plan"),
            "worker": roster.Agent("worker", "codex", "implement", model="example-model"),
            "unused": roster.Agent("unused", "codex", "research"),
        },
        max_workers=2,
    )


def health_for(declared, name="worker", *, status="passed", at=NOW):
    agent = declared.agents[name]
    return seat_health.SeatHealthResult(
        "probe-example",
        name,
        seat_health.seat_fingerprint(agent, declared),
        "healthy" if status == "passed" else "unhealthy",
        {},
        tuple(seat_health.SeatHealthCheck(check, status) for check in seat_health.CHECK_NAMES),
        0,
        0,
        at,
        at,
    )


def observed(declared, request, tmp_path, **changes):
    scope = readiness.operation_scope(declared, request, tmp_path)
    evidence = {
        category: readiness.Observation("passed", NOW, scope, capacity=2 if category == "capacity" else None)
        for category in ("route-support", "isolation", "admission", "capacity")
    }
    evidence.update(changes)
    return evidence


def evaluate(declared, request, tmp_path, *, health=None, observations=None):
    return readiness.evaluate_readiness(
        declared,
        request,
        workspace=tmp_path,
        now=NOW,
        health=health if health is not None else {"worker": health_for(declared)},
        observations=observations or {},
    )


@pytest.mark.parametrize("workers,reason", [(("chef",), "orchestrator-selected"), (("missing",), "worker-missing")])
def test_rejects_non_worker_selection(declared, tmp_path, workers, reason):
    if workers == ("chef",):
        declared = replace(declared, agents={"chef": declared.agents["chef"]}, max_workers=1)
    result = evaluate(declared, readiness.ReadinessRequest(workers), tmp_path, health={})
    assert result["status"] == "blocked"
    assert result["exit_code"] == 2
    assert any(item["reason"] == reason for item in result["evidence"])
    if workers == ("chef",):
        assert result["structural_worker_count"] == 0
        assert result["configured_limit"] == 1


def test_valid_worker_needs_fresh_admission_and_capacity(declared, tmp_path):
    result = evaluate(declared, readiness.ReadinessRequest(("worker",)), tmp_path)
    assert result["status"] == "unverified"
    assert result["exit_code"] == 3
    assert result["admitted_capacity"] is None
    assert {item["category"] for item in result["evidence"]} >= {"admission", "capacity", "model-reachability"}


def test_complete_scoped_observations_are_ready_and_unused_failure_is_ignored(declared, tmp_path):
    request = readiness.ReadinessRequest(("worker",))
    result = evaluate(
        declared,
        request,
        tmp_path,
        health={"worker": health_for(declared), "unused": health_for(declared, "unused", status="failed")},
        observations=observed(declared, request, tmp_path),
    )
    assert result["status"] == "ready"
    assert result["exit_code"] == 0
    assert result["usable_worker_count"] == 1
    assert result["configured_limit"] == 2
    assert result["admitted_capacity"] == 2
    assert not any(item.get("seat") == "unused" for item in result["evidence"])


@pytest.mark.parametrize("category", ["admission", "capacity", "route-support", "isolation"])
@pytest.mark.parametrize("change", [{"observed_at": 900}, {"scope": "other-operation"}, {"observed_at": 1001}])
def test_stale_future_or_wrong_scope_observations_cannot_establish_ready(declared, tmp_path, category, change):
    request = readiness.ReadinessRequest(("worker",), route="t3")
    evidence = observed(declared, request, tmp_path)
    evidence[category] = replace(evidence[category], **change)
    result = evaluate(declared, request, tmp_path, observations=evidence)
    assert result["status"] == "unverified"
    assert any(item["category"] == category and item["status"] == "unknown" for item in result["evidence"])


@pytest.mark.parametrize("status", ["unsupported", "denied"])
def test_t3_support_or_policy_refusal_never_falls_back(declared, tmp_path, status):
    request = readiness.ReadinessRequest(("worker",), route="t3")
    evidence = observed(declared, request, tmp_path)
    evidence["route-support"] = replace(evidence["route-support"], status=status)
    result = evaluate(declared, request, tmp_path, observations=evidence)
    assert result["status"] == "blocked"
    assert result["request"]["route"] == "t3"
    assert result["fallback"]["automatic"] is False
    assert result["fallback"]["admitted"] is False
    assert result["limiting_reason"] == f"route-support-{status}"
    assert any(item["status"] == status for item in result["evidence"])


def test_missing_t3_contract_stays_unverified(declared, tmp_path):
    result = evaluate(declared, readiness.ReadinessRequest(("worker",), route="t3"), tmp_path)
    assert result["status"] == "unverified"
    support = next(item for item in result["evidence"] if item["category"] == "route-support")
    assert support["reason"] == "t3-contract-unavailable"
    assert support["action"]


def test_observed_capacity_constrains_batch(declared, tmp_path):
    request = readiness.ReadinessRequest(("worker", "unused"), concurrency=2)
    evidence = observed(declared, request, tmp_path)
    evidence["capacity"] = replace(evidence["capacity"], capacity=1)
    result = evaluate(declared, request, tmp_path, observations=evidence)
    assert result["status"] == "blocked"
    assert result["admitted_capacity"] == 1
    assert any(item["reason"] == "insufficient-capacity" for item in result["evidence"])


def test_stale_health_failure_is_unknown(declared, tmp_path):
    request = readiness.ReadinessRequest(("worker",))
    result = evaluate(declared, request, tmp_path, health={"worker": health_for(declared, status="failed", at=900)})
    assert result["status"] == "unverified"
    assert any(item["freshness"] == "stale" for item in result["evidence"])


@pytest.mark.parametrize(
    "scenario",
    [
        readiness.ReadinessRequest(("worker",), concurrency=0),
        readiness.ReadinessRequest(("worker", "worker")),
        readiness.ReadinessRequest(("worker",), concurrency=2),
        readiness.ReadinessRequest(("worker",), capability="bad capability"),
        readiness.ReadinessRequest(("worker",), route="invalid"),
    ],
)
def test_malformed_operation_is_blocked(declared, tmp_path, scenario):
    assert evaluate(declared, scenario, tmp_path)["exit_code"] == 2


def test_capability_and_shared_acpx_incompatibility_are_blocked(declared, tmp_path):
    agent = replace(declared.agents["worker"], cli="cursor", transport="acpx", transport_version="0.12.0")
    declared = replace(declared, agents={**declared.agents, "worker": agent})
    result = evaluate(declared, readiness.ReadinessRequest(("worker",), capability="research.plan"), tmp_path)
    assert result["status"] == "blocked"
    assert {item["reason"] for item in result["evidence"]} >= {"capability-unsupported", "isolation-incompatible"}


def test_cached_health_keeps_original_age_and_categories_independent(declared, tmp_path):
    request = readiness.ReadinessRequest(("worker",))
    row = replace(health_for(declared), cached=True, cache_age_seconds=400)
    result = evaluate(declared, request, tmp_path, health={"worker": row})
    assert result["status"] == "unverified"
    assert next(item for item in result["evidence"] if item["category"] == "executable-identity")["age_seconds"] == 400
    row = replace(
        health_for(declared),
        checks=(
            seat_health.SeatHealthCheck("executable-identity", "passed"),
            seat_health.SeatHealthCheck("authentication-entitlement", "degraded"),
            seat_health.SeatHealthCheck("transport-liveness", "failed", cause_code="missing-transport"),
        ),
    )
    result = evaluate(declared, request, tmp_path, health={"worker": row})
    assert result["status"] == "blocked"
    categories = {item["category"]: item["status"] for item in result["evidence"]}
    assert categories["executable-identity"] == "passed"
    assert categories["authentication-entitlement"] == "unknown"
    assert categories["model-reachability"] == "unknown"
    assert categories["transport-liveness"] == "failed"


def test_nonfinite_observation_is_unknown_and_json_serializable(declared, tmp_path):
    import json

    request = readiness.ReadinessRequest(("worker",))
    evidence = observed(declared, request, tmp_path)
    evidence["admission"] = replace(evidence["admission"], observed_at=float("nan"))
    result = evaluate(declared, request, tmp_path, observations=evidence)
    assert result["status"] == "unverified"
    json.dumps(result, allow_nan=False)
