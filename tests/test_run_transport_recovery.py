from dataclasses import replace

from brigade import acpx_adapter, agents, run_transport
from brigade.roster import Agent, Roster
from brigade.run_receipts import worker_payload, write_worker_logs
from brigade.run_transport import Assignment, WorkerAttempt, WorkerResult


def _attempt(**overrides):
    values = {
        "kind": "initial",
        "worker": "grok-review",
        "task": "Review the diff.",
        "transport": "direct",
        "model": "grok-4.5",
        "reasoning": "high",
        "started_at": "2026-07-17T08:00:00+00:00",
        "finished_at": "2026-07-17T08:00:01+00:00",
        "exit_code": None,
        "terminal_reason": "malformed-final-output",
        "failure_phase": "output-validation",
        "failure_kind": "malformed-final-output",
        "session_id": "019f0000-0000-7000-8000-000000000001",
        "selected": False,
    }
    values.update(overrides)
    return WorkerAttempt(**values)


def test_worker_payload_serializes_required_attempt_fields_with_null_exit_code():
    result = WorkerResult(
        worker="grok-review",
        task="Review the diff.",
        text="progress",
        ok=False,
        attempts=(_attempt(),),
    )

    attempt = worker_payload([result])[0]["attempts"][0]

    assert attempt == {
        "kind": "initial",
        "worker": "grok-review",
        "task": "Review the diff.",
        "transport": "direct",
        "model": "grok-4.5",
        "reasoning": "high",
        "started_at": "2026-07-17T08:00:00+00:00",
        "finished_at": "2026-07-17T08:00:01+00:00",
        "exit_code": None,
        "terminal_reason": "malformed-final-output",
        "failure_phase": "output-validation",
        "failure_kind": "malformed-final-output",
        "session_id": "019f0000-0000-7000-8000-000000000001",
        "selected": False,
    }


def test_write_worker_logs_preserves_distinct_attempt_streams(tmp_path):
    first = _attempt(stdout="first stdout\n", stderr="first stderr\n")
    second = _attempt(
        kind="continuation",
        started_at="2026-07-17T08:00:02+00:00",
        finished_at="2026-07-17T08:00:03+00:00",
        terminal_reason="EndTurn",
        failure_phase=None,
        failure_kind=None,
        exit_code=0,
        selected=True,
        stdout="second stdout\n",
        stderr="",
    )
    result = WorkerResult(
        worker="grok-review",
        task="Review the diff.",
        text="answer",
        ok=True,
        attempts=(first, second),
    )

    recorded = write_worker_logs(tmp_path, [result])[0]

    assert recorded.attempts[0].stdout_log == "logs/worker-001-grok-review-attempt-001-initial.stdout.log"
    assert recorded.attempts[1].stdout_log == "logs/worker-001-grok-review-attempt-002-continuation.stdout.log"
    assert (tmp_path / recorded.attempts[0].stdout_log).read_text() == "first stdout\n"
    assert (tmp_path / recorded.attempts[0].stderr_log).read_text() == "first stderr\n"
    assert (tmp_path / recorded.attempts[1].stdout_log).read_text() == "second stdout\n"


def _recovery_roster(*, fallback: bool = True, source_env: dict[str, str] | None = None) -> Roster:
    grok = Agent(
        name="grok-review",
        cli="grok",
        role="review",
        timeout_seconds=31,
        model="grok-4.5",
        reasoning="high",
        env=source_env or {},
        invalid_final_fallback="cursor-grok" if fallback else None,
    )
    seats = {
        "chef": Agent(name="chef", cli="codex", role="plan"),
        "grok-review": grok,
    }
    if fallback:
        seats["cursor-grok"] = Agent(
            name="cursor-grok",
            cli="cursor",
            role="fallback review",
            model="grok-4.5",
            transport="acpx",
            transport_version="0.12.0",
        )
    return Roster(orchestrator="chef", agents=seats, max_workers=1)


def _invalid_final(*, session_id="019f0000-0000-7000-8000-000000000001") -> agents.AgentResult:
    return agents.AgentResult(
        text="Reviewing files first.",
        ok=False,
        detail="grok exited 0 without a structured final response",
        stdout="invalid stdout\n",
        stderr="",
        exit_code=0,
        requested_model="grok-4.5",
        reasoning="high",
        stop_reason="Cancelled",
        session_id=session_id,
        failure_phase="output-validation",
        failure_kind="malformed-final-output",
    )


def _successful_final(text="No actionable findings.") -> agents.AgentResult:
    return agents.AgentResult(
        text=text,
        ok=True,
        stdout="success stdout\n",
        stderr="",
        exit_code=0,
        requested_model="grok-4.5",
        reasoning="high",
        stop_reason="EndTurn",
        session_id="019f0000-0000-7000-8000-000000000001",
    )


def _dispatch_recovery(
    monkeypatch,
    tmp_path,
    direct_results,
    *,
    fallback_result=None,
    fallback=True,
    read_only=True,
    direct=True,
    source_env=None,
):
    direct_calls = []
    fallback_calls = []
    results = iter(direct_results)

    def fake_run_agent(cli_ref, prompt, **kwargs):
        direct_calls.append((cli_ref, prompt, kwargs))
        return next(results)

    def fake_run_cursor(prompt, **kwargs):
        fallback_calls.append((prompt, kwargs))
        assert fallback_result is not None
        return fallback_result

    monkeypatch.setattr(agents, "run_agent", fake_run_agent)
    monkeypatch.setattr(acpx_adapter, "run_cursor", fake_run_cursor)
    result = run_transport.dispatch(
        [Assignment(worker="grok-review", task="Review the diff.")],
        _recovery_roster(fallback=fallback, source_env=source_env),
        build_prompt=lambda agent, assignment, **kwargs: assignment.task,
        run_appserver_worker=lambda *args, **kwargs: agents.AgentResult(text="", ok=False, detail="unused"),
        event_writer=lambda events_dir, worker, verbose=False: None,
        cwd=tmp_path,
        read_only=read_only,
        sandbox="read-only" if read_only else None,
        direct=direct,
    )[0]
    return result, direct_calls, fallback_calls


def test_direct_grok_first_attempt_success_is_selected(monkeypatch, tmp_path):
    result, direct_calls, fallback_calls = _dispatch_recovery(monkeypatch, tmp_path, [_successful_final()])

    assert result.ok is True
    assert result.text == "No actionable findings."
    assert len(direct_calls) == 1
    assert fallback_calls == []
    assert [attempt.kind for attempt in result.attempts] == ["initial"]
    assert [attempt.selected for attempt in result.attempts] == [True]


def test_direct_grok_continuation_reuses_exact_session_and_settings(monkeypatch, tmp_path):
    result, direct_calls, fallback_calls = _dispatch_recovery(
        monkeypatch,
        tmp_path,
        [_invalid_final(), _successful_final("Recovered final.")],
    )

    assert result.ok is True
    assert result.text == "Recovered final."
    assert fallback_calls == []
    assert len(direct_calls) == 2
    _, continuation_prompt, continuation_kwargs = direct_calls[1]
    assert "Return the final answer now" in continuation_prompt
    assert continuation_kwargs["resume_session_id"] == "019f0000-0000-7000-8000-000000000001"
    assert continuation_kwargs["timeout"] == 31
    assert continuation_kwargs["cwd"] == tmp_path
    assert continuation_kwargs["read_only"] is True
    assert continuation_kwargs["sandbox"] == "read-only"
    assert continuation_kwargs["model"] == "grok-4.5"
    assert continuation_kwargs["reasoning"] == "high"
    assert [attempt.kind for attempt in result.attempts] == ["initial", "continuation"]
    assert [attempt.selected for attempt in result.attempts] == [False, True]


def test_direct_grok_fallback_recovers_after_two_invalid_finals(monkeypatch, tmp_path):
    fallback = replace(_successful_final("Fallback final."), transport="acpx", stop_reason="end_turn")
    result, direct_calls, fallback_calls = _dispatch_recovery(
        monkeypatch,
        tmp_path,
        [_invalid_final(), _invalid_final()],
        fallback_result=fallback,
    )

    assert result.ok is True
    assert result.text == "Fallback final."
    assert len(direct_calls) == 2
    assert len(fallback_calls) == 1
    assert fallback_calls[0][0] == "Review the diff."
    assert [attempt.kind for attempt in result.attempts] == ["initial", "continuation", "fallback"]
    assert [attempt.worker for attempt in result.attempts] == ["grok-review", "grok-review", "cursor-grok"]
    assert [attempt.selected for attempt in result.attempts] == [False, False, True]


def test_direct_grok_fallback_uses_selected_seat_endpoint_provenance(monkeypatch, tmp_path):
    fallback = replace(_successful_final("Fallback final."), transport="acpx", stop_reason="end_turn")
    result, _, _ = _dispatch_recovery(
        monkeypatch,
        tmp_path,
        [_invalid_final(), _invalid_final()],
        fallback_result=fallback,
        source_env={"OPENAI_BASE_URL": "https://direct.example.com/v1"},
    )

    assert result.ok is True
    assert result.env_overrides == ()
    assert result.endpoint_host is None


def test_direct_grok_missing_fallback_fails_after_one_continuation(monkeypatch, tmp_path):
    result, direct_calls, fallback_calls = _dispatch_recovery(
        monkeypatch,
        tmp_path,
        [_invalid_final(), _invalid_final()],
        fallback=False,
    )

    assert result.ok is False
    assert result.failure_kind == "grok-fallback-missing"
    assert len(direct_calls) == 2
    assert fallback_calls == []
    assert [attempt.selected for attempt in result.attempts] == [False, False]


def test_direct_grok_all_attempts_invalid_has_no_selected_result(monkeypatch, tmp_path):
    result, direct_calls, fallback_calls = _dispatch_recovery(
        monkeypatch,
        tmp_path,
        [_invalid_final(), _invalid_final()],
        fallback_result=agents.AgentResult(
            text="Still reviewing.",
            ok=False,
            detail="ACP stream contained no final assistant text",
            stdout="fallback invalid\n",
            stderr="",
            exit_code=0,
            transport="acpx",
            requested_model="grok-4.5",
            failure_phase="output-validation",
            failure_kind="empty-output",
        ),
    )

    assert result.ok is False
    assert result.failure_kind == "empty-output"
    assert len(direct_calls) == 2
    assert len(fallback_calls) == 1
    assert len(result.attempts) == 3
    assert not any(attempt.selected for attempt in result.attempts)


def test_direct_grok_missing_session_id_does_not_guess_or_fallback(monkeypatch, tmp_path):
    result, direct_calls, fallback_calls = _dispatch_recovery(
        monkeypatch,
        tmp_path,
        [_invalid_final(session_id=None)],
    )

    assert result.ok is False
    assert result.failure_kind == "grok-session-missing"
    assert len(direct_calls) == 1
    assert fallback_calls == []


def test_direct_grok_nonzero_failure_does_not_enter_recovery(monkeypatch, tmp_path):
    failure = agents.AgentResult(
        text="",
        ok=False,
        detail="network failed",
        stderr="network failed",
        exit_code=1,
        requested_model="grok-4.5",
        failure_phase="inference",
        failure_kind="transport-error",
    )
    result, direct_calls, fallback_calls = _dispatch_recovery(monkeypatch, tmp_path, [failure])

    assert result.failure_kind == "transport-error"
    assert len(direct_calls) == 1
    assert fallback_calls == []


def test_direct_grok_continuation_timeout_does_not_enter_fallback(monkeypatch, tmp_path):
    timeout = agents.AgentResult(
        text="",
        ok=False,
        detail="timed out",
        exit_code=124,
        timed_out=True,
        requested_model="grok-4.5",
        failure_phase="inference",
        failure_kind="timeout",
    )
    result, direct_calls, fallback_calls = _dispatch_recovery(
        monkeypatch,
        tmp_path,
        [_invalid_final(), timeout],
    )

    assert result.failure_kind == "timeout"
    assert len(direct_calls) == 2
    assert fallback_calls == []


def test_writable_grok_dispatch_does_not_enter_recovery(monkeypatch, tmp_path):
    result, direct_calls, fallback_calls = _dispatch_recovery(
        monkeypatch,
        tmp_path,
        [_invalid_final()],
        read_only=False,
    )

    assert result.failure_kind == "malformed-final-output"
    assert len(direct_calls) == 1
    assert fallback_calls == []
    assert result.attempts == ()


def test_orchestrated_grok_dispatch_does_not_enter_recovery(monkeypatch, tmp_path):
    result, direct_calls, fallback_calls = _dispatch_recovery(
        monkeypatch,
        tmp_path,
        [_invalid_final()],
        direct=False,
    )

    assert result.failure_kind == "malformed-final-output"
    assert len(direct_calls) == 1
    assert fallback_calls == []
    assert result.attempts == ()
