"""Hub jobs must expire on their own ``timeout_seconds``, not only on lease loss.

Issue #1353: seven of ten Grok Bot jobs sat queued long past their declared
timeout because nothing but an operator ``expire`` call ever terminalized a
row. These tests pin the deadline to the read paths, to the claim refusal, and
to a sweep that needs no request at all.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from brigade import fleet_hub, fleet_hub_grokbot


FEED_NODE = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
WORKER_NODE = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
OPERATOR_NODE = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
QUEUE_ID = "grokbot-queue-main"
JOB_ID = "grokbot-" + "a" * 24
TIMEOUT_SECONDS = 900


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    _enroll(connection)
    yield connection
    connection.close()


def _enroll(conn: sqlite3.Connection) -> None:
    for node, kind, role in (
        (FEED_NODE, "feed", None),
        (WORKER_NODE, "implementation-worker", "implementation-worker"),
        (OPERATOR_NODE, "operator", None),
    ):
        body = {
            "action": "enroll-actor",
            "enroll_node_id": node,
            "queue_owner_node_id": FEED_NODE,
            "queue_id": QUEUE_ID,
            "actor_kind": kind,
            "enabled": True,
        }
        if role is not None:
            body["role"] = role
        status, payload = fleet_hub_grokbot.handle_grokbot(conn, body, caller_node=None)
        assert status == 200, payload


def _enqueue(conn: sqlite3.Connection, job_id: str = JOB_ID) -> dict[str, object]:
    digest = "b" * 64
    body = {
        "action": "enqueue",
        "job_id": job_id,
        "role": "implementation-worker",
        "repository": "example/brigade",
        "label": "safe label",
        "task_digest": digest,
        "idempotency_key_hash": digest,
        "timeout_seconds": TIMEOUT_SECONDS,
        "artifact_kind": "draft-pr",
        "operation_id": f"op-enqueue-{job_id[-4:]}",
    }
    status, payload = fleet_hub_grokbot.handle_grokbot(conn, body, caller_node=FEED_NODE)
    assert status == 200, payload
    return payload["job"]


def _claim_body(job_id: str = JOB_ID, *, revision: int = 1) -> dict[str, object]:
    return {
        "action": "claim",
        "job_id": job_id,
        "lease_id": "lease-a",
        "expected_item_revision": revision,
        "lease_seconds": 300,
        "operation_id": "op-claim-1",
    }


def _backdate(conn: sqlite3.Connection, job_id: str = JOB_ID, *, seconds: int = TIMEOUT_SECONDS + 60) -> None:
    """Move the job's own clock past its deadline without touching the code's."""
    queued_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
    stamp = queued_at.isoformat().replace("+00:00", "Z")
    conn.execute(
        "UPDATE grokbot_jobs SET queued_at=?, created_at=?, updated_at=? WHERE job_id=?",
        (stamp, stamp, stamp, job_id),
    )
    conn.commit()


def _operations(conn: sqlite3.Connection, job_id: str = JOB_ID) -> list[tuple[str, str, str]]:
    return [
        (row[0], row[1], row[2])
        for row in conn.execute(
            "SELECT action, operation_id, result_state FROM grokbot_operations WHERE job_id=?", (job_id,)
        )
    ]


def _state(conn: sqlite3.Connection, job_id: str = JOB_ID) -> str:
    row = conn.execute("SELECT state FROM grokbot_jobs WHERE job_id=?", (job_id,)).fetchone()
    assert row is not None
    return str(row[0])


def test_status_expires_a_queued_job_past_its_deadline(conn):
    _enqueue(conn)
    _backdate(conn)

    status, payload = fleet_hub_grokbot.handle_grokbot(
        conn, {"action": "status", "job_id": JOB_ID}, caller_node=OPERATOR_NODE
    )

    assert status == 200
    assert payload["job"]["state"] == "expired"
    assert _state(conn) == "expired"
    assert [op for op in _operations(conn) if op[0] == "expire"], _operations(conn)


def test_list_expires_a_queued_job_and_records_an_expire_operation(conn):
    _enqueue(conn)
    _backdate(conn)

    status, payload = fleet_hub_grokbot.handle_grokbot(conn, {"action": "list"}, caller_node=OPERATOR_NODE)

    assert status == 200
    assert payload["jobs"] == []  # terminal jobs leave the active list
    assert _state(conn) == "expired"
    expires = [op for op in _operations(conn) if op[0] == "expire"]
    assert len(expires) == 1
    assert expires[0][1].startswith(fleet_hub_grokbot.SWEEP_OPERATION_PREFIX)
    assert expires[0][2] == "expired"


def test_claim_of_a_job_past_its_deadline_is_refused_as_job_expired(conn):
    _enqueue(conn)
    _backdate(conn)

    status, payload = fleet_hub_grokbot.handle_grokbot(conn, _claim_body(), caller_node=WORKER_NODE)

    assert status == 409
    assert payload == {"claimed": False, "error": "job-expired"}
    assert _state(conn) == "expired"


def test_renew_past_the_deadline_expires_the_running_job(conn):
    _enqueue(conn)
    claimed = fleet_hub_grokbot.handle_grokbot(conn, _claim_body(), caller_node=WORKER_NODE)
    assert claimed[0] == 200, claimed[1]
    generation = claimed[1]["lease_generation"]
    revision = claimed[1]["job"]["item_revision"]
    _backdate(conn)

    status, payload = fleet_hub_grokbot.handle_grokbot(
        conn,
        {
            "action": "renew",
            "job_id": JOB_ID,
            "lease_id": "lease-a",
            "expected_item_revision": revision,
            "lease_generation": generation,
            "lease_seconds": 300,
            "operation_id": "op-renew-1",
        },
        caller_node=WORKER_NODE,
    )

    assert status == 409
    assert payload["renewed"] is False
    assert _state(conn) == "expired"
    assert [op for op in _operations(conn) if op[0] == "expire"], _operations(conn)


def test_sweep_expires_a_queued_job_with_no_request_at_all(conn):
    _enqueue(conn)
    _backdate(conn)

    assert fleet_hub_grokbot.sweep_expired_jobs(conn) == [JOB_ID]
    assert _state(conn) == "expired"
    # A second pass is a no-op: terminal jobs are never swept twice.
    assert fleet_hub_grokbot.sweep_expired_jobs(conn) == []
    assert len([op for op in _operations(conn) if op[0] == "expire"]) == 1


def test_sweep_leaves_a_job_inside_its_timeout_alone(conn):
    _enqueue(conn)

    assert fleet_hub_grokbot.sweep_expired_jobs(conn) == []
    assert _state(conn) == "queued"


def test_sweep_reuses_an_existing_transaction(conn):
    """A caller already inside a transaction must not get a nested-tx error."""
    _enqueue(conn)
    _backdate(conn)

    conn.execute("BEGIN IMMEDIATE")
    try:
        assert fleet_hub_grokbot.sweep_expired_jobs(conn) == [JOB_ID]
        assert _state(conn) == "expired"
    finally:
        conn.commit()


def test_periodic_sweeper_thread_expires_without_any_hub_traffic(tmp_path):
    db_path = tmp_path / "fleet.db"
    connection = fleet_hub.init_db(db_path)
    try:
        _enroll(connection)
        _enqueue(connection)
        _backdate(connection)
    finally:
        connection.close()

    stop = threading.Event()
    thread = fleet_hub_grokbot.start_expiry_sweeper(db_path, interval=0.01, stop=stop)
    try:
        state = "queued"
        deadline = datetime.now(timezone.utc) + timedelta(seconds=5)
        while datetime.now(timezone.utc) < deadline:
            probe = fleet_hub.open_db(db_path)
            try:
                state = _state(probe)
            finally:
                probe.close()
            if state == "expired":
                break
        assert state == "expired"
    finally:
        stop.set()
        thread.join(timeout=5)
    assert not thread.is_alive()


def test_operator_expire_still_records_exactly_one_operation(conn):
    _enqueue(conn)
    _backdate(conn)

    status, payload = fleet_hub_grokbot.handle_grokbot(
        conn,
        {"action": "expire", "job_id": JOB_ID, "expected_item_revision": 1, "operation_id": "op-expire-1"},
        caller_node=OPERATOR_NODE,
    )

    # The sweep skips the very job an operator is expiring, so the operator's
    # own operation id is the only audit row: no duplicate deadline row.
    assert status == 200
    assert payload["job"]["state"] == "expired"
    assert [op[1] for op in _operations(conn) if op[0] == "expire"] == ["op-expire-1"]
