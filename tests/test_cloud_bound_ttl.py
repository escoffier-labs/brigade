"""Bound cloud leases outlive the 5 minute admission TTL (#1794 item 1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from brigade import cloud_tracker, fleet_client, fleet_command_deck as deck, fleet_hub


NODE_A = "11111111-1111-4111-8111-111111111111"
CLOCK_START = 1_700_000_000.0


@pytest.fixture()
def conn(tmp_path):
    connection = fleet_hub.init_db(tmp_path / "fleet.db")
    yield connection
    connection.close()


@pytest.fixture()
def clock(monkeypatch):
    state = {"now": CLOCK_START}
    monkeypatch.setattr(fleet_hub, "_now_epoch", lambda: state["now"])
    return state


def _admit(lease_id: str = "lease-a", **extra: object) -> dict[str, object]:
    return {
        "action": "admit",
        "provider": "jules",
        "lease_id": lease_id,
        "node_id": NODE_A,
        "holder": "holder-a",
        "repo": "repo-a",
        "label": "safe label",
        "prompt_hash": "a" * 64,
        **extra,
    }


def _bind(lease_id: str = "lease-a", **extra: object) -> dict[str, object]:
    return {
        "action": "bind",
        "lease_id": lease_id,
        "node_id": NODE_A,
        "holder": "holder-a",
        "provider_task_id": "task-1",
        **extra,
    }


def _handle(conn, body: dict[str, object]):
    return fleet_hub.handle_cloud(conn, body, caller_node=NODE_A, config=deck.DeckConfig())


def _admitted(conn, **extra: object) -> dict[str, Any]:
    status, payload = _handle(conn, _admit(ttl_seconds=300, **extra))
    assert status == 200 and payload["admitted"] is True
    return payload["lease"]


def test_bind_with_ttl_extends_the_lease(conn, clock):
    admitted = _admitted(conn)
    clock["now"] += 100
    status, payload = _handle(conn, _bind(ttl_seconds=21600))
    assert status == 200 and payload["bound"] is True
    lease = payload["lease"]
    assert lease["state"] == "bound"
    assert lease["ttl_seconds"] == 21600
    assert lease["renewed_at"] != admitted["renewed_at"]
    assert lease["expires_at"] == fleet_hub._epoch_to_iso(CLOCK_START + 100 + 21600)
    # Still visible long after the original 5 minute window closed.
    clock["now"] += 3600
    assert [row["lease_id"] for row in fleet_hub.list_cloud_leases(conn)] == ["lease-a"]


def test_bind_without_ttl_leaves_the_lease_clock_untouched(conn, clock):
    admitted = _admitted(conn)
    clock["now"] += 100
    status, payload = _handle(conn, _bind())
    assert status == 200 and payload["bound"] is True
    lease = payload["lease"]
    assert lease["ttl_seconds"] == 300
    assert lease["renewed_at"] == admitted["renewed_at"]
    assert lease["expires_at"] == admitted["expires_at"]


@pytest.mark.parametrize("bad", [0, -1, 86401, "3600", 3600.5, True, None])
def test_bind_rejects_an_invalid_ttl(conn, clock, bad):
    _admitted(conn)
    with pytest.raises(fleet_hub.FleetHubError, match="ttl_seconds"):
        _handle(conn, _bind(ttl_seconds=bad))


def test_bind_with_ttl_still_refuses_an_expired_lease(conn, clock):
    _admitted(conn)
    clock["now"] += 301
    status, payload = _handle(conn, _bind(ttl_seconds=21600))
    assert status == 409 and payload["bound"] is False
    assert fleet_hub.list_cloud_leases(conn) == []


def test_bind_with_ttl_still_refuses_a_released_or_fenced_lease(conn, clock):
    _admitted(conn)
    status, payload = _handle(conn, _bind(ttl_seconds=21600, holder="intruder"))
    assert status == 409 and payload["bound"] is False
    status, payload = _handle(conn, {**_bind(), "action": "release", "state": "released"})
    assert status == 200 and payload["released"] is True
    status, payload = _handle(conn, _bind(ttl_seconds=21600))
    assert status == 409 and payload["bound"] is False


def test_older_hub_parser_ignores_unknown_bind_fields(conn, clock):
    """Pins why no client retry exists: an older hub never rejects ``ttl_seconds`` on bind.

    The request parser reads named fields only, so a bind carrying an unknown
    key validates exactly like one without it. An older hub therefore binds and
    keeps the admission TTL instead of failing the launch.
    """
    _admitted(conn)
    request = fleet_hub._validate_cloud_request({**_bind(), "field_from_a_newer_client": 7})
    assert request["action"] == "bind" and request["provider_task_id"] == "task-1"
    assert "field_from_a_newer_client" not in request


def _capture_bind_bodies(monkeypatch) -> list[dict[str, Any]]:
    bodies: list[dict[str, Any]] = []

    def capture_post(_hub, _token, body, *, timeout):
        bodies.append(dict(body))
        return 200, {"bound": True, "lease": {"lease_id": body["lease_id"], "provider": "jules"}}

    monkeypatch.setattr(fleet_client, "load_fleet_config", lambda: {"hub_url": "http://hub", "token": "token"})
    monkeypatch.setattr(fleet_client, "resolve_node_id", lambda _base=None: NODE_A)
    monkeypatch.setattr(fleet_client, "_post_cloud_blocking", capture_post)
    return bodies


def test_bind_cloud_sends_ttl_only_when_given(monkeypatch):
    bodies = _capture_bind_bodies(monkeypatch)
    assert fleet_client.bind_cloud("lease-a", "task-1", holder="holder-a").granted is True
    assert "ttl_seconds" not in bodies[0]
    assert fleet_client.bind_cloud("lease-a", "task-1", holder="holder-a", ttl_seconds=21600).granted is True
    assert bodies[1]["ttl_seconds"] == 21600


def test_bound_ttl_constant_matches_the_tracker_stale_threshold():
    assert fleet_client.CLOUD_BOUND_TTL_SECONDS == 21600
    assert fleet_client.CLOUD_BOUND_TTL_SECONDS == cloud_tracker.DEFAULT_STALE_READY_HOURS * 3600


def test_sync_renews_active_leases_with_the_bound_ttl(monkeypatch, tmp_path: Path):
    now = cloud_tracker._parse_time("2026-08-12T20:00:00Z")
    cloud_tracker.register(
        tmp_path,
        provider="jules",
        task_id="sess-long",
        label="jules:owner/repo@001122334455",
        prompt_hash="sha256:" + "b" * 64,
        lease_holder="holder-a",
        dispatched_at="2026-08-12T19:59:00Z",
    )
    renew_calls: list[dict[str, Any]] = []

    def capture_renew(lease_id: str, **kwargs: Any):
        renew_calls.append({"lease_id": lease_id, **kwargs})
        return fleet_client.CloudDecision(True, "ok")

    monkeypatch.setattr(fleet_client, "renew_cloud", capture_renew)
    payload = cloud_tracker.sync_payload(
        tmp_path,
        now=now,
        provider_tasks={"sess-long": {"state": "running"}},
        github={"branches": [], "prs": []},
        hub_leases=[{"lease_id": "lease-long", "provider_task_id": "sess-long"}],
    )
    assert payload["counts"]["renewed"] == 1
    assert renew_calls == [
        {"lease_id": "lease-long", "holder": "holder-a", "ttl_seconds": fleet_client.CLOUD_BOUND_TTL_SECONDS}
    ]
