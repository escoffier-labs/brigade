"""Supported external lifecycle reporting and durable CLI receipts."""

from __future__ import annotations

import io
import json
from hashlib import sha256

import pytest

from brigade import fleet_client
from brigade.cli import main


@pytest.fixture()
def reporter(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: "node-test")
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "http://hub.invalid", "token": "test"})
    monkeypatch.setattr(fleet_client, "spool_path", lambda _node: tmp_path / "spool" / "events.jsonl")
    sent = []
    monkeypatch.setattr(fleet_client, "_post_events", lambda _hub, _token, body, **_kw: sent.append(body))
    return sent, tmp_path / "spool" / "events.jsonl"


def _args(*extra):
    return [
        "fleet",
        "events",
        "report",
        "--run-id",
        "t3f:request-test",
        "--state",
        "run.started",
        "--ts",
        "2026-10-17T12:00:00Z",
        "--sequence",
        "1",
        "--json",
        *extra,
    ]


def test_report_digest_is_stable_over_all_reported_fields(reporter, capsys, monkeypatch):
    sent, _spool = reporter
    context = {"schema": "brigade.t3_execution_context.v1", "request_id": "request-test"}
    for _ in range(2):
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(context)))
        assert main(_args("--context-stdin", "--repo", "repo-test")) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["result"] == "delivered"
        assert set(payload) == {"result", "run_id", "sequence", "digest"}
    assert sent[0] == sent[1]
    event = sent[0].copy()
    digest = event.pop("digest")
    assert digest == sha256(json.dumps(event, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert event["node_id"] == "node-test"
    assert event["context"] == context


def test_outage_queues_then_flushes(reporter, capsys, monkeypatch):
    _sent, spool = reporter

    def outage(*_args, **_kw):
        raise OSError("hub unavailable")

    monkeypatch.setattr(fleet_client, "_post_events", outage)
    assert main(_args()) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"] == "queued"
    event = json.loads(spool.read_text())
    assert event["digest"] == payload["digest"]
    sent = []
    monkeypatch.setattr(fleet_client, "_post_events", lambda _hub, _token, body, **_kw: sent.extend(body))
    assert fleet_client.flush_spool("node-test") == 1
    assert sent == [event]
    assert not spool.exists()


def test_unpersistable_event_is_failure_not_queued(reporter, capsys, monkeypatch):
    _sent, spool = reporter
    spool.parent.write_text("blocked")
    monkeypatch.setattr(fleet_client, "_post_events", lambda *_a, **_kw: (_ for _ in ()).throw(OSError("offline")))
    assert main(_args()) == 1
    output = capsys.readouterr()
    assert not output.out
    assert "persist" in output.err


@pytest.mark.parametrize(
    "extra",
    [
        ["--sequence", "-1"],
        ["--ts", "yesterday"],
        ["--ts", "2026-10-17T12:00:00"],
        ["--state", "external.running"],
        ["--harness", "grokbot"],
        ["--harness", "codex"],
        ["--repo-identity", "https://user:pass@example.invalid/repo"],
    ],
)
def test_invalid_input_never_reports(reporter, capsys, extra):
    sent, spool = reporter
    assert main(_args(*extra)) == 2
    assert not sent and not spool.exists()
    assert not capsys.readouterr().out


def test_context_validation_precedes_spooling(reporter, capsys, monkeypatch):
    sent, spool = reporter
    monkeypatch.setattr("sys.stdin", io.StringIO('{"schema":"brigade.t3_execution_context.v1","prompt":"private"}'))
    assert main(_args("--context-stdin")) == 2
    assert not sent and not spool.exists()
    assert "private" not in capsys.readouterr().err


def test_context_file_and_signed_exit_status(reporter, tmp_path, capsys):
    sent, _spool = reporter
    context_file = tmp_path / "context.json"
    context_file.write_text('{"schema":"brigade.t3_execution_context.v1"}')
    assert main(_args("--context-file", str(context_file), "--exit-status", "-15", "--state", "external.failed")) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "delivered"
    assert sent[0]["exit_status"] == -15
    assert sent[0]["context"] == {"schema": "brigade.t3_execution_context.v1"}


def test_missing_hub_is_failure_not_queued(reporter, monkeypatch, capsys):
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "", "token": ""})
    assert main(_args()) == 1
    assert not capsys.readouterr().out


def test_hub_rejection_during_backlog_is_not_reported_as_delivery(reporter, monkeypatch, capsys):
    _sent, spool = reporter
    spool.parent.mkdir()
    spool.write_text(json.dumps({"run_id": "earlier", "node_id": "node-test"}) + "\n")

    def rejected(*_a, **_kw):
        raise fleet_client._PoisonResponse("rejected")

    monkeypatch.setattr(fleet_client, "_post_events", rejected)
    assert main(_args()) == 2
    assert json.loads(capsys.readouterr().out)["result"] == "rejected"
    assert len(spool.read_text().splitlines()) == 1


def test_delivery_receipt_survives_spool_cleanup_failure(reporter, capsys, monkeypatch):
    _sent, spool = reporter
    spool.parent.mkdir()
    spool.write_text(json.dumps({"run_id": "earlier", "node_id": "node-test"}) + "\n")

    def cleanup_failed(*_args, **_kw):
        raise OSError("cleanup failed")

    monkeypatch.setattr(fleet_client, "_write_spool_atomic", cleanup_failed)
    assert main(_args()) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "delivered"


def test_unenrolled_identity_is_failure_without_spooling(reporter, capsys, monkeypatch):
    sent, spool = reporter
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: "unknown")
    assert main(_args()) == 1
    assert not sent and not spool.exists()
    assert not capsys.readouterr().out
