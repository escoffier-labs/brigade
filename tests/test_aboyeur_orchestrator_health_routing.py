import json

from brigade import aboyeur
from brigade import agents
from brigade.roster import Agent, Roster
from brigade.seat_health import SeatHealthCheck, SeatHealthProbe
from tests.run_test_helpers import run_aboyeur_guarded


class PerSeatFakeAdapter:
    """Return per-seat health by failing or degrading the first check only."""

    def __init__(self, *, unhealthy: frozenset[str] = frozenset(), degraded: frozenset[str] = frozenset()):
        self.unhealthy = unhealthy
        self.degraded = degraded

    def check(self, name, *, seat, roster, workspace, timeout_seconds):
        if seat.name in self.unhealthy:
            return SeatHealthCheck(name, "failed", "auth required", cause_code="auth-required")
        if seat.name in self.degraded:
            return SeatHealthCheck(name, "degraded", "auth status unavailable", cause_code="auth-status-unavailable")
        return SeatHealthCheck(name, "passed", "ok")


class RaisingProbe:
    def probe_roster(self, *args, **kwargs):
        raise RuntimeError("probe failed")


def _roster(*, fallback: tuple[str, ...] = (), worker_fallback: tuple[str, ...] = ()):
    agents = {
        "chef": Agent("chef", "codex", "plan and synthesize", fallback=fallback),
        "chef-fallback": Agent("chef-fallback", "claude", "plan and synthesize"),
        "coder": Agent("coder", "codex", "write code", fallback=worker_fallback),
        "coder-fallback": Agent("coder-fallback", "claude", "write code"),
    }
    return Roster(orchestrator="chef", agents=agents, max_workers=1)


def _stub_run_phases(monkeypatch, *, plan_calls=None):
    if plan_calls is None:
        plan_calls = []

    def capture_plan(task, roster, **kwargs):
        plan_calls.append(roster)
        return [aboyeur.Assignment(worker="coder", task="implement")]

    monkeypatch.setattr(aboyeur, "plan", capture_plan)
    monkeypatch.setattr(
        aboyeur,
        "dispatch",
        lambda *args, **kwargs: [aboyeur.WorkerResult(worker="coder", task="implement", text="done", ok=True)],
    )
    monkeypatch.setattr(
        aboyeur,
        "_run_orchestrator",
        lambda *args, **kwargs: agents.AgentResult(text="final answer", ok=True),
    )
    return plan_calls


def _install_probe(monkeypatch, adapter: PerSeatFakeAdapter):
    monkeypatch.setattr(
        aboyeur.seat_health,
        "SeatHealthProbe",
        lambda **kwargs: SeatHealthProbe(adapter=adapter),
    )


def test_unhealthy_orchestrator_uses_healthy_declared_fallback(monkeypatch, tmp_path, capsys):
    plan_calls = _stub_run_phases(monkeypatch)
    output_dir = tmp_path / "run-fallback"
    _install_probe(monkeypatch, PerSeatFakeAdapter(unhealthy=frozenset({"chef"})))

    assert (
        run_aboyeur_guarded(
            "build feature",
            _roster(fallback=("chef-fallback",)),
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    assert len(plan_calls) == 1
    assert plan_calls[0].orchestrator == "chef-fallback"
    stderr = capsys.readouterr().err
    assert "warning: orchestrator chef is unhealthy [auth-required]; using declared fallback chef-fallback" in stderr

    routing = json.loads((output_dir / "seat-routing.json").read_text())
    assert routing["schema"] == "brigade.seat_routing.v1"
    assert routing["run_id"] == "run-fallback"
    assert routing["requested_orchestrator"] == "chef"
    assert routing["effective_orchestrator"] == "chef-fallback"
    assert routing["outcome"] == "fallback"
    assert routing["typed_cause"] == "auth-required"


def test_unhealthy_orchestrator_without_declared_fallback_aborts(monkeypatch, tmp_path, capsys):
    plan_calls = _stub_run_phases(monkeypatch)
    output_dir = tmp_path / "run-no-fallback"
    _install_probe(monkeypatch, PerSeatFakeAdapter(unhealthy=frozenset({"chef"})))

    assert (
        run_aboyeur_guarded(
            "build feature",
            _roster(),
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 2
    )

    assert plan_calls == []
    stderr = capsys.readouterr().err
    assert "error: orchestrator chef is unhealthy [auth-required]" in stderr

    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "failed"
    assert run_meta["failure"]["kind"] == "auth-required"
    expected = {
        "requested_seat": "chef",
        "effective_seat": None,
        "outcome": "abort",
        "typed_cause": "auth-required",
        "rejected_fallbacks": [],
    }
    # Chef and coder share a codex seat-health fingerprint, so an unhealthy chef can
    # cache through to coder and produce a legitimate skip decision alongside abort.
    assert expected in run_meta["seat_routing"]

    routing = json.loads((output_dir / "seat-routing.json").read_text())
    assert routing["outcome"] == "abort"
    assert routing["effective_orchestrator"] is None
    assert expected in routing["decisions"]


def test_unhealthy_orchestrator_with_unhealthy_declared_fallback_aborts(monkeypatch, tmp_path, capsys):
    plan_calls = _stub_run_phases(monkeypatch)
    output_dir = tmp_path / "run-bad-fallback"
    _install_probe(monkeypatch, PerSeatFakeAdapter(unhealthy=frozenset({"chef", "chef-fallback"})))

    assert (
        run_aboyeur_guarded(
            "build feature",
            _roster(fallback=("chef-fallback",)),
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 2
    )

    assert plan_calls == []
    stderr = capsys.readouterr().err
    assert "chef-fallback (unhealthy[auth-required])" in stderr

    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "failed"
    assert run_meta["failure"]["kind"] == "auth-required"

    routing = json.loads((output_dir / "seat-routing.json").read_text())
    assert routing["outcome"] == "abort"
    assert routing["rejected_fallbacks"] == [
        {"seat": "chef-fallback", "status": "unhealthy", "cause": "auth-required"},
    ]


def test_degraded_orchestrator_proceeds_without_reroute(monkeypatch, tmp_path, capsys):
    plan_calls = _stub_run_phases(monkeypatch)
    output_dir = tmp_path / "run-degraded"
    _install_probe(monkeypatch, PerSeatFakeAdapter(degraded=frozenset({"chef"})))

    assert (
        run_aboyeur_guarded(
            "build feature",
            _roster(fallback=("chef-fallback",)),
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    assert len(plan_calls) == 1
    assert plan_calls[0].orchestrator == "chef"
    stderr = capsys.readouterr().err
    assert "warning: orchestrator" not in stderr
    assert not (output_dir / "seat-routing.json").exists()


def test_probe_exception_proceeds_without_abort(monkeypatch, tmp_path, capsys):
    plan_calls = _stub_run_phases(monkeypatch)
    output_dir = tmp_path / "run-probe-exc"
    monkeypatch.setattr(aboyeur.seat_health, "SeatHealthProbe", lambda **kwargs: RaisingProbe())

    assert (
        run_aboyeur_guarded(
            "build feature",
            _roster(fallback=("chef-fallback",)),
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    assert len(plan_calls) == 1
    assert plan_calls[0].orchestrator == "chef"
    stderr = capsys.readouterr().err
    assert "error: orchestrator" not in stderr
    assert not (output_dir / "seat-routing.json").exists()

    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "ok"


def test_unhealthy_worker_routes_to_fallback_and_persists_decision(monkeypatch, tmp_path):
    seen = {}

    monkeypatch.setattr(
        aboyeur,
        "plan",
        lambda *args, **kwargs: [aboyeur.Assignment(worker="coder", task="implement")],
    )

    def capture_dispatch(assignments, roster, **kwargs):
        seen["agent"] = roster.agents["coder"]
        return [aboyeur.WorkerResult(worker="coder", task="implement", text="done", ok=True)]

    monkeypatch.setattr(aboyeur, "dispatch", capture_dispatch)
    monkeypatch.setattr(
        aboyeur,
        "_run_orchestrator",
        lambda *args, **kwargs: agents.AgentResult(text="final answer", ok=True),
    )
    _install_probe(monkeypatch, PerSeatFakeAdapter(unhealthy=frozenset({"coder"})))
    output_dir = tmp_path / "run-worker-fallback"

    assert (
        run_aboyeur_guarded(
            "build feature",
            _roster(worker_fallback=("coder-fallback",)),
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    assert seen["agent"].name == "coder"
    assert seen["agent"].cli == "claude"
    expected = {
        "requested_seat": "coder",
        "effective_seat": "coder-fallback",
        "outcome": "fallback",
        "typed_cause": "auth-required",
        "rejected_fallbacks": [],
    }
    routing = json.loads((output_dir / "seat-routing.json").read_text())
    assert routing["decisions"] == [expected]
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["seat_routing"] == [expected]
    assert run_meta["health"]["schema"] == "brigade.seat_health_summary.v1"
    assert run_meta["health"]["fallbacks_selected"] == 1
    assert run_meta["health"]["unhealthy"] >= 1


def test_healthy_worker_path_has_no_routing_decisions(monkeypatch, tmp_path):
    _stub_run_phases(monkeypatch)
    _install_probe(monkeypatch, PerSeatFakeAdapter())
    output_dir = tmp_path / "run-worker-healthy"

    assert (
        run_aboyeur_guarded(
            "build feature",
            _roster(worker_fallback=("coder-fallback",)),
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    assert not (output_dir / "seat-routing.json").exists()
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert "seat_routing" not in run_meta
    assert run_meta["health"]["schema"] == "brigade.seat_health_summary.v1"
    assert run_meta["health"]["fallbacks_selected"] == 0


def test_unhealthy_direct_worker_without_fallback_aborts(monkeypatch, tmp_path, capsys):
    _install_probe(monkeypatch, PerSeatFakeAdapter(unhealthy=frozenset({"coder"})))
    output_dir = tmp_path / "run-direct-unhealthy"

    assert (
        run_aboyeur_guarded(
            "build feature",
            _roster(),
            worker="coder",
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 2
    )

    stderr = capsys.readouterr().err
    assert "error: seat coder is unhealthy [auth-required]" in stderr
    assert "no declared fallbacks" in stderr

    expected = {
        "requested_seat": "coder",
        "effective_seat": None,
        "outcome": "abort",
        "typed_cause": "auth-required",
        "rejected_fallbacks": [],
    }
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "failed"
    assert run_meta["failure"]["kind"] == "auth-required"
    assert run_meta["failure"]["seat"] == "coder"
    assert run_meta["seat_routing"] == [expected]

    routing = json.loads((output_dir / "seat-routing.json").read_text())
    assert routing["decisions"] == [expected]


def test_direct_worker_fallback_prints_requested_and_effective(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        aboyeur,
        "dispatch",
        lambda assignments, roster, **kwargs: [
            aboyeur.WorkerResult(worker="coder", task="build feature", text="done", ok=True)
        ],
    )
    monkeypatch.setattr(
        aboyeur,
        "_run_orchestrator",
        lambda *args, **kwargs: agents.AgentResult(text="final answer", ok=True),
    )
    _install_probe(monkeypatch, PerSeatFakeAdapter(unhealthy=frozenset({"coder"})))
    output_dir = tmp_path / "run-direct-fallback"

    assert (
        run_aboyeur_guarded(
            "build feature",
            _roster(worker_fallback=("coder-fallback",)),
            worker="coder",
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    stderr = capsys.readouterr().err
    assert "requested worker coder; effective seat coder-fallback [auth-required]" in stderr
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["health"]["fallbacks_selected"] == 1


def test_direct_worker_run_ignores_unhealthy_orchestrator(monkeypatch, tmp_path, capsys):
    """#1174: a pinned worker must not abort on an orchestrator it never uses."""
    dispatch_calls = []
    monkeypatch.setattr(
        aboyeur,
        "dispatch",
        lambda assignments, roster, **kwargs: (
            dispatch_calls.append([assignment.worker for assignment in assignments]),
            [aboyeur.WorkerResult(worker="coder", task="build feature", text="done", ok=True)],
        )[1],
    )
    monkeypatch.setattr(
        aboyeur,
        "_run_orchestrator",
        lambda *args, **kwargs: agents.AgentResult(text="final answer", ok=True),
    )
    # Distinct CLIs so the codex seat-health cache cannot carry chef's failure
    # over to the coder seat.
    roster = Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "claude", "plan and synthesize"),
            "coder": Agent("coder", "codex", "write code"),
        },
        max_workers=1,
    )
    _install_probe(monkeypatch, PerSeatFakeAdapter(unhealthy=frozenset({"chef"})))
    output_dir = tmp_path / "run-direct-ignores-orchestrator"

    assert (
        run_aboyeur_guarded(
            "build feature",
            roster,
            worker="coder",
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    stderr = capsys.readouterr().err
    assert "error:" not in stderr
    assert dispatch_calls == [["coder"]]
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "ok"
    for decision in run_meta.get("seat_routing", []):
        assert decision["requested_seat"] != "chef"
