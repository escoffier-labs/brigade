"""Three-phase task footprint lifecycle (#777)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from brigade import work_cmd
from brigade.work_cmd import footprint as footprint_mod

from tests.work_cmd_test_helpers import _init_git_repo, _write_json


def _add(target: Path, text: str, **kwargs):
    task, created = work_cmd._add_task(target, text, **kwargs)
    assert created
    return task


def test_filing_degrades_to_empty_predicted_without_graphtrail(tmp_path):
    """Real degrade path: no graphtrail binary/index in this VM."""
    _init_git_repo(tmp_path)
    task = _add(
        tmp_path,
        "Touch ledger helpers",
        metadata={"symbol_ids": ["brigade.work_cmd.ledger._add_task"]},
    )
    footprint = task["metadata"]["footprint"]
    assert footprint["phase"] == "predicted"
    assert footprint["files"] == []
    assert footprint["symbol_ids"] == []
    assert footprint["snapshot_hash"] == ""
    assert footprint["degraded"] is True
    assert "graphtrail" in footprint["degraded_reason"]


def test_filing_predicted_enrichment_via_mocked_impact(tmp_path, monkeypatch):
    _init_git_repo(tmp_path)
    db = tmp_path / ".graphtrail" / "graphtrail.db"
    db.parent.mkdir(parents=True)
    db.write_bytes(b"fake-graphtrail-index")

    monkeypatch.setattr(footprint_mod.component_bins, "resolve", lambda _name: "/fake/graphtrail")

    def fake_impact(target, binary, db_path, query):
        assert binary == "/fake/graphtrail"
        assert db_path == db
        assert query == "pkg.mod.fn"
        return {
            "related_files": ["src/pkg/mod.py", "tests/test_mod.py"],
            "symbol_ids": ["pkg.mod.fn", "pkg.mod.helper"],
        }

    monkeypatch.setattr(footprint_mod, "_run_graphtrail_impact", fake_impact)

    task = _add(
        tmp_path,
        "Enrich me",
        metadata={"symbol_ids": ["pkg.mod.fn"]},
    )
    footprint = task["metadata"]["footprint"]
    assert footprint["phase"] == "predicted"
    assert footprint["files"] == ["src/pkg/mod.py", "tests/test_mod.py"]
    assert footprint["symbol_ids"] == ["pkg.mod.fn", "pkg.mod.helper"]
    assert footprint["snapshot_hash"]
    assert "degraded" not in footprint


def test_three_phase_writes_predict_refine_reconcile(tmp_path, monkeypatch, capsys):
    _init_git_repo(tmp_path)
    monkeypatch.setattr(
        work_cmd.helpers,
        "_now",
        lambda: datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
    )

    # Phase 1: predicted (degraded in this environment).
    task = _add(tmp_path, "Three phase task", metadata={"symbol_ids": ["pkg.a"]})
    predicted = task["metadata"]["footprint"]
    assert predicted["phase"] == "predicted"

    # Write a plan that names concrete files, then claim (phase 2: refined).
    assert (
        work_cmd.task_plan(
            target=tmp_path,
            task_id=task["id"],
            write=True,
            steps=["Edit src/brigade/work_cmd/footprint.py", "Cover tests/test_work_cmd_footprint.py"],
            accept=True,
        )
        == 0
    )
    capsys.readouterr()
    assert work_cmd.task_claim(target=tmp_path, task_id=task["id"], actor="lane-a", json_output=True) == 0
    claimed = json.loads(capsys.readouterr().out)
    assert claimed["status"] == "in_progress"
    assert claimed["assignee"] == "lane-a"
    refined = claimed["footprint"]
    assert refined["phase"] == "refined"
    assert "src/brigade/work_cmd/footprint.py" in refined["files"]
    assert "tests/test_work_cmd_footprint.py" in refined["files"]

    # Phase 3: reconcile from verify receipt code_graph_delta / graphtrail_delta.
    run_dir = tmp_path / ".brigade" / "work" / "verify-runs" / "20260809-120000-work-verify"
    run_dir.mkdir(parents=True)
    _write_json(
        run_dir / "receipt.json",
        {
            "run_id": "20260809-120000-work-verify",
            "status": "completed",
            "started_at": "2026-08-09T12:00:00+00:00",
            "code_graph_delta": {
                "ok": True,
                "status": "ok",
                "changed_symbols": ["pkg.a", "pkg.b"],
                "code_reference_nodes": [
                    {
                        "kind": "function",
                        "qualified_name": "pkg.a",
                        "file_path": "src/pkg/a.py",
                        "start_line": 1,
                        "end_line": 2,
                    },
                    {
                        "kind": "function",
                        "qualified_name": "pkg.b",
                        "file_path": "src/pkg/b.py",
                        "start_line": 3,
                        "end_line": 4,
                    },
                ],
                "attestations": {"after_snapshot_sha256": "abc123snapshot"},
            },
        },
    )

    assert work_cmd.task_done(target=tmp_path, task_id=task["id"], json_output=True) == 0
    done = json.loads(capsys.readouterr().out)
    assert done["status"] == "done"
    reconciled = done["footprint"]
    assert reconciled["phase"] == "reconciled"
    assert reconciled["files"] == ["src/pkg/a.py", "src/pkg/b.py"]
    assert reconciled["symbol_ids"] == ["pkg.a", "pkg.b"]
    assert reconciled["snapshot_hash"] == "abc123snapshot"

    stored, _ = work_cmd._find_task(tmp_path, task["id"])
    assert stored["metadata"]["footprint"]["phase"] == "reconciled"


def test_reconcile_joins_graphtrail_delta_alias(tmp_path):
    prior = footprint_mod.normalize_footprint(
        {"files": ["old.py"], "symbol_ids": ["old.sym"], "snapshot_hash": "oldhash"},
        phase="refined",
    )
    receipt = {
        "graphtrail_delta": {
            "changed_symbols": ["new.sym"],
            "changed_files": ["new.py"],
            "snapshot_hash": "newhash",
        }
    }
    delta = footprint_mod.receipt_graph_delta(receipt)
    reconciled = footprint_mod.reconcile_footprint(delta, prior=prior)
    assert reconciled["phase"] == "reconciled"
    assert reconciled["files"] == ["new.py"]
    assert reconciled["symbol_ids"] == ["new.sym"]
    assert reconciled["snapshot_hash"] == "newhash"


def test_claim_accepts_explicit_files_without_plan(tmp_path, capsys):
    _init_git_repo(tmp_path)
    task = _add(tmp_path, "Explicit files claim")
    assert (
        work_cmd.task_claim(
            target=tmp_path,
            task_id=task["id"],
            files=["src/a.py", "src/b.py"],
            from_plan=False,
            json_output=True,
        )
        == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["footprint"]["phase"] == "refined"
    assert payload["footprint"]["files"] == ["src/a.py", "src/b.py"]


def test_extract_plan_files_from_steps_and_source_context():
    files = footprint_mod.extract_plan_files(
        {
            "steps": ["Update src/brigade/cli/work/registration.py", "plain prose"],
            "source_context": ["see docs/technical-guide.md for detail"],
            "files": ["explicit/path.py"],
        }
    )
    assert files == [
        "explicit/path.py",
        "src/brigade/cli/work/registration.py",
        "docs/technical-guide.md",
    ]
