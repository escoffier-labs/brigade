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

    acpx_roster = _roster()
    acpx_roster = Roster(
        orchestrator=acpx_roster.orchestrator,
        agents={
            **acpx_roster.agents,
            "cursor": Agent(
                "cursor",
                "cursor",
                "answer",
                model="composer-2.5",
                transport="acpx",
                transport_version="0.12.0",
            ),
        },
    )
    newer_acpx = Roster(
        orchestrator=acpx_roster.orchestrator,
        agents={
            **acpx_roster.agents,
            "cursor": Agent(
                "cursor",
                "cursor",
                "answer",
                model="composer-2.5",
                transport="acpx",
                transport_version="0.13.0",
            ),
        },
    )
    assert (
        model_trials.expand_cells(_manifest(), acpx_roster)[0].cell_id
        != model_trials.expand_cells(_manifest(), newer_acpx)[0].cell_id
    )


def test_codex_transport_and_execution_mode_change_cell_identity():
    manifest = _manifest()
    manifest["seats"] = ["worker"]
    manifest["execution"] = {"mode": "read-only"}
    exec_roster = Roster(
        orchestrator="chef",
        agents={"chef": Agent("chef", "codex", "plan"), "worker": Agent("worker", "codex", "work")},
        codex_transport="exec",
    )
    appserver_roster = Roster(
        orchestrator="chef",
        agents=exec_roster.agents,
        codex_transport="app-server",
    )
    first = model_trials.expand_cells(manifest, exec_roster)[0]
    assert model_trials.expand_cells(manifest, appserver_roster)[0].cell_id != first.cell_id
    manifest["execution"] = {"mode": "writable-worktree"}
    assert model_trials.expand_cells(manifest, exec_roster)[0].cell_id != first.cell_id


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
    assert results[0]["exit_code"] == 0
    assert results[0]["component_checks"] == [
        {"name": "exact_output", "passed": False, "detail": "output did not match"}
    ]
    assert results[1]["exit_code"] is None


def test_execute_writes_running_marker_before_aboyeur_run(tmp_path, monkeypatch):
    manifest = _manifest()
    manifest["trials"] = 1
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(manifest))
    cell = model_trials.expand_cells(manifest, _roster())[0]
    started_at_seen: list[str] = []
    root = tmp_path / "results"

    def fake_run(task, roster, **kwargs):
        cell_path = root / "cells" / cell.cell_id / "cell.json"
        assert cell_path.is_file()
        running = json.loads(cell_path.read_text())
        assert running["schema"] == model_trials.CELL_SCHEMA
        assert running["state"] == "running"
        assert running["attempt"] == 1
        for key, value in cell.payload().items():
            assert running[key] == value
        assert isinstance(running.get("started_at"), str)
        started_at_seen.append(running["started_at"])
        out = kwargs["output_dir"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "final.txt").write_text("hello\n")
        (out / "run.json").write_text(json.dumps({"status": "ok", "duration_seconds": 0.5}))
        return 0

    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 0
    final = json.loads((root / "cells" / cell.cell_id / "cell.json").read_text())
    assert final["state"] == "accepted"
    assert final["started_at"] == started_at_seen[0]


def test_resume_does_not_skip_running_cells(tmp_path, monkeypatch):
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(_manifest()))
    calls: list[str] = []

    def fake_run(task, roster, **kwargs):
        calls.append(kwargs["worker"])
        out = kwargs["output_dir"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "final.txt").write_text("hello\n")
        (out / "run.json").write_text(json.dumps({"status": "ok", "duration_seconds": 0.5}))
        return 0

    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    root = tmp_path / "results"
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 0
    assert calls == ["cursor", "cursor"]

    crashed_path = next((root / "cells").glob("*/cell.json"))
    crashed = json.loads(crashed_path.read_text())
    crashed["state"] = "running"
    crashed_path.write_text(json.dumps(crashed))

    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=True) == 0
    assert calls == ["cursor", "cursor", "cursor"]


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
    summary = model_trials.summarize(root)
    assert summary["counts"] == {"rejected": 2}
    assert summary["stale_counts"] == {"accepted": 2}


def test_grader_envelope_links_digested_output(tmp_path, monkeypatch):
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(_manifest()))

    def fake_run(task, roster, **kwargs):
        out = kwargs["output_dir"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "final.txt").write_text("hello\n")
        (out / "run.json").write_text(json.dumps({"status": "ok", "duration_seconds": 0.5}))
        (out / "worker-results.json").write_text(
            json.dumps({"results": [{"worker": "cursor", "ok": True, "exit_code": 0, "transport": "cli"}]})
        )
        return 0

    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    root = tmp_path / "results"
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 0
    cell = json.loads(next((root / "cells").glob("*/cell.json")).read_text())
    grader = cell["graders"][0]
    assert grader["cell_id"] == cell["cell_id"]
    assert grader["output_refs"] == [{"path": "run/final.txt", "sha256": grader["output_digest"]}]


def test_adapter_failure_is_distinct_from_execution_failure(tmp_path, monkeypatch):
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(_manifest()))

    def fake_run(task, roster, **kwargs):
        out = kwargs["output_dir"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "run.json").write_text(json.dumps({"status": "failed"}))
        (out / "worker-results.json").write_text(
            json.dumps(
                {
                    "results": [
                        {"worker": "cursor", "ok": False, "detail": "cursor-agent not installed", "transport": "cli"}
                    ]
                }
            )
        )
        return 2

    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    root = tmp_path / "results"
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 1
    cell = json.loads(next((root / "cells").glob("*/cell.json")).read_text())
    assert cell["state"] == "adapter_error"


def test_writable_trials_use_fresh_isolated_worktrees(tmp_path, monkeypatch):
    workspace = tmp_path / "repo"
    workspace.mkdir()
    manifest = _manifest()
    manifest["execution"] = {"mode": "writable-worktree"}
    manifest_path = workspace / "eval.json"
    manifest_path.write_text(json.dumps(manifest))
    created: list[object] = []
    removed: list[object] = []
    run_cwds: list[object] = []

    def fake_create(repo, path):
        created.append((repo, path))
        path.mkdir(parents=True)
        return path

    def fake_remove(repo, path):
        removed.append((repo, path))

    def fake_run(task, roster, **kwargs):
        run_cwds.append(kwargs["cwd"])
        assert kwargs["read_only"] is False
        assert kwargs["authorized_writable_worktree"] is True
        out = kwargs["output_dir"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "final.txt").write_text("hello\n")
        (out / "run.json").write_text(json.dumps({"status": "ok"}))
        (out / "worker-results.json").write_text(
            json.dumps({"results": [{"worker": "cursor", "ok": True, "exit_code": 0, "transport": "cli"}]})
        )
        return 0

    monkeypatch.setattr(model_trials.runguard, "is_git_worktree", lambda path: True)
    monkeypatch.setattr(
        model_trials,
        "_trial_worktree_path",
        lambda workspace, output_dir, cell, attempt: tmp_path / "checkouts" / f"{cell.cell_id}-{attempt}",
    )
    monkeypatch.setattr(model_trials.runguard, "create_detached_worktree", fake_create)
    monkeypatch.setattr(model_trials.runguard, "remove_worktree", fake_remove)
    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    root = tmp_path / "results"
    assert model_trials.execute(manifest_path, _roster(), workspace=workspace, output_dir=root, resume=False) == 0
    assert len(created) == 2
    assert len(removed) == 2
    assert len(set(run_cwds)) == 2


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
