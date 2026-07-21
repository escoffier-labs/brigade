"""Per-seat env overrides flowing through worker dispatch (issue #301)."""

import threading

import pytest

from brigade import acpx_adapter, agents
from brigade import run_transport
from brigade.roster import Agent, Roster
from brigade.run_receipts import write_worker_logs
from brigade.run_transport import Assignment


def _roster_with_env(env):
    chef = Agent(name="chef", cli="codex", role="plan")
    k3 = Agent(name="k3", cli="claude", role="worker", model="kimi-k3", env=env)
    return Roster(orchestrator="chef", agents={"chef": chef, "k3": k3})


def _dispatch(roster, monkeypatch, captured, *, stdout="worker answer", stderr=""):
    def fake_run(argv, **kw):
        captured["env"] = kw.get("env")
        captured["process_registry"] = kw.get("process_registry")
        return agents.proc.Result(0, stdout, stderr)

    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", fake_run)
    return run_transport.dispatch(
        [Assignment(worker="k3", task="do the thing")],
        roster,
        build_prompt=lambda agent, assignment, **kw: assignment.task,
        run_appserver_worker=lambda *a, **kw: agents.AgentResult(text="", ok=False, detail="unused"),
        event_writer=lambda events_dir, worker, verbose=False: None,
        read_only=True,
    )


def test_worker_env_overrides_reach_child_process(monkeypatch):
    monkeypatch.setenv("LANE_KEY", "sk-lane-value")
    captured = {}
    roster = _roster_with_env(
        {
            "ANTHROPIC_BASE_URL": "https://api.example.com/anthropic",
            "ANTHROPIC_AUTH_TOKEN_REF": "LANE_KEY",
        }
    )
    results = _dispatch(roster, monkeypatch, captured)
    assert len(results) == 1
    assert results[0].ok
    assert captured["env"]["ANTHROPIC_BASE_URL"] == "https://api.example.com/anthropic"
    assert captured["env"]["ANTHROPIC_AUTH_TOKEN"] == "sk-lane-value"
    assert "ANTHROPIC_AUTH_TOKEN_REF" not in captured["env"]


def test_worker_result_records_env_provenance_without_values(monkeypatch):
    monkeypatch.setenv("LANE_KEY", "sk-lane-value")
    captured = {}
    roster = _roster_with_env(
        {
            "ANTHROPIC_BASE_URL": "https://api.example.com/anthropic",
            "ANTHROPIC_AUTH_TOKEN_REF": "LANE_KEY",
        }
    )
    result = _dispatch(roster, monkeypatch, captured)[0]
    assert result.env_overrides == ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL")
    assert result.endpoint_host == "api.example.com"
    serialized = str(result)
    assert "sk-lane-value" not in serialized


def test_worker_logs_scrub_resolved_env_values_before_persistence(monkeypatch, tmp_path):
    token = "lane-token-value-for-test"
    monkeypatch.setenv("LANE_KEY", token)
    captured = {}
    roster = _roster_with_env({"ANTHROPIC_AUTH_TOKEN_REF": "LANE_KEY"})

    result = _dispatch(
        roster,
        monkeypatch,
        captured,
        stdout=f"worker answer token={token}\n",
        stderr=f"adapter diagnostic bearer={token}\n",
    )[0]
    recorded = write_worker_logs(tmp_path, [result])[0]

    assert token not in result.text
    assert token not in result.stdout
    assert token not in result.stderr
    assert recorded.stdout_log is not None
    assert recorded.stderr_log is not None
    stdout_log = (tmp_path / recorded.stdout_log).read_text()
    stderr_log = (tmp_path / recorded.stderr_log).read_text()
    assert token not in stdout_log
    assert token not in stderr_log
    assert stdout_log == "worker answer token=[ANTHROPIC_AUTH_TOKEN]\n"
    assert stderr_log == "adapter diagnostic bearer=[ANTHROPIC_AUTH_TOKEN]\n"


def test_worker_env_missing_ref_fails_the_worker(monkeypatch):
    monkeypatch.delenv("LANE_KEY", raising=False)
    captured = {}
    roster = _roster_with_env({"ANTHROPIC_AUTH_TOKEN_REF": "LANE_KEY"})
    result = _dispatch(roster, monkeypatch, captured)[0]
    assert not result.ok
    assert "LANE_KEY" in result.detail
    assert captured.get("env") is None


def test_keyboard_interrupt_notifies_before_blocked_worker_finishes(monkeypatch):
    worker_started = threading.Event()
    allow_interrupt = threading.Event()
    release_worker = threading.Event()
    worker_finished = threading.Event()
    interruption_recorded = threading.Event()
    dispatch_finished = threading.Event()
    errors = []

    def blocked_run_agent(*args, **kwargs):  # noqa: ARG001
        worker_started.set()
        release_worker.wait()
        worker_finished.set()
        return agents.AgentResult(text="done", ok=True)

    def interrupted_wait(futures):  # noqa: ARG001
        allow_interrupt.wait()
        raise KeyboardInterrupt
        yield  # pragma: no cover

    monkeypatch.setattr(agents, "run_agent", blocked_run_agent)
    monkeypatch.setattr(run_transport, "as_completed", interrupted_wait)

    def invoke_dispatch():
        try:
            run_transport.dispatch(
                [Assignment(worker="k3", task="do the thing")],
                _roster_with_env(None),
                build_prompt=lambda agent, assignment, **kw: assignment.task,
                run_appserver_worker=lambda *a, **kw: agents.AgentResult(text="", ok=False, detail="unused"),
                event_writer=lambda events_dir, worker, verbose=False: None,
                read_only=True,
                on_interrupt=interruption_recorded.set,
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            dispatch_finished.set()

    controller = threading.Thread(target=invoke_dispatch)
    controller.start()
    try:
        assert worker_started.wait(2)
        allow_interrupt.set()
        assert interruption_recorded.wait(2)
        assert not dispatch_finished.wait(0.1)
        assert not worker_finished.is_set()
    finally:
        release_worker.set()
        controller.join(2)

    assert not controller.is_alive()
    assert worker_finished.wait(2)
    assert len(errors) == 1
    assert isinstance(errors[0], KeyboardInterrupt)


def test_keyboard_interrupt_stops_scoped_appserver_turn_before_return(monkeypatch):
    worker_started = threading.Event()
    allow_interrupt = threading.Event()
    release_worker = threading.Event()
    registry_interrupted = threading.Event()
    appserver_closed = threading.Event()
    dispatch_finished = threading.Event()
    errors = []

    class StubRegistry:
        def interrupt(self):
            registry_interrupted.set()

    class StubAppserver:
        def close(self):
            appserver_closed.set()
            release_worker.set()

    def blocked_appserver(*args, **kwargs):  # noqa: ARG001
        worker_started.set()
        release_worker.wait()
        return agents.AgentResult(text="done", ok=True)

    def interrupted_wait(futures):  # noqa: ARG001
        allow_interrupt.wait()
        raise KeyboardInterrupt
        yield  # pragma: no cover

    monkeypatch.setattr(run_transport, "as_completed", interrupted_wait)

    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="plan"),
            "worker": Agent(name="worker", cli="codex", role="worker"),
        },
    )

    def invoke_dispatch():
        try:
            run_transport.dispatch(
                [Assignment(worker="worker", task="do the thing")],
                roster,
                build_prompt=lambda agent, assignment, **kw: assignment.task,
                run_appserver_worker=blocked_appserver,
                event_writer=lambda events_dir, worker, verbose=False: None,
                appserver=StubAppserver(),
                control_registry=StubRegistry(),
                read_only=True,
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            dispatch_finished.set()

    controller = threading.Thread(target=invoke_dispatch)
    controller.start()
    try:
        assert worker_started.wait(2)
        allow_interrupt.set()
        assert registry_interrupted.wait(2)
        assert appserver_closed.wait(2)
        assert dispatch_finished.wait(2)
    finally:
        release_worker.set()
        controller.join(2)

    assert not controller.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], KeyboardInterrupt)


def test_worker_without_env_keeps_legacy_call_shape(monkeypatch):
    captured = {}
    roster = _roster_with_env(None)
    result = _dispatch(roster, monkeypatch, captured)[0]
    assert result.ok
    assert captured["env"] is None
    assert result.env_overrides == ()
    assert result.endpoint_host is None


def test_direct_worker_uses_dispatch_scoped_process_registry(monkeypatch):
    captured = {}

    assert _dispatch(_roster_with_env(None), monkeypatch, captured)[0].ok
    assert captured["process_registry"] is not None


def test_acpx_worker_uses_dispatch_scoped_process_registry(monkeypatch, tmp_path):
    captured = {}
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="plan"),
            "composer": Agent(
                name="composer",
                cli="cursor",
                role="worker",
                transport="acpx",
                transport_version="0.12.0",
                model="composer-2.5",
            ),
        },
    )

    def fake_run_cursor(prompt, **kwargs):
        captured["process_registry"] = kwargs.get("process_registry")
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(acpx_adapter, "run_cursor", fake_run_cursor)
    result = run_transport.dispatch(
        [Assignment(worker="composer", task="do the thing")],
        roster,
        build_prompt=lambda agent, assignment, **kw: assignment.task,
        run_appserver_worker=lambda *a, **kw: agents.AgentResult(text="", ok=False, detail="unused"),
        event_writer=lambda events_dir, worker, verbose=False: None,
        cwd=tmp_path,
        read_only=True,
    )[0]

    assert result.ok
    assert captured["process_registry"] is not None


def test_worker_payload_serializes_env_provenance(monkeypatch):
    from brigade.run_receipts import worker_payload

    monkeypatch.setenv("LANE_KEY", "sk-lane-value")
    captured = {}
    roster = _roster_with_env(
        {
            "ANTHROPIC_BASE_URL": "https://api.example.com/anthropic",
            "ANTHROPIC_AUTH_TOKEN_REF": "LANE_KEY",
        }
    )
    entry = worker_payload(_dispatch(roster, monkeypatch, captured))[0]
    assert entry["env_overrides"] == ["ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"]
    assert entry["endpoint_host"] == "api.example.com"
    assert "sk-lane-value" not in str(entry)


def test_worker_payload_omits_env_fields_without_overrides(monkeypatch):
    from brigade.run_receipts import worker_payload

    captured = {}
    entry = worker_payload(_dispatch(_roster_with_env(None), monkeypatch, captured))[0]
    assert "env_overrides" not in entry
    assert "endpoint_host" not in entry


def test_env_seat_dispatches_direct_even_under_appserver(monkeypatch):
    """A codex seat with env must not silently lose it to the appserver branch."""
    monkeypatch.setenv("LANE_KEY", "sk-lane-value")
    captured = {}

    def fake_run(argv, **kw):
        captured["env"] = kw.get("env")
        return agents.proc.Result(0, "worker answer", "")

    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", fake_run)
    chef = Agent(name="chef", cli="codex", role="plan")
    worker = Agent(
        name="w",
        cli="codex",
        role="worker",
        env={"OPENAI_BASE_URL": "https://api.example.com/v1", "OPENAI_API_KEY_REF": "LANE_KEY"},
    )
    roster = Roster(orchestrator="chef", agents={"chef": chef, "w": worker})
    appserver_calls = []
    results = run_transport.dispatch(
        [Assignment(worker="w", task="do the thing")],
        roster,
        build_prompt=lambda agent, assignment, **kw: assignment.task,
        run_appserver_worker=lambda *a, **kw: (
            appserver_calls.append(a) or agents.AgentResult(text="appserver answer", ok=True)
        ),
        event_writer=lambda events_dir, worker, verbose=False: None,
        appserver=object(),
        read_only=True,
    )
    assert appserver_calls == []
    assert results[0].ok
    assert captured["env"]["OPENAI_API_KEY"] == "sk-lane-value"


def test_env_provenance_absent_when_ref_missing_fails_before_spawn(monkeypatch):
    monkeypatch.delenv("LANE_KEY", raising=False)
    captured = {}
    roster = _roster_with_env({"ANTHROPIC_AUTH_TOKEN_REF": "LANE_KEY"})
    result = _dispatch(roster, monkeypatch, captured)[0]
    assert not result.ok
    assert result.failure_kind == "env-ref-missing"
    assert result.env_overrides == ()
    assert result.endpoint_host is None


def test_endpoint_host_resolves_ref_passed_base_url(monkeypatch):
    monkeypatch.setenv("LANE_URL", "https://ref.example.com/anthropic")
    monkeypatch.setenv("LANE_KEY", "sk-lane-value")
    captured = {}
    roster = _roster_with_env({"ANTHROPIC_BASE_URL_REF": "LANE_URL", "ANTHROPIC_AUTH_TOKEN_REF": "LANE_KEY"})
    result = _dispatch(roster, monkeypatch, captured)[0]
    assert result.endpoint_host == "ref.example.com"


def test_endpoint_host_generalizes_to_any_base_url(monkeypatch):
    monkeypatch.setenv("LANE_KEY", "sk-lane-value")
    captured = {}
    roster = _roster_with_env(
        {"OPENAI_BASE_URL": "https://openai-lane.example.com/v1", "OPENAI_API_KEY_REF": "LANE_KEY"}
    )
    result = _dispatch(roster, monkeypatch, captured)[0]
    assert result.endpoint_host == "openai-lane.example.com"


_CF_MODEL_ROUTE = "cloudflare-ai-gateway/openai/gpt-5.3-codex"
_FAKE_CF_ACCOUNT = "fake-account-id-for-test"
_FAKE_CF_GATEWAY = "fake-gateway-id-for-test"


def _assert_cloudflare_values_not_echoed(result):
    """Secret values from env must never leak into WorkerResult or its payload."""
    payload = str(result)
    assert _FAKE_CF_ACCOUNT not in result.detail
    assert _FAKE_CF_GATEWAY not in result.detail
    assert _FAKE_CF_ACCOUNT not in payload
    assert _FAKE_CF_GATEWAY not in payload


def _cloudflare_gateway_roster():
    chef = Agent(name="chef", cli="codex", role="plan")
    worker = Agent(name="cf_worker", cli="codex", role="worker", model=_CF_MODEL_ROUTE)
    return Roster(orchestrator="chef", agents={"chef": chef, "cf_worker": worker})


def _dispatch_cloudflare_worker(monkeypatch, *, run_impl):
    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", run_impl)
    return run_transport.dispatch(
        [Assignment(worker="cf_worker", task="do the thing")],
        _cloudflare_gateway_roster(),
        build_prompt=lambda agent, assignment, **kw: assignment.task,
        run_appserver_worker=lambda *a, **kw: agents.AgentResult(text="", ok=False, detail="unused"),
        event_writer=lambda events_dir, worker, verbose=False: None,
        read_only=True,
    )


def _clear_cloudflare_gateway_env(monkeypatch) -> None:
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_GATEWAY_ID", raising=False)


def test_cloudflare_gateway_preflight_fails_without_required_env(monkeypatch):
    _clear_cloudflare_gateway_env(monkeypatch)

    def should_not_run(*args, **kwargs):
        raise AssertionError("proc.run must not be called when Cloudflare gateway env is missing")

    result = _dispatch_cloudflare_worker(monkeypatch, run_impl=should_not_run)[0]

    assert not result.ok
    assert result.failure_phase == "preflight"
    assert result.failure_kind == "provider-config"
    assert "CLOUDFLARE_ACCOUNT_ID" in result.detail
    assert "CLOUDFLARE_GATEWAY_ID" in result.detail


@pytest.mark.parametrize(
    ("env", "missing_vars"),
    [
        ({}, ("CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_GATEWAY_ID")),
        ({"CLOUDFLARE_ACCOUNT_ID": _FAKE_CF_ACCOUNT}, ("CLOUDFLARE_GATEWAY_ID",)),
        ({"CLOUDFLARE_GATEWAY_ID": _FAKE_CF_GATEWAY}, ("CLOUDFLARE_ACCOUNT_ID",)),
    ],
)
def test_cloudflare_gateway_preflight_names_each_missing_env_var(monkeypatch, env, missing_vars):
    _clear_cloudflare_gateway_env(monkeypatch)
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    def should_not_run(*args, **kwargs):
        raise AssertionError("proc.run must not be called when Cloudflare gateway env is missing")

    result = _dispatch_cloudflare_worker(monkeypatch, run_impl=should_not_run)[0]

    assert not result.ok
    assert result.failure_kind == "provider-config"
    for var in missing_vars:
        assert var in result.detail
    _assert_cloudflare_values_not_echoed(result)


def test_cloudflare_gateway_preflight_payload_serializes_provider_config_failure(monkeypatch):
    from brigade.run_receipts import worker_payload

    _clear_cloudflare_gateway_env(monkeypatch)

    def should_not_run(*args, **kwargs):
        raise AssertionError("proc.run must not be called when Cloudflare gateway env is missing")

    entry = worker_payload(_dispatch_cloudflare_worker(monkeypatch, run_impl=should_not_run))[0]

    assert entry["ok"] is False
    assert entry["failure_phase"] == "preflight"
    assert entry["failure_kind"] == "provider-config"
    assert "CLOUDFLARE_ACCOUNT_ID" in entry["detail"]
    assert "CLOUDFLARE_GATEWAY_ID" in entry["detail"]


def test_cloudflare_gateway_preflight_empty_string_env_counts_as_missing(monkeypatch):
    _clear_cloudflare_gateway_env(monkeypatch)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", "")
    monkeypatch.setenv("CLOUDFLARE_GATEWAY_ID", _FAKE_CF_GATEWAY)

    def should_not_run(*args, **kwargs):
        raise AssertionError("proc.run must not be called when Cloudflare gateway env is missing")

    result = _dispatch_cloudflare_worker(monkeypatch, run_impl=should_not_run)[0]

    assert not result.ok
    assert result.failure_phase == "preflight"
    assert result.failure_kind == "provider-config"
    assert "CLOUDFLARE_ACCOUNT_ID" in result.detail
    assert "CLOUDFLARE_GATEWAY_ID" not in result.detail
    _assert_cloudflare_values_not_echoed(result)


def test_cloudflare_gateway_preflight_passes_when_env_present(monkeypatch):
    _clear_cloudflare_gateway_env(monkeypatch)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", _FAKE_CF_ACCOUNT)
    monkeypatch.setenv("CLOUDFLARE_GATEWAY_ID", _FAKE_CF_GATEWAY)
    captured = {"called": False}

    def fake_run(argv, **kw):
        captured["called"] = True
        return agents.proc.Result(0, "worker answer", "")

    result = _dispatch_cloudflare_worker(monkeypatch, run_impl=fake_run)[0]

    assert result.ok
    assert captured["called"] is True


def test_non_cloudflare_route_unaffected_when_gateway_env_missing(monkeypatch):
    _clear_cloudflare_gateway_env(monkeypatch)
    captured = {}

    def fake_run(argv, **kw):
        captured["called"] = True
        return agents.proc.Result(0, "worker answer", "")

    monkeypatch.setattr(agents.proc, "which", lambda c: "/x/" + c)
    monkeypatch.setattr(agents.proc, "run", fake_run)
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="plan"),
            "coder": Agent(name="coder", cli="codex", role="worker", model="gpt-5.5"),
        },
    )
    result = run_transport.dispatch(
        [Assignment(worker="coder", task="do the thing")],
        roster,
        build_prompt=lambda agent, assignment, **kw: assignment.task,
        run_appserver_worker=lambda *a, **kw: agents.AgentResult(text="", ok=False, detail="unused"),
        event_writer=lambda events_dir, worker, verbose=False: None,
        read_only=True,
    )[0]

    assert result.ok
    assert captured["called"] is True


def test_cloudflare_gateway_preflight_fallback_agent_fails_without_launch(monkeypatch):
    _clear_cloudflare_gateway_env(monkeypatch)
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent(name="chef", cli="codex", role="plan"),
            "grok_cli": Agent(
                name="grok_cli",
                cli="grok",
                role="worker",
                model="grok-4.5",
                invalid_final_fallback="cf_fallback",
            ),
            "cf_fallback": Agent(
                name="cf_fallback",
                cli="codex",
                role="worker",
                model=_CF_MODEL_ROUTE,
            ),
        },
        max_workers=1,
    )
    calls = []

    def fake_run_agent(cli_ref, prompt, **kwargs):
        calls.append(cli_ref)
        if cli_ref == "grok":
            return agents.AgentResult(
                text="",
                ok=False,
                detail="grok produced malformed final output",
                failure_phase="output-validation",
                failure_kind="malformed-final-output",
                session_id="session-1",
                requested_model="grok-4.5",
            )
        raise AssertionError(f"run_agent must not be called for {cli_ref!r} after fallback preflight fails")

    monkeypatch.setattr(agents, "run_agent", fake_run_agent)

    results = run_transport.dispatch(
        [Assignment(worker="grok_cli", task="do the thing")],
        roster,
        build_prompt=lambda agent, assignment, **kw: assignment.task,
        run_appserver_worker=lambda *a, **kw: agents.AgentResult(text="", ok=False, detail="unused"),
        event_writer=lambda events_dir, worker, verbose=False: None,
        read_only=True,
        direct=True,
    )

    assert len(results) == 1
    result = results[0]
    assert not result.ok
    assert result.failure_phase == "preflight"
    assert result.failure_kind == "provider-config"
    assert "CLOUDFLARE_ACCOUNT_ID" in result.detail
    assert "CLOUDFLARE_GATEWAY_ID" in result.detail
    assert "codex" not in calls


def test_endpoint_host_records_all_distinct_hosts(monkeypatch):
    monkeypatch.setenv("LANE_URL", "https://ref.example.com/v1")
    monkeypatch.setenv("LANE_KEY", "sk-lane-value")
    captured = {}
    roster = _roster_with_env(
        {
            "ANTHROPIC_BASE_URL": "https://anthropic-lane.example.com/anthropic",
            "OPENAI_BASE_URL_REF": "LANE_URL",
            "OPENAI_API_KEY_REF": "LANE_KEY",
        }
    )
    result = _dispatch(roster, monkeypatch, captured)[0]
    assert result.endpoint_host == "anthropic-lane.example.com,ref.example.com"
