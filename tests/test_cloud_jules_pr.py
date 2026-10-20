"""Jules sessions reach ``landed`` through their provider PR URL (#1794 item 2)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from brigade import cloud_tracker

NOW = datetime(2026, 8, 12, 20, 0, 0, tzinfo=timezone.utc)
PR_URL = "https://github.com/owner/repo/pull/42"
HEAD = "fix-widget-parser-8837263551"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _register(tmp_path: Path, task_id: str = "sess-1", **extra: Any) -> dict[str, Any]:
    return cloud_tracker.register(
        tmp_path,
        provider="jules",
        task_id=task_id,
        label=f"jules:owner/repo@{task_id}",
        prompt_hash="sha256:" + "a" * 64,
        dispatched_at=_iso(NOW),
        **extra,
    )


def _observation(task_id: str = "sess-1", url: str | None = PR_URL, state: str = "completed"):
    info: dict[str, Any] = {"state": state}
    if url is not None:
        info["pull_request_url"] = url
    return {"jules": cloud_tracker.ProviderObservation(True, True, None, {task_id: info})}


def _pr(state: str, *, url: str = PR_URL, head: str = HEAD) -> dict[str, Any]:
    return {"head": head, "state": state, "number": 42, "url": url, "isDraft": False, "headRefOid": "abc"}


def _row(tmp_path: Path, github: dict[str, Any], observations: dict[str, Any], task_id: str = "sess-1"):
    payload = cloud_tracker.status_payload(
        tmp_path, now=NOW, github=github, provider_observations=observations, cursor_wired=False
    )
    return payload, next(r for r in payload["entries"] if r.get("task_id") == task_id)


def test_merged_provider_pr_lands_the_session_and_records_its_branch(tmp_path: Path):
    _register(tmp_path)
    github = {"branches": [], "prs": [_pr("MERGED")]}
    _payload, row = _row(tmp_path, github, _observation())
    assert row["classification"] == "landed"
    assert row["branch"] == HEAD
    assert row["pr"]["url"] == PR_URL
    assert row["evidence"]["github"]["branch"] == HEAD


def test_open_provider_pr_is_ready_to_land_not_stale_or_investigation(tmp_path: Path):
    _register(tmp_path)
    github = {"branches": [], "prs": [_pr("OPEN")]}
    _payload, row = _row(tmp_path, github, _observation())
    assert row["classification"] == "ready-to-land"
    assert row["branch"] == HEAD
    assert row["pr"]["state"] == "OPEN"


def test_provider_pr_that_is_not_in_the_github_snapshot_changes_nothing(tmp_path: Path):
    _register(tmp_path)
    _payload, row = _row(tmp_path, {"branches": [], "prs": []}, _observation())
    assert row["classification"] == "ready-to-land"
    assert row["branch"] is None
    assert row["pr"] is None


def test_a_different_pr_url_is_never_adopted(tmp_path: Path):
    _register(tmp_path)
    other = _pr("MERGED", url="https://github.com/owner/repo/pull/43", head="unrelated-branch")
    _payload, row = _row(tmp_path, {"branches": [], "prs": [other]}, _observation())
    assert row["classification"] == "ready-to-land"
    assert row["branch"] is None


def test_explicit_entry_branch_is_kept_and_the_provider_pr_still_binds(tmp_path: Path):
    _register(tmp_path, branch="jules/ready-fix")
    github = {"branches": [{"name": "jules/ready-fix"}], "prs": [_pr("MERGED")]}
    _payload, row = _row(tmp_path, github, _observation())
    assert row["classification"] == "landed"
    assert row["branch"] == "jules/ready-fix"


def test_linked_prefixed_head_branch_is_not_reported_as_an_orphan(tmp_path: Path):
    _register(tmp_path)
    head = "jules/fix-widget-parser"
    github = {"branches": [{"name": head}], "prs": [_pr("OPEN", head=head)]}
    payload, row = _row(tmp_path, github, _observation())
    assert row["branch"] == head
    assert not [r for r in payload["entries"] if r.get("source") == "github-branch"]


def test_other_providers_never_adopt_a_provider_pr(tmp_path: Path):
    cloud_tracker.register(
        tmp_path,
        provider="cursor-cloud",
        task_id="agent-1",
        label="cursor:owner/repo@1",
        prompt_hash="sha256:" + "a" * 64,
        dispatched_at=_iso(NOW),
    )
    observations = {
        "cursor-cloud": cloud_tracker.ProviderObservation(
            True, True, None, {"agent-1": {"state": "completed", "pull_request_url": PR_URL}}
        )
    }
    _payload, row = _row(tmp_path, {"branches": [], "prs": [_pr("MERGED")]}, observations, task_id="agent-1")
    assert row["classification"] != "landed"
    assert row["branch"] is None


def _fake_providers(monkeypatch, observations: dict[str, Any], github: dict[str, Any]) -> None:
    def observe(provider: str, _target: Path):
        return observations.get(provider, cloud_tracker.ProviderObservation(False, False, "unconfigured", {}))

    monkeypatch.setattr(cloud_tracker, "observe_provider", observe)
    monkeypatch.setattr(cloud_tracker, "observe_github", lambda _target: {**github, "prs": list(github["prs"])})


def _gh_view(calls: list[list[str]], payloads: dict[str, dict[str, Any]]):
    def run_text(command: list[str], *, cwd: Path | None = None, timeout: float = 30.0):
        calls.append(command)
        payload = payloads.get(command[3]) if command[:3] == ["gh", "pr", "view"] else None
        if payload is None:
            return 1, "", "not found"
        return 0, json.dumps(payload), ""

    return run_text


def _gh_pr(state: str = "MERGED", *, url: str = PR_URL, head: str = HEAD) -> dict[str, Any]:
    return {"number": 42, "state": state, "headRefName": head, "url": url, "isDraft": False, "headRefOid": "abc"}


def test_observation_resolves_the_provider_pr_by_url_and_lands_the_session(monkeypatch, tmp_path: Path):
    _register(tmp_path)
    _fake_providers(monkeypatch, _observation(), {"branches": [], "prs": [], "repo": "owner/repo"})
    calls: list[list[str]] = []
    monkeypatch.setattr(cloud_tracker, "_run_text", _gh_view(calls, {PR_URL: _gh_pr("MERGED")}))

    observations, github = cloud_tracker.observe_provider_details(tmp_path)

    views = [c for c in calls if c[:3] == ["gh", "pr", "view"]]
    assert len(views) == 1 and views[0][3] == PR_URL
    assert github["prs"] == [
        {"head": HEAD, "state": "MERGED", "number": 42, "url": PR_URL, "isDraft": False, "headRefOid": "abc"}
    ]
    payload = cloud_tracker.status_payload(
        tmp_path, now=NOW, github=github, provider_observations=observations, cursor_wired=False
    )
    row = next(r for r in payload["entries"] if r["task_id"] == "sess-1")
    assert row["classification"] == "landed" and row["branch"] == HEAD


def test_observation_skips_urls_the_snapshot_already_has(monkeypatch, tmp_path: Path):
    _register(tmp_path)
    _fake_providers(monkeypatch, _observation(), {"branches": [], "prs": [_pr("OPEN")]})
    calls: list[list[str]] = []
    monkeypatch.setattr(cloud_tracker, "_run_text", _gh_view(calls, {}))
    _observations, github = cloud_tracker.observe_provider_details(tmp_path)
    assert [c for c in calls if c[:3] == ["gh", "pr", "view"]] == []
    assert len(github["prs"]) == 1


def test_observation_only_resolves_registered_sessions_with_a_valid_github_pr_url(monkeypatch, tmp_path: Path):
    _register(tmp_path, "sess-registered")
    tasks = {
        "sess-registered": {"state": "completed", "pull_request_url": "https://example.com/owner/repo/pull/1"},
        "sess-unregistered": {"state": "completed", "pull_request_url": PR_URL},
    }
    observations = {"jules": cloud_tracker.ProviderObservation(True, True, None, tasks)}
    _fake_providers(monkeypatch, observations, {"branches": [], "prs": []})
    calls: list[list[str]] = []
    monkeypatch.setattr(cloud_tracker, "_run_text", _gh_view(calls, {PR_URL: _gh_pr()}))
    _observations, github = cloud_tracker.observe_provider_details(tmp_path)
    assert [c for c in calls if c[:3] == ["gh", "pr", "view"]] == []
    assert github["prs"] == []


def test_observation_survives_a_failed_or_malformed_pr_lookup(monkeypatch, tmp_path: Path):
    _register(tmp_path)
    _fake_providers(monkeypatch, _observation(), {"branches": [], "prs": []})
    monkeypatch.setattr(cloud_tracker, "_run_text", lambda *a, **k: (1, "", "boom"))
    _observations, github = cloud_tracker.observe_provider_details(tmp_path)
    assert github["prs"] == []
    monkeypatch.setattr(cloud_tracker, "_run_text", lambda *a, **k: (0, "[not json", ""))
    _observations, github = cloud_tracker.observe_provider_details(tmp_path)
    assert github["prs"] == []
    mismatched = _gh_pr(url="https://github.com/other/repo/pull/9")
    monkeypatch.setattr(cloud_tracker, "_run_text", lambda *a, **k: (0, json.dumps(mismatched), ""))
    _observations, github = cloud_tracker.observe_provider_details(tmp_path)
    assert github["prs"] == []
