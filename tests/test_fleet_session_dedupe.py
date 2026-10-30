"""Exact provider links and session-root attribution keep presence counts honest."""

import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from brigade import fleet_client, fleet_command_deck as deck, fleet_hub, fleet_hub_sessions
from brigade.claude_hooks import runtime
from brigade.config import Config, write_config
from brigade.selection import Selection
from brigade.work_cmd.session import briefing

NODE = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
NOW = 1_700_000_000.0


def body(harness, session_id, **updates):
    return {
        "action": "upsert",
        "harness": harness,
        "session_id": session_id,
        "repo_identity": "github.com/example/project",
        "identity_scope": "fleet",
        "repo_label": "project",
        "checkout_path": "/repos/project",
        "branch": "main",
        "dirty_paths": [],
        "dirty_truncated": False,
        **updates,
    }


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_exact_provider_link_dedupes_before_filter_and_limit(monkeypatch, harness):
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW)
    conn = sqlite3.connect(":memory:")
    fleet_hub_sessions.init_schema(conn)
    for request in (
        body(harness, "native-session", dirty_paths=["src/example.py"]),
        body("t3", "app-thread", provider_link={"harness": harness, "session_id": "native-session"}),
        body(harness, "unrelated-session"),
        body("t3", "unlinked-thread"),
    ):
        fleet_hub_sessions.handle_session(conn, request, caller_node=NODE)
    assert fleet_hub_sessions.count_live_by_node(conn, now_epoch=NOW) == {NODE: 3}
    rows = fleet_hub_sessions.list_sessions(conn, now_epoch=NOW)
    assert {r["session_id"] for r in rows} == {"native-session", "unrelated-session", "unlinked-thread"}
    assert next(r for r in rows if r["session_id"] == "native-session")["dirty_paths"] == ["src/example.py"]
    assert not fleet_hub_sessions.list_sessions(conn, now_epoch=NOW, text_filter="app-thread", limit=1)
    now = datetime.fromtimestamp(NOW, timezone.utc)
    listed = deck.fetch_interactive_sessions(conn, now=now)
    view = deck.build_view(
        deck.DeckConfig(stations=(deck.StationConfig(NODE, "alpha-host", 10),)),
        live_runs=(),
        claims=(),
        enrolled_labels={NODE: "alpha-host"},
        last_heard={},
        outcomes=(),
        failed_outcomes=(),
        observers=(),
        now=now,
        interactive_sessions=listed,
        session_counts=fleet_hub_sessions.count_live_by_node(conn, now_epoch=NOW),
    )
    assert view.stations[0].sessions == len(view.interactive_sessions) == 3
    assert conn.execute("SELECT COUNT(*) FROM interactive_sessions").fetchone()[0] == 4
    conn.close()


@pytest.mark.parametrize("difference", ["node", "checkout", "harness", "expired", "ended"])
def test_link_never_hides_a_distinct_or_only_live_observation(monkeypatch, difference):
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW)
    conn = sqlite3.connect(":memory:")
    fleet_hub_sessions.init_schema(conn)
    linked = body("t3", "app-thread", provider_link={"harness": "claude", "session_id": "native-session"})
    fleet_hub_sessions.handle_session(conn, linked, caller_node=NODE)
    provider = body("codex" if difference == "harness" else "claude", "native-session")
    if difference == "checkout":
        provider["checkout_path"] = "/repos/other"
    fleet_hub_sessions.handle_session(conn, provider, caller_node=OTHER if difference == "node" else NODE)
    if difference == "expired":
        conn.execute("UPDATE interactive_sessions SET expires_at=? WHERE harness='claude'", (NOW,))
    if difference == "ended":
        conn.execute("UPDATE interactive_sessions SET state='ended' WHERE harness='claude'")
    rows = fleet_hub_sessions.list_sessions(conn, now_epoch=NOW)
    assert any(r["session_id"] == "app-thread" for r in rows)
    assert sum(fleet_hub_sessions.count_live_by_node(conn, now_epoch=NOW).values()) == len(rows)
    conn.close()


def test_provider_link_roundtrips_through_client():
    from brigade.fleet_session_presence import SessionSnapshot

    snapshot = SessionSnapshot("t3", "app", "local:example", "node", "project", "/repos/project", None, (), True)
    snapshot = replace(snapshot, provider_link={"harness": "codex", "session_id": "native-session"})
    assert fleet_client._session_request_body(snapshot, action="upsert")["provider_link"] == snapshot.provider_link


def test_provider_link_survives_missing_lookup_and_accepts_replacement(monkeypatch):
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW)
    conn = sqlite3.connect(":memory:")
    fleet_hub_sessions.init_schema(conn)
    first = {"harness": "codex", "session_id": "first-session"}
    second = {"harness": "claude", "session_id": "second-session"}
    for updates, expected in (
        ({"provider_link": first}, first),
        ({}, first),
        ({"provider_link": None}, None),
        ({"provider_link": second}, second),
    ):
        _, result = fleet_hub_sessions.handle_session(conn, body("t3", "app-thread", **updates), caller_node=NODE)
        assert result["session"].get("provider_link") == expected
    conn.close()


@pytest.mark.parametrize(
    "tool,tool_input",
    [
        ("Read", {"file_path": "OTHER/notes.md"}),
        ("Bash", {"command": "cat OTHER/notes.md"}),
        ("Write", {"file_path": "OTHER/notes.md"}),
    ],
)
def test_claude_presence_uses_cwd_even_when_tool_targets_other_checkout(tmp_path, monkeypatch, tool, tool_input):
    root, other = tmp_path / "project", tmp_path / "other"
    for target in (root, other):
        write_config(target, Config(version=1, selection=Selection(depth="repo", harnesses=("claude",))))
    monkeypatch.setattr(runtime, "_run_brief", lambda *a: "brief")
    monkeypatch.setattr(runtime, "_run_recall", lambda *a: "")
    calls = []
    monkeypatch.setattr(runtime.presence, "emit_presence", lambda e, t, s: calls.append(t))
    payload = {
        "cwd": str(root),
        "session_id": "native-session",
        "tool_name": tool,
        "tool_input": {k: v.replace("OTHER", str(other)) for k, v in tool_input.items()},
    }
    runtime.handle_payload("PreToolUse", payload)
    assert calls and set(calls) == {root}


def test_codex_brief_presence_uses_session_metadata_cwd(tmp_path, monkeypatch):
    root, other = tmp_path / "project", tmp_path / "other"
    root.mkdir()
    other.mkdir()
    metadata = tmp_path / "codex" / "sessions" / "2026" / "01" / "01"
    metadata.mkdir(parents=True)
    (metadata / "rollout-example-native-session.jsonl").write_text(
        json.dumps({"type": "session_meta", "payload": {"id": "native-session", "cwd": str(root)}}) + "\n"
    )
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CODEX_THREAD_ID", "native-session")
    monkeypatch.setattr("brigade.fleet_session_presence._hub_url_configured", lambda: True)
    sent = []
    monkeypatch.setattr(
        fleet_client, "publish_session", lambda s: sent.append(s) or fleet_client.SessionWriteResult(ok=True)
    )
    monkeypatch.setattr(fleet_client, "fetch_sessions", lambda: [])
    briefing._interactive_presence_for_brief(other)
    assert [s.checkout_path for s in sent] == [str(root)]


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_new_provider_checkout_ends_secondary_rows_without_deleting_history(monkeypatch, harness):
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW)
    conn = sqlite3.connect(":memory:")
    fleet_hub_sessions.init_schema(conn)
    secondary = body(harness, "native-session", repo_identity="github.com/example/other", checkout_path="/repos/other")
    fleet_hub_sessions.handle_session(conn, secondary, caller_node=NODE)
    fleet_hub_sessions.handle_session(conn, body(harness, "native-session"), caller_node=NODE)
    assert [r["checkout_path"] for r in fleet_hub_sessions.list_sessions(conn)] == ["/repos/project"]
    assert conn.execute(
        "SELECT state FROM interactive_sessions WHERE repo_label='project' AND checkout_path='/repos/other'"
    ).fetchone() == ("ended",)
    assert len(fleet_hub_sessions.list_sessions(conn, include_all=True)) == 2
    conn.close()


def test_current_version_database_adds_provider_link_without_losing_rows(tmp_path):
    path = tmp_path / "hub.db"
    conn = fleet_hub.init_db(path)
    fleet_hub_sessions.handle_session(conn, body("claude", "native-session"), caller_node=NODE)
    conn.execute("ALTER TABLE interactive_sessions DROP COLUMN provider_link_json")
    conn.commit()
    conn.close()
    conn = fleet_hub.init_db(path)
    assert [r["session_id"] for r in fleet_hub_sessions.list_sessions(conn)] == ["native-session"]
    assert "provider_link_json" in {r[1] for r in conn.execute("PRAGMA table_info(interactive_sessions)")}
    conn.close()


@pytest.mark.parametrize(
    "link",
    [
        [],
        {"harness": [], "session_id": "id"},
        {"harness": "codex", "session_id": "bad id"},
        {"harness": "codex", "session_id": "id", "extra": "value"},
    ],
)
def test_malformed_provider_link_is_a_validation_error(link):
    conn = sqlite3.connect(":memory:")
    fleet_hub_sessions.init_schema(conn)
    with pytest.raises(fleet_hub.FleetHubError):
        fleet_hub_sessions.handle_session(conn, body("t3", "thread", provider_link=link), caller_node=NODE)
    conn.close()
