from __future__ import annotations

import json
import os
import signal
import time
from contextlib import nullcontext
from pathlib import Path

import pytest

from brigade import agents
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


def test_model_trial_ordinary_path_does_not_declare_run_budget(tmp_path, monkeypatch):
    """Model-trial cell starts must not invent verification_contract / run_budget payloads."""
    manifest = _manifest()
    manifest["trials"] = 1
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(manifest))
    root = tmp_path / "results"
    start_kwargs: list[dict] = []
    run_kwargs: list[dict] = []

    def fake_record_run_start(output_dir, **kwargs):
        start_kwargs.append(dict(kwargs))
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "run.json").write_text(json.dumps({"status": "started"}))

    def fake_run(task, roster, **kwargs):
        run_kwargs.append(dict(kwargs))
        out = kwargs["output_dir"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "final.txt").write_text("hello\n")
        (out / "run.json").write_text(json.dumps({"status": "ok", "duration_seconds": 0.5}))
        return 0

    monkeypatch.setattr(model_trials.aboyeur, "record_run_start", fake_record_run_start)
    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    monkeypatch.setattr(model_trials.runguard, "run_lock", lambda *a, **k: nullcontext())

    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 0
    assert start_kwargs
    assert "verification_contract_payload" not in start_kwargs[0]
    assert "run_budget_payload" not in start_kwargs[0]
    assert run_kwargs
    assert "verification_contract_payload" not in run_kwargs[0]
    assert "run_budget_payload" not in run_kwargs[0]


def test_model_trial_forwards_explicit_budget_payload(tmp_path, monkeypatch):
    manifest = _manifest()
    manifest["trials"] = 1
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(manifest))
    root = tmp_path / "results"
    budget_payload = {
        "schema": "brigade.run_budget.v1",
        "schema_version": 1,
        "ceilings": {"worker_dispatch_count": 1},
    }
    start_kwargs: list[dict] = []
    run_kwargs: list[dict] = []

    def fake_record_run_start(output_dir, **kwargs):
        start_kwargs.append(dict(kwargs))
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "run.json").write_text(json.dumps({"status": "started"}))

    def fake_run(_task, _roster, **kwargs):
        run_kwargs.append(dict(kwargs))
        out = kwargs["output_dir"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "final.txt").write_text("hello\n")
        (out / "run.json").write_text(json.dumps({"status": "ok", "duration_seconds": 0.5}))
        return 0

    monkeypatch.setattr(model_trials.aboyeur, "record_run_start", fake_record_run_start)
    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    monkeypatch.setattr(model_trials.runguard, "run_lock", lambda *a, **k: nullcontext())

    assert (
        model_trials.execute(
            manifest_path,
            _roster(),
            workspace=tmp_path,
            output_dir=root,
            resume=False,
            run_budget_payload=budget_payload,
        )
        == 0
    )
    assert start_kwargs[0]["run_budget_payload"] == budget_payload
    assert run_kwargs[0]["run_budget_payload"] == budget_payload


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
        # Contract: writable-worktree trials must forward the canonical checkout
        # as lock_workspace so aboyeur.run's drift check compares against it
        # rather than the per-trial worktree cwd.
        assert kwargs["lock_workspace"] == workspace.resolve()
        out = kwargs["output_dir"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "final.txt").write_text("hello\n")
        (out / "run.json").write_text(json.dumps({"status": "ok"}))
        (out / "worker-results.json").write_text(
            json.dumps({"results": [{"worker": "cursor", "ok": True, "exit_code": 0, "transport": "cli"}]})
        )
        return 0

    monkeypatch.setattr(model_trials.runguard, "is_git_worktree", lambda path: True)
    monkeypatch.setattr(model_trials.runguard, "git_root", lambda path: workspace.resolve())
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
    summary = {"measurement_failures": 0, "counts": {"execution_error": 1}}
    assert model_trials.process_exit(summary) == 1
    summary = {"measurement_failures": 0, "counts": {"accepted": 1, "unscored": 1}}
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


def test_execute_mixed_battery_through_real_aboyeur_run_boundary(tmp_path, monkeypatch):
    manifest = {
        "schema": "brigade.eval_manifest.v1",
        "name": "mixed-battery",
        "trials": 1,
        "seats": ["cursor"],
        "cases": [
            {"id": "reject", "prompt": "produce-rejected"},
            {"id": "timeout", "prompt": "produce-timeout"},
            {"id": "nonzero", "prompt": "produce-nonzero-exit"},
        ],
        "graders": [{"type": "exact_output", "expected": "hello"}],
    }
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(manifest))

    def fake_run_agent(cli_ref, prompt, **kwargs):
        if "produce-rejected" in prompt:
            return agents.AgentResult(text="goodbye\n", ok=True)
        if "produce-timeout" in prompt:
            return agents.AgentResult(
                text="partial\n",
                ok=False,
                detail="worker timed out",
                timed_out=True,
                status="timeout",
            )
        if "produce-nonzero-exit" in prompt:
            return agents.AgentResult(
                text="partial\n",
                ok=False,
                detail="worker exited with status 1",
                exit_code=1,
            )
        raise AssertionError(f"unexpected prompt: {prompt!r}")

    monkeypatch.setattr(model_trials.aboyeur.agents, "run_agent", fake_run_agent)
    root = tmp_path / "results"
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 3

    by_case = {}
    for cell_path in (root / "cells").glob("*/cell.json"):
        cell = json.loads(cell_path.read_text())
        by_case[cell["case_id"]] = cell

    assert by_case["reject"]["state"] == "rejected"
    assert by_case["reject"].get("failure_reason") is None

    assert by_case["timeout"]["state"] == "execution_error"
    assert by_case["timeout"]["failure_reason"] == "timeout"

    assert by_case["nonzero"]["state"] == "execution_error"
    assert by_case["nonzero"].get("failure_reason") != "timeout"

    summary = model_trials.summarize(root)
    assert summary["measurement_failures"] == 1
    assert summary["counts"] == {"execution_error": 2, "rejected": 1}

    nonzero_only = dict(manifest)
    nonzero_only["cases"] = [{"id": "nonzero", "prompt": "produce-nonzero-exit"}]
    nonzero_manifest_path = tmp_path / "nonzero-eval.json"
    nonzero_manifest_path.write_text(json.dumps(nonzero_only))
    assert (
        model_trials.execute(
            nonzero_manifest_path,
            _roster(),
            workspace=tmp_path,
            output_dir=tmp_path / "nonzero-results",
            resume=False,
        )
        == 1
    )


def test_regrade_refuses_tampered_final_txt_without_seat_rerun(tmp_path, monkeypatch, capsys):
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

    (run_dir / "final.txt").write_text("tampered\n")
    monkeypatch.setattr(
        model_trials.aboyeur, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("seat rerun"))
    )

    assert model_trials.regrade(root) == 2
    assert "stored output digest mismatch" in capsys.readouterr().err

    unchanged = json.loads((root / "cells" / cell.cell_id / "cell.json").read_text())
    assert unchanged["state"] == "grader_error"
    assert unchanged["graders"][0]["output_digest"] == digest


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
    secret = "secret prompt contents must not leak"
    run_dir = tmp_path / "results" / "cells" / "abc" / "attempts" / "attempt-001" / "run"
    outside_run_dir = tmp_path / "other-results" / "prior-run"
    cell = {
        "schema": model_trials.CELL_SCHEMA,
        "cell_id": "abc",
        "prompt": secret,
        "case": {"id": "hello", "prompt": secret},
        "run_dir": str(run_dir),
        "prior_attempts": [
            {"attempt": 1, "state": "adapter_error", "run_dir": str(run_dir)},
            {"attempt": 2, "state": "execution_error", "run_dir": str(outside_run_dir)},
            {"attempt": 3, "state": "interrupted"},
        ],
    }
    stored_cell = json.dumps(cell, sort_keys=True)
    projected = model_trials.project_cell(cell, base_dir=tmp_path / "results")
    serialized = json.dumps(projected)
    assert "prompt" not in projected
    assert secret not in serialized
    assert str(tmp_path) not in serialized
    assert str(run_dir) not in serialized
    assert projected["prompt_digest"].startswith("sha256:")
    assert projected["case"]["prompt_digest"].startswith("sha256:")
    assert "prompt" not in projected["case"]
    assert projected["run_dir"] == "cells/abc/attempts/attempt-001/run"
    assert projected["prior_attempts"] == [
        {"attempt": 1, "state": "adapter_error", "run_dir": "cells/abc/attempts/attempt-001/run"},
        {"attempt": 2, "state": "execution_error", "run_dir": "prior-run"},
        {"attempt": 3, "state": "interrupted"},
    ]
    assert json.dumps(cell, sort_keys=True) == stored_cell


# --- #1977: --retry-transient -------------------------------------------------


def _single_cell_manifest(tmp_path):
    manifest = _manifest()
    manifest["trials"] = 1
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(manifest))
    return manifest_path


def _write_outcome(out, kind):
    out.mkdir(parents=True, exist_ok=True)
    if kind == "ok":
        (out / "final.txt").write_text("hello\n")
        (out / "run.json").write_text(json.dumps({"status": "ok", "duration_seconds": 0.5}))
        (out / "worker-results.json").write_text(
            json.dumps({"results": [{"worker": "cursor", "ok": True, "exit_code": 0, "transport": "cli"}]})
        )
        return 0
    if kind == "adapter":
        (out / "run.json").write_text(json.dumps({"status": "failed"}))
        (out / "worker-results.json").write_text(
            json.dumps(
                {"results": [{"worker": "cursor", "ok": False, "detail": "transport dropped", "transport": "cli"}]}
            )
        )
        return 2
    if kind == "timeout":
        (out / "final.txt").write_text("partial\n")
        (out / "run.json").write_text(json.dumps({"status": "timeout", "failure_kind": "timeout"}))
        (out / "worker-results.json").write_text(
            json.dumps({"results": [{"worker": "cursor", "ok": False, "timed_out": True, "transport": "cli"}]})
        )
        return 124
    if kind == "crash":
        (out / "final.txt").write_text("boom\n")
        (out / "run.json").write_text(json.dumps({"status": "failed"}))
        (out / "worker-results.json").write_text(
            json.dumps({"results": [{"worker": "cursor", "ok": False, "exit_code": 1, "transport": "cli"}]})
        )
        return 1
    if kind == "wrong":
        (out / "final.txt").write_text("nope\n")
        (out / "run.json").write_text(json.dumps({"status": "ok"}))
        return 0
    raise AssertionError(kind)


def _scripted_run(monkeypatch, script):
    """Replace aboyeur.run; each call consumes the next scripted outcome (last one repeats)."""
    calls: list[str] = []

    def fake_run(task, roster, **kwargs):
        kind = script[min(len(calls), len(script) - 1)]
        calls.append(kind)
        return _write_outcome(kwargs["output_dir"], kind)

    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    return calls


def _only_cell(root):
    return json.loads(next((root / "cells").glob("*/cell.json")).read_text())


def test_resume_retry_transient_reruns_adapter_error_as_new_attempt(tmp_path, monkeypatch):
    manifest_path = _single_cell_manifest(tmp_path)
    root = tmp_path / "results"
    calls = _scripted_run(monkeypatch, ["adapter", "ok"])
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 3
    assert _only_cell(root)["state"] == "adapter_error"

    rc = model_trials.execute(
        manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=True, retry_transient=True
    )
    assert rc == 0
    assert calls == ["adapter", "ok"]
    cell = _only_cell(root)
    assert cell["state"] == "accepted"
    assert cell["attempt"] == 2
    assert [item["attempt"] for item in cell["prior_attempts"]] == [1]
    assert cell["prior_attempts"][0]["state"] == "adapter_error"
    assert cell["prior_attempts"][0]["failure_reason"] == "transport_drop"
    cell_dir = next((root / "cells").iterdir())
    assert sorted(p.name for p in (cell_dir / "attempts").iterdir()) == ["attempt-001", "attempt-002"]
    assert (cell_dir / "attempts" / "attempt-001" / "run" / "worker-results.json").is_file()


def test_resume_without_retry_transient_keeps_transient_cells_terminal(tmp_path, monkeypatch):
    manifest_path = _single_cell_manifest(tmp_path)
    root = tmp_path / "results"
    calls = _scripted_run(monkeypatch, ["adapter", "ok"])
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 3
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=True) == 3
    assert calls == ["adapter"]
    assert "prior_attempts" not in _only_cell(root)


def test_retry_transient_reruns_timeout_reason_cells_only(tmp_path, monkeypatch):
    manifest_path = _single_cell_manifest(tmp_path)
    root = tmp_path / "results"
    calls = _scripted_run(monkeypatch, ["timeout", "ok"])
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 3
    rc = model_trials.execute(
        manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=True, retry_transient=True
    )
    assert rc == 0
    assert calls == ["timeout", "ok"]
    cell = _only_cell(root)
    assert cell["state"] == "accepted"
    assert cell["prior_attempts"][0]["failure_reason"] == "timeout"
    assert cell["prior_attempts"][0]["state"] == "execution_error"


def test_retry_transient_never_reruns_deterministic_results(tmp_path, monkeypatch):
    for kind, expected_rc in (("crash", 1), ("wrong", 1)):
        root = tmp_path / f"results-{kind}"
        manifest_path = _single_cell_manifest(tmp_path)
        calls = _scripted_run(monkeypatch, [kind, "ok"])
        rc = model_trials.execute(
            manifest_path,
            _roster(),
            workspace=tmp_path,
            output_dir=root,
            resume=False,
            retry_transient=True,
        )
        assert rc == expected_rc
        assert calls == [kind]
        rc = model_trials.execute(
            manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=True, retry_transient=True
        )
        assert rc == expected_rc
        assert calls == [kind]
        assert "prior_attempts" not in _only_cell(root)


def test_run_with_retry_transient_retries_inline_up_to_the_cap(tmp_path, monkeypatch):
    manifest_path = _single_cell_manifest(tmp_path)
    root = tmp_path / "adapter"
    calls = _scripted_run(monkeypatch, ["adapter"])
    rc = model_trials.execute(
        manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False, retry_transient=True
    )
    assert rc == 3
    # adapter_error cap: two retries after the original attempt.
    assert calls == ["adapter"] * (1 + model_trials.RETRY_CAPS["adapter_error"])
    cell = _only_cell(root)
    assert cell["state"] == "adapter_error"
    assert cell["attempt"] == 3
    assert [item["attempt"] for item in cell["prior_attempts"]] == [1, 2]

    # A later resume with the flag does not exceed the cap.
    model_trials.execute(
        manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=True, retry_transient=True
    )
    assert len(calls) == 3

    root = tmp_path / "timeout"
    calls = _scripted_run(monkeypatch, ["timeout"])
    rc = model_trials.execute(
        manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False, retry_transient=True
    )
    assert rc == 3
    assert calls == ["timeout"] * (1 + model_trials.RETRY_CAPS["timeout"])
    assert len(_only_cell(root)["prior_attempts"]) == model_trials.RETRY_CAPS["timeout"]


def test_run_with_retry_transient_stops_retrying_after_success(tmp_path, monkeypatch):
    manifest_path = _single_cell_manifest(tmp_path)
    root = tmp_path / "results"
    calls = _scripted_run(monkeypatch, ["adapter", "adapter", "ok", "adapter"])
    rc = model_trials.execute(
        manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False, retry_transient=True
    )
    assert rc == 0
    assert calls == ["adapter", "adapter", "ok"]
    cell = _only_cell(root)
    assert cell["state"] == "accepted"
    assert cell["attempt"] == 3
    assert len(cell["prior_attempts"]) == 2


def test_regrade_preserves_prior_attempts(tmp_path, monkeypatch):
    manifest_path = _single_cell_manifest(tmp_path)
    root = tmp_path / "results"
    _scripted_run(monkeypatch, ["adapter", "ok"])
    model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False)
    model_trials.execute(
        manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=True, retry_transient=True
    )
    assert model_trials.regrade(root) == 0
    assert len(_only_cell(root)["prior_attempts"]) == 1


def test_cli_retry_transient_flag_reaches_execute_for_run_and_resume_only(tmp_path, monkeypatch, capsys):
    manifest_path = _single_cell_manifest(tmp_path)
    roster_path = tmp_path / "roster.toml"
    roster_path.write_text(
        'orchestrator = "chef"\n'
        '[agents.chef]\ncli = "codex"\nrole = "plan"\n'
        '[agents.cursor]\ncli = "cursor"\nmodel = "composer-2.5"\nrole = "answer"\n'
    )
    seen: list[dict] = []

    def fake_execute(manifest, roster, **kwargs):
        seen.append(kwargs)
        return 0

    monkeypatch.setattr(model_trials, "execute", fake_execute)
    base = ["--target", str(tmp_path), "--roster", str(roster_path)]
    assert cli.main(["model", "trial", "run", str(manifest_path), *base]) == 0
    assert cli.main(["model", "trial", "resume", str(manifest_path), *base, "--retry-transient"]) == 0
    assert cli.main(["model", "trial", "run", str(manifest_path), *base, "--retry-transient"]) == 0
    assert [bool(item.get("retry_transient")) for item in seen] == [False, True, True]
    try:
        cli.main(["model", "trial", "plan", str(manifest_path), *base, "--retry-transient"])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("plan must not accept --retry-transient")
    capsys.readouterr()


# --- #1977: clean drain -------------------------------------------------------


def _three_cell_manifest(tmp_path):
    manifest = _manifest()
    manifest["trials"] = 3
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(manifest))
    return manifest_path


def _cells_by_trial(root):
    cells = [json.loads(path.read_text()) for path in (root / "cells").glob("*/cell.json")]
    return {cell["trial"]: cell for cell in cells}


def _drain_during_first_cell(monkeypatch, trigger):
    calls: list[str] = []

    def fake_run(task, roster, **kwargs):
        calls.append(kwargs["output_dir"].parent.parent.parent.name)
        if len(calls) == 1:
            trigger()
        return _write_outcome(kwargs["output_dir"], "ok")

    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    return calls


def test_drain_marker_lets_in_flight_cell_finish_and_marks_rest_not_run(tmp_path, monkeypatch):
    manifest_path = _three_cell_manifest(tmp_path)
    root = tmp_path / "results"
    calls = _drain_during_first_cell(monkeypatch, lambda: model_trials.request_drain(root))
    rc = model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False)
    assert len(calls) == 1
    cells = _cells_by_trial(root)
    assert cells[1]["state"] == "accepted"
    for trial in (2, 3):
        assert cells[trial]["state"] == "not_run"
        assert cells[trial]["not_run_reason"] == "drained"
        assert "run_dir" not in cells[trial]
        assert "failure_reason" not in cells[trial]
    summary = json.loads((root / "summary.json").read_text())
    assert summary["counts"] == {"accepted": 1, "not_run": 2}
    assert summary["measurement_failures"] == 0
    # An incomplete trial must not look like a clean pass.
    assert rc == model_trials.EXIT_INCOMPLETE == 4


def test_sigterm_drains_then_restores_the_previous_handlers(tmp_path, monkeypatch):

    manifest_path = _three_cell_manifest(tmp_path)
    root = tmp_path / "results"
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    calls = _drain_during_first_cell(monkeypatch, lambda: os.kill(os.getpid(), signal.SIGTERM))
    rc = model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False)
    assert len(calls) == 1
    assert rc == 4
    cells = _cells_by_trial(root)
    assert cells[1]["state"] == "accepted"
    assert [cells[t]["state"] for t in (2, 3)] == ["not_run", "not_run"]
    assert {sig: signal.getsignal(sig) for sig in before} == before


def test_resume_after_drain_runs_only_the_unlaunched_cells(tmp_path, monkeypatch):
    manifest_path = _three_cell_manifest(tmp_path)
    root = tmp_path / "results"
    _drain_during_first_cell(monkeypatch, lambda: model_trials.request_drain(root))
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 4
    calls = _scripted_run(monkeypatch, ["ok"])
    # The drain marker from the earlier run must not stop the resume.
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=True) == 0
    assert len(calls) == 2
    assert {cell["state"] for cell in _cells_by_trial(root).values()} == {"accepted"}


def test_drain_keeps_existing_terminal_records_and_skips_retries(tmp_path, monkeypatch):
    manifest_path = _three_cell_manifest(tmp_path)
    root = tmp_path / "results"
    _scripted_run(monkeypatch, ["adapter"])
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 3
    states = {t: c["state"] for t, c in _cells_by_trial(root).items()}
    assert set(states.values()) == {"adapter_error"}

    calls: list[int] = []

    def fake_run(task, roster, **kwargs):
        calls.append(1)
        model_trials.request_drain(root)
        return _write_outcome(kwargs["output_dir"], "adapter")

    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    rc = model_trials.execute(
        manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=True, retry_transient=True
    )
    # One retry started before the drain; no inline retry follows it, and the
    # other failed cells keep their terminal records instead of becoming not_run.
    assert len(calls) == 1
    assert {c["state"] for c in _cells_by_trial(root).values()} == {"adapter_error"}
    assert rc == 3


def test_request_drain_writes_marker_and_cli_drain_command(tmp_path, capsys):
    root = tmp_path / "results"
    assert cli.main(["model", "trial", "drain", str(root)]) == 2
    assert "no trial plan" in capsys.readouterr().err
    root.mkdir()
    (root / "plan.json").write_text("{}")
    assert cli.main(["model", "trial", "drain", str(root)]) == 0
    marker = json.loads((root / model_trials.DRAIN_MARKER).read_text())
    assert marker["schema"] == model_trials.DRAIN_SCHEMA
    assert isinstance(marker["requested_at"], str)
    assert "drain requested" in capsys.readouterr().out


def test_process_exit_incomplete_sits_between_measurement_and_deterministic_failures():
    assert model_trials.process_exit({"measurement_failures": 0, "counts": {"accepted": 1, "not_run": 1}}) == 4
    assert model_trials.process_exit({"measurement_failures": 0, "counts": {"rejected": 1, "not_run": 1}}) == 4
    assert model_trials.process_exit({"measurement_failures": 1, "counts": {"adapter_error": 1, "not_run": 1}}) == 3


# --- #1977: quota and capacity stop -------------------------------------------


def _two_seat_roster() -> Roster:
    roster = _roster()
    return Roster(
        orchestrator=roster.orchestrator,
        agents={**roster.agents, "other": Agent("other", "cursor", "answer", model="composer-2.5")},
    )


def _quota_manifest(tmp_path):
    manifest = _manifest()
    manifest["trials"] = 2
    manifest["seats"] = ["cursor", "other"]
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(manifest))
    return manifest_path


def _write_capacity(out, *, structured: bool):
    out.mkdir(parents=True, exist_ok=True)
    (out / "run.json").write_text(json.dumps({"status": "failed"}))
    result: dict = {"worker": "cursor", "ok": False, "exit_code": 1, "transport": "cli"}
    if structured:
        result["failure"] = {
            "schema": "brigade.worker_failure.v1",
            "class": "capacity-exhausted",
            "domain": "infrastructure",
            "phase": "dispatch",
            "retry": "fallback",
            "detected_by": "adapter",
            "detail": "provider quota exhausted",
        }
    else:
        result["failure_kind"] = "rate-limit-error"
        result["detail"] = "usage limit reached"
    (out / "worker-results.json").write_text(json.dumps({"results": [result]}))
    return 1


def _seat_of(kwargs) -> str:
    return kwargs["worker"]


def test_capacity_response_is_its_own_failure_reason_not_a_model_failure(tmp_path, monkeypatch):
    for structured in (False, True):
        root = tmp_path / f"results-{structured}"
        manifest_path = _single_cell_manifest(tmp_path)

        def fake_run(task, roster, _structured=structured, **kwargs):
            return _write_capacity(kwargs["output_dir"], structured=_structured)

        monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
        rc = model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False)
        cell = _only_cell(root)
        # Nonzero exit would otherwise score as a deterministic execution_error.
        assert cell["state"] == "adapter_error"
        assert cell["failure_reason"] == "capacity-exhausted"
        summary = model_trials.summarize(root)
        assert summary["measurement_failures"] == 1
        assert summary["scores"]["count"] == 0
        assert summary["scores"]["mean"] is None
        assert summary["partial_scores"]["count"] == 0
        assert rc == 3


def test_other_provider_failures_still_score_as_execution_errors(tmp_path, monkeypatch):
    manifest_path = _single_cell_manifest(tmp_path)
    root = tmp_path / "results"

    def fake_run(task, roster, **kwargs):
        out = kwargs["output_dir"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "run.json").write_text(json.dumps({"status": "failed"}))
        (out / "worker-results.json").write_text(
            json.dumps(
                {
                    "results": [
                        {"worker": "cursor", "ok": False, "exit_code": 1, "failure_kind": "provider-error"},
                    ]
                }
            )
        )
        return 1

    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 1
    cell = _only_cell(root)
    assert cell["state"] == "execution_error"
    assert "failure_reason" not in cell
    summary = model_trials.summarize(root)
    assert summary["scores"]["count"] == 1
    assert summary["scores"]["mean"] == 0.0


def test_capacity_stops_remaining_cells_for_that_seat_only(tmp_path, monkeypatch):
    manifest_path = _quota_manifest(tmp_path)
    root = tmp_path / "results"
    seen: list[str] = []

    def fake_run(task, roster, **kwargs):
        seen.append(_seat_of(kwargs))
        if _seat_of(kwargs) == "cursor":
            return _write_capacity(kwargs["output_dir"], structured=False)
        return _write_outcome(kwargs["output_dir"], "ok")

    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    rc = model_trials.execute(manifest_path, _two_seat_roster(), workspace=tmp_path, output_dir=root, resume=False)
    assert seen == ["cursor", "other", "other"]
    cells = {c["coordinate"]: c for c in (json.loads(p.read_text()) for p in (root / "cells").glob("*/cell.json"))}
    assert cells["hello:cursor:1"]["failure_reason"] == "capacity-exhausted"
    skipped = cells["hello:cursor:2"]
    assert skipped["state"] == "not_run"
    assert skipped["not_run_reason"] == "capacity-exhausted"
    assert skipped["stopped_by"] == "hello:cursor:1"
    assert "failure_reason" not in skipped
    assert cells["hello:other:1"]["state"] == cells["hello:other:2"]["state"] == "accepted"
    summary = json.loads((root / "summary.json").read_text())
    assert summary["counts"] == {"accepted": 2, "adapter_error": 1, "not_run": 1}
    assert summary["measurement_failures"] == 1
    assert summary["scores"]["count"] == 2
    assert summary["scores"]["mean"] == 1.0
    assert rc == 3


def test_capacity_exhausted_cells_are_never_retried_as_transient(tmp_path, monkeypatch):
    manifest_path = _single_cell_manifest(tmp_path)
    root = tmp_path / "results"
    calls: list[int] = []

    def fake_run(task, roster, **kwargs):
        calls.append(1)
        return _write_capacity(kwargs["output_dir"], structured=True)

    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    kwargs = {"workspace": tmp_path, "output_dir": root, "retry_transient": True}
    assert model_trials.execute(manifest_path, _roster(), resume=False, **kwargs) == 3
    assert model_trials.execute(manifest_path, _roster(), resume=True, **kwargs) == 3
    assert len(calls) == 1
    assert "prior_attempts" not in _only_cell(root)


def test_resume_after_capacity_stop_launches_the_not_run_cells(tmp_path, monkeypatch):
    manifest_path = _quota_manifest(tmp_path)
    root = tmp_path / "results"

    def exhausted(task, roster, **kwargs):
        if _seat_of(kwargs) == "cursor":
            return _write_capacity(kwargs["output_dir"], structured=False)
        return _write_outcome(kwargs["output_dir"], "ok")

    monkeypatch.setattr(model_trials.aboyeur, "run", exhausted)
    model_trials.execute(manifest_path, _two_seat_roster(), workspace=tmp_path, output_dir=root, resume=False)
    calls = _scripted_run(monkeypatch, ["ok"])
    model_trials.execute(manifest_path, _two_seat_roster(), workspace=tmp_path, output_dir=root, resume=True)
    cells = {c["coordinate"]: c for c in (json.loads(p.read_text()) for p in (root / "cells").glob("*/cell.json"))}
    assert len(calls) == 1
    assert cells["hello:cursor:2"]["state"] == "accepted"
    assert cells["hello:cursor:1"]["failure_reason"] == "capacity-exhausted"


# --- #1977 review: drain through the real aboyeur.run decorator ---------------


def _signal_during_first_real_cell(monkeypatch, *signals):
    """Keep the real aboyeur.run (and its SIGTERM terminalizer); signal from inside the seat call."""
    prompts: list[str] = []
    sigint_before = signal.getsignal(signal.SIGINT)

    def fake_run_agent(cli_ref, prompt, **kwargs):
        prompts.append(prompt)
        if len(prompts) == 1:
            for index, signum in enumerate(signals):
                if index:
                    # Signals sent back to back coalesce; wait until the drain took the first
                    # one (it hands SIGINT back to the prior handler) before sending the next.
                    deadline = time.monotonic() + 5
                    while signal.getsignal(signal.SIGINT) is not sigint_before and time.monotonic() < deadline:
                        time.sleep(0.01)
                os.kill(os.getpid(), signum)
        return agents.AgentResult(text="hello\n", ok=True)

    monkeypatch.setattr(model_trials.aboyeur.agents, "run_agent", fake_run_agent)
    return prompts


def test_sigterm_during_a_real_aboyeur_cell_drains_instead_of_aborting(tmp_path, monkeypatch):
    manifest_path = _three_cell_manifest(tmp_path)
    root = tmp_path / "results"
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    prompts = _signal_during_first_real_cell(monkeypatch, signal.SIGTERM)
    rc = model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False)
    assert len(prompts) == 1
    cells = _cells_by_trial(root)
    assert cells[1]["state"] == "accepted"
    run_receipt = json.loads((Path(cells[1]["run_dir"]) / "run.json").read_text())
    assert run_receipt["status"] != "canceled"
    assert [cells[t]["state"] for t in (2, 3)] == ["not_run", "not_run"]
    assert json.loads((root / "summary.json").read_text())["counts"] == {"accepted": 1, "not_run": 2}
    assert rc == model_trials.EXIT_INCOMPLETE
    assert {sig: signal.getsignal(sig) for sig in before} == before


def test_sigint_during_a_real_aboyeur_cell_drains(tmp_path, monkeypatch):
    manifest_path = _three_cell_manifest(tmp_path)
    root = tmp_path / "results"
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    prompts = _signal_during_first_real_cell(monkeypatch, signal.SIGINT)
    rc = model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False)
    assert len(prompts) == 1
    cells = _cells_by_trial(root)
    assert cells[1]["state"] == "accepted"
    assert [cells[t]["state"] for t in (2, 3)] == ["not_run", "not_run"]
    assert rc == model_trials.EXIT_INCOMPLETE
    assert {sig: signal.getsignal(sig) for sig in before} == before


def test_second_sigterm_during_a_real_aboyeur_cell_still_terminalizes_the_run(tmp_path, monkeypatch):
    manifest_path = _three_cell_manifest(tmp_path)
    root = tmp_path / "results"
    before = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    _signal_during_first_real_cell(monkeypatch, signal.SIGTERM, signal.SIGTERM)
    with pytest.raises(SystemExit) as excinfo:
        model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False)
    assert excinfo.value.code == 128 + signal.SIGTERM
    run_dir = next((root / "cells").glob("*/attempts/attempt-001/run"))
    receipt = json.loads((run_dir / "run.json").read_text())
    assert receipt["status"] == "canceled"
    assert receipt["failure"]["kind"] == "signal"
    assert {sig: signal.getsignal(sig) for sig in before} == before


# --- #1977 review: retry history survives running and not_run records --------


def _seed_running_cell_with_history(tmp_path, root):
    manifest_path = _single_cell_manifest(tmp_path)
    plan, cells = model_trials.build_plan(manifest_path, _roster(), root)
    cell = cells[0]
    cell_dir = root / "cells" / cell.cell_id
    (cell_dir / "attempts" / "attempt-001" / "run").mkdir(parents=True)
    (cell_dir / "attempts" / "attempt-002" / "run").mkdir(parents=True)
    running = {
        "schema": model_trials.CELL_SCHEMA,
        **cell.payload(),
        "state": "running",
        "attempt": 2,
        "started_at": "2026-01-01T00:00:00+00:00",
        "manifest_digest": plan["manifest_digest"],
        "prior_attempts": [{"attempt": 1, "state": "adapter_error", "failure_reason": "transport_drop"}],
    }
    (cell_dir / "cell.json").write_text(json.dumps(running))
    return manifest_path, plan, cell, cell_dir, running


def test_resume_keeps_prior_attempts_and_retry_cap_of_a_running_cell(tmp_path, monkeypatch):
    root = tmp_path / "results"
    manifest_path, _plan, _cell, _cell_dir, _running = _seed_running_cell_with_history(tmp_path, root)
    calls = _scripted_run(monkeypatch, ["adapter"])
    rc = model_trials.execute(
        manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=True, retry_transient=True
    )
    # Attempt 1 failed and attempt 2 was orphaned, so only one more attempt fits the cap of 3.
    assert calls == ["adapter"]
    assert rc == 3
    cell = _only_cell(root)
    assert cell["attempt"] == 3
    assert [item["attempt"] for item in cell["prior_attempts"]] == [1, 2]
    assert cell["prior_attempts"][0]["state"] == "adapter_error"
    assert cell["prior_attempts"][1]["state"] == "interrupted"


def test_mark_not_run_keeps_prior_attempts_and_resume_honors_the_cap(tmp_path, monkeypatch):
    root = tmp_path / "results"
    manifest_path, plan, cell, cell_dir, running = _seed_running_cell_with_history(tmp_path, root)
    assert model_trials._mark_not_run(cell, cell_dir, running, plan, reason=model_trials.NOT_RUN_DRAINED)
    marked = json.loads((cell_dir / "cell.json").read_text())
    assert marked["state"] == "not_run"
    assert [item["attempt"] for item in marked["prior_attempts"]] == [1, 2]
    assert marked["prior_attempts"][1]["state"] == "interrupted"
    # Re-marking an already not_run cell must not duplicate the orphaned attempt.
    assert model_trials._mark_not_run(cell, cell_dir, marked, plan, reason=model_trials.NOT_RUN_DRAINED)
    assert json.loads((cell_dir / "cell.json").read_text())["prior_attempts"] == marked["prior_attempts"]

    calls = _scripted_run(monkeypatch, ["adapter"])
    model_trials.execute(
        manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=True, retry_transient=True
    )
    assert calls == ["adapter"]
    assert [item["attempt"] for item in _only_cell(root)["prior_attempts"]] == [1, 2]


@pytest.mark.parametrize("failure", ["adapter", "timeout"])
@pytest.mark.parametrize("mark_not_run", [False, True])
@pytest.mark.parametrize("retry_transient", [False, True])
def test_resume_never_launches_beyond_an_interrupted_final_retry(
    tmp_path, monkeypatch, failure, mark_not_run, retry_transient
):
    manifest_path = _single_cell_manifest(tmp_path)
    root = tmp_path / "results"
    _scripted_run(monkeypatch, [failure])
    assert model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False) == 3
    interrupted: list[int] = []

    def interrupt_provider(task, roster, **kwargs):
        interrupted.append(_only_cell(root)["attempt"])
        assert len(interrupted) <= cap, "provider launched beyond the retry cap"
        raise KeyboardInterrupt

    monkeypatch.setattr(model_trials.aboyeur, "run", interrupt_provider)
    kind = "adapter_error" if failure == "adapter" else "timeout"
    cap = model_trials.RETRY_CAPS[kind]
    for _ in range(cap):
        with pytest.raises(KeyboardInterrupt):
            model_trials.execute(
                manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=True, retry_transient=True
            )
    assert interrupted == list(range(2, cap + 2))
    current = _only_cell(root)
    assert current["state"] == "running"
    assert current["attempt"] == cap + 1
    if mark_not_run:
        plan, cells = model_trials.build_plan(manifest_path, _roster(), root)
        cell = cells[0]
        assert model_trials._mark_not_run(
            cell, root / "cells" / cell.cell_id, current, plan, reason=model_trials.NOT_RUN_DRAINED
        )
    stored = _only_cell(root)
    for _ in range(3):
        assert (
            model_trials.execute(
                manifest_path,
                _roster(),
                workspace=tmp_path,
                output_dir=root,
                resume=True,
                retry_transient=retry_transient,
            )
            == model_trials.EXIT_INCOMPLETE
        )
        assert _only_cell(root) == stored
    assert interrupted == list(range(2, cap + 2))
    cell_dir = next((root / "cells").iterdir())
    assert model_trials._attempt_number(cell_dir) == cap + 2
    assert not (cell_dir / "attempts" / f"attempt-{cap + 2:03d}").exists()


# --- #1977 review: output text never stops a seat -----------------------------


def test_rate_limit_text_in_a_model_answer_does_not_stop_the_seat(tmp_path, monkeypatch):
    manifest = _manifest()
    manifest["trials"] = 2
    manifest_path = tmp_path / "eval.json"
    manifest_path.write_text(json.dumps(manifest))
    prompts: list[str] = []

    def fake_run_agent(cli_ref, prompt, **kwargs):
        prompts.append(prompt)
        return agents.AgentResult(text="Error: rate limit exceeded, retry later\n", ok=True)

    monkeypatch.setattr(model_trials.aboyeur.agents, "run_agent", fake_run_agent)
    root = tmp_path / "results"
    rc = model_trials.execute(manifest_path, _roster(), workspace=tmp_path, output_dir=root, resume=False)
    cells = _cells_by_trial(root)
    # Both cells ran: the answer text is a model result, not a provider capacity response.
    assert len(prompts) == 2
    for cell in cells.values():
        assert cell["state"] not in {"not_run", "adapter_error"}
        assert cell.get("failure_reason") != "capacity-exhausted"
    assert "not_run" not in json.loads((root / "summary.json").read_text())["counts"]
    assert rc == 1


def test_legacy_rate_limit_kind_from_output_validation_is_not_capacity(tmp_path, monkeypatch):
    manifest_path = _quota_manifest(tmp_path)
    root = tmp_path / "results"
    seen: list[str] = []

    def fake_run(task, roster, **kwargs):
        seen.append(_seat_of(kwargs))
        out = kwargs["output_dir"]
        out.mkdir(parents=True, exist_ok=True)
        (out / "final.txt").write_text("Error: rate limit exceeded, retry later\n")
        (out / "run.json").write_text(json.dumps({"status": "failed"}))
        result = {
            "worker": _seat_of(kwargs),
            "ok": False,
            "exit_code": 1,
            "failure_phase": "output-validation",
            "failure_kind": "rate-limit-error",
            "detail": "provider returned an operational error instead of a final result",
        }
        (out / "worker-results.json").write_text(json.dumps({"results": [result]}))
        return 1

    monkeypatch.setattr(model_trials.aboyeur, "run", fake_run)
    rc = model_trials.execute(manifest_path, _two_seat_roster(), workspace=tmp_path, output_dir=root, resume=False)
    assert sorted(seen) == ["cursor", "cursor", "other", "other"]
    for path in (root / "cells").glob("*/cell.json"):
        cell = json.loads(path.read_text())
        assert cell["state"] == "execution_error"
        assert cell.get("failure_reason") != "capacity-exhausted"
    summary = model_trials.summarize(root)
    assert summary["scores"]["count"] == 4
    assert summary["scores"]["mean"] == 0.0
    assert rc == 1


def test_process_exit_counts_running_cells_as_incomplete():
    assert model_trials.process_exit({"measurement_failures": 0, "counts": {"accepted": 1, "running": 1}}) == 4
    assert model_trials.process_exit({"measurement_failures": 0, "counts": {"rejected": 1, "running": 1}}) == 4
    assert model_trials.process_exit({"measurement_failures": 1, "counts": {"running": 1}}) == 3
