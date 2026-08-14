# tests/test_research_registry.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

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
    registry.finish_run(tmp_path, rid, status="done", stats={"rounds": 3}, artifacts={"report_html": "report.html"})
    rec = registry.show_run(tmp_path, rid)
    assert rec["status"] == "done" and rec["stats"]["rounds"] == 3


def test_update_phase_selective_reset_preserves_repair_and_clears_publishing(
    tmp_path: Path,
) -> None:
    run_id = registry.create_run(tmp_path, question="q", run_id="phase-reset-repair", caps={})
    registry.update_phase(tmp_path, run_id, "repair", status="completed", reset_downstream=False)
    registry.update_phase(tmp_path, run_id, "publishing", status="completed", reset_downstream=False)

    registry.update_phase(
        tmp_path,
        run_id,
        "review",
        status="running",
        reset_downstream=["publishing"],
    )

    phases = registry.show_run(tmp_path, run_id)["phases"]
    assert phases["repair"]["status"] == "completed"
    assert phases["publishing"]["status"] == "pending"
    assert phases["review"]["status"] == "running"


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


def _sidecar_lock_held(target: Path, run_id: str) -> bool:
    key = (str(registry.standard_run_dir(target, run_id).resolve()), registry.RESEARCH_SIDECAR_LOCK)
    depth = getattr(registry._SIDECAR_LOCK_TLS, "depth", None) or {}
    return int(depth.get(key, 0)) > 0


def test_update_phase_seeds_run_id_when_creating_sidecar(tmp_path: Path) -> None:
    run_id = "phase-seed-run"

    stamped = registry.update_phase(tmp_path, run_id, "planning", status="running")

    assert stamped["run_id"] == run_id
    assert registry.read_research(tmp_path, run_id)["run_id"] == run_id


def test_request_cancel_ownerless_terminalizes_under_sidecar_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "ownerless-cancel"
    roster = Roster(
        orchestrator="chef",
        agents={"chef": Agent(name="chef", cli="codex", role="orchestrator")},
    )
    registry.create_standard_run(
        tmp_path,
        run_id=run_id,
        question="q",
        profile="grounded",
        caps={"max_time": 30, "max_dispatches": 4},
        roster=roster,
    )
    observed: list[str] = []

    def fake_owner(*_args: object, **_kwargs: object) -> bool:
        observed.append("owner-check-locked" if _sidecar_lock_held(tmp_path, run_id) else "owner-check-unlocked")
        return False

    original_update = registry.update_research

    def wrapped_update(*args: object, **kwargs: object):
        if kwargs.get("status") == "cancelled":
            observed.append("terminalize-locked" if _sidecar_lock_held(tmp_path, run_id) else "terminalize-unlocked")
        return original_update(*args, **kwargs)

    monkeypatch.setattr(registry.runguard, "has_active_run_owner", fake_owner)
    monkeypatch.setattr(registry, "update_research", wrapped_update)

    out = registry.request_cancel(tmp_path, run_id, requested_by="operator")

    assert out["status"] == "cancelled"
    assert observed == ["owner-check-locked", "terminalize-locked"]


def test_request_cancel_missing_sidecar_raises_typed_error(tmp_path: Path) -> None:
    run_id = "missing-sidecar"
    run_directory = registry.standard_run_dir(tmp_path, run_id)
    run_directory.mkdir(parents=True)
    (run_directory / "run.json").write_text(
        json.dumps({"run_id": run_id, "status": "completed", "kind": "research"}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="research artifact") as caught:
        registry.request_cancel(tmp_path, run_id, requested_by="operator")

    assert not isinstance(caught.value, FileNotFoundError)
    assert not (run_directory / registry.RESEARCH_ARTIFACT).is_file()
