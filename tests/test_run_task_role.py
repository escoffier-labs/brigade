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
    monkeypatch.setattr(fleet_client_policy, "show_policy", lambda: {"document": {"seats": {}}})
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


def test_policy_fallback_replaces_local_and_drops_unusable(run_task, monkeypatch):
    invoke, seen = run_task
    monkeypatch.setattr(
        fleet_client_policy,
        "show_policy",
        lambda: {"document": {"seats": {"author": {"fallback": ["coder", "missing"]}}}},
    )
    original = run_cli._direct_worker_error
    monkeypatch.setattr(
        run_cli,
        "_direct_worker_error",
        lambda seat, *a, **kw: "denied" if seat == "missing" else original(seat, *a, **kw),
    )
    assert invoke("Draft a blog post") == 0
    roster = seen[0][1]
    assert roster.agents["author"].fallback == ("coder",)
    assert roster.agents["coder"].fallback == ()
    assert roster.agents["coder"].invalid_final_fallback is None
    assert roster.seat_routing[0]["task_role"]["fallback"] == ["coder"]


@pytest.mark.parametrize("role,task", [("writing", "Draft a blog post"), ("frontend", "Redesign the login page")])
def test_role_instructions_preserve_detached_original(run_task, tmp_path, role, task):
    import hashlib
    from brigade import aboyeur as module

    invoke, seen = run_task
    rules = tmp_path / "rules.md"
    rules.write_text("Use concrete examples.")
    with (tmp_path / "roster.toml").open("a") as stream:
        stream.write(f'\n[task_roles.{role}]\ninstructions_file = "{rules}"\n')
    assert invoke(task, "--worker", "author", "--applied-preference", "--task-role-workload", role) == 0
    original, roster, _ = seen[0]
    assert original == task
    assert roster.worker_instructions == rules.read_text()
    evidence = roster.seat_routing[0]["task_role"]["instructions"]
    assert evidence == {"file": "rules.md", "sha256": hashlib.sha256(rules.read_bytes()).hexdigest()[:12]}
    for seat in ("author", "coder"):
        prompt = module._worker_prompt(
            roster.agents[seat], module.Assignment(worker=seat, task=task), instructions=roster.worker_instructions
        )
        assert prompt.startswith("Use concrete examples.\n\n")
        assert task in prompt


@pytest.mark.parametrize("task,extra", [("Write a function", ()), ("Draft a blog post", ("--worker", "coder"))])
def test_instructions_do_not_apply_to_general_or_explicit_worker(run_task, tmp_path, task, extra):
    invoke, seen = run_task
    with (tmp_path / "roster.toml").open("a") as stream:
        stream.write('\n[task_roles.writing]\ninstructions_file = "missing-rules.md"\n')
    assert invoke(task, *extra) == 0
    assert seen[0][1].worker_instructions == ""


@pytest.mark.parametrize("mode", ["missing", "oversize", "directory", "invalid-utf8"])
def test_instructions_fail_closed(run_task, tmp_path, capsys, mode):
    invoke, seen = run_task
    path = tmp_path / "rules.md"
    if mode == "oversize":
        path.write_bytes(b"x" * 65537)
    elif mode == "directory":
        path.mkdir()
    elif mode == "invalid-utf8":
        path.write_bytes(b"\xff")
    with (tmp_path / "roster.toml").open("a") as stream:
        stream.write(f'\n[task_roles.writing]\ninstructions_file = "{path}"\n')
    assert invoke("Draft a blog post") == 2
    assert seen == []
    assert "instructions file 'rules.md' is unreadable or exceeds 64 KiB" in capsys.readouterr().err


def test_resolved_policy_fallback_synthesizes_hub_seat(run_task, monkeypatch):
    from brigade.roster import Agent

    invoke, seen = run_task
    monkeypatch.setattr(
        fleet_client_policy,
        "resolve_policy",
        lambda **kw: {
            "effective": {"roles": {"writing": "author"}},
            "seats": {"author": {"fallback": ["remote-author"]}},
        },
    )

    def no_fetch():
        raise AssertionError("resolved policy seats must take precedence")

    monkeypatch.setattr(fleet_client_policy, "show_policy", no_fetch)
    monkeypatch.setattr(
        run_cli,
        "_hub_synthesized_agent",
        lambda seat, roster: (Agent(name=seat, cli="claude", role="code", fallback=("coder",)), None),
    )
    assert invoke("Draft a blog post") == 0
    roster = seen[0][1]
    assert roster.agents["author"].fallback == ("remote-author",)
    assert roster.agents["remote-author"].fallback == ()
    assert roster.seat_routing[0]["task_role"]["fallback"] == ["remote-author"]


def test_role_prompt_dispatch_builder_covers_fallback(run_task, monkeypatch, tmp_path):
    from brigade.aboyeur import planning
    from brigade import run_transport

    invoke, seen = run_task
    rules = tmp_path / "rules.md"
    rules.write_text("R" * 65536)
    with (tmp_path / "roster.toml").open("a") as stream:
        stream.write(f'\n[task_roles.writing]\ninstructions_file = "{rules}"\n')
    monkeypatch.setattr(
        fleet_client_policy, "show_policy", lambda: {"document": {"seats": {"author": {"fallback": ["coder"]}}}}
    )
    assert invoke("Draft a blog post") == 0
    roster = seen[0][1]

    def dispatch(assignments, effective, *, build_prompt, **kwargs):
        for name in ("author", *effective.agents["author"].fallback):
            prompt = build_prompt(
                effective.agents[name], run_transport.Assignment(worker=name, task="Draft a blog post")
            )
            assert prompt.startswith("R" * 65536 + "\n\n")
        return []

    monkeypatch.setattr(run_transport, "dispatch", dispatch)
    assert planning.dispatch([], roster, cwd=tmp_path) == []
