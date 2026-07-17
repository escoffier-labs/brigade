"""Tests for `brigade operator checkup`."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import cli
from brigade.operator_cmd import lifecycle


def _stub(rc: int, payload: dict):
    def _doctor(**kwargs):
        print(json.dumps(payload))
        return rc

    return _doctor


def _patch_all_doctors(monkeypatch, *, skills_rc: int = 0):
    monkeypatch.setattr(lifecycle.core_doctor, "run", _stub(0, {"ready": True, "summary": {"failed": 0}}))
    monkeypatch.setattr(lifecycle, "operator_doctor", _stub(0, {"ready": True, "blocking_issue_count": 0}))
    monkeypatch.setattr(lifecycle.handoff_cmd, "doctor", _stub(0, {"issue_count": 0}))
    monkeypatch.setattr(lifecycle.tools_cmd, "doctor", _stub(0, {"issue_count": 0}))
    monkeypatch.setattr(lifecycle.skills_cmd, "doctor", _stub(skills_rc, {"issue_count": 3 if skills_rc else 0}))
    monkeypatch.setattr(lifecycle.security_cmd, "doctor", _stub(0, {"issue_count": 0}))


def test_operator_checkup_rolls_up_each_first_run_doctor(monkeypatch, capsys):
    _patch_all_doctors(monkeypatch, skills_rc=1)  # one failing surface
    rc = lifecycle.checkup(target=Path("."), json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["ready"] is False
    assert payload["blocking_surface_count"] == 1
    assert [s["name"] for s in payload["surfaces"]] == [
        "doctor",
        "operator",
        "handoff",
        "tools",
        "skills",
        "security",
    ]
    skills = next(s for s in payload["surfaces"] if s["name"] == "skills")
    assert skills["ready"] is False
    assert skills["issue_count"] == 3
    assert payload["next_command"] == "brigade skills doctor --target ."
    assert "loop" in payload
    assert set(payload["loop"]) == {"graph", "ledger", "context_eval"}


def test_operator_checkup_is_ready_when_all_surfaces_pass(monkeypatch, capsys):
    _patch_all_doctors(monkeypatch, skills_rc=0)
    rc = lifecycle.checkup(target=Path("."), json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["ready"] is True
    assert payload["blocking_surface_count"] == 0
    assert payload["next_command"] is None


def test_operator_checkup_loop_reports_graph_ledger_and_brief_hit_rate(monkeypatch, tmp_path, capsys):
    _patch_all_doctors(monkeypatch, skills_rc=0)
    monkeypatch.setattr("brigade.context_cmd._graphtrail_bin", lambda: "/usr/bin/graphtrail")
    monkeypatch.setattr("brigade.evidence_brief._miseledger_bin", lambda: "/usr/bin/miseledger")
    db = tmp_path / ".graphtrail" / "graphtrail.db"
    db.parent.mkdir(parents=True)
    db.write_text("ok")
    run_dir = tmp_path / ".brigade" / "runs" / "2026-07-09T00-00-00Z"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "started_at": "2026-07-09T00:00:00Z",
                "context_eval": {
                    "brief_hit_rate": 0.75,
                    "hits": ["a.py"],
                    "missed": ["b.py"],
                },
            }
        )
    )

    rc = lifecycle.checkup(target=tmp_path, json_output=False)
    out = capsys.readouterr().out
    assert rc == 0
    assert "loop:" in out
    assert "[ok] graph:" in out
    assert "[ok] ledger:" in out
    assert "[ok] brief_hit_rate:" in out
    assert "last=0.750" in out

    rc = lifecycle.checkup(target=tmp_path, json_output=True)
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["loop"]["graph"]["ok"] is True
    assert payload["loop"]["ledger"]["ok"] is True
    assert payload["loop"]["context_eval"]["last_brief_hit_rate"] == 0.75
    assert payload["loop"]["context_eval"]["sample_count"] == 1


def test_operator_checkup_cli_dispatch(monkeypatch, tmp_path):
    seen = {}

    def fake_checkup(**kwargs):
        seen.update(kwargs)
        return 0

    from brigade import operator_cmd

    monkeypatch.setattr(operator_cmd, "checkup", fake_checkup)
    assert cli.main(["operator", "checkup", "--target", str(tmp_path), "--json"]) == 0
    assert seen == {"target": tmp_path, "profile": "internal-dogfood", "json_output": True}


def test_operator_checkup_scoped_runs_only_selected_doctor(monkeypatch, tmp_path, capsys):
    called = []

    def selected(**kwargs):
        called.append("doctor")
        print(json.dumps({"ready": True, "summary": {"failed": 0}}))
        return 0

    def unexpected(**kwargs):
        raise AssertionError("unselected doctor ran")

    monkeypatch.setattr(lifecycle.core_doctor, "run", selected)
    monkeypatch.setattr(lifecycle, "operator_doctor", unexpected)
    monkeypatch.setattr(lifecycle.handoff_cmd, "doctor", unexpected)
    monkeypatch.setattr(lifecycle.tools_cmd, "doctor", unexpected)
    monkeypatch.setattr(lifecycle.skills_cmd, "doctor", unexpected)
    monkeypatch.setattr(lifecycle.security_cmd, "doctor", unexpected)

    rc = lifecycle.checkup(target=tmp_path, surfaces=["doctor"], json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert called == ["doctor"]
    assert payload["ready"] is True
    assert payload["selected_ready"] is True
    assert payload["overall_ready"] is None
    assert payload["selected_surfaces"] == ["doctor"]
    assert payload["skipped_surfaces"] == [
        "operator",
        "handoff",
        "tools",
        "skills",
        "security",
        "work",
        "graph",
        "ledger",
    ]
    assert payload["surfaces"][0]["name"] == "doctor"
    assert isinstance(payload["surfaces"][0]["elapsed_seconds"], float)


def test_operator_checkup_lists_surfaces_and_evidence_loop_preset(tmp_path, capsys):
    assert cli.main(["operator", "checkup", "--target", str(tmp_path), "--list-surfaces", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["default_surfaces"] == ["doctor", "operator", "handoff", "tools", "skills", "security"]
    assert payload["presets"]["evidence-loop"] == ["work", "graph", "ledger"]
    assert set(payload["surface_names"]) == {
        "doctor",
        "operator",
        "handoff",
        "tools",
        "skills",
        "security",
        "work",
        "graph",
        "ledger",
    }


def test_operator_checkup_cli_routes_repeated_surfaces(monkeypatch, tmp_path):
    seen = {}

    def fake_checkup(**kwargs):
        seen.update(kwargs)
        return 0

    from brigade import operator_cmd

    monkeypatch.setattr(operator_cmd, "checkup", fake_checkup)
    assert (
        cli.main(
            [
                "operator",
                "checkup",
                "--target",
                str(tmp_path),
                "--surface",
                "work",
                "--surface",
                "graph",
                "--json",
            ]
        )
        == 0
    )
    assert seen == {
        "target": tmp_path,
        "profile": "internal-dogfood",
        "surfaces": ["work", "graph"],
        "json_output": True,
    }


def test_operator_checkup_evidence_loop_preset_runs_only_evidence_surfaces(monkeypatch, tmp_path, capsys):
    called = []

    def evidence_surface(name):
        def check(**kwargs):
            called.append(name)
            print(json.dumps({"ready": True, "issue_count": 0, "name": name}))
            return 0

        return check

    monkeypatch.setattr(lifecycle, "_checkup_work", evidence_surface("work"))
    monkeypatch.setattr(lifecycle, "_checkup_graph", evidence_surface("graph"))
    monkeypatch.setattr(lifecycle, "_checkup_ledger", evidence_surface("ledger"))

    rc = lifecycle.checkup(target=tmp_path, preset="evidence-loop", json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert called == ["work", "graph", "ledger"]
    assert payload["selected_ready"] is True
    assert payload["overall_ready"] is None
    assert payload["selected_surfaces"] == ["work", "graph", "ledger"]
    assert [surface["name"] for surface in payload["surfaces"]] == ["work", "graph", "ledger"]
    assert all("details" in surface for surface in payload["surfaces"])


def test_operator_checkup_evidence_surface_details_drive_exit(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        lifecycle,
        "_checkup_work",
        _stub(1, {"ready": False, "issue_count": 2, "next_command": "brigade receipts verify --target ."}),
    )

    rc = lifecycle.checkup(target=tmp_path, surfaces=["work"], json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["selected_ready"] is False
    assert payload["blocking_surface_count"] == 1
    assert payload["surfaces"][0]["details"]["issue_count"] == 2
    assert payload["next_command"] == "brigade operator checkup --target . --surface work"


def test_operator_checkup_rejects_surface_with_preset(tmp_path, capsys):
    rc = lifecycle.checkup(
        target=tmp_path,
        surfaces=["work"],
        preset="evidence-loop",
        json_output=True,
    )

    assert rc == 2
    assert "--surface and --preset cannot be combined" in capsys.readouterr().err


def test_operator_checkup_work_requires_receipt_integrity_and_outcome_capture(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        "brigade.receipts_cmd.verify_payload",
        lambda target: {
            "artifacts": [
                {"artifact_type": "work-verify-receipt", "status": "OK"},
                {"artifact_type": "runbook-receipt", "status": "MISSING"},
            ]
        },
    )
    monkeypatch.setattr(
        "brigade.work_cmd.verification._verify_receipts",
        lambda target: [{"run_id": "verify-1", "status": "completed", "started_at": "2026-07-16T00:00:00Z"}],
    )
    monkeypatch.setattr(
        "brigade.outcome_cmd.health",
        lambda target: {"issue_count": 0, "record_count": 1, "verify_run_count": 1},
    )

    rc = lifecycle._checkup_work(target=tmp_path, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["receipt_integrity"] == {"artifact_count": 1, "failure_count": 0}
    assert payload["latest_receipt"]["run_id"] == "verify-1"
    assert payload["outcome_capture"]["record_count"] == 1


def test_operator_checkup_work_fails_tampered_receipt_without_double_counting_dormancy(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        "brigade.receipts_cmd.verify_payload",
        lambda target: {"artifacts": [{"artifact_type": "work-verify-receipt", "status": "MISMATCH"}]},
    )
    monkeypatch.setattr("brigade.work_cmd.verification._verify_receipts", lambda target: [])
    monkeypatch.setattr(
        "brigade.outcome_cmd.health",
        lambda target: {"issue_count": 1, "top_issue": {"name": "outcome_loop_dormant"}},
    )

    rc = lifecycle._checkup_work(target=tmp_path, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["issue_count"] == 2
    assert payload["receipt_integrity"]["failure_count"] == 1
    assert payload["next_command"] == "brigade receipts verify --target ."


def test_operator_checkup_graph_requires_live_db_and_fresh_delta(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        "brigade.search_cmd.status_payload",
        lambda target: {
            "tools": {
                "graphtrail": {
                    "installed": True,
                    "db_present": True,
                    "health": "ok",
                    "summary": "graph healthy",
                }
            }
        },
    )
    monkeypatch.setattr(
        "brigade.work_cmd.verification._latest_verify_receipt",
        lambda target: {
            "run_id": "verify-1",
            "code_graph_delta": {"status": "ok", "stale_graph_used": False, "changed_symbol_count": 2},
        },
    )

    rc = lifecycle._checkup_graph(target=tmp_path, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["graphtrail"]["health"] == "ok"
    assert payload["code_graph_delta"]["changed_symbol_count"] == 2


def test_operator_checkup_graph_missing_delta_requests_new_verification(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        "brigade.search_cmd.status_payload",
        lambda target: {"tools": {"graphtrail": {"installed": True, "db_present": True, "health": "ok"}}},
    )
    monkeypatch.setattr(
        "brigade.work_cmd.verification._latest_verify_receipt",
        lambda target: {"run_id": "verify-1"},
    )

    rc = lifecycle._checkup_graph(target=tmp_path, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["next_command"] == ("brigade work verify run --target . --command '<check>' --capture brigade-work")


def test_operator_checkup_ledger_reports_pending_work_receipt_imports(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        "brigade.evidence_cmd.status_payload",
        lambda target, include_doctor: {
            "installed": True,
            "health": "ok",
            "summary": "miseledger status ok",
            "export_cursor_present": True,
        },
    )
    monkeypatch.setattr(
        "brigade.work_cmd.verification._verify_receipts",
        lambda target: [{"run_id": "verify-1", "path": str(tmp_path / "verify-1")}],
    )
    monkeypatch.setattr("brigade.receipts_cmd._read_miseledger_cursor_hashes", lambda target: set())
    monkeypatch.setattr(
        "brigade.receipts_cmd._receipt_hash",
        lambda payload, path: ("sha256:pending", "receipt_digest"),
    )

    rc = lifecycle._checkup_ledger(target=tmp_path, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 1
    assert payload["pending_work_receipt_count"] == 1
    assert payload["next_command"] == "brigade receipts export miseledger --target . --new-only --import"


def test_operator_checkup_ledger_treats_empty_work_receipts_as_no_import_backlog(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(
        "brigade.evidence_cmd.status_payload",
        lambda target, include_doctor: {
            "installed": True,
            "health": "ok",
            "summary": "miseledger status ok",
            "export_cursor_present": False,
        },
    )
    monkeypatch.setattr("brigade.work_cmd.verification._verify_receipts", lambda target: [])
    monkeypatch.setattr("brigade.receipts_cmd._read_miseledger_cursor_hashes", lambda target: set())

    rc = lifecycle._checkup_ledger(target=tmp_path, json_output=True)
    payload = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert payload["ready"] is True
    assert payload["pending_work_receipt_count"] == 0


def test_operator_checkup_scoped_text_labels_selected_readiness(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(lifecycle.core_doctor, "run", _stub(0, {"ready": True, "summary": {"failed": 0}}))

    rc = lifecycle.checkup(target=tmp_path, surfaces=["doctor"], json_output=False)
    out = capsys.readouterr().out

    assert rc == 0
    assert "selected_ready: yes" in out
    assert "overall_ready: not evaluated" in out
