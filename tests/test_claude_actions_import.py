"""Offline owner-boundary regressions for Actions read authority and budgets."""

import builtins
import copy
import importlib
import io
import json
import socket
import subprocess

import pytest

from brigade.claude_actions_evidence import ConfirmedWorkflow
from brigade.claude_review_import import GetResponse

REPOSITORY = "example-org/example-repo"
BASE = f"/repos/{REPOSITORY}/actions"
WORKFLOW = f"{BASE}/workflows/7"
RUNS = f"{WORKFLOW}/runs"
HEAD = "a" * 40
OLD = "b" * 40
DEFINITION = "c" * 40
OBSERVED = "2026-10-06T12:00:00Z"
PROVIDER_TIME = "2026-10-05T12:00:00Z"
PRIVATE = "FAKE_PRIVATE_CREDENTIAL_VALUE"


def api():
    return importlib.import_module("brigade.claude_actions_import")


def run(run_id=11, attempt=1, **changes):
    row = {
        "id": run_id,
        "run_attempt": attempt,
        "workflow_id": 7,
        "repository": {"full_name": REPOSITORY},
        "head_sha": HEAD,
        "head_branch": PRIVATE,
        "name": "Claude Code",
        "event": "pull_request",
        "status": "completed",
        "conclusion": "success",
        "created_at": PROVIDER_TIME,
        "run_started_at": PROVIDER_TIME,
        "updated_at": PROVIDER_TIME,
        "html_url": f"https://github.com/{REPOSITORY}/actions/runs/{run_id}",
        "url": f"https://api.github.com{BASE}/runs/{run_id}",
        "details_url": f"https://provider.example/{PRIVATE}",
        "pull_requests": [],
        "jobs": [{"name": "Claude Code", "token": PRIVATE}],
        "definition_revision": HEAD,
    }
    row.update(changes)
    return row


class FixtureGet:
    def __init__(self, rows=(), *, total=None, overrides=None):
        self.pages = {
            (WORKFLOW, 1): {
                "id": 7,
                "name": f"Claude {PRIVATE}",
                "path": ".github/workflows/claude.yml",
                "state": "active",
                "created_at": PROVIDER_TIME,
                "updated_at": PROVIDER_TIME,
            },
            (RUNS, 1): {"total_count": len(rows) if total is None else total, "workflow_runs": list(rows)},
        }
        self.pages.update(overrides or {})
        self.calls = []

    def get(self, path, *, params, max_bytes):
        self.calls.append((path, dict(params), max_bytes))
        value = self.pages.get((path, params.get("page", 1)), GetResponse(404, b""))
        if isinstance(value, Exception):
            raise value
        if isinstance(value, GetResponse):
            return value
        return GetResponse(200, json.dumps(value).encode())


def confirmations(*identities, **changes):
    values = {"repository": REPOSITORY, "workflow_id": 7, "revision": DEFINITION}
    values.update(changes)
    return {identity: ConfirmedWorkflow(**values) for identity in identities}


def collect(transport=None, **options):
    return api().collect_actions(
        transport,
        repository=REPOSITORY,
        workflow_id=7,
        current_head_sha=HEAD,
        observed_at=OBSERVED,
        **options,
    )


def test_confirmation_is_per_attempt_and_metadata_never_manufactures_it():
    transport = FixtureGet([run()])
    unknown = collect(transport)
    assert unknown["runs"] == []
    assert unknown["quarantined"][0]["reason"] == "unconfirmed-applicability"
    confirmed = collect(transport, confirmations=confirmations((11, 1)))
    record = confirmed["runs"][0]
    assert record["workflow"]["revision"] == DEFINITION
    assert record["head_relation"] == "current-head"
    assert record["identity_key"] == f"github-actions:{REPOSITORY}:11:1"
    assert record["review_state"] == record["findings_state"] == record["merge_readiness"] == "unknown"
    assert record["runner_kind"] == "unknown" and record["local_node_id"] is None
    assert record["updated_at"] == PROVIDER_TIME
    assert record["observed_at"] == OBSERVED
    assert record["html_url"] == f"https://github.com/{REPOSITORY}/actions/runs/11"
    assert record["source_url"] == f"https://api.github.com{RUNS}"
    assert record["source_page"] == 1
    assert PRIVATE not in json.dumps(confirmed)
    assert confirmed["workflow"]["state"] == "active"
    assert confirmed["workflow"]["source_url"] == f"https://api.github.com{WORKFLOW}"
    assert {path for path, _, _ in transport.calls} == {WORKFLOW, RUNS}


@pytest.mark.parametrize("event", ["pull_request_target", "workflow_call", "issue_comment"])
def test_names_job_names_and_special_events_are_not_definition_authority(event):
    result = collect(FixtureGet([run(event=event)]))
    assert not result["runs"]
    assert result["quarantined"][0]["applicability"] == "unknown"


@pytest.mark.parametrize(
    "status,conclusion,expected",
    [("queued", "success", "unknown"), ("in_progress", "success", "unknown")]
    + [("completed", c, c) for c in ("success", "cancelled", "skipped", "timed_out", "failure")],
)
def test_lifecycle_is_execution_only(status, conclusion, expected):
    result = collect(FixtureGet([run(status=status, conclusion=conclusion)]), confirmations=confirmations((11, 1)))
    record = result["runs"][0]
    assert (record["status"], record["conclusion"]) == (status, expected)
    assert record["review_state"] == "unknown"


def test_reruns_preserve_attempts_dedupe_and_require_independent_confirmation():
    transport = FixtureGet(
        [run(attempt=2), run(attempt=2)],
        total=1,
        overrides={(f"{BASE}/runs/11/attempts/1", 1): run()},
    )
    result = collect(transport, confirmations=confirmations((11, 2)))
    assert [(r["run_id"], r["run_attempt"]) for r in result["runs"]] == [(11, 2)]
    assert result["quarantined"][0]["run_attempt"] == 1
    assert result["duplicate_objects"] == 1
    assert [path for path, _, _ in transport.calls].count(f"{BASE}/runs/11/attempts/1") == 1
    result = collect(transport, confirmations=confirmations((11, 1), (11, 2)))
    assert {(r["run_id"], r["run_attempt"]) for r in result["runs"]} == {(11, 1), (11, 2)}


@pytest.mark.parametrize(
    "field,boolean,integer",
    [
        ("pull_requests", {"number": True}, {"number": 1}),
        ("jobs", {"metadata": {"enabled": False}}, {"metadata": {"enabled": 0}}),
    ],
)
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("cross_page", [False, True])
def test_nested_boolean_and_integer_duplicates_quarantine_the_entire_identity(
    field, boolean, integer, reverse, cross_page
):
    rows = [run(**{field: [boolean]}), run(**{field: [integer]})]
    if reverse:
        rows.reverse()
    if cross_page:
        transport = FixtureGet(rows[:1], total=2, overrides={(RUNS, 2): {"total_count": 2, "workflow_runs": rows[1:]}})
        limits = api().Limits(per_page=1, max_pages=2)
    else:
        transport = FixtureGet(rows, total=1)
        limits = api().Limits()
    result = collect(transport, confirmations=confirmations((11, 1)), limits=limits)
    assert result["runs"] == []
    assert result["current_head_identities"] == []
    assert result["quarantined"] == [
        {
            "run_id": 11,
            "run_attempt": 1,
            "reason": "conflicting-identity",
            "applicability": "unknown",
            "observed_at": OBSERVED,
        }
    ]
    assert result["duplicate_objects"] == 1
    assert not result["complete"] and "conflicting-identity" in result["incomplete_reasons"]
    assert [path for path, _, _ in transport.calls] == [WORKFLOW, RUNS] + ([RUNS] if cross_page else [])


def test_identical_duplicate_with_reordered_nested_keys_remains_deduplicated():
    row = run(pull_requests=[{"number": 1, "head": {"sha": HEAD, "ref": "topic"}}])
    reordered = json.loads(json.dumps(row, sort_keys=True))
    result = collect(FixtureGet([row, reordered], total=1), confirmations=confirmations((11, 1)))
    assert len(result["runs"]) == 1
    assert result["current_head_identities"] == [f"github-actions:{REPOSITORY}:11:1"]
    assert result["duplicate_objects"] == 1
    assert result["quarantined"] == []
    assert result["complete"] and result["incomplete_reasons"] == []


def test_delayed_stale_head_and_conflicting_identity_cannot_restore_current_coverage():
    result = collect(
        FixtureGet([run(), run(12, head_sha=OLD)]),
        confirmations=confirmations((11, 1), (12, 1)),
    )
    assert [r["head_relation"] for r in result["runs"]] == ["current-head", "stale-head"]
    assert result["current_head_identities"] == [f"github-actions:{REPOSITORY}:11:1"]
    conflict = collect(FixtureGet([run(), run(head_sha=OLD)], total=1), confirmations=confirmations((11, 1)))
    assert conflict["runs"] == [] and conflict["current_head_identities"] == []
    assert conflict["quarantined"][0]["reason"] == "conflicting-identity"


@pytest.mark.parametrize("change", [{"repository": "example-org/other"}, {"workflow_id": 8}])
def test_confirmed_definition_identity_mismatch_rejected_before_transport(change):
    transport = FixtureGet([run()])
    with pytest.raises(ValueError, match="confirmation"):
        collect(transport, confirmations=confirmations((11, 1), **change))
    assert transport.calls == []


@pytest.mark.parametrize(
    "changes",
    [{"workflow_id": 8}, {"repository": {"full_name": "example-org/other"}}, {"run_attempt": 0}, {"id": True}],
)
def test_provider_identity_mismatch_is_quarantined(changes):
    result = collect(FixtureGet([run(**changes)]), confirmations=confirmations((11, 1)))
    assert result["runs"] == [] and not result["complete"]


def test_pagination_exact_boundary_and_truncation_never_prove_absence():
    overrides = {(RUNS, 2): {"total_count": 2, "workflow_runs": [run(12)]}}
    result = collect(
        FixtureGet([run()], total=2, overrides=overrides),
        confirmations=confirmations((11, 1), (12, 1)),
        limits=api().Limits(per_page=1, max_pages=2),
    )
    assert result["complete"] and len(result["runs"]) == 2
    truncated = collect(FixtureGet([run()], total=2), limits=api().Limits(per_page=1, max_pages=1))
    assert not truncated["complete"] and "page-budget" in truncated["incomplete_reasons"]
    short = collect(FixtureGet([], total=1))
    assert not short["complete"] and "pagination-truncated" in short["incomplete_reasons"]
    repeated = collect(
        FixtureGet([run()], total=2, overrides={(RUNS, 2): {"total_count": 2, "workflow_runs": [run()]}}),
        limits=api().Limits(per_page=1),
    )
    assert not repeated["complete"] and "repeated-page" in repeated["incomplete_reasons"]


@pytest.mark.parametrize("status,reason", [(403, "access-denied"), (404, "unavailable"), (429, "rate-limited")])
def test_denial_unavailability_and_rate_limit_are_separate(status, reason):
    result = collect(FixtureGet(overrides={(RUNS, 1): GetResponse(status, PRIVATE.encode())}))
    assert reason in result["incomplete_reasons"] and not result["complete"]
    assert result["runs"] == [] and PRIVATE not in json.dumps(result)


@pytest.mark.parametrize(
    "body",
    [
        b"{bad",
        b'{"total_count":0,"total_count":1,"workflow_runs":[]}',
        b'{"total_count":' + b"9" * 2000 + b',"workflow_runs":[]}',
        b'{"total_count":0,"workflow_runs":[],"extra":' + b"[" * 200 + b"0" + b"]" * 200 + b"}",
        b'{"total_count":0,"workflow_runs":[],"extra":NaN}',
        b"[]",
        b'{"total_count":0,"workflow_runs":[],"extra":"\xff"}',
    ],
)
def test_malformed_raw_json_giant_integer_and_deep_nesting_fail_closed(body):
    result = collect(FixtureGet(overrides={(RUNS, 1): GetResponse(200, body)}))
    assert result["runs"] == [] and not result["complete"]
    assert "malformed-payload" in result["incomplete_reasons"]


@pytest.mark.parametrize("response", [GetResponse(200, b" " * 1001), GetResponse(200, b"{}", True)])
def test_transport_byte_cap_is_independently_enforced(response):
    result = collect(FixtureGet(overrides={(RUNS, 1): response}), limits=api().Limits(max_body_bytes=1000))
    assert not result["complete"] and "truncated-response" in result["incomplete_reasons"]
    assert result["runs"] == []


@pytest.mark.parametrize(
    "limits,reason",
    [
        ({"max_requests": 1}, "request-budget"),
        ({"max_objects": 1}, "object-budget"),
        ({"max_bytes": 1}, "byte-budget"),
        ({"max_json_nodes": 1}, "malformed-payload"),
    ],
)
def test_shared_read_budgets_fail_closed(limits, reason):
    result = collect(FixtureGet([run()]), limits=api().Limits(**limits))
    assert not result["complete"] and reason in result["incomplete_reasons"]


def test_elapsed_budget_checks_before_and_after_read():
    ticks = iter([0.0, 0.0, 2.0, 2.0])
    transport = FixtureGet([run()])
    result = collect(transport, limits=api().Limits(max_elapsed_seconds=1), clock=lambda: next(ticks))
    assert len(transport.calls) == 1 and result["runs"] == []
    assert "elapsed-budget" in result["incomplete_reasons"]
    ticks = iter([0.0, 2.0, 2.0])
    transport = FixtureGet()
    result = collect(transport, limits=api().Limits(max_elapsed_seconds=1), clock=lambda: next(ticks))
    assert transport.calls == [] and "elapsed-budget" in result["incomplete_reasons"]


@pytest.mark.parametrize(
    "repository",
    [
        "../example",
        "example-org/..",
        "example-org/example.git",
        "example-org/example%2frepo",
        "https://github.com/example-org/example-repo",
        "example-org/example?x=1",
        "example-org/example/repo",
        "example-org/example\\repo",
    ],
)
def test_deceptive_repository_inputs_cannot_build_paths(repository):
    transport = FixtureGet()
    with pytest.raises(ValueError):
        api().collect_actions(
            transport, repository=repository, workflow_id=7, current_head_sha=HEAD, observed_at=OBSERVED
        )
    assert transport.calls == []


def test_offline_default_and_no_credentials_files_network_processes_or_mutations(monkeypatch):
    module = api()
    transport = FixtureGet([run()])

    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected IO")

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(io, "open", forbidden)
        patch.setattr(socket, "socket", forbidden)
        patch.setattr(subprocess, "Popen", forbidden)
        result = collect(transport, confirmations=confirmations((11, 1)))
        default = collect()
    assert len(result["runs"]) == 1
    assert default["runs"] == [] and "unavailable" in default["incomplete_reasons"]
    assert isinstance(module.FixtureTransport(), module.FixtureTransport)
    assert {p for p, _, _ in transport.calls} == {WORKFLOW, RUNS}
    assert PRIVATE not in json.dumps(result)


def test_untrusted_urls_and_exception_text_do_not_survive_or_get_followed():
    row = run(html_url=f"https://provider.example/{PRIVATE}", url=f"https://provider.example/{PRIVATE}")
    result = collect(FixtureGet([row]), confirmations=confirmations((11, 1)))
    assert result["runs"][0]["html_url"] is None and result["runs"][0]["api_url"] is None
    result = collect(FixtureGet(overrides={(RUNS, 1): RuntimeError(PRIVATE)}))
    assert "transport-unavailable" in result["incomplete_reasons"]
    assert PRIVATE not in json.dumps(result)


def test_mismatched_attempt_response_and_metadata_are_not_promoted():
    result = collect(
        FixtureGet([run(attempt=2)], overrides={(f"{BASE}/runs/11/attempts/1", 1): run(12)}),
        confirmations=confirmations((11, 1), (11, 2), (12, 1)),
    )
    assert {(r["run_id"], r["run_attempt"]) for r in result["runs"]} == {(11, 2)}
    assert "attempt-identity-mismatch" in result["incomplete_reasons"]
    metadata = copy.deepcopy(FixtureGet().pages[(WORKFLOW, 1)])
    metadata["id"] = 8
    result = collect(FixtureGet(overrides={(WORKFLOW, 1): metadata}))
    assert result["workflow"] is None and "workflow-identity-mismatch" in result["incomplete_reasons"]


def test_attempt_denial_retains_independently_confirmed_latest_without_retry():
    attempt_path = f"{BASE}/runs/11/attempts/1"
    transport = FixtureGet([run(attempt=2)], overrides={(attempt_path, 1): GetResponse(403, PRIVATE.encode())})
    result = collect(transport, confirmations=confirmations((11, 1), (11, 2)))
    assert [(r["run_id"], r["run_attempt"]) for r in result["runs"]] == [(11, 2)]
    assert not result["complete"] and "access-denied" in result["incomplete_reasons"]
    assert [path for path, _, _ in transport.calls] == [WORKFLOW, RUNS, attempt_path]


def test_huge_attempt_cannot_escape_shared_request_budget():
    transport = FixtureGet([run(attempt=(1 << 63) - 1)], overrides={(f"{BASE}/runs/11/attempts/1", 1): run()})
    result = collect(transport, limits=api().Limits(max_requests=3))
    assert len(transport.calls) == 3
    assert "request-budget" in result["incomplete_reasons"]
    assert result["runs"] == []


def test_raw_provider_event_text_is_withheld_and_fact_times_do_not_refresh():
    transport = FixtureGet([run(event=PRIVATE.lower())])
    first = collect(transport, confirmations=confirmations((11, 1)))
    later = api().collect_actions(
        transport,
        repository=REPOSITORY,
        workflow_id=7,
        current_head_sha=OLD,
        observed_at="2026-10-07T12:00:00Z",
        confirmations=confirmations((11, 1)),
    )
    assert first["runs"][0]["event"] is None
    assert PRIVATE.lower() not in json.dumps(first)
    assert later["runs"][0]["updated_at"] == first["runs"][0]["updated_at"] == PROVIDER_TIME
    assert later["runs"][0]["identity_key"] == first["runs"][0]["identity_key"]
    assert later["runs"][0]["head_relation"] == "stale-head"
    assert later["current_head_identities"] == []


@pytest.mark.parametrize(
    "response", [None, GetResponse(True, b"{}"), GetResponse(200, "bad"), GetResponse(200, b"{}", 1)]
)
def test_malformed_transport_response_is_sanitized(response):
    class MalformedGet(FixtureGet):
        def get(self, path, *, params, max_bytes):
            if path == RUNS:
                return response
            return super().get(path, params=params, max_bytes=max_bytes)

    result = collect(MalformedGet())
    assert "malformed-response" in result["incomplete_reasons"]
    assert not result["complete"] and not result["runs"]


@pytest.mark.parametrize(
    "page",
    [
        {"total_count": 1, "workflow_runs": [None]},
        {"total_count": True, "workflow_runs": []},
        {"total_count": 0, "workflow_runs": [run()]},
        {"total_count": 2, "workflow_runs": [run(), run(12)]},
    ],
)
def test_inconsistent_or_oversized_pages_do_not_establish_completeness(page):
    result = collect(FixtureGet(overrides={(RUNS, 1): page}), limits=api().Limits(per_page=1))
    assert not result["complete"]
    assert result["absence"] == "unknown"


def test_changing_page_total_withholds_completeness():
    transport = FixtureGet([run()], total=2, overrides={(RUNS, 2): {"total_count": 3, "workflow_runs": [run(12)]}})
    result = collect(transport, limits=api().Limits(per_page=1))
    assert not result["complete"] and "malformed-payload" in result["incomplete_reasons"]
