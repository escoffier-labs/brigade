"""CLI transport for `brigade fleet owners`."""

from __future__ import annotations

import json
from io import StringIO

from brigade import cli, fleet_client_policy
from brigade.fleet_client_policy import FleetPolicyClientError


def _run(monkeypatch, argv: list[str]) -> tuple[int, str, str]:
    out = StringIO()
    err = StringIO()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)
    try:
        rc = cli.main(["fleet", "owners", *argv])
    except SystemExit as exc:
        rc = int(exc.code or 0)
    return rc, out.getvalue(), err.getvalue()


def test_help_lists_show(monkeypatch):
    rc, out, err = _run(monkeypatch, ["--help"])
    assert rc == 0
    assert "show" in out + err


def test_show_prints_the_hub_payload_as_json(monkeypatch):
    payload = {"schema": "brigade.fleet_owners.v1", "policy_version": 4, "owners": [], "unassigned": [], "overlaps": []}
    monkeypatch.setattr(fleet_client_policy, "show_owners", lambda: payload)
    rc, out, err = _run(monkeypatch, ["show"])
    assert rc == 0 and err == ""
    assert json.loads(out) == payload


def test_show_reports_a_structured_error_without_a_token(monkeypatch):
    def refused():
        raise FleetPolicyClientError("auth-failed", "hub returned HTTP 401")

    monkeypatch.setattr(fleet_client_policy, "show_owners", refused)
    rc, out, err = _run(monkeypatch, ["show"])
    assert rc == 1
    body = json.loads(out or err)
    assert body["error"]["code"] == "auth-failed"
    assert "token" not in json.dumps(body).lower()


def test_show_owners_reads_get_owners_with_the_node_token(monkeypatch):
    seen = {}

    def fake_request(method, path, *, token, hub_url, **kwargs):
        seen.update(method=method, path=path, token=token, hub_url=hub_url)
        return 200, {"schema": "brigade.fleet_owners.v1"}

    monkeypatch.setattr(fleet_client_policy, "_hub_and_token", lambda *, admin: ("https://hub.example", "node-secret"))
    monkeypatch.setattr(fleet_client_policy, "_request", fake_request)
    assert fleet_client_policy.show_owners() == {"schema": "brigade.fleet_owners.v1"}
    assert seen == {"method": "GET", "path": "/owners", "token": "node-secret", "hub_url": "https://hub.example"}


def test_an_older_hub_without_the_route_maps_to_policy_unsupported(monkeypatch):
    monkeypatch.setattr(fleet_client_policy, "_hub_and_token", lambda *, admin: ("https://hub.example", "node-secret"))
    monkeypatch.setattr(fleet_client_policy, "_request", lambda *args, **kwargs: (404, {"error": "not found"}))
    try:
        fleet_client_policy.show_owners()
    except FleetPolicyClientError as exc:
        assert exc.code == "policy-unsupported"
    else:
        raise AssertionError("expected FleetPolicyClientError")
