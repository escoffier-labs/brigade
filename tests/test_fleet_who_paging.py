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
    status, page = messages.handle(conn, {"action": "who", "limit": 200, **args}, caller="reader-node")
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
    status, legacy = messages.handle(directory, {"action": "who"}, caller="reader-node")
    assert status == 200
    assert len(legacy["nodes"]) == len(legacy["principals"]) == 200
    assert legacy["truncated"] and legacy["next_cursor"] is None
    assert directory.execute("SELECT COUNT(*) FROM fleet_read_snapshots").fetchone()[0] == 0
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
        status, page = messages.handle(directory, {"action": "who", "limit": 200, **args}, caller="agent:limited")
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
    with pytest.raises(messages.MessageError, match="cursor") as caught:
        who(directory, cursor=cursor)
    assert caught.value.code is None


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
    raw_pages = []

    def fetch(body, **_kw):
        page = who(directory, **{k: v for k, v in body.items() if k != "action"})
        raw_pages.append(dict(page))
        return page

    monkeypatch.setattr(fleet_client_messages, "_online", fetch)
    args = {"limit": 500, "all": True}
    nodes, principals, sessions = [], [], []
    for _ in range(20):
        response = mcp_server.handle_message(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "fleet_who", "arguments": args}},
            **station.callbacks(),
        )
        assert not response["result"]["isError"]
        page = raw_pages[-1]
        assert len(json.dumps(page).encode()) < 8 * 1024 * 1024
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


@pytest.mark.parametrize("action", ["who", "claims"])
def test_cursor_keeps_snapshot_order_during_inserts_from_another_connection(directory, action):
    roster(directory, 4)
    for target in ("b", "d", "f", "h"):
        claim(directory, target)
    keys = ("nodes", "principals", "sessions") if action == "who" else ("claims",)
    status, original = messages.handle(directory, {"action": action}, caller="reader-node")
    assert status == 200
    status, page = messages.handle(directory, {"action": action, "limit": 1}, caller="reader-node")
    assert status == 200 and page["next_cursor"]
    seen = {key: list(page[key]) for key in keys}
    path = directory.execute("PRAGMA database_list").fetchone()[2]
    writer = fleet_hub.open_db(path)
    try:
        for index in range(10):
            # Insert before and after the current roster/target position.
            for prefix in ("aaa", "zzz"):
                fleet_hub.add_node(writer, f"{prefix}-{index}")
                messages.add_principal(writer, {"name": f"{prefix}-{index}"})
                claim(writer, f"{prefix}-{index}")
            status, page = messages.handle(
                directory, {"action": action, "limit": 1, "cursor": page["next_cursor"]}, caller="reader-node"
            )
            assert status == 200
            for key in keys:
                seen[key].extend(page[key])
                assert seen[key] == original[key][: len(seen[key])]
            if page["next_cursor"] is None:
                break
        else:
            pytest.fail("snapshot traversal did not terminate during concurrent inserts")
    finally:
        writer.close()
    assert seen == {key: original[key] for key in keys}


@pytest.mark.parametrize("action", ["who", "claims"])
def test_agent_pages_filter_before_cuts_and_recheck_remaining_grants(directory, monkeypatch, action):
    from brigade import fleet_hub_message_grants as grants

    roster(directory, 6)
    allowed_nodes = ["node-0001", "node-0003", "node-0005"]
    allowed_peers = ["agent:peer-0001", "agent:peer-0003", "agent:peer-0005"]
    messages.add_principal(directory, {"name": "limited", "may_send_to": allowed_nodes + allowed_peers})
    for index in range(6):
        node = f"node-{index:04d}"
        session(directory, node, f"session-{index}")
        status, _ = fleet_hub.handle_claim(
            directory,
            {"action": "acquire", "target": f"target-{index}", "node_id": node, "holder": "holder"},
            caller_node=node,
        )
        assert status == 200
    # Newer forbidden sessions must not consume the one-row session budget.
    monkeypatch.setattr(fleet_hub_sessions, "LIST_ACTIVE_LIMIT", 1)
    directory.execute("UPDATE interactive_sessions SET heartbeat_at=? WHERE node_id=?", ("9999", "node-0004"))
    directory.commit()
    status, first = messages.handle(directory, {"action": action, "limit": 1}, caller="agent:limited")
    assert status == 200 and first["next_cursor"]
    if action == "who":
        assert [row["node_id"] for row in first["nodes"]] == [allowed_nodes[0]]
        assert [row["principal_id"] for row in first["principals"]] == [allowed_peers[0]]
        assert [row["node_id"] for row in first["sessions"]] == [allowed_nodes[-1]]
    else:
        assert [row["target"] for row in first["claims"]] == ["target-1"]
    # Remove the middle permitted rows after the cursor is issued. The next
    # page must fill from the remaining snapshot, without exposing hidden rows.
    grants.replace_grants(directory, "agent:limited", [allowed_nodes[-1], allowed_peers[-1]])
    directory.commit()
    status, last = messages.handle(
        directory, {"action": action, "limit": 1, "cursor": first["next_cursor"]}, caller="agent:limited"
    )
    assert status == 200 and last["next_cursor"] is None
    if action == "who":
        assert [row["node_id"] for row in last["nodes"]] == [allowed_nodes[-1]]
        assert [row["principal_id"] for row in last["principals"]] == [allowed_peers[-1]]
        assert last["sessions"] == []
        assert first["total"] == {"nodes": 3, "principals": 3, "sessions": 1}
        assert last["total"] == {"nodes": 1, "principals": 1, "sessions": 1}
    else:
        assert [row["target"] for row in last["claims"]] == ["target-5"]
        assert first["total"] == 3 and last["total"] == 1


@pytest.mark.parametrize("caller", ["reader-node", "agent:limited"])
def test_claim_pages_preserve_node_metadata_and_redact_agent_metadata(directory, caller):
    from datetime import datetime, timedelta, timezone

    from tests.test_fleet_claim_display import _insert_job

    messages.add_principal(directory, {"name": "limited", "may_send_to": ["reader-node"]})
    for target in ("a", "z"):
        status, _ = fleet_hub.handle_claim(
            directory,
            {
                "action": "acquire",
                "node_id": "reader-node",
                "target": target,
                "holder": "holder",
                "conductor": "example-conductor",
                "job": "example-job",
                "session": "example-session",
            },
            caller_node="reader-node",
        )
        assert status == 200
    _insert_job(
        directory,
        "grokbot-" + "4" * 24,
        state="running",
        claimant_node="reader-node",
        lease_expires_at=(datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
    )
    rows = []
    raw = {"action": "claims", "limit": 1}
    for _ in range(4):
        status, page = messages.handle(directory, raw, caller=caller)
        assert status == 200
        rows.extend(page["claims"])
        if page["next_cursor"] is None:
            break
        raw["cursor"] = page["next_cursor"]
    else:
        pytest.fail("claim view traversal did not terminate")
    if caller.startswith("agent:"):
        assert [row["target"] for row in rows] == ["a", "z"]
        assert all(not {"job", "session", "owner_conductor"}.intersection(row) for row in rows)
    else:
        assert len(rows) == 3
        ordinary = [row for row in rows if row["target"] in {"a", "z"}]
        assert len(ordinary) == 2
        assert all(row["job"] == "example-job" and row["session"] == "example-session" for row in ordinary)
        assert all(row["owner_conductor"] == "example-conductor" for row in ordinary)
        assert any(row["harness"] == "grokbot" for row in rows)


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
        with pytest.raises(messages.MessageError, match="cursor") as caught:
            messages.handle(directory, raw, caller=caller)
        assert caught.value.code == "paging_cursor_expired"


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

    with pytest.raises(messages.MessageError, match="cursor") as caught:
        messages.handle(directory, {"action": "who", "cursor": first["next_cursor"]}, caller=caller)
    assert caught.value.code == "paging_cursor_expired"
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
    replacement = who(directory, limit=1)
    with pytest.raises(messages.MessageError, match="restart from the first page") as caught:
        who(directory, cursor=first["next_cursor"])
    assert caught.value.code == "paging_cursor_expired"
    first = replacement
    assert directory.execute("SELECT COUNT(*) FROM fleet_read_snapshots").fetchone()[0] == 1
    now = fleet_hub._now_epoch()
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: now + fleet_hub_paging.SNAPSHOT_TTL_SECONDS + 1)
    with pytest.raises(messages.MessageError, match="cursor") as caught:
        who(directory, cursor=first["next_cursor"])
    assert caught.value.code == "paging_cursor_expired"
    assert who(directory, limit=1)["next_cursor"]
    fleet_hub_paging.ensure_schema(directory)
    fleet_hub_paging.ensure_schema(directory)
    assert directory.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION


def test_full_caller_budget_evicts_the_abandoned_walk_not_the_active_one(directory, monkeypatch):
    from brigade import fleet_hub_paging

    roster(directory, 3)
    monkeypatch.setattr(fleet_hub_paging, "SNAPSHOT_MAX_PER_CALLER", 2)
    clock = [fleet_hub._now_epoch()]
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: clock[0])
    walk_a = who(directory, limit=1)
    clock[0] += 1
    walk_b = who(directory, limit=1)
    # Walk A is older by creation but was continued after B started; B was abandoned.
    clock[0] += 9
    walk_a = who(directory, cursor=walk_a["next_cursor"], limit=1)
    assert walk_a["next_cursor"]
    clock[0] += 1
    assert who(directory, limit=1)["next_cursor"]
    assert directory.execute("SELECT COUNT(*) FROM fleet_read_snapshots").fetchone()[0] == 2
    with pytest.raises(messages.MessageError, match="restart from the first page") as caught:
        who(directory, cursor=walk_b["next_cursor"])
    assert caught.value.code == "paging_cursor_expired"
    assert who(directory, cursor=walk_a["next_cursor"], limit=1)["nodes"]


@pytest.mark.parametrize("cap", ["SNAPSHOT_MAX_COUNT", "SNAPSHOT_MAX_BYTES", "SNAPSHOT_SINGLE_MAX_BYTES"])
def test_snapshot_global_count_and_byte_caps_fail_closed(directory, monkeypatch, cap):
    from brigade import fleet_hub_paging

    roster(directory, 2)
    monkeypatch.setattr(fleet_hub_paging, cap, 0)
    with pytest.raises(messages.MessageError, match="capacity"):
        who(directory, limit=1)
    assert directory.execute("SELECT COUNT(*) FROM fleet_read_snapshots").fetchone()[0] == 0


@pytest.mark.parametrize("previous_version", [40, pytest.param(42, id="main-v42"), 43, 44])
def test_previous_schema_migrates_once_and_preserves_snapshot_on_reopen(tmp_path, previous_version):
    path = tmp_path / "fleet.db"
    conn = fleet_hub.init_db(path)
    fleet_hub.add_node(conn, "reader-node")
    fleet_hub.add_node(conn, "other-node")
    questions, paging = _both_schemas(conn)
    conn.execute("DROP TABLE fleet_read_snapshots")
    if previous_version == 42:
        # Schema 42 predates the questions tables and trigger as well.
        _drop_schema(conn, questions)
        assert not _schema_objects(conn, "fleet_questions")
    conn.execute(f"PRAGMA user_version={previous_version}")
    conn.commit()
    conn.close()
    conn = fleet_hub.init_db(path)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
        assert _both_schemas(conn) == (questions, paging)
        first = who(conn, limit=1)
    finally:
        conn.close()
    conn = fleet_hub.init_db(path)
    try:
        second = who(conn, cursor=first["next_cursor"])
        assert [row["node_id"] for row in first["nodes"] + second["nodes"]] == ["other-node", "reader-node"]
    finally:
        conn.close()


@pytest.mark.parametrize("missing", ["table", "index"])
def test_current_schema_repairs_paging_objects_and_preserves_cursors(tmp_path, missing):
    path = tmp_path / "fleet.db"
    conn = fleet_hub.init_db(path)
    fleet_hub.add_node(conn, "reader-node")
    fleet_hub.add_node(conn, "other-node")
    nodes = conn.execute("SELECT * FROM nodes ORDER BY node_id").fetchall()
    first = who(conn, limit=1)
    snapshots = conn.execute("SELECT * FROM fleet_read_snapshots").fetchall()
    if missing == "table":
        conn.execute("DROP TABLE fleet_read_snapshots")
        snapshots = []
    else:
        conn.execute("DROP INDEX fleet_read_snapshots_expiry")
    conn.commit()
    conn.close()

    for startup in range(2):
        conn = fleet_hub.init_db(path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
            assert conn.execute("SELECT * FROM nodes ORDER BY node_id").fetchall() == nodes
            assert conn.execute("SELECT * FROM fleet_read_snapshots").fetchall() == snapshots
            assert "fleet_read_snapshots_expiry" in {
                row[1] for row in conn.execute("PRAGMA index_list(fleet_read_snapshots)")
            }
            if missing == "table" and startup == 0:
                first = who(conn, limit=1)
                snapshots = conn.execute("SELECT * FROM fleet_read_snapshots").fetchall()
            if startup == 1:
                second = who(conn, cursor=first["next_cursor"])
                assert [row["node_id"] for row in first["nodes"] + second["nodes"]] == ["other-node", "reader-node"]
                assert conn.execute("SELECT COUNT(*) FROM fleet_read_snapshots").fetchone()[0] == 0
        finally:
            conn.close()


def _schema_objects(conn, prefix):
    return {
        (kind, name)
        for kind, name in conn.execute("SELECT type, name FROM sqlite_master WHERE name LIKE ?", (f"{prefix}%",))
    }


def _both_schemas(conn):
    return _schema_objects(conn, "fleet_questions"), _schema_objects(conn, "fleet_read_")


def _drop_schema(conn, objects):
    # Triggers and indexes go with their tables; drop the rest by type to model a database that never had them.
    for kind, name in sorted(objects, key=lambda item: item[0] != "trigger"):
        conn.execute(f"DROP {kind.upper()} IF EXISTS {name}")


def _add_question(conn):
    conn.execute(
        "INSERT INTO fleet_questions(question_id, caller, generation, request_id, payload_hash, operator, question, "
        "message_id, created_at, deadline) VALUES ('q-kept', 'reader-node', 'g', 'r', 'h', 'agent:operator', "
        "'Keep me?', 1, '2026-10-22T00:00:00+00:00', '2026-10-22T00:01:00+00:00')"
    )


def test_fresh_database_reaches_schema_45_with_questions_and_paging(tmp_path):
    assert fleet_hub.SCHEMA_VERSION == 46
    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
        questions, paging = _both_schemas(conn)
        assert ("table", "fleet_questions") in questions and ("trigger", "fleet_questions_reply") in questions
        assert ("table", "fleet_read_snapshots") in paging and ("index", "fleet_read_snapshots_expiry") in paging
    finally:
        conn.close()


@pytest.mark.parametrize("stamped", [42, 43], ids=["main-before-questions-v42", "main-v43"])
def test_pre_paging_schemas_upgrade_to_45_and_keep_question_rows(tmp_path, stamped):
    path = tmp_path / "fleet.db"
    conn = fleet_hub.init_db(path)
    fleet_hub.add_node(conn, "reader-node")
    questions, paging = _both_schemas(conn)
    _drop_schema(conn, paging)
    if stamped == 42:
        # Schema 42 predates the questions tables and trigger as well.
        _drop_schema(conn, questions)
    else:
        _add_question(conn)
    conn.execute(f"PRAGMA user_version={stamped}")
    conn.commit()
    conn.close()

    for _startup in range(2):
        conn = fleet_hub.init_db(path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
            assert _both_schemas(conn) == (questions, paging)
            kept = conn.execute("SELECT question_id FROM fleet_questions").fetchall()
            assert kept == ([("q-kept",)] if stamped == 43 else [])
            assert who(conn, limit=1)["nodes"][0]["node_id"] == "reader-node"
        finally:
            conn.close()


@pytest.mark.parametrize("damage", ["table", "trigger"])
def test_schema_44_missing_question_objects_are_repaired_at_startup(tmp_path, damage):
    path = tmp_path / "fleet.db"
    conn = fleet_hub.init_db(path)
    fleet_hub.add_node(conn, "reader-node")
    fleet_hub.add_node(conn, "other-node")
    first = who(conn, limit=1)
    questions, paging = _both_schemas(conn)
    snapshots = conn.execute("SELECT * FROM fleet_read_snapshots").fetchall()
    if damage == "table":
        _drop_schema(conn, questions)
    else:
        conn.execute("DROP TRIGGER fleet_questions_reply")
    conn.execute("PRAGMA user_version=44")
    conn.commit()
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 44
    conn.close()

    for _startup in range(2):
        conn = fleet_hub.init_db(path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
            assert _both_schemas(conn) == (questions, paging)
            # The repair leaves the paging snapshot, and its cursor, untouched.
            assert conn.execute("SELECT * FROM fleet_read_snapshots").fetchall() == snapshots
        finally:
            conn.close()
    conn = fleet_hub.init_db(path)
    try:
        second = who(conn, cursor=first["next_cursor"])
        assert [row["node_id"] for row in first["nodes"] + second["nodes"]] == ["other-node", "reader-node"]
    finally:
        conn.close()
