"""Tests for the evidence station CLI (MiseLedger process-boundary sidecar)."""

from __future__ import annotations

import json

from brigade import evidence_cmd


def test_evidence_status_reports_uninstalled(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: None)

    payload = evidence_cmd.status_payload(tmp_path)

    assert payload["installed"] is False
    assert payload["health"] == "missing"
    assert "brigade add evidence" in payload["summary"]
    assert "brigade evidence crawl plan" in payload["next_commands"]
    assert payload["pipeline"][0].startswith("miseledger crawl")


def test_evidence_status_ok_with_status_json(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: "/x/miseledger")

    def fake_run(args, **kw):
        if args[:2] == ["/x/miseledger", "status"]:
            return evidence_cmd.proc.Result(0, json.dumps({"items": 12, "sources": ["sessions"]}), "")
        if args[:2] == ["/x/miseledger", "doctor"]:
            return evidence_cmd.proc.Result(0, json.dumps({"fail_count": 0, "warn_count": 0, "checks": []}), "")
        raise AssertionError(args)

    monkeypatch.setattr(evidence_cmd.proc, "run", fake_run)

    payload = evidence_cmd.status_payload(tmp_path)

    assert payload["installed"] is True
    assert payload["health"] == "ok"
    assert "items=12" in payload["summary"]
    assert any("receipts export miseledger" in cmd for cmd in payload["next_commands"])


def test_evidence_doctor_exits_nonzero_on_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: "/x/miseledger")

    def fake_run(args, **kw):
        if args[:2] == ["/x/miseledger", "status"]:
            return evidence_cmd.proc.Result(0, json.dumps({"items": 1}), "")
        if args[:2] == ["/x/miseledger", "doctor"]:
            return evidence_cmd.proc.Result(
                1,
                json.dumps(
                    {
                        "fail_count": 2,
                        "warn_count": 0,
                        "checks": [{"status": "FAIL", "name": "fts", "detail": "missing"}],
                    }
                ),
                "",
            )
        raise AssertionError(args)

    monkeypatch.setattr(evidence_cmd.proc, "run", fake_run)
    assert evidence_cmd.doctor(target=tmp_path) == 1


def test_crawl_plan_is_review_only(tmp_path):
    payload = evidence_cmd.crawl_plan_payload(target=tmp_path)
    rendered = evidence_cmd._render_plan_md(payload)

    assert ["miseledger", "crawl", "sessions"] in payload["commands"]
    assert "Brigade does not execute miseledger crawl" in payload["boundaries"][0]
    assert "miseledger crawl sessions" in rendered


def test_crawl_plan_write_creates_files(tmp_path):
    rc = evidence_cmd.crawl_plan(target=tmp_path, write=True, json_output=True)
    assert rc == 0
    plans = list((tmp_path / ".brigade" / "evidence" / "plans").glob("*/plan.json"))
    assert len(plans) == 1
    assert (plans[0].parent / "PLAN.md").exists()


def test_export_plan_points_at_receipts_export(tmp_path):
    payload = evidence_cmd.export_plan_payload(target=tmp_path)
    commands = [" ".join(cmd) for cmd in payload["commands"]]
    assert any("receipts export miseledger" in cmd for cmd in commands)
    assert payload["export_cursor_present"] is False
