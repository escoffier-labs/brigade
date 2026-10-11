"""Offline contracts for attempt jobs and run-wide artifact metadata."""

import importlib
import json
import builtins
import socket
import subprocess
from dataclasses import replace

import pytest

from brigade.claude_actions_import import Limits
from brigade.claude_actions_evidence import ConfirmedWorkflow, normalize_run
from brigade.claude_review_import import GetResponse

REPO = "example/project"
HEAD = "a" * 40
REVISION = "b" * 40
OBSERVED = "2026-10-10T12:00:00Z"
PROVIDER_TIME = "2026-10-09T12:00:00Z"
BASE = f"/repos/{REPO}/actions"
JOBS = f"{BASE}/runs/11/attempts/2/jobs"
ARTIFACTS = f"{BASE}/runs/11/artifacts"


def evidence(**changes):
    row = {
        "repository": {"full_name": REPO},
        "workflow_id": 7,
        "id": 11,
        "run_attempt": 2,
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": "success",
    }
    row.update(changes)
    return normalize_run(row, workflow=ConfirmedWorkflow(REPO, 7, REVISION), observed_at=PROVIDER_TIME)


def job(job_id=21, **changes):
    row = {
        "id": job_id,
        "run_id": 11,
        "run_attempt": 2,
        "head_sha": HEAD,
        "url": f"https://api.github.com{BASE}/jobs/{job_id}",
        "html_url": f"https://github.com/{REPO}/actions/runs/11/job/{job_id}",
        "status": "completed",
        "conclusion": "success",
        "started_at": PROVIDER_TIME,
        "completed_at": PROVIDER_TIME,
        "name": "FAKE_PRIVATE_NAME",
        "runner_name": "FAKE_PRIVATE_RUNNER",
        "labels": ["ubuntu-latest"],
    }
    row.update(changes)
    return row


def artifact(artifact_id=31, **changes):
    row = {
        "id": artifact_id,
        "workflow_run": {"id": 11, "head_sha": HEAD},
        "url": f"https://api.github.com{BASE}/artifacts/{artifact_id}",
        "archive_download_url": f"https://api.github.com{BASE}/artifacts/{artifact_id}/zip",
        "name": "FAKE_PRIVATE_ARTIFACT",
        "expired": False,
        "size_in_bytes": 123,
        "created_at": PROVIDER_TIME,
        "updated_at": PROVIDER_TIME,
        "expires_at": "2026-10-20T12:00:00Z",
    }
    row.update(changes)
    return row


class FixtureGet:
    def __init__(self, jobs=(), artifacts=(), overrides=None):
        self.pages = {
            (JOBS, 1): {"total_count": len(jobs), "jobs": list(jobs)},
            (ARTIFACTS, 1): {"total_count": len(artifacts), "artifacts": list(artifacts)},
        }
        self.pages.update(overrides or {})
        self.calls = []

    def get(self, path, *, params, max_bytes):
        self.calls.append((path, dict(params), max_bytes))
        value = self.pages.get((path, params.get("page", 1)), GetResponse(404, b""))
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, GetResponse) else GetResponse(200, json.dumps(value).encode())


def collect(transport=None, **changes):
    options = {"run": evidence(), "current_head_sha": HEAD, "observed_at": OBSERVED}
    options.update(changes)
    return importlib.import_module("brigade.claude_actions_metadata").collect_metadata(transport, **options)


def test_attempt_jobs_and_run_wide_artifacts_keep_distinct_provenance():
    transport = FixtureGet([job()], [artifact()])
    result = collect(transport)
    assert [call[0] for call in transport.calls] == [JOBS, ARTIFACTS]
    assert result["complete"] is True
    record = result["jobs"][0]
    assert record["identity_key"] == f"github-actions-job:{REPO}:11:2:21"
    assert record["run_attempt"] == 2
    assert record["definition_revision"] == REVISION
    assert record["head_relation"] == "current-head"
    assert record["status"] == "completed" and record["conclusion"] == "success"
    assert record["api_url"] == f"https://api.github.com{BASE}/jobs/21"
    assert record["html_url"] == f"https://github.com/{REPO}/actions/runs/11/job/21"
    assert record["started_at"] == PROVIDER_TIME and record["observed_at"] == OBSERVED
    assert record["runner_kind"] == "unknown" and record["local_node_id"] is None
    record = result["artifacts"][0]
    assert record["identity_key"] == f"github-actions-artifact:{REPO}:31"
    assert record["run_attempt"] is None and record["attempt_association"] == "unknown"
    assert record["definition_revision"] is None
    assert record["availability"] == "present" and record["content_availability"] == "unknown"
    assert record["api_url"] == f"https://api.github.com{BASE}/artifacts/31"
    assert record["updated_at"] == PROVIDER_TIME
    assert "FAKE_PRIVATE" not in json.dumps(result)
    assert "archive_download_url" not in json.dumps(result)
    assert result["review_state"] == result["findings_state"] == result["merge_readiness"] == "unknown"


@pytest.mark.parametrize(
    ("status", "conclusion", "expected"),
    [
        ("queued", None, "unknown"),
        ("in_progress", "success", "unknown"),
        ("completed", "cancelled", "cancelled"),
        ("completed", "skipped", "skipped"),
        ("completed", "timed_out", "timed_out"),
        ("completed", "failure", "failure"),
        ([], {"private": "FAKE_PRIVATE"}, "unknown"),
    ],
)
def test_job_execution_does_not_claim_review_or_completion(status, conclusion, expected):
    result = collect(FixtureGet([job(status=status, conclusion=conclusion)]))
    record = result["jobs"][0]
    assert record["conclusion"] == expected
    assert record["status"] == (status if isinstance(status, str) else "unknown")
    assert record["review_state"] == record["findings_state"] == "unknown"


@pytest.mark.parametrize("kind", ["jobs", "artifacts"])
@pytest.mark.parametrize("identity", [None, True, -1, 0])
def test_invalid_provider_ids_are_quarantined(kind, identity):
    rows = {kind: [job(id=identity) if kind == "jobs" else artifact(id=identity)]}
    result = collect(FixtureGet(**rows))
    assert result[kind] == []
    assert result["complete"] is False
    assert result["quarantined"][0]["reason"] == "object-identity-mismatch"


@pytest.mark.parametrize("kind", ["jobs", "artifacts"])
def test_oversized_provider_integer_is_refused_before_object_quarantine(kind):
    rows = {kind: [job(id=2**63) if kind == "jobs" else artifact(id=2**63)]}
    result = collect(FixtureGet(**rows))
    assert result[kind] == [] and result["quarantined"] == []
    assert result["complete"] is False
    assert "malformed-payload" in result["incomplete_reasons"]
    assert result[f"{kind}_state"] == "unknown"
    other = "artifacts" if kind == "jobs" else "jobs"
    assert result[f"{other}_state"] == "absent-in-complete-scope"
    assert str(2**63) not in json.dumps(result)


@pytest.mark.parametrize(
    "changes",
    [{"run_id": 12}, {"run_attempt": 1}, {"run_attempt": True}, {"head_sha": "c" * 40}],
)
def test_jobs_from_other_attempts_runs_or_heads_cannot_cover_the_supplied_run(changes):
    result = collect(FixtureGet([job(**changes)]))
    assert result["jobs"] == []
    assert result["quarantined"][0]["reason"] == "object-identity-mismatch"


def test_attempt_endpoint_supplies_attempt_when_job_body_omits_it():
    row = job()
    del row["run_attempt"]
    assert collect(FixtureGet([row]))["jobs"][0]["run_attempt"] == 2


@pytest.mark.parametrize("binding", [None, {}, {"id": 12}, {"id": True}])
def test_artifacts_require_independent_provider_run_binding(binding):
    result = collect(FixtureGet(artifacts=[artifact(workflow_run=binding)]))
    assert result["artifacts"] == [] and result["complete"] is False


def test_artifact_head_conflicting_with_confirmed_run_is_quarantined():
    result = collect(FixtureGet(artifacts=[artifact(workflow_run={"id": 11, "head_sha": "c" * 40})]))
    assert result["artifacts"] == []
    assert result["quarantined"][0]["reason"] == "object-identity-mismatch"
    assert result["complete"] is False


def test_stale_artifact_and_job_heads_never_inherit_current_attempt_coverage():
    old = "c" * 40
    result = collect(
        FixtureGet([job(head_sha=old)], [artifact(workflow_run={"id": 11, "head_sha": old})]),
        run=evidence(head_sha=old),
    )
    assert result["jobs"][0]["head_relation"] == result["artifacts"][0]["head_relation"] == "stale-head"
    assert result["current_head_job_identities"] == []
    assert result["artifacts"][0]["attempt_association"] == "unknown"


@pytest.mark.parametrize("expired", [True, False, None, "false", 0, {}])
def test_artifact_expiry_never_proves_download_readiness(expired):
    record = collect(FixtureGet(artifacts=[artifact(expired=expired)]))["artifacts"][0]
    assert record["availability"] == ("expired" if expired is True else "present")
    assert record["expired"] == (expired if type(expired) is bool else None)
    assert record["content_availability"] == record["deletion_state"] == "unknown"


@pytest.mark.parametrize("runner_kind", ["github-hosted", "self-hosted"])
def test_runner_provenance_requires_job_specific_confirmation(runner_kind):
    row = job(labels=["self-hosted"], runner_name="GitHub Actions hosted runner")
    transport = FixtureGet([row])
    assert collect(transport)["jobs"][0]["runner_kind"] == "unknown"
    record = collect(transport, runner_kinds={21: runner_kind})["jobs"][0]
    assert record["runner_kind"] == runner_kind
    assert record["runner_authority"] == "caller-confirmed-job-runner"
    assert record["local_node_id"] is None


@pytest.mark.parametrize(
    ("status", "state"), [(403, "access-denied"), (404, "unavailable"), (429, "rate-limited"), (302, "unavailable")]
)
def test_endpoint_failure_is_not_empty_scope_or_a_provider_health_claim(status, state):
    result = collect(FixtureGet(overrides={(JOBS, 1): GetResponse(status, b"")}))
    assert result["jobs"] == [] and result["jobs_state"] == state
    assert result["complete"] is False and result["absence"] == "unknown"
    assert result["artifacts_state"] == "absent-in-complete-scope"
    assert result["deletion_state"] == "unknown"


def test_successful_empty_lists_only_establish_metadata_snapshot_absence():
    result = collect(FixtureGet())
    assert result["jobs_state"] == result["artifacts_state"] == "absent-in-complete-scope"
    assert result["absence"] == result["review_state"] == "unknown"
    assert result["complete"] is True
    offline = collect()
    assert offline["jobs_state"] == offline["artifacts_state"] == "unavailable"
    assert offline["complete"] is False


def test_paginated_metadata_uses_unique_object_totals_and_shared_budget():
    transport = FixtureGet(
        overrides={
            (JOBS, 1): {"total_count": 2, "jobs": [job()]},
            (JOBS, 2): {"total_count": 2, "jobs": [job(22)]},
        }
    )
    result = collect(transport, limits=Limits(per_page=1))
    assert result["complete"] is True and len(result["jobs"]) == 2
    assert [call[1].get("page") for call in transport.calls] == [1, 2, 1]
    result = collect(transport, limits=Limits(per_page=1, max_requests=2))
    assert result["artifacts_state"] == "unknown"
    assert "request-budget" in result["incomplete_reasons"]


def test_page_budget_requires_another_page_not_an_oversized_first_page():
    transport = FixtureGet(overrides={(JOBS, 1): {"total_count": 2, "jobs": [job()]}})
    result = collect(transport, limits=Limits(max_pages=1, per_page=1))
    assert "page-budget" in result["incomplete_reasons"]
    assert result["jobs_state"] == "unknown" and len(result["jobs"]) == 1
    assert [call[0] for call in transport.calls] == [JOBS, ARTIFACTS]


def test_late_job_response_exhausts_elapsed_budget_before_artifact_read():
    transport = FixtureGet([job()], [artifact()])
    result = collect(transport, clock=iter([0, 0, 31, 31]).__next__)
    assert "elapsed-budget" in result["incomplete_reasons"]
    assert result["jobs"] == result["artifacts"] == []
    assert result["jobs_state"] == result["artifacts_state"] == "unknown"
    assert [call[0] for call in transport.calls] == [JOBS]


def test_conflicting_duplicate_on_later_page_removes_prior_object_and_keeps_page_provenance():
    result = collect(
        FixtureGet(
            overrides={
                (JOBS, 1): {"total_count": 2, "jobs": [job()]},
                (JOBS, 2): {"total_count": 2, "jobs": [job(updated_at=OBSERVED)]},
            }
        ),
        limits=Limits(per_page=1),
    )
    assert result["jobs"] == [] and result["current_head_job_identities"] == []
    assert result["quarantined"][0]["reason"] == "conflicting-identity"
    assert result["quarantined"][0]["source_page"] == 2
    assert result["complete"] is False


def test_page_denial_preserves_earlier_observations_without_complete_scope():
    result = collect(
        FixtureGet(
            overrides={
                (JOBS, 1): {"total_count": 2, "jobs": [job()]},
                (JOBS, 2): GetResponse(403, b""),
            }
        ),
        limits=Limits(per_page=1),
    )
    assert len(result["jobs"]) == 1 and result["jobs"][0]["source_page"] == 1
    assert result["jobs_state"] == "access-denied" and result["complete"] is False


@pytest.mark.parametrize(
    "payload",
    [
        {"jobs": []},
        {"total_count": True, "jobs": []},
        {"total_count": -1, "jobs": []},
        {"total_count": 0, "artifacts": []},
        {"total_count": 1, "jobs": [[]]},
    ],
)
def test_invalid_metadata_envelope_cannot_establish_empty_scope(payload):
    result = collect(FixtureGet(overrides={(JOBS, 1): payload}))
    assert result["jobs"] == [] and result["jobs_state"] == "unknown"
    assert "malformed-payload" in result["incomplete_reasons"]
    assert result["complete"] is False


def test_legacy_job_html_link_retains_exact_identity():
    url = f"https://github.com/{REPO}/runs/11/jobs/21"
    assert collect(FixtureGet([job(html_url=url)]))["jobs"][0]["html_url"] == url


def test_missing_provider_head_keeps_metadata_out_of_current_head_identities():
    result = collect(FixtureGet([job(head_sha=None)]))
    assert len(result["jobs"]) == 1 and result["jobs"][0]["head_relation"] == "unknown"
    assert result["current_head_job_identities"] == []


@pytest.mark.parametrize(
    ("second", "reason"),
    [
        ({"total_count": 2, "jobs": [job()]}, "repeated-page"),
        ({"total_count": 3, "jobs": [job(22)]}, "inconsistent-total"),
        ({"total_count": 2, "jobs": []}, "pagination-truncated"),
    ],
)
def test_repeated_or_changing_pages_withhold_complete_scope(second, reason):
    result = collect(
        FixtureGet(overrides={(JOBS, 1): {"total_count": 2, "jobs": [job()]}, (JOBS, 2): second}),
        limits=Limits(per_page=1),
    )
    assert result["complete"] is False and reason in result["incomplete_reasons"]


@pytest.mark.parametrize("kind", ["jobs", "artifacts"])
def test_duplicate_ids_are_idempotent_and_conflicting_snapshots_are_quarantined(kind):
    first = job() if kind == "jobs" else artifact()
    identical = dict(reversed(list(first.items())))
    result = collect(FixtureGet(**{kind: [first, identical]}))
    assert len(result[kind]) == 1 and result["duplicate_objects"] == 1
    different = dict(first, updated_at=OBSERVED)
    result = collect(FixtureGet(**{kind: [first, different]}))
    assert result[kind] == []
    assert result["quarantined"][0]["reason"] == "conflicting-identity"
    assert result["complete"] is False


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/FAKE_PRIVATE",
        f"https://api.github.com{BASE}/jobs/21/logs",
        f"https://api.github.com{BASE}/jobs/21?token=FAKE_PRIVATE",
        "https://github.com/example/other/actions/runs/11/job/21",
    ],
)
def test_untrusted_links_never_survive_or_control_followup_paths(url):
    transport = FixtureGet([job(url=url, html_url=url)], [artifact(url=url)])
    result = collect(transport)
    assert result["jobs"][0]["api_url"] is result["jobs"][0]["html_url"] is None
    assert result["artifacts"][0]["api_url"] is None
    assert [call[0] for call in transport.calls] == [JOBS, ARTIFACTS]
    assert "FAKE_PRIVATE" not in json.dumps(result)


@pytest.mark.parametrize("changes", [{"run_id": True}, {"run_attempt": -1}, {"workflow": None}])
def test_invalid_confirmation_is_refused_before_transport(changes):
    transport = FixtureGet()
    with pytest.raises(ValueError, match="confirmed"):
        collect(transport, run=replace(evidence(), **changes))
    assert transport.calls == []


@pytest.mark.parametrize("runner_kinds", [{True: "github-hosted"}, {21: "inferred-from-name"}, {21: []}])
def test_invalid_runner_attestations_are_refused_before_transport(runner_kinds):
    transport = FixtureGet()
    with pytest.raises(ValueError, match="runner"):
        collect(transport, runner_kinds=runner_kinds)
    assert transport.calls == []


@pytest.mark.parametrize("limits", [Limits(max_pages=1, per_page=1), Limits(max_objects=1), Limits(max_bytes=40)])
def test_partial_metadata_cannot_be_promoted_by_later_empty_results(limits):
    result = collect(FixtureGet([job(), job(22)], [artifact()]), limits=limits)
    assert result["complete"] is False and result["incomplete_reasons"]
    assert result["absence"] == "unknown"


def test_passive_boundary_never_reads_credentials_or_executes_or_downloads(monkeypatch):
    transport = FixtureGet([job()], [artifact()])
    confirmed = evidence()
    module = importlib.import_module("brigade.claude_actions_metadata")

    def forbidden(*args, **kwargs):
        raise AssertionError("metadata collection attempted unauthorized IO")

    with monkeypatch.context() as guard:
        guard.setattr(builtins, "open", forbidden)
        guard.setattr(socket, "socket", forbidden)
        guard.setattr(subprocess, "run", forbidden)
        guard.setattr(subprocess, "Popen", forbidden)
        result = module.collect_metadata(transport, run=confirmed, current_head_sha=HEAD, observed_at=OBSERVED)
    assert result["complete"] is True
    assert [call[0] for call in transport.calls] == [JOBS, ARTIFACTS]


def test_transport_limits_and_exception_redaction_are_applied_at_metadata_boundary():
    transport = FixtureGet(overrides={(JOBS, 1): RuntimeError("FAKE_PRIVATE_EXCEPTION")})
    result = collect(transport)
    assert "transport-unavailable" in result["incomplete_reasons"]
    assert "FAKE_PRIVATE" not in json.dumps(result)
    result = collect(FixtureGet(overrides={(JOBS, 1): GetResponse(200, b"{" * 100)}))
    assert "malformed-payload" in result["incomplete_reasons"]
    result = collect(FixtureGet([job()]), limits=Limits(max_body_bytes=30))
    assert "truncated-response" in result["incomplete_reasons"]
