"""CLI task routing selects the effective role and refuses unavailable policy."""

import pytest

from brigade import aboyeur, cli, fleet_client, fleet_client_policy, run_preference
from brigade.cli import run as run_cli

from tests.test_aboyeur_orchestrator_health_routing import _install_probe, PerSeatFakeAdapter

_REAL_RUN = aboyeur.run


@pytest.fixture
def run_task(tmp_path, monkeypatch):
    monkeypatch.setenv("BRIGADE_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: {"hub_url": "https://hub.example.test"})
    roster = tmp_path / "roster.toml"
    roster.write_text("""orchestrator = "chef"
[agents.chef]
cli = "codex"
role = "plan"
[agents.coder]
cli = "codex"
role = "code"
[agents.author]
cli = "claude"
role = "code"
fallback = ["coder"]
""")
    monkeypatch.setattr(run_preference, "refresh_cache", lambda: run_preference.RunPreference(impl="coder"))
    monkeypatch.setattr("brigade.cli.fleet_policy._canonical_repo", lambda path: "example/project")
    monkeypatch.setattr(
        fleet_client_policy,
        "resolve_policy",
        lambda **kwargs: {
            "effective": {"roles": {"writing": "author", "frontend": "author"}},
            "sources": {"roles.writing": {"layer": "repo:example/project"}},
        },
    )
    seen = []

    def fake_run(task, roster, **kwargs):
        seen.append((task, roster, kwargs))
        return 0

    monkeypatch.setattr(aboyeur, "run", fake_run)

    def invoke(task, *extra):
        return cli.main(
            [
                "run",
                task,
                "--cwd",
                str(tmp_path),
                "--roster",
                str(roster),
                "--dry-run",
                "--no-artifacts",
                "--no-fleet-claim",
                *extra,
            ]
        )

    return invoke, seen


def test_matched_role_direct_dispatch_and_strict_fallback(run_task):
    invoke, seen = run_task
    assert invoke("Draft a blog post about an API") == 0
    _, roster, kwargs = seen[0]
    assert kwargs["worker"] == "author"
    assert roster.agents["author"].fallback == ()
    assert roster.seat_routing[0]["task_role"]["source_layer"] == "repo:example/project"


@pytest.mark.parametrize(
    "task,extra,worker",
    [
        ("Draft a blog post", ("--worker", "coder"), "coder"),
        ("Have author draft a blog post", (), None),
        ("Write a function that writes rows to the database", (), "coder"),
        ("Draft a blog post and add an endpoint", (), "coder"),
    ],
)
def test_explicit_named_none_and_ambiguous_precedence(run_task, task, extra, worker, capsys):
    invoke, seen = run_task
    assert invoke(task, *extra) == 0
    assert seen[0][2].get("worker") == worker
    if "endpoint" in task:
        assert "task role: ambiguous" in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["hub", "missing", "unusable"])
def test_matched_task_refuses_without_dispatch(run_task, monkeypatch, capsys, failure):
    invoke, seen = run_task
    if failure == "hub":

        def unavailable(**kwargs):
            raise fleet_client_policy.FleetPolicyClientError("unavailable", "Hub unavailable")

        monkeypatch.setattr(fleet_client_policy, "resolve_policy", unavailable)
    elif failure == "missing":
        monkeypatch.setattr(fleet_client_policy, "resolve_policy", lambda **kwargs: {"effective": {"roles": {}}})
    else:
        monkeypatch.setattr(run_cli, "_direct_worker_error", lambda *args, **kwargs: "seat denied")
    assert invoke("Draft a blog post") == 2
    assert seen == []
    assert "role seat unavailable" in capsys.readouterr().err


def test_detached_classified_role_is_revalidated_and_strict(run_task):
    invoke, seen = run_task
    assert (
        invoke("Draft a blog post", "--worker", "author", "--applied-preference", "--task-role-workload", "writing")
        == 0
    )
    assert seen[0][1].agents["author"].fallback == ()


def test_detached_role_mismatch_refuses(run_task):
    invoke, seen = run_task
    assert (
        invoke(
            "Write rows to the database",
            "--worker",
            "author",
            "--applied-preference",
            "--task-role-workload",
            "writing",
        )
        == 2
    )
    assert seen == []


@pytest.mark.parametrize("failure", ["admission", "health"])
def test_classified_role_runtime_refusal_never_launches_fallback(run_task, monkeypatch, tmp_path, capsys, failure):
    from brigade import agents, fleet_client, fleet_model_admission
    from tests.test_fleet_model_admission import _versioned_snapshot, _versioned_seat
    from tests.run_test_helpers import run_aboyeur_guarded

    invoke, seen = run_task
    fragment = "/customers/SyntheticOnlyHealth42"
    assert invoke(f"Draft a blog post about {fragment} endpoint") == 0
    effective = seen[0][1]
    monkeypatch.setattr(aboyeur, "run", _REAL_RUN)
    snapshot = _versioned_snapshot(
        _versioned_seat("author", "anthropic", "model-author", instance_id="claude"),
        expires_at="2099-08-30T14:15:00Z",
    )
    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", lambda: snapshot)
    admitted = []

    def deny(**kwargs):
        admitted.append(kwargs["seat"])
        return fleet_model_admission.ModelAdmissionDecision(False, 2, "role admission denied", {})

    monkeypatch.setattr(fleet_model_admission, "admit_model", deny)
    launches = []
    monkeypatch.setattr(agents, "run_agent", lambda *args, **kwargs: launches.append(args))
    if failure == "health":
        _install_probe(monkeypatch, PerSeatFakeAdapter(unhealthy=frozenset({"author"})))
    rc = run_aboyeur_guarded(
        "Draft a blog post",
        effective,
        worker="author",
        output_dir=tmp_path / "runtime",
        code_graph_enabled=False,
        route_enabled=False,
    )
    assert rc != 0
    assert launches == []
    if failure == "health":
        import json

        payload = json.loads((tmp_path / "runtime" / "run.json").read_text())
        assert any(item.get("task_role", {}).get("seat") == "author" for item in payload["seat_routing"])
        assert any(item.get("outcome") == "abort" for item in payload["seat_routing"])
        assert fragment.lower() not in json.dumps(payload["seat_routing"]).lower()
        assert fragment.lower() not in capsys.readouterr().err.lower()
    if failure == "admission":
        assert admitted == ["author"]


@pytest.mark.parametrize("task", ["Draft a blog post", "Redesign the login page"])
def test_standalone_classified_task_keeps_existing_routing_without_policy_network(run_task, monkeypatch, task):
    invoke, seen = run_task
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: {"hub_url": ""})

    def no_policy(**kwargs):
        raise AssertionError("standalone task must not resolve central roles")

    monkeypatch.setattr(fleet_client_policy, "resolve_policy", no_policy)
    assert invoke(task) == 0
    assert seen[0][2]["worker"] == "coder"
    assert seen[0][1].seat_routing == ()


def test_detached_required_role_does_not_downgrade_when_hub_configuration_disappears(run_task, monkeypatch):
    invoke, seen = run_task
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: {"hub_url": ""})

    def missing(**kwargs):
        raise fleet_client_policy.FleetPolicyClientError("network", "no fleet hub configured")

    monkeypatch.setattr(fleet_client_policy, "resolve_policy", missing)
    assert (
        invoke("Draft a blog post", "--worker", "author", "--applied-preference", "--task-role-workload", "writing")
        == 2
    )
    assert seen == []


def test_ambiguous_run_evidence_omits_task_fragments(run_task, capsys):
    import json

    invoke, seen = run_task
    fragment = "/customers/SyntheticOnlyRunFragment42"
    assert invoke(f"Draft release notes and add {fragment} endpoint") == 0
    assert fragment.lower() not in capsys.readouterr().err.lower()
    evidence = seen[0][1].seat_routing
    assert fragment.lower() not in json.dumps(evidence).lower()
    assert evidence[0]["task_role"]["markers"] == ["backend-request:endpoint"]


def test_role_resolution_error_does_not_echo_arbitrary_diagnostic_text(run_task, monkeypatch, capsys):
    invoke, seen = run_task
    fragment = "/customers/SyntheticOnlyErrorPayload19"

    def unavailable(**kwargs):
        raise fleet_client_policy.FleetPolicyClientError("network", fragment)

    monkeypatch.setattr(fleet_client_policy, "resolve_policy", unavailable)
    assert invoke("Draft release notes") == 2
    assert fragment not in capsys.readouterr().err
    assert seen == []
