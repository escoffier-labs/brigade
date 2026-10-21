"""Action authority regressions at the persisted Hub boundary."""

from concurrent.futures import ThreadPoolExecutor

import pytest

from brigade import fleet_hub


def setup_action(tmp_path):
    from brigade import fleet_action_auth as auth, fleet_action_sources as sources, fleet_action_targets as targets

    path = tmp_path / "hub.db"
    conn = fleet_hub.init_db(path)
    fleet_hub.add_node(conn, "node-fixture")
    with conn:
        conn.execute(
            "INSERT INTO fleet_machine_telemetry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "node-fixture",
                "fixture",
                "2026-01-01T00:00:00+00:00",
                300,
                "unavailable",
                0,
                "[]",
                "[]",
                "[]",
                "ok",
                "observation-1",
                "2026-01-01T00:00:00+00:00",
            ),
        )
    source = {"kind": "telemetry", "node_id": "node-fixture"}
    grant = auth.grant_operator(conn, "operator@example.test", [{"target_id": "repair", "subject": source}])
    auth.grant_broker(conn, "node-fixture", ["repair"], [source])
    target = {
        "target_id": "repair",
        "family": "openclaw",
        "agent_id": "fixture-agent",
        "label": "Fixture agent",
        "subjects": [source],
        "models": [],
        "capability": "execution",
        "binding_digest": "a" * 64,
    }
    targets.sync(conn, "node-fixture", [target])
    evidence = sources.resolve(conn, source)
    request = {
        "source": source,
        "observed_revision": evidence["revision"],
        "target_id": "repair",
        "model_id": "",
        "intent": "repair",
        "prompt": "Investigate 雪 <script>bad()</script>",
        "client_id": "fixture-click-1",
    }
    return conn, path, grant, request


def seed_t3_roster(conn, *, enabled=True):
    from brigade import fleet_hub_model_roster as roster

    roster.set_model_policy(
        conn,
        {
            "action": "set",
            "expected_revision": roster.raw_revision(conn),
            "provider": "openai",
            "model": "current-model",
            "seat": "fixture-seat",
            "enabled": enabled,
            "reasoning": "high",
            "t3_instance_id": "fixture-instance",
            "t3_model": "fixture-native-model",
        },
    )


def test_immutable_submission_exact_retry_and_scope(tmp_path):
    from brigade import fleet_hub_actions as actions
    from brigade.fleet_action_auth import ActionError

    conn, _, grant, request = setup_action(tmp_path)
    first = actions.submit(conn, grant, request)
    assert first["status"] == "queued"
    assert actions.submit(conn, grant, request)["action_id"] == first["action_id"]
    with pytest.raises(ActionError, match="client-id-conflict"):
        actions.submit(conn, grant, {**request, "prompt": "different"})
    with pytest.raises(ActionError, match="scope-refused"):
        actions.submit(conn, grant, {**request, "client_id": "other", "target_id": "elsewhere"})
    assert conn.execute("SELECT count(*) FROM agent_actions").fetchone()[0] == 1
    conn.close()


@pytest.mark.parametrize("tied_timestamps", [False, True])
def test_history_keeps_newest_receipts_in_chronological_total_order(tmp_path, monkeypatch, tied_timestamps):
    from brigade import fleet_hub_actions as actions

    conn, _, grant, request = setup_action(tmp_path)
    action = actions.submit(conn, grant, request)
    action_id = action["action_id"]
    generation = actions.claim(conn, "node-fixture")["generation"]
    actions.start(conn, "node-fixture", action_id, generation)
    # Events may reuse an id in different generations at the same timestamp.
    with conn:
        for old_generation in (3, 2):
            actions._event(conn, action_id, old_generation, "poll_005", {"status": "dispatching"})
        conn.execute("UPDATE action_events SET created_at=0")
        if tied_timestamps:
            conn.execute("UPDATE action_events SET created_at=1 WHERE event_id='poll_005'")
    receipts = []
    for index in range(105):
        receipt = {
            "event_id": f"poll_{index:03}",
            "status": "finished" if index == 104 else "running",
            "destination_ref": "fixture-turn",
        }
        monkeypatch.setattr(actions.time, "time", lambda now=1 if tied_timestamps else index + 1: now)
        actions.report(conn, "node-fixture", action_id, generation, receipt)
        receipts.append(receipt)
    result = actions.show(conn, action_id)
    assert result["status"] == "finished" and result["verification"] == "pending"
    assert len(result["events"]) == 100
    if tied_timestamps:
        assert (result["events"][0]["event_id"], result["events"][0]["generation"]) == ("poll_005", 3)
        assert [event["receipt"] for event in result["events"][1:]] == receipts[6:]
    else:
        assert [event["receipt"] for event in result["events"]] == receipts[-100:]
    assert result["events"][-1]["receipt"]["status"] == "finished"
    assert conn.execute("SELECT count(*) FROM action_events").fetchone()[0] == 110
    conn.close()


def test_stale_revision_requires_review_not_silent_refresh(tmp_path):
    from brigade import fleet_hub_actions as actions
    from brigade.fleet_action_auth import ActionError

    conn, _, grant, request = setup_action(tmp_path)
    with conn:
        conn.execute("UPDATE fleet_machine_telemetry SET credential_state = 'error'")
    with pytest.raises(ActionError, match="source-changed") as error:
        actions.submit(conn, grant, request)
    assert error.value.status == 409
    assert conn.execute("SELECT count(*) FROM agent_actions").fetchone()[0] == 0
    conn.close()


def test_claim_generation_and_post_start_crash_never_redispatch(tmp_path, monkeypatch):
    from brigade import fleet_hub_actions as actions
    from brigade.fleet_action_auth import ActionError

    conn, _, grant, request = setup_action(tmp_path)
    action = actions.submit(conn, grant, request)
    first = actions.claim(conn, "node-fixture")
    assert first["action_id"] == action["action_id"]
    assert actions.claim(conn, "node-fixture") is None
    with pytest.raises(ActionError, match="fence-refused"):
        actions.start(conn, "node-fixture", action["action_id"], first["generation"] + 1)
    actions.start(conn, "node-fixture", action["action_id"], first["generation"])
    with conn:
        conn.execute("UPDATE agent_actions SET claim_until = 0")
    assert actions.claim(conn, "node-fixture") is None
    held = actions.show(conn, action["action_id"])
    assert held["status"] == "unknown"
    assert held["verification"] == "pending"
    conn.close()


def test_simultaneous_claims_have_one_owner(tmp_path):
    from brigade import fleet_hub_actions as actions

    conn, path, grant, request = setup_action(tmp_path)
    actions.submit(conn, grant, request)
    conn.close()

    def attempt(_):
        local = fleet_hub.open_db(path)
        try:
            return actions.claim(local, "node-fixture")
        finally:
            local.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(result is not None for result in results) == 1


def test_revocation_expires_unstarted_and_ack_does_not_verify(tmp_path):
    from brigade import fleet_action_auth as auth, fleet_hub_actions as actions
    from brigade.fleet_action_auth import ActionError

    conn, _, grant, request = setup_action(tmp_path)
    action = actions.submit(conn, grant, request)
    claimed = actions.claim(conn, "node-fixture")
    auth.revoke_operator(conn, grant)
    with pytest.raises(ActionError, match="scope-revoked"):
        actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])
    assert actions.show(conn, action["action_id"])["status"] == "expired"
    conn.close()


@pytest.mark.parametrize("expire_before_start", [False, True])
def test_recovered_source_projection_does_not_imply_execution(tmp_path, expire_before_start):
    from datetime import datetime, timezone

    from brigade import fleet_action_auth as auth, fleet_hub_actions as actions

    conn, _, grant, request = setup_action(tmp_path)
    action = actions.submit(conn, grant, request)
    if expire_before_start:
        from brigade.fleet_action_auth import ActionError

        claimed = actions.claim(conn, "node-fixture")
        auth.revoke_operator(conn, grant)
        with pytest.raises(ActionError, match="scope-revoked"):
            actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])
        assert actions.show(conn, action["action_id"])["status"] == "expired"

    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute(
            "UPDATE fleet_machine_telemetry SET status='available', observed_id='fresh', observed_at=?",
            (now,),
        )
        conn.execute(
            "UPDATE agent_actions SET created_at=? WHERE action_id=?", (actions.time.time() - 10, action["action_id"])
        )

    projected = actions.show(conn, action["action_id"])
    assert projected["verification"] == "condition-cleared"
    assert projected["status"] == ("expired" if expire_before_start else "queued")
    assert projected["started_at"] is None
    conn.close()


def test_broker_cannot_submit_verification_on_valid_receipt(tmp_path):
    from brigade import fleet_hub_actions as actions
    from brigade.fleet_action_auth import ActionError

    conn, _, grant, request = setup_action(tmp_path)
    action = actions.submit(conn, grant, request)
    claimed = actions.claim(conn, "node-fixture")
    actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])
    with pytest.raises(ActionError, match="invalid-receipt"):
        actions.report(
            conn,
            "node-fixture",
            action["action_id"],
            claimed["generation"],
            {
                "event_id": "fixture-event",
                "status": "finished",
                "destination_ref": "fixture-turn",
                "verification": "passed",
            },
        )
    assert actions.show(conn, action["action_id"])["status"] == "dispatching"
    conn.close()


def test_current_t3_roster_labels_and_revocation_are_authority(tmp_path):
    from brigade import fleet_action_targets as targets, fleet_hub_policy, fleet_policy

    conn, _, _, request = setup_action(tmp_path)
    seed_t3_roster(conn)
    document = fleet_policy.empty_document()
    document["seats"] = {"fixture-seat": {"provider": "fixture-provider", "model": "current-model"}}
    fleet_hub_policy.save_policy(conn, document, expected_version=1, actor="fixture", reason="test policy")
    target = {
        "target_id": "repair-t3",
        "family": "t3",
        "label": "Fixture project",
        "subjects": [request["source"]],
        "models": [{"id": "fixture-seat", "label": "stale-label"}],
        "capability": "execution",
        "binding_digest": "a" * 64,
        "host": "fixture-host",
        "repository": "fixture-repo",
        "repo_identity": "repo/fixture",
        "source_revision": "a" * 40,
    }
    from brigade import fleet_action_auth as auth

    auth.grant_broker(conn, "node-fixture", ["repair-t3"], [request["source"]])
    targets.sync(conn, "node-fixture", [target])
    assert targets.available(conn)[0]["models"] == [{"id": "fixture-seat", "label": "current-model"}]
    seed_t3_roster(conn, enabled=False)
    assert targets.available(conn) == []
    conn.close()


def test_only_fresh_original_surface_evidence_can_clear_condition(tmp_path):
    from datetime import datetime, timezone
    from brigade import fleet_hub_actions as actions

    conn, _, grant, request = setup_action(tmp_path)
    action = actions.submit(conn, grant, request)
    claimed = actions.claim(conn, "node-fixture")
    actions.start(conn, "node-fixture", action["action_id"], claimed["generation"])
    actions.report(
        conn,
        "node-fixture",
        action["action_id"],
        claimed["generation"],
        {"event_id": "finished", "status": "finished", "destination_ref": "fixture-turn"},
    )
    assert actions.show(conn, action["action_id"])["verification"] == "pending"
    with conn:
        conn.execute(
            "UPDATE fleet_machine_telemetry SET status='available', observed_id='fresh', observed_at=?",
            (datetime.now(timezone.utc).isoformat(),),
        )
    assert actions.show(conn, action["action_id"])["verification"] == "condition-cleared"
    conn.close()


@pytest.mark.parametrize("state", ["run.completed", "succeeded"])
def test_original_run_zero_exit_is_not_independent_verification(tmp_path, state):
    from datetime import datetime, timezone
    from brigade import fleet_action_sources as sources
    from tests.test_fleet_sync import _event

    conn = fleet_hub.init_db(tmp_path / "hub.db")
    now = datetime.now(timezone.utc)
    event = {**_event(node="fixture-node", run_id="fixture-run", state=state), "ts": now.isoformat(), "exit_status": 0}
    fleet_hub.store_events(conn, [event], caller_node="fixture-node")
    snapshot = sources.resolve(conn, {"kind": "run", "node_id": "fixture-node", "run_id": "fixture-run"})
    assert sources.recovered(snapshot, after=now.timestamp() - 1) is False
    conn.close()


def test_expired_unstarted_claim_retries_with_new_fence_and_target_revocation(tmp_path):
    from brigade import fleet_hub_actions as actions, fleet_action_targets as targets
    from brigade.fleet_action_auth import ActionError

    conn, _, grant, request = setup_action(tmp_path)
    action = actions.submit(conn, grant, request)
    first = actions.claim(conn, "node-fixture")
    with conn:
        conn.execute("UPDATE agent_actions SET claim_until=0")
    second = actions.claim(conn, "node-fixture")
    assert second["generation"] == first["generation"] + 1
    with pytest.raises(ActionError, match="fence-refused"):
        actions.start(conn, "node-fixture", action["action_id"], first["generation"])
    targets.sync(conn, "node-fixture", [])
    with pytest.raises(ActionError, match="scope-revoked"):
        actions.start(conn, "node-fixture", action["action_id"], second["generation"])
    assert actions.show(conn, action["action_id"])["status"] == "expired"
    conn.close()
