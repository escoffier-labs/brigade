"""Tests for the search station CLI (GraphTrail + code-search process-boundary sidecars)."""

from __future__ import annotations

import json

from brigade import search_cmd


def test_search_status_reports_uninstalled(monkeypatch, tmp_path):
    monkeypatch.setattr(search_cmd.proc, "which", lambda cmd: None)

    payload = search_cmd.status_payload(tmp_path)

    assert payload["installed"] is False
    assert payload["health"] == "missing"
    assert "brigade add" in payload["summary"]
    assert "brigade search sync plan" in payload["next_commands"]
    assert payload["pipeline"][0] == "graphtrail sync"


def test_search_status_ok_with_graphtrail(monkeypatch, tmp_path):
    db = tmp_path / ".graphtrail" / "graphtrail.db"
    db.parent.mkdir(parents=True)
    db.write_text("x")

    def which(cmd):
        return f"/x/{cmd}" if cmd == "graphtrail" else None

    monkeypatch.setattr(search_cmd.proc, "which", which)

    def fake_run(args, **kw):
        if args[:2] == ["/x/graphtrail", "doctor"]:
            return search_cmd.proc.Result(0, json.dumps({"ok": True}), "")
        raise AssertionError(args)

    monkeypatch.setattr(search_cmd.proc, "run", fake_run)

    payload = search_cmd.status_payload(tmp_path)

    assert payload["installed"] is True
    assert payload["health"] == "ok"
    assert payload["tools"]["graphtrail"]["db_present"] is True
    assert any("sync plan" in cmd for cmd in payload["next_commands"])


def test_search_status_unwired_without_db(monkeypatch, tmp_path):
    def which(cmd):
        return f"/x/{cmd}" if cmd == "graphtrail" else None

    monkeypatch.setattr(search_cmd.proc, "which", which)
    monkeypatch.setattr(
        search_cmd.proc,
        "run",
        lambda args, **kw: search_cmd.proc.Result(0, "{}", ""),
    )

    payload = search_cmd.status_payload(tmp_path)
    assert payload["health"] == "unwired"
    # unwired is advisory guidance, not a hard doctor failure
    assert search_cmd.doctor(target=tmp_path) == 0


def test_sync_plan_is_review_only(tmp_path):
    payload = search_cmd.sync_plan_payload(target=tmp_path)
    rendered = search_cmd.health.render_plan_md("search sync plan", payload)

    assert ["graphtrail", "sync", str(tmp_path.resolve())] in payload["commands"]
    assert "Brigade does not start code-search-api" in payload["boundaries"][1]
    assert "graphtrail sync" in rendered


def test_sync_plan_write_creates_files(tmp_path):
    rc = search_cmd.sync_plan(target=tmp_path, write=True, json_output=True)
    assert rc == 0
    plans = list((tmp_path / ".brigade" / "search" / "plans").glob("*/plan.json"))
    assert len(plans) == 1
    assert (plans[0].parent / "PLAN.md").exists()


def test_search_names_code_search_api_mcp_as_maintained_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(search_cmd.proc, "which", lambda cmd: None)
    payload = search_cmd.status_payload(tmp_path)
    compat = payload["tools"]["code-search-mcp"]
    assert compat["owner"] == "code-search-api/mcp"
    assert compat["compatibility_key"] == "code-search-mcp"
    assert "github.com/escoffier-labs/code-search-mcp" not in str(payload)
