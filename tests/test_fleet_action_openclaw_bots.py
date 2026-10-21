"""Exact bot destinations stay inside protected bindings and permanent identities."""

import json
import os

import pytest

from brigade import fleet_action_adapters as adapters
from brigade import fleet_action_auth as auth
from brigade import fleet_action_runner as runner
from brigade import fleet_action_targets as targets
from brigade import fleet_client_actions as client
from brigade import fleet_hub_actions as actions
from brigade import fleet_hub
from brigade.fleet_action_auth import ActionError, canonical, digest
from brigade.fleet_action_http import handle_api
from tests.test_fleet_action_runner import fake_executable, manifest
from tests.test_fleet_actions import setup_action

BOT_KEY = "agent:fixture-agent:clawchat:A1111111-b222-4333-8444-555555555555"
OTHER_KEY = "agent:fixture-agent:clawchat:aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


requires_posix = pytest.mark.skipif(
    os.name != "posix", reason="Broker execution requires POSIX private files, process groups, and systemd"
)


def setup_action_schema(tmp_path):
    # The isolated adapter checkout intentionally lacks parent-owned Hub wiring.
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    actions.ensure_schema(conn)
    conn.commit()
    conn.close()
    return setup_action(tmp_path)


@pytest.fixture
def bot(tmp_path, monkeypatch):
    conn, _, _, request = setup_action_schema(tmp_path)
    original = json.loads(conn.execute("SELECT document FROM action_targets").fetchone()[0])
    target = {**original, "target_id": "fixture-bot", "label": "Fixture bot", "session_key": BOT_KEY}
    auth.grant_broker(conn, "node-fixture", ["repair", "fixture-bot"], [request["source"]])
    grant = auth.grant_operator(
        conn, "operator@example.test", [{"target_id": "fixture-bot", "subject": request["source"]}]
    )
    exe, log = fake_executable(tmp_path, {"status": "ok", "runId": "fixture-native-run"})
    path = manifest(tmp_path, exe, target, binding={"agent_id": "fixture-agent", "session_key": BOT_KEY})
    monkeypatch.setattr(client, "request", lambda body, **kwargs: handle_api(conn, (False, "node-fixture"), body))
    yield conn, path, exe, log, grant, {**request, "target_id": "fixture-bot"}
    conn.close()


@requires_posix
def test_chosen_bot_runs_exact_native_session_once_and_keeps_run_handle(bot):
    conn, path, _, log, grant, request = bot
    runner.sync_targets(path)
    action = actions.submit(conn, grant, request)
    claimed = actions.claim(conn, "node-fixture")
    result = runner.execute(path, action["action_id"], claimed["generation"])
    assert result["status"] == "finished" and result["verification"] == "pending"
    assert result["destination_ref"] == "fixture-native-run"
    record = json.loads(log.read_text())
    assert record["args"] == [
        "agent",
        "--agent",
        "fixture-agent",
        "--session-key",
        BOT_KEY,
        "--message-file",
        str(path.parent / "journal" / (action["action_id"] + ".prompt")),
        "--timeout",
        "3500",
        "--json",
    ]
    runner.execute(path, action["action_id"], claimed["generation"])
    assert len(log.read_text().splitlines()) == 1
    assert result["request"]["target"]["session_key"] == BOT_KEY
    loaded = runner.load_manifest(path)["targets"][0]["public"]
    assert loaded["binding_digest"] == digest(
        canonical(
            {
                "family": "openclaw",
                "agent_id": "fixture-agent",
                "session_key": BOT_KEY,
            }
        )
    )


@pytest.mark.parametrize("replacement", [OTHER_KEY, None])
def test_bot_target_id_cannot_rebind_after_withdrawal(bot, replacement):
    conn, path, _, _, _, _ = bot
    public = json.loads(path.read_text())["targets"][0]["public"]
    targets.sync(
        conn, "node-fixture", [{**public, "binding_digest": digest(canonical(targets.native_identity(public)))}]
    )
    original = targets.available(conn)[0]
    original.pop("node_id")
    changed = {**original, "session_key": replacement}
    if replacement is None:
        changed.pop("session_key")
    targets.sync(conn, "node-fixture", [])
    with pytest.raises(ActionError, match="target-identity-conflict"):
        targets.sync(conn, "node-fixture", [changed])
    targets.sync(conn, "node-fixture", [original])
    assert targets.available(conn)[0]["session_key"] == BOT_KEY


@pytest.mark.parametrize(
    "key",
    [
        "agent:other-agent:clawchat:11111111-2222-4333-8444-555555555555",
        BOT_KEY + ":extra",
        BOT_KEY + "\n",
        " " + BOT_KEY,
        "agent:fixture-agent:clawchat:not-a-uuid",
        BOT_KEY.replace(":clawchat:", ":main:"),
        "",
        None,
        123,
        "x" * 129,
    ],
)
@pytest.mark.parametrize("boundary", ["hub", pytest.param("manifest", marks=requires_posix)])
def test_manifest_and_hub_reject_invalid_bot_session_keys(bot, boundary, key):
    conn, path, _, _, _, _ = bot
    data = json.loads(path.read_text())
    data["targets"][0]["public"]["session_key"] = key
    data["targets"][0]["binding"]["session_key"] = key
    path.write_text(json.dumps(data))
    if boundary == "manifest":
        with pytest.raises(ActionError, match="invalid-openclaw-session-key"):
            runner.load_manifest(path)
        return
    public = {**data["targets"][0]["public"], "binding_digest": "a" * 64}
    with pytest.raises(ActionError, match="invalid-openclaw-session-key"):
        targets.sync(conn, "node-fixture", [public])
    assert conn.execute("SELECT count(*) FROM action_targets").fetchone()[0] == 1


@pytest.mark.parametrize("agent", ["fixture.agent", "fixture:agent", "-fixture", "a" * 65])
@pytest.mark.parametrize("boundary", ["hub", pytest.param("manifest", marks=requires_posix)])
def test_manifest_and_hub_reject_non_native_agent_syntax(bot, boundary, agent):
    conn, path, _, _, _, _ = bot
    data = json.loads(path.read_text())
    for part in ("public", "binding"):
        data["targets"][0][part].update(agent_id=agent, session_key=BOT_KEY.replace("fixture-agent", agent))
    path.write_text(json.dumps(data))
    if boundary == "manifest":
        with pytest.raises(ActionError, match="invalid-openclaw-agent"):
            runner.load_manifest(path)
        return
    with pytest.raises(ActionError, match="invalid-openclaw-agent"):
        targets.sync(conn, "node-fixture", [{**data["targets"][0]["public"], "binding_digest": "a" * 64}])


@pytest.mark.parametrize("public_key,binding_key", [(BOT_KEY, OTHER_KEY), (BOT_KEY, None), (None, BOT_KEY)])
@requires_posix
def test_manifest_refuses_different_or_missing_public_bot_identity(bot, public_key, binding_key):
    _, path, _, _, _, _ = bot
    data = json.loads(path.read_text())
    for part, key in (("public", public_key), ("binding", binding_key)):
        if key is None:
            data["targets"][0][part].pop("session_key")
        else:
            data["targets"][0][part]["session_key"] = key
    path.write_text(json.dumps(data))
    with pytest.raises(ActionError, match="native-binding-mismatch"):
        runner.load_manifest(path)


def test_bot_key_cannot_be_supplied_by_operator_submission(bot):
    conn, path, _, _, grant, request = bot
    public = json.loads(path.read_text())["targets"][0]["public"]
    targets.sync(
        conn, "node-fixture", [{**public, "binding_digest": digest(canonical(targets.native_identity(public)))}]
    )
    with pytest.raises(ActionError, match="invalid-submission"):
        actions.submit(conn, grant, {**request, "session_key": OTHER_KEY})
    assert conn.execute("SELECT count(*) FROM agent_actions").fetchone()[0] == 0


def test_bot_catalog_requires_owned_enrolled_broker_and_explicit_target_grant(bot):
    conn, path, _, _, _, request = bot
    public = {**json.loads(path.read_text())["targets"][0]["public"], "binding_digest": "a" * 64}
    for caller in ((True, None), (False, None), (False, "unknown-node")):
        with pytest.raises(ActionError, match="enrolled-broker-required"):
            handle_api(conn, caller, {"op": "targets-sync", "targets": [public]})
    auth.grant_broker(conn, "node-fixture", ["repair"], [request["source"]])
    with pytest.raises(ActionError, match="broker-scope-refused"):
        handle_api(conn, (False, "node-fixture"), {"op": "targets-sync", "targets": [public]})


@requires_posix
def test_bot_key_does_not_replace_missing_native_run_id_or_allow_resubmit(bot, monkeypatch):
    conn, path, exe, log, grant, request = bot
    fake_executable(path.parent, {"status": "completed", "sessionKey": BOT_KEY}, name=exe.name)
    runner.sync_targets(path)
    action = actions.submit(conn, grant, request)
    claimed = actions.claim(conn, "node-fixture")
    result = runner.execute(path, action["action_id"], claimed["generation"])
    assert result["status"] == "unknown" and result["destination_ref"] is None

    def forbidden(*args, **kwargs):
        pytest.fail("unknown acceptance without runId must never dispatch or recover")

    monkeypatch.setattr(adapters, "dispatch", forbidden)
    monkeypatch.setattr(adapters, "recover", forbidden)
    runner.execute(path, action["action_id"], claimed["generation"])
    assert len(log.read_text().splitlines()) == 1


@requires_posix
def test_generic_agent_manifest_identity_and_action_session_are_unchanged(tmp_path):
    conn, _, _, _ = setup_action_schema(tmp_path)
    target = json.loads(conn.execute("SELECT document FROM action_targets").fetchone()[0])
    exe, log = fake_executable(tmp_path, {"status": "ok", "runId": "fixture-native-run"})
    path = manifest(tmp_path, exe, target)
    public = runner.load_manifest(path)["targets"][0]["public"]
    assert targets.native_identity(public) == {"family": "openclaw", "agent_id": "fixture-agent"}
    assert public["binding_digest"] == digest(canonical({"family": "openclaw", "agent_id": "fixture-agent"}))
    prompt = tmp_path / "prompt"
    prompt.write_text("Fixture prompt")
    receipt = adapters.dispatch(
        {"action_id": "aa_fixture", "request": {"target": public}}, {"agent_id": "fixture-agent"}, exe, prompt
    )
    assert receipt == {"status": "finished", "destination_ref": "fixture-native-run"}
    argv = json.loads(log.read_text())["args"]
    assert argv[argv.index("--session-key") + 1] == "brigade-action-aa_fixture"
    conn.close()


@pytest.fixture
def roster_target(tmp_path):
    from brigade import fleet_hub_model_roster as roster

    conn, _, _, request = setup_action_schema(tmp_path)
    roster.set_model_policy(
        conn,
        {
            "action": "set",
            "expected_revision": roster.raw_revision(conn),
            "provider": "openai",
            "model": "fixture-current-model",
            "seat": "fixture-seat",
            "enabled": True,
            "reasoning": "high",
            "t3_instance_id": "fixture-instance",
            "t3_model": "fixture-native-model",
        },
    )
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
    auth.grant_broker(conn, "node-fixture", ["repair-t3"], [request["source"]])
    targets.sync(conn, "node-fixture", [target])
    yield conn
    conn.close()


def test_t3_picker_uses_current_roster_when_legacy_policy_lacks_seat(roster_target):
    from brigade import fleet_hub_policy

    conn = roster_target
    assert "fixture-seat" not in fleet_hub_policy.current_policy(conn)["document"]["seats"]
    before = conn.total_changes
    assert targets.available(conn)[0]["models"] == [{"id": "fixture-seat", "label": "fixture-current-model"}]
    assert conn.total_changes == before  # Listing never admits, reserves, or mutates authority.
    with conn:
        conn.execute("UPDATE model_policy SET model='fixture-fresh-label' WHERE seat='fixture-seat'")
    assert targets.available(conn)[0]["models"] == [{"id": "fixture-seat", "label": "fixture-fresh-label"}]


@pytest.mark.parametrize("denial", ["disabled", "missing-binding", "retired-model", "retired-native", "missing-seat"])
def test_t3_picker_refuses_unavailable_roster_seats(roster_target, denial):
    conn = roster_target
    assert targets.available(conn)  # A denied case must first have a reachable target.
    with conn:
        if denial == "disabled":
            conn.execute("UPDATE model_policy SET enabled=0 WHERE seat='fixture-seat'")
        elif denial == "missing-binding":
            conn.execute("UPDATE model_policy SET t3_instance_id='', t3_model='' WHERE seat='fixture-seat'")
        elif denial == "missing-seat":
            conn.execute("DELETE FROM model_policy WHERE seat='fixture-seat'")
        else:
            model = "fixture-native-model" if denial == "retired-native" else "fixture-current-model"
            conn.execute(
                "INSERT INTO retired_models VALUES ('openai', ?, 'exact', 0, 'operator-retired', '2026-01-01T00:00:00Z')",
                (model,),
            )
    assert targets.available(conn) == []


def test_t3_picker_respects_activated_policy_constraints_and_consumer_bindings(roster_target):
    from brigade import fleet_hub_model_roster as roster, fleet_hub_policy, fleet_policy_migration

    conn = roster_target
    annotations = {"seats": {"fixture-seat": {"pinned": False, "training_allowed": False}}}
    version = fleet_hub_policy.current_policy(conn)["revision"]
    preview = fleet_policy_migration.preview_migration(
        conn,
        expected_policy_version=version,
        expected_roster_revision=roster.raw_revision(conn),
        annotations=annotations,
    )
    assert preview["ok"], preview["errors"]
    fleet_policy_migration.activate_migration(
        conn,
        expected_policy_version=version,
        expected_roster_revision=roster.raw_revision(conn),
        preview_digest=preview["preview_digest"],
        actor="fixture",
        reason="test authority",
        annotations=annotations,
    )
    assert targets.available(conn)
    current = fleet_hub_policy.current_policy(conn)
    document = current["document"]
    document["seats"]["fixture-seat"]["training_allowed"] = True
    fleet_hub_policy.save_policy(
        conn, document, expected_version=current["revision"], actor="fixture", reason="restrict"
    )
    assert targets.available(conn) == []
    current = fleet_hub_policy.current_policy(conn)
    document = current["document"]
    document["seats"]["fixture-seat"]["training_allowed"] = False
    document["consumers"].setdefault("t3-fleet", {})["seat_bindings"] = {
        "fixture-seat": {"native": {"instance_id": "fixture-other-instance", "model": "fixture-consumer-model"}}
    }
    fleet_hub_policy.save_policy(
        conn, document, expected_version=current["revision"], actor="fixture", reason="consumer binding"
    )
    assert targets.available(conn)
    with conn:
        conn.execute(
            "INSERT INTO retired_models VALUES ('openai', 'fixture-consumer-model', 'exact', 0, 'operator-retired', '2026-01-01T00:00:00Z')"
        )
    assert targets.available(conn) == []
