"""Offline contracts for explicitly confirmed Claude Action run evidence."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import pytest

from brigade import claude_actions_evidence as actions


REPO = "example-owner/example-repo"
HEAD = "a" * 40
REVISION = "b" * 40
OBSERVED = "2026-10-06T10:00:00Z"


def workflow(**overrides: Any) -> actions.ConfirmedWorkflow:
    return actions.ConfirmedWorkflow(**{"repository": REPO, "workflow_id": 23, "revision": REVISION, **overrides})


def run(**overrides: Any) -> dict[str, Any]:
    return {
        "id": 101,
        "run_attempt": 2,
        "workflow_id": 23,
        "repository": {"full_name": REPO},
        "event": "pull_request",
        "head_branch": "feature/example",
        "head_sha": HEAD,
        "status": "completed",
        "conclusion": "success",
        "html_url": f"https://github.com/{REPO}/actions/runs/101/attempts/2",
        "url": f"https://api.github.com/repos/{REPO}/actions/runs/101",
        "created_at": "2026-10-06T09:00:00Z",
        "run_started_at": "2026-10-06T09:01:00Z",
        "updated_at": "2026-10-06T09:03:00Z",
        "pull_requests": [{"number": 7, "head": {"sha": HEAD}, "base": {"sha": REVISION}}],
        **overrides,
    }


def normalize(**overrides: Any) -> actions.ActionRunEvidence:
    return actions.normalize_run(run(**overrides), workflow=workflow(), observed_at=OBSERVED, current_head_sha=HEAD)


def test_confirmed_run_preserves_attempt_provenance_without_review_claims():
    evidence = actions.normalize_run(
        run(), workflow=workflow(), observed_at=OBSERVED, current_head_sha=HEAD, runner_kind="github-hosted"
    )
    assert evidence.identity == (REPO, 101, 2)
    assert evidence.workflow == workflow()
    assert evidence.event == "pull_request"
    assert evidence.branch == "feature/example"
    assert evidence.head_sha == HEAD
    assert evidence.head_relation == "current-head"
    assert (evidence.status, evidence.conclusion) == ("completed", "success")
    assert evidence.provider == "github-actions"
    assert evidence.runner_kind == "github-hosted"
    assert evidence.observed_at == OBSERVED
    assert evidence.created_at == "2026-10-06T09:00:00Z"
    assert evidence.started_at == "2026-10-06T09:01:00Z"
    assert evidence.updated_at == "2026-10-06T09:03:00Z"
    assert evidence.html_url == f"https://github.com/{REPO}/actions/runs/101/attempts/2"
    assert evidence.api_url == f"https://api.github.com/repos/{REPO}/actions/runs/101"
    assert evidence.pull_requests[0].number == 7
    assert evidence.pull_requests[0].head_sha == HEAD
    assert evidence.pull_requests[0].base_sha == REVISION
    assert evidence.pull_requests[0].url == f"https://github.com/{REPO}/pull/7"
    assert evidence.review_state == "unknown"
    assert evidence.findings_state == "unknown"
    assert evidence.merge_readiness == "unknown"
    assert evidence.local_node_id is None


@pytest.mark.parametrize(
    ("status", "conclusion", "expected_status", "expected_conclusion"),
    [
        ("queued", None, "queued", "unknown"),
        ("in_progress", None, "in_progress", "unknown"),
        ("requested", None, "requested", "unknown"),
        ("waiting", None, "waiting", "unknown"),
        ("pending", None, "pending", "unknown"),
        ("completed", "failure", "completed", "failure"),
        ("completed", "cancelled", "completed", "cancelled"),
        ("completed", "skipped", "completed", "skipped"),
        ("completed", "timed_out", "completed", "timed_out"),
        ("completed", "neutral", "completed", "neutral"),
        ("completed", "action_required", "completed", "action_required"),
        ("completed", "stale", "completed", "stale"),
        ("completed", None, "completed", "unknown"),
        ("completed", "made-up", "completed", "unknown"),
        ("cancelled", "success", "unknown", "unknown"),
        (None, "success", "unknown", "unknown"),
        ("queued", "success", "queued", "unknown"),
        ("in_progress", "failure", "in_progress", "unknown"),
        ({}, [], "unknown", "unknown"),
    ],
)
def test_lifecycle_and_outcome_never_infer_each_other(status, conclusion, expected_status, expected_conclusion):
    evidence = normalize(status=status, conclusion=conclusion)
    assert (evidence.status, evidence.conclusion) == (expected_status, expected_conclusion)
    assert evidence.review_state == evidence.findings_state == evidence.merge_readiness == "unknown"


def test_refresh_identity_is_stable_and_reruns_and_repositories_are_distinct():
    first = normalize(run_attempt=1)
    assert normalize(run_attempt=1) == first
    assert normalize().identity != first.identity
    other_repo = "example-owner/another-example"
    other = actions.normalize_run(
        run(repository={"full_name": other_repo}), workflow=workflow(repository=other_repo), observed_at=OBSERVED
    )
    assert other.identity != first.identity


def test_stale_run_never_becomes_current_from_pr_association_or_success():
    old = normalize(head_sha="c" * 40)
    assert old.head_relation == "stale-head"
    assert old.pull_requests[0].head_sha == HEAD
    assert old.conclusion == "success"
    assert old.review_state == "unknown"
    assert actions.normalize_run(run(), workflow=workflow(), observed_at=OBSERVED).head_relation == "unknown"
    assert normalize(head_sha=None).head_relation == "unknown"
    assert (
        actions.normalize_run(
            run(), workflow=workflow(), observed_at=OBSERVED, current_head_sha="invalid"
        ).head_relation
        == "unknown"
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"workflow_id": 24, "name": "Claude Code Review"},
        {"workflow_id": None, "name": "Claude Code Review"},
        {"workflow_id": True},
        {"repository": {"full_name": "other-owner/example-repo"}},
        {"repository": {}},
        {"id": None},
        {"id": True},
        {"id": 0},
        {"id": "101"},
        {"run_attempt": None},
        {"run_attempt": False},
        {"run_attempt": -1},
        {"run_attempt": 1 << 64},
    ],
)
def test_wrong_or_missing_identity_is_rejected_without_name_inference(changes):
    with pytest.raises(actions.ActionEvidenceError):
        normalize(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"repository": "../example-repo"},
        {"repository": "owner/repo/extra"},
        {"repository": "owner/repo?token=secret"},
        {"workflow_id": False},
        {"revision": "main"},
        {"revision": None},
    ],
)
def test_confirmation_requires_bounded_exact_workflow_identity(changes):
    with pytest.raises(actions.ActionEvidenceError):
        workflow(**changes)


def test_canonical_repository_and_sha_casing_do_not_split_identity():
    evidence = actions.normalize_run(
        run(repository={"full_name": REPO.upper()}, head_sha=HEAD.upper()),
        workflow=workflow(repository=REPO.upper(), revision=REVISION.upper()),
        observed_at=OBSERVED,
        current_head_sha=HEAD,
    )
    assert evidence.identity == (REPO, 101, 2)
    assert evidence.workflow.revision == REVISION
    assert evidence.head_sha == HEAD
    assert evidence.head_relation == "current-head"


@pytest.mark.parametrize(
    "url",
    [
        "http://github.com/example-owner/example-repo/actions/runs/101",
        "https://github.com.evil.example/example-owner/example-repo/actions/runs/101",
        "https://user:secret@github.com/example-owner/example-repo/actions/runs/101",  # content-guard: allow email
        "https://github.com:443/example-owner/example-repo/actions/runs/101",
        f"https://github.com/{REPO}/actions/runs/102",
        f"https://github.com/{REPO}/actions/runs/101/attempts/1",
        f"https://github.com/{REPO}/actions/runs/101?token=secret",
        f"https://github.com/{REPO}/actions/runs/101#secret",
        f"https://github.com/{REPO}/actions/runs/101/logs",
        f"https://github.com/{REPO}/actions/runs/%31%30%31",
        f"https://github.com/{REPO}/actions/runs/101\n",
        101,
    ],
)
def test_untrusted_run_links_are_dropped(url):
    assert normalize(html_url=url).html_url is None
    assert normalize(url=url).api_url is None


def test_valid_links_match_exact_repository_run_attempt_and_read_only_route():
    assert normalize(html_url=f"https://github.com/{REPO}/actions/runs/101").html_url is not None
    api = f"https://api.github.com/repos/{REPO}/actions/runs/101/attempts/2"
    assert normalize(url=api).api_url == api
    assert normalize(url=f"https://api.github.com/repos/{REPO}/actions/runs/101/rerun").api_url is None
    assert normalize(html_url="https://github.com/another-owner/example-repo/actions/runs/101").html_url is None


def test_provider_times_validate_and_normalize_utc_without_fabricating_completion():
    evidence = normalize(
        created_at="2026-10-06T11:00:00+02:00", updated_at="not-a-time", run_started_at="2026-10-06T09:01:00"
    )
    assert evidence.created_at == "2026-10-06T09:00:00Z"
    assert evidence.updated_at is None
    assert evidence.started_at is None
    assert normalize(status=None, updated_at=OBSERVED).status == "unknown"
    for bad in (None, "", "2026-10-06", "invalid", "2026-10-06T09:00:00", "x" * 100):
        with pytest.raises(actions.ActionEvidenceError):
            actions.normalize_run(run(), workflow=workflow(), observed_at=bad)


def test_pr_associations_are_bounded_deduplicated_and_distinguish_missing_from_empty():
    rows = [{"number": n, "head": {"sha": HEAD}} for n in range(1, 40)]
    evidence = normalize(pull_requests=rows)
    assert len(evidence.pull_requests) == 20
    assert evidence.pull_requests_truncated is True
    assert evidence.pull_requests_state == "partial"
    assert normalize(pull_requests=[]).pull_requests_state == "complete"
    assert normalize(pull_requests=None).pull_requests_state == "unknown"
    assert normalize(pull_requests="not a list").pull_requests_state == "unknown"
    partial = normalize(pull_requests=[{}, {"number": True}, rows[0], rows[0]])
    assert [p.number for p in partial.pull_requests] == [1]
    assert partial.pull_requests_state == "partial"


@pytest.mark.parametrize("offset", ["+00:60", "-00:99", "+12:75", "+99:00"])
def test_invalid_offset_is_unknown_for_provider_and_rejected_for_caller(offset):
    invalid = "2026-10-06T10:00:00" + offset
    evidence = normalize(created_at=invalid, updated_at=invalid, run_started_at=invalid)
    assert evidence.created_at is None
    assert evidence.updated_at is None
    assert evidence.started_at is None
    with pytest.raises(actions.ActionEvidenceError):
        actions.normalize_run(run(), workflow=workflow(), observed_at=invalid)


def test_arbitrary_payloads_and_unbounded_fields_are_not_echoed_or_included_in_errors():
    payload = run(
        name="Claude " + "x" * 5000,
        display_title="secret transcript",
        actor={"login": "private-user"},
        head_commit={"message": "private prompt"},
        logs_url="secret log location",
        event="x" * 100,
        head_branch="x" * 300,
        head_sha="x" * 500,
    )
    evidence = actions.normalize_run(payload, workflow=workflow(), observed_at=OBSERVED)
    assert evidence.event is evidence.branch is evidence.head_sha is None
    assert evidence.runner_kind == "unknown"
    assert "secret" not in repr(asdict(evidence))
    assert "private" not in repr(asdict(evidence))
    with pytest.raises(actions.ActionEvidenceError) as exc:
        actions.normalize_run({"id": "secret"}, workflow=workflow(), observed_at=OBSERVED)
    assert "secret" not in str(exc.value)
    assert normalize(head_branch="example\nbranch").branch is None


def test_offline_normalization_never_reads_files_credentials_or_executes_work(monkeypatch):
    import builtins
    import os
    import socket
    import subprocess

    def forbidden(*args, **kwargs):
        pytest.fail("normalization attempted external IO")

    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    assert normalize().identity == (REPO, 101, 2)
