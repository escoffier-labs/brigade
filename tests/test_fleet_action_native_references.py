"""T3-native references survive Hub storage and recovery without another submit."""

import json

import pytest

from brigade import fleet_action_adapters as adapters
from brigade import fleet_action_auth as auth
from brigade import fleet_action_runner as runner
from brigade import fleet_action_sources as sources
from brigade import fleet_client_actions as client
from brigade import fleet_hub, fleet_hub_actions as actions
from brigade.fleet_action_auth import ActionError
from brigade.fleet_action_http import handle_api
from tests.test_fleet_action_adapter_bounds import binding, json_executable, t3_action
from tests.test_fleet_action_openclaw_bots import roster_target as roster_target
from tests.test_fleet_action_runner import fake_executable, manifest
from tests.test_fleet_sync import _event

REFERENCE = "a" * 64 + "-" + "b" * 64 + "-12345678-1234-1234-1234-123456789abc"


@pytest.fixture
def t3_broker(roster_target, tmp_path, monkeypatch):
    conn = roster_target
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
    source = {"kind": "run", "node_id": "node-fixture", "run_id": "fixture-run"}
    auth.grant_broker(conn, "node-fixture", ["repair-t3"], [source])
    grant = auth.grant_operator(conn, "operator@example.test", [{"target_id": "repair-t3", "subject": source}])
    target = json.loads(conn.execute("SELECT document FROM action_targets").fetchone()[0])
    target["subjects"] = [source]
    exe, log = fake_executable(tmp_path, {})
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
            "source": source,
            "observed_revision": sources.resolve(conn, source)["revision"],
            "target_id": "repair-t3",
            "model_id": "fixture-seat",
            "intent": "repair",
            "prompt": "Fixture repair",
            "client_id": "fixture-native-ref",
        },
    )
    claimed = actions.claim(conn, "node-fixture")
    return conn, path, exe, log, action, claimed["generation"]


@pytest.mark.parametrize("unknown_error", [False, True])
def test_full_length_t3_handle_survives_report_readback_and_recovery(t3_broker, unknown_error):
    conn, path, exe, log, action, generation = t3_broker
    assert len(REFERENCE) == 166
    payload = {"schema": "t3-fleet.result.v1", "request_id": REFERENCE, "state": "running"}
    if unknown_error:
        payload = {
            "schema": "t3-fleet.error.v1",
            "request_id": REFERENCE,
            "error": {"code": "unknown_state", "message": "private provider detail"},
        }
    fake_executable(path.parent, payload, name=exe.name)
    if unknown_error:
        exe.write_text(exe.read_text() + "\nsys.exit(1)\n")
    first = runner.execute(path, action["action_id"], generation)
    assert first["status"] == ("unknown" if unknown_error else "running")
    assert first["destination_ref"] == REFERENCE
    assert actions.show(conn, action["action_id"])["destination_ref"] == REFERENCE
    fake_executable(
        path.parent, {"schema": "t3-fleet.result.v1", "request_id": REFERENCE, "state": "succeeded"}, name=exe.name
    )
    final = runner.execute(path, action["action_id"], generation)
    assert final["status"] == "finished" and final["verification"] == "pending"
    assert final["destination_ref"] == REFERENCE
    calls = [json.loads(line)["args"] for line in log.read_text().splitlines()]
    assert len(calls) == 2 and calls[0][0] == "submit"
    assert "--request-id" not in calls[0]
    assert calls[1] == ["recover" if unknown_error else "status", "--request-id", REFERENCE]
    assert "private provider detail" not in auth.canonical(final)


@pytest.mark.parametrize("schema", [None, "wrong.result.v9", "t3-fleet.error.v1"])
def test_t3_success_requires_result_schema(tmp_path, schema):
    payload = {
        "request_id": "fixture-host-fixture-controller-12345678-1234-1234-1234-123456789abc",
        "state": "succeeded",
    }
    if schema is not None:
        payload["schema"] = schema
    exe = json_executable(tmp_path, payload)
    with pytest.raises(ActionError, match="transport-acceptance-unknown"):
        adapters.dispatch(t3_action(), binding(), exe, tmp_path / "prompt")


@pytest.mark.parametrize("reference", [REFERENCE + "x", REFERENCE + "\n", "peer:fixture", "x" * 166])
def test_hub_rejects_non_native_t3_reference_grammar(t3_broker, reference):
    conn, _, _, _, action, generation = t3_broker
    actions.start(conn, "node-fixture", action["action_id"], generation)
    with pytest.raises(ActionError, match="invalid-t3-reference"):
        actions.report(
            conn,
            "node-fixture",
            action["action_id"],
            generation,
            {
                "event_id": "fixture-receipt",
                "status": "running",
                "destination_ref": reference,
            },
        )
    assert actions.show(conn, action["action_id"])["destination_ref"] is None


def test_long_t3_grammar_does_not_widen_generic_identities_or_other_providers(tmp_path):
    from tests.test_fleet_action_openclaw_bots import setup_action_schema

    with pytest.raises(ActionError, match="invalid-identity"):
        auth.opaque(REFERENCE)
    conn, _, grant, request = setup_action_schema(tmp_path)
    action = actions.submit(conn, grant, request)
    generation = actions.claim(conn, "node-fixture")["generation"]
    actions.start(conn, "node-fixture", action["action_id"], generation)
    with pytest.raises(ActionError, match="invalid-identity"):
        actions.report(
            conn,
            "node-fixture",
            action["action_id"],
            generation,
            {
                "event_id": "fixture-receipt",
                "status": "running",
                "destination_ref": REFERENCE,
            },
        )
    conn.close()


@pytest.mark.parametrize("code", ["invalid_config", "invalid_request", "policy_rejected", "target_busy"])
def test_proven_preaccept_refusal_is_closed_failed_without_native_detail(tmp_path, code):
    exe = json_executable(
        tmp_path, {"schema": "t3-fleet.error.v1", "error": {"code": code, "message": "private detail"}}, code=3
    )
    assert adapters.dispatch(t3_action(), binding(), exe, tmp_path / "prompt") == {
        "status": "failed",
        "rejection_code": code,
    }


@pytest.mark.parametrize("code", ["host_unavailable", "internal_error", "unknown_state", "unrecognized"])
def test_unproven_or_ambiguous_refusal_keeps_unknown_acceptance(tmp_path, code):
    exe = json_executable(tmp_path, {"schema": "t3-fleet.error.v1", "error": {"code": code}}, code=3)
    with pytest.raises(ActionError, match="transport-acceptance-unknown"):
        adapters.dispatch(t3_action(), binding(), exe, tmp_path / "prompt")


def test_t3_submit_deadline_leaves_lease_margin(tmp_path, monkeypatch):
    observed = []

    def invoke(executable, argv, *, timeout):
        observed.append(timeout)
        return {"schema": "t3-fleet.result.v1", "request_id": REFERENCE, "state": "running"}

    monkeypatch.setattr(adapters, "invoke", invoke)
    adapters.dispatch(t3_action(), binding(), tmp_path / "fixture", tmp_path / "prompt")
    assert observed == [180]


@pytest.mark.parametrize("exit_code", [0, 1])
def test_openclaw_failed_turn_retains_its_real_native_run_id(tmp_path, exit_code):
    exe = json_executable(tmp_path, {"status": "error", "runId": "fixture-failed-native-run"}, code=exit_code)
    action = {"action_id": "aa_fixture", "request": {"target": {"family": "openclaw"}}}
    assert adapters.dispatch(action, {"agent_id": "fixture-agent"}, exe, tmp_path / "prompt") == {
        "status": "failed",
        "destination_ref": "fixture-failed-native-run",
    }
