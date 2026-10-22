"""Directory paging and wire-size contracts for credential-bound fleet_who."""

from __future__ import annotations

import base64
import json

import pytest

from brigade import fleet_hub, fleet_hub_messages as messages, fleet_hub_sessions, mcp_server
from brigade.fleet_mcp import FleetMcp


@pytest.fixture
def directory(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    fleet_hub.add_node(conn, "reader-node")
    yield conn
    conn.close()


def roster(conn, count):
    # Reverse insertion order makes accidental database-order paging observable.
    for index in reversed(range(count)):
        fleet_hub.add_node(conn, f"node-{index:04d}")
        messages.add_principal(conn, {"name": f"peer-{index:04d}"})


def who(conn, **args):
    status, page = messages.handle(conn, {"action": "who", **args}, caller="reader-node")
    assert status == 200
    return page


def session(conn, node, sid, **extra):
    status, _ = fleet_hub_sessions.handle_session(
        conn,
        {
            "action": "upsert",
            "harness": "codex",
            "session_id": sid,
            "repo_identity": "local:" + "a" * 64,
            "identity_scope": "node",
            "repo_label": "example",
            "checkout_path": "/example/repo",
            "branch": "main",
            "dirty_paths": [],
            "dirty_truncated": False,
            **extra,
        },
        caller_node=node,
    )
    assert status == 200


def test_both_rosters_traverse_default_and_max_pages(directory):
    roster(directory, 507)
    first = who(directory)
    assert len(first["nodes"]) == len(first["principals"]) == 200
    assert first["truncated"] and isinstance(first["next_cursor"], str)
    for limit in (17, 500):
        nodes, principals = [], []
        args = {"limit": limit}
        for _ in range(40):
            page = who(directory, **args)
            assert len(page["nodes"]) <= limit and len(page["principals"]) <= limit
            nodes.extend(row["node_id"] for row in page["nodes"])
            principals.extend(row["principal_id"] for row in page["principals"])
            if not page["truncated"]:
                assert page["next_cursor"] is None
                break
            args["cursor"] = page["next_cursor"]
        else:
            pytest.fail("directory cursor did not terminate")
        assert nodes == [f"node-{index:04d}" for index in range(507)] + ["reader-node"]
        assert principals == [f"agent:peer-{index:04d}" for index in range(507)]


def test_grants_and_text_filter_precede_cuts_and_do_not_hide_sessions(directory):
    roster(directory, 5)
    messages.add_principal(directory, {"name": "limited", "may_send_to": ["node-0003", "node-0004", "agent:peer-0004"]})
    session(directory, "node-0004", "later-session", repo_label="unique-match")
    session(directory, "node-0000", "hidden-session", repo_label="unique-match")
    for args in ({"limit": 1}, {"limit": 1, "filter": "UNIQUE-MATCH"}):
        status, page = messages.handle(directory, {"action": "who", **args}, caller="agent:limited")
        assert status == 200
        assert [row["session_id"] for row in page["sessions"]] == ["later-session"]
        assert [row["node_id"] for row in page["nodes"]] == ([] if "filter" in args else ["node-0003"])
    page = who(directory, limit=1, filter="PEER-0004")
    assert [row["principal_id"] for row in page["principals"]] == ["agent:peer-0004"]
    assert page["next_cursor"] is None
    fleet_hub.revoke_node(directory, "node-0004")
    assert not who(directory, all=True, filter="node-0004")["nodes"]
    fleet_hub_sessions.handle_session(
        directory,
        {"action": "end", "harness": "codex", "session_id": "hidden-session", "repo_identity": "local:" + "a" * 64},
        caller_node="node-0000",
    )
    assert not who(directory, filter="hidden-session")["sessions"]
    assert len(who(directory, all=True, filter="hidden-session")["sessions"]) == 1


@pytest.mark.parametrize("limit", [True, False, 0, -1, 501, "2", 2.5, None])
def test_invalid_limits_are_rejected(directory, limit):
    with pytest.raises(messages.MessageError, match="limit"):
        who(directory, limit=limit)


@pytest.mark.parametrize(
    "cursor",
    [True, 1, [], {}, "", "garbage", "x" * 2049]
    + [
        base64.urlsafe_b64encode(json.dumps(value).encode()).decode()
        for value in ([1, -1, 0, 0], [1, True, 0, 0], [9, 0, 0, 0], {})
    ],
)
def test_malformed_cursors_are_rejected(directory, cursor):
    with pytest.raises(messages.MessageError, match="cursor"):
        who(directory, cursor=cursor)


def test_mcp_cursor_round_trip_and_credential_arguments(directory, monkeypatch):
    from brigade import fleet_client_messages

    roster(directory, 3)
    station = FleetMcp()
    monkeypatch.setattr(station, "_authority", lambda: object())
    monkeypatch.setattr(
        fleet_client_messages,
        "_online",
        lambda body, **_kw: who(directory, **{k: v for k, v in body.items() if k != "action"}),
    )
    first, error = station.call("fleet_who", {"limit": 1})
    assert not error and isinstance(first["next_cursor"], str)
    second, error = station.call("fleet_who", {"limit": 1, "cursor": first["next_cursor"]})
    assert not error and second["nodes"][0]["node_id"] == "node-0001"
    assert isinstance(second["principals"][0]["label"], dict)
    for field in ("caller", "node_id", "principal_id", "token_file"):
        _, error = station.call("fleet_who", {field: "other"})
        assert error


def test_full_mcp_wire_response_is_bounded_with_maximum_sessions(directory, monkeypatch):
    from brigade import fleet_client_messages

    roster(directory, 501)
    # Legal maximum-length Unicode text expands at both JSON serialization layers.
    for index in range(1000):
        session(directory, "reader-node", f"session-{index:04d}", repo_label="😀" * 256, branch="😀" * 256)
    station = FleetMcp()
    monkeypatch.setattr(station, "_authority", lambda: object())
    monkeypatch.setattr(
        fleet_client_messages,
        "_online",
        lambda body, **_kw: who(directory, **{k: v for k, v in body.items() if k != "action"}),
    )
    args = {"limit": 500, "all": True}
    nodes, principals, sessions = [], [], []
    for _ in range(20):
        page = who(directory, **args)
        assert len(json.dumps(page).encode()) < 8 * 1024 * 1024
        response = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "fleet_who", "arguments": args}},
            **station.callbacks(),
        )
        assert not response["result"]["isError"]
        assert len(json.dumps(response).encode()) < 8 * 1024 * 1024
        nodes.extend(row["node_id"] for row in page["nodes"])
        principals.extend(row["principal_id"] for row in page["principals"])
        sessions.extend(row["session_id"] for row in page["sessions"])
        if page["next_cursor"] is None:
            break
        args["cursor"] = page["next_cursor"]
    else:
        pytest.fail("byte-bounded paging did not terminate")
    assert len(nodes) == len(set(nodes)) == 502
    assert len(principals) == len(set(principals)) == 501
    assert len(sessions) == len(set(sessions)) == 1000


def claim(conn, target, action="acquire", holder="holder-a"):
    status, payload = fleet_hub.handle_claim(
        conn,
        {"action": action, "node_id": "reader-node", "target": target, "holder": holder},
        caller_node="reader-node",
    )
    assert status == 200
    return payload


def test_claim_snapshot_survives_renew_release_and_reacquire(directory):
    for target in ("a", "b", "c", "d", "e"):
        claim(directory, target)
    status, first = messages.handle(directory, {"action": "claims", "limit": 2}, caller="reader-node")
    assert status == 200 and first["truncated"]
    original = fleet_hub.list_claims(directory)
    claim(directory, "c", "renew")
    claim(directory, "d", "release")
    claim(directory, "b", "release")
    claim(directory, "b", holder="holder-b")
    claim(directory, "aa", holder="holder-b")
    seen = first["claims"]
    cursor = first["next_cursor"]
    for _ in range(5):
        status, page = messages.handle(
            directory, {"action": "claims", "limit": 2, "cursor": cursor}, caller="reader-node"
        )
        assert status == 200
        seen.extend(page["claims"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == sorted(original, key=lambda row: row["target"])


def test_snapshot_rechecks_grants_revocation_and_query_binding(directory):
    from brigade import fleet_hub_message_grants as grants

    roster(directory, 5)
    messages.add_principal(directory, {"name": "limited", "may_send_to": ["*"]})
    status, first = messages.handle(directory, {"action": "who", "limit": 1}, caller="agent:limited")
    assert status == 200
    cursor = first["next_cursor"]
    grants.replace_grants(directory, "agent:limited", ["node-0004", "agent:peer-0004"])
    directory.commit()
    fleet_hub.revoke_node(directory, "node-0004")
    generation = messages.get_principal(directory, {"action": "get", "name": "peer-0004"})["enrollment_id"]
    messages.revoke_principal(
        directory, {"action": "revoke", "name": "peer-0004", "expected_enrollment_id": generation}
    )
    status, second = messages.handle(directory, {"action": "who", "cursor": cursor}, caller="agent:limited")
    assert status == 200 and second["nodes"] == second["principals"] == []
    for raw, caller in (
        ({"action": "who", "cursor": cursor}, "reader-node"),
        ({"action": "who", "cursor": cursor, "filter": "other"}, "agent:limited"),
        ({"action": "claims", "cursor": cursor}, "agent:limited"),
    ):
        with pytest.raises(messages.MessageError, match="cursor"):
            messages.handle(directory, raw, caller=caller)


@pytest.mark.parametrize("kind", ["node", "agent"])
def test_snapshot_rejects_cursor_after_caller_reenrollment(directory, kind):
    roster(directory, 3)
    name = "paging-reader"
    if kind == "node":
        original, _ = fleet_hub.add_node(directory, name)
        caller = original["node_id"]
    else:
        original = messages.add_principal(directory, {"name": name, "may_send_to": ["*"]})
        caller = original["principal_id"]
    status, first = messages.handle(directory, {"action": "who", "limit": 1}, caller=caller)
    assert status == 200 and first["next_cursor"]

    if kind == "node":
        fleet_hub.revoke_node(directory, name)
        replacement, token = fleet_hub.add_node(directory, name)
        assert replacement["node_id"] == caller
        assert fleet_hub.lookup_node_token(directory, token) == (caller, False)
    else:
        messages.revoke_principal(
            directory, {"action": "revoke", "name": name, "expected_enrollment_id": original["enrollment_id"]}
        )
        replacement = messages.add_principal(
            directory, {"name": name, "expected_enrollment_id": original["enrollment_id"], "may_send_to": ["*"]}
        )
        assert replacement["principal_id"] == caller
        assert messages.lookup_principal_token(directory, replacement["token"]) == caller

    with pytest.raises(messages.MessageError, match="cursor"):
        messages.handle(directory, {"action": "who", "cursor": first["next_cursor"]}, caller=caller)
    status, fresh = messages.handle(directory, {"action": "who", "limit": 1}, caller=caller)
    assert status == 200 and fresh["next_cursor"]
    status, rest = messages.handle(directory, {"action": "who", "cursor": fresh["next_cursor"]}, caller=caller)
    assert status == 200 and rest["next_cursor"] is None
    assert [row["node_id"] for row in fresh["nodes"] + rest["nodes"]] == [
        "node-0000",
        "node-0001",
        "node-0002",
        *([name] if kind == "node" else []),
        "reader-node",
    ]


def test_snapshot_capacity_expiry_and_schema_migration(directory, monkeypatch):
    from brigade import fleet_hub_paging

    roster(directory, 2)
    monkeypatch.setattr(fleet_hub_paging, "SNAPSHOT_MAX_PER_CALLER", 1)
    first = who(directory, limit=1)
    with pytest.raises(messages.MessageError, match="capacity"):
        who(directory, limit=1)
    now = fleet_hub._now_epoch()
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: now + fleet_hub_paging.SNAPSHOT_TTL_SECONDS + 1)
    with pytest.raises(messages.MessageError, match="cursor"):
        who(directory, cursor=first["next_cursor"])
    assert who(directory, limit=1)["next_cursor"]
    fleet_hub_paging.ensure_schema(directory)
    fleet_hub_paging.ensure_schema(directory)
    assert directory.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION


@pytest.mark.parametrize("cap", ["SNAPSHOT_MAX_COUNT", "SNAPSHOT_MAX_BYTES", "SNAPSHOT_SINGLE_MAX_BYTES"])
def test_snapshot_global_count_and_byte_caps_fail_closed(directory, monkeypatch, cap):
    from brigade import fleet_hub_paging

    roster(directory, 2)
    monkeypatch.setattr(fleet_hub_paging, cap, 0)
    with pytest.raises(messages.MessageError, match="capacity"):
        who(directory, limit=1)
    assert directory.execute("SELECT COUNT(*) FROM fleet_read_snapshots").fetchone()[0] == 0


def test_schema_37_migrates_once_and_preserves_snapshot_on_reopen(tmp_path):
    path = tmp_path / "fleet.db"
    conn = fleet_hub.init_db(path)
    fleet_hub.add_node(conn, "reader-node")
    fleet_hub.add_node(conn, "other-node")
    conn.execute("DROP TABLE fleet_read_snapshots")
    conn.execute("PRAGMA user_version=37")
    conn.commit()
    conn.close()
    conn = fleet_hub.init_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
        first = who(conn, limit=1)
    finally:
        conn.close()
    conn = fleet_hub.init_db(path)
    try:
        second = who(conn, cursor=first["next_cursor"])
        assert [row["node_id"] for row in first["nodes"] + second["nodes"]] == ["other-node", "reader-node"]
    finally:
        conn.close()
