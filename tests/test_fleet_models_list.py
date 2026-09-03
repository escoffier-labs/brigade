"""Tests for 'brigade fleet models list'."""

from __future__ import annotations

import json
from io import StringIO
from typing import Any

from brigade import cli, fleet_model_admission, fleet_model_roster


def _versioned_seat(
    seat: str,
    provider: str,
    model: str,
    *,
    reasoning: str = "high",
    enabled: bool = True,
    brigade_cli: str = "cursor-agent",
    t3_instance_id: str = "cursor",
) -> dict[str, Any]:
    return {
        "seat": seat,
        "provider": provider,
        "model": model,
        "reasoning": reasoning,
        "enabled": enabled,
        "bindings": {
            "brigade": {"cli": brigade_cli},
            "t3_fleet": {"instance_id": t3_instance_id, "service_tier": "standard"},
        },
    }


def _versioned_snapshot(
    *seats: dict[str, Any],
    revision: int = 7,
    digest: str = "sha256:" + ("ab" * 32),
) -> dict[str, Any]:
    return {
        "schema": fleet_model_roster.ROSTER_SCHEMA,
        "revision": revision,
        "roster_revision": revision,
        "document_sha256": digest,
        "expires_at": "2026-08-30T14:15:00Z",
        "seats": list(seats),
        "consumer_defaults": {"brigade-run": seats[0]["seat"] if seats else None},
        "retired_models": [],
    }


def _run_list(monkeypatch, snapshot: dict[str, Any], *extra_args: str) -> tuple[int, str, str]:
    decision = fleet_model_admission.ModelAdmissionDecision(
        ok=True,
        exit_code=0,
        reason="authoritative",
        payload=snapshot,
    )
    monkeypatch.setattr(fleet_model_admission, "fetch_versioned_roster", lambda **kwargs: decision)
    out = StringIO()
    err = StringIO()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)
    rc = cli.main(["fleet", "models", "list", *extra_args])
    return rc, out.getvalue(), err.getvalue()


def test_list_text_table_includes_revision_and_bindings(monkeypatch):
    snapshot = _versioned_snapshot(
        _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast"),
        _versioned_seat("cursor_composer", "cursor", "composer-2.5", enabled=False, brigade_cli="composer"),
    )
    rc, out, err = _run_list(monkeypatch, snapshot)
    assert rc == 0
    assert err == ""
    assert "roster revision 7 (consumer brigade-run)" in out
    assert "cursor_grok" in out
    assert "cursor_composer" in out
    assert "cursor-agent" in out
    assert "composer" in out
    assert "cursor-grok-4.6-high-fast" in out
    assert "composer-2.5" in out


def test_list_json_includes_revision_and_consumer(monkeypatch):
    snapshot = _versioned_snapshot(
        _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast"),
    )
    rc, out, err = _run_list(monkeypatch, snapshot, "--json")
    assert rc == 0
    assert err == ""
    parsed = json.loads(out)
    assert parsed["roster_revision"] == 7
    assert parsed["consumer"] == "brigade-run"
    assert len(parsed["seats"]) == 1
    seat = parsed["seats"][0]
    assert seat["seat"] == "cursor_grok"
    assert seat["provider"] == "cursor"
    assert seat["model"] == "cursor-grok-4.6-high-fast"
    assert seat["reasoning"] == "high"
    assert seat["enabled"] is True
    assert seat["binding"] == "cursor-agent"


def test_list_filters_by_seat(monkeypatch):
    snapshot = _versioned_snapshot(
        _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast"),
        _versioned_seat("cursor_composer", "cursor", "composer-2.5"),
    )
    rc, out, err = _run_list(monkeypatch, snapshot, "--seat", "cursor_composer")
    assert rc == 0
    assert "cursor_grok" not in out
    assert "cursor_composer" in out


def test_list_uses_t3_consumer_binding_when_requested(monkeypatch):
    snapshot = _versioned_snapshot(
        _versioned_seat("cursor_grok", "cursor", "cursor-grok-4.6-high-fast", t3_instance_id="t3-cursor"),
    )
    rc, out, err = _run_list(monkeypatch, snapshot, "--consumer", "t3-fleet")
    assert rc == 0
    assert "t3-cursor" in out


def test_list_rejects_unsupported_consumer(monkeypatch):
    snapshot = _versioned_snapshot()
    rc, out, err = _run_list(monkeypatch, snapshot, "--consumer", "unknown")
    assert rc == 2
    assert "unsupported consumer" in err


def test_list_reports_fetch_failure(monkeypatch):
    decision = fleet_model_admission.ModelAdmissionDecision(
        ok=False,
        exit_code=1,
        reason="auth-failed",
        payload={},
    )
    monkeypatch.setattr(fleet_model_admission, "fetch_versioned_roster", lambda **kwargs: decision)
    out = StringIO()
    err = StringIO()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)
    rc = cli.main(["fleet", "models", "list"])
    assert rc == 1
    assert "auth-failed" in err.getvalue()
