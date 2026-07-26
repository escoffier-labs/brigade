import json
from pathlib import Path

from brigade import aboyeur
from brigade import agents
from brigade import cli
from brigade import roster as roster_mod
from brigade.route_catalog import ROUTE_DECISION_CONFIDENCE, ROUTE_TEMPLATE_VERSION, route_brief
from brigade.route_receipts import (
    ROUTE_DECISION_SCHEMA_VERSION,
    admissible_seats,
    route_decision_payload,
    write_route_decision,
)


def _roster() -> roster_mod.Roster:
    return roster_mod.Roster(
        orchestrator="chef",
        agents={
            "chef": roster_mod.Agent("chef", "codex", "plan and synthesize"),
            "coder": roster_mod.Agent("coder", "codex", "write code"),
            "reviewer": roster_mod.Agent("reviewer", "codex", "review code"),
            "researcher": roster_mod.Agent(
                "researcher",
                None,
                "research",
                endpoint="http://example.invalid/v1",
                model="research-model",
            ),
        },
        max_workers=2,
        allow_models=("codex", "ollama:*"),
    )


def _write_run_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2) + "\n")


def test_route_brief_payload_includes_decision_metadata():
    payload = route_brief("implement the config loader helper", template="vertical-slice").payload()
    assert payload["confidence"] == ROUTE_DECISION_CONFIDENCE
    assert payload["template_version"] == ROUTE_TEMPLATE_VERSION


def test_route_decision_payload_reads_route_from_run_json():
    run_receipt = {
        "route": {
            "attached": True,
            "route": ["implement", "correctness-review", "verify"],
            "size": "S",
            "confidence": ROUTE_DECISION_CONFIDENCE,
            "template_version": ROUTE_TEMPLATE_VERSION,
        }
    }
    payload = route_decision_payload(run_receipt, _roster())
    assert payload == {
        "schema_version": ROUTE_DECISION_SCHEMA_VERSION,
        "chosen_route": ["implement", "correctness-review", "verify"],
        "confidence": ROUTE_DECISION_CONFIDENCE,
        "template_version": ROUTE_TEMPLATE_VERSION,
        "admissible_seats": ["coder", "researcher", "reviewer"],
    }


def test_route_decision_payload_no_route_nulls_route_fields():
    payload = route_decision_payload({"status": "ok"}, _roster())
    assert payload["chosen_route"] is None
    assert payload["confidence"] is None
    assert payload["template_version"] is None
    assert payload["admissible_seats"] == ["coder", "researcher", "reviewer"]


def test_route_decision_payload_malformed_route_nulls_route_fields():
    payload = route_decision_payload(
        {
            "route": {
                "attached": True,
                "route": "implement",
                "confidence": ROUTE_DECISION_CONFIDENCE,
                "template_version": ROUTE_TEMPLATE_VERSION,
            }
        },
        _roster(),
    )
    assert payload["chosen_route"] is None
    assert payload["confidence"] is None
    assert payload["template_version"] is None


def test_admissible_seats_includes_endpoint_backed_workers():
    seats = admissible_seats(_roster())
    assert seats == ["coder", "researcher", "reviewer"]


def test_write_route_decision_preserves_field_order(tmp_path):
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    _write_run_json(
        output_dir / "run.json",
        {
            "route": {
                "attached": True,
                "route": ["implement"],
                "size": "XS",
            }
        },
    )
    write_route_decision(output_dir, _roster())
    text = (output_dir / "route-decision.json").read_text()
    keys = ["schema_version", "chosen_route", "confidence", "template_version", "admissible_seats"]
    positions = [text.index(f'"{key}"') for key in keys]
    assert positions == sorted(positions)


def _write_roster(tmp_path: Path) -> None:
    (tmp_path / ".brigade").mkdir(parents=True)
    (tmp_path / ".brigade" / "roster.toml").write_text(
        """
orchestrator = "chef"

[agents.chef]
cli = "codex"
role = "plan"

[agents.coder]
cli = "codex"
role = "code"

[agents.reviewer]
cli = "codex"
role = "review"
"""
    )


def test_run_cli_writes_route_decision_for_routed_run(tmp_path, monkeypatch):
    _write_roster(tmp_path)
    output_dir = tmp_path / "out"
    monkeypatch.setattr(
        aboyeur.agents,
        "run_agent",
        lambda *args, **kwargs: agents.AgentResult(
            text=json.dumps({"assignments": [{"worker": "coder", "task": "implement"}]}),
            ok=True,
        ),
    )

    rc = cli.main(
        [
            "run",
            "implement the config loader helper",
            "--cwd",
            str(tmp_path),
            "--output-dir",
            str(output_dir),
            "--dry-run",
            "--no-code-graph",
            "--no-evidence",
            "--route-template",
            "vertical-slice",
        ]
    )

    assert rc == 0
    run_meta = json.loads((output_dir / "run.json").read_text())
    decision = json.loads((output_dir / "route-decision.json").read_text())
    assert decision["schema_version"] == ROUTE_DECISION_SCHEMA_VERSION
    assert decision["chosen_route"] == run_meta["route"]["route"]
    assert decision["confidence"] == ROUTE_DECISION_CONFIDENCE
    assert decision["template_version"] == ROUTE_TEMPLATE_VERSION
    assert run_meta["route"]["confidence"] == ROUTE_DECISION_CONFIDENCE
    assert run_meta["route"]["template_version"] == ROUTE_TEMPLATE_VERSION
    assert decision["admissible_seats"] == ["coder", "reviewer"]  # roster.toml has no endpoint seat


def test_run_cli_writes_route_decision_for_no_route_run(tmp_path, monkeypatch):
    _write_roster(tmp_path)
    output_dir = tmp_path / "out"
    monkeypatch.setattr(
        aboyeur.agents,
        "run_agent",
        lambda *args, **kwargs: agents.AgentResult(
            text=json.dumps({"assignments": [{"worker": "coder", "task": "inspect"}]}),
            ok=True,
        ),
    )

    rc = cli.main(
        [
            "run",
            "inspect the tree",
            "--cwd",
            str(tmp_path),
            "--output-dir",
            str(output_dir),
            "--dry-run",
            "--no-code-graph",
            "--no-evidence",
            "--no-route",
            "--route-template",
            "vertical-slice",
        ]
    )

    assert rc == 0
    assert "route" not in json.loads((output_dir / "run.json").read_text())
    decision = json.loads((output_dir / "route-decision.json").read_text())
    assert decision["schema_version"] == ROUTE_DECISION_SCHEMA_VERSION
    assert decision["chosen_route"] is None
    assert decision["confidence"] is None
    assert decision["template_version"] is None
    assert decision["admissible_seats"] == ["coder", "reviewer"]  # roster.toml has no endpoint seat


def test_run_cli_writes_route_decision_when_run_fails_after_routing(tmp_path, monkeypatch):
    _write_roster(tmp_path)
    output_dir = tmp_path / "out"

    def fail_after_routing(*args, **kwargs):
        run_path = kwargs["output_dir"] / "run.json"
        run_meta = json.loads(run_path.read_text())
        run_meta["route"] = {
            "attached": True,
            "route": ["implement", "verify"],
            "confidence": ROUTE_DECISION_CONFIDENCE,
            "template_version": ROUTE_TEMPLATE_VERSION,
        }
        _write_run_json(run_path, run_meta)
        raise RuntimeError("worker failed")

    monkeypatch.setattr(aboyeur, "run", fail_after_routing)

    rc = cli.main(
        [
            "run",
            "implement the config loader helper",
            "--cwd",
            str(tmp_path),
            "--output-dir",
            str(output_dir),
            "--dry-run",
            "--no-code-graph",
            "--no-evidence",
        ]
    )

    assert rc == 2
    decision = json.loads((output_dir / "route-decision.json").read_text())
    assert decision["chosen_route"] == ["implement", "verify"]
    assert decision["confidence"] == ROUTE_DECISION_CONFIDENCE
    assert decision["template_version"] == ROUTE_TEMPLATE_VERSION
