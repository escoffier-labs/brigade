"""Tests for seat-health run policy: summaries, retry, quarantine."""

from __future__ import annotations

from dataclasses import replace

from brigade import agents, seat_health, seat_health_policy
from brigade.run_transport import Assignment, WorkerResult, dispatch
from brigade.roster import Agent, Roster
from brigade.worker_failure import FailureClass, FailurePhase, RetryDisposition, WorkerFailure


def _failure(*, failure_class: FailureClass, retry: RetryDisposition, phase: FailurePhase = FailurePhase.DISPATCH):
    return WorkerFailure(
        failure_class=failure_class,
        phase=phase,
        retry=retry,
        detected_by="test",
        detail="boom",
    )


def test_decide_retry_allows_one_same_seat_then_quarantines():
    state = seat_health_policy.SeatQuarantineState()
    failure = _failure(
        failure_class=FailureClass.NETWORK_UNAVAILABLE,
        retry=RetryDisposition.SAME_SEAT_ONCE,
    )
    first = seat_health_policy.decide_retry(failure, seat="coder", state=state)
    assert first.should_retry_same_seat
    assert not state.is_quarantined("coder")
    second = seat_health_policy.decide_retry(failure, seat="coder", state=state)
    assert second.action == "quarantine"
    assert state.is_quarantined("coder")


def test_decide_retry_quarantines_non_same_seat_dispositions():
    state = seat_health_policy.SeatQuarantineState()
    failure = _failure(failure_class=FailureClass.TIMEOUT, retry=RetryDisposition.FALLBACK)
    decision = seat_health_policy.decide_retry(failure, seat="coder", state=state)
    assert decision.action == "fallback"
    assert state.is_quarantined("coder")


def test_seat_health_and_worker_failure_summaries():
    checks = (seat_health.SeatHealthCheck("declaration", "passed", "ok"),)
    healthy = seat_health.SeatHealthResult(
        "p1",
        "chef",
        "sha256:a",
        "healthy",
        {"adapter": "codex", "transport": "exec", "model": None, "reasoning": None},
        checks,
        0.0,
        0.1,
        0.0,
        0.1,
    )
    unhealthy = replace(healthy, probe_id="p2", seat="coder", status="unhealthy")
    summary = seat_health_policy.seat_health_summary(
        (healthy, unhealthy),
        routing_decisions=({"outcome": "fallback", "requested_seat": "coder", "effective_seat": "coder-b"},),
    )
    assert summary == {
        "schema": "brigade.seat_health_summary.v1",
        "receipt": "seat-health.json",
        "healthy": 1,
        "degraded": 0,
        "unhealthy": 1,
        "fallbacks_selected": 1,
    }
    workers = [
        WorkerResult(worker="coder", task="t", text="", ok=False, detail="x", failure_kind="network-error"),
        WorkerResult(worker="scout", task="t", text="ok", ok=True),
    ]
    failures = seat_health_policy.worker_failure_summary(workers)
    assert failures is not None
    assert failures["classes"]["network-unavailable"] == 1
    assert failures["seats"] == ["coder"]
    warning = seat_health_policy.format_incomplete_warning(workers)
    assert "coder failed [network-unavailable]" in warning


def test_probe_invalidate_clears_cache_by_seat():
    class AlwaysPass:
        def check(self, name, *, seat, roster, workspace, timeout_seconds):
            return seat_health.SeatHealthCheck(name, "passed", "ok")

    clock = {"now": 0.0}
    probe = seat_health.SeatHealthProbe(
        adapter=AlwaysPass(), clock=lambda: clock["now"], wall_clock=lambda: clock["now"]
    )
    roster = Roster(
        orchestrator="chef",
        agents={"chef": Agent("chef", "codex", "plan"), "coder": Agent("coder", "claude", "code")},
    )
    first = probe.probe(roster.agents["coder"], roster, allow_model_smoke=False)
    assert first.cached is False
    clock["now"] += 1.0
    second = probe.probe(roster.agents["coder"], roster, allow_model_smoke=False)
    assert second.cached is True
    assert probe.invalidate(seat="coder") == 1
    clock["now"] += 1.0
    third = probe.probe(roster.agents["coder"], roster, allow_model_smoke=False)
    assert third.cached is False


def test_same_seat_once_persists_then_retries_once(monkeypatch):
    calls: list[str] = []
    persisted: list[WorkerResult] = []

    def fake_run_agent(cli, prompt, **kwargs):
        calls.append(cli)
        if len(calls) == 1:
            return agents.AgentResult(
                text="",
                ok=False,
                detail="network down",
                failure_phase="dispatch",
                failure_kind="network-error",
            )
        return agents.AgentResult(text="recovered", ok=True, detail="")

    monkeypatch.setattr(agents, "run_agent", fake_run_agent)

    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan"),
            "coder": Agent("coder", "claude", "code"),
        },
        max_workers=1,
    )
    state = seat_health_policy.SeatQuarantineState()
    results = dispatch(
        [Assignment(worker="coder", task="implement")],
        roster,
        build_prompt=lambda *args, **kwargs: "prompt",
        run_appserver_worker=lambda *args, **kwargs: agents.AgentResult(text="", ok=False),
        event_writer=lambda *args, **kwargs: lambda *a, **k: None,
        quarantine_state=state,
        reprobe_seat=lambda agent: True,
        on_failed_attempt_persisted=persisted.append,
    )
    assert len(calls) == 2
    assert len(persisted) == 1
    assert persisted[0].ok is False
    assert len(results) == 1
    assert results[0].ok is True
    assert [attempt.kind for attempt in results[0].attempts] == ["initial", "same-seat-once"]


def test_decide_retry_records_receipt_entries():
    state = seat_health_policy.SeatQuarantineState()
    failure = _failure(failure_class=FailureClass.TIMEOUT, retry=RetryDisposition.FALLBACK)
    decision = seat_health_policy.decide_retry(failure, seat="coder", state=state)
    assert state.retry_decisions == [
        {
            "attempt": 1,
            "seat": "coder",
            "failure_kind": "timeout",
            "decision": "fallback",
            "reason": decision.detail,
        }
    ]
    second_failure = _failure(
        failure_class=FailureClass.NETWORK_UNAVAILABLE,
        retry=RetryDisposition.SAME_SEAT_ONCE,
    )
    seat_health_policy.decide_retry(second_failure, seat="coder", state=state)
    assert state.retry_decisions[1]["attempt"] == 2
    assert state.retry_decisions[1]["decision"] == "quarantine"
    assert state.retry_decisions[1]["failure_kind"] == "network-unavailable"


def test_dispatch_appends_retry_decisions_to_state(monkeypatch):
    calls: list[str] = []

    def fake_run_agent(cli, prompt, **kwargs):
        calls.append(cli)
        if len(calls) == 1:
            return agents.AgentResult(
                text="",
                ok=False,
                detail="network down",
                failure_phase="dispatch",
                failure_kind="network-error",
            )
        return agents.AgentResult(text="recovered", ok=True, detail="")

    monkeypatch.setattr(agents, "run_agent", fake_run_agent)

    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan"),
            "coder": Agent("coder", "claude", "code"),
        },
        max_workers=1,
    )
    state = seat_health_policy.SeatQuarantineState()
    dispatch(
        [Assignment(worker="coder", task="implement")],
        roster,
        build_prompt=lambda *args, **kwargs: "prompt",
        run_appserver_worker=lambda *args, **kwargs: agents.AgentResult(text="", ok=False),
        event_writer=lambda *args, **kwargs: lambda *a, **k: None,
        quarantine_state=state,
        reprobe_seat=lambda agent: True,
        on_failed_attempt_persisted=lambda result: None,
    )
    assert [entry["seat"] for entry in state.retry_decisions] == ["coder"]
    assert state.retry_decisions[0]["attempt"] == 1
    assert state.retry_decisions[0]["failure_kind"] == "network-unavailable"
    assert state.retry_decisions[0]["decision"] == "same-seat-once"


def test_format_reroute_summary_names_fallback_and_skip():
    fallback = {
        "requested_seat": "coder",
        "effective_seat": "coder-fallback",
        "outcome": "fallback",
        "typed_cause": "auth-required",
    }
    skipped = {"requested_seat": "reviewer", "effective_seat": None, "outcome": "skip", "typed_cause": "timeout"}
    summary = seat_health_policy.format_reroute_summary([fallback, skipped])
    assert summary == "note: seat reroute: coder -> coder-fallback [auth-required]; reviewer skipped [timeout]"
    assert seat_health_policy.format_reroute_summary(None) is None
    assert seat_health_policy.format_reroute_summary([]) is None


def test_transport_fallback_decision_shape():
    decision = seat_health_policy.transport_fallback_decision(
        requested_transport="app-server",
        effective_transport="exec",
        cause="transport-unavailable",
        detail="initialize failed",
    )
    assert decision["schema"] == "brigade.transport_routing.v1"
    assert decision["outcome"] == "fallback"
    assert decision["typed_cause"] == "transport-unavailable"


def test_require_hard_isolation_fails_soft_enforcement(monkeypatch):
    monkeypatch.setattr(agents, "read_only_enforcement", lambda *args, **kwargs: "soft")
    monkeypatch.setattr(
        agents,
        "resolve_agent_executable",
        lambda cli: type("E", (), {"runnable": True, "command": cli, "kind": "path", "detail": "ok"})(),
    )

    probe = seat_health.SeatHealthProbe(collect_executable_version=False)
    seat = Agent("coder", "claude", "code")
    roster = Roster(orchestrator="coder", agents={"coder": seat})
    result = probe.probe(seat, roster, allow_model_smoke=False, require_hard_isolation=True)
    isolation = next(check for check in result.checks if check.name == "isolation-compatibility")
    assert isolation.status == "failed"
    assert isolation.cause_code == "unsafe-isolation"
    assert result.status == "unhealthy"
    assert result.failure is not None
    assert result.failure.failure_class is FailureClass.CONFIGURATION_INVALID
