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
    request = readiness.ReadinessRequest(("worker",), concurrency=2)
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


@pytest.mark.parametrize("workers", [("worker",), ("worker", "worker")])
def test_one_seat_pool_can_supply_multiple_parallel_assignments(declared, tmp_path, workers):
    declared = replace(declared, max_workers=3)
    request = readiness.ReadinessRequest(workers, concurrency=3)
    evidence = observed(declared, request, tmp_path)
    evidence["capacity"] = replace(evidence["capacity"], capacity=3)
    result = evaluate(declared, request, tmp_path, observations=evidence)
    assert result["status"] == "ready"
    assert result["request"]["workers"] == ("worker",)
    assert len(result["selected_seats"]) == result["usable_worker_count"] == 1
    assert result["scope"] == readiness.operation_scope(declared, replace(request, workers=("worker",)), tmp_path)


def test_configured_limit_still_constrains_one_seat_pool(declared, tmp_path):
    request = readiness.ReadinessRequest(("worker",), concurrency=3)
    evidence = observed(declared, request, tmp_path)
    evidence["capacity"] = replace(evidence["capacity"], capacity=3)
    result = evaluate(declared, request, tmp_path, observations=evidence)
    assert result["status"] == "blocked"
    assert result["limiting_reason"] == "configured-capacity"


@pytest.mark.parametrize(
    "changes",
    [
        {"concurrency": None},
        {"concurrency": True},
        {"concurrency": "2"},
        {"concurrency": 0},
        {"capability": 1},
        {"capability": []},
        {"workers": None},
        {"workers": "worker"},
        {"workers": (None,)},
        {"workers": (1,)},
        {"workers": ([],)},
        {"workers": (" ",)},
        {"route": []},
        {"isolation": {}},
    ],
)
def test_malformed_request_stops_before_scope_fingerprints_or_probes(declared, tmp_path, monkeypatch, changes):
    import json

    request = replace(readiness.ReadinessRequest(("worker",)), **changes)

    def forbidden(*args, **kwargs):
        raise AssertionError("malformed request reached scope, fingerprint, or provider probe")

    monkeypatch.setattr(readiness, "operation_scope", forbidden)
    monkeypatch.setattr(seat_health, "seat_fingerprint", forbidden)
    monkeypatch.setattr(seat_health, "SeatHealthProbe", forbidden)
    result = readiness.inspect_readiness(declared, request, workspace=tmp_path)
    assert result["status"] == "blocked"
    assert result["limiting_reason"] == "malformed-request"
    assert [item["reason"] for item in result["evidence"]] == ["malformed-request"]
    assert result["scope"] is None
    json.dumps(result, allow_nan=False)
    assert list(tmp_path.iterdir()) == []


def test_cached_health_timestamp_matches_retained_age(declared, tmp_path):
    request = readiness.ReadinessRequest(("worker",))
    row = replace(health_for(declared), cached=True, cache_age_seconds=40)
    result = evaluate(
        declared, request, tmp_path, health={"worker": row}, observations=observed(declared, request, tmp_path)
    )
    assert result["status"] == "ready"
    health_rows = [item for item in result["evidence"] if item.get("seat") == "worker"]
    assert all(item["observed_at"] == 960 for item in health_rows)
    assert all(item["age_seconds"] == NOW - item["observed_at"] for item in health_rows)


@pytest.mark.parametrize(
    "finished,cache_age",
    [
        (1001, 40),
        (1000, -1),
        (1000, float("nan")),
        (1000, float("inf")),
        (float("inf"), 0),
        (float("nan"), 0),
    ],
)
def test_invalid_cached_health_times_never_establish_ready(declared, tmp_path, finished, cache_age):
    import json

    request = readiness.ReadinessRequest(("worker",))
    row = replace(health_for(declared), finished_wall_at=finished, cached=True, cache_age_seconds=cache_age)
    result = evaluate(
        declared, request, tmp_path, health={"worker": row}, observations=observed(declared, request, tmp_path)
    )
    assert result["status"] == "unverified"
    assert all(item["status"] == "unknown" for item in result["evidence"] if item.get("seat") == "worker")
    json.dumps(result, allow_nan=False)


@pytest.mark.parametrize(
    "changes",
    [
        {"observed_at": "1000"},
        {"observed_at": None},
        {"observed_at": True},
        {"observed_at": 10**400},
        {"status": ["passed"]},
        {"scope": {}},
        {"detail": None},
        None,
    ],
)
def test_malformed_observation_is_unknown_without_hiding_refusal(declared, tmp_path, changes):
    import json

    request = readiness.ReadinessRequest(("worker",))
    evidence = observed(declared, request, tmp_path)
    evidence["admission"] = replace(evidence["admission"], **changes) if changes is not None else {}
    result = evaluate(declared, request, tmp_path, observations=evidence)
    row = next(item for item in result["evidence"] if item["category"] == "admission")
    assert result["status"] == "unverified"
    assert (row["status"], row["reason"]) == ("unknown", "observation-malformed")
    json.dumps(result, allow_nan=False)

    evidence["route-support"] = replace(evidence["route-support"], status="denied")
    result = evaluate(declared, request, tmp_path, observations=evidence)
    assert result["status"] == "blocked"
    assert result["limiting_reason"] == "route-support-denied"


@pytest.mark.parametrize(
    "changes",
    [
        {"finished_wall_at": None},
        {"finished_wall_at": "1000"},
        {"finished_wall_at": True},
        {"finished_wall_at": 10**400},
        {"cache_age_seconds": "0"},
        {"cache_age_seconds": False},
        {"status": []},
        {"seat": []},
        {"fingerprint": {}},
        {"checks": None},
        None,
    ],
)
def test_malformed_health_is_unobserved_without_hiding_refusal(declared, tmp_path, changes):
    import json

    request = readiness.ReadinessRequest(("worker",))
    health = replace(health_for(declared), **changes) if changes is not None else {}
    evidence = observed(declared, request, tmp_path)
    result = evaluate(declared, request, tmp_path, health={"worker": health}, observations=evidence)
    rows = [item for item in result["evidence"] if item.get("seat") == "worker"]
    assert result["status"] == "unverified"
    assert all((item["status"], item["reason"]) == ("unknown", "health-unobserved") for item in rows)
    json.dumps(result, allow_nan=False)

    evidence["admission"] = replace(evidence["admission"], status="denied")
    result = evaluate(declared, request, tmp_path, health={"worker": health}, observations=evidence)
    assert result["status"] == "blocked"
    assert result["limiting_reason"] == "admission-denied"


@pytest.mark.parametrize("changes", [{"name": []}, {"status": []}, {"detail": None}, {"cause_code": []}, None])
def test_malformed_health_check_keeps_independent_health_failure(declared, tmp_path, changes):
    import json

    request = readiness.ReadinessRequest(("worker",))
    health = health_for(declared)
    malformed = replace(health.checks[0], **changes) if changes is not None else {}
    health = replace(health, checks=(malformed, *health.checks[1:]))
    evidence = observed(declared, request, tmp_path)
    result = evaluate(declared, request, tmp_path, health={"worker": health}, observations=evidence)
    row = next(item for item in result["evidence"] if item["category"] == "declaration")
    assert result["status"] == "unverified"
    assert (row["status"], row["reason"]) == ("unknown", "health-unobserved")
    json.dumps(result, allow_nan=False)

    health = replace(health, checks=(*health.checks[:-1], replace(health.checks[-1], status="failed")))
    result = evaluate(declared, request, tmp_path, health={"worker": health}, observations=evidence)
    assert result["status"] == "blocked"
    assert any(
        item["category"] == "isolation-compatibility" and item["status"] == "failed" for item in result["evidence"]
    )


@pytest.mark.parametrize("with_observation", [False, True])
def test_operation_isolation_reflects_known_seat_incompatibility(declared, tmp_path, with_observation):
    agent = replace(declared.agents["worker"], cli="cursor", transport="acpx", transport_version="0.12.0")
    declared = replace(declared, agents={**declared.agents, "worker": agent})
    request = readiness.ReadinessRequest(("worker",))
    evidence = observed(declared, request, tmp_path) if with_observation else {}
    result = evaluate(declared, request, tmp_path, observations=evidence)
    rows = [item for item in result["evidence"] if item["category"] == "isolation"]
    assert result["status"] == "blocked"
    assert any(item.get("seat") == "worker" and item["reason"] == "isolation-incompatible" for item in rows)
    operation = next(item for item in rows if "seat" not in item)
    assert (operation["status"], operation["reason"]) == ("failed", "isolation-incompatible")
    if with_observation:
        assert operation["observed_status"] == "passed"
        assert operation["observed_at"] == NOW


def test_pool_order_and_duplicates_share_scope_preserving_display_and_probe_order(declared, tmp_path):
    request = readiness.ReadinessRequest(("worker", "unused"), concurrency=2)
    evidence = observed(declared, request, tmp_path)
    reordered = replace(request, workers=("unused", "worker", "unused"))
    result = evaluate(
        declared,
        reordered,
        tmp_path,
        health={name: health_for(declared, name) for name in request.workers},
        observations=evidence,
    )
    assert result["scope"] == readiness.operation_scope(declared, request, tmp_path)
    assert result["status"] == "ready"
    assert result["request"]["workers"] == ("unused", "worker")
    assert result["request"]["concurrency"] == 2
    assert [seat["name"] for seat in result["selected_seats"]] == ["unused", "worker"]
    assert result["usable_worker_count"] == 2
    probed = []

    class RecordingProbe:
        def probe_roster(self, selected, **kwargs):
            probed.extend(selected.agents)
            return []

    inspected = readiness.inspect_readiness(declared, reordered, workspace=tmp_path, health_probe=RecordingProbe())
    assert probed == ["unused", "worker"]
    assert inspected["scope"] == result["scope"]
    assert [seat["name"] for seat in inspected["selected_seats"]] == probed


@pytest.mark.parametrize(
    "change",
    [
        "workers",
        "concurrency",
        "route",
        "isolation",
        "capability",
        "workspace",
        "orchestrator",
        "max_workers",
        "allow_models",
        "model",
        "capabilities",
        "role",
        "transport",
    ],
)
def test_scope_changes_invalidate_operation_evidence(declared, tmp_path, change):
    request = readiness.ReadinessRequest(("worker",))
    evidence = observed(declared, request, tmp_path)
    original_scope = readiness.operation_scope(declared, request, tmp_path)
    if change in {"workers", "concurrency", "route", "isolation", "capability"}:
        request = replace(
            request,
            **{
                change: {
                    "workers": ("worker", "unused"),
                    "concurrency": 2,
                    "route": "t3",
                    "isolation": "worktree",
                    "capability": "research.plan",
                }[change]
            },
        )
    elif change == "workspace":
        tmp_path = tmp_path / "other-workspace"
    elif change in {"orchestrator", "max_workers", "allow_models"}:
        declared = replace(
            declared, **{change: {"orchestrator": "unused", "max_workers": 3, "allow_models": ("codex",)}[change]}
        )
    else:
        agent = replace(
            declared.agents["worker"],
            **{
                change: {
                    "model": "other-model",
                    "capabilities": ("research.plan",),
                    "role": "research",
                    "transport": "acpx",
                }[change]
            },
        )
        declared = replace(declared, agents={**declared.agents, "worker": agent})
    result = evaluate(declared, request, tmp_path, observations=evidence)
    assert result["scope"] != original_scope
    assert result["status"] != "ready"
    assert all(
        item["status"] == "unknown" and item["reason"] == "observation-stale-or-scope-mismatch"
        for item in result["evidence"]
        if item["category"] in {"route-support", "admission", "capacity"}
    )
