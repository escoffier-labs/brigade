"""Strict read-only handoff consumer at the authenticated transport boundary."""

import copy
import io
import json
import urllib.error
from contextlib import contextmanager
from urllib.parse import urlsplit

import pytest

from brigade import cli, worklore_client

WORK_ID = "work-fixture"
STAMP = "2026-01-01T00:00:00+02:00"


@pytest.fixture
def hub(monkeypatch):
    view = {
        "schema": "brigade.prepared_handoff.v1",
        "observed_at": STAMP,
        "consistency": "single-db-snapshot",
        "visibility": "fleet-trust",
        "authority": "read-only-context",
        "person_attribution": "unrecorded",
        "historical_human_attribution": "unrecorded",
        "item": {
            "work_id": WORK_ID,
            "version": 7,
            "title": "Fixture",
            "kind": "repo",
            "status": "running",
            "acceptance": [],
            "effective_acceptance": ["Source criterion"],
            "blocker": None,
        },
        "ownership": {
            "state": "owned",
            "revision": 12,
            "generation": 2,
            "owner_node": "node-a",
            "offered_to": None,
            "last_seq": 23,
            "updated_at": STAMP,
            "liveness": "unknown",
        },
        "checkpoint": {
            "repo_identity": "example/project",
            "write_scope": ["src"],
            "source_revision": "a" * 40,
            "next_action": {"kind": "verify", "resume_condition": ""},
            "evidence_refs": [{"kind": "receipt", "ref": "receipt-a", "source_revision": "b" * 40}],
            "conflict_check": "path-admission",
            "conflict_check_freshness": "historical-checkpoint-admission",
            "evidence_verification": "reported-unverified",
            "checkpoint_current_item_version_binding": "unavailable",
        },
        "last_report": {
            "data": {
                "version": 1,
                "provider": "dot",
                "session_id": "session-a",
                "agent_label": "worker-a",
                "observed_at": STAMP,
                "sequence": 91,
                "progress": "Checking fixture",
                "evidence_refs": ["receipt-a", "https://example.com/check/1"],
                "source": "explicit-metadata",
                "source_scope": "explicit-session",
                "coverage": "explicitly-reported-sessions",
                "evidence_verification": "reported-unverified",
                "provider_lifecycle": "unobserved",
            },
            "reporter_node": "node-a",
            "freshness": "unassessed",
        },
        "run_refs": {
            "items": [{"node_id": "node-a", "run_id": "run-a"}],
            "truncated": False,
            "coverage": "recorded-execution-events",
            "ownership_generation_binding": "unavailable",
        },
    }
    reply = {"prepared_handoff": view}
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
        yield io.BytesIO(json.dumps(reply).encode())

    monkeypatch.setattr(worklore_client, "_hub_open", offline_open)
    return reply, requests


def show(*extra):
    return cli.main(["fleet", "work", "handoff", "show", WORK_ID, *extra])


@pytest.mark.parametrize("status", ["running", "archived"])
def test_one_authenticated_get_exact_projection_and_report_provenance(hub, capsys, status):
    reply, requests = hub
    reply["prepared_handoff"]["item"]["status"] = status
    before = copy.deepcopy(reply)
    assert show("--json") == 0
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out) == reply["prepared_handoff"]
    assert reply == before
    assert len(requests) == 1
    request = requests[0]
    assert request.get_method() == "GET" and request.data is None
    assert urlsplit(request.full_url).path == f"/work/items/{WORK_ID}/prepared-handoff"
    assert dict(request.header_items()) == {"Authorization": "Bearer fixture-node-token"}


def test_human_output_states_context_and_evidence_limits(hub, capsys):
    assert show() == 0
    output = capsys.readouterr().out
    for text in (
        "read-only context",
        "person attribution: unrecorded",
        "historical human attribution: unrecorded",
        "reported/unverified",
        "report freshness: unassessed",
        "liveness: unknown",
        "historical-checkpoint-admission",
        "unavailable",
        "single-db-snapshot",
    ):
        assert text in output


@pytest.mark.parametrize(
    "path,value",
    [
        ([], {"prepared_handoff": {}, "transcript": "PRIVATE-MARKER"}),
        (["prepared_handoff", "schema"], "brigade.prepared_handoff.v2"),
        (["prepared_handoff", "prompt"], "PRIVATE-MARKER"),
        (["prepared_handoff", "item", "description"], "PRIVATE-MARKER"),
        (["prepared_handoff", "item", "work_id"], "another-work"),
        (["prepared_handoff", "item", "title"], "x" * 241),
        (["prepared_handoff", "item", "acceptance"], ["x"] * 21),
        (["prepared_handoff", "item", "version"], True),
        (["prepared_handoff", "ownership", "holder_hash"], "PRIVATE-MARKER"),
        (["prepared_handoff", "ownership", "liveness"], "alive"),
        (["prepared_handoff", "checkpoint", "next_action", "prompt"], "PRIVATE-MARKER"),
        (
            ["prepared_handoff", "checkpoint", "evidence_refs"],
            [{"kind": "receipt", "ref": "https://example.com", "source_revision": "a" * 40}],
        ),
        (["prepared_handoff", "checkpoint", "checkpoint_current_item_version_binding"], "current"),
        (["prepared_handoff", "last_report", "data", "transcript"], "PRIVATE-MARKER"),
        (["prepared_handoff", "last_report", "data", "reporter_node"], "node-a"),
        (["prepared_handoff", "last_report", "data", "progress"], "x" * 401),
        (["prepared_handoff", "last_report", "data", "evidence_verification"], "verified"),
        (["prepared_handoff", "last_report", "data", "evidence_refs"], ["https://example.com/check?secret=x"]),
        (["prepared_handoff", "last_report", "freshness"], "fresh"),
        (["prepared_handoff", "run_refs", "items"], [{"node_id": "node-a", "run_id": "run-a"}] * 51),
        (
            ["prepared_handoff", "run_refs", "items"],
            [{"node_id": "node-a", "run_id": "run-a", "auth": "PRIVATE-MARKER"}],
        ),
        (["prepared_handoff", "run_refs", "items"], [{"node_id": "node-a", "run_id": "run-a"}] * 2),
        (["prepared_handoff", "run_refs", "ownership_generation_binding"], "current"),
        (["prepared_handoff", "run_refs", "truncated"], 1),
        (["prepared_handoff", "observed_at"], None),
    ],
)
def test_strict_response_refuses_without_partial_context(hub, capsys, path, value):
    reply, requests = hub
    target = reply
    for field in path[:-1]:
        target = target[field]
    if path:
        target[path[-1]] = value
    else:
        reply.clear()
        reply.update(value)
    assert show("--json") == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {
        "code": "invalid-response",
        "state": "unavailable",
        "error": "Invalid prepared handoff response.",
    }
    assert len(requests) == 1


@pytest.mark.parametrize(
    "code,status,state,message",
    [
        ("not-found", 404, "missing", "Work item was not found."),
        ("unauthorized", 401, "forbidden", "Prepared handoff read is not authorized."),
        (None, 401, "forbidden", "Prepared handoff read is not authorized."),
        ("forbidden", 403, "forbidden", "Prepared handoff read is not authorized."),
        (None, 403, "forbidden", "Prepared handoff read is not authorized."),
        ("hub-unavailable", 503, "unavailable", "Prepared handoff read is unavailable."),
        ("unknown-refusal", 400, "unavailable", "Prepared handoff read is unavailable."),
    ],
)
def test_fixed_refusals_never_echo_remote_text_or_retry(hub, monkeypatch, capsys, code, status, state, message):
    requests = hub[1]

    def denied(request, **kwargs):
        requests.append(request)
        body = {"error": "PRIVATE-MARKER"} if code is None else {"code": code, "error": "PRIVATE-MARKER"}
        raise urllib.error.HTTPError(
            request.full_url,
            status,
            "PRIVATE-MARKER",
            {},
            io.BytesIO(json.dumps(body).encode()),
        )

    monkeypatch.setattr(worklore_client, "_hub_open", denied)
    assert show("--json") == 1
    output = capsys.readouterr()
    assert output.out == ""
    expected_code = code
    if code is None:
        expected_code = "unauthorized" if status == 401 else "forbidden"
    elif code == "unknown-refusal":
        expected_code = "read-failed"
    assert json.loads(output.err) == {
        "code": expected_code,
        "state": state,
        "error": message,
    }
    assert len(requests) == 1


def test_null_empty_metadata_is_accepted_without_invented_observations(hub, capsys):
    view = hub[0]["prepared_handoff"]
    view.update(checkpoint=None, last_report=None)
    view["run_refs"]["items"] = []
    assert show("--json") == 0
    assert json.loads(capsys.readouterr().out) == view
