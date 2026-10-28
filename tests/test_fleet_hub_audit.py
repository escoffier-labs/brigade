"""Standalone audit storage, transaction, secrecy and cursor contracts."""

import json
import re
import sqlite3
from datetime import datetime, timezone

import pytest

from brigade import fleet_hub_audit as audit


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    audit.ensure_schema(connection)
    yield connection
    connection.close()


def test_explicit_schema_repeat_reopen_and_transaction_ownership(tmp_path):
    path = tmp_path / "audit.sqlite"
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
        audit.ensure_schema(connection)
        connection.execute("CREATE TABLE sibling (id INTEGER)")
        connection.execute("INSERT INTO sibling VALUES (1)")
        first = audit.append(connection, actor="admin", action="grants.change", subject="agent:fixture", detail={})
        audit.ensure_schema(connection)
        assert connection.in_transaction
        connection.rollback()
        assert audit.page(connection) == []
        assert connection.execute("SELECT * FROM sibling").fetchall() == []
        saved = audit.append(connection, actor="admin", action="member.remove", subject="agent:fixture", detail={})
        assert first > 0 and saved > 0
    with sqlite3.connect(path) as connection:
        audit.ensure_schema(connection)
        assert [row["id"] for row in audit.page(connection)] == [saved]
        for sql in ("UPDATE fleet_audit_log SET actor='other'", "DELETE FROM fleet_audit_log"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(sql)
        assert len(audit.page(connection)) == 1


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT OR REPLACE INTO fleet_audit_log VALUES (?, 'now', 'other', 'member.remove', 'fixture', '{}')",
        "REPLACE INTO fleet_audit_log VALUES (?, 'now', 'other', 'member.remove', 'fixture', '{}')",
        "INSERT INTO fleet_audit_log VALUES (?, 'now', 'other', 'member.remove', 'fixture', '{}') "
        "ON CONFLICT(id) DO UPDATE SET actor=excluded.actor",
    ],
    ids=["insert-or-replace", "replace", "upsert"],
)
def test_duplicate_id_cannot_replace_committed_history_or_finish_caller_work(tmp_path, statement):
    path = tmp_path / "audit.sqlite"
    with sqlite3.connect(path) as connection:
        audit.ensure_schema(connection)
        connection.execute("CREATE TABLE sibling (id INTEGER)")
        saved = audit.append(connection, actor="admin", action="invite.create", subject="fixture", detail={})
    with sqlite3.connect(path) as connection, sqlite3.connect(path) as reader:
        assert connection.execute("PRAGMA recursive_triggers").fetchone() == (0,)
        audit.ensure_schema(connection)
        original = audit.page(reader)
        connection.execute("INSERT INTO sibling VALUES (1)")
        audit.ensure_schema(connection)
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(statement, (saved,))
        assert connection.in_transaction
        assert connection.execute("SELECT * FROM sibling").fetchall() == [(1,)]
        assert reader.execute("SELECT * FROM sibling").fetchall() == []
        assert audit.page(connection) == audit.page(reader) == original
        next_id = audit.append(connection, actor="admin", action="member.remove", subject="fixture", detail={})
        assert next_id > saved
        assert [row["id"] for row in audit.page(connection)] == [saved, next_id]
        assert audit.page(reader) == original
        connection.rollback()
        assert connection.execute("SELECT * FROM sibling").fetchall() == []
        assert audit.page(connection) == original
        final_id = audit.append(connection, actor="admin", action="member.remove", subject="fixture", detail={})
        connection.commit()
        assert final_id > saved
        assert audit.page(reader)[0] == original[0]
        assert [row["id"] for row in audit.page(reader)] == [saved, final_id]
        assert connection.execute("PRAGMA recursive_triggers").fetchone() == (0,)


ACTIONS = (
    "invite.create",
    "invite.redeem",
    "invite.redeem_refused",
    "invite.revoke",
    "member.remove",
    "grants.change",
    "key.publish",
    "key.rotate",
    "key.revoke",
    "hub.identity",
    "hub.migration",
    "statement.upload",
)


@pytest.mark.parametrize("action", ACTIONS)
def test_actions_and_all_structured_fields_round_trip(conn, action):
    detail = {
        "principal_id": "agent:fixture",
        "node_id": "fixture-node",
        "invite_id": "abcdefghijklmnopqrstuv",
        "hub_id": "fixture-hub",
        "key_id": "fixture-key",
        "statement_id": "fixture-statement",
        "role": "teammate",
        "grants": ["*", "node:*", "agent:*", "agent:fixture", "fixture-node"],
        "count": 2,
    }
    row_id = audit.append(conn, actor="admin", action=action, subject="agent:fixture", detail=detail)
    row = audit.page(conn)[0]
    assert row["id"] == row_id
    assert row["actor"] == "admin" and row["subject"] == "agent:fixture" and row["action"] == action
    assert row["detail"] == detail
    assert datetime.fromisoformat(row["at"]).utcoffset() == timezone.utc.utcoffset(None)


@pytest.mark.parametrize("role", ["agent", "teammate"])
def test_roles_and_count_boundaries(conn, role):
    for count in (0, 1_000_000):
        audit.append(
            conn,
            actor="fixture-node",
            action="grants.change",
            subject="agent:fixture",
            detail={"role": role, "count": count, "grants": []},
        )
    assert [row["detail"]["count"] for row in audit.page(conn)] == [0, 1_000_000]


# Synthetic credentials only. Every value fits identifier syntax except deliberate
# free-text cases, so rejection must also protect allowed fields, not just JSON keys.
SECRETS = (
    "bfi1.hub.invite.synthetic-secret.none",
    "ghp_" + "a" * 36,
    "github_pat_" + "a" * 35,
    "sk-test-" + "a" * 24,
    "AKIA" + "A" * 16,
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.fake",
    "prefix.bfi1.hub.invite.secret.none",
    "fixture.token.value",
)


PUBLIC_NODE_IDS = (
    pytest.param("a" * 64, id="approved-64"),
    pytest.param("aB3_" * 10 + "xyz", id="approved-43"),
    pytest.param("0123456789abcdef0123456789abcdef", id="node-32"),
    pytest.param("n" * 128, id="node-128"),
)


@pytest.mark.parametrize("value", PUBLIC_NODE_IDS)
@pytest.mark.parametrize("field", ["actor", "subject", "principal_id", "node_id", "grants"])
def test_public_node_identities_round_trip(conn, field, value):
    kwargs = {"actor": "admin", "action": "grants.change", "subject": "fixture", "detail": {}}
    if field in ("actor", "subject"):
        kwargs[field] = value
    else:
        kwargs["detail"] = {field: [value] if field == "grants" else value}
    audit.append(conn, **kwargs)
    row = audit.page(conn)[0]
    assert {key: row[key] for key in kwargs} == kwargs


@pytest.mark.parametrize("value", ["desk-01", "kiosk-2", "content", "token", "binding"])
@pytest.mark.parametrize("field", ["actor", "subject", "principal_id", "node_id", "grants"])
def test_ordinary_identity_words_are_not_credential_markers(conn, field, value):
    kwargs = {"actor": "admin", "action": "grants.change", "subject": "fixture", "detail": {}}
    if field in ("actor", "subject"):
        kwargs[field] = value
    else:
        kwargs["detail"] = {field: [value] if field == "grants" else value}
    audit.append(conn, **kwargs)
    row = audit.page(conn)[0]
    assert {key: row[key] for key in kwargs} == kwargs


@pytest.mark.parametrize("name", ["task-runner", "disk_cleaner", "content-bot", "tokenizer"])
@pytest.mark.parametrize("field", ["actor", "subject", "principal_id", "grants"])
def test_ordinary_agent_names_are_not_credential_markers(conn, field, name):
    value = "agent:" + name
    kwargs = {"actor": "admin", "action": "grants.change", "subject": "fixture", "detail": {}}
    if field in ("actor", "subject"):
        kwargs[field] = value
    else:
        kwargs["detail"] = {field: [value] if field == "grants" else value}
    audit.append(conn, **kwargs)
    row = audit.page(conn)[0]
    assert {key: row[key] for key in kwargs} == kwargs


@pytest.mark.parametrize("value", ["agent:token.synthetic", "agent:secret.synthetic"])
@pytest.mark.parametrize("field", ["actor", "subject", "principal_id", "grants"])
def test_agent_sensitive_labels_are_refused_without_writes_or_finishing_caller_work(conn, field, value):
    conn.execute("CREATE TABLE sibling (id INTEGER)")
    conn.execute("INSERT INTO sibling VALUES (1)")
    audit.append(conn, actor="admin", action="invite.create", subject="fixture", detail={})
    original = audit.page(conn)
    changes = conn.total_changes
    kwargs = {"actor": "admin", "action": "grants.change", "subject": "fixture", "detail": {}}
    if field in ("actor", "subject"):
        kwargs[field] = value
    else:
        kwargs["detail"] = {field: [value] if field == "grants" else value}

    with pytest.raises(ValueError) as error:
        audit.append(conn, **kwargs)

    assert value not in str(error.value)
    assert value.removeprefix("agent:") not in str(error.value)
    assert conn.total_changes == changes
    assert conn.in_transaction
    assert conn.execute("SELECT * FROM sibling").fetchall() == [(1,)]
    assert audit.page(conn) == original
    conn.rollback()
    assert conn.execute("SELECT * FROM sibling").fetchall() == []
    assert audit.page(conn) == []


@pytest.mark.parametrize("value", PUBLIC_NODE_IDS)
@pytest.mark.parametrize("field", ["invite_id", "hub_id", "key_id", "statement_id"])
def test_nonidentity_fields_still_refuse_opaque_runs(conn, field, value):
    with pytest.raises(ValueError) as error:
        audit.append(conn, actor="admin", action="invite.create", subject="fixture", detail={field: value})
    assert value not in str(error.value)
    assert audit.page(conn) == []


@pytest.mark.parametrize("field", ["actor", "subject", "principal_id", "grants"])
@pytest.mark.parametrize("name", ["a" * 64, "Fixture", "1fixture", "fixt*"])
def test_invalid_agent_names_cannot_fall_back_to_node_identities(conn, field, name):
    value = "agent:" + name
    kwargs = {"actor": "admin", "action": "grants.change", "subject": "fixture", "detail": {}}
    if field in ("actor", "subject"):
        kwargs[field] = value
    else:
        kwargs["detail"] = {field: [value] if field == "grants" else value}
    with pytest.raises(ValueError) as error:
        audit.append(conn, **kwargs)
    assert value not in str(error.value)
    assert audit.page(conn) == []


def test_maximum_length_agent_identity_round_trips(conn):
    value = "agent:" + "a" * 63
    detail = {"principal_id": value, "grants": [value]}
    audit.append(conn, actor=value, action="grants.change", subject=value, detail=detail)
    row = audit.page(conn)[0]
    assert row["actor"] == row["subject"] == value
    assert row["detail"] == detail


@pytest.mark.parametrize("value", SECRETS)
@pytest.mark.parametrize(
    "field", ["actor", "subject", "principal_id", "node_id", "invite_id", "hub_id", "key_id", "statement_id", "grants"]
)
def test_credentials_cannot_hide_in_identifiers_or_grants(conn, field, value):
    kwargs = {"actor": "admin", "action": "invite.create", "subject": "agent:fixture", "detail": {}}
    if field in ("actor", "subject"):
        kwargs[field] = value
    else:
        kwargs["detail"] = {field: ["agent:" + value] if field == "grants" else value}
    with pytest.raises(ValueError) as error:
        audit.append(conn, **kwargs)
    assert value not in str(error.value)
    assert audit.page(conn) == []


@pytest.mark.parametrize(
    "value",
    [
        "AIza" + "synthetic" * 4,
        "xoxe.synthetic-configuration",
        "xoxe-synthetic-configuration",
        "gldt-synthetic-deploy",
        "npm_synthetic-package",
        "hf_synthetic-model",
    ],
)
@pytest.mark.parametrize("prefix", ["", "prefix.", "prefix_", "prefix-"])
@pytest.mark.parametrize(
    "field", ["actor", "subject", "principal_id", "node_id", "invite_id", "hub_id", "key_id", "statement_id", "grants"]
)
def test_additional_provider_markers_are_refused_at_identifier_boundaries(conn, field, prefix, value):
    value = prefix + value
    kwargs = {"actor": "admin", "action": "invite.create", "subject": "fixture", "detail": {}}
    if field in ("actor", "subject"):
        kwargs[field] = value
    else:
        kwargs["detail"] = {field: [value] if field == "grants" else value}
    with pytest.raises(ValueError) as error:
        audit.append(conn, **kwargs)
    assert value not in str(error.value)
    assert audit.page(conn) == []


@pytest.mark.parametrize(
    "value", ["xoxe.synthetic", "xoxe-synthetic", "gldt-synthetic", "npm_synthetic", "hf_synthetic"]
)
@pytest.mark.parametrize("field", ["actor", "subject", "principal_id", "grants"])
def test_additional_provider_markers_are_refused_in_agent_identities(conn, field, value):
    value = "agent:" + value
    kwargs = {"actor": "admin", "action": "grants.change", "subject": "fixture", "detail": {}}
    if field in ("actor", "subject"):
        kwargs[field] = value
    else:
        kwargs["detail"] = {field: [value] if field == "grants" else value}
    with pytest.raises(ValueError) as error:
        audit.append(conn, **kwargs)
    assert value not in str(error.value)
    assert audit.page(conn) == []


@pytest.mark.parametrize(
    "detail",
    [
        {"token": "synthetic"},
        {"secret": "synthetic"},
        {"credential": "synthetic"},
        {"key_record": {}},
        {"binding": {}},
        {"content": "synthetic"},
        {"body": "synthetic"},
        {"note": "arbitrary text"},
        {"principal_id": {"token": "synthetic"}},
        {"grants": [["agent:fixture"]]},
        {"grants": [True]},
        {"role": "admin"},
        {"role": {"role": "teammate"}},
        {"count": True},
        {"count": -1},
        {"count": 1.5},
        {"count": 1_000_001},
        {"count": "1"},
        {"node_id": "a" * 129},
        {"node_id": "hello world"},
        {"grants": ["agent:fixture"] * 65},
        {"grants": ["agent:*"] * 64 + ["node:*"]},
        {"grants": "*"},
        {"grants": ["agent:fixt*"]},
        {"grants": ["a" * 129]},
        {"principal_id": "a" * 129},
        {"node_id": "agent:fixture"},
        {"hub_id": "prefix." + "a" * 32 + ".suffix"},
        {"node_id": ""},
        [],
        None,
    ],
)
def test_invalid_detail_is_refused_without_writing_or_rolling_back(conn, detail):
    audit.append(conn, actor="admin", action="invite.create", subject="fixture", detail={})
    with pytest.raises(ValueError):
        audit.append(conn, actor="admin", action="invite.create", subject="fixture", detail=detail)
    assert conn.in_transaction
    assert len(audit.page(conn)) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("actor", "hello world"),
        ("actor", ""),
        ("subject", "x" * 129),
        ("subject", None),
        ("action", "custom.action"),
        ("action", "invite.create\n"),
        ("action", []),
    ],
)
def test_invalid_envelope_is_refused(conn, field, value):
    kwargs = {"actor": "admin", "action": "invite.create", "subject": "fixture", "detail": {}}
    kwargs[field] = value
    with pytest.raises(ValueError):
        audit.append(conn, **kwargs)
    assert audit.page(conn) == []


def test_paging_is_ordered_strictly_after_id_with_parsed_detail(conn):
    assert audit.page(conn) == []
    ids = [
        audit.append(conn, actor="admin", action="invite.create", subject="fixture", detail={"count": n})
        for n in range(5)
    ]
    rows = audit.page(conn, limit=2)
    rows += audit.page(conn, after_id=rows[-1]["id"], limit=2)
    rows += audit.page(conn, after_id=rows[-1]["id"], limit=2)
    assert [row["id"] for row in rows] == ids == sorted(ids)
    assert [row["detail"]["count"] for row in rows] == list(range(5))
    assert audit.page(conn, after_id=ids[-1]) == []
    assert len(audit.page(conn, limit=500)) == 5


@pytest.mark.parametrize("field", ["actor", "subject"])
@pytest.mark.parametrize(
    "value",
    [
        pytest.param("", id="empty"),
        pytest.param(" \t", id="whitespace"),
        pytest.param("n" * 129, id="oversized"),
        pytest.param("unknown", id="reserved"),
        pytest.param("agent:Fixture", id="uppercase-agent"),
        pytest.param("agent:1fixture", id="numeric-agent"),
        pytest.param("agent:" + "a" * 64, id="oversized-agent"),
        pytest.param("agent:fixt*", id="wildcard-agent"),
        pytest.param(b"fixture", id="blob"),
    ],
)
def test_paging_refuses_malformed_stored_identities_without_echoing_values(conn, field, value):
    saved = audit.append(conn, actor="admin", action="invite.create", subject="fixture", detail={})
    row = {"actor": "admin", "subject": "fixture"}
    row[field] = value
    conn.execute(
        "INSERT INTO fleet_audit_log(at,actor,action,subject,detail) VALUES(?,?,?,?,?)",
        ("2026-01-01T00:00:00+00:00", row["actor"], "invite.create", row["subject"], "{}"),
    )
    assert [row["id"] for row in audit.page(conn, limit=1)] == [saved]
    for kwargs in ({}, {"after_id": saved}):
        with pytest.raises(ValueError) as error:
            audit.page(conn, **kwargs)
        assert str(error.value) == "invalid audit identifier"
    assert conn.execute("SELECT COUNT(*) FROM fleet_audit_log").fetchone() == (2,)


@pytest.mark.parametrize("action", ["custom.action", "invite.create\n", "", b"invite.create"])
def test_paging_refuses_invalid_stored_actions_without_echoing_values(conn, action):
    conn.execute(
        "INSERT INTO fleet_audit_log(at,actor,action,subject,detail) VALUES(?,?,?,?,?)",
        ("2026-01-01T00:00:00+00:00", "admin", action, "fixture", "{}"),
    )
    with pytest.raises(ValueError) as error:
        audit.page(conn)
    assert str(error.value) == "invalid audit action"
    assert conn.execute("SELECT COUNT(*) FROM fleet_audit_log").fetchone() == (1,)


@pytest.mark.parametrize("action", ACTIONS)
def test_paging_accepts_structurally_valid_direct_sql_rows(conn, action):
    conn.execute(
        "INSERT INTO fleet_audit_log(at,actor,action,subject,detail) VALUES(?,?,?,?,?)",
        ("2026-01-01T00:00:00+00:00", "n" * 128, action, "agent:" + "a" * 63, '{"count":2}'),
    )
    row = audit.page(conn)[0]
    assert row["actor"] == "n" * 128
    assert row["action"] == action
    assert row["subject"] == "agent:" + "a" * 63
    assert row["detail"] == {"count": 2}


@pytest.mark.parametrize("field", ["actor", "subject"])
@pytest.mark.parametrize("value", ["fixture-node", "agent:fixture"])
def test_identity_history_remains_readable_when_write_admission_markers_tighten(conn, monkeypatch, field, value):
    kwargs = {"actor": "admin", "action": "invite.create", "subject": "other", "detail": {}}
    kwargs[field] = value
    audit.append(conn, **kwargs)
    conn.commit()
    original = audit.page(conn)
    monkeypatch.setattr(audit, "_CREDENTIAL", re.compile(re.escape(value)))
    assert audit.page(conn) == original
    with pytest.raises(ValueError) as error:
        audit.append(conn, **kwargs)
    assert value not in str(error.value)
    assert audit.page(conn) == original


def test_history_remains_readable_when_write_admission_markers_tighten(conn, monkeypatch):
    detail = {
        "principal_id": "agent:fixture",
        "node_id": "fixture-node",
        "invite_id": "fixture-invite",
        "hub_id": "fixture-hub",
        "key_id": "fixture-key",
        "statement_id": "fixture-statement",
        "role": "teammate",
        "grants": ["fixture-node", "agent:fixture", "*"],
        "count": 2,
    }
    saved = audit.append(conn, actor="fixture-node", action="grants.change", subject="agent:fixture", detail=detail)
    next_id = audit.append(conn, actor="admin", action="member.remove", subject="other", detail={})
    conn.commit()
    original = audit.page(conn)
    monkeypatch.setattr(audit, "_CREDENTIAL", re.compile("fixture"))
    with pytest.raises(ValueError) as error:
        audit.append(conn, actor="fixture-node", action="grants.change", subject="agent:fixture", detail=detail)
    assert "fixture" not in str(error.value)
    assert audit.page(conn) == original
    assert audit.page(conn, limit=1)[0]["detail"] == detail
    assert [row["id"] for row in audit.page(conn, after_id=saved)] == [next_id]


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "[]",
        json.dumps({"note": "free text"}),
        json.dumps({"principal_id": {"nested": "fixture"}}),
        json.dumps({"node_id": "n" * 129}),
        json.dumps({"principal_id": "agent:" + "a" * 64}),
        json.dumps({"principal_id": "agent:Fixture"}),
        json.dumps({"hub_id": "a" * 32}),
        json.dumps({"role": "admin"}),
        json.dumps({"count": True}),
        json.dumps({"count": -1}),
        json.dumps({"count": 1_000_001}),
        json.dumps({"count": 1.5}),
        json.dumps({"grants": ["fixture"] * 65}),
        json.dumps({"grants": "*"}),
        json.dumps({"grants": [["fixture"]]}),
        json.dumps({"grants": [True]}),
        json.dumps({"grants": ["agent:fixt*"]}),
    ],
)
def test_paging_refuses_malformed_stored_detail_without_dropping_rows(conn, raw):
    saved = audit.append(conn, actor="admin", action="invite.create", subject="fixture", detail={})
    conn.execute(
        "INSERT INTO fleet_audit_log(at,actor,action,subject,detail) VALUES(?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(), "admin", "invite.create", "fixture", raw),
    )
    with pytest.raises(ValueError):
        audit.page(conn)
    assert [row["id"] for row in audit.page(conn, limit=1)] == [saved]
    with pytest.raises(ValueError):
        audit.page(conn, after_id=saved)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"after_id": -1},
        {"after_id": True},
        {"after_id": 1.0},
        {"after_id": "0"},
        {"after_id": 2**63},
        {"limit": 0},
        {"limit": 501},
        {"limit": True},
        {"limit": 1.0},
        {"limit": "2"},
    ],
)
def test_invalid_page_bounds(conn, kwargs):
    with pytest.raises(ValueError):
        audit.page(conn, **kwargs)


@pytest.mark.parametrize("value", ["glpat-" + "a" * 20, "GLPAT-fixture", "x.glpat-fixture"])
@pytest.mark.parametrize(
    "field", ["actor", "subject", "principal_id", "node_id", "invite_id", "hub_id", "key_id", "statement_id", "grants"]
)
def test_gitlab_credentials_are_refused_everywhere(conn, field, value):
    kwargs = {"actor": "admin", "action": "invite.create", "subject": "fixture", "detail": {}}
    if field in ("actor", "subject"):
        kwargs[field] = value
    else:
        kwargs["detail"] = {field: [value] if field == "grants" else value}
    with pytest.raises(ValueError) as error:
        audit.append(conn, **kwargs)
    assert value not in str(error.value)
    assert audit.page(conn) == []


@pytest.mark.parametrize("field", ["actor", "subject", "principal_id", "grants"])
def test_agent_grants_and_principals_refuse_gitlab_credentials(conn, field):
    value = "agent:glpat-fixture"
    kwargs = {"actor": "admin", "action": "grants.change", "subject": "fixture", "detail": {}}
    if field in ("actor", "subject"):
        kwargs[field] = value
    else:
        kwargs["detail"] = {field: [value] if field == "grants" else value}
    with pytest.raises(ValueError) as error:
        audit.append(conn, **kwargs)
    assert value not in str(error.value)
    assert audit.page(conn) == []


@pytest.mark.parametrize("field", ["actor", "subject", "principal_id", "node_id", "grants"])
def test_reserved_unknown_identity_is_refused(conn, field):
    kwargs = {"actor": "admin", "action": "grants.change", "subject": "fixture", "detail": {}}
    if field in ("actor", "subject"):
        kwargs[field] = "unknown"
    else:
        kwargs["detail"] = {field: ["unknown"] if field == "grants" else "unknown"}
    with pytest.raises(ValueError) as error:
        audit.append(conn, **kwargs)
    assert "unknown" not in str(error.value)
    assert audit.page(conn) == []


TRANSACTION_MODES = [
    pytest.param({"isolation_level": None}, id="legacy-explicit"),
    pytest.param({"isolation_level": ""}, id="legacy-default"),
    pytest.param({"isolation_level": "DEFERRED"}, id="legacy-deferred"),
    pytest.param({"isolation_level": "IMMEDIATE"}, id="legacy-immediate"),
    pytest.param({"isolation_level": "EXCLUSIVE"}, id="legacy-exclusive"),
]
NO_TRANSACTION_MODES = [pytest.param({"isolation_level": None}, id="legacy-explicit")]
if hasattr(sqlite3.Connection, "autocommit"):
    for mode in (True, False):
        for isolation in (None, "DEFERRED"):
            case = pytest.param({"autocommit": mode, "isolation_level": isolation}, id=f"autocommit-{mode}-{isolation}")
            TRANSACTION_MODES.append(case)
            NO_TRANSACTION_MODES.append(case)


@pytest.mark.parametrize("options", NO_TRANSACTION_MODES)
def test_append_refuses_no_active_transaction_without_leaking(tmp_path, options):
    path = tmp_path / "audit.sqlite"
    connection = sqlite3.connect(path, **options)
    reader = sqlite3.connect(path, isolation_level=None)
    try:
        audit.ensure_schema(connection)
        if connection.in_transaction:
            # autocommit=False does not guarantee a transaction after SQL COMMIT.
            connection.execute("COMMIT")
        assert not connection.in_transaction
        with pytest.raises(ValueError, match="transaction"):
            audit.append(connection, actor="admin", action="invite.create", subject="fixture", detail={})
        assert not connection.in_transaction
        assert audit.page(connection) == audit.page(reader) == []
    finally:
        connection.close()
        reader.close()


@pytest.mark.parametrize("options", TRANSACTION_MODES)
def test_explicit_transactions_keep_mutation_and_audit_atomic(tmp_path, options):
    path = tmp_path / "audit.sqlite"
    connection = sqlite3.connect(path, **options)
    reader = sqlite3.connect(path, isolation_level=None)
    try:
        audit.ensure_schema(connection)
        connection.execute("CREATE TABLE sibling (id INTEGER)")
        if connection.in_transaction:
            connection.execute("COMMIT")
        connection.execute("BEGIN")
        connection.execute("INSERT INTO sibling VALUES (1)")
        audit.append(connection, actor="admin", action="invite.create", subject="fixture", detail={})
        with pytest.raises(ValueError):
            audit.append(connection, actor="glpat-fixture", action="invite.create", subject="fixture", detail={})
        audit.ensure_schema(connection)
        assert connection.in_transaction
        assert connection.execute("SELECT * FROM sibling").fetchall() == [(1,)]
        assert len(audit.page(connection)) == 1
        assert audit.page(reader) == []
        assert reader.execute("SELECT * FROM sibling").fetchall() == []
        connection.execute("ROLLBACK")
        assert audit.page(reader) == []
        assert reader.execute("SELECT * FROM sibling").fetchall() == []
        connection.execute("BEGIN")
        connection.execute("INSERT INTO sibling VALUES (2)")
        saved = audit.append(connection, actor="admin", action="invite.create", subject="fixture", detail={})
        connection.execute("COMMIT")
        assert [row["id"] for row in audit.page(reader)] == [saved]
        assert reader.execute("SELECT * FROM sibling").fetchall() == [(2,)]
    finally:
        connection.close()
        reader.close()


@pytest.mark.parametrize("isolation", ["", "DEFERRED", "IMMEDIATE", "EXCLUSIVE"])
def test_legacy_implicit_transaction_remains_caller_owned(tmp_path, isolation):
    path = tmp_path / "audit.sqlite"
    connection = sqlite3.connect(path, isolation_level=isolation)
    reader = sqlite3.connect(path, isolation_level=None)
    try:
        audit.ensure_schema(connection)
        assert not connection.in_transaction
        audit.append(connection, actor="admin", action="invite.create", subject="fixture", detail={})
        assert connection.in_transaction
        assert len(audit.page(connection)) == 1
        if isolation != "EXCLUSIVE":
            assert audit.page(reader) == []
        connection.rollback()
        assert audit.page(reader) == []
    finally:
        connection.close()
        reader.close()
