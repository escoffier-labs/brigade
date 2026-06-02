# tests/test_research_registry.py
from pathlib import Path
from brigade.research import registry as reg

def test_create_then_list_and_show(tmp_path: Path):
    rid = reg.create_run(tmp_path, question="what is X?", run_id="20260602-100000-x", caps={"max_rounds": 4})
    assert rid == "20260602-100000-x"
    runs = reg.list_runs(tmp_path)
    assert [r["run_id"] for r in runs] == [rid]
    rec = reg.show_run(tmp_path, rid)
    assert rec["status"] == "running"
    assert rec["question"] == "what is X?"
    assert rec["caps"]["max_rounds"] == 4

def test_checkpoint_roundtrip_and_resume(tmp_path: Path):
    rid = reg.create_run(tmp_path, question="q", run_id="20260602-100001-q", caps={})
    reg.save_checkpoint(tmp_path, rid, {"round": 2, "report": "r", "findings": [], "urls": ["u"], "queries": ["x"]})
    cp = reg.load_checkpoint(tmp_path, rid)
    assert cp["round"] == 2 and cp["urls"] == ["u"]

def test_status_transitions(tmp_path: Path):
    rid = reg.create_run(tmp_path, question="q", run_id="20260602-100002-q", caps={})
    reg.set_status(tmp_path, rid, "cancelled")
    assert reg.show_run(tmp_path, rid)["status"] == "cancelled"
    reg.finish_run(tmp_path, rid, status="done", stats={"rounds": 3}, artifacts={"report_html": "report.html"})
    rec = reg.show_run(tmp_path, rid)
    assert rec["status"] == "done" and rec["stats"]["rounds"] == 3
