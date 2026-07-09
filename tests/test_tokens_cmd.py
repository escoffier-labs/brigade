"""Tests for the tokens station CLI (Token Glace + usage-tracker)."""

from __future__ import annotations

import json

from brigade import tokens_cmd


def test_tokens_status_reports_uninstalled(monkeypatch, tmp_path):
    monkeypatch.setattr(tokens_cmd.proc, "which", lambda cmd: None)

    payload = tokens_cmd.status_payload(tmp_path)

    assert payload["installed"] is False
    assert payload["health"] == "missing"
    assert "brigade add tokens" in payload["summary"]
    assert "brigade tokens wire plan" in payload["next_commands"]


def test_tokens_status_ok_with_token_glace(monkeypatch, tmp_path):
    def which(cmd):
        return f"/x/{cmd}" if cmd == "token-glace" else None

    monkeypatch.setattr(tokens_cmd.proc, "which", which)

    def fake_run(args, **kw):
        if args[:3] == ["/x/token-glace", "doctor", "hooks"]:
            return tokens_cmd.proc.Result(0, json.dumps({"status": "ok", "integrations": {}}), "")
        raise AssertionError(args)

    monkeypatch.setattr(tokens_cmd.proc, "run", fake_run)

    payload = tokens_cmd.status_payload(tmp_path)

    assert payload["installed"] is True
    assert payload["health"] == "ok"
    assert "hook status: ok" in payload["summary"]


def test_tokens_doctor_exits_nonzero_on_broken(monkeypatch, tmp_path):
    def which(cmd):
        return f"/x/{cmd}" if cmd == "token-glace" else None

    monkeypatch.setattr(tokens_cmd.proc, "which", which)
    monkeypatch.setattr(
        tokens_cmd.proc,
        "run",
        lambda args, **kw: tokens_cmd.proc.Result(0, json.dumps({"status": "broken"}), ""),
    )

    assert tokens_cmd.doctor(target=tmp_path) == 1


def test_wire_plan_is_review_only(tmp_path):
    payload = tokens_cmd.wire_plan_payload(target=tmp_path)
    rendered = tokens_cmd.health.render_plan_md("tokens wire plan", payload)

    assert ["token-glace", "install", "claude-code"] in payload["commands"]
    assert "Token Glace" in payload["boundaries"][0]
    assert "token-glace install" in rendered


def test_wire_plan_write_creates_files(tmp_path):
    rc = tokens_cmd.wire_plan(target=tmp_path, write=True, json_output=True)
    assert rc == 0
    plans = list((tmp_path / ".brigade" / "tokens" / "plans").glob("*/plan.json"))
    assert len(plans) == 1
