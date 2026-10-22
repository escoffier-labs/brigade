"""Question audit, ownership and terminal arbitration over the real hub route."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from brigade import fleet_hub, fleet_hub_messages
from tests.test_fleet_messages import NODE_A, NODE_B, _request

pytest_plugins = ["tests._fleet_message_fixtures"]


@pytest.fixture(autouse=True)
def operator(monkeypatch):
    monkeypatch.setenv("BRIGADE_FLEET_OPERATOR_RECIPIENT", NODE_B)


def create(hub, request_id="question-1", **fields):
    base, token, _other, _db = hub
    return _request(
        base,
        token,
        {"action": "question_create", "question": "Proceed?", "timeout_secs": 60, "request_id": request_id, **fields},
    )


def status(hub, question_id, token=None):
    return _request(hub[0], token or hub[1], {"action": "question_status", "question_id": question_id})


def test_online_creation_idempotency_and_trigger_audits_acked_answer(hub):
    code, question = create(hub)
    assert code == 201, question
    assert question["status"] == "pending" and question["created"] is True
    assert question["caller"] == NODE_A and question["operator"] == NODE_B
    code, duplicate = create(hub)
    assert code == 200 and duplicate["created"] is False
    assert duplicate["question_id"] == question["question_id"]
    assert duplicate["deadline"] == question["deadline"]
    assert create(hub, question="Different?")[0] == 409
    assert status(hub, question["question_id"], hub[2])[0] == 403
    code, reply = _request(hub[0], hub[2], {"action": "reply", "id": int(question["message_id"]), "body": "Yes"})
    assert code == 201
    _request(hub[0], hub[1], {"action": "ack", "id": reply["message"]["id"]})
    # The insert trigger records the answer before any question reader runs.
    with sqlite3.connect(hub[3]) as conn:
        row = conn.execute("SELECT status,answer,source FROM fleet_questions").fetchone()
    assert row == ("answered", "Yes", "operator_reply")
    result = status(hub, question["question_id"])[1]
    assert result["answer"] == "Yes" and result["answer_id"] == str(reply["message"]["id"])
    assert result["answered_by"] == NODE_B
    assert not any("generation" in key or "hash" in key for key in result)


@pytest.mark.parametrize(
    "fields",
    [
        {"caller": NODE_B},
        {"operator": NODE_A},
        {"source": "operator_reply"},
        {"timeout_secs": True},
        {"timeout_secs": 0},
        {"timeout_secs": 121},
        {"question": "x" * 4097},
        {"request_id": "bad\nkey"},
        {"question": "\ud800"},
    ],
)
def test_strict_create_validation(hub, fields):
    assert create(hub, **fields)[0] == 400
    with sqlite3.connect(hub[3]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 0


@pytest.mark.parametrize("recipient", ["", "missing-node", NODE_A])
def test_operator_selected_by_hub_and_must_be_active_other_principal(hub, monkeypatch, recipient):
    monkeypatch.setenv("BRIGADE_FLEET_OPERATOR_RECIPIENT", recipient)
    assert create(hub)[0] == 400


def test_first_terminal_wins_and_received_time_prevents_backdated_late_answer(hub):
    question = create(hub)[1]
    qid = question["question_id"]
    desktop = {"action": "question_desktop", "question_id": qid, "answer": "Local"}
    assert _request(hub[0], hub[1], {**desktop, "source": "operator_reply"})[0] == 400
    code, accepted = _request(hub[0], hub[1], desktop)
    assert code == 200 and accepted["source"] == "caller_reported_desktop"
    assert accepted["answered_by"] == NODE_A
    assert _request(hub[0], hub[2], {"action": "reply", "id": int(question["message_id"]), "body": "Later"})[0] == 201
    assert status(hub, qid)[1]["answer"] == "Local"
    late = create(hub, "late")[1]
    past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(hub[3]) as conn:
        conn.execute("UPDATE fleet_questions SET deadline=? WHERE question_id=?", (past, late["question_id"]))
    code, _ = _request(
        hub[0],
        hub[2],
        {
            "action": "reply",
            "id": int(late["message_id"]),
            "body": "Late",
            "enqueued_at": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        },
    )
    assert code == 201
    assert status(hub, late["question_id"])[1]["status"] == "timeout"
    assert _request(hub[0], hub[1], {**desktop, "question_id": late["question_id"]})[1]["status"] == "timeout"


@pytest.mark.parametrize("identity", ["node", "agent"])
def test_credential_generation_owns_questions_and_blocks_old_reply(hub, identity):
    base, token, operator_token, db = hub
    if identity == "agent":
        with fleet_hub.open_db(db) as conn:
            principal = fleet_hub_messages.add_principal(conn, {"name": "asker", "may_send_to": [NODE_B]})
        token = principal["token"]
    request = {"action": "question_create", "question": "Proceed?", "timeout_secs": 60, "request_id": "rotate"}
    code, original = _request(base, token, request)
    assert code == 201, original
    with fleet_hub.open_db(db) as conn:
        if identity == "node":
            fleet_hub.revoke_node(conn, NODE_A)
            _node, replacement = fleet_hub.add_node(conn, NODE_A)
        else:
            fleet_hub_messages.revoke_principal(
                conn, {"action": "revoke", "name": "asker", "expected_enrollment_id": principal["enrollment_id"]}
            )
            replacement = fleet_hub_messages.add_principal(
                conn, {"name": "asker", "may_send_to": [NODE_B], "expected_enrollment_id": principal["enrollment_id"]}
            )["token"]
    assert status(hub, original["question_id"], replacement)[0] == 403
    assert (
        _request(base, operator_token, {"action": "reply", "id": int(original["message_id"]), "body": "Old"})[0] == 201
    )
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT answer FROM fleet_questions WHERE question_id=?", (original["question_id"],)
        ).fetchone() == (None,)
    code, renewed = _request(base, replacement, request)
    assert code == 201 and renewed["question_id"] != original["question_id"]


def test_only_operator_reply_can_answer_and_grants_apply(hub):
    base, ta, tb, db = hub
    with fleet_hub.open_db(db) as conn:
        other = fleet_hub_messages.add_principal(conn, {"name": "other", "may_send_to": [NODE_A]})
    question = create(hub)[1]
    assert (
        _request(base, other["token"], {"action": "reply", "id": int(question["message_id"]), "body": "Forged"})[0]
        == 404
    )
    assert (
        _request(
            base,
            other["token"],
            {
                "action": "send",
                "to": NODE_A,
                "subject": "Answer",
                "body": "Forged",
                "reply_to": int(question["message_id"]),
            },
        )[0]
        == 400
    )
    assert status(hub, question["question_id"])[1]["status"] == "pending"
    assert (
        _request(
            base,
            other["token"],
            {"action": "question_create", "question": "Denied?", "timeout_secs": 1, "request_id": "denied"},
        )[0]
        == 403
    )


def test_schema_37_migration_and_current_version_selfheal(hub):
    db = hub[3]
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER IF EXISTS fleet_questions_reply")
        conn.execute("DROP TABLE IF EXISTS fleet_questions")
        conn.execute("PRAGMA user_version=37")
    fleet_hub.init_db(db).close()
    assert create(hub)[0] == 201
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 38
        conn.execute("DROP TRIGGER fleet_questions_reply")
        conn.execute("DROP INDEX fleet_questions_deadline")
    fleet_hub.init_db(db).close()
    with sqlite3.connect(db) as conn:
        assert {row[0] for row in conn.execute("SELECT name FROM sqlite_master")} >= {
            "fleet_questions_reply",
            "fleet_questions_deadline",
        }
    assert status(hub, create(hub)[1]["question_id"])[0] == 200


def test_reply_first_cannot_be_replaced_by_desktop_or_later_reply(hub):
    question = create(hub)[1]
    assert _request(hub[0], hub[2], {"action": "reply", "id": int(question["message_id"]), "body": "First"})[0] == 201
    assert _request(hub[0], hub[2], {"action": "reply", "id": int(question["message_id"]), "body": "Second"})[0] == 201
    result = _request(
        hub[0], hub[1], {"action": "question_desktop", "question_id": question["question_id"], "answer": "Desktop"}
    )[1]
    assert result["status"] == "answered" and result["answer"] == "First" and result["source"] == "operator_reply"


def test_atomic_creation_rolls_back_question_message_on_audit_failure(hub):
    with sqlite3.connect(hub[3]) as conn:
        conn.execute(
            "CREATE TRIGGER reject_question BEFORE INSERT ON fleet_questions BEGIN SELECT RAISE(ABORT, 'audit unavailable'); END"
        )
    assert create(hub)[0] == 500
    with sqlite3.connect(hub[3]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM fleet_message_sends").fetchone()[0] == 0


def test_generation_change_after_authentication_is_rejected(hub, monkeypatch):
    lookup = fleet_hub.lookup_node_token
    rotated = False

    def rotating_lookup(conn, presented):
        nonlocal rotated
        result = lookup(conn, presented)
        if result[0] == NODE_A and not rotated:
            rotated = True
            fleet_hub.revoke_node(conn, NODE_A)
            fleet_hub.add_node(conn, NODE_A)
        return result

    monkeypatch.setattr(fleet_hub, "lookup_node_token", rotating_lookup)
    assert create(hub)[0] == 403
    with sqlite3.connect(hub[3]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fleet_questions").fetchone()[0] == 0


def test_current_schema_repairs_missing_question_table(hub):
    with sqlite3.connect(hub[3]) as conn:
        conn.execute("DROP TABLE fleet_questions")
    fleet_hub.init_db(hub[3]).close()
    assert create(hub)[0] == 201


def test_question_uses_existing_send_quota_without_partial_audit(hub, monkeypatch):
    monkeypatch.setattr(fleet_hub_messages, "UNACKED_QUOTA", 0)
    code, result = create(hub)
    assert code == 429 and result["code"] == "message_recipient_quota"
    with sqlite3.connect(hub[3]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fleet_questions").fetchone()[0] == 0


def test_reply_one_microsecond_after_hub_deadline_is_late(hub, monkeypatch):
    question = create(hub)[1]
    deadline = (datetime.now(timezone.utc) - timedelta(seconds=1)).replace(microsecond=100000)
    received = deadline + timedelta(microseconds=1)

    class ReceivedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return received

    with sqlite3.connect(hub[3]) as conn:
        conn.execute("UPDATE fleet_questions SET deadline=?", (deadline.isoformat(),))
    monkeypatch.setattr(fleet_hub_messages, "datetime", ReceivedClock)
    code, _ = _request(hub[0], hub[2], {"action": "reply", "id": int(question["message_id"]), "body": "Too late"})
    assert code == 201
    with sqlite3.connect(hub[3]) as conn:
        assert conn.execute("SELECT status,answer FROM fleet_questions").fetchone() == ("pending", None)
    assert status(hub, question["question_id"])[1]["status"] == "timeout"


def test_answer_copy_survives_message_pruning(hub):
    question = create(hub)[1]
    code, reply = _request(
        hub[0], hub[2], {"action": "reply", "id": int(question["message_id"]), "body": "Keep the audit"}
    )
    assert code == 201
    with fleet_hub.open_db(hub[3]) as conn:
        past = (datetime.now(timezone.utc) - timedelta(days=91)).isoformat()
        conn.execute("UPDATE fleet_messages SET created_at=?", (past,))
        conn.commit()
        fleet_hub_messages._prune_expired(conn)
        assert conn.execute("SELECT COUNT(*) FROM fleet_messages").fetchone()[0] == 0
    result = status(hub, question["question_id"])[1]
    assert result["answer"] == "Keep the audit" and result["answer_id"] == str(reply["message"]["id"])
