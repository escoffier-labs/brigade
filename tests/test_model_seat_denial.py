"""Offline local worker model-seat denials, distinct from cloud admission."""

import json

import pytest

from brigade import agents, fleet_client, fleet_client_cloud, fleet_hub, fleet_hub_model_roster
from tests.run_test_helpers import run_aboyeur_guarded
from tests.test_fleet_model_admission import (
    _mock_exact_runtime_admission,
    _roster,
    _versioned_seat,
    _versioned_snapshot,
)

NODE = "11111111-1111-4111-8111-111111111111"
SEAT = "cursor_grok"
MODEL = "cursor-grok-4.6-high-fast"
PRIVATE_RESPONSE = "private-response-must-not-appear\nBearer fake-secret"


@pytest.fixture
def offline_client(monkeypatch):
    monkeypatch.setattr(
        fleet_client_cloud,
        "load_fleet_config",
        lambda: {"hub_url": "https://hub.example.invalid", "token": "fake-node-token"},
    )
    monkeypatch.setattr(fleet_client_cloud, "resolve_node_id", lambda: NODE)
    # Run the offline transport directly, without network or deadline workers.
    monkeypatch.setattr(fleet_client_cloud, "_run_with_deadline", lambda fn, **kwargs: fn())


@pytest.mark.parametrize("condition", ["disabled", "capacity"])
def test_local_worker_lease_denial_survives_preflight_without_launch_or_retry(
    offline_client, monkeypatch, tmp_path, capsys, condition
):
    conn = fleet_hub.init_db(tmp_path / "fleet.db")
    try:
        fleet_hub_model_roster.set_model_policy(
            conn,
            {
                "action": "set",
                "expected_revision": 1,
                "seat": SEAT,
                "provider": "cursor",
                "model": MODEL,
                "reasoning": "high",
                "enabled": condition != "disabled",
                "limit": 1,
                "brigade_cli": "cursor-agent",
            },
        )
        if condition == "capacity":
            status, payload = fleet_hub_model_roster.handle_model_policy(
                conn,
                {
                    "action": "acquire",
                    "seat": SEAT,
                    "provider": "cursor",
                    "model": MODEL,
                    "node_id": NODE,
                    "lease_id": "existing-lease",
                    "holder": "fake-holder",
                },
                caller_node=NODE,
            )
            assert status == 200 and payload["acquired"] is True
        snapshot = _versioned_snapshot(_versioned_seat(SEAT, "cursor", MODEL), expires_at="2099-08-30T14:15:00Z")
        # A stale enabled snapshot lets the worker reach the current lease policy.
        monkeypatch.setattr(fleet_client, "load_model_policy_snapshot", lambda: snapshot)
        _mock_exact_runtime_admission(monkeypatch, snapshot)
        requests = []

        def post(hub, token, body, **kwargs):
            requests.append(body)
            # Worker preflight runs in a dispatch thread with its own connection.
            worker_conn = fleet_hub.open_db(tmp_path / "fleet.db")
            try:
                status, payload = fleet_hub_model_roster.handle_model_policy(worker_conn, body, caller_node=NODE)
            finally:
                worker_conn.close()
            assert status == 409 and payload["acquired"] is False
            return status, {
                **payload,
                "error": PRIVATE_RESPONSE,
                "seat": PRIVATE_RESPONSE,
                "provider": PRIVATE_RESPONSE,
                "holder": PRIVATE_RESPONSE,
            }

        monkeypatch.setattr(fleet_client_cloud, "_post_model_policy_blocking", post)

        def forbidden(*args, **kwargs):
            pytest.fail("a refused model seat must not launch or consult cloud admission")

        monkeypatch.setattr(agents, "run_agent", forbidden)
        # Preserve existing cleanup of the locally minted fence, even on denial.
        released = []
        monkeypatch.setattr(
            fleet_client,
            "release_model_lease",
            lambda lease_id, *, holder: (
                released.append((lease_id, holder)) or fleet_client.ModelLeaseDecision(False, "refused")
            ),
        )
        monkeypatch.setattr(fleet_client, "admit_cloud", forbidden)
        run_dir = tmp_path / "run"
        result = run_aboyeur_guarded(
            "implement it",
            _roster(),
            worker=SEAT,
            output_dir=run_dir,
            code_graph_enabled=False,
            route_enabled=False,
        )
        output = capsys.readouterr()
        reason = "seat-disabled" if condition == "disabled" else "seat-capacity-exhausted"
        diagnostic = f"fleet model policy denied seat '{SEAT}' (provider 'cursor'): {reason}"
        assert result != 0
        assert diagnostic in output.err
        receipt = json.loads((run_dir / "run.json").read_text())
        assert receipt["error"] == diagnostic
        assert receipt["failure"] == {
            "detail": diagnostic,
            "kind": "fleet-model-policy",
            "phase": "preflight",
            "seat": SEAT,
        }
        assert len(requests) == 1 and requests[0]["action"] == "acquire"
        assert released == [(requests[0]["lease_id"], requests[0]["holder"])]
        assert conn.execute("SELECT COUNT(*) FROM model_leases WHERE released_at IS NULL").fetchone()[0] == (
            1 if condition == "capacity" else 0
        )
        for artifact in run_dir.rglob("*.json"):
            assert PRIVATE_RESPONSE.splitlines()[0] not in artifact.read_text()
        assert PRIVATE_RESPONSE.splitlines()[0] not in output.out + output.err
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (409, {"acquired": False, "reason_code": "seat-disabled", "error": PRIVATE_RESPONSE}, "seat-disabled"),
        (409, {"acquired": False, "error": "model policy capacity is exhausted"}, "seat-capacity-exhausted"),
        (409, {"acquired": False, "error": "model policy denied lease"}, "refused"),
        (409, {"acquired": False, "reason_code": PRIVATE_RESPONSE, "error": PRIVATE_RESPONSE}, "refused"),
        (409, {"acquired": False, "reason_code": ["seat-disabled"]}, "refused"),
        (409, {"acquired": False, "reason_code": "provider-disabled"}, "refused"),
        (409, [], "malformed-response"),
        (409, {}, "malformed-response"),
        (200, {"acquired": "true", "error": PRIVATE_RESPONSE}, "malformed-response"),
        (401, {"reason_code": "seat-disabled", "error": PRIVATE_RESPONSE}, "auth-failed"),
        (403, [], "auth-failed"),
        (500, {"acquired": False, "reason_code": "seat-disabled"}, "refused"),
    ],
)
def test_model_lease_denial_parser_is_bounded_and_fails_closed(offline_client, monkeypatch, status, payload, expected):
    monkeypatch.setattr(fleet_client_cloud, "_post_model_policy_blocking", lambda *args, **kwargs: (status, payload))
    decision = fleet_client.acquire_model_lease(SEAT, "cursor", MODEL, lease_id="fake-lease", holder="fake-holder")
    assert decision.granted is False
    assert decision.reason == expected
    assert decision.lease_id == "fake-lease" and decision.holder == "fake-holder"


@pytest.mark.parametrize("action,key", [("acquire", "acquired"), ("release", "released")])
def test_model_lease_success_and_release_semantics_are_unchanged(offline_client, monkeypatch, action, key):
    monkeypatch.setattr(
        fleet_client_cloud,
        "_post_model_policy_blocking",
        lambda *args, **kwargs: (200, {key: True, "reason_code": "seat-disabled", "error": PRIVATE_RESPONSE}),
    )
    decision = fleet_client_cloud._model_lease_op(action, lease_id="fake-lease", holder="fake-holder")
    assert decision.granted is True and decision.reason == "ok"
