"""Exact PR #1940 relay output and safe acknowledgement of incomplete events.

ci_relay_1940.json was generated from normalizeEvent at relay head
2e87f6d030e4382c4d0e44265b6102b031f54ad4, then wrapped by listEvents.
Only fake payloads were supplied. Status run_id=null is an upstream limitation.
"""

import copy
import json
from pathlib import Path

import pytest

from brigade import fleet_ci_board as ci, fleet_ci_node as node, fleet_hub
from tests.test_fleet_ci import NOW, check, observation, select_policy

FIXTURE = Path(__file__).parent / "fixtures" / "ci_relay_1940.json"


def relay_page():
    return json.loads(FIXTURE.read_text())


def test_exact_relay_contract_persists_known_objects_and_invalidates_unknown_scope(tmp_path, monkeypatch):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.store(conn, {"events": [check(check_run_id=99)], "repositories": [observation()]}, now=NOW)
    select_policy(conn, "example/public")
    posts = []
    page = relay_page()
    # Extra payload bodies/context fields are outside the minimal relay schema.
    page["events"][1].update(name="ci", app_id=7, output={"text": "example body"})

    def transport(url, *, token, body=None):
        if "/v1/events?" in url:
            return page
        assert token == "example-node"
        posts.append(body)
        return ci.store(conn, body, now=NOW)

    monkeypatch.setattr(node, "fetch_json", transport)
    state = node.NodeState(tmp_path / "ci.db")
    result = node.pull(
        state,
        relay_url="https://relay.example",
        relay_token="example-relay",
        hub_url="https://hub.example",
        node_token="example-node",
    )
    assert result == {"events": 4, "cursor": "5", "incomplete": 3}
    assert state.cursor("https://relay.example") == "5"
    row = ci.load_view(conn, now=NOW)["repositories"][0]
    assert row["latest_run"]["object_id"] == 11
    assert row["latest_run"]["attempt"] == 2
    assert row["latest_run"]["status"] == "completed"
    assert row["pulls"][0]["number"] == 5
    assert row["required"]["state"] == "unknown"
    assert row["pulls"][0]["rollup"]["state"] == "unknown"
    assert row["last_event_received"] == NOW
    assert row["refresh_requested"] is None
    assert ci.work_plan(conn, operator=False)["refresh_requests"] == {}
    rows = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM ci_check_runs")]
    assert any(
        r["event"] == "check_run"
        and r["object_id"] == 22
        and r["attempt"] == 1
        and r["name"] is None
        and r["app_id"] is None
        for r in rows
    )
    assert any(r["event"] == "check_suite" and r["object_id"] == 33 for r in rows)
    assert not any(r["event"] == "status" for r in rows)
    wire = json.dumps(posts)
    assert "example body" not in wire
    assert "example-delivery" not in wire
    conn.close()


@pytest.mark.parametrize("bad_id", [None, "0", "-1", "1.0", True, "9" * 24, "1" * 10000])
def test_incomplete_provider_identity_advances_only_after_unknown_is_durable(tmp_path, monkeypatch, bad_id):
    conn = fleet_hub.init_db(tmp_path / "hub.db")
    ci.store(conn, {"events": [check(check_run_id=99)], "repositories": [observation()]}, now=NOW)
    page = relay_page()
    page["events"] = [page["events"][0]]
    page["events"][0]["run_id"] = bad_id
    page["next_cursor"] = 1
    refuse = [True]

    def transport(url, *, token, body=None):
        if "/v1/events?" in url:
            return page
        if refuse[0]:
            raise ValueError("Hub unavailable")
        return ci.store(conn, body, now=NOW)

    monkeypatch.setattr(node, "fetch_json", transport)
    state = node.NodeState(tmp_path / "ci.db")
    kwargs = dict(
        relay_url="https://relay.example",
        relay_token="example-relay",
        hub_url="https://hub.example",
        node_token="example-node",
    )
    with pytest.raises(ValueError, match="Hub unavailable"):
        node.pull(state, **kwargs)
    assert state.cursor("https://relay.example") == "0"
    refuse[0] = False
    result = node.pull(state, **kwargs)
    assert result == {"events": 0, "cursor": "1", "incomplete": 1}
    assert conn.execute("SELECT count(*) FROM ci_runs").fetchone()[0] == 0
    row = ci.load_view(conn, now=NOW)["repositories"][0]
    assert row["required"]["state"] == "unknown"
    assert row["refresh_requested"] is None
    conn.close()


def test_cursor_requires_acknowledgement_of_invalidation_even_on_http_success(tmp_path, monkeypatch):
    page = relay_page()
    page["events"] = [page["events"][-1]]

    def transport(url, *, token, body=None):
        return page if "/v1/events?" in url else {"events": 0, "repositories": 0}

    monkeypatch.setattr(node, "fetch_json", transport)
    state = node.NodeState(tmp_path / "ci.db")
    with pytest.raises(ValueError, match="invalidation"):
        node.pull(
            state,
            relay_url="https://relay.example",
            relay_token="example-relay",
            hub_url="https://hub.example",
            node_token="example-node",
        )
    assert state.cursor("https://relay.example") == "0"


@pytest.mark.parametrize("field", ["head_sha", "provider_updated_at", "run_id"])
def test_missing_minimal_fact_is_an_unknown_repair_not_a_synthesized_fact(tmp_path, monkeypatch, field):
    page = relay_page()
    page["events"] = [copy.deepcopy(page["events"][0])]
    page["events"][0][field] = None
    page["next_cursor"] = 1
    posts = []

    def transport(url, *, token, body=None):
        if "/v1/events?" in url:
            return page
        posts.append(body)
        return {"events": 0, "repositories": 0, "invalidations": 1}

    monkeypatch.setattr(node, "fetch_json", transport)
    result = node.pull(
        node.NodeState(tmp_path / "ci.db"),
        relay_url="https://relay.example",
        relay_token="example-relay",
        hub_url="https://hub.example",
        node_token="example-node",
    )
    assert result["incomplete"] == 1
    assert posts[0]["events"] == []
    assert posts[0]["invalidations"][0]["repo"] == "example/public"


@pytest.mark.parametrize("change", ["repository", "cursor-order", "next-cursor"])
def test_unscopable_or_unordered_page_does_not_advance_cursor(tmp_path, monkeypatch, change):
    page = relay_page()
    if change == "repository":
        page["events"][0]["repository"] = None
    elif change == "cursor-order":
        page["events"][1]["cursor"] = 1
    else:
        page["next_cursor"] = 9
    posts = []

    def transport(url, *, token, body=None):
        if body is not None:
            posts.append(body)
        return page

    monkeypatch.setattr(node, "fetch_json", transport)
    state = node.NodeState(tmp_path / "ci.db")
    with pytest.raises(ValueError):
        node.pull(
            state,
            relay_url="https://relay.example",
            relay_token="example-relay",
            hub_url="https://hub.example",
            node_token="example-node",
        )
    assert state.cursor("https://relay.example") == "0"
    assert posts == []


@pytest.mark.parametrize("status", [401, 429, 503])
def test_relay_http_failures_are_explicit_and_leave_cursor(tmp_path, monkeypatch, status):
    from urllib.error import HTTPError

    calls = []

    def reject(self, request, **kw):
        calls.append(request.full_url)
        raise HTTPError(request.full_url, status, "example unavailable", {}, None)

    monkeypatch.setattr("urllib.request.OpenerDirector.open", reject)
    state = node.NodeState(tmp_path / "ci.db")
    with pytest.raises(ValueError, match=f"CI transport HTTP {status}"):
        node.pull(
            state,
            relay_url="https://relay.example",
            relay_token="example-relay",
            hub_url="https://hub.example",
            node_token="example-node",
        )
    assert state.cursor("https://relay.example") == "0"
    assert len(calls) == 1
