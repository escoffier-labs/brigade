# tests/test_research_registry.py
from __future__ import annotations

import json
from pathlib import Path

from brigade.research import registry
from brigade.roster import Agent, Roster


def test_create_then_list_and_show(tmp_path: Path):
    rid = registry.create_run(tmp_path, question="what is X?", run_id="20260602-100000-x", caps={"max_rounds": 4})
    assert rid == "20260602-100000-x"
    runs = registry.list_runs(tmp_path)
    assert [r["run_id"] for r in runs] == [rid]
    rec = registry.show_run(tmp_path, rid)
    assert rec["status"] == "running"
    assert rec["question"] == "what is X?"
    assert rec["caps"]["max_rounds"] == 4


def test_checkpoint_roundtrip_and_resume(tmp_path: Path):
    rid = registry.create_run(tmp_path, question="q", run_id="20260602-100001-q", caps={})
    registry.save_checkpoint(
        tmp_path, rid, {"round": 2, "report": "r", "findings": [], "urls": ["u"], "queries": ["x"]}
    )
    cp = registry.load_checkpoint(tmp_path, rid)
    assert cp["round"] == 2 and cp["urls"] == ["u"]


def test_status_transitions(tmp_path: Path):
    rid = registry.create_run(tmp_path, question="q", run_id="20260602-100002-q", caps={})
    registry.set_status(tmp_path, rid, "cancelled")
    assert registry.show_run(tmp_path, rid)["status"] == "cancelled"
    registry.finish_run(
        tmp_path, rid, status="done", stats={"rounds": 3}, artifacts={"report_html": "report.html"}
    )
    rec = registry.show_run(tmp_path, rid)
    assert rec["status"] == "done" and rec["stats"]["rounds"] == 3


def test_update_research_fails_closed_on_corrupt_artifact(tmp_path: Path) -> None:
    run_id = "r-corrupt"
    path = registry.standard_run_dir(tmp_path, run_id) / registry.RESEARCH_ARTIFACT
    path.parent.mkdir(parents=True)
    path.write_text("{not-json", encoding="utf-8")

    try:
        registry.update_research(tmp_path, run_id, current_phase="extracting")
    except ValueError as exc:
        assert "unreadable" in str(exc) or "not an object" in str(exc)
    else:
        raise AssertionError("expected ValueError for corrupt research.json")
    assert path.read_text(encoding="utf-8") == "{not-json"


def test_update_research_fails_closed_on_non_object_artifact(tmp_path: Path) -> None:
    run_id = "r-non-object"
    path = registry.standard_run_dir(tmp_path, run_id) / registry.RESEARCH_ARTIFACT
    path.parent.mkdir(parents=True)
    path.write_text("[]\n", encoding="utf-8")

    try:
        registry.update_research(tmp_path, run_id, current_phase="extracting")
    except ValueError as exc:
        assert "unreadable" in str(exc) or "not an object" in str(exc)
    else:
        raise AssertionError("expected ValueError for non-object research.json")
    assert path.read_text(encoding="utf-8") == "[]\n"


def test_create_standard_research_run_and_dual_read(tmp_path: Path) -> None:
    roster = Roster(
        orchestrator="chef",
        agents={"chef": Agent(name="chef", cli="codex", role="orchestrator")},
    )
    record = registry.create_standard_run(
        tmp_path,
        run_id="r-new",
        question="What changed?",
        profile="grounded",
        caps={"max_time": 300, "max_dispatches": 12},
        roster=roster,
    )
    legacy = tmp_path / ".brigade" / "research" / "r-old"
    legacy.mkdir(parents=True)
    (legacy / "run.json").write_text(
        json.dumps({"run_id": "r-old", "question": "Old", "status": "done"}),
        encoding="utf-8",
    )

    assert record["schema"] == "brigade.research.v1"
    assert json.loads((tmp_path / ".brigade/runs/r-new/run.json").read_text())["kind"] == "research"
    listed = registry.list_runs(tmp_path)
    assert [(item["run_id"], item["legacy"]) for item in listed] == [
        ("r-new", False),
        ("r-old", True),
    ]
