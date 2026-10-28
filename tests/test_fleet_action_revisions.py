"""Review regressions at the action authority and durable broker boundaries."""

import json
import os

import pytest

from brigade import fleet_hub
from brigade.fleet_action_auth import ActionError
from tests.test_fleet_actions import backdate_action, setup_action, seed_t3_roster


requires_posix = pytest.mark.skipif(
    os.name != "posix", reason="Broker execution requires POSIX private files, process groups, and systemd"
)


def test_target_id_cannot_rebind_even_after_withdrawal(tmp_path):
    from brigade import fleet_action_targets as targets

    conn, _, _, _ = setup_action(tmp_path)
    original = json.loads(conn.execute("SELECT document FROM action_targets").fetchone()[0])
    replacement = {**original, "family": "dot", "capability": "inbox", "principal": "node-fixture", "models": []}
    with pytest.raises(ActionError, match="target-identity-conflict"):
        targets.sync(conn, "node-fixture", [replacement])
    targets.sync(conn, "node-fixture", [])
    with pytest.raises(ActionError, match="target-identity-conflict"):
        targets.sync(conn, "node-fixture", [replacement])
    targets.sync(conn, "node-fixture", [original])
    assert targets.available(conn)[0]["family"] == "openclaw"
    conn.close()


def test_sync_rejects_non_utf8_labels_and_preserves_unicode(tmp_path):
    from brigade import fleet_action_auth as auth, fleet_action_targets as targets

    conn, _, _, request = setup_action(tmp_path)
    original = json.loads(conn.execute("SELECT document FROM action_targets").fetchone()[0])
    label = "Agent 日本語 😀"
    targets.sync(conn, "node-fixture", [{**original, "label": label}])
    assert targets.available(conn)[0]["label"] == label

    with pytest.raises(ActionError, match="invalid-label"):
        targets.sync(conn, "node-fixture", [{**original, "label": "bad\ud800"}])
    assert targets.available(conn)[0]["label"] == label

    model_target = {
        **original,
        "target_id": "repair-t3",
        "family": "t3",
        "host": "fixture-host",
        "repository": "fixture-repo",
        "repo_identity": "repo/fixture",
        "source_revision": "a" * 40,
        "models": [{"id": "fixture-seat", "label": "Modèle 😀"}],
    }
    auth.grant_broker(conn, "node-fixture", ["repair-t3"], [request["source"]])
    targets.sync(conn, "node-fixture", [model_target])
    stored_model = json.loads(
        conn.execute("SELECT document FROM action_targets WHERE target_id='repair-t3'").fetchone()[0]
    )["models"][0]
    assert stored_model["label"] == "Modèle 😀"
    with pytest.raises(ActionError, match="invalid-model"):
        targets.sync(conn, "node-fixture", [{**model_target, "models": [{"id": "fixture-seat", "label": "bad\ud800"}]}])
    assert (
        json.loads(conn.execute("SELECT document FROM action_targets WHERE target_id='repair-t3'").fetchone()[0])[
            "models"
        ][0]["label"]
        == "Modèle 😀"
    )
    conn.close()


@requires_posix
def test_manifest_public_agent_must_match_native_binding(tmp_path):
    from brigade import fleet_action_runner as runner
    from tests.test_fleet_action_runner import fake_executable, manifest

    conn, _, _, _ = setup_action(tmp_path)
    original = json.loads(conn.execute("SELECT document FROM action_targets").fetchone()[0])
    original["agent_id"] = "other-agent"
    executable, _ = fake_executable(tmp_path, {})
    path = manifest(tmp_path, executable, original, binding={"agent_id": "fixture-agent"})
    with pytest.raises(ActionError, match="native-binding-mismatch"):
        runner.load_manifest(path)
    conn.close()


@requires_posix
def test_accepted_inbox_does_not_starve_next_queued_action(tmp_path, monkeypatch):
    from brigade import (
        fleet_action_auth as auth,
        fleet_action_runner as runner,
        fleet_client_actions as client,
        fleet_hub_actions as actions,
    )
    from brigade.fleet_action_http import handle_api
    from tests.test_fleet_action_runner import fake_executable, manifest

    conn, _, _, request = setup_action(tmp_path)
    original = json.loads(conn.execute("SELECT document FROM action_targets").fetchone()[0])
    inbox = {
        **original,
        "target_id": "fixture-inbox",
        "family": "dot",
        "capability": "inbox",
        "principal": "node-fixture",
    }
    inbox.pop("agent_id")
    auth.grant_broker(conn, "node-fixture", ["repair", "fixture-inbox"], [request["source"]])
    grant = auth.grant_operator(
        conn,
        "operator@example.test",
        [{"target_id": name, "subject": request["source"]} for name in ("repair", "fixture-inbox")],
    )
    exe, _ = fake_executable(tmp_path, {})
    path = manifest(tmp_path, exe, original)
    data = json.loads(path.read_text())
    data["targets"].append({"public": {k: v for k, v in inbox.items() if k != "binding_digest"}, "binding": {}})
    path.write_text(json.dumps(data))
    monkeypatch.setattr(client, "request", lambda body, **kwargs: handle_api(conn, (False, "node-fixture"), body))
    runner.sync_targets(path)
    first = actions.submit(conn, grant, {**request, "target_id": "fixture-inbox"})
    normal = runner.adapters.invoke

    def invoke(executable, argv, **kwargs):
        if "--inbox-send" in argv:
            return {"message": {"id": 42, "client_id": first["action_id"]}}
        return normal(executable, argv, **kwargs)

    monkeypatch.setattr(runner.adapters, "invoke", invoke)
    claimed = actions.claim(conn, "node-fixture")
    runner.execute(path, first["action_id"], claimed["generation"])
    second = actions.submit(conn, grant, {**request, "client_id": "second-click"})
    runner.tick(path)
    assert actions.show(conn, second["action_id"])["generation"] == 1
    assert actions.show(conn, first["action_id"])["status"] == "accepted"
    conn.close()


@requires_posix
def test_lost_prestart_response_can_reclaim_without_replaying_old_generation(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner, fleet_client_actions as client, fleet_hub_actions as actions
    from tests.test_fleet_action_runner import broker_fixture

    conn, path, log, action = broker_fixture(tmp_path, monkeypatch)
    first = actions.claim(conn, "node-fixture")
    normal = client.request

    def unavailable(body, **kwargs):
        if body["op"] == "start":
            raise OSError("fixture start did not arrive")
        return normal(body, **kwargs)

    monkeypatch.setattr(client, "request", unavailable)
    with pytest.raises(OSError):
        runner.execute(path, action["action_id"], first["generation"])
    monkeypatch.setattr(client, "request", normal)
    assert actions.show(conn, action["action_id"])["started_at"] is None
    with conn:
        conn.execute("UPDATE agent_actions SET claim_until=0")
    runner.tick(path)
    reclaimed = actions.show(conn, action["action_id"])
    assert reclaimed["generation"] == first["generation"] + 1
    assert reclaimed["started_at"] is None
    assert not any(json.loads(row)["args"][:1] == ["agent"] for row in log.read_text().splitlines())
    conn.close()


def test_run_selector_admits_future_exact_node_repo_and_deck_source(tmp_path):
    from brigade import fleet_action_auth as auth, fleet_action_sources as sources, fleet_action_targets as targets
    from brigade import fleet_hub_actions as actions
    from tests.test_fleet_sync import _event

    conn, _, _, request = setup_action(tmp_path)
    fleet_hub.add_node(conn, "other-node")
    scope = {"kind": "run", "node_id": "node-fixture", "repo_identity": "repo/fixture"}
    auth.grant_broker(conn, "node-fixture", ["repair"], [scope])
    grant = auth.grant_operator(conn, "operator@example.test", [{"target_id": "repair", "subject": scope}])
    target = json.loads(conn.execute("SELECT document FROM action_targets").fetchone()[0])
    targets.sync(conn, "node-fixture", [{**target, "subjects": [scope]}])
    for run, node, repo in (
        ("future", "node-fixture", "repo/fixture"),
        ("wrong-repo", "node-fixture", "repo/other"),
        ("wrong-node", "other-node", "repo/fixture"),
    ):
        fleet_hub.store_events(
            conn,
            [{**_event(node=node, run_id=run, state="run.completed"), "repo_identity": repo, "exit_status": 1}],
            caller_node=node,
        )
    source = {"kind": "run", "node_id": "node-fixture", "run_id": "future"}
    snapshot = sources.resolve(conn, source)
    action = actions.submit(conn, grant, {**request, "source": source, "observed_revision": snapshot["revision"]})
    assert action["request"]["source"] == source
    refs = [
        auth.canonical({"kind": "run", "node_id": n, "run_id": r})
        for r, n in (("future", "node-fixture"), ("wrong-repo", "node-fixture"), ("wrong-node", "other-node"))
    ]
    assert targets.linked_subjects(conn, refs) == frozenset({refs[0]})
    with pytest.raises(ActionError, match="invalid-subject"):
        actions.submit(conn, grant, {**request, "source": scope, "client_id": "selector-is-not-source"})
    conn.close()


@requires_posix
def test_catalog_metadata_refresh_does_not_expire_queued_request(tmp_path, monkeypatch):
    from brigade import fleet_action_runner as runner, fleet_hub_actions as actions
    from tests.test_fleet_action_runner import broker_fixture

    conn, path, _, action = broker_fixture(tmp_path, monkeypatch)
    data = json.loads(path.read_text())
    data["targets"][0]["public"]["label"] = "Refreshed display label"
    path.write_text(json.dumps(data))
    runner.sync_targets(path)
    claimed = actions.claim(conn, "node-fixture")
    assert claimed and claimed["action_id"] == action["action_id"]
    assert runner.execute(path, action["action_id"], claimed["generation"])["status"] == "finished"
    conn.close()


def test_admin_revoke_broker_expires_claim_and_preserves_telemetry_enrollment(tmp_path):
    from brigade import fleet_action_auth as auth, fleet_action_targets as targets, fleet_hub_actions as actions
    from brigade.fleet_action_http import handle_api

    conn, _, grant, request = setup_action(tmp_path)
    action = actions.submit(conn, grant, request)
    claimed = actions.claim(conn, "node-fixture")
    target = json.loads(conn.execute("SELECT document FROM action_targets").fetchone()[0])
    with pytest.raises(ActionError, match="admin-required"):
        handle_api(conn, (False, "node-fixture"), {"op": "revoke-broker", "node_id": "node-fixture"})
    assert handle_api(conn, (True, None), {"op": "revoke-broker", "node_id": "node-fixture"})["revoked"]
    assert auth.active_node(conn, "node-fixture")
    assert conn.execute("SELECT count(*) FROM fleet_machine_telemetry").fetchone()[0] == 1
    assert targets.available(conn) == []
    expired = actions.show(conn, action["action_id"])
    assert expired["status"] == "expired" and sum(e["event_id"] == "expired" for e in expired["events"]) == 1
    with pytest.raises(ActionError):
        targets.sync(conn, "node-fixture", [target])
    assert actions.claim(conn, "node-fixture") is None
    with pytest.raises(ActionError):
        actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])
    conn.close()


def test_execution_receipts_require_correlation_and_forward_transitions(tmp_path):
    from brigade import fleet_hub_actions as actions

    conn, _, grant, request = setup_action(tmp_path)
    action = actions.submit(conn, grant, request)
    claimed = actions.claim(conn, "node-fixture")
    actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])

    def report(event, status, **extra):
        return actions.report(
            conn,
            "node-fixture",
            action["action_id"],
            claimed["generation"],
            {"event_id": event, "status": status, **extra},
        )

    with pytest.raises(ActionError, match="destination-reference-required"):
        report("no-reference", "finished")
    report("running", "running", destination_ref="peer:request", destination_thread="thread-fixture")
    with pytest.raises(ActionError, match="transition-refused"):
        report("backwards", "accepted")
    with pytest.raises(ActionError, match="destination-conflict"):
        report("rebound", "finished", destination_ref="peer:other")
    with pytest.raises(ActionError, match="destination-conflict"):
        report("thread-rebound", "finished", destination_thread="other-thread")
    final = report("finished", "finished")
    assert final["destination_thread"] == "thread-fixture" and final["verification"] == "pending"
    conn.close()


def test_blocking_turn_lease_and_unknown_event_are_bounded_and_idempotent(tmp_path, monkeypatch):
    from brigade import fleet_hub_actions as actions

    conn, _, grant, request = setup_action(tmp_path)
    action = actions.submit(conn, grant, request)
    claimed = actions.claim(conn, "node-fixture")
    started = actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])
    assert 3600 < started["claim_until"] - started["started_at"] < 3900
    monkeypatch.setattr(actions.time, "time", lambda: started["started_at"] + 61)
    assert actions.claim(conn, "node-fixture") is None
    assert actions.show(conn, action["action_id"])["status"] == "dispatching"
    monkeypatch.setattr(actions.time, "time", lambda: started["claim_until"] + 1)
    assert actions.claim(conn, "node-fixture") is None
    assert actions.claim(conn, "node-fixture") is None
    held = actions.show(conn, action["action_id"])
    assert held["status"] == "unknown"
    assert sum(e["event_id"] == "unknown" for e in held["events"]) == 1
    conn.close()


def test_telemetry_refresh_preserves_condition_revision_but_staleness_changes_it(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    from brigade import fleet_action_sources as sources, fleet_hub_actions as actions

    conn, _, grant, request = setup_action(tmp_path)
    now = sources.time.time()
    monkeypatch.setattr(sources.time, "time", lambda: now)

    def stamp(age):
        return datetime.fromtimestamp(now - age, timezone.utc).isoformat()

    with conn:
        conn.execute("UPDATE fleet_machine_telemetry SET observed_at=?, status='unavailable'", (stamp(1),))
    first = sources.resolve(conn, request["source"])
    action = actions.submit(conn, grant, {**request, "observed_revision": first["revision"]})
    with conn:
        conn.execute("UPDATE fleet_machine_telemetry SET observed_id='new-observation', observed_at=?", (stamp(0),))
    assert sources.resolve(conn, request["source"])["revision"] == first["revision"]
    claim = actions.claim(conn, "node-fixture")
    assert actions.start(conn, "node-fixture", action["action_id"], claim["generation"])["started_at"] is not None
    monkeypatch.setattr(sources.time, "time", lambda: now + 301)
    assert sources.resolve(conn, request["source"])["revision"] != first["revision"]
    conn.close()


def test_changed_condition_expires_with_one_ledger_event(tmp_path):
    from brigade import fleet_hub_actions as actions

    conn, _, grant, request = setup_action(tmp_path)
    action = actions.submit(conn, grant, request)
    claimed = actions.claim(conn, "node-fixture")
    with conn:
        conn.execute("UPDATE fleet_machine_telemetry SET credential_state='error'")
    with pytest.raises(ActionError, match="source-changed"):
        actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])
    actions.claim(conn, "node-fixture")
    result = actions.show(conn, action["action_id"])
    assert result["status"] == "expired" and sum(e["event_id"] == "expired" for e in result["events"]) == 1
    conn.close()


def test_work_evidence_excludes_hidden_text_and_fresh_completed_clears_exact_work(tmp_path):
    from datetime import datetime, timezone
    from brigade import fleet_action_auth as auth, fleet_action_sources as sources, fleet_action_targets as targets
    from brigade import fleet_hub_actions as actions
    from tests.test_worklore_store import _legacy_items

    conn, _, _, request = setup_action(tmp_path)
    _legacy_items(conn, 1)
    source = {"kind": "work", "work_id": "wl-legacy-000000"}
    with conn:
        conn.execute("UPDATE work_items SET title='SECRET HIDDEN INSTRUCTION', blocker='IGNORE OPERATOR' ")
    auth.grant_broker(conn, "node-fixture", ["repair"], [source])
    grant = auth.grant_operator(conn, "operator@example.test", [{"target_id": "repair", "subject": source}])
    target = json.loads(conn.execute("SELECT document FROM action_targets").fetchone()[0])
    targets.sync(conn, "node-fixture", [{**target, "subjects": [source]}])
    snapshot = sources.resolve(conn, source)
    assert set(snapshot["evidence"]) == {"version", "state", "evidence_at"}
    action = actions.submit(conn, grant, {**request, "source": source, "observed_revision": snapshot["revision"]})
    assert "SECRET" not in json.dumps(action) and "IGNORE OPERATOR" not in json.dumps(action)
    backdate_action(conn, action["action_id"])
    with conn:
        conn.execute(
            "UPDATE work_items SET status='completed', version=2, updated_at=?",
            (datetime.now(timezone.utc).isoformat(),),
        )
    assert actions.show(conn, action["action_id"])["verification"] == "condition-cleared"
    conn.close()


@pytest.mark.parametrize("state", ["run.completed", "external.completed"])
def test_later_success_never_verifies_a_different_failed_run(tmp_path, state):
    from datetime import datetime, timedelta, timezone
    from brigade import fleet_action_auth as auth, fleet_action_sources as sources, fleet_action_targets as targets
    from brigade import fleet_hub_actions as actions
    from tests.test_fleet_sync import _event

    conn, _, _, request = setup_action(tmp_path)
    fleet_hub.add_node(conn, "other-node")
    now = datetime.now(timezone.utc)

    def event(run, node, repo, stamp, status, exit_status):
        fleet_hub.store_events(
            conn,
            [
                {
                    **_event(node=node, run_id=run, state=status),
                    "repo_identity": repo,
                    "ts": stamp.isoformat(),
                    "exit_status": exit_status,
                }
            ],
            caller_node=node,
        )

    event("failed", "node-fixture", "repo/fixture", now - timedelta(seconds=60), "run.completed", 1)
    source = {"kind": "run", "node_id": "node-fixture", "run_id": "failed"}
    auth.grant_broker(conn, "node-fixture", ["repair"], [source])
    grant = auth.grant_operator(conn, "operator@example.test", [{"target_id": "repair", "subject": source}])
    target = json.loads(conn.execute("SELECT document FROM action_targets").fetchone()[0])
    targets.sync(conn, "node-fixture", [{**target, "subjects": [source]}])
    action = actions.submit(
        conn, grant, {**request, "source": source, "observed_revision": sources.resolve(conn, source)["revision"]}
    )
    for run, node, repo, stamp in (
        ("old", "node-fixture", "repo/fixture", now - timedelta(seconds=30)),
        ("other-repo", "node-fixture", "repo/other", now),
        ("other-node", "other-node", "repo/fixture", now),
    ):
        event(run, node, repo, stamp, state, 0)
    assert actions.show(conn, action["action_id"])["verification"] == "pending"
    event("later", "node-fixture", "repo/fixture", datetime.now(timezone.utc), state, 0)
    assert actions.show(conn, action["action_id"])["verification"] == "pending"
    conn.close()


@requires_posix
def test_t3_repository_scope_and_sha_refresh_keep_frozen_request_revision(tmp_path, monkeypatch):
    from brigade import fleet_action_auth as auth, fleet_action_sources as sources, fleet_action_targets as targets
    from brigade import fleet_action_runner as runner, fleet_client_actions as client, fleet_hub_actions as actions
    from brigade import fleet_hub_policy, fleet_policy
    from brigade.fleet_action_http import handle_api
    from tests.test_fleet_action_runner import fake_executable, manifest
    from tests.test_fleet_sync import _event

    conn, _, _, request = setup_action(tmp_path)
    seed_t3_roster(conn)
    document = fleet_policy.empty_document()
    document["seats"] = {"fixture-seat": {"provider": "fixture-provider", "model": "current-model"}}
    fleet_hub_policy.save_policy(conn, document, expected_version=1, actor="fixture", reason="test policy")
    scope = {"kind": "run", "node_id": "node-fixture", "repo_identity": "repo/fixture"}
    auth.grant_broker(conn, "node-fixture", ["repair-t3"], [scope])
    grant = auth.grant_operator(conn, "operator@example.test", [{"target_id": "repair-t3", "subject": scope}])
    target = {
        "target_id": "repair-t3",
        "family": "t3",
        "label": "Fixture project",
        "subjects": [scope],
        "models": [{"id": "fixture-seat", "label": "model"}],
        "capability": "execution",
        "host": "fixture-host",
        "repository": "fixture-repo",
        "repo_identity": "repo/fixture",
        "source_revision": "a" * 40,
    }
    exe, log = fake_executable(
        tmp_path,
        {
            "schema": "t3-fleet.result.v1",
            "request_id": "fixture-host-fixture-repo-12345678-1234-1234-1234-123456789abc",
            "state": "running",
        },
    )
    binding = {key: target[key] for key in ("host", "repository", "repo_identity", "source_revision")}
    binding["runtime_mode"] = "full-access"
    path = manifest(tmp_path, exe, target, binding=binding)
    monkeypatch.setattr(client, "request", lambda body, **kwargs: handle_api(conn, (False, "node-fixture"), body))
    runner.sync_targets(path)
    for run, repo in (("new-run", "repo/fixture"), ("other-repo", "repo/other")):
        fleet_hub.store_events(
            conn,
            [
                {
                    **_event(node="node-fixture", run_id=run, state="run.completed"),
                    "repo_identity": repo,
                    "exit_status": 1,
                }
            ],
            caller_node="node-fixture",
        )
    source = {"kind": "run", "node_id": "node-fixture", "run_id": "new-run"}
    assert not targets.available(conn, source={**source, "run_id": "other-repo"})
    action = actions.submit(
        conn,
        grant,
        {
            **request,
            "target_id": "repair-t3",
            "model_id": "fixture-seat",
            "source": source,
            "observed_revision": sources.resolve(conn, source)["revision"],
        },
    )
    data = json.loads(path.read_text())
    data["targets"][0]["binding"]["source_revision"] = "b" * 40
    data["targets"][0]["public"]["source_revision"] = "b" * 40
    path.write_text(json.dumps(data))
    runner.sync_targets(path)
    claim = actions.claim(conn, "node-fixture")
    assert claim and claim["action_id"] == action["action_id"]
    assert runner.execute(path, action["action_id"], claim["generation"])["status"] == "running"
    argv = json.loads(log.read_text().splitlines()[-1])["args"]
    assert argv[argv.index("--source-revision") + 1] == "a" * 40
    del data["targets"][0]["binding"]["repo_identity"]
    path.write_text(json.dumps(data))
    with pytest.raises(ActionError, match="invalid-binding"):
        runner.load_manifest(path)
    conn.close()


def test_revoked_broker_cannot_report_started_execution(tmp_path):
    from brigade import fleet_action_auth as auth, fleet_hub_actions as actions

    conn, _, grant, request = setup_action(tmp_path)
    action = actions.submit(conn, grant, request)
    claimed = actions.claim(conn, "node-fixture")
    actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])
    auth.revoke_broker(conn, "node-fixture")
    assert auth.active_node(conn, "node-fixture")
    with pytest.raises(ActionError, match="broker-scope-refused"):
        actions.report(
            conn,
            "node-fixture",
            action["action_id"],
            claimed["generation"],
            {"event_id": "forged", "status": "finished", "destination_ref": "fixture-turn"},
        )
    conn.close()


def test_execution_unknown_does_not_erase_previous_running_transition(tmp_path):
    from brigade import fleet_hub_actions as actions

    conn, _, grant, request = setup_action(tmp_path)
    action = actions.submit(conn, grant, request)
    claimed = actions.claim(conn, "node-fixture")
    actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])
    for event, status in (("running", "running"), ("uncertain", "unknown")):
        actions.report(
            conn,
            "node-fixture",
            action["action_id"],
            claimed["generation"],
            {"event_id": event, "status": status, "destination_ref": "fixture-turn"},
        )
    with pytest.raises(ActionError, match="transition-refused"):
        actions.report(
            conn,
            "node-fixture",
            action["action_id"],
            claimed["generation"],
            {"event_id": "backwards", "status": "accepted"},
        )
    conn.close()


@requires_posix
def test_known_unknown_t3_reference_reconciles_without_another_submit(tmp_path, monkeypatch):
    from brigade import fleet_action_auth as auth, fleet_action_sources as sources, fleet_action_runner as runner
    from brigade import fleet_client_actions as client, fleet_hub_actions as actions, fleet_hub_policy, fleet_policy
    from brigade.fleet_action_http import handle_api
    from tests.test_fleet_action_runner import fake_executable, manifest
    from tests.test_fleet_sync import _event

    conn, _, _, request = setup_action(tmp_path)
    seed_t3_roster(conn)
    document = fleet_policy.empty_document()
    document["seats"] = {"fixture-seat": {"provider": "fixture-provider", "model": "current-model"}}
    fleet_hub_policy.save_policy(conn, document, expected_version=1, actor="fixture", reason="test policy")
    source = {"kind": "run", "node_id": "node-fixture", "run_id": "fixture-run"}
    fleet_hub.store_events(
        conn,
        [
            {
                **_event(node="node-fixture", run_id="fixture-run", state="run.completed"),
                "repo_identity": "repo/fixture",
                "exit_status": 1,
            }
        ],
        caller_node="node-fixture",
    )
    auth.grant_broker(conn, "node-fixture", ["repair-t3"], [source])
    grant = auth.grant_operator(conn, "operator@example.test", [{"target_id": "repair-t3", "subject": source}])
    target = {
        "target_id": "repair-t3",
        "family": "t3",
        "label": "Fixture project",
        "subjects": [source],
        "models": [{"id": "fixture-seat", "label": "model"}],
        "capability": "execution",
        "host": "fixture-host",
        "repository": "fixture-repo",
        "repo_identity": "repo/fixture",
        "source_revision": "a" * 40,
    }
    exe, log = fake_executable(
        tmp_path,
        {
            "schema": "t3-fleet.result.v1",
            "request_id": "fixture-host-fixture-repo-12345678-1234-1234-1234-123456789abc",
            "state": "unknown",
        },
    )
    path = manifest(
        tmp_path,
        exe,
        target,
        binding={key: target[key] for key in ("host", "repository", "repo_identity", "source_revision")},
    )
    monkeypatch.setattr(client, "request", lambda body, **kwargs: handle_api(conn, (False, "node-fixture"), body))
    runner.sync_targets(path)
    action = actions.submit(
        conn,
        grant,
        {
            **request,
            "target_id": "repair-t3",
            "model_id": "fixture-seat",
            "source": source,
            "observed_revision": sources.resolve(conn, source)["revision"],
        },
    )
    claim = actions.claim(conn, "node-fixture")
    assert runner.execute(path, action["action_id"], claim["generation"])["status"] == "unknown"
    fake_executable(
        tmp_path,
        {
            "schema": "t3-fleet.result.v1",
            "request_id": "fixture-host-fixture-repo-12345678-1234-1234-1234-123456789abc",
            "state": "running",
        },
    )
    assert runner.execute(path, action["action_id"], claim["generation"])["status"] == "running"
    calls = [json.loads(line)["args"][0] for line in log.read_text().splitlines()]
    assert calls == ["submit", "recover"]
    conn.close()
