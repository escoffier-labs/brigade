import json

from brigade import aboyeur
from brigade import agents
from brigade.roster import Agent, Roster
from brigade.seat_health import SeatHealthCheck, SeatHealthProbe
from tests.run_test_helpers import run_aboyeur_guarded


class FakeAdapter:
    def __init__(self, *, on_check=None):
        self.on_check = on_check

    def check(self, name, *, seat, roster, workspace, timeout_seconds):
        if self.on_check is not None:
            self.on_check(name, seat=seat, roster=roster, workspace=workspace)
        return SeatHealthCheck(name, "passed", "ok")


class RaisingProbe:
    def probe_roster(self, *args, **kwargs):
        raise RuntimeError("probe failed")


def _roster():
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan and synthesize"),
            "coder": Agent("coder", "codex", "write code"),
        },
        max_workers=1,
    )


def _stub_run_phases(monkeypatch):
    monkeypatch.setattr(
        aboyeur,
        "plan",
        lambda *args, **kwargs: [aboyeur.Assignment(worker="coder", task="implement")],
    )
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


def test_run_writes_seat_health_receipt(monkeypatch, tmp_path):
    _stub_run_phases(monkeypatch)
    output_dir = tmp_path / "run-abc"
    monkeypatch.setattr(
        aboyeur.seat_health,
        "SeatHealthProbe",
        lambda **kwargs: SeatHealthProbe(adapter=FakeAdapter()),
    )

    assert (
        run_aboyeur_guarded(
            "build feature",
            _roster(),
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    receipt = json.loads((output_dir / "seat-health.json").read_text())
    assert receipt["schema"] == "brigade.seat_health.v1"
    assert receipt["run_id"] == "run-abc"
    assert receipt["results"]


def test_probe_failure_still_receipts_without_crashing(monkeypatch, tmp_path):
    _stub_run_phases(monkeypatch)
    output_dir = tmp_path / "run-fail"
    monkeypatch.setattr(aboyeur.seat_health, "SeatHealthProbe", lambda **kwargs: RaisingProbe())

    assert (
        run_aboyeur_guarded(
            "build feature",
            _roster(),
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["status"] == "ok"
    receipt = json.loads((output_dir / "seat-health.json").read_text())
    assert receipt["run_id"] == "run-fail"
    # Every declared seat must appear. A reader cannot distinguish a missing row from a
    # healthy one, so a blown-up probe records the whole chain rather than one seat.
    assert {result["seat"] for result in receipt["results"]} == {"chef", "coder"}
    assert all(result["status"] == "unhealthy" for result in receipt["results"])
    assert all(result["checks"][0]["cause_code"] == "probe-exception" for result in receipt["results"])


def test_seat_health_probe_runs_after_started_run_json(monkeypatch, tmp_path):
    _stub_run_phases(monkeypatch)
    output_dir = tmp_path / "run-order"
    observed = []

    def on_check(name, *, seat, roster, workspace):
        run_path = output_dir / "run.json"
        observed.append(run_path.is_file())
        if run_path.is_file():
            observed.append(json.loads(run_path.read_text())["status"])

    monkeypatch.setattr(
        aboyeur.seat_health,
        "SeatHealthProbe",
        lambda **kwargs: SeatHealthProbe(adapter=FakeAdapter(on_check=on_check)),
    )

    assert (
        run_aboyeur_guarded(
            "build feature",
            _roster(),
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        == 0
    )

    assert observed
    assert all(flag is True for flag in observed if isinstance(flag, bool))
    assert "started" in observed
