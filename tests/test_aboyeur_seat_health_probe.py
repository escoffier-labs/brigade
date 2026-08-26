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


class RecordingProbe:
    """Wrap a healthy fixture probe while recording probe_roster kwargs."""

    def __init__(self):
        self.kwargs: dict[str, object] = {}
        self._probe = SeatHealthProbe(adapter=FakeAdapter())

    def probe_roster(self, roster, **kwargs):
        self.kwargs = kwargs
        return self._probe.probe_roster(roster, **kwargs)


def test_run_passes_effective_sandbox_to_seat_health(monkeypatch, tmp_path):
    """#1173: --sandbox read-only must reach the hard-isolation preflight."""
    _stub_run_phases(monkeypatch)
    recording = RecordingProbe()
    monkeypatch.setattr(aboyeur.seat_health, "SeatHealthProbe", lambda **kwargs: recording)
    output_dir = tmp_path / "run-sandbox-override"

    assert (
        run_aboyeur_guarded(
            "build feature",
            _roster(),
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
            sandbox="read-only",
        )
        == 0
    )

    assert recording.kwargs["sandbox"] == "read-only"
    assert recording.kwargs["require_hard_isolation"] is False
    run_meta = json.loads((output_dir / "run.json").read_text())
    assert run_meta["health"]["effective_sandbox"] == "read-only"


def test_read_only_run_preflight_records_sandbox_and_hard_isolation(monkeypatch, tmp_path):
    _stub_run_phases(monkeypatch)
    recording = RecordingProbe()
    monkeypatch.setattr(aboyeur.seat_health, "SeatHealthProbe", lambda **kwargs: recording)
    output_dir = tmp_path / "run-readonly-preflight"

    assert (
        run_aboyeur_guarded(
            "build feature",
            _roster(),
            output_dir=output_dir,
            code_graph_enabled=False,
            route_enabled=False,
            read_only=True,
            sandbox="read-only",
        )
        == 0
    )

    assert recording.kwargs["require_hard_isolation"] is True
    assert recording.kwargs["sandbox"] == "read-only"


def test_sandbox_override_upgrades_soft_declaration_to_hard_isolation():
    """#1173: a soft roster declaration is judged against the runtime override."""
    roster = Roster(
        orchestrator="chef",
        agents={"chef": Agent("chef", "codex", "plan and synthesize")},
        max_workers=1,
        sandbox="workspace-write",
    )
    probe = SeatHealthProbe(collect_executable_version=False)

    declared = probe._default_check(
        "isolation-compatibility", roster.agents["chef"], roster, None, 1.0, False, require_hard_isolation=True
    )
    assert declared.status == "failed"
    assert declared.cause_code == "unsafe-isolation"

    overridden = probe._default_check(
        "isolation-compatibility",
        roster.agents["chef"],
        roster,
        None,
        1.0,
        False,
        require_hard_isolation=True,
        sandbox="read-only",
    )
    assert overridden.status == "passed"
