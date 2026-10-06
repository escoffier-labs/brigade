"""Read-only ownership CLI contracts, including untrusted Hub responses."""

import copy
import io
import json
from contextlib import contextmanager
from datetime import datetime
from urllib.parse import urlsplit

import pytest

from brigade import cli, worklore_client

WORK_ID = "work-fixture"
STAMP = "2026-01-01T00:00:00Z"
REVISION = "a" * 40


@pytest.fixture
def hub(monkeypatch):
    item = {"work_id": WORK_ID, "title": "Fixture", "kind": "repo", "status": "running", "version": 7}
    ownership = {
        "state": "owned",
        "revision": 12,
        "generation": 2,
        "owner_node": "node-a",
        "offered_to": None,
        "last_seq": 23,
        "updated_at": STAMP,
        "repo_identity": "example/project",
        "write_scope": ["src"],
        "source_revision": REVISION,
        "next_action": {"kind": "verify", "resume_condition": ""},
        "evidence_refs": [{"kind": "receipt", "ref": "receipt-a", "source_revision": "b" * 40}],
        "last_report": None,
        "liveness": "unknown",
        "conflict_check": "not-performed",
    }
    replies = [{"item": item}, {"ownership": ownership}]
    requests = []
    monkeypatch.setattr(
        worklore_client,
        "load_fleet_settings",
        lambda: {
            "hub_url": "https://example.com",
            "node_token": "fixture-node-token",
            "admin_token": "fixture-admin-token",
        },
    )

    @contextmanager
    def offline_open(request, **kwargs):
        requests.append(request)
        yield io.BytesIO(json.dumps(replies[len(requests) - 1]).encode())

    monkeypatch.setattr(worklore_client, "_hub_open", offline_open)
    return replies, requests


def test_show_uses_only_existing_authenticated_gets_and_separate_revisions(hub, capsys):
    replies, requests = hub
    replies[0]["recent_events"] = [{"transcript": "PRIVATE-UNEXPECTED"}]
    replies[0]["item"]["description"] = "PRIVATE-UNEXPECTED"
    replies[1]["ownership"]["holder_hash"] = "PRIVATE-UNEXPECTED"
    replies[1]["ownership"]["next_action"]["nonce"] = "PRIVATE-UNEXPECTED"
    replies[1]["ownership"]["evidence_refs"][0]["credential"] = "PRIVATE-UNEXPECTED"
    before = copy.deepcopy(replies)
    assert cli.main(["fleet", "work", "ownership", "show", WORK_ID, "--json"]) == 0
    output = capsys.readouterr()
    payload = json.loads(output.out)
    assert output.err == ""
    assert "PRIVATE-UNEXPECTED" not in output.out
    assert payload["item"]["version"] == 7
    assert payload["ownership"]["revision"] == 12
    assert payload["ownership"]["generation"] == 2
    assert payload["ownership"]["checkpoint"]["source_revision"] == REVISION
    assert payload["ownership"]["checkpoint"]["evidence_refs"][0]["source_revision"] == "b" * 40
    assert payload["consistency"] == "separate-reads"
    assert payload["liveness"] == "unknown"
    assert payload["conflict_check"] == "not-performed"
    for name in ("item", "ownership"):
        assert datetime.fromisoformat(payload["observations"][name]).tzinfo is not None
    assert [request.get_method() for request in requests] == ["GET", "GET"]
    assert [urlsplit(request.full_url).path for request in requests] == [
        f"/work/items/{WORK_ID}",
        f"/work/items/{WORK_ID}/ownership",
    ]
    for request in requests:
        assert request.data is None
        assert dict(request.header_items()) == {"Authorization": "Bearer fixture-node-token"}
    assert replies == before


@pytest.mark.parametrize("state", ["unowned", "offered", "owned", "handoff-pending"])
def test_show_keeps_states_and_unobserved_metadata_distinct(hub, capsys, state):
    ownership = hub[0][1]["ownership"]
    ownership.update(state=state, repo_identity=None, last_report=None)
    assert cli.main(["fleet", "work", "ownership", "show", WORK_ID, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ownership"]["state"] == state
    assert payload["ownership"]["checkpoint"] == {"state": "unobserved"}
    assert payload["ownership"]["report"] == {"state": "unobserved"}


def test_report_is_an_observation_without_freshness_or_liveness_claim(hub, capsys):
    hub[0][1]["ownership"]["last_report"] = {
        "version": 1,
        "provider": "dot",
        "session_id": "session-a",
        "agent_label": "worker-a",
        "observed_at": STAMP,
        "sequence": 3,
        "reporter_node": "node-a",
        "progress": "Checking fixture",
        "evidence_refs": ["receipt-a"],
        "unknown": "PRIVATE-UNEXPECTED",
    }
    assert cli.main(["fleet", "work", "ownership", "show", WORK_ID, "--json"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)["ownership"]["report"]
    assert report["observed_at"] == STAMP
    assert report["sequence"] == 3
    assert report["freshness"] == "unassessed"
    assert report["provider_lifecycle"] == "unobserved"
    assert report["evidence_verification"] == "reported-unverified"
    assert "PRIVATE-UNEXPECTED" not in output


@pytest.mark.parametrize(
    "part,key,value",
    [
        ("item", "version", True),
        ("item", "work_id", "other"),
        ("item", "title", "\x1b[31munsafe"),
        ("item", "title", "x" * 241),
        ("item", "title", "Bearer ghp_" + "x" * 30),
        ("ownership", "state", "invented"),
        ("ownership", "revision", -1),
        ("ownership", "write_scope", ["x"] * 33),
        ("ownership", "source_revision", "bad"),
        ("ownership", "last_report", {"provider": "dot", "sequence": -1}),
        ("ownership", "updated_at", "bad"),
    ],
    ids=["bool", "identity", "terminal", "length", "secret", "state", "counter", "scope", "sha", "report", "time"],
)
def test_malformed_recognized_values_refuse_without_reflecting_data(hub, capsys, part, key, value):
    hub[0][0 if part == "item" else 1][part][key] = value
    assert cli.main(["fleet", "work", "ownership", "show", WORK_ID, "--json"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {
        "code": "invalid-response",
        "state": "unavailable",
        "error": "Invalid ownership read response.",
    }


@pytest.mark.parametrize(
    "stage,code,state",
    [
        (0, "not-found", "missing"),
        (1, "not-found", "missing"),
        (1, "hub-unavailable", "unavailable"),
        (1, "version-conflict", "stale"),
        (1, "stale-generation", "stale"),
        (0, None, "unavailable"),
        (0, "PRIVATE-UNEXPECTED", "unavailable"),
    ],
)
def test_read_refusals_have_fixed_json_and_no_partial_success(monkeypatch, capsys, stage, code, state):
    def refused(*args, **kwargs):
        raise worklore_client.WorkloreClientError("PRIVATE-UNEXPECTED\x1b[31m", code=code)

    monkeypatch.setattr(worklore_client, "get_item", refused if stage == 0 else lambda *a: {"item": {}})
    monkeypatch.setattr(worklore_client, "get_ownership", refused)
    assert cli.main(["fleet", "work", "ownership", "show", WORK_ID, "--json"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    payload = json.loads(output.err)
    assert payload["state"] == state
    assert payload["code"] == (
        code if code in {"not-found", "hub-unavailable", "version-conflict", "stale-generation"} else "read-failed"
    )
    assert "PRIVATE-UNEXPECTED" not in output.err


def test_human_show_states_scope_and_uncertainty(hub, capsys):
    assert cli.main(["fleet", "work", "ownership", "show", WORK_ID]) == 0
    output = capsys.readouterr()
    for expected in (
        "Fixture",
        "owned",
        "node-a",
        "src",
        REVISION,
        "unknown",
        "not-performed",
        "separate-reads",
        "unobserved",
    ):
        assert expected in output.out
    assert output.err == ""
