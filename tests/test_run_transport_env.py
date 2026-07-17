"""Per-seat env overrides flowing through worker dispatch (issue #301)."""

from brigade import agents
from brigade import run_transport
from brigade.roster import Agent, Roster
from brigade.run_transport import Assignment


def _roster_with_env(env):
    chef = Agent(name="chef", cli="codex", role="plan")
    k3 = Agent(name="k3", cli="claude", role="worker", model="kimi-k3", env=env)
    return Roster(orchestrator="chef", agents={"chef": chef, "k3": k3})


def _dispatch(roster, monkeypatch, captured):
    def fake_run(argv, **kw):
        captured["env"] = kw.get("env")
        return agents.proc.Result(0, "worker answer", "")

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


def test_worker_env_missing_ref_fails_the_worker(monkeypatch):
    monkeypatch.delenv("LANE_KEY", raising=False)
    captured = {}
    roster = _roster_with_env({"ANTHROPIC_AUTH_TOKEN_REF": "LANE_KEY"})
    result = _dispatch(roster, monkeypatch, captured)[0]
    assert not result.ok
    assert "LANE_KEY" in result.detail
    assert captured.get("env") is None


def test_worker_without_env_keeps_legacy_call_shape(monkeypatch):
    captured = {}
    roster = _roster_with_env(None)
    result = _dispatch(roster, monkeypatch, captured)[0]
    assert result.ok
    assert captured["env"] is None
    assert result.env_overrides == ()
    assert result.endpoint_host is None


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
