"""T3 wire contract v1.1 regression coverage."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone

import pytest

from brigade import fleet_client, fleet_command_deck, fleet_hub
from brigade.cli import main
from brigade.fleet_execution_context import ExecutionContextError, validate_context
from tests.test_fleet_events_cli import _args


# Defined here rather than shared through pytest_plugins, which does not
# expose a fixture when pytest collected the other module first.
@pytest.fixture()
def reporter(tmp_path, monkeypatch):
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: "node-test")
    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "http://hub.invalid", "token": "test"})
    monkeypatch.setattr(fleet_client, "spool_path", lambda _node: tmp_path / "spool" / "events.jsonl")
    sent = []
    monkeypatch.setattr(fleet_client, "_post_events", lambda _hub, _token, body, **_kw: sent.append(body))
    return sent, tmp_path / "spool" / "events.jsonl"


@pytest.mark.parametrize("version", [31, 32, fleet_hub.SCHEMA_VERSION])
def test_missing_additive_context_is_repaired_and_second_open_is_read_only(tmp_path, version):
    db = tmp_path / "hub.db"
    conn = fleet_hub.init_db(db)
    assert "context" in {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    conn.execute("ALTER TABLE events DROP COLUMN context")
    conn.execute(f"PRAGMA user_version={version}")
    conn.commit()
    conn.close()
    conn = fleet_hub.init_db(db)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
    assert "context" in {row[1] for row in conn.execute("PRAGMA table_info(events)")}
    # A fully repaired database must retain the no-write-lock startup path.
    conn.execute("BEGIN IMMEDIATE")
    reopened = fleet_hub.init_db(db)
    reopened.close()
    conn.rollback()
    conn.close()


@pytest.mark.parametrize("state", ["run.started", "run.paused"])
@pytest.mark.parametrize("age", [301, 600, 899, 900, 901])
def test_observation_freshness_keeps_three_heartbeat_windows(tmp_path, state, age):
    now = datetime(2026, 10, 17, 12, tzinfo=timezone.utc)
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    try:
        fleet_hub.store_events(
            conn,
            {
                "node_id": "node-test",
                "run_id": "t3f:test",
                "sequence": 1,
                "digest": "test",
                "harness": "t3-fleet",
                "state": state,
                "ts": (now - timedelta(seconds=age)).isoformat(),
            },
        )
        conn.execute("UPDATE events SET received_at = ?", (now.isoformat(),))
        expected = "run.stale" if age > 900 else state
        assert fleet_hub.latest_status(conn, include_all=True, now=now)[0]["state"] == expected
        run = fleet_command_deck.fetch_live_runs(conn, now=now, stale_after_seconds=1800)[0]
        assert run.state == expected
        if state == "run.paused" and age <= 900:
            assert run.bucket == "awaiting approval"
    finally:
        conn.close()


@pytest.mark.parametrize("backlog", [False, True])
def test_permanent_rejection_never_queues_own_event(reporter, monkeypatch, capsys, backlog):
    _sent, spool = reporter
    old = json.dumps({"run_id": "poisoned"}) + "\n"
    if backlog:
        spool.parent.mkdir()
        spool.write_text(old)

    def reject(*_a, **_kw):
        raise fleet_client._PoisonResponse("rejected")

    monkeypatch.setattr(fleet_client, "_post_events", reject)
    assert main(_args()) == 2
    assert json.loads(capsys.readouterr().out)["result"] == "rejected"
    assert spool.read_text() == old if backlog else not spool.exists()


def test_poisoned_backlog_does_not_block_direct_delivery(reporter, monkeypatch, capsys):
    _sent, spool = reporter
    spool.parent.mkdir()
    old = json.dumps({"run_id": "poisoned"}) + "\n"
    spool.write_text(old)

    def post(_hub, _token, body, **_kw):
        assert isinstance(body, dict)
        assert body["run_id"] == "t3f:request-test"

    monkeypatch.setattr(fleet_client, "_post_events", post)
    assert main(_args()) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "delivered"
    assert spool.read_text() == old


def test_stdin_context_is_utf8_even_with_locale_text_wrapper(reporter, monkeypatch, capsys):
    sent, _spool = reporter
    context = {"schema": "brigade.t3_execution_context.v1", "initiator": {"label": "caf\u00e9"}}
    raw = json.dumps(context, ensure_ascii=False).encode("utf-8")
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(raw), encoding="cp1252"))
    assert main(_args("--context-stdin")) == 0
    assert sent[0]["context"] == context
    capsys.readouterr()


@pytest.mark.parametrize("fraction", ["1", "12", "123", "1234", "12345", "123456", "1234567"])
def test_fractional_utc_timestamp_is_python310_compatible(reporter, capsys, monkeypatch, fraction):
    from brigade import fleet_event_freshness

    class Python310Datetime(datetime):
        @classmethod
        def fromisoformat(cls, value):
            # Python 3.10 requires an offset and exactly 3 or 6 fractional digits.
            assert not value.endswith("Z")
            digits = value.split(".")[1].split("+")[0]
            assert len(digits) in {3, 6}
            return datetime.fromisoformat(value)

    monkeypatch.setattr(fleet_event_freshness, "datetime", Python310Datetime)
    ts = f"2026-10-17T12:00:00.{fraction}Z"
    assert main(_args("--ts", ts)) == 0
    assert json.loads(capsys.readouterr().out)["result"] == "delivered"
    now = datetime(2026, 10, 17, 12, 1, tzinfo=timezone.utc)
    assert fleet_event_freshness.observation_state("run.started", "t3-fleet", ts, now=now) == "run.started"


@pytest.mark.parametrize(
    "value,accepted",
    [
        ("", True),
        ("agent:test", True),
        ("caf\u00e9", True),
        ("x" * 128, True),
        ("x" * 129, False),
        ("a/b", False),
        ("a\\b", False),
        ("a%2Fb", False),
        ("op:item", False),
        ("Basic example", False),
        ("Bearer example", False),
        ("password=example", False),
        ("a\nb", False),
        ("a\x00b", False),
        ("a\u200bb", False),
    ],
)
@pytest.mark.parametrize(
    "field",
    [
        "request_id",
        "thread_id",
        "turn_ref",
        "controller_id",
        "origin_node",
        "target_host",
        "work_id",
        "initiator.label",
        "policy.session_id",
        "policy.decision_id",
        "policy.reservation_id",
        "policy.delegation_id",
    ],
)
def test_context_text_contract_table(field, value, accepted):
    context = {"schema": "brigade.t3_execution_context.v1"}
    if "." in field:
        parent, key = field.split(".")
        context[parent] = {key: value}
    else:
        context[field] = value
    if accepted:
        assert validate_context(context) == context
    else:
        with pytest.raises(ExecutionContextError):
            validate_context(context)


@pytest.mark.parametrize(
    "value,accepted",
    [
        ("https://github.com/example/repo/issues/1", True),
        ("https://github.com/example/repo/pull/12", True),
        ("https://github.com/example/repo/issues/0", False),
        ("https://github.com/example/repo/issues/1/", False),
        ("https://github.com/example/repo/issues/1?q=x", False),
        ("https://github.com/example/repo/issues/%31", False),
        ("http://github.com/example/repo/issues/1", False),
        ("https://example.invalid/example/repo/issues/1", False),
    ],
)
def test_context_issue_url_contract_table(value, accepted):
    context = {"schema": "brigade.t3_execution_context.v1", "issue_url": value}
    if accepted:
        assert validate_context(context) == context
    else:
        with pytest.raises(ExecutionContextError):
            validate_context(context)
