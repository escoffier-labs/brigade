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
    assert conn.execute("PRAGMA user_version").fetchone()[0] == fleet_hub.SCHEMA_VERSION
    assert fleet_hub_sessions.list_sessions(conn, include_all=True) == before
    fleet_hub_sessions.handle_session(conn, legacy_body, caller_node=NODE)
    fleet_hub_sessions.init_schema(conn)  # Mixed local writes and idempotent additive migration.
    conn.close()
    current_version = fleet_hub.SCHEMA_VERSION
    monkeypatch.setattr(fleet_hub, "SCHEMA_VERSION", 23)
    with pytest.raises(fleet_hub.FleetHubError, match=f"schema version {current_version}"):
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


def v2_report(**updates):
    return report(
        **{
            "version": 2,
            "task": "Check build",
            "reported_state": "running",
            "source": "explicit-metadata",
            "source_scope": "explicit-session",
            **updates,
        }
    )


def test_v2_stores_full_report_replaces_dynamic_fields_and_fences_replay(monkeypatch):
    from brigade import fleet_dot

    conn = sqlite3.connect(":memory:")
    fleet_hub_sessions.init_schema(conn)
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW)
    raw = v2_report(progress="Testing", blocker="Waiting", evidence_refs=["receipt-a"])
    body = fleet_dot.report_session(raw)["presence_payload"]
    _, first = fleet_hub_sessions.handle_session(conn, body, caller_node=NODE)
    context = first["session"]["cloud_context"]
    assert first["session"]["evidence_verification"] == "reported-unverified"
    assert context["task"] == "Check build"
    assert context["reported_state"] == "running"
    assert context["evidence_refs"] == ["receipt-a"]
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW + 100)
    _, replay = fleet_hub_sessions.handle_session(conn, body, caller_node=NODE)
    assert replay == first
    changed = {**body, "cloud_context": {**context, "progress": "Changed"}}
    with pytest.raises(fleet_hub.FleetHubError, match="sequence conflicts"):
        fleet_hub_sessions.handle_session(conn, changed, caller_node=NODE)
    raw = {**raw, "sequence": 2, "reported_state": "completed", "result": "Build checked"}
    for key in ("progress", "blocker", "evidence_refs"):
        raw.pop(key)
    _, second = fleet_hub_sessions.handle_session(
        conn, fleet_dot.report_session(raw)["presence_payload"], caller_node=NODE
    )
    context = second["session"]["cloud_context"]
    assert context["result"] == "Build checked"
    assert "progress" not in context and "blocker" not in context
    assert context["evidence_refs"] == []
    history = fleet_hub_sessions.list_cloud_reports(conn, now_epoch=NOW + 1000)
    assert history[0]["cloud_context"]["reported_state"] == "completed"
    assert history[0]["presence_state"] == "stale"
    assert len(fleet_hub_sessions.list_cloud_reports(conn, now_epoch=NOW + 100)) == 1
    with pytest.raises(fleet_hub.FleetHubError, match="linkage"):
        fleet_hub_sessions.handle_session(conn, cloud_body(sequence=3), caller_node=NODE)


@pytest.mark.parametrize(
    "updates,remove",
    [
        ({"reported_state": "verified"}, None),
        ({"reported_state": True}, None),
        ({"work_id": "w-a", "ownership_revision": 0, "generation": 1}, None),
        ({"task": "secret=short"}, None),
        ({"result": "x" * 401}, None),
        ({"evidence_refs": ["r"] * 9}, None),
        ({"provider": "other"}, None),
        ({}, "task"),
        ({}, "reported_state"),
        ({}, "source"),
        ({}, "source_scope"),
    ],
)
def test_v2_strict_fields(updates, remove):
    from brigade import fleet_dot

    raw = {**v2_report(), **updates}
    if remove:
        raw.pop(remove)
    with pytest.raises(fleet_dot.DotReportError):
        fleet_dot.parse_report(raw)


def test_cloud_history_filters_before_limit(monkeypatch):
    conn = sqlite3.connect(":memory:")
    fleet_hub_sessions.init_schema(conn)
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW)
    from brigade import fleet_dot

    body = fleet_dot.report_session(v2_report())["presence_payload"]
    fleet_hub_sessions.handle_session(conn, body, caller_node=NODE)
    monkeypatch.setattr(fleet_hub_sessions, "LIST_HISTORY_LIMIT", 1)
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW + 901)
    # A newer active cloud row must not hide the older stale report.
    newer = fleet_dot.report_session(v2_report(session_id="newer", observed_at="2026-01-01T00:15:01Z"))[
        "presence_payload"
    ]
    fleet_hub_sessions.handle_session(conn, newer, caller_node=OTHER)
    history = fleet_hub_sessions.list_cloud_reports(conn, now_epoch=NOW + 901, node_ids={NODE})
    assert [row["session_id"] for row in history] == ["session-a"]
    assert fleet_hub_sessions.list_cloud_reports(conn, now_epoch=NOW + 901, text_filter="Check build") == history


def test_cloud_history_limit_and_text_filter_use_decoded_report_text(monkeypatch):
    conn = sqlite3.connect(":memory:")
    fleet_hub_sessions.init_schema(conn)
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: NOW)
    from brigade import fleet_dot

    for session_id, task in (("session-a", "Überprüfung der Stufe"), ("session-b", "Check build")):
        body = fleet_dot.report_session(v2_report(session_id=session_id, task=task))["presence_payload"]
        fleet_hub_sessions.handle_session(conn, body, caller_node=NODE)
    later = NOW + 901
    assert len(fleet_hub_sessions.list_cloud_reports(conn, now_epoch=later)) == 2
    assert len(fleet_hub_sessions.list_cloud_reports(conn, now_epoch=later, limit=1)) == 1
    found = fleet_hub_sessions.list_cloud_reports(conn, now_epoch=later, text_filter="überprüfung")
    assert [row["session_id"] for row in found] == ["session-a"]
    # Key names and provider metadata are not report text.
    assert fleet_hub_sessions.list_cloud_reports(conn, now_epoch=later, text_filter="source_scope") == []
    # The session directory filters on its own projected fields, so report prose never matches there.
    assert fleet_hub_sessions.list_sessions(conn, include_all=True, now_epoch=later, text_filter="überprüfung") == []


@pytest.mark.parametrize(
    "reply", [{}, {"session": None}, {"session": {"node_id": NODE}}, "invalid", "timeout", "old-hub"]
)
def test_v2_publication_never_confirms_missing_or_malformed_readback(monkeypatch, reply):
    from io import BytesIO
    from brigade import fleet_dot, fleet_client_messages

    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://example.test", "node_token": "n" * 40, "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE)
    monkeypatch.setattr(fleet_client_messages, "identity", lambda **kw: {"principal": NODE})
    calls = []

    def opened(request, **kwargs):
        calls.append(request)
        if reply == "timeout":
            raise TimeoutError
        if reply == "old-hub":
            import urllib.error

            raise urllib.error.HTTPError(request.full_url, 400, "old hub", {}, None)
        response = BytesIO(reply.encode() if isinstance(reply, str) else json.dumps(reply).encode())
        response.status = 200
        return response

    monkeypatch.setattr(fleet_client, "_hub_open", opened)
    result = fleet_dot.report_session(v2_report(), publish=True, expected_node=NODE)
    assert result["status"] == ("refused" if reply == "old-hub" else "unknown")
    assert result["summaries_persisted"] is False
    assert len(calls) == 1  # Never downgrade or retry a v2 report as v1.


@pytest.mark.parametrize("race", ["runtime", "principal", "settings", "node", "admin", "missing"])
def test_v2_refuses_identity_and_configuration_races_before_write(monkeypatch, race):
    from brigade import fleet_dot, fleet_client_messages

    settings = {"hub_url": "https://example.test", "node_token": "n" * 40, "admin_token": ""}
    if race == "admin":
        settings["admin_token"] = settings["node_token"]
    if race == "missing":
        settings["node_token"] = ""
    current_node = [NODE]
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: dict(settings))
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: current_node[0])

    def identity(**kwargs):
        if race == "settings":
            settings["hub_url"] = "https://other.example.test"
            settings["node_token"] = "b" * 40
        if race == "node":
            current_node[0] = OTHER
        return {"principal": OTHER if race == "principal" else NODE}

    monkeypatch.setattr(fleet_client_messages, "identity", identity)
    monkeypatch.setattr(fleet_client, "_hub_open", lambda *a, **k: pytest.fail("unauthorized session write"))
    result = fleet_dot.report_session(v2_report(), publish=True, expected_node=OTHER if race == "runtime" else NODE)
    assert result["status"] == "refused"
    assert result["summaries_persisted"] is False
    assert "n" * 40 not in json.dumps(result) and "b" * 40 not in json.dumps(result)


def test_v2_local_http_publication_readback_replay_and_revocation(tmp_path, monkeypatch):
    import threading
    from datetime import datetime, timezone
    from brigade import fleet_dot

    db = tmp_path / "fleet.db"
    conn = fleet_hub.init_db(db)
    _, token = fleet_hub.add_node(conn, NODE)
    conn.close()
    server = fleet_hub.make_server("127.0.0.1", 0, db, "fixture-admin")
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    settings = {"hub_url": f"http://127.0.0.1:{server.server_address[1]}", "node_token": token, "admin_token": ""}
    monkeypatch.setattr(fleet_client, "load_fleet_settings", lambda: dict(settings))
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE)
    # Message-only runtime credentials must never be read for node publication.
    monkeypatch.setenv("BRIGADE_FLEET_MESSAGE_TOKEN_FILE", str(tmp_path / "nonexistent"))
    raw = v2_report(observed_at=datetime.now(timezone.utc).isoformat(), result="Checked fixture")
    try:
        result = fleet_dot.report_session(raw, publish=True, expected_node=NODE)
        assert result["status"] == "confirmed" and result["summaries_persisted"] is True
        conn = fleet_hub.open_db(db)
        before = fleet_hub_sessions.list_sessions(conn)[0]
        assert before["cloud_context"]["result"] == "Checked fixture"
        assert fleet_dot.report_session(raw, publish=True, expected_node=NODE)["status"] == "confirmed"
        assert fleet_hub_sessions.list_sessions(conn)[0]["expires_at"] == before["expires_at"]
        fleet_hub.revoke_node(conn, NODE)
        conn.close()
        assert fleet_dot.report_session(raw, publish=True, expected_node=NODE)["status"] == "refused"
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize("change", ["node", "session", "sequence", "task", "omitted", "extra", "boolean"])
def test_v2_public_session_client_requires_complete_exact_context(monkeypatch, change):
    from io import BytesIO
    from brigade import fleet_dot, fleet_client_messages

    monkeypatch.setattr(
        fleet_client,
        "load_fleet_settings",
        lambda: {"hub_url": "https://example.test", "node_token": "n" * 40, "admin_token": ""},
    )
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda: NODE)
    monkeypatch.setattr(fleet_client_messages, "identity", lambda **kw: {"principal": NODE})
    snapshot = fleet_dot.parse_report(v2_report()).snapshot
    session = {
        "node_id": NODE,
        "harness": "dot",
        "session_id": snapshot.session_id,
        "repo_identity": None,
        "cloud_context": dict(snapshot.cloud_context),
        "ttl_seconds": snapshot.ttl_seconds,
    }
    if change == "node":
        session["node_id"] = OTHER
    elif change == "session":
        session["session_id"] = "another"
    elif change == "omitted":
        session["cloud_context"].pop("task")
    elif change == "extra":
        session["cloud_context"]["unexpected"] = "value"
    else:
        session["cloud_context"]["sequence" if change in {"sequence", "boolean"} else "task"] = (
            True if change == "boolean" else 3
        )

    def opened(*a, **k):
        response = BytesIO(json.dumps({"session": session}).encode())
        response.status = 200
        return response

    monkeypatch.setattr(fleet_client, "_hub_open", opened)
    assert fleet_client.publish_session(snapshot).ok is False
