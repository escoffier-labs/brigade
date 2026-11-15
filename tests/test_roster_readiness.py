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


def _hub_roster_payload(
    *,
    source: str = "hub",
    seats: list[dict] | None = None,
    fleet_policy: dict | None = None,
):
    payload = {
        "schema": "brigade.fleet_model_roster.v1",
        "source": source,
        "revision": 7,
        "document_sha256": "sha256:" + ("a" * 64),
        "expires_at": "2099-01-01T00:00:00Z",
        "seats": seats
        if seats is not None
        else [
            {
                "seat": "worker",
                "provider": "openai",
                "model": "example-model",
                "reasoning": "medium",
                "brigade_cli": "codex",
                "bindings": {"brigade": {"cli": "codex", "model": "example-model"}},
                "enabled": True,
                "limit": None,
            },
            {
                "seat": "unused",
                "provider": "openai",
                "model": "example-model",
                "reasoning": "medium",
                "brigade_cli": "codex",
                "bindings": {"brigade": {"cli": "codex", "model": "example-model"}},
                "enabled": False,
                "limit": 0,
            },
        ],
        "consumer_defaults": {},
        "retired_models": [],
    }
    if fleet_policy is not None:
        payload["fleet_policy"] = fleet_policy
    return payload


def _stub_standalone_env(monkeypatch, *, enrollment_observed: bool = False):
    from brigade import fleet_client, fleet_model_admission

    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: {"hub_url": "", "node_token": ""})
    monkeypatch.setattr(fleet_model_admission, "enrollment_activation_observed", lambda: enrollment_observed)


def _forbid_mutating_hub_calls(monkeypatch):
    from brigade import fleet_client, fleet_client_policy, fleet_model_admission

    def _forbidden(*args, **kwargs):
        raise AssertionError("read-only readiness inspection attempted a mutating Hub operation")

    monkeypatch.setattr(fleet_model_admission, "admit_model", _forbidden)
    monkeypatch.setattr(fleet_client, "acquire_model_lease", _forbidden)
    monkeypatch.setattr(fleet_client, "acquire_claim", _forbidden)
    monkeypatch.setattr(fleet_client_policy, "prepare_session", _forbidden)
    monkeypatch.setattr(fleet_client_policy, "acknowledge_session", _forbidden)


def test_brigade_standalone_inspect_readiness_reaches_ready_without_writes(declared, tmp_path, monkeypatch):
    _stub_standalone_env(monkeypatch)
    clean_ws = tmp_path / "clean"
    clean_ws.mkdir()

    request = readiness.ReadinessRequest(("worker",), concurrency=2)
    result = readiness.inspect_readiness(declared, request, workspace=clean_ws)

    assert result["status"] == "ready"
    assert result["exit_code"] == 0
    assert result["admitted_capacity"] == 2
    assert result["usable_worker_count"] == 1
    assert result["limiting_reason"] is None

    by_category = {item["category"]: item for item in result["evidence"] if "seat" not in item}
    assert by_category["admission"]["status"] == "passed"
    assert by_category["admission"]["freshness"] == "fresh"
    assert "local roster admissible seats" in by_category["admission"]["detail"]
    assert by_category["capacity"]["status"] == "passed"
    assert by_category["capacity"]["freshness"] == "fresh"
    assert "local run lock (absent)" in by_category["capacity"]["detail"]
    assert list(clean_ws.iterdir()) == []


@pytest.mark.parametrize(
    "lock_state,expected_status,expected_exit,expected_cap,expected_reason",
    [
        ("unreconciled", "ready", 0, 2, None),
        ("live", "blocked", 2, 0, "insufficient-capacity"),
        ("skipped", "blocked", 2, None, "capacity-failed"),
        ("invalid", "unverified", 3, None, "observation-incomplete"),
    ],
)
def test_brigade_standalone_reflects_local_run_lock_states(
    declared,
    tmp_path,
    monkeypatch,
    lock_state,
    expected_status,
    expected_exit,
    expected_cap,
    expected_reason,
):
    from brigade import runguard

    _stub_standalone_env(monkeypatch)
    monkeypatch.setattr(runguard, "inspect_run_lock_reconcile", lambda ws: lock_state)

    result = readiness.inspect_readiness(declared, readiness.ReadinessRequest(("worker",)), workspace=tmp_path)
    assert result["status"] == expected_status
    assert result["exit_code"] == expected_exit
    assert result["admitted_capacity"] == expected_cap
    assert result["limiting_reason"] == expected_reason
    capacity_row = next(item for item in result["evidence"] if item["category"] == "capacity")
    assert "local run lock" in capacity_row["detail"]


def test_brigade_standalone_blocks_retired_model_and_prior_enrollment(declared, tmp_path, monkeypatch):
    _stub_standalone_env(monkeypatch, enrollment_observed=True)
    result = readiness.inspect_readiness(declared, readiness.ReadinessRequest(("worker",)), workspace=tmp_path)
    assert result["status"] == "blocked"
    assert result["exit_code"] == 2
    assert result["limiting_reason"] == "admission-denied"

    _stub_standalone_env(monkeypatch, enrollment_observed=False)
    retired_worker = replace(declared.agents["worker"], model="gpt-5.4")
    retired_roster = replace(declared, agents={**declared.agents, "worker": retired_worker})
    result = readiness.inspect_readiness(retired_roster, readiness.ReadinessRequest(("worker",)), workspace=tmp_path)
    assert result["status"] == "blocked"
    assert result["exit_code"] == 2
    assert result["limiting_reason"] == "admission-denied"
    admission_row = next(item for item in result["evidence"] if item["category"] == "admission")
    assert "local roster admissible seats" in admission_row["detail"]


def test_brigade_hub_ready_uses_read_only_cache_write_false_and_inspect_claim(declared, tmp_path, monkeypatch):
    from brigade import fleet_client, fleet_model_admission

    _forbid_mutating_hub_calls(monkeypatch)
    seen_calls = []

    def fake_fetch(*, allow_lkg=True, hub_url=None, cache_write=True, inspect_only=False):
        seen_calls.append({"allow_lkg": allow_lkg, "hub_url": hub_url, "cache_write": cache_write})
        return fleet_model_admission.ModelAdmissionDecision(True, 0, "authoritative", _hub_roster_payload(source="hub"))

    monkeypatch.setattr(
        fleet_client, "load_fleet_settings", lambda: {"hub_url": "https://hub.example.test", "node_token": "tok"}
    )
    monkeypatch.setattr(fleet_model_admission, "fetch_versioned_roster", fake_fetch)
    monkeypatch.setattr(fleet_client, "resolve_claim_target", lambda ws: "repo:example/brigade")
    monkeypatch.setattr(
        fleet_client,
        "inspect_claim",
        lambda target: fleet_client.ClaimDecision(True, "free", None),
    )

    result = readiness.inspect_readiness(
        declared,
        readiness.ReadinessRequest(("worker",), concurrency=2),
        workspace=tmp_path,
        hub_discovery_possible=True,
    )

    assert seen_calls == [{"allow_lkg": True, "hub_url": "https://hub.example.test", "cache_write": False}]
    assert result["status"] == "ready"
    assert result["exit_code"] == 0
    assert result["admitted_capacity"] == 2
    admission_row = next(item for item in result["evidence"] if item["category"] == "admission")
    capacity_row = next(item for item in result["evidence"] if item["category"] == "capacity")
    assert "hub model admission" in admission_row["detail"]
    assert "hub repo claim" in capacity_row["detail"]


def test_brigade_hub_denied_seat_and_held_claim_and_zero_limit_block(declared, tmp_path, monkeypatch):
    from brigade import fleet_client, fleet_model_admission

    _forbid_mutating_hub_calls(monkeypatch)
    monkeypatch.setattr(
        fleet_client, "load_fleet_settings", lambda: {"hub_url": "https://hub.example.test", "node_token": "tok"}
    )
    monkeypatch.setattr(fleet_client, "resolve_claim_target", lambda ws: "repo:example/brigade")
    monkeypatch.setattr(fleet_client, "inspect_claim", lambda target: fleet_client.ClaimDecision(True, "free", None))

    # 1. Selected seat disabled on Hub -> blocked
    disabled_payload = _hub_roster_payload(
        seats=[
            {
                "seat": "worker",
                "provider": "openai",
                "model": "example-model",
                "reasoning": "medium",
                "brigade_cli": "codex",
                "bindings": {"brigade": {"cli": "codex", "model": "example-model"}},
                "enabled": False,
                "limit": None,
            }
        ]
    )
    monkeypatch.setattr(
        fleet_model_admission,
        "fetch_versioned_roster",
        lambda **kw: fleet_model_admission.ModelAdmissionDecision(True, 0, "authoritative", disabled_payload),
    )
    res_denied = readiness.inspect_readiness(declared, readiness.ReadinessRequest(("worker",)), workspace=tmp_path)
    assert res_denied["status"] == "blocked"
    assert res_denied["exit_code"] == 2
    assert res_denied["limiting_reason"] == "admission-denied"

    # 2. Selected seat enabled with limit=0 -> blocked (admitted_capacity == 0)
    zero_limit_payload = _hub_roster_payload(
        seats=[
            {
                "seat": "worker",
                "provider": "openai",
                "model": "example-model",
                "reasoning": "medium",
                "brigade_cli": "codex",
                "bindings": {"brigade": {"cli": "codex", "model": "example-model"}},
                "enabled": True,
                "limit": 0,
            }
        ]
    )
    monkeypatch.setattr(
        fleet_model_admission,
        "fetch_versioned_roster",
        lambda **kw: fleet_model_admission.ModelAdmissionDecision(True, 0, "authoritative", zero_limit_payload),
    )
    res_zero = readiness.inspect_readiness(declared, readiness.ReadinessRequest(("worker",)), workspace=tmp_path)
    assert res_zero["status"] == "blocked"
    assert res_zero["exit_code"] == 2
    assert res_zero["admitted_capacity"] == 0
    assert res_zero["limiting_reason"] == "insufficient-capacity"

    # 3. Hub repo claim held by another node -> blocked (admitted_capacity == 0)
    monkeypatch.setattr(
        fleet_model_admission,
        "fetch_versioned_roster",
        lambda **kw: fleet_model_admission.ModelAdmissionDecision(
            True, 0, "authoritative", _hub_roster_payload(source="hub")
        ),
    )
    monkeypatch.setattr(
        fleet_client,
        "inspect_claim",
        lambda target: fleet_client.ClaimDecision(
            True, "held", {"owner_node": "other-node", "lock_owner_token": "tok-1"}
        ),
    )
    res_held = readiness.inspect_readiness(declared, readiness.ReadinessRequest(("worker",)), workspace=tmp_path)
    assert res_held["status"] == "blocked"
    assert res_held["exit_code"] == 2
    assert res_held["admitted_capacity"] == 0
    assert res_held["limiting_reason"] == "insufficient-capacity"


@pytest.mark.parametrize(
    "scenario",
    [
        "hub-unavailable",
        "auth-failed",
        "lkg-capacity-unverified",
        "positive-seat-limit-unverified",
        "malformed-payload",
        "claim-unverified",
    ],
)
def test_brigade_hub_unavailable_lkg_or_unverified_capacity_stays_unverified(declared, tmp_path, monkeypatch, scenario):
    from brigade import fleet_client, fleet_model_admission

    _forbid_mutating_hub_calls(monkeypatch)
    monkeypatch.setattr(
        fleet_client, "load_fleet_settings", lambda: {"hub_url": "https://hub.example.test", "node_token": "tok"}
    )
    monkeypatch.setattr(fleet_client, "resolve_claim_target", lambda ws: "repo:example/brigade")
    monkeypatch.setattr(fleet_client, "inspect_claim", lambda target: fleet_client.ClaimDecision(True, "free", None))

    if scenario in {"hub-unavailable", "auth-failed"}:
        monkeypatch.setattr(
            fleet_model_admission,
            "fetch_versioned_roster",
            lambda **kw: fleet_model_admission.ModelAdmissionDecision(False, 1, scenario, {}),
        )
    elif scenario == "lkg-capacity-unverified":
        monkeypatch.setattr(
            fleet_model_admission,
            "fetch_versioned_roster",
            lambda **kw: fleet_model_admission.ModelAdmissionDecision(
                True, 0, "lkg", _hub_roster_payload(source="lkg")
            ),
        )
    elif scenario == "positive-seat-limit-unverified":
        limited = _hub_roster_payload(
            seats=[
                {
                    "seat": "worker",
                    "provider": "openai",
                    "model": "example-model",
                    "reasoning": "medium",
                    "brigade_cli": "codex",
                    "bindings": {"brigade": {"cli": "codex", "model": "example-model"}},
                    "enabled": True,
                    "limit": 2,
                }
            ]
        )
        monkeypatch.setattr(
            fleet_model_admission,
            "fetch_versioned_roster",
            lambda **kw: fleet_model_admission.ModelAdmissionDecision(True, 0, "authoritative", limited),
        )
    elif scenario == "malformed-payload":
        monkeypatch.setattr(
            fleet_model_admission,
            "fetch_versioned_roster",
            lambda **kw: fleet_model_admission.ModelAdmissionDecision(True, 0, "authoritative", {"unexpected": 1}),
        )
    else:
        monkeypatch.setattr(
            fleet_model_admission,
            "fetch_versioned_roster",
            lambda **kw: fleet_model_admission.ModelAdmissionDecision(
                True, 0, "authoritative", _hub_roster_payload(source="hub")
            ),
        )
        monkeypatch.setattr(
            fleet_client,
            "inspect_claim",
            lambda target: fleet_client.ClaimDecision(False, "hub-unavailable", None),
        )

    result = readiness.inspect_readiness(declared, readiness.ReadinessRequest(("worker",)), workspace=tmp_path)
    assert result["status"] == "unverified"
    assert result["exit_code"] == 3
    if scenario == "lkg-capacity-unverified":
        admission_row = next(item for item in result["evidence"] if item["category"] == "admission")
        capacity_row = next(item for item in result["evidence"] if item["category"] == "capacity")
        assert admission_row["status"] == "passed"
        assert "hub model admission LKG" in admission_row["detail"]
        assert capacity_row["status"] == "unknown"


def test_brigade_enrolled_fleet_policy_enforces_admissible_seat(declared, tmp_path, monkeypatch):
    from brigade import fleet_client, fleet_client_policy, fleet_model_admission

    _forbid_mutating_hub_calls(monkeypatch)
    monkeypatch.setattr(
        fleet_client, "load_fleet_settings", lambda: {"hub_url": "https://hub.example.test", "node_token": "tok"}
    )
    monkeypatch.setattr(fleet_client, "resolve_claim_target", lambda ws: "repo:example/brigade")
    monkeypatch.setattr(fleet_client, "inspect_claim", lambda target: fleet_client.ClaimDecision(True, "free", None))
    enrolled_payload = _hub_roster_payload(
        source="hub",
        fleet_policy={"active": True, "version": 3, "digest": "sha256:" + ("b" * 64)},
    )
    monkeypatch.setattr(
        fleet_model_admission,
        "fetch_versioned_roster",
        lambda **kw: fleet_model_admission.ModelAdmissionDecision(True, 0, "authoritative", enrolled_payload),
    )

    policy_doc = {
        "schema": "brigade.fleet_policy.v1",
        "defaults": {"roles": {}, "data": {"allow_training": False}, "execution": {"concurrency": 1}},
        "machines": {},
        "seats": {
            "worker": {
                "provider": "openai",
                "model": "example-model",
                "enabled": True,
                "concurrency": 1,
                "training_allowed": True,
                "bindings": {"brigade": {"cli": "codex", "model": "example-model"}},
            }
        },
        "consumers": {
            "brigade-run": {
                "reload": "none",
                "coverage": "verified",
            }
        },
        "repositories": {},
    }
    monkeypatch.setattr(fleet_client_policy, "show_policy", lambda **kw: {"document": policy_doc})

    denied_res = readiness.inspect_readiness(declared, readiness.ReadinessRequest(("worker",)), workspace=tmp_path)
    assert denied_res["status"] == "blocked"
    assert denied_res["exit_code"] == 2
    assert denied_res["limiting_reason"] == "admission-denied"

    policy_doc["seats"]["worker"]["training_allowed"] = False
    allowed_res = readiness.inspect_readiness(declared, readiness.ReadinessRequest(("worker",)), workspace=tmp_path)
    admission_row = next(item for item in allowed_res["evidence"] if item["category"] == "admission")
    capacity_row = next(item for item in allowed_res["evidence"] if item["category"] == "capacity")
    assert admission_row["status"] == "passed"
    assert "fleet policy" in admission_row["detail"]
    assert capacity_row["status"] == "unknown"
    assert allowed_res["status"] == "unverified"
    assert allowed_res["exit_code"] == 3

    policy_doc["seats"]["worker"]["concurrency"] = 0
    zero_res = readiness.inspect_readiness(declared, readiness.ReadinessRequest(("worker",)), workspace=tmp_path)
    assert zero_res["status"] == "blocked"
    assert zero_res["exit_code"] == 2
    assert zero_res["admitted_capacity"] == 0
    assert zero_res["limiting_reason"] == "insufficient-capacity"


def test_produced_brigade_observations_invalidate_on_staleness_and_scope_change(declared, tmp_path, monkeypatch):
    _stub_standalone_env(monkeypatch)
    request = readiness.ReadinessRequest(("worker",))
    obs = readiness.brigade_route_observations(declared, request, workspace=tmp_path, now=NOW)
    assert set(obs) == {"admission", "capacity"}
    assert obs["admission"].scope == readiness.operation_scope(declared, request, tmp_path)
    assert obs["capacity"].observed_at == NOW

    stale = evaluate(declared, request, tmp_path, observations=obs)
    assert stale["status"] == "ready"

    expired_eval = readiness.evaluate_readiness(
        declared,
        request,
        workspace=tmp_path,
        now=NOW + readiness.OPERATION_TTL_SECONDS + 1.0,
        health={"worker": health_for(declared, at=NOW + readiness.OPERATION_TTL_SECONDS + 1.0)},
        observations=obs,
    )
    assert expired_eval["status"] == "unverified"
    assert expired_eval["limiting_reason"] == "observation-stale-or-scope-mismatch"

    changed_eval = evaluate(declared, replace(request, concurrency=2), tmp_path, observations=obs)
    assert changed_eval["status"] == "unverified"
    assert changed_eval["limiting_reason"] == "observation-stale-or-scope-mismatch"


def test_t3_route_remains_untouched_by_brigade_producer(declared, tmp_path, monkeypatch):
    monkeypatch.setattr(
        readiness,
        "brigade_route_observations",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("T3 must not call brigade producer")),
    )
    result = readiness.inspect_readiness(
        declared, readiness.ReadinessRequest(("worker",), route="t3"), workspace=tmp_path
    )
    assert result["status"] == "unverified"
    support = next(item for item in result["evidence"] if item["category"] == "route-support")
    assert support["reason"] == "t3-contract-unavailable"


def test_brigade_roster_ready_cli_end_to_end(tmp_path, monkeypatch, capsys):
    import json
    from brigade import cli, fleet_client, roster_cmd, runguard

    ws = tmp_path / "ws"
    ws.mkdir()
    assert roster_cmd.init(ws) == 0
    capsys.readouterr()

    _stub_standalone_env(monkeypatch)
    rc_ready = cli.main(["roster", "ready", "--target", str(ws), "--worker", "coder", "--json"])
    ready_payload = json.loads(capsys.readouterr().out)
    assert rc_ready == 0
    assert ready_payload["status"] == "ready"
    assert ready_payload["admitted_capacity"] == 4

    monkeypatch.setattr(runguard, "inspect_run_lock_reconcile", lambda _: "live")
    rc_blocked = cli.main(["roster", "ready", "--target", str(ws), "--worker", "coder", "--json"])
    blocked_payload = json.loads(capsys.readouterr().out)
    assert rc_blocked == 2
    assert blocked_payload["status"] == "blocked"
    assert blocked_payload["admitted_capacity"] == 0

    def _broken_settings():
        raise fleet_client.FleetClientError("bad fleet.toml")

    monkeypatch.setattr(fleet_client, "load_fleet_settings", _broken_settings)
    monkeypatch.setattr(runguard, "inspect_run_lock_reconcile", lambda _: "absent")
    rc_broken = cli.main(["roster", "ready", "--target", str(ws), "--worker", "coder", "--json"])
    broken_payload = json.loads(capsys.readouterr().out)
    assert rc_broken == 3
    assert broken_payload["status"] == "unverified"
