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


def test_cell_identity_payload_is_locked():
    # Snapshot of the frozen identity contract (docs/phase-eval-cell-identity.md).
    # Any change to the field set below must come with a CELL_SCHEMA bump.
    manifest = {
        "schema": "brigade.eval_manifest.v1",
        "name": "identity-lock",
        "trials": 1,
        "seats": ["cursor"],
        "cases": [{"id": "hello", "prompt": "Say hello"}],
        "graders": [{"type": "exact_output", "expected": "hello"}],
    }
    cell = model_trials.expand_cells(manifest, _roster())[0]
    expected_identity = {
        "schema": "brigade.eval_cell.v1",
        "case": {"id": "hello", "prompt": "Say hello"},
        "seat": {
            "seat": "cursor",
            "cli": "cursor",
            "model": "composer-2.5",
            "reasoning": None,
            "transport": "direct",
            "transport_version": None,
            "env": None,
            "codex_transport": None,
        },
        "trial": 1,
        "graders": [{"type": "exact_output", "expected": "hello"}],
        "execution_mode": "read-only",
    }
    assert model_trials._canonical_digest(expected_identity) == cell.cell_id
    assert cell.cell_id == "55c07e87f401b5aa49f956b2cec1bfee87986088702bc0b1762cc722ff2638c1"


def test_prompt_line_endings_do_not_change_identity():
    crlf = _manifest()
    crlf["cases"][0]["prompt"] = "Say hello\r\nagain\rnow"
    unix = _manifest()
    unix["cases"][0]["prompt"] = "Say hello\nagain\nnow"
    crlf_cells = model_trials.expand_cells(crlf, _roster())
    unix_cells = model_trials.expand_cells(unix, _roster())
    assert [cell.cell_id for cell in crlf_cells] == [cell.cell_id for cell in unix_cells]
    assert crlf_cells[0].prompt == "Say hello\nagain\nnow"


def test_attempt_number_uses_max_plus_one_and_tolerates_gaps(tmp_path):
    assert model_trials._attempt_number(tmp_path) == 1
    attempts = tmp_path / "attempts"
    attempts.mkdir()
    (attempts / "attempt-001").mkdir()
    (attempts / "attempt-003").mkdir()
    (attempts / "scratch").mkdir()
    (attempts / "attempt-002-partial").mkdir()
    assert model_trials._attempt_number(tmp_path) == 4


def test_attempt_number_does_not_reuse_deleted_highest_attempt(tmp_path):
    attempts = tmp_path / "attempts"
    attempts.mkdir()
    (attempts / "attempt-001").mkdir()
    (tmp_path / "cell.json").write_text(json.dumps({"attempt": 3}))
    assert model_trials._attempt_number(tmp_path) == 4


def test_attempt_number_counts_running_marker_without_attempt_dir(tmp_path):
    # Crash window: cell.json was written as running before attempt-001 existed.
    (tmp_path / "cell.json").write_text(json.dumps({"state": "running", "attempt": 1}))
    assert model_trials._attempt_number(tmp_path) == 2


def test_attempt_number_ignores_nonpositive_recorded_attempt(tmp_path):
    # Corrupt-but-valid-JSON markers must not produce attempt-000.
    (tmp_path / "cell.json").write_text(json.dumps({"state": "running", "attempt": -1}))
    assert model_trials._attempt_number(tmp_path) == 1
    (tmp_path / "cell.json").write_text(json.dumps({"state": "running", "attempt": 0}))
    assert model_trials._attempt_number(tmp_path) == 1


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
    assert "exit_code" not in results[0]
    assert results[0]["component_checks"] == [
        {"name": "exact_output", "passed": False, "detail": "output did not match"}
    ]
    assert "exit_code" not in results[1]


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


def test_resume_after_manifest_edit_reruns_only_changed_cells(tmp_path, monkeypatch, capsys):
    manifest = _manifest()
    manifest["trials"] = 1
    manifest["cases"] = [
        {"id": "alpha", "prompt": "Say alpha"},
        {"id": "beta", "prompt": "Say beta"},
    ]
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(manifest))
    tasks: list[str] = []

    def fake_run(task, roster, **kwargs):
        tasks.append(task)
        out = kwargs["output_dir"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "final.txt").write_text("hello\n")
        (out / "run.json").write_text(json.dumps({"status": "ok", "duration_seconds": 0.5}))
        return 0

    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    root = tmp_path / "results"
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 0
    assert sorted(tasks) == ["Say alpha", "Say beta"]

    original_ids = {cell.case_id: cell.cell_id for cell in model_trials.expand_cells(manifest, _roster())}
    alpha_path = root / "cells" / original_ids["alpha"] / "cell.json"

    edited = json.loads(manifest_path.read_text())
    edited["cases"][1]["prompt"] = "Say beta differently"
    manifest_path.write_text(json.dumps(edited))
    tasks.clear()
    capsys.readouterr()
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=True) == 0

    # The unchanged cell is skipped; the edited cell re-runs under a new cell_id.
    assert tasks == ["Say beta differently"]
    alpha = json.loads(alpha_path.read_text())
    assert alpha["state"] == "accepted"
    assert alpha["attempt"] == 1
    edited_ids = {cell.case_id: cell.cell_id for cell in model_trials.expand_cells(edited, _roster())}
    assert edited_ids["alpha"] == original_ids["alpha"]
    assert edited_ids["beta"] != original_ids["beta"]
    new_beta = json.loads((root / "cells" / edited_ids["beta"] / "cell.json").read_text())
    assert new_beta["state"] == "accepted"
    plan = json.loads((root / "plan.json").read_text())
    assert new_beta["manifest_digest"] == plan["manifest_digest"]

    # The old cell is kept and reported, not pruned; resume warns on stderr.
    assert (root / "cells" / original_ids["beta"] / "cell.json").is_file()
    summary = json.loads((root / "summary.json").read_text())
    assert summary["counts"] == {"accepted": 2}
    assert summary["stale_counts"] == {"accepted": 1}
    assert "1 stale cell(s)" in capsys.readouterr().err


def test_resume_reruns_killed_running_cell_as_new_attempt(tmp_path, monkeypatch):
    manifest = _manifest()
    manifest["trials"] = 1
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(manifest))

    def fake_run(task, roster, **kwargs):
        out = kwargs["output_dir"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "final.txt").write_text("hello\n")
        (out / "run.json").write_text(json.dumps({"status": "ok", "duration_seconds": 0.5}))
        return 0

    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    root = tmp_path / "results"
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 0
    cell_path = next((root / "cells").glob("*/cell.json"))

    # Simulate a kill mid-run: the last durable state is "running".
    killed = json.loads(cell_path.read_text())
    killed["state"] = "running"
    cell_path.write_text(json.dumps(killed))

    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=True) == 0
    final = json.loads(cell_path.read_text())
    assert final["state"] == "accepted"
    assert final["attempt"] == 2
    attempts = sorted(p.name for p in (cell_path.parent / "attempts").iterdir())
    assert attempts == ["attempt-001", "attempt-002"]


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
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 3
    cell = json.loads(next((root / "cells").glob("*/cell.json")).read_text())
    assert cell["state"] == "adapter_error"
    assert cell["failure_reason"] == "transport_drop"


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


def test_timeout_sets_failure_reason_and_measurement_exit(tmp_path, monkeypatch):
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(_manifest()))

    def fake_run(task, roster, **kwargs):
        out = kwargs["output_dir"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "final.txt").write_text("partial\n")
        (out / "run.json").write_text(
            json.dumps({"status": "timeout", "failure_kind": "timeout", "duration_seconds": 30.0})
        )
        (out / "worker-results.json").write_text(
            json.dumps({"results": [{"worker": "cursor", "ok": False, "timed_out": True, "transport": "cli"}]})
        )
        return 124

    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    root = tmp_path / "results"
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 3
    cell = json.loads(next((root / "cells").glob("*/cell.json")).read_text())
    assert cell["state"] == "execution_error"
    assert cell["failure_reason"] == "timeout"
    summary = model_trials.summarize(root)
    assert summary["measurement_failures"] == 2


def test_process_exit_measurement_dominates_deterministic_failure():
    summary = {
        "measurement_failures": 1,
        "counts": {"rejected": 2, "adapter_error": 1},
    }
    assert model_trials.process_exit(summary) == 3
    summary = {"measurement_failures": 0, "counts": {"rejected": 1}}
    assert model_trials.process_exit(summary) == 1
    summary = {"measurement_failures": 0, "counts": {"accepted": 2}}
    assert model_trials.process_exit(summary) == 0


def test_summarize_splits_partial_scores_from_headline_scores(tmp_path):
    root = tmp_path / "results"
    cell_dir = root / "cells" / "cell-a"
    cell_dir.mkdir(parents=True)
    localio = model_trials.localio
    localio.write_json(
        root / "plan.json",
        {
            "schema": model_trials.MANIFEST_SCHEMA,
            "name": "partial",
            "manifest_digest": "abc",
            "cells": [{"cell_id": "cell-a", "coordinate": "x:cursor:1"}],
        },
    )
    localio.write_json(
        cell_dir / "cell.json",
        {
            "schema": model_trials.CELL_SCHEMA,
            "cell_id": "cell-a",
            "state": "grader_error",
            "graders": [
                {"status": "scored", "score": 1.0},
                {"status": "grader_error", "score": None},
            ],
        },
    )
    summary = model_trials.summarize(root)
    assert summary["measurement_failures"] == 1
    assert summary["partial_scores"]["count"] == 1
    assert summary["partial_scores"]["mean"] == 1.0
    assert summary["scores"]["count"] == 0


def test_regrade_rescores_grader_error_without_seat_rerun(tmp_path, monkeypatch):
    manifest = _manifest()
    manifest["trials"] = 1
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(manifest))
    root = tmp_path / "results"
    cell = model_trials.expand_cells(manifest, _roster())[0]
    run_dir = root / "cells" / cell.cell_id / "attempts" / "attempt-001" / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "final.txt").write_text("hello\n")
    (run_dir / "run.json").write_text(json.dumps({"status": "ok", "cwd": str(tmp_path), "duration_seconds": 0.1}))
    digest = model_trials._output_digest("hello\n")
    plan, _ = model_trials.build_plan(manifest_path, _roster(), root)
    model_trials.localio.write_json(root / "plan.json", plan)
    broken_graders = model_trials.grade_output(
        graders=[{"type": "regex_output", "pattern": "["}],
        text="hello\n",
        exit_code=0,
        workspace=tmp_path,
        run_dir=run_dir,
    )
    payload = model_trials._finalize_cell_payload(
        cell,
        plan=plan,
        attempt=1,
        started_at="2026-01-01T00:00:00+00:00",
        exit_code=0,
        run_dir=run_dir,
        graders=broken_graders,
        text="hello\n",
        failure_reason=None,
    )
    model_trials._write_cell_payload(root / "cells" / cell.cell_id, run_dir.parent, payload)
    assert payload["state"] == "grader_error"

    monkeypatch.setattr(
        model_trials.aboyeur, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("seat rerun"))
    )
    assert model_trials.regrade(root) == 0
    regressed = json.loads((root / "cells" / cell.cell_id / "cell.json").read_text())
    assert regressed["state"] == "accepted"
    assert regressed["graders"][0]["output_digest"] == digest


def test_resume_regrades_grader_error_cells(tmp_path, monkeypatch):
    manifest = _manifest()
    manifest["trials"] = 1
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(manifest))
    root = tmp_path / "results"
    cell = model_trials.expand_cells(manifest, _roster())[0]
    run_dir = root / "cells" / cell.cell_id / "attempts" / "attempt-001" / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "final.txt").write_text("hello\n")
    (run_dir / "run.json").write_text(json.dumps({"status": "ok", "cwd": str(tmp_path)}))
    plan, _ = model_trials.build_plan(manifest_path, _roster(), root)
    model_trials.localio.write_json(root / "plan.json", plan)
    broken = model_trials.grade_output(
        graders=[{"type": "regex_output", "pattern": "["}],
        text="hello\n",
        exit_code=0,
        workspace=tmp_path,
        run_dir=run_dir,
    )
    payload = model_trials._finalize_cell_payload(
        cell,
        plan=plan,
        attempt=1,
        started_at="2026-01-01T00:00:00+00:00",
        exit_code=0,
        run_dir=run_dir,
        graders=broken,
        text="hello\n",
        failure_reason=None,
    )
    model_trials._write_cell_payload(root / "cells" / cell.cell_id, run_dir.parent, payload)
    monkeypatch.setattr(
        model_trials.aboyeur, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("seat rerun"))
    )
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=True) == 0


def test_project_cell_strips_prompt_and_relativizes_run_dir(tmp_path):
    run_dir = tmp_path / "results" / "cells" / "abc" / "attempts" / "attempt-001" / "run"
    cell = {
        "schema": model_trials.CELL_SCHEMA,
        "cell_id": "abc",
        "prompt": "secret prompt",
        "case": {"id": "hello", "prompt": "nested secret"},
        "run_dir": str(run_dir),
    }
    projected = model_trials.project_cell(cell, base_dir=tmp_path / "results")
    assert "prompt" not in projected
    assert projected["prompt_digest"].startswith("sha256:")
    assert projected["case"]["prompt_digest"].startswith("sha256:")
    assert "prompt" not in projected["case"]
    assert projected["run_dir"] == "cells/abc/attempts/attempt-001/run"
