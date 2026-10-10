"""Offline contracts for explicit Dot metadata and existing session persistence."""

import json
import sqlite3

import pytest

from brigade import fleet_client, fleet_hub, fleet_hub_sessions

NODE = "22222222-2222-4222-8222-222222222222"
OTHER = "33333333-3333-4333-8333-333333333333"
STAMP = "2026-01-01T00:00:00Z"
NOW = 1767225600.0


def report(**updates):
    return {
        "version": 1,
        "session_id": "session-a",
        "agent_label": "worker-a",
        "observed_at": STAMP,
        "sequence": 1,
        **updates,
    }


def test_dry_run_projects_without_clients_or_checkout(monkeypatch):
    from brigade import fleet_dot

    def forbidden(*args, **kwargs):
        pytest.fail("dry run loaded authentication or called network")

    monkeypatch.setattr(fleet_client, "load_fleet_settings", forbidden)
    monkeypatch.setattr(fleet_client, "publish_session", forbidden)
    monkeypatch.setattr(fleet_client, "resolve_node_id", forbidden)
    result = fleet_dot.report_session(
        report(progress="Checking fixture", evidence_refs=["https://example.com/check/1"])
    )
    assert result["inventory_coverage"] == "explicitly-reported-sessions"
    assert result["provider_lifecycle"] == "unobserved"
    assert result["presence_payload"]["checkout_path"] is None
    assert result["presence_payload"]["dirty_paths"] == []
    assert "progress" not in result["presence_payload"]
    assert result["summaries_persisted"] is False
    assert result["evidence_verification"] == "reported-unverified"


def cloud_body(**updates):
    from brigade import fleet_dot

    return fleet_dot.report_session(report(**updates))["presence_payload"]


def test_cloud_linkage_replay_and_staleness(monkeypatch):
    conn = sqlite3.connect(":memory:")
    fleet_hub_sessions.init_schema(conn)
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW)
    body = cloud_body(parent_session_id="parent-a")
    _, first = fleet_hub_sessions.handle_session(conn, body, caller_node=NODE)
    assert first["session"]["checkout_path"] is None
    assert first["session"]["cloud_context"]["parent_session_id"] == "parent-a"
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW + 100)
    _, replay = fleet_hub_sessions.handle_session(conn, body, caller_node=NODE)
    assert replay == first
    for changed in (
        cloud_body(parent_session_id="parent-b", sequence=2),
        cloud_body(agent_label="worker-b", sequence=2),
        cloud_body(parent_session_id="parent-a", sequence=0),
        cloud_body(parent_session_id="parent-a", sequence=1, observed_at="2026-01-01T00:00:01Z"),
        cloud_body(parent_session_id="parent-a", sequence=2, repo_identity="github.com/example/project"),
    ):
        with pytest.raises(fleet_hub.FleetHubError):
            fleet_hub_sessions.handle_session(conn, changed, caller_node=NODE)
    assert fleet_hub_sessions.list_sessions(conn, now_epoch=NOW + 901) == []
    history = fleet_hub_sessions.list_sessions(conn, include_all=True, now_epoch=NOW + 901)
    assert history[0]["presence_state"] == "stale"
    assert history[0]["provider_lifecycle"] == "unobserved"
    # Another enrolled reporter does not refresh or replace this node's row.
    fleet_hub_sessions.handle_session(conn, body, caller_node=OTHER)
    assert len(fleet_hub_sessions.list_sessions(conn, include_all=True)) == 2
    with pytest.raises(fleet_hub.FleetHubError):
        fleet_hub_sessions.handle_session(conn, {**body, "node_id": NODE}, caller_node=OTHER)


@pytest.mark.parametrize(
    "updates",
    [
        {"instructions": "reject"},
        {"progress": "ghp_" + "a" * 20},
        {"result": "result=(/etc/private-file)"},
        {"progress": "path:/tmp/private-file"},
        {"evidence_refs": ["https://127.0.0.1/check"]},
        {"evidence_refs": ["https://192.0.2.1/check"]},
        {"evidence_refs": ["https://localhost/check"]},
        {"evidence_refs": ["https://[::1]/check"]},
        {"evidence_refs": ["https://example.com/check?token=short"]},
        {"evidence_refs": ["https://user@example.com/check"]},
        {"progress": "token=short"},
        {"progress": "x\u2028private"},
        {"progress": "x" * 401},
        {"evidence_refs": ["ref"] * 9},
        {"source": "cloud_threads", "source_scope": "account-wide"},
        {"session_id": "a\u0001b"},
        {"observed_at": "2026-01-01T00:00:00"},
    ],
)
def test_invalid_reports_fail_before_any_clients(updates, monkeypatch):
    from brigade import fleet_dot

    def forbidden(*args, **kwargs):
        pytest.fail("invalid metadata reached auth or client")

    monkeypatch.setattr(fleet_client, "load_fleet_settings", forbidden)
    monkeypatch.setattr(fleet_client, "publish_session", forbidden)
    with pytest.raises(fleet_dot.DotReportError):
        fleet_dot.report_session(report(**updates), publish=True)


def test_parent_cycles_are_refused_even_for_initially_unresolved_parent(monkeypatch):
    conn = sqlite3.connect(":memory:")
    fleet_hub_sessions.init_schema(conn)
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW)
    a = cloud_body(parent_session_id="session-b")
    fleet_hub_sessions.handle_session(conn, a, caller_node=NODE)
    b = cloud_body(session_id="session-b", parent_session_id="session-a")
    with pytest.raises(fleet_hub.FleetHubError, match="cycle"):
        fleet_hub_sessions.handle_session(conn, b, caller_node=NODE)
    assert len(fleet_hub_sessions.list_sessions(conn, include_all=True)) == 1
    # Parent references are reporter-scoped and never cross to this other node.
    fleet_hub_sessions.handle_session(conn, b, caller_node=OTHER)
    assert len(fleet_hub_sessions.list_sessions(conn, include_all=True)) == 2


def test_schema23_migration_preserves_old_snapshots_and_refuses_downgrade(tmp_path, monkeypatch):
    from brigade.fleet_session_presence import SessionSnapshot

    path = tmp_path / "hub.db"
    conn = fleet_hub.init_db(path)
    local = SessionSnapshot(
        "claude", "local-a", "github.com/example/project", "fleet", "project", "/tmp/fixture", "main", (), False
    )
    legacy_body = fleet_client._session_request_body(local, action="upsert")
    assert "cloud_context" not in legacy_body
    fleet_hub_sessions.handle_session(conn, legacy_body, caller_node=NODE)
    before = fleet_hub_sessions.list_sessions(conn, include_all=True)
    conn.execute("ALTER TABLE interactive_sessions DROP COLUMN cloud_context_json")
    conn.execute("PRAGMA user_version=23")
    conn.commit()
    conn.close()
    conn = fleet_hub.init_db(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 25
    assert fleet_hub_sessions.list_sessions(conn, include_all=True) == before
    fleet_hub_sessions.handle_session(conn, legacy_body, caller_node=NODE)
    fleet_hub_sessions.init_schema(conn)  # Mixed local writes and idempotent additive migration.
    conn.close()
    monkeypatch.setattr(fleet_hub, "SCHEMA_VERSION", 23)
    with pytest.raises(fleet_hub.FleetHubError, match="schema version 25"):
        fleet_hub.init_db(path)


def test_parent_traversal_is_bounded_and_source_scope_cannot_change(monkeypatch):
    conn = sqlite3.connect(":memory:")
    fleet_hub_sessions.init_schema(conn)
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW)
    # References may arrive before their parents, but the traversal is bounded.
    for index in range(65):
        fleet_hub_sessions.handle_session(
            conn, cloud_body(session_id=f"s-{index}", parent_session_id=f"s-{index + 1}"), caller_node=NODE
        )
    with pytest.raises(fleet_hub.FleetHubError, match="bound"):
        fleet_hub_sessions.handle_session(
            conn, cloud_body(session_id="new-root", parent_session_id="s-0"), caller_node=NODE
        )
    original = cloud_body(source="cloud_threads", source_scope="caller-created-tasks")
    fleet_hub_sessions.handle_session(conn, original, caller_node=NODE)
    changed = cloud_body(source="cloud_threads", source_scope="authorized-visible-threads", sequence=2)
    with pytest.raises(fleet_hub.FleetHubError, match="linkage"):
        fleet_hub_sessions.handle_session(conn, changed, caller_node=NODE)


def test_report_decoding_refuses_duplicates_and_size_without_truncating():
    from brigade import fleet_dot

    with pytest.raises(fleet_dot.DotReportError):
        fleet_dot.read_report(b'{"version":1,"version":1}')
    with pytest.raises(fleet_dot.DotReportError):
        fleet_dot.read_report(b" " * (fleet_dot.MAX_REPORT_BYTES + 1))
    exact = fleet_dot.parse_report(report(progress="x" * 400, evidence_refs=[f"ref-{i}" for i in range(8)]))
    assert len(exact.summaries["progress"]) == 400 and len(exact.evidence_refs) == 8


def test_cli_stdin_and_file_default_to_no_auth(tmp_path, monkeypatch, capsys):
    import argparse
    import io

    from brigade.cli import fleet_dot

    raw = json.dumps(report()).encode()
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: pytest.fail("dry-run loaded auth"))
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(raw)))
    args = argparse.Namespace(metadata_file=None, publish=False, holder_file=tmp_path / "missing-holder")
    assert fleet_dot.dispatch(args) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "dry-run"
    metadata = tmp_path / "metadata.json"
    metadata.write_bytes(raw)
    args.metadata_file = metadata
    assert fleet_dot.dispatch(args) == 0
    capsys.readouterr()
    metadata.write_text('{"version":1,"prompt":"never read"}')
    assert fleet_dot.dispatch(args) == 2
    assert "prompt" not in capsys.readouterr().out


@pytest.fixture
def report_transport(tmp_path, monkeypatch):
    """Real protocol/store boundaries with offline HTTP descriptors, no sockets."""
    from contextlib import contextmanager

    from brigade import worklore_client, worklore_http, worklore_ownership, worklore_store
    from brigade.fleet_session_presence import SessionSnapshot

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    fleet_hub.add_node(conn, NODE, "fixture-node")
    work_id = worklore_store.create_item(conn, {"title": "Fixture", "kind": "repo"}, actor_id="admin")["work_id"]
    nonce = worklore_client.new_ownership_nonce()
    worklore_ownership.ownership_action(
        conn,
        work_id,
        {
            "action": "offer",
            "target_node": NODE,
            "exclusions": [],
            "authorization_ref": "grant-a",
            "attempt_budget": {"cap": 1, "source_ref": "budget-a"},
        },
        expected_revision=0,
        idempotency_key="offer",
        actor_id="admin",
        actor_type="admin",
        is_admin=True,
    )
    worklore_ownership.ownership_action(
        conn,
        work_id,
        {"action": "accept", "generation": 0},
        expected_revision=1,
        idempotency_key="accept",
        actor_id=NODE,
        actor_type="node",
        holder_nonce=nonce,
    )
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW)
    settings = {"hub_url": "https://example.com", "node_token": "fixture-node-token", "admin_token": ""}
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: settings)
    monkeypatch.setattr(worklore_client, "load_fleet_settings", lambda: settings)
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE)

    def presence(snapshot: SessionSnapshot):
        fleet_hub_sessions.handle_session(
            conn, fleet_client._session_request_body(snapshot, action="upsert"), caller_node=NODE
        )
        return fleet_client.SessionWriteResult(ok=True)

    @contextmanager
    def offline_open(request, **kwargs):
        import io
        from urllib.parse import urlsplit

        headers = dict(request.header_items())
        assert headers["Authorization"] == "Bearer fixture-node-token"
        # node-only principal, deliberately without operator authorization.
        status, payload = worklore_http.handle(
            conn,
            worklore_http.Request(
                method=request.get_method(),
                path=urlsplit(request.full_url).path,
                node_id=NODE,
                is_admin=False,
                is_operator=False,
                operator_authorization_resolved=True,
                headers=headers,
                body=json.loads(request.data),
            ),
        )
        if status != 200:
            raise worklore_client.WorkloreClientError("fixture refusal", code=payload["code"])
        yield io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(fleet_client, "publish_session", presence)
    monkeypatch.setattr(worklore_client, "_hub_open", offline_open)
    yield conn, work_id, nonce
    conn.close()


def test_adapter_replay_200_does_not_duplicate_work_and_conflicts_on_changed_summary(report_transport):
    from brigade import fleet_dot, worklore_store

    conn, work_id, nonce = report_transport
    raw = report(
        work_id=work_id,
        ownership_revision=2,
        generation=1,
        progress="Check fixture",
        source="cloud_threads",
        source_scope="authorized-visible-threads",
    )
    before = worklore_store.get_item(conn, work_id)
    first = fleet_dot.report_session(raw, publish=True, holder_nonce=nonce)
    assert first["components"] == {"presence": "published", "work": "reported"}
    assert first["summaries_persisted"] is True
    assert fleet_dot.report_session(raw, publish=True, holder_nonce=nonce)["components"] == first["components"]
    assert conn.execute("SELECT COUNT(*) FROM work_events WHERE event_type='ownership-reported'").fetchone()[0] == 1
    conflict = fleet_dot.report_session({**raw, "progress": "Different"}, publish=True, holder_nonce=nonce)
    assert conflict["components"] == {"presence": "published", "work": "failed:idempotency-conflict"}
    assert conflict["summaries_persisted"] is False
    assert worklore_store.get_item(conn, work_id) == before
    assert nonce not in json.dumps(first)


def test_adapter_partial_publication_missing_holder_and_stale_ownership(report_transport):
    from brigade import fleet_dot

    conn, work_id, nonce = report_transport
    raw = report(work_id=work_id, ownership_revision=2, generation=1, blocker="Waiting for review")
    missing = fleet_dot.report_session(raw, publish=True)
    assert missing["components"] == {"presence": "published", "work": "failed:holder-capability-required"}
    assert missing["summaries_persisted"] is False
    stale = fleet_dot.report_session({**raw, "ownership_revision": 1}, publish=True, holder_nonce=nonce)
    assert stale["components"] == {"presence": "published", "work": "failed:version-conflict"}
    assert conn.execute("SELECT COUNT(*) FROM work_events WHERE event_type='ownership-reported'").fetchone()[0] == 0
    # Echoed runtime capability is refused before any publication.
    with pytest.raises(fleet_dot.DotReportError):
        fleet_dot.report_session({**raw, "progress": nonce}, publish=True, holder_nonce=nonce)


def test_presence_failure_stops_work_and_admin_credentials_are_not_authority(monkeypatch):
    from brigade import fleet_dot, worklore_client

    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE)
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: {"node_token": "", "admin_token": "fixture-admin"})
    monkeypatch.setattr(fleet_client, "publish_session", lambda *a: pytest.fail("admin token used for cloud presence"))
    raw = report(work_id="work-a", ownership_revision=2, generation=1)
    assert fleet_dot.report_session(raw, publish=True)["components"] == {
        "presence": "failed:node_auth_required",
        "work": "not-attempted",
    }
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: {"node_token": "fixture-node"})
    monkeypatch.setattr(
        fleet_client, "publish_session", lambda *a: fleet_client.SessionWriteResult(ok=False, reason="http_status:400")
    )
    monkeypatch.setattr(
        worklore_client, "report_ownership", lambda *a, **k: pytest.fail("work attempted after refused presence")
    )
    assert fleet_dot.report_session(raw, publish=True)["components"] == {
        "presence": "failed:http_status:400",
        "work": "not-attempted",
    }


@pytest.mark.parametrize("field", ["progress", "result"])
def test_percent_encoded_holder_and_bearer_headers_are_refused_before_presence(report_transport, monkeypatch, field):
    from brigade import fleet_dot

    _, work_id, nonce = report_transport
    monkeypatch.setattr(fleet_client, "publish_session", lambda *a: pytest.fail("secret metadata reached presence"))
    for text in ("".join(f"%{ord(char):02X}" for char in nonce), "Bearer short", "Authorization: Bearer short"):
        raw = report(work_id=work_id, ownership_revision=2, generation=1, **{field: text})
        with pytest.raises(fleet_dot.DotReportError) as failure:
            fleet_dot.report_session(raw, publish=True, holder_nonce=nonce)
        assert nonce not in str(failure.value) and text not in str(failure.value)


def test_late_observation_does_not_refresh_ttl_or_end_provider(monkeypatch):
    conn = sqlite3.connect(":memory:")
    fleet_hub_sessions.init_schema(conn)
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW)
    body = cloud_body(repo_identity="github.com/example/project")
    fleet_hub_sessions.handle_session(conn, body, caller_node=NODE)
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW + 1000)
    _, refreshed = fleet_hub_sessions.handle_session(
        conn, cloud_body(repo_identity="github.com/example/project", sequence=2), caller_node=NODE
    )
    assert refreshed["session"]["expires_at"] == NOW + 900
    assert refreshed["session"]["presence_state"] == "stale"
    end = {key: value for key, value in body.items() if key in {"harness", "session_id", "repo_identity"}}
    with pytest.raises(fleet_hub.FleetHubError, match="unobserved"):
        fleet_hub_sessions.handle_session(conn, {**end, "action": "end"}, caller_node=NODE)


@pytest.mark.parametrize(
    "changed", [{"owner_node": OTHER}, {"generation": 2}, {"state": "unowned"}, {"last_report": {"progress": "fake"}}]
)
def test_work_response_must_confirm_bound_report_authority(report_transport, monkeypatch, changed):
    from brigade import fleet_dot, worklore_client

    _, work_id, nonce = report_transport
    original = worklore_client.report_ownership

    def altered(*args, **kwargs):
        result = original(*args, **kwargs)
        return {"ownership": {**result["ownership"], **changed}}

    monkeypatch.setattr(worklore_client, "report_ownership", altered)
    result = fleet_dot.report_session(
        report(work_id=work_id, ownership_revision=2, generation=1), publish=True, holder_nonce=nonce
    )
    assert result["components"] == {"presence": "published", "work": "unknown:invalid-report-response"}
    assert result["summaries_persisted"] is False
