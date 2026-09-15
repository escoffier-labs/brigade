"""Read-only display projection tests for active Grok Bot jobs."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from brigade import cli, fleet_claim_display, fleet_client, fleet_hub, fleet_hub_grokbot


NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _insert_job(
    conn,
    job_id: str,
    *,
    state: str,
    label: str = "Issue #1502",
    repository: str = "example/brigade",
    lease_expires_at: str | None = None,
    claimant_node: str | None = "worker-node",
    claimant_worker: str | None = "implementation-worker",
) -> None:
    stamp = NOW.isoformat()
    conn.execute(
        "INSERT INTO grokbot_jobs ("
        "job_id, role, repository, label, task_digest, idempotency_key_hash, state, item_revision, sequence, "
        "created_at, updated_at, queued_at, timeout_seconds, artifact_kind, owner_node, claimed_at, "
        "lease_expires_at, claimant_node, claimant_worker"
        ") VALUES (?, 'implementation-worker', ?, ?, ?, ?, ?, 1, 1, ?, ?, ?, 900, 'draft-pr', "
        "'queue-owner', ?, ?, ?, ?)",
        (
            job_id,
            repository,
            label,
            "a" * 64,
            job_id[-24:].ljust(64, "b"),
            state,
            stamp,
            stamp,
            stamp,
            stamp,
            lease_expires_at,
            claimant_node,
            claimant_worker,
        ),
    )
    conn.commit()


def _conn(tmp_path):
    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    fleet_hub_grokbot.ensure_schema(conn)
    return conn


def test_projects_active_jobs_and_keeps_repeated_labels_distinct(tmp_path):
    conn = _conn(tmp_path)
    try:
        deadline = (NOW + timedelta(minutes=5)).isoformat()
        first = "grokbot-" + "1" * 24
        second = "grokbot-" + "2" * 24
        _insert_job(conn, first, state="claimed", lease_expires_at=deadline)
        _insert_job(
            conn,
            second,
            state="running",
            lease_expires_at=deadline,
            claimant_node=None,
            claimant_worker=None,
        )

        changes_before = conn.total_changes
        rows = fleet_claim_display.list_display_claims(conn, now=NOW)

        assert conn.total_changes == changes_before
        assert [row["job"] for row in rows] == [first, second]
        assert rows[0]["target"] == f"example/brigade · Issue #1502 [{first}]"
        assert rows[0]["owner_node"] == "worker-node"
        assert rows[0]["owner_conductor"] == "implementation-worker"
        assert rows[0]["acquired_at"] == NOW.isoformat()
        assert rows[0]["expires_at"] == deadline
        assert rows[1]["owner_node"] == "queue-owner"
        assert rows[1]["owner_conductor"] == "grokbot"
        assert len({row["target"] for row in rows}) == 2
        assert all(row["harness"] == "grokbot" and row["role"] == "implementation-worker" for row in rows)
        forbidden = {"lease_token_digest", "task_digest", "private_snapshot_id", "idempotency_key_hash"}
        assert all(not forbidden.intersection(row) for row in rows)
    finally:
        conn.close()


def test_label_falls_back_to_job_id_when_stored_label_is_not_displayable(tmp_path):
    conn = _conn(tmp_path)
    try:
        job_id = "grokbot-" + "7" * 24
        _insert_job(
            conn,
            job_id,
            state="claimed",
            label="   ",
            lease_expires_at=(NOW + timedelta(minutes=5)).isoformat(),
        )

        row = fleet_claim_display.list_display_claims(conn, now=NOW)[0]

        assert row["target"] == f"example/brigade · {job_id}"
    finally:
        conn.close()


def test_missing_grokbot_table_keeps_ordinary_claim_projection_compatible(tmp_path):
    conn = _conn(tmp_path)
    try:
        status, _payload = fleet_hub.handle_claim(
            conn,
            {"action": "acquire", "target": "ordinary-repo", "node_id": "ordinary-node", "holder": "holder"},
        )
        assert status == 200
        conn.execute("DROP TABLE grokbot_jobs")
        conn.commit()

        assert fleet_claim_display.list_display_claims(conn, now=NOW) == fleet_hub.list_claims(conn)
    finally:
        conn.close()


def test_cli_claims_json_prints_sqlite_projection(monkeypatch, capsys, tmp_path):
    conn = _conn(tmp_path)
    try:
        job_id = "grokbot-" + "c" * 24
        _insert_job(
            conn,
            job_id,
            state="running",
            lease_expires_at=(NOW + timedelta(minutes=5)).isoformat(),
        )
        rows = fleet_claim_display.list_display_claims(conn, now=NOW)
    finally:
        conn.close()

    monkeypatch.setattr(fleet_client, "fetch_claims", lambda *, include_all=False: rows)

    assert cli.main(["fleet", "claims", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["claims"] == rows
    assert payload["claims"][0]["target"].endswith(f"[{job_id}]")


def test_excludes_queued_and_terminal_jobs_even_with_include_all(tmp_path):
    conn = _conn(tmp_path)
    try:
        deadline = (NOW + timedelta(minutes=5)).isoformat()
        for index, state in enumerate(("queued", "completed", "failed", "expired", "canceled"), start=3):
            _insert_job(conn, "grokbot-" + str(index) * 24, state=state, lease_expires_at=deadline)

        assert fleet_claim_display.list_display_claims(conn, now=NOW) == []
        assert fleet_claim_display.list_display_claims(conn, include_all=True, now=NOW) == []
    finally:
        conn.close()


def test_expired_and_malformed_leases_require_include_all(tmp_path):
    conn = _conn(tmp_path)
    try:
        expired = "grokbot-" + "8" * 24
        malformed = "grokbot-" + "9" * 24
        _insert_job(conn, expired, state="claimed", lease_expires_at=(NOW - timedelta(seconds=1)).isoformat())
        _insert_job(conn, malformed, state="running", lease_expires_at="not-a-timestamp")

        assert fleet_claim_display.list_display_claims(conn, now=NOW) == []
        rows = fleet_claim_display.list_display_claims(conn, include_all=True, now=NOW)
        assert {row["job"] for row in rows} == {expired, malformed}
        assert all(row["expired"] for row in rows)
        assert next(row for row in rows if row["job"] == malformed)["expires_at"] == ""
    finally:
        conn.close()


def test_preserves_ordinary_claims_without_changing_core_occupancy(tmp_path):
    conn = _conn(tmp_path)
    try:
        status, _payload = fleet_hub.handle_claim(
            conn,
            {"action": "acquire", "target": "ordinary-repo", "node_id": "ordinary-node", "holder": "holder"},
        )
        assert status == 200
        _insert_job(
            conn,
            "grokbot-" + "f" * 24,
            state="running",
            lease_expires_at=(NOW + timedelta(minutes=5)).isoformat(),
        )

        core_claims = fleet_hub.list_claims(conn)
        display_claims = fleet_claim_display.list_display_claims(conn, now=NOW)

        assert core_claims == fleet_hub.list_claims(conn)
        assert [row["target"] for row in core_claims] == ["ordinary-repo"]
        assert display_claims[0] == core_claims[0]
        assert display_claims[1]["job"] == "grokbot-" + "f" * 24
    finally:
        conn.close()
