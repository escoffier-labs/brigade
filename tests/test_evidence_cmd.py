"""Tests for the evidence station CLI (MiseLedger process-boundary sidecar)."""

from __future__ import annotations

import json
from pathlib import Path

from brigade import code_cmd, evidence_cmd, search_cmd


def test_evidence_status_reports_uninstalled(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: None)

    payload = evidence_cmd.status_payload(tmp_path)

    assert payload["installed"] is False
    assert payload["health"] == "missing"
    assert "brigade add evidence" in payload["summary"]
    assert "brigade evidence crawl plan" in payload["next_commands"]
    assert payload["pipeline"][0].startswith("miseledger crawl")


def test_evidence_status_distinguishes_explicit_execution_from_review_only_plans(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: None)

    payload = evidence_cmd.status_payload(tmp_path)
    boundaries = payload["boundaries"]

    assert (
        "Explicit user-invoked `brigade evidence crawl` and `brigade evidence search` execute MiseLedger across a process boundary."
        in boundaries
    )
    assert (
        "Review-only `brigade evidence crawl plan` and `brigade evidence export plan` never execute MiseLedger."
        in boundaries
    )
    assert "Brigade does not start daemons or upload data; receipt export remains local." in boundaries
    assert "Brigade does not crawl sessions or import adapter JSONL from these commands." not in boundaries
    assert "only: they do not crawl sessions" not in (evidence_cmd.__doc__ or "")
    repo_root = Path(__file__).parents[1]
    quickstart = (repo_root / "QUICKSTART.md").read_text()
    station_contract = (repo_root / "docs" / "station-contract.md").read_text()
    search_docstring = search_cmd.__doc__ or ""
    evidence_docstring = evidence_cmd.__doc__ or ""

    assert "brigade evidence crawl sessions" in quickstart
    assert "brigade evidence search" in quickstart
    assert "brigade evidence crawl plan     # review-only" in quickstart
    assert "brigade evidence export plan    # review-only" in quickstart
    assert "brigade code sync .             # preferred GraphTrail facade" in quickstart
    assert "brigade search sync|context|impact` remain compatibility aliases" in quickstart
    assert "explicitly runs local MiseLedger for `brigade evidence crawl|search`" in station_contract
    assert "crawl/export plans are review-only" in station_contract
    assert "Explicit user-invoked" in evidence_docstring
    assert "brigade code sync|context|impact" in search_docstring
    assert "compatibility aliases" in search_docstring
    assert "sync plan`` stays\nreview-only" in search_docstring
    for text in (quickstart, station_contract, search_docstring, evidence_docstring):
        assert "does not crawl for you" not in text


def test_code_run_relays_child_stderr_unchanged(monkeypatch, capsys):
    monkeypatch.setattr(code_cmd.context_cmd, "_graphtrail_bin", lambda: "/x/graphtrail")
    monkeypatch.setattr(code_cmd.proc, "run", lambda *_, **__: code_cmd.proc.Result(7, "", "graph warning\\n"))

    assert code_cmd.run("impact", ["brigade.cli.main"]) == 7
    assert capsys.readouterr().err == "graph warning\\n"


def test_evidence_run_engine_relays_child_stderr_unchanged(monkeypatch, capsys):
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: "/x/miseledger")
    monkeypatch.setattr(evidence_cmd.proc, "run", lambda *_, **__: evidence_cmd.proc.Result(6, "", "ledger warning\\n"))

    assert evidence_cmd.run_engine("search", ["needle"]) == 6
    assert capsys.readouterr().err == "ledger warning\\n"


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


def test_evidence_summary_status_skips_doctor(monkeypatch, tmp_path):
    monkeypatch.setattr(evidence_cmd.evidence_brief, "_miseledger_bin", lambda: "/x/miseledger")
    calls = []

    def fake_run_json(args, *, timeout):
        calls.append((args, timeout))
        return {
            "command": args,
            "exit_code": 0,
            "stdout_json": {"items": 12},
            "stdout_unparsed": None,
            "stderr": "",
        }

    monkeypatch.setattr(evidence_cmd, "_run_json", fake_run_json)

    payload = evidence_cmd.status_payload(tmp_path, include_doctor=False, timeout=5.0)

    assert payload["health"] == "ok"
    assert calls == [(["/x/miseledger", "status", "--json"], 5.0)]
    assert payload["doctor"] is None


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
    assert "review-only crawl plan never executes MiseLedger" in payload["boundaries"][0]
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
