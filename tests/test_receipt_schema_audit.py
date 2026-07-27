"""SIEM-readiness schema audit fixes for sanctioned receipt families (#506)."""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from brigade import aboyeur, outcome_cmd, receipt_schema, runguard
from brigade.work_cmd import helpers, verification as verify_mod


def _init_git_repo(path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True, capture_output=True)
    (path / "README.md").write_text("ok\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def test_verify_receipt_emits_schema_version(tmp_path, capsys):
    _init_git_repo(tmp_path)
    rc = verify_mod.verify_run(
        target=tmp_path,
        commands=[f"{sys.executable} -c \"print('ok')\""],
        reuse=False,
        json_output=True,
    )
    assert rc == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["schema_version"] == receipt_schema.VERIFY_RECEIPT_SCHEMA_VERSION
    stored = json.loads((Path(receipt["path"]) / "receipt.json").read_text())
    assert stored["schema_version"] == receipt_schema.VERIFY_RECEIPT_SCHEMA_VERSION


def test_verify_receipt_legacy_without_schema_version_still_loads(tmp_path):
    run_dir = tmp_path / ".brigade" / "work" / "verify-runs" / "legacy-run"
    run_dir.mkdir(parents=True)
    legacy = {
        "run_id": "legacy-run",
        "target": str(tmp_path),
        "status": "completed",
        "started_at": "2026-01-01T00:00:00+00:00",
        "commands": [],
    }
    (run_dir / "receipt.json").write_text(json.dumps(legacy) + "\n")
    loaded = verify_mod._verify_read_receipt(run_dir)
    assert loaded is not None
    assert loaded["run_id"] == "legacy-run"
    assert "schema_version" not in loaded


def test_finalize_verify_receipt_writes_once_when_summary_fails(tmp_path, monkeypatch):
    run_dir = tmp_path / "verify-run"
    run_dir.mkdir()
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    receipt = {
        "schema_version": receipt_schema.VERIFY_RECEIPT_SCHEMA_VERSION,
        "run_id": "verify-run",
        "target": str(tmp_path),
        "status": "running",
        "started_at": started.isoformat(),
        "path": str(run_dir),
        "commands": [
            {
                "command": "true",
                "status": "completed",
                "exit_code": 0,
            }
        ],
    }
    writes: list[Path] = []
    original_write = helpers._write_json

    def counting_write(path: Path, payload: object) -> None:
        if path.name == "receipt.json":
            writes.append(path)
        original_write(path, payload)

    monkeypatch.setattr(helpers, "_write_json", counting_write)
    monkeypatch.setattr(
        verify_mod,
        "_write_verify_markdown",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("summary failed")),
    )
    monkeypatch.setattr(verify_mod, "_prune_verify_runs", lambda *_args, **_kwargs: None)

    finalized, rc = verify_mod._finalize_verify_receipt(
        tmp_path,
        run_dir,
        receipt,
        started=started,
        rc=0,
        canceled=False,
    )

    assert rc == 0
    assert finalized["status"] == "completed"
    assert len(writes) == 1
    assert json.loads((run_dir / "receipt.json").read_text())["status"] == "completed"


def test_run_receipt_emits_schema_version_and_sorted_keys(tmp_path):
    payload = aboyeur._run_payload(
        task="audit",
        cwd=tmp_path,
        lock_workspace=tmp_path,
        roster=_minimal_roster(),
        dry_run=True,
        read_only=True,
        status="started",
        started_at=aboyeur.datetime.now(aboyeur.timezone.utc),
        include_git=False,
    )
    assert payload["schema_version"] == receipt_schema.RUN_RECEIPT_SCHEMA_VERSION
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    key_order = re.findall(r'^  "([^"]+)":', rendered, flags=re.MULTILINE)
    assert key_order == sorted(key_order)


def test_run_receipt_legacy_without_schema_version_still_loads(tmp_path):
    run_dir = tmp_path / ".brigade" / "runs" / "legacy-run"
    run_dir.mkdir(parents=True)
    legacy = {
        "schema": receipt_schema.RUN_RECEIPT_SCHEMA,
        "task": "legacy",
        "cwd": str(tmp_path),
        "orchestrator": "chef",
        "dry_run": False,
        "read_only": True,
        "status": "ok",
        "started_at": "2026-01-01T00:00:00Z",
        "status_started_at": "2026-01-01T00:00:00Z",
        "suspected_noop": False,
        "code_graph_brief": {"attached": False},
        "drift_impact_brief": {"attached": False},
        "evidence_brief": {"attached": False},
        "brief_budget": {"attached": []},
    }
    (run_dir / "run.json").write_text(json.dumps(legacy, sort_keys=True) + "\n")
    payload, run_json = outcome_cmd._read_run_receipt(run_dir)
    assert payload is not None
    assert run_json.name == "run.json"
    assert payload["schema"] == receipt_schema.RUN_RECEIPT_SCHEMA
    assert "schema_version" not in payload


def test_run_receipt_omits_null_cwd(tmp_path):
    payload = aboyeur._run_payload(
        task="audit",
        cwd=None,
        lock_workspace=tmp_path,
        roster=_minimal_roster(),
        dry_run=True,
        read_only=True,
        status="started",
        started_at=aboyeur.datetime.now(aboyeur.timezone.utc),
        include_git=False,
    )
    assert "cwd" not in payload


def test_run_json_writer_uses_sorted_keys(tmp_path):
    path = tmp_path / "run.json"
    aboyeur._write_json(path, {"z": 1, "a": 2, "m": {"y": 1, "b": 2}})
    text = path.read_text()
    assert text.index('"a"') < text.index('"m"') < text.index('"z"')
    nested = re.search(r'"m": \{\n(.*?)\n  \}', text, flags=re.DOTALL)
    assert nested is not None
    assert nested.group(1).index('"b"') < nested.group(1).index('"y"')


@pytest.mark.parametrize(
    ("document", "schema", "schema_version"),
    [
        (
            lambda: aboyeur._roster_payload(_minimal_roster()),
            receipt_schema.ROSTER_SNAPSHOT_SCHEMA,
            receipt_schema.ROSTER_SNAPSHOT_SCHEMA_VERSION,
        ),
        (
            lambda: receipt_schema.run_plan_document([]),
            receipt_schema.RUN_PLAN_SCHEMA,
            receipt_schema.RUN_PLAN_SCHEMA_VERSION,
        ),
        (
            lambda: receipt_schema.worker_results_document([], ground_truth={}),
            receipt_schema.WORKER_RESULTS_SCHEMA,
            receipt_schema.WORKER_RESULTS_SCHEMA_VERSION,
        ),
        (
            lambda: receipt_schema.synthesis_document(
                orchestrator="chef",
                result={"ok": True, "detail": "", "text": "done"},
                ground_truth={},
            ),
            receipt_schema.SYNTHESIS_SCHEMA,
            receipt_schema.SYNTHESIS_SCHEMA_VERSION,
        ),
    ],
)
def test_run_sidecar_emits_schema_version(document, schema, schema_version):
    payload = document()
    assert payload["schema"] == schema
    assert payload["schema_version"] == schema_version


def test_outcome_record_emits_schema_version(tmp_path):
    outcome_cmd.record(
        target=tmp_path,
        artifact_id="brigade-work",
        source="friction",
        status="cleared",
        json_output=True,
    )
    row = json.loads((tmp_path / "memory" / "outcome" / "records.jsonl").read_text().strip())
    assert row["schema_version"] == receipt_schema.OUTCOME_RECORD_SCHEMA_VERSION


def test_outcome_record_legacy_without_schema_version_still_loads(tmp_path):
    path = tmp_path / "memory" / "outcome" / "records.jsonl"
    path.parent.mkdir(parents=True)
    legacy = {
        "artifact_id": "brigade-work",
        "artifact_kind": "skill",
        "task_id": "",
        "source": "friction",
        "signal_value": 1,
        "evidence_ref": "",
        "ts": "2026-01-01T00:00:00+00:00",
        "prev_digest": None,
        "digest": "abc",
    }
    path.write_text(json.dumps(legacy, sort_keys=True) + "\n")
    records = outcome_cmd.load_records(tmp_path)
    assert len(records) == 1
    assert records[0].artifact_id == "brigade-work"


def test_recover_run_artifact_stamps_schema_on_missing_run_json(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    result = runguard._recover_run_artifact({"run_dir": str(run_dir), "pid": 4321})
    assert result == "recovered"
    payload = json.loads((run_dir / "run.json").read_text())
    assert payload["schema"] == receipt_schema.RUN_RECEIPT_SCHEMA
    assert payload["schema_version"] == receipt_schema.RUN_RECEIPT_SCHEMA_VERSION
    assert payload["status"] == "failed"
    assert payload["failure_phase"] == "stale-lock-recovery"
    assert payload["failure"]["owner_pid"] == 4321
    assert payload["failure"]["prior_status"] == "artifact-unavailable"


def test_set_artifact_patch_ref_stamps_schema_on_legacy_sidecars(tmp_path):
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    (output_dir / "worker-results.json").write_text(
        json.dumps(
            {
                "schema": receipt_schema.WORKER_RESULTS_SCHEMA,
                "results": [],
                "ground_truth": {},
            }
        )
        + "\n"
    )
    (output_dir / "synthesis.json").write_text(
        json.dumps(
            {
                "schema": receipt_schema.SYNTHESIS_SCHEMA,
                "orchestrator": "chef",
                "result": {"ok": True, "detail": "", "text": "done"},
                "ground_truth": {},
            }
        )
        + "\n"
    )
    aboyeur.set_artifact_patch_ref(output_dir, "changes.patch")
    worker = json.loads((output_dir / "worker-results.json").read_text())
    synthesis = json.loads((output_dir / "synthesis.json").read_text())
    assert worker["schema_version"] == receipt_schema.WORKER_RESULTS_SCHEMA_VERSION
    assert synthesis["schema_version"] == receipt_schema.SYNTHESIS_SCHEMA_VERSION
    assert worker["ground_truth"]["patch_ref"] == "changes.patch"


def test_record_dispatch_stage_stamps_schema_on_legacy_run_json(tmp_path):
    output_dir = tmp_path / "run"
    output_dir.mkdir()
    (output_dir / "run.json").write_text(
        json.dumps(
            {
                "schema": receipt_schema.RUN_RECEIPT_SCHEMA,
                "status": "planning",
                "started_at": "2026-01-01T00:00:00Z",
            }
        )
        + "\n"
    )
    aboyeur.record_dispatch_stage(output_dir, stage=1, seats=("coder",))
    payload = json.loads((output_dir / "run.json").read_text())
    assert payload["schema_version"] == receipt_schema.RUN_RECEIPT_SCHEMA_VERSION
    assert payload["status"] == "dispatching"


def test_verify_receipt_emits_null_identity_tuple_outside_git(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("GRAPHTRAIL_BIN", str(tmp_path / "missing-graphtrail"))
    rc = verify_mod.verify_run(
        target=tmp_path,
        commands=[f"{sys.executable} -c \"print('ok')\""],
        reuse=False,
        json_output=True,
    )
    assert rc == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["baseline_commit"] is None
    assert receipt["tree_fingerprint"] is None
    assert receipt["changes_patch_sha256"] is None
    stored = json.loads((Path(receipt["path"]) / "receipt.json").read_text())
    assert stored["baseline_commit"] is None
    assert stored["tree_fingerprint"] is None
    assert stored["changes_patch_sha256"] is None


def test_write_reused_receipt_omits_reused_from_without_source_run_id(tmp_path):
    receipt, rc = verify_mod._write_reused_receipt(
        tmp_path,
        {"commands": [], "status": "completed"},
        ["true"],
        60,
    )
    assert rc == 0
    assert "reused_from" not in receipt
    assert receipt["baseline_commit"] is None
    assert receipt["tree_fingerprint"] is None
    assert receipt["changes_patch_sha256"] is None


def test_work_closeout_emits_schema_version(tmp_path, monkeypatch):
    session_path = tmp_path / ".brigade" / "work" / "20260101-session"
    session_path.mkdir(parents=True)
    session_payload = {
        "id": "20260101-session",
        "status": "ended",
        "title": "Audit",
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T01:00:00+00:00",
    }
    (session_path / "session.json").write_text(json.dumps(session_payload) + "\n")
    monkeypatch.setattr(
        verify_mod,
        "_verification_evidence_payload",
        lambda _target, _session: {
            "latest_verify": {
                "run_id": "verify-run",
                "status": "completed",
                "path": str(tmp_path / "verify"),
                "commands": [{"command": "true"}],
            },
            "task": {"id": "task-one", "text": "Audit"},
            "task_acceptance": ["Done"],
            "scanner_sweep": {},
            "code_review": {},
            "handoff_drafts": {},
        },
    )
    closeout, rc = verify_mod._work_closeout_payload(tmp_path, "20260101-session", write=True)
    assert rc == 0
    assert closeout["schema_version"] == receipt_schema.WORK_CLOSEOUT_SCHEMA_VERSION
    stored = json.loads(Path(closeout["path"]).read_text())
    assert stored["schema_version"] == receipt_schema.WORK_CLOSEOUT_SCHEMA_VERSION


def test_outcome_decision_emits_schema_version(tmp_path, monkeypatch):
    from tests.test_outcome_cmd import _write_registry_skill
    from tests.test_scorecard_reconcile import _stub_execute, seed_registry_skill_scorecard_promotion

    _stub_execute(monkeypatch)
    _write_registry_skill(tmp_path, "skill-x")
    seed_registry_skill_scorecard_promotion(tmp_path, "skill-x")
    assert outcome_cmd.reconcile(target=tmp_path, apply=True, json_output=True) == 0
    decision_path = next((tmp_path / "memory" / "outcome" / "decisions").glob("*.json"))
    decision = json.loads(decision_path.read_text())
    assert decision["schema_version"] == receipt_schema.OUTCOME_DECISION_SCHEMA_VERSION


def test_sorted_receipt_writer_is_byte_deterministic(tmp_path):
    payload = {
        "schema": receipt_schema.RUN_RECEIPT_SCHEMA,
        "schema_version": receipt_schema.RUN_RECEIPT_SCHEMA_VERSION,
        "task": "audit",
        "orchestrator": "chef",
        "dry_run": False,
        "read_only": True,
        "status": "ok",
        "started_at": "2026-01-01T00:00:00Z",
        "nested": {"z": 1, "a": 2},
    }
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    aboyeur._write_json(first, payload)
    aboyeur._write_json(second, payload)
    assert first.read_bytes() == second.read_bytes()


def _minimal_roster():
    from brigade.roster import Agent, Roster

    return Roster(
        orchestrator="chef",
        max_workers=1,
        agents={
            "chef": Agent(
                name="chef",
                cli="echo",
                role="orchestrator",
                model="test",
                transport="local",
            )
        },
    )
