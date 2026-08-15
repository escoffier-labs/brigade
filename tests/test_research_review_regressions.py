# Regression pins for Opus 5 review findings against first-class research.
# Tests encode the desired contract; several intentionally fail until production fixes land.
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brigade import aboyeur, localio, research_cmd
from brigade.research import registry
from brigade.research.types import ResearchRunError
from brigade.research_cmd import (
    cli_cancel,
    cli_doctor,
    cli_open,
    cli_resume,
    cli_run,
    cli_show,
    cli_status,
)

from tests.test_research_cmd import (
    RecordingProcessRegistry,
    minimal_research_roster,
    patch_stub_lanes,
)


def test_resume_exhausted_wall_clock_refuses_before_reopen(monkeypatch, tmp_path: Path, capsys) -> None:
    """Finding 1/D: refuse wall-clock-exhausted resume before mutating research.json."""
    (tmp_path / "a.md").write_text("plants use light", encoding="utf-8")
    patch_stub_lanes(monkeypatch)
    run_id = "wall-clock-spent"
    registry.create_standard_run(
        tmp_path,
        run_id=run_id,
        question="q",
        profile="grounded",
        caps={"max_time": 30, "max_dispatches": 4},
        roster=minimal_research_roster(),
    )
    run_dir = registry.standard_run_dir(tmp_path, run_id)
    started = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    receipt = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    receipt["started_at"] = started
    receipt["run_budget"] = {
        "schema": "brigade.run_budget.v1",
        "schema_version": 1,
        "ceilings": {"wall_clock_seconds": 30, "worker_dispatch_count": 4},
        "observed": {},
        "threshold_pct": 80,
    }
    localio.write_json(run_dir / "run.json", receipt)
    aboyeur.record_run_termination(
        run_dir,
        status="failed",
        failure_phase="discovery",
        failure_kind="timeout",
        detail="research run exceeded max_time",
    )
    registry.update_research(
        tmp_path,
        run_id,
        status="failed",
        failure_kind="timeout",
        failure_phase="discovery",
        manifest={"sources": [str(tmp_path / "*.md")], "web_enabled": False},
        caps={"max_time": 30, "max_dispatches": 4},
    )
    research_path = run_dir / "research.json"
    before_research = research_path.read_bytes()

    reopened: list[str] = []
    real_reopen = research_cmd._reopen_standard_run

    def track_reopen(target: Path, rid: str) -> None:
        reopened.append(rid)
        return real_reopen(target, rid)

    monkeypatch.setattr(research_cmd, "_reopen_standard_run", track_reopen)

    code = cli_resume(target=tmp_path, run_id=run_id, overrides={}, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert reopened == [], "wall-clock-exhausted resume must refuse before reopening"
    assert research_path.read_bytes() == before_research, (
        "budget-exhausted resume must leave research.json byte-for-byte unchanged"
    )
    assert code == 2
    assert registry.show_run(tmp_path, run_id)["status"] == "failed"
    assert payload.get("failure_kind") == "budget-exhausted"


def test_budget_exhausted_run_resume_leaves_research_json_unchanged(monkeypatch, tmp_path: Path) -> None:
    """Finding D: run(..., resume=) must refuse before update_research/_persist_category."""
    (tmp_path / "a.md").write_text("plants use light", encoding="utf-8")
    patch_stub_lanes(monkeypatch)
    run_id = "wall-clock-spent-run"
    registry.create_standard_run(
        tmp_path,
        run_id=run_id,
        question="q",
        profile="grounded",
        caps={"max_time": 30, "max_dispatches": 4},
        roster=minimal_research_roster(),
    )
    run_dir = registry.standard_run_dir(tmp_path, run_id)
    started = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    receipt = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    receipt["started_at"] = started
    receipt["run_budget"] = {
        "schema": "brigade.run_budget.v1",
        "schema_version": 1,
        "ceilings": {"wall_clock_seconds": 30, "worker_dispatch_count": 4},
        "observed": {},
        "threshold_pct": 80,
    }
    localio.write_json(run_dir / "run.json", receipt)
    aboyeur.record_run_termination(
        run_dir,
        status="failed",
        failure_phase="discovery",
        failure_kind="timeout",
        detail="research run exceeded max_time",
    )
    registry.update_research(
        tmp_path,
        run_id,
        status="failed",
        failure_kind="timeout",
        failure_phase="discovery",
        manifest={"sources": [str(tmp_path / "*.md")], "web_enabled": False},
        caps={"max_time": 30, "max_dispatches": 4},
    )
    research_path = run_dir / "research.json"
    before_research = research_path.read_bytes()
    updates: list[str] = []
    real_update = registry.update_research

    def track_update(target: Path, rid: str, **fields):
        updates.append(rid)
        return real_update(target, rid, **fields)

    monkeypatch.setattr(registry, "update_research", track_update)
    monkeypatch.setattr(research_cmd.registry, "update_research", track_update)

    with pytest.raises(research_cmd.ResearchCommandFailure) as caught:
        research_cmd.run(
            target=tmp_path,
            question="q",
            sources=[str(tmp_path / "*.md")],
            web=False,
            overrides={"max_rounds": 1, "min_rounds": 1, "max_time": 30},
            run_id=run_id,
            resume=research_cmd.ResumeState(),
        )
    assert caught.value.failure_kind == "budget-exhausted"
    assert research_path.read_bytes() == before_research
    assert updates == [], "budget-exhausted resume must not call update_research"


def test_discovery_failure_cancels_process_registry(monkeypatch, tmp_path: Path) -> None:
    """Finding 2/F: discovery terminal errors must cancel before terminate on _fail."""
    (tmp_path / "a.md").write_text("plants use light", encoding="utf-8")
    patch_stub_lanes(monkeypatch)
    instances: list[RecordingProcessRegistry] = []
    order: list[str] = []

    class TrackingRegistry(RecordingProcessRegistry):
        def __init__(self) -> None:
            super().__init__()
            instances.append(self)

        def cancel(self) -> None:
            order.append("cancel")
            return super().cancel()

    monkeypatch.setattr("brigade.proc.ProcessRegistry", TrackingRegistry)

    real_terminate = research_cmd._terminate_standard_run

    def track_terminate(*args, **kwargs):
        order.append("terminate")
        return real_terminate(*args, **kwargs)

    monkeypatch.setattr(research_cmd, "_terminate_standard_run", track_terminate)

    def boom(self, question: str, *, resume=None):
        del self, question, resume
        raise ResearchRunError("discovery", "timeout", "research run exceeded max_time")

    monkeypatch.setattr(research_cmd.ResearchEngine, "run", boom)
    research_cmd.run(
        target=tmp_path,
        question="q",
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1, "min_rounds": 1, "max_time": 30},
        run_id="discovery-cleanup",
    )
    assert instances, "ProcessRegistry was never constructed"
    assert instances[-1].cancel_calls >= 1
    assert "cancel" in order and "terminate" in order
    assert order.index("cancel") < order.index("terminate"), (
        "process_registry.cancel must occur before _terminate_standard_run on _fail paths"
    )


def test_cli_run_invalid_config_exits_2_json(monkeypatch, tmp_path: Path, capsys) -> None:
    """Finding 3: invalid research.toml must not escape as traceback/exit 1."""
    del monkeypatch
    brigade = tmp_path / ".brigade"
    brigade.mkdir(parents=True, exist_ok=True)
    (brigade / "research.toml").write_text(
        "[profiles.grounded]\nplanner = []\n",
        encoding="utf-8",
    )
    try:
        code = cli_run(
            target=tmp_path,
            question="q",
            corpus=None,
            sources=[],
            web=False,
            overrides={},
            json_output=True,
        )
    except Exception as exc:  # noqa: BLE001 - pin that CLI must not escape
        pytest.fail(f"invalid config escaped as {type(exc).__name__}: {exc}")
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == 2
    assert payload.get("failure_kind") == "invalid-config"
    assert "Traceback" not in out


def test_cli_run_unreadable_config_exits_2_json(monkeypatch, tmp_path: Path, capsys) -> None:
    """Finding E: unreadable research.toml must be typed invalid-config without chmod."""
    monkeypatch.setattr(
        research_cmd.rconfig,
        "load",
        lambda _target: (_ for _ in ()).throw(OSError("Permission denied: research.toml")),
    )
    try:
        code = cli_run(
            target=tmp_path,
            question="q",
            corpus=None,
            sources=[],
            web=False,
            overrides={},
            json_output=True,
        )
    except Exception as exc:  # noqa: BLE001 - pin that CLI must not escape
        pytest.fail(f"unreadable config escaped as {type(exc).__name__}: {exc}")
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == 2
    assert payload.get("failure_kind") == "invalid-config"
    assert "Traceback" not in out


def test_cli_run_preserves_typed_non_config_failure_kind(monkeypatch, tmp_path: Path, capsys) -> None:
    """Finding E: already-typed non-config failures must keep their original failure_kind."""
    monkeypatch.setattr(
        research_cmd,
        "run",
        lambda **_kwargs: (_ for _ in ()).throw(
            research_cmd.ResearchCommandFailure(
                run_id="typed-run",
                failure_kind="budget-exhausted",
                failure_phase="admission",
                detail="run wall-clock budget exhausted; resume refused",
                exit_code=2,
            )
        ),
    )
    code = cli_run(
        target=tmp_path,
        question="q",
        corpus=None,
        sources=[],
        web=False,
        overrides={},
        json_output=True,
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 2
    assert payload.get("failure_kind") == "budget-exhausted"


def test_cli_resume_missing_roster_exits_2_json(monkeypatch, tmp_path: Path, capsys) -> None:
    """Finding 3: missing roster on resume must be typed invalid-config exit 2."""
    run_id = "resume-no-roster"
    registry.create_standard_run(
        tmp_path,
        run_id=run_id,
        question="q",
        profile="grounded",
        caps={"max_time": 30, "max_dispatches": 4},
        roster=minimal_research_roster(),
    )
    aboyeur.update_run_receipt(registry.standard_run_dir(tmp_path, run_id), status="failed")
    registry.update_research(
        tmp_path,
        run_id,
        status="failed",
        manifest={"sources": [], "web_enabled": False},
        caps={"max_time": 30, "max_dispatches": 4},
    )

    def missing_roster(_target: Path):
        raise FileNotFoundError("roster not found: checked /tmp/missing/roster.toml")

    monkeypatch.setattr(research_cmd, "_load_roster", missing_roster)
    try:
        code = cli_resume(target=tmp_path, run_id=run_id, overrides={}, json_output=True)
    except Exception as exc:  # noqa: BLE001 - pin that CLI must not escape
        pytest.fail(f"missing roster escaped as {type(exc).__name__}: {exc}")
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == 2
    assert payload.get("failure_kind") == "invalid-config"
    assert "Traceback" not in out


def test_cli_doctor_invalid_config_exits_2_json(tmp_path: Path, capsys) -> None:
    """Finding 3: doctor must map invalid research config to exit 2 + JSON."""
    brigade = tmp_path / ".brigade"
    brigade.mkdir(parents=True, exist_ok=True)
    (brigade / "research.toml").write_text(
        "[profiles.grounded]\nplanner = []\n",
        encoding="utf-8",
    )
    (brigade / "roster.toml").write_text(
        'orchestrator = "chef"\n\n[agents.chef]\ncli = "codex"\nrole = "orchestrator"\n',
        encoding="utf-8",
    )
    try:
        code = cli_doctor(target=tmp_path, json_output=True)
    except Exception as exc:  # noqa: BLE001 - pin that CLI must not escape
        pytest.fail(f"invalid config escaped as {type(exc).__name__}: {exc}")
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert code == 2
    assert payload.get("failure_kind") == "invalid-config"
    assert "Traceback" not in out


def test_cli_doctor_unreadable_config_exits_2_json(monkeypatch, tmp_path: Path, capsys) -> None:
    """Doctor must type config read failures instead of escaping an OSError."""
    monkeypatch.setattr(
        "brigade.research.doctor.doctor_payload",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("Permission denied: research.toml")),
    )

    code = cli_doctor(target=tmp_path, json_output=True)
    out = capsys.readouterr().out
    payload = json.loads(out)

    assert code == 2
    assert payload.get("failure_kind") == "invalid-config"
    assert "Traceback" not in out


@pytest.mark.parametrize(
    "cli_call",
    [
        lambda target, run_id, json_output: cli_status(target=target, run_id=run_id, json_output=json_output),
        lambda target, run_id, json_output: cli_show(target=target, run_id=run_id, json_output=json_output),
        lambda target, run_id, json_output: cli_cancel(target=target, run_id=run_id, json_output=json_output),
        lambda target, run_id, json_output: cli_open(target=target, run_id=run_id, json_output=json_output),
    ],
    ids=["status", "show", "cancel", "open"],
)
def test_cli_missing_run_honors_json(tmp_path: Path, capsys, cli_call) -> None:
    """Finding 4/C: valid-but-missing run IDs must emit JSON with failure_kind no-such-run."""
    code = cli_call(tmp_path, "20260814-000000-missing-run", True)
    out = capsys.readouterr().out
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        pytest.fail(f"--json missing-run emitted non-JSON: {out!r} ({exc})")
    assert code == 1
    assert payload.get("failure_kind") == "no-such-run"
    assert payload.get("run_id") == "20260814-000000-missing-run"
    assert "no such run" in str(payload.get("error") or payload.get("detail") or "").lower()


def test_cli_open_missing_report_emits_json(tmp_path: Path, capsys) -> None:
    """Finding B: cli_open without a report must emit JSON missing-artifact under --json."""
    run_id = "open-no-report"
    registry.create_standard_run(
        tmp_path,
        run_id=run_id,
        question="q",
        profile="grounded",
        caps={"max_time": 30, "max_dispatches": 4},
        roster=minimal_research_roster(),
    )
    aboyeur.update_run_receipt(registry.standard_run_dir(tmp_path, run_id), status="completed")
    registry.update_research(tmp_path, run_id, status="completed", artifacts={})

    code = cli_open(target=tmp_path, run_id=run_id, json_output=True)
    out = capsys.readouterr().out
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        pytest.fail(f"--json open missing-report emitted non-JSON: {out!r} ({exc})")
    assert code == 1
    assert payload.get("failure_kind") == "missing-artifact"
    assert payload.get("run_id") == run_id
    assert "Traceback" not in out
    assert not out.strip().startswith("no report for run:")


def test_same_second_identical_questions_do_not_reuse_run_dir(monkeypatch, tmp_path: Path) -> None:
    """Finding 7: identical questions in the same second must not collide on run id."""
    (tmp_path / "a.md").write_text("plants use light", encoding="utf-8")
    patch_stub_lanes(monkeypatch)
    question = "identical collision question"
    frozen_id = "20260814-120000-" + registry.slug(question)
    registry.create_standard_run(
        tmp_path,
        run_id=frozen_id,
        question=question,
        profile="grounded",
        caps={"max_time": 30, "max_dispatches": 4},
        roster=minimal_research_roster(),
    )
    aboyeur.update_run_receipt(registry.standard_run_dir(tmp_path, frozen_id), status="completed")
    registry.update_research(tmp_path, frozen_id, status="completed")
    before = (registry.standard_run_dir(tmp_path, frozen_id) / "run.json").read_text(encoding="utf-8")

    monkeypatch.setattr(research_cmd, "_new_run_id", lambda _q: frozen_id)
    rid = research_cmd.run(
        target=tmp_path,
        question=question,
        sources=[str(tmp_path / "*.md")],
        web=False,
        overrides={"max_rounds": 1, "min_rounds": 1, "max_time": 30},
    )
    after = (registry.standard_run_dir(tmp_path, frozen_id) / "run.json").read_text(encoding="utf-8")
    assert rid != frozen_id
    assert after == before


def test_cli_resume_invalid_status_exits_2(tmp_path: Path, capsys) -> None:
    """Finding 8: non-resumable statuses must use typed admission exit 2, not substring exit 1."""
    run_id = "resume-archived"
    registry.create_standard_run(
        tmp_path,
        run_id=run_id,
        question="q",
        profile="grounded",
        caps={"max_time": 30, "max_dispatches": 4},
        roster=minimal_research_roster(),
    )
    run_dir = registry.standard_run_dir(tmp_path, run_id)
    aboyeur.update_run_receipt(run_dir, status="completed", finished_at="2026-08-14T12:00:00+00:00")
    # Force a non-completed terminal-ish status that is still not in the resumable set.
    receipt = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    receipt["status"] = "archived"
    receipt["finished_at"] = "2026-08-14T12:00:00+00:00"
    localio.write_json(run_dir / "run.json", receipt)
    registry.update_research(tmp_path, run_id, status="archived")

    code = cli_resume(target=tmp_path, run_id=run_id, overrides={}, json_output=True)
    out = capsys.readouterr().out
    assert code == 2
    assert "cannot resume" in out.lower() or "archived" in out.lower()
