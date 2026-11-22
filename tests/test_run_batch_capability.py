"""Capability selection: one admitted roster seat per batch task, resolved once."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import Mock

import pytest

from brigade import aboyeur, agents, cli, run_batch, run_batch_capability
from brigade import roster as roster_mod
from brigade.run_batch import SCHEMA
from tests._home import set_home

CAPABILITY = "review.code"
SENTINEL = "SENTINEL-task-text-must-never-pick-a-seat"

ROSTER = f"""orchestrator = "chef"
[limits]
max_workers = 2
[agents.chef]
cli = "codex"
role = "plan"
[agents.coder]
cli = "codex"
role = "implement"
capabilities = ["{CAPABILITY}"]
[agents.reviewer]
cli = "codex"
role = "review"
capabilities = ["{CAPABILITY}"]
"""

ROSTER_REORDERED = f"""orchestrator = "chef"
[limits]
max_workers = 2
[agents.chef]
cli = "codex"
role = "plan"
[agents.reviewer]
cli = "codex"
role = "review"
capabilities = ["{CAPABILITY}"]
[agents.coder]
cli = "codex"
role = "implement"
capabilities = ["{CAPABILITY}"]
"""


@dataclass
class Site:
    root: Path
    home: Path
    calls: list[str] = field(default_factory=list)

    def roster(self, text: str) -> None:
        (self.root / ".brigade" / "roster.toml").write_text(text)

    def run_dir(self, request: str = "req") -> Path:
        return self.root / ".brigade" / "runs" / ("batch-" + hashlib.sha256(request.encode()).hexdigest())


@pytest.fixture
def site(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".brigade").mkdir()
    calls: list[str] = []
    site = Site(root=root, home=home, calls=calls)
    site.roster(ROSTER)
    # A batch never asks the planner or the orchestrator for a seat.
    monkeypatch.setattr(aboyeur.planning, "plan", Mock(side_effect=AssertionError("planner called")))
    monkeypatch.setattr(aboyeur.planning, "_run_orchestrator", Mock(side_effect=AssertionError("synthesis called")))

    def model(cli_ref, prompt, **kwargs):
        calls.append(prompt)
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    return site


def task(name: str, *, capability: str = CAPABILITY, read_only: bool = True, seat: str | None = None) -> dict:
    row = {"task_id": name, "text": SENTINEL, "read_only": read_only}
    if seat is None:
        row["capability"] = capability
    else:
        row["seat"] = seat
    return row


def invoke(site: Site, capsys, tasks: list[dict], *, request: str = "req"):
    path = site.home / f"{request}.json"
    path.write_text(json.dumps({"schema": SCHEMA, "request_id": request, "tasks": tasks}))
    rc = cli.main(["run", "batch", str(path), "--cwd", str(site.root), "--json"])
    captured = capsys.readouterr()
    assert captured.out, captured.err
    return rc, json.loads(captured.out), captured.err


def _marker(site: Site, request: str = "req") -> dict:
    return json.loads((site.run_dir(request) / "batch-request.json").read_text())


def _stages(site: Site, request: str = "req") -> list[int]:
    return [row["stage"] for row in json.loads((site.run_dir(request) / "plan.json").read_text())["assignments"]]


def _workers(site: Site, request: str = "req") -> list[str]:
    """The seats that actually dispatched, from the retained worker results."""
    payload = json.loads((site.run_dir(request) / "worker-results.json").read_text())
    return [row["worker"] for row in payload["results"]]


def test_capability_resolves_to_the_first_eligible_seat_in_roster_order(site, capsys):
    rc, result, err = invoke(site, capsys, [task("a")])
    assert rc == 0, err
    assert _workers(site) == ["coder"]
    assert result["tasks"]["a"]["selection"] == {"kind": "capability", "capability": CAPABILITY}
    marker = _marker(site)
    assert [
        (row["task_id"], row["capability"], row["seat"], row["candidates"]) for row in marker["capability_resolutions"]
    ] == [("a", CAPABILITY, "coder", 2)]

    # Roster order alone decides: the same request on a reordered roster picks
    # the seat that now comes first.
    site.roster(ROSTER_REORDERED)
    rc, result, err = invoke(site, capsys, [task("a")], request="req-2")
    assert rc == 0, err
    assert _workers(site, "req-2") == ["reviewer"]
    assert result["tasks"]["a"]["status"] == "completed"


def test_capability_skips_a_seat_that_cannot_enforce_read_only(site, capsys):
    site.roster(
        f"""orchestrator = "chef"
[agents.chef]
cli = "codex"
role = "plan"
[agents.soft]
cli = "opencode"
role = "review"
capabilities = ["{CAPABILITY}"]
[agents.coder]
cli = "codex"
role = "implement"
capabilities = ["{CAPABILITY}"]
"""
    )
    rc, result, err = invoke(site, capsys, [task("a", read_only=True)])
    assert rc == 0, err
    assert result["tasks"]["a"]["selection"] == {"kind": "capability", "capability": CAPABILITY}
    assert _workers(site) == ["coder"]
    assert _marker(site)["tasks"][0]["seat"] == "coder"


def test_capability_skips_a_seat_the_model_policy_denies():
    from tests.test_fleet_model_admission import _versioned_seat, _versioned_snapshot

    roster = roster_mod.Roster(
        orchestrator="chef",
        agents={
            "chef": roster_mod.Agent("chef", "codex", "plan"),
            "coder": roster_mod.Agent("coder", "codex", "implement", capabilities=(CAPABILITY,)),
            "reviewer": roster_mod.Agent("reviewer", "codex", "review", capabilities=(CAPABILITY,)),
        },
    )
    plan = {"request_id": "req", "tasks": [task("a")]}
    snapshot = _versioned_snapshot(
        _versioned_seat("coder", "openai", "gpt-6.1-sol", instance_id="codex", enabled=False),
        _versioned_seat("reviewer", "openai", "gpt-6.1-sol", instance_id="codex"),
    )
    resolved = run_batch_capability.resolve(plan, roster, snapshot=snapshot)
    assert resolved["tasks"][0]["seat"] == "reviewer"


def test_no_eligible_seat_refuses_the_batch_before_any_launch(site, capsys):
    rc, result, err = invoke(site, capsys, [task("a", capability="review.audit")])
    assert rc == 2
    assert result["error"]["code"] == "capability-unavailable"
    assert "task a" in result["error"]["message"] and "review.audit" in result["error"]["message"]
    assert SENTINEL not in json.dumps(result) and SENTINEL not in err
    assert site.calls == []
    assert not (site.run_dir() / "batch-journal-enrolled.json").exists()


@pytest.mark.parametrize(
    ("row", "code"),
    [
        ({**task("a"), "seat": "coder"}, "invalid-selection"),
        ({"task_id": "a", "text": SENTINEL}, "invalid-selection"),
        (task("a", capability="Review.Code"), "invalid-capability"),
        (task("a", capability=""), "invalid-capability"),
    ],
)
def test_a_task_names_exactly_one_selector(row, code):
    with pytest.raises(run_batch.BatchError) as caught:
        run_batch.validate_batch({"schema": SCHEMA, "request_id": "req", "tasks": [row]})
    assert caught.value.code == code


def test_fingerprint_is_unchanged_for_seat_batches_and_moves_for_capability():
    golden = {
        "schema": SCHEMA,
        "request_id": "request-1",
        "tasks": [
            {"task_id": "b", "text": "café \U0001f600\n", "seat": "coder", "depends_on": ["a"]},
            {"task_id": "a", "text": "Inspect", "seat": "coder"},
        ],
    }
    digest = "d69a5ad741c986a250fa6e9d2089a2550a5dc334de268ec9ee94dc76d53e1d48"
    assert run_batch.request_fingerprint(golden) == digest
    canonical = run_batch.validate_batch(golden)
    assert all("capability" not in row and "seat" in row for row in canonical["tasks"])

    capability = {"task_id": "a", "text": "Inspect", "capability": CAPABILITY}
    seat = {"task_id": "a", "text": "Inspect", "seat": "coder"}
    by_capability = run_batch.request_fingerprint({"schema": SCHEMA, "request_id": "r", "tasks": [capability]})
    by_seat = run_batch.request_fingerprint({"schema": SCHEMA, "request_id": "r", "tasks": [seat]})
    assert by_capability != by_seat
    other = run_batch.request_fingerprint(
        {"schema": SCHEMA, "request_id": "r", "tasks": [{**capability, "capability": "review.audit"}]}
    )
    assert other != by_capability


def test_retry_reuses_the_persisted_seat_after_the_roster_reorders(site, capsys, monkeypatch):
    from brigade.cli import run

    original = run.dispatch
    monkeypatch.setattr(run, "dispatch", Mock(return_value=2))
    rc, _, err = invoke(site, capsys, [task("a")])
    assert rc == 2, err
    assert site.calls == []
    assert _marker(site)["capability_resolutions"][0]["seat"] == "coder"

    # A reordered roster would resolve the other seat; the accepted request keeps
    # the seat its journal already binds.
    site.roster(ROSTER_REORDERED)
    monkeypatch.setattr(run, "dispatch", original)
    rc, result, err = invoke(site, capsys, [task("a")])
    assert rc == 0, err
    assert _workers(site) == ["coder"]
    assert _marker(site)["capability_resolutions"][0]["seat"] == "coder"
    assert result["tasks"]["a"]["selection"] == {"kind": "capability", "capability": CAPABILITY}


def test_a_removed_seat_refuses_the_retry_instead_of_resolving_again(site, capsys, monkeypatch):
    from brigade.cli import run

    original = run.dispatch
    monkeypatch.setattr(run, "dispatch", Mock(return_value=2))
    assert invoke(site, capsys, [task("a")])[0] == 2
    site.roster(
        ROSTER.replace(
            f"""[agents.coder]
cli = "codex"
role = "implement"
capabilities = ["{CAPABILITY}"]
""",
            "",
        )
    )
    monkeypatch.setattr(run, "dispatch", original)
    rc, result, err = invoke(site, capsys, [task("a")])
    assert rc == 2
    assert result["error"]["code"] == "capability-seat-missing"
    assert "coder" in result["error"]["message"]
    assert site.calls == []


def test_dry_run_plans_a_capability_without_a_roster(site, capsys):
    (site.root / ".brigade" / "roster.toml").unlink()
    path = site.home / "dry.json"
    path.write_text(json.dumps({"schema": SCHEMA, "request_id": "dry", "tasks": [task("a")]}))
    assert cli.main(["run", "batch", str(path), "--cwd", str(site.root), "--dry-run", "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["tasks"] == [
        {"task_id": "a", "capability": CAPABILITY, "depends_on": [], "isolation": "shared", "read_only": True}
    ]
    assert cli.main(["run", "batch", str(path), "--cwd", str(site.root), "--dry-run"]) == 0
    assert f"a (capability:{CAPABILITY})" in capsys.readouterr().out
    assert not (site.root / ".brigade" / "runs").exists()
    assert site.calls == []


def test_a_missing_roster_refuses_rather_than_crashing(site, capsys):
    (site.root / ".brigade" / "roster.toml").unlink()
    rc, result, err = invoke(site, capsys, [task("a")])
    assert rc == 2
    assert result["error"]["code"] == "roster-unavailable"
    assert site.calls == []
    assert SENTINEL not in err


def test_two_capability_tasks_on_one_seat_split_waves_by_remaining_capacity(site, capsys, monkeypatch):
    active = 0
    peak = 0
    lock = threading.Lock()

    def model(cli_ref, prompt, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        site.calls.append(prompt)
        with lock:
            active -= 1
        return agents.AgentResult(text="done", ok=True)

    monkeypatch.setattr(agents, "run_agent", model)
    monkeypatch.setattr(run_batch_capability, "remaining_capacity", lambda roster: {"coder": 1})
    rc, result, err = invoke(site, capsys, [task("a"), task("b")])
    assert rc == 0, err
    # Both resolved to the one capable seat and share a dependency layer, yet
    # only one ran at a time.
    assert {row["seat"] for row in _marker(site)["capability_resolutions"]} == {"coder"}
    assert _stages(site) == [0, 0]
    assert peak == 1
    assert len(_workers(site)) == 2


def test_wave_building_caps_one_seat_by_its_known_capacity():
    from brigade.run_transport import Assignment

    assignments = [Assignment(worker="coder", task=f"task-{index}", stage=0) for index in range(3)]
    assert [a.stage for a in run_batch_capability.build_waves(assignments, 4, {})] == [0, 0, 0]
    assert [a.stage for a in run_batch_capability.build_waves(assignments, 2, {})] == [0, 0, 1]
    assert [a.stage for a in run_batch_capability.build_waves(assignments, 4, {"coder": 1})] == [0, 1, 2]
    # A different seat's cap never delays this one, and order is preserved.
    mixed = [
        Assignment(worker="coder", task="first", stage=0),
        Assignment(worker="reviewer", task="second", stage=0),
        Assignment(worker="coder", task="third", stage=0),
    ]
    capped = run_batch_capability.build_waves(mixed, 4, {"coder": 1})
    assert [(a.task, a.stage) for a in capped] == [("first", 0), ("second", 0), ("third", 1)]


def test_remaining_capacity_needs_an_enrolled_fleet_that_answers(monkeypatch):
    from brigade import fleet_client, fleet_client_policy, fleet_session_bootstrap
    from tests.test_roster_readiness import _enrolled_policy_document

    roster = roster_mod.Roster(orchestrator="chef", agents={"coder": roster_mod.Agent("coder", "codex", "implement")})
    monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", lambda **kwargs: {"state": "authoritative"})
    monkeypatch.setattr(fleet_session_bootstrap, "classify_enrollment", lambda snapshot, **kwargs: "unenrolled")
    assert run_batch_capability.remaining_capacity(roster) == {}

    monkeypatch.setattr(fleet_session_bootstrap, "classify_enrollment", lambda snapshot, **kwargs: "enrolled")
    document = _enrolled_policy_document(concurrency=1)
    document["seats"]["coder"] = document["seats"].pop("worker")
    monkeypatch.setattr(fleet_client_policy, "show_policy", lambda **kwargs: {"document": document})
    monkeypatch.setattr(fleet_client_policy, "read_seat_usage", lambda: {"seats": {"coder": {"occupied": 0}}})
    assert run_batch_capability.remaining_capacity(roster) == {"coder": 1}

    monkeypatch.setattr(fleet_client_policy, "read_seat_usage", lambda: {"seats": {"coder": {"occupied": 1}}})
    # A full seat adds no scheduling cap; the launch-time lease is the refusal.
    assert run_batch_capability.remaining_capacity(roster) == {}


def test_output_reports_the_selection_of_every_task(site, capsys):
    rc, result, err = invoke(site, capsys, [task("a"), task("b", seat="reviewer"), task("c", seat="coder")])
    assert rc == 0, err
    assert result["tasks"]["a"]["selection"] == {"kind": "capability", "capability": CAPABILITY}
    assert result["tasks"]["c"]["selection"] == {"kind": "seat", "capability": None}

    run_dir = site.run_dir()
    assert cli.main(["runs", "tasks", "show", str(run_dir), "a", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["task"]["selection"] == {"kind": "capability", "capability": CAPABILITY}
    assert cli.main(["runs", "tasks", "show", str(run_dir), "c", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["task"]["selection"] == {"kind": "seat", "capability": None}
    assert cli.main(["runs", "tasks", "show", str(run_dir), "a"]) == 0
    assert f"selection: capability {CAPABILITY}" in capsys.readouterr().out


def test_no_planner_call_is_made_for_a_capability_batch(site, capsys):
    planner = aboyeur.planning.plan
    rc, _result, err = invoke(site, capsys, [task("a"), task("b")])
    assert rc == 0, err
    assert planner.call_count == 0
    assert len(site.calls) == 2
    # The task capability selects a seat; it is not a tool gate on the assignment.
    assignments = json.loads((site.run_dir() / "plan.json").read_text())["assignments"]
    assert all("capabilities" not in row for row in assignments)
