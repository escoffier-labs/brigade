"""Question audit, ownership and terminal arbitration over the real hub route."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from multiprocessing import get_context

import pytest

from brigade import fleet_hub, fleet_hub_messages
from tests._fleet_question_fixtures import OPERATOR
from tests.test_fleet_messages import NODE_A, NODE_B, _request

pytest_plugins = ["tests._fleet_question_fixtures"]


@pytest.fixture
def hub(question_hub):
    return question_hub


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
    assert question["caller"] == NODE_A and question["operator"] == OPERATOR
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
    assert result["answered_by"] == OPERATOR
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


@pytest.mark.parametrize("recipient", ["", "missing-node", NODE_A, NODE_B, "admin"])
def test_operator_selected_by_hub_and_must_be_active_other_principal(hub, monkeypatch, recipient):
    monkeypatch.setenv("BRIGADE_FLEET_OPERATOR_RECIPIENT", recipient)
    code, result = create(hub)
    assert code == 403 and result["code"] == "question_operator_invalid"
    assert "dedicated active agent:" in result["error"]


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
            principal = fleet_hub_messages.add_principal(conn, {"name": "asker", "may_send_to": [OPERATOR]})
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
                conn, {"name": "asker", "may_send_to": [OPERATOR], "expected_enrollment_id": principal["enrollment_id"]}
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


@pytest.mark.parametrize("version", [37, 40, 42])
def test_legacy_migration_and_current_version_selfheal(hub, version):
    db = hub[3]
    with sqlite3.connect(db) as conn:
        conn.execute("DROP TRIGGER IF EXISTS fleet_questions_reply")
        conn.execute("DROP TABLE IF EXISTS fleet_questions")
        conn.execute(f"PRAGMA user_version={version}")
    fleet_hub.init_db(db).close()
    fleet_hub.init_db(db).close()
    assert create(hub)[0] == 201
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
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
        assert conn.execute("SELECT status,answer FROM fleet_questions").fetchone() == ("timeout", None)
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


@pytest.fixture(params=["node", "agent"])
def asking_station(hub, request):
    from tests._fleet_message_fixtures import _RedactedHub

    if request.param == "node":
        return hub, NODE_A, None
    with fleet_hub.open_db(hub[3]) as conn:
        principal = fleet_hub_messages.add_principal(conn, {"name": "asker", "may_send_to": [OPERATOR]})
    station = _RedactedHub((hub[0], principal["token"], hub[2], hub[3]))
    return station, principal["principal_id"], principal["enrollment_id"]


def test_pending_limit_is_per_caller_and_resumes_at_capacity(asking_station):
    station, caller, _enrollment = asking_station
    first = create(station, "first")[1]
    assert create(station, "second")[0] == 201
    code, rejected = create(station, "third")
    assert code == 429 and rejected["code"] == "question_station_busy"
    code, resumed = create(station, "first")
    assert code == 200 and resumed["created"] is False
    assert resumed["question_id"] == first["question_id"] and resumed["deadline"] == first["deadline"]
    assert create(station, "first", question="Changed?")[0] == 409
    with sqlite3.connect(station[3]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fleet_questions").fetchone() == (2,)
        assert conn.execute("SELECT COUNT(*) FROM fleet_messages").fetchone() == (2,)
        assert conn.execute("SELECT COUNT(*) FROM fleet_message_sends").fetchone() == (2,)
        assert conn.execute("SELECT COUNT(*) FROM fleet_messages WHERE acked_at IS NULL").fetchone() == (2,)
    # Another station identity has its own quota, even with the same operator.
    with fleet_hub.open_db(station[3]) as conn:
        other = fleet_hub_messages.add_principal(conn, {"name": "other-asker", "may_send_to": [OPERATOR]})
    code, independent = _request(
        station[0],
        other["token"],
        {"action": "question_create", "question": "Independent?", "timeout_secs": 60, "request_id": "first"},
    )
    assert code == 201 and independent["caller"] != caller


def test_pending_limit_counts_old_credential_generations(asking_station):
    station, caller, enrollment = asking_station
    first = create(station, "first")[1]
    assert create(station, "second")[0] == 201
    with fleet_hub.open_db(station[3]) as conn:
        if enrollment is None:
            fleet_hub.revoke_node(conn, caller)
            replacement = fleet_hub.add_node(conn, caller)[1]
        else:
            expected = {"name": "asker", "expected_enrollment_id": enrollment}
            fleet_hub_messages.revoke_principal(conn, {"action": "revoke", **expected})
            replacement = fleet_hub_messages.add_principal(conn, {**expected, "may_send_to": [OPERATOR]})["token"]
    # Reusing the old request ID under a new generation must also obey the cap.
    raw = {"action": "question_create", "question": "Proceed?", "timeout_secs": 60, "request_id": "first"}
    code, rejected = _request(station[0], replacement, raw)
    assert code == 429 and rejected["code"] == "question_station_busy"
    with sqlite3.connect(station[3]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fleet_questions").fetchone() == (2,)
        assert conn.execute("SELECT COUNT(*) FROM fleet_message_sends").fetchone() == (2,)
        conn.execute(
            "UPDATE fleet_questions SET deadline=? WHERE question_id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), first["question_id"]),
        )
    assert _request(station[0], replacement, raw)[0] == 201
    with sqlite3.connect(station[3]) as conn:
        assert conn.execute(
            "SELECT status FROM fleet_questions WHERE question_id=?", (first["question_id"],)
        ).fetchone() == ("timeout",)


@pytest.mark.parametrize("terminal", ["operator_reply", "desktop", "timeout"])
def test_terminal_question_frees_pending_quota(asking_station, terminal):
    station, _caller, _enrollment = asking_station
    first = create(station, "first")[1]
    assert create(station, "second")[0] == 201
    assert create(station, "third")[0] == 429
    if terminal == "operator_reply":
        assert (
            _request(station[0], station[2], {"action": "reply", "id": int(first["message_id"]), "body": "Yes"})[0]
            == 201
        )
    elif terminal == "desktop":
        assert (
            _request(
                station[0],
                station[1],
                {"action": "question_desktop", "question_id": first["question_id"], "answer": "Yes"},
            )[0]
            == 200
        )
    else:
        with sqlite3.connect(station[3]) as conn:
            conn.execute(
                "UPDATE fleet_questions SET deadline=? WHERE question_id=?",
                ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), first["question_id"]),
            )
    assert create(station, "third")[0] == 201
    assert status(station, first["question_id"])[1]["status"] == ("timeout" if terminal == "timeout" else "answered")
    with sqlite3.connect(station[3]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM fleet_questions WHERE status='pending'").fetchone() == (2,)
        assert conn.execute(
            "SELECT COUNT(*) FROM fleet_messages WHERE kind='operator_question' AND acked_at IS NULL"
        ).fetchone() == (3,)


def test_concurrent_process_stations_share_pending_quota(asking_station):
    station, caller, _enrollment = asking_station
    with ProcessPoolExecutor(max_workers=4, mp_context=get_context("spawn")) as pool:
        attempts = [pool.submit(create, station, f"concurrent-{index}") for index in range(4)]
        results = [attempt.result(timeout=15) for attempt in attempts]
    assert sorted(code for code, _result in results) == [201, 201, 429, 429]
    assert all(result["code"] == "question_station_busy" for code, result in results if code == 429)
    with sqlite3.connect(station[3]) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM fleet_questions WHERE caller=? AND status='pending'", (caller,)
        ).fetchone() == (2,)
        assert conn.execute("SELECT COUNT(*) FROM fleet_messages").fetchone() == (2,)
        assert conn.execute("SELECT COUNT(*) FROM fleet_message_sends").fetchone() == (2,)


@pytest.mark.parametrize("schema_version", [37, fleet_hub.SCHEMA_VERSION])
def test_retention_index_migration_and_current_version_selfheal(hub, schema_version):
    with sqlite3.connect(hub[3]) as conn:
        conn.execute("DROP INDEX IF EXISTS fleet_questions_created_at")
        conn.execute(f"PRAGMA user_version={schema_version}")
    # Both upgrade and already-current repair must be safe on repeated startup.
    fleet_hub.init_db(hub[3]).close()
    fleet_hub.init_db(hub[3]).close()
    with sqlite3.connect(hub[3]) as conn:
        assert conn.execute("PRAGMA user_version").fetchone() == (fleet_hub.SCHEMA_VERSION,)
        assert [row[2] for row in conn.execute("PRAGMA index_info(fleet_questions_created_at)")] == ["created_at"]


def test_direct_peer_message_cannot_answer_question(hub):
    question = create(hub)[1]
    with sqlite3.connect(hub[3]) as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO fleet_messages(sender,recipient,subject,body,priority,client_id,payload_hash,reply_to,created_at,received_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "agent:peer",
                NODE_A,
                "Forged reply",
                "Peer answer",
                "normal",
                "peer-direct",
                "fake-digest",
                int(question["message_id"]),
                now,
                now,
            ),
        )
        assert conn.execute(
            "SELECT status,answer,answer_id FROM fleet_questions WHERE question_id=?", (question["question_id"],)
        ).fetchone() == ("pending", None, None)


@pytest.mark.parametrize("expired_first", [False, True])
def test_late_operator_reply_is_audited_without_becoming_authoritative(hub, expired_first):
    question = create(hub)[1]
    qid = question["question_id"]
    with sqlite3.connect(hub[3]) as conn:
        conn.execute(
            "UPDATE fleet_questions SET deadline=? WHERE question_id=?",
            ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), qid),
        )
    if expired_first:
        assert status(hub, qid)[1]["status"] == "timeout"
    code, reply = _request(
        hub[0], hub[2], {"action": "reply", "id": int(question["message_id"]), "body": "Late operator answer"}
    )
    assert code == 201
    result = status(hub, qid)[1]
    assert result["status"] == "timeout" and result["answer"] is None and result["answer_id"] is None
    assert result["source"] is None and result["answered_by"] is None
    with sqlite3.connect(hub[3]) as conn:
        row = conn.execute(
            "SELECT late_answer_message_id,late_answered_at FROM fleet_questions WHERE question_id=?", (qid,)
        ).fetchone()
        received = conn.execute(
            "SELECT received_at FROM fleet_messages WHERE id=?", (reply["message"]["id"],)
        ).fetchone()[0]
        assert row == (reply["message"]["id"], received)
        assert result["late_answer_message_id"] == str(reply["message"]["id"])
        assert result["late_answered_at"] == received
    assert (
        _request(hub[0], hub[2], {"action": "reply", "id": int(question["message_id"]), "body": "Another late answer"})[
            0
        ]
        == 201
    )
    with sqlite3.connect(hub[3]) as conn:
        assert (
            conn.execute(
                "SELECT late_answer_message_id,late_answered_at FROM fleet_questions WHERE question_id=?", (qid,)
            ).fetchone()
            == row
        )


@pytest.mark.parametrize("version", [42, 43])
def test_schema_43_repairs_missing_late_answer_columns(hub, version):
    question = create(hub)[1]
    with sqlite3.connect(hub[3]) as conn:
        conn.execute("DROP TRIGGER fleet_questions_reply")
        conn.execute("ALTER TABLE fleet_questions DROP COLUMN late_answer_message_id")
        conn.execute("ALTER TABLE fleet_questions DROP COLUMN late_answered_at")
        conn.execute("ALTER TABLE fleet_questions DROP COLUMN operator_reply_after_desktop_message_id")
        conn.execute("ALTER TABLE fleet_questions DROP COLUMN operator_reply_after_desktop_at")
        conn.execute("CREATE TRIGGER fleet_questions_reply AFTER INSERT ON fleet_messages BEGIN SELECT 1; END")
        conn.execute(f"PRAGMA user_version={version}")
    fleet_hub.init_db(hub[3]).close()
    fleet_hub.init_db(hub[3]).close()
    with sqlite3.connect(hub[3]) as conn:
        assert {
            "late_answer_message_id",
            "late_answered_at",
            "operator_reply_after_desktop_message_id",
            "operator_reply_after_desktop_at",
        } <= {r[1] for r in conn.execute("PRAGMA table_info(fleet_questions)")}

    code, reply = _request(
        hub[0], hub[2], {"action": "reply", "id": int(question["message_id"]), "body": "After migration"}
    )
    assert code == 201
    result = status(hub, question["question_id"])[1]
    assert result["answer"] == "After migration" and result["answer_id"] == str(reply["message"]["id"])


@pytest.mark.parametrize("version", [42, 43])
def test_schema_43_migrates_only_audited_question_messages(hub, version):
    question = create(hub)[1]
    code, peer = _request(
        hub[0],
        hub[1],
        {
            "action": "send",
            "to": OPERATOR,
            "subject": "Peer",
            "body": "Peer question",
            "kind": "question",
        },
    )
    assert code == 201
    with sqlite3.connect(hub[3]) as conn:
        conn.execute("UPDATE fleet_messages SET kind='question' WHERE id=?", (question["message_id"],))
        conn.execute(f"PRAGMA user_version={version}")
    fleet_hub.init_db(hub[3]).close()
    fleet_hub.init_db(hub[3]).close()
    code, inbox = _request(hub[0], hub[2], {"action": "list"})
    assert code == 200
    assert {row["id"]: row["kind"] for row in inbox["messages"]} == {
        int(question["message_id"]): "operator_question",
        peer["message"]["id"]: "question",
    }
    assert status(hub, question["question_id"])[1]["status"] == "pending"


def test_first_operator_reply_after_desktop_is_immutable_audit_only(hub, monkeypatch):
    from brigade import fleet_hub_questions

    started = datetime.now(timezone.utc)

    class Clock(datetime):
        current = started

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(fleet_hub_questions, "datetime", Clock)
    monkeypatch.setattr(fleet_hub_messages, "datetime", Clock)
    question = create(hub)[1]
    Clock.current = started + timedelta(seconds=5)
    code, accepted = _request(
        hub[0],
        hub[1],
        {
            "action": "question_desktop",
            "question_id": question["question_id"],
            "answer": "yes",
        },
    )
    assert code == 200 and accepted["answer"] == "yes"
    Clock.current = started + timedelta(seconds=10)
    code, reply = _request(
        hub[0],
        hub[2],
        {
            "action": "reply",
            "id": int(question["message_id"]),
            "body": "no",
        },
    )
    assert code == 201
    result = status(hub, question["question_id"])[1]
    assert result["operator_reply_after_desktop_message_id"] == str(reply["message"]["id"])
    assert result["operator_reply_after_desktop_at"] == Clock.current.isoformat()
    assert {key: result[key] for key in accepted if not key.startswith("operator_reply_after_desktop_")} == {
        key: value for key, value in accepted.items() if not key.startswith("operator_reply_after_desktop_")
    }
    Clock.current = started + timedelta(seconds=70)
    assert (
        _request(
            hub[0],
            hub[2],
            {
                "action": "reply",
                "id": int(question["message_id"]),
                "body": "another reply",
            },
        )[0]
        == 201
    )
    assert status(hub, question["question_id"])[1] == result
    with sqlite3.connect(hub[3]) as conn:
        assert conn.execute(
            "SELECT answer,answer_id,source,answered_at,late_answer_message_id FROM fleet_questions"
        ).fetchone() == (
            "yes",
            None,
            "caller_reported_desktop",
            (started + timedelta(seconds=5)).isoformat(),
            None,
        )


def test_schema_repairs_stale_trigger_that_contains_late_answer_column(hub):
    question = create(hub)[1]
    with sqlite3.connect(hub[3]) as conn:
        conn.execute("DROP TRIGGER fleet_questions_reply")
        conn.execute(
            "CREATE TRIGGER fleet_questions_reply AFTER INSERT ON fleet_messages BEGIN SELECT late_answer_message_id FROM fleet_questions; END"
        )
    fleet_hub.init_db(hub[3]).close()
    fleet_hub.init_db(hub[3]).close()
    code, reply = _request(
        hub[0],
        hub[2],
        {
            "action": "reply",
            "id": int(question["message_id"]),
            "body": "repaired",
        },
    )
    assert code == 201
    result = status(hub, question["question_id"])[1]
    assert result["answer"] == "repaired" and result["answer_id"] == str(reply["message"]["id"])


def test_operator_cannot_be_a_node_with_agent_prefix(hub, monkeypatch):
    operator = "agent:node-operator"
    with sqlite3.connect(hub[3]) as conn:
        conn.execute("UPDATE nodes SET node_id=? WHERE node_id=?", (operator, NODE_B))
    monkeypatch.setenv("BRIGADE_FLEET_OPERATOR_RECIPIENT", operator)
    code, result = create(hub)
    assert code == 403 and result["code"] == "question_operator_invalid"


def test_operator_question_message_created_event_matches_catalog(hub):
    from brigade import fleet_hub_events

    question = create(hub)[1]
    code, inbox = _request(hub[0], hub[2], {"action": "list"})
    assert code == 200
    message = next(row for row in inbox["messages"] if str(row["id"]) == question["message_id"])
    event = next(spec for spec in fleet_hub_events.definitions() if spec["name"] == fleet_hub_events.MESSAGE_CREATED)
    assert message["kind"] == "operator_question"
    assert message["kind"] in event["payloadSchema"]["properties"]["kind"]["enum"]
