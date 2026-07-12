from __future__ import annotations

import json

from brigade import cli
from brigade import model_trials
from brigade.roster import Agent, Roster


def _manifest() -> dict:
    return {
        "schema": "brigade.eval_manifest.v1",
        "name": "adapter-check",
        "trials": 2,
        "seats": ["cursor"],
        "cases": [{"id": "hello", "prompt": "Say hello"}],
        "graders": [{"type": "exact_output", "expected": "hello"}],
    }


def _roster() -> Roster:
    return Roster(
        orchestrator="chef",
        agents={
            "chef": Agent("chef", "codex", "plan"),
            "cursor": Agent("cursor", "cursor", "answer", model="composer-2.5"),
        },
    )


def test_expand_cells_is_stable_and_conditions_change_identity():
    first = model_trials.expand_cells(_manifest(), _roster())
    second = model_trials.expand_cells(_manifest(), _roster())
    assert [cell.cell_id for cell in first] == [cell.cell_id for cell in second]
    assert len(first) == 2

    changed = _manifest()
    changed["cases"][0]["prompt"] = "Say hello clearly"
    assert model_trials.expand_cells(changed, _roster())[0].cell_id != first[0].cell_id


def test_graders_distinguish_zero_score_from_error(tmp_path):
    results = model_trials.grade_output(
        graders=[
            {"type": "exact_output", "expected": "wanted"},
            {"type": "regex_output", "pattern": "["},
        ],
        text="actual",
        exit_code=0,
        workspace=tmp_path,
        run_dir=tmp_path,
    )
    assert results[0]["status"] == "scored"
    assert results[0]["score"] == 0.0
    assert results[1]["status"] == "grader_error"
    assert results[1]["score"] is None


def test_run_then_resume_skips_matching_terminal_cells(tmp_path, monkeypatch):
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(_manifest()))
    calls: list[str] = []

    def fake_run(task, roster, **kwargs):
        calls.append(kwargs["worker"])
        out = kwargs["output_dir"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "final.txt").write_text("hello\n")
        (out / "run.json").write_text(json.dumps({"status": "ok", "duration_seconds": 1.25}))
        return 0

    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    root = tmp_path / "results"

    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 0
    assert calls == ["cursor", "cursor"]
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=True) == 0
    assert calls == ["cursor", "cursor"]

    summary = model_trials.summarize(root)
    assert summary["counts"] == {"accepted": 2}
    assert summary["scores"]["count"] == 2
    assert summary["scores"]["mean"] == 1.0


def test_resume_reports_stale_cells_when_conditions_change(tmp_path, monkeypatch):
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(_manifest()))

    def fake_run(task, roster, **kwargs):
        out = kwargs["output_dir"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "final.txt").write_text("hello\n")
        (out / "run.json").write_text(json.dumps({"status": "ok", "duration_seconds": 0.5}))
        return 0

    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    root = tmp_path / "results"
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 0

    changed = _manifest()
    changed["graders"] = [{"type": "exact_output", "expected": "goodbye"}]
    manifest_path.write_text(json.dumps(changed))
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=True) == 1
    plan = json.loads((root / "plan.json").read_text())
    assert len(plan["stale_cells"]) == 2


def test_cli_trial_plan_uses_explicit_roster(tmp_path, capsys):
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(_manifest()))
    roster_path = tmp_path / "roster.toml"
    roster_path.write_text(
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.cursor]\ncli = "cursor"\nmodel = "composer-2.5"\nrole = "answer"\n'
    )

    rc = cli.main(
        [
            "model",
            "trial",
            "plan",
            str(manifest_path),
            "--target",
            str(tmp_path),
            "--roster",
            str(roster_path),
        ]
    )

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["cells"]) == 2
