import json
import time
from dataclasses import replace

import pytest

from brigade import roster
from brigade import seat_health
from brigade import proc
from brigade import agents
from brigade.seat_health import (
    CHECK_NAMES,
    HEALTHY_CACHE_TTL_SECONDS,
    UNHEALTHY_CACHE_TTL_SECONDS,
    SeatHealthCheck,
    SeatHealthProbe,
    seat_fingerprint,
    write_seat_health_receipt,
)


class FakeAdapter:
    def __init__(self, statuses=None):
        self.statuses = statuses or {}
        self.calls = []

    def check(self, name, *, seat, roster, workspace, timeout_seconds):
        self.calls.append((seat.name, name, timeout_seconds, workspace))
        value = self.statuses.get((seat.name, name), self.statuses.get(name, ("passed", "ok", None)))
        return SeatHealthCheck(name, value[0], value[1], cause_code=value[2])


def _roster():
    primary = roster.Agent(
        "primary", "codex", "plan", model="gpt-test", fallback=("backup",), env={"TOKEN_REF": "TOKEN"}
    )
    backup = roster.Agent("backup", "cursor", "work", model="gpt-test", transport="acpx", transport_version="0.12.0")
    return roster.Roster("primary", {"primary": primary, "backup": backup}, sandbox="read-only", codex_transport="exec")


def test_probe_runs_all_adapter_checks_and_maps_health_states():
    fake = FakeAdapter({"authentication-entitlement": ("degraded", "no status API", None)})
    result = SeatHealthProbe(adapter=fake).probe(_roster().agents["primary"], _roster(), allow_model_smoke=False)
    assert result.status == "degraded"
    assert tuple(check.name for check in result.checks) == CHECK_NAMES
    assert {name for _, name, _, _ in fake.calls} == set(CHECK_NAMES)


def test_probe_normalizes_auth_version_model_transport_hang_and_timeout_failures():
    cases = [
        ("authentication-entitlement", "auth-required", "auth-required"),
        ("authentication-entitlement", "entitlement-denied", "entitlement-denied"),
        ("version-gates", "version-mismatch", "version-gate"),
        ("model-reachability", "model-unavailable", "model-unavailable"),
        ("transport-liveness", "missing-transport", "transport-unavailable"),
        ("transport-liveness", "initialize-no-progress", "transport-hang"),
        ("declaration", "timeout", "timeout"),
    ]
    for name, cause, expected in cases:
        fake = FakeAdapter({name: ("failed", "token=secret-value", cause)})
        result = SeatHealthProbe(adapter=fake).probe(_roster().agents["primary"], _roster())
        assert result.status == "unhealthy"
        assert result.failure is not None
        assert result.failure.failure_class.value == expected
        assert "secret-value" not in result.failure.payload()["detail"]


def test_antigravity_prompt_free_probes_pass_for_listed_model(monkeypatch):
    seat = roster.Agent("agy", "antigravity", "work", model="gemini-3.8-flash")
    roster_value = roster.Roster("agy", {"agy": seat}, sandbox="read-only")
    calls = []

    monkeypatch.setattr(
        seat_health.agents,
        "resolve_agent_executable",
        lambda cli: proc.ExecutableIdentity("agy", None, "fixture", True, "fixture"),
    )

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv == ["agy", "models"]:
            return proc.Result(0, "gemini-3.8-flash-low (Gemini Flash)", "")
        if argv == ["agy", "--version"]:
            return proc.Result(0, "agy 1.1.25", "")
        raise AssertionError(f"unexpected command: {argv}")

    monkeypatch.setattr(seat_health.proc, "run", fake_run)

    result = SeatHealthProbe().probe(seat, roster_value, allow_model_smoke=False)

    assert result.status == "healthy"
    assert {check.name: check.status for check in result.checks} == {
        "declaration": "passed",
        "executable-identity": "passed",
        "authentication-entitlement": "passed",
        "transport-liveness": "passed",
        "version-gates": "passed",
        "model-reachability": "passed",
        "isolation-compatibility": "passed",
    }
    assert all(timeout <= 2.0 for _, kwargs in calls for timeout in [kwargs.get("timeout", 0.0)])


@pytest.mark.parametrize("requested_model", ("Gemini 3.8 Flash (Low)", "Gemini 3.8 Flash"))
def test_antigravity_model_probe_matches_display_name_and_model_override(monkeypatch, requested_model):
    declared = roster.Agent("agy", "antigravity", "work", model="gemini-3.8-pro-low")
    seat = replace(declared, model=requested_model)  # Effective seat after a --model override.
    roster_value = roster.Roster("agy", {"agy": seat}, sandbox="read-only")
    monkeypatch.setattr(
        seat_health.agents,
        "resolve_agent_executable",
        lambda cli: proc.ExecutableIdentity("agy", None, "fixture", True, "fixture"),
    )
    monkeypatch.setattr(
        seat_health.proc,
        "run",
        lambda argv, **kwargs: proc.Result(
            0,
            "gemini-3.8-flash-low (Gemini Flash)" if argv[-1] == "models" else "agy 1.1.25",
            "",
        ),
    )

    result = SeatHealthProbe().probe(seat, roster_value, allow_model_smoke=False)

    model_check = next(check for check in result.checks if check.name == "model-reachability")
    assert model_check.status == "passed"


def test_antigravity_prompt_free_probes_use_agent_command_prefix_and_environment(monkeypatch):
    seat = roster.Agent(
        "agy",
        "antigravity",
        "work",
        command=("agy-wrapper", "--profile", "seat-profile"),
        env={"AGY_PROFILE": "seat-profile"},
        model="gemini-3.8-flash",
    )
    roster_value = roster.Roster("agy", {"agy": seat}, sandbox="read-only")
    calls = []
    monkeypatch.setattr(
        seat_health.agents,
        "resolve_agent_executable",
        lambda cli, command=None: proc.ExecutableIdentity("agy-wrapper", None, "fixture", True, "fixture"),
    )

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return proc.Result(
            0,
            "gemini-3.8-flash-low (Gemini Flash)" if argv[-1] == "models" else "agy 1.1.25",
            "",
        )

    monkeypatch.setattr(seat_health.proc, "run", fake_run)

    result = SeatHealthProbe(collect_executable_version=False).probe(seat, roster_value, allow_model_smoke=False)

    assert result.status == "healthy"
    assert [argv for argv, _ in calls] == [
        ["agy-wrapper", "--profile", "seat-profile", "models"],
        ["agy-wrapper", "--profile", "seat-profile", "--version"],
        ["agy-wrapper", "--profile", "seat-profile", "models"],
    ]
    assert all(kwargs["env"]["AGY_PROFILE"] == "seat-profile" for _, kwargs in calls)


@pytest.mark.parametrize(
    ("models", "models_code", "version", "check_name", "cause_code"),
    [
        ("Error: authentication required", 1, "1.1.25", "authentication-entitlement", "auth-required"),
        ("gemini-3.8-pro-low (Gemini Pro)", 0, "1.1.25", "model-reachability", "model-not-listed"),
        ("gemini-3.8-flash-low (Gemini Flash)", 0, "1.1.24", "version-gates", "version-mismatch"),
    ],
)
def test_antigravity_prompt_free_probes_report_auth_model_and_version_failures(
    monkeypatch, models, models_code, version, check_name, cause_code
):
    seat = roster.Agent("agy", "antigravity", "work", model="gemini-3.8-flash")
    roster_value = roster.Roster("agy", {"agy": seat}, sandbox="read-only")
    monkeypatch.setattr(
        seat_health.agents,
        "resolve_agent_executable",
        lambda cli: proc.ExecutableIdentity("agy", None, "fixture", True, "fixture"),
    )
    monkeypatch.setattr(
        seat_health.proc,
        "run",
        lambda argv, **kwargs: proc.Result(
            models_code if argv[-1] == "models" else 0,
            models if argv[-1] == "models" else f"agy {version}",
            "",
        ),
    )

    result = SeatHealthProbe().probe(seat, roster_value, allow_model_smoke=False)
    check = next(item for item in result.checks if item.name == check_name)

    assert check.status == "failed"
    assert check.cause_code == cause_code


def test_probe_roster_includes_fallbacks_and_cache_ttls(monkeypatch):
    now = [10.0]
    fake = FakeAdapter()
    probe = SeatHealthProbe(adapter=fake, clock=lambda: now[0])
    results = probe.probe_roster(_roster())
    assert [result.seat for result in results] == ["primary", "backup"]
    first_calls = len(fake.calls)
    assert probe.probe(_roster().agents["primary"], _roster()).cached
    assert len(fake.calls) == first_calls + 1  # isolation compatibility is never cached
    now[0] += HEALTHY_CACHE_TTL_SECONDS + 1
    assert not probe.probe(_roster().agents["primary"], _roster()).cached

    unhealthy = SeatHealthProbe(
        adapter=FakeAdapter({"declaration": ("failed", "bad", "timeout")}), clock=lambda: now[0]
    )
    unhealthy.probe(_roster().agents["primary"], _roster())
    now[0] += UNHEALTHY_CACHE_TTL_SECONDS + 1
    assert not unhealthy.probe(_roster().agents["primary"], _roster()).cached


def test_fingerprint_uses_presence_not_secret_values(monkeypatch):
    roster_value = _roster()
    monkeypatch.setenv("TOKEN", "first-secret")
    first = seat_fingerprint(roster_value.agents["primary"], roster_value)
    monkeypatch.setenv("TOKEN", "second-secret")
    assert seat_fingerprint(roster_value.agents["primary"], roster_value) == first
    changed = replace(roster_value.agents["primary"], model="other")
    assert seat_fingerprint(changed, roster_value) != first


def test_receipt_is_atomic_bounded_and_redacted(tmp_path):
    fake = FakeAdapter({"declaration": ("failed", "https://user:pass@example.test/x?token=secret", "timeout")})
    result = SeatHealthProbe(adapter=fake).probe(_roster().agents["primary"], _roster())
    path = tmp_path / "seat-health.json"
    write_seat_health_receipt(path, [result], run_id="run-test")
    payload = json.loads(path.read_text())
    assert payload["schema"] == "brigade.seat_health.v1"
    text = path.read_text()
    assert "secret" not in text and "user:pass" not in text
    assert not list(tmp_path.glob(".seat-health.json.*.tmp"))


def test_receipt_timestamps_use_captured_wall_clock_values_and_monotonic_duration(tmp_path):
    monotonic = [10.0]

    def clock():
        value = monotonic[0]
        monotonic[0] += 0.25
        return value

    wall_values = iter((1_700_000_000.0, 1_700_000_002.0))
    result = SeatHealthProbe(adapter=FakeAdapter(), clock=clock, wall_clock=lambda: next(wall_values)).probe(
        _roster().agents["primary"], _roster()
    )
    path = tmp_path / "seat-health.json"
    write_seat_health_receipt(path, [result])

    payload = json.loads(path.read_text())
    expected_started = "2023-11-14T22:13:20.000Z"
    expected_finished = "2023-11-14T22:13:22.000Z"
    assert payload["started_at"] == expected_started
    assert payload["finished_at"] == expected_finished
    assert payload["started_at"] < payload["finished_at"]
    assert payload["results"][0]["started_at"] == expected_started
    assert payload["results"][0]["finished_at"] == expected_finished
    assert payload["results"][0]["duration_seconds"] != 2.0


def test_model_smoke_runs_only_in_a_temporary_git_repository(monkeypatch):
    roster_value = _roster()
    seat = replace(roster_value.agents["primary"], cli="aider")
    seen = []
    monkeypatch.setattr(
        seat_health.agents,
        "resolve_agent_executable",
        lambda cli: proc.ExecutableIdentity(cli, None, "fixture", True, "fixture"),
    )
    monkeypatch.setattr(seat_health.proc, "run", lambda *args, **kwargs: proc.Result(0, "1.2.3", ""))

    def smoke(agent, roster_value, workspace, timeout):
        seen.append(workspace)
        assert (workspace / ".git").is_dir()
        return SeatHealthCheck("model-reachability", "passed", "exact marker returned")

    result = SeatHealthProbe(model_smoke=smoke).probe(seat, roster_value)
    assert result.status == "degraded"  # auth and version are advisory for direct fixture seats
    assert len(seen) == 1
    assert not seen[0].exists()


def test_default_direct_smoke_is_no_tools_no_write_exact_marker(monkeypatch):
    roster_value = _roster()
    seat = replace(roster_value.agents["primary"], cli="aider")
    calls = []
    monkeypatch.setattr(
        seat_health.agents,
        "resolve_agent_executable",
        lambda cli: proc.ExecutableIdentity(cli, None, "fixture", True, "fixture"),
    )
    monkeypatch.setattr(seat_health.proc, "run", lambda *args, **kwargs: proc.Result(0, "1.2.3", ""))

    def fake_run_agent(cli, prompt, **kwargs):
        calls.append((cli, prompt, kwargs))
        return agents.AgentResult(seat_health._MODEL_SMOKE_MARKER, True)

    monkeypatch.setattr(seat_health.agents, "run_agent", fake_run_agent)
    result = SeatHealthProbe().probe(seat, roster_value)
    assert next(check for check in result.checks if check.name == "model-reachability").status == "passed"
    _, prompt, kwargs = calls[0]
    assert "Do not use tools" in prompt and "Do not write files" in prompt
    assert kwargs["read_only"] and kwargs["sandbox"] == "read-only"
    assert (kwargs["cwd"] / ".git").is_dir() is False  # cleaned after the smoke completes


def test_codex_cloud_model_reachability_skips_provider_smoke(monkeypatch):
    seat = roster.Agent(
        name="cloud-worker",
        cli="codex-cloud:configured",
        role="cloud worker",
    )
    roster_value = roster.Roster(
        orchestrator="chef",
        agents={
            "chef": roster.Agent(name="chef", cli="codex", role="plan"),
            "cloud-worker": seat,
        },
    )
    calls = []

    def fake_run_agent(*args, **kwargs):
        calls.append((args, kwargs))
        return agents.AgentResult("should-not-run", True)

    monkeypatch.setattr(seat_health.agents, "run_agent", fake_run_agent)
    result = SeatHealthProbe().probe(seat, roster_value)
    check = next(item for item in result.checks if item.name == "model-reachability")
    assert check.status == "degraded"
    assert "brigade run cloud canary" in check.detail
    assert calls == []

    hosted = roster.Agent(
        name="hosted",
        cli=None,
        role="code",
        endpoint="https://example.test/v1",
        model="hosted-model",
    )
    endpoint_roster = roster.Roster(
        orchestrator="chef",
        agents={
            "chef": roster.Agent(name="chef", cli="codex", role="plan"),
            "hosted": hosted,
        },
    )
    endpoint_result = SeatHealthProbe().probe(hosted, endpoint_roster, allow_model_smoke=False)
    liveness = next(item for item in endpoint_result.checks if item.name == "transport-liveness")
    assert liveness.status == "degraded"
    assert liveness.cause_code == "probe-incomplete"
    assert all(item.status == "passed" or item.cause_code == "probe-incomplete" for item in endpoint_result.checks)


def test_overall_deadline_returns_timeout_results_without_serial_wait(monkeypatch):
    monkeypatch.setattr(seat_health, "OVERALL_TIMEOUT_SECONDS", 0.01)

    class HangingAdapter(FakeAdapter):
        def check(self, name, **kwargs):
            time.sleep(0.05)
            return super().check(name, **kwargs)

    started = time.monotonic()
    results = SeatHealthProbe(adapter=HangingAdapter()).probe_roster(_roster())
    assert time.monotonic() - started < 0.04
    assert len(results) == 2
    assert [result.seat for result in results] == ["primary", "backup"]
    assert len({result.seat for result in results}) == len(results)
    assert all(result.status == "unhealthy" for result in results)
    assert all(result.failure and result.failure.failure_class.value == "timeout" for result in results)
