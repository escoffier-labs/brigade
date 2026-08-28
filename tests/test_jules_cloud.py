"""Tests for the Jules Cloud alpha adapter (Task 4)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from urllib import error as urllib_error
from urllib import request as urllib_request

import pytest

from brigade import cloud_tracker, fleet_client, jules_cloud


def test_jules_redirect_handler_refuses_cross_origin_and_https_downgrade():
    handler = jules_cloud._JulesRedirectHandler()
    request = jules_cloud._build_request("https://jules.googleapis.com/v1alpha/sessions", "secret")

    assert handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/steal") is None
    assert (
        handler.redirect_request(request, None, 302, "Found", {}, "http://jules.googleapis.com/v1alpha/sessions")
        is None
    )


def test_jules_source_sanitizer_preserves_official_nested_source_name():
    source = _source()
    source["name"] = "sources/github/owner/repo"
    sanitized = jules_cloud.sanitize_source(source)
    assert sanitized is not None
    assert sanitized["name"] == "sources/github/owner/repo"


_FAKE_KEY = "jules-api-key-deadbeef"


def _json_response(body: dict) -> MagicMock:
    response = MagicMock()
    response.status = 200
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    response.read.return_value = json.dumps(body).encode("utf-8")
    return response


def _raw_response(raw: bytes) -> MagicMock:
    response = MagicMock()
    response.status = 200
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    response.read.return_value = raw
    return response


def _error_response(code: int, body: dict | None = None) -> urllib_error.HTTPError:
    response = MagicMock()
    response.status = code
    response.read.return_value = (json.dumps(body) if body else "").encode("utf-8")
    err = urllib_error.HTTPError("https://jules.googleapis.com/v1alpha/sessions", code, "err", {}, None)
    err.fp = response  # type: ignore[attr-defined]
    return err


def _header(request: urllib_request.Request, name: str) -> str | None:
    """urllib normalizes header capitalization, so match case-insensitively."""
    wanted = name.lower()
    for key, value in (request.headers or {}).items():
        if key.lower() == wanted:
            return value
    return None


def _source(
    owner: str = "owner",
    repo: str = "repo",
    branches: tuple[str, ...] = ("main", "dev"),
    default: str | None = "main",
) -> dict[str, Any]:
    github: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "branches": [{"displayName": name} for name in branches],
        # Fields the adapter must drop.
        "isPrivate": True,
        "description": "PRIVATE-DESCRIPTION",
    }
    if default is not None:
        github["defaultBranch"] = {"displayName": default}
    return {
        "name": f"sources/github-{owner}-{repo}",
        "id": f"github-{owner}-{repo}",
        "githubRepo": github,
    }


def _sources_page(*sources: dict[str, Any]) -> dict[str, Any]:
    return {"sources": list(sources), "nextPageToken": None}


class FakeOpener:
    """Records calls and returns queued responses."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[urllib_request.Request] = []

    def __call__(self, req: urllib_request.Request, timeout: float | None = None):
        self.calls.append(req)
        if not self.responses:
            raise AssertionError(f"unexpected provider call: {req.method} {req.full_url}")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def open(self, req: urllib_request.Request, timeout: float | None = None):
        return self(req, timeout)

    @property
    def posts(self) -> list[urllib_request.Request]:
        return [c for c in self.calls if c.method == "POST"]

    @property
    def gets(self) -> list[urllib_request.Request]:
        return [c for c in self.calls if c.method == "GET"]


def _launch_opener(*after_sources, sources: dict[str, Any] | None = None) -> FakeOpener:
    """An opener that answers the read-only /sources probe, then the launch calls."""
    page = sources if sources is not None else _sources_page(_source())
    return FakeOpener([_json_response(page), *after_sources])


def _grant_hub(monkeypatch, *, lease_id: str | None = "lease-1") -> dict[str, list]:
    """Wire fleet_client so admission succeeds; return the recorded calls."""
    recorded: dict[str, list] = {"admit": [], "bind": [], "release": []}

    def fake_admit(*args, **kwargs):
        recorded["admit"].append({"args": args, "kwargs": kwargs})
        return fleet_client.CloudDecision(granted=True, reason="ok", lease={"lease_id": lease_id}, holder="h1")

    def fake_bind(*args, **kwargs):
        recorded["bind"].append({"args": args, "kwargs": kwargs})
        return fleet_client.CloudDecision(granted=True, reason="ok")

    def fake_release(*args, **kwargs):
        recorded["release"].append({"args": args, "kwargs": kwargs})
        return fleet_client.CloudDecision(granted=True, reason="ok")

    monkeypatch.setattr(fleet_client, "admit_cloud", fake_admit)
    monkeypatch.setattr(fleet_client, "bind_cloud", fake_bind)
    monkeypatch.setattr(fleet_client, "release_cloud", fake_release)
    return recorded


class TestInventory:
    def test_list_sessions_sends_api_key_header_without_leaking_key(self):
        opener = FakeOpener(
            [
                _json_response({"sessions": [{"id": "sess-1", "state": "IN_PROGRESS"}], "nextPageToken": None}),
            ]
        )
        sessions = jules_cloud.list_sessions(_FAKE_KEY, opener=opener.open, max_pages=1, max_items=10)
        assert len(sessions) == 1
        req = opener.calls[0]
        # urllib title-cases header names, so the lookup must be case-insensitive.
        assert _header(req, "X-Goog-Api-Key") == _FAKE_KEY
        assert _header(req, "Authorization") is None
        assert _FAKE_KEY not in json.dumps(sessions)

    def test_list_sessions_paginates_until_exhausted_or_bounded(self):
        opener = FakeOpener(
            [
                _json_response({"sessions": [{"id": "s1", "state": "QUEUED"}], "nextPageToken": "p1"}),
                _json_response({"sessions": [{"id": "s2", "state": "COMPLETED"}], "nextPageToken": None}),
            ]
        )
        sessions = jules_cloud.list_sessions(_FAKE_KEY, opener=opener.open, max_pages=5, max_items=10)
        assert len(sessions) == 2
        assert {s["id"] for s in sessions} == {"s1", "s2"}
        assert "pageToken=p1" in opener.calls[1].full_url

    def test_list_sessions_respects_max_pages_and_max_items(self):
        opener = FakeOpener(
            [
                _json_response({"sessions": [{"id": "s1", "state": "QUEUED"}], "nextPageToken": "p1"}),
                _json_response({"sessions": [{"id": "s2", "state": "COMPLETED"}], "nextPageToken": "p2"}),
            ]
        )
        sessions = jules_cloud.list_sessions(_FAKE_KEY, opener=opener.open, max_pages=1, max_items=10)
        assert len(sessions) == 1
        assert sessions[0]["id"] == "s1"
        assert len(opener.calls) == 1

    def test_max_items_truncates_within_a_single_page(self):
        opener = FakeOpener(
            [
                _json_response(
                    {
                        "sessions": [
                            {"id": "s1", "state": "QUEUED"},
                            {"id": "s2", "state": "QUEUED"},
                            {"id": "s3", "state": "QUEUED"},
                        ],
                        "nextPageToken": "p1",
                    }
                ),
            ]
        )
        sessions = jules_cloud.list_sessions(_FAKE_KEY, opener=opener.open, max_pages=5, max_items=2)
        assert [s["id"] for s in sessions] == ["s1", "s2"]
        assert len(opener.calls) == 1

    def test_list_sources_paginates_and_sanitizes(self):
        opener = FakeOpener(
            [
                _json_response({"sources": [_source("a", "one")], "nextPageToken": "t1"}),
                _json_response({"sources": [_source("b", "two")], "nextPageToken": None}),
            ]
        )
        sources = jules_cloud.list_sources(_FAKE_KEY, opener=opener.open, max_pages=5, max_items=10)
        assert [s["name"] for s in sources] == ["sources/github-a-one", "sources/github-b-two"]
        assert sources[0]["owner"] == "a"
        assert sources[0]["repo"] == "one"
        assert sources[0]["branches"] == ["main", "dev"]
        assert sources[0]["default_branch"] == "main"
        assert "pageToken=t1" in opener.calls[1].full_url
        assert "PRIVATE-DESCRIPTION" not in json.dumps(sources)
        assert "isPrivate" not in json.dumps(sources)

    def test_list_activities_paginates_and_drops_payloads(self):
        opener = FakeOpener(
            [
                _json_response(
                    {
                        "activities": [
                            {
                                "id": "act-1",
                                "createTime": "2026-08-12T20:00:00Z",
                                "description": "PRIVATE",
                                "agentMessaged": {"message": "PRIVATE"},
                                "artifacts": [{"patch": "PRIVATE"}],
                            }
                        ],
                        "nextPageToken": None,
                    }
                ),
            ]
        )
        activities = jules_cloud.list_activities("sess-1", _FAKE_KEY, opener=opener.open, max_pages=1, max_items=10)
        assert activities == [{"id": "act-1", "create_time": "2026-08-12T20:00:00Z"}]
        assert "PRIVATE" not in json.dumps(activities)
        # Collection reads always carry the bounded pageSize query.
        url = opener.calls[0].full_url
        assert "/sessions/sess-1/activities?" in url
        assert "pageSize=50" in url

    def test_get_session_fetches_by_id_and_sanitizes(self):
        opener = FakeOpener(
            [
                _json_response(
                    {
                        "id": "sess-1",
                        "name": "sessions/sess-1",
                        "state": "IN_PROGRESS",
                        "createTime": "2026-08-12T20:00:00Z",
                        "updateTime": "2026-08-12T21:00:00Z",
                        "url": "https://jules.google.com/task/sess-1",
                        "title": "PRIVATE-TITLE",
                        "prompt": "PRIVATE-PROMPT",
                        "outputs": [{"pullRequest": {"url": "https://github.com/owner/repo/pull/7"}}],
                    }
                )
            ]
        )
        session = jules_cloud.get_session("sess-1", _FAKE_KEY, opener=opener.open)
        assert session == {
            "id": "sess-1",
            "state": "in_progress",
            "create_time": "2026-08-12T20:00:00Z",
            "update_time": "2026-08-12T21:00:00Z",
            "url": "https://jules.google.com/task/sess-1",
            "pull_request_url": "https://github.com/owner/repo/pull/7",
        }
        assert opener.calls[0].full_url.endswith("/sessions/sess-1")

    def test_get_session_drops_unvalidated_urls_and_timestamps(self):
        opener = FakeOpener(
            [
                _json_response(
                    {
                        "id": "sess-2",
                        "state": "COMPLETED",
                        "createTime": "not-a-timestamp",
                        "url": "https://evil.example.com/jules",
                        "pullRequestUrl": "https://evil.example.com/owner/repo/pull/1",
                    }
                )
            ]
        )
        session = jules_cloud.get_session("sess-2", _FAKE_KEY, opener=opener.open)
        assert session["url"] is None
        assert session["pull_request_url"] is None
        assert session["create_time"] is None
        assert "evil.example.com" not in json.dumps(session)

    def test_session_id_derived_from_resource_name_when_id_absent(self):
        opener = FakeOpener([_json_response({"sessions": [{"name": "sessions/from-name"}]})])
        sessions = jules_cloud.list_sessions(_FAKE_KEY, opener=opener.open, max_pages=1)
        assert [s["id"] for s in sessions] == ["from-name"]

    @pytest.mark.parametrize("page_size", [0, -1, 101, 1000])
    def test_page_size_bounds_enforced(self, page_size):
        opener = FakeOpener([])
        with pytest.raises(jules_cloud.JulesCloudError):
            jules_cloud.list_sessions(_FAKE_KEY, opener=opener.open, page_size=page_size)
        assert opener.calls == []

    @pytest.mark.parametrize("max_pages", [0, -1, 21])
    def test_max_pages_bounds_enforced(self, max_pages):
        opener = FakeOpener([])
        with pytest.raises(jules_cloud.JulesCloudError):
            jules_cloud.list_sessions(_FAKE_KEY, opener=opener.open, max_pages=max_pages)
        assert opener.calls == []

    @pytest.mark.parametrize("max_items", [0, -1, 1001])
    def test_max_items_bounds_enforced(self, max_items):
        opener = FakeOpener([])
        with pytest.raises(jules_cloud.JulesCloudError):
            jules_cloud.list_sessions(_FAKE_KEY, opener=opener.open, max_items=max_items)
        assert opener.calls == []

    def test_base_url_override_is_honored_for_tests(self):
        opener = FakeOpener([_json_response({"sessions": [], "nextPageToken": None})])
        jules_cloud.list_sessions(_FAKE_KEY, base_url="http://127.0.0.1:9099/v1alpha", opener=opener.open, max_pages=1)
        assert opener.calls[0].full_url.startswith("http://127.0.0.1:9099/v1alpha/sessions?")

    @pytest.mark.parametrize(
        "base_url",
        ["", "   ", "http://jules.googleapis.com/v1alpha", "ftp://example.com", "https://x/v1?a=b"],
    )
    def test_bad_base_url_rejected_before_any_call(self, base_url):
        opener = FakeOpener([])
        with pytest.raises(jules_cloud.JulesCloudError):
            jules_cloud.list_sessions(_FAKE_KEY, base_url=base_url, opener=opener.open)
        assert opener.calls == []


class TestIdentifierValidation:
    @pytest.mark.parametrize("session_id", ["", "   ", "a/b", "sessions/abc", "../secrets", 42, None])
    def test_bad_session_ids_rejected_before_any_call(self, session_id):
        for fn in (jules_cloud.get_session, jules_cloud.approve_plan):
            opener = FakeOpener([])
            with pytest.raises(jules_cloud.JulesCloudError):
                fn(session_id, _FAKE_KEY, opener=opener.open)
            assert opener.calls == []
        opener = FakeOpener([])
        with pytest.raises(jules_cloud.JulesCloudError):
            jules_cloud.list_activities(session_id, _FAKE_KEY, opener=opener.open)
        assert opener.calls == []

    def test_session_id_path_segment_is_percent_encoded(self):
        opener = FakeOpener([_json_response({"id": "weird id?x=1", "state": "QUEUED"})])
        jules_cloud.get_session("weird id?x=1", _FAKE_KEY, opener=opener.open)
        assert opener.calls[0].full_url.endswith("/sessions/weird%20id%3Fx%3D1")

    @pytest.mark.parametrize("mode", ["auto_create_pr", "AUTO_MERGE", "", "  ", 1, True])
    def test_invalid_automation_mode_rejected_before_post(self, mode):
        opener = FakeOpener([])
        with pytest.raises(jules_cloud.JulesCloudError):
            jules_cloud.create_session(
                _FAKE_KEY,
                prompt="hi",
                source_name="sources/github-owner-repo",
                starting_branch="main",
                automation_mode=mode,
                opener=opener.open,
            )
        assert opener.calls == []

    @pytest.mark.parametrize("source_name", ["", "USER", "owner/repo", "sources/", "sources/a/b/c/d/e", None])
    def test_invalid_source_name_rejected_before_post(self, source_name):
        opener = FakeOpener([])
        with pytest.raises(jules_cloud.JulesCloudError):
            jules_cloud.create_session(
                _FAKE_KEY,
                prompt="hi",
                source_name=source_name,
                starting_branch="main",
                opener=opener.open,
            )
        assert opener.calls == []

    @pytest.mark.parametrize("branch", ["", "  ", "-leading-dash", None, 7])
    def test_invalid_starting_branch_rejected_before_post(self, branch):
        opener = FakeOpener([])
        with pytest.raises(jules_cloud.JulesCloudError):
            jules_cloud.create_session(
                _FAKE_KEY,
                prompt="hi",
                source_name="sources/github-owner-repo",
                starting_branch=branch,
                opener=opener.open,
            )
        assert opener.calls == []

    def test_create_requires_top_level_id(self):
        opener = FakeOpener([_json_response({"session": {"id": "nested-only"}, "state": "QUEUED"})])
        with pytest.raises(jules_cloud.JulesCloudError):
            jules_cloud.create_session(
                _FAKE_KEY,
                prompt="hi",
                source_name="sources/github-owner-repo",
                starting_branch="main",
                opener=opener.open,
            )
        assert len(opener.posts) == 1


class TestSourceResolution:
    def test_resolve_source_matches_owner_and_repo_exactly(self):
        opener = FakeOpener(
            [
                _json_response(
                    _sources_page(
                        _source("other", "repo"),
                        _source("owner", "repo-extra"),
                        _source("owner", "repo"),
                    )
                )
            ]
        )
        source = jules_cloud.resolve_source(_FAKE_KEY, "https://github.com/owner/repo", opener=opener.open)
        assert source is not None
        assert source["name"] == "sources/github-owner-repo"
        assert opener.posts == []

    def test_resolve_source_is_case_insensitive_on_owner_and_repo(self):
        opener = FakeOpener([_json_response(_sources_page(_source("Owner", "Repo")))])
        source = jules_cloud.resolve_source(_FAKE_KEY, "owner/repo", opener=opener.open)
        assert source is not None
        assert source["name"] == "sources/github-Owner-Repo"

    def test_resolve_source_returns_none_when_repo_not_connected(self):
        opener = FakeOpener([_json_response(_sources_page(_source("other", "thing")))])
        assert jules_cloud.resolve_source(_FAKE_KEY, "owner/repo", opener=opener.open) is None

    def test_source_without_github_repo_is_dropped(self):
        opener = FakeOpener([_json_response(_sources_page({"name": "sources/x", "id": "x"}))])
        assert jules_cloud.list_sources(_FAKE_KEY, opener=opener.open, max_pages=1) == []

    def test_select_branch_validates_request_against_listed_branches(self):
        source = jules_cloud.sanitize_source(_source(branches=("main", "dev")))
        assert source is not None
        assert jules_cloud.select_branch(source, "dev") == "dev"
        assert jules_cloud.select_branch(source, "nope") is None
        assert jules_cloud.select_branch(source, "") is None
        assert jules_cloud.select_branch(source) == "main"

    def test_select_branch_is_none_when_ambiguous(self):
        source = jules_cloud.sanitize_source(_source(branches=("alpha", "beta"), default=None))
        assert source is not None
        assert jules_cloud.select_branch(source) is None

    def test_select_branch_picks_sole_listed_branch(self):
        source = jules_cloud.sanitize_source(_source(branches=("trunk",), default=None))
        assert source is not None
        assert jules_cloud.select_branch(source) == "trunk"


class TestStateAndErrors:
    @pytest.mark.parametrize(
        ("raw", "active", "terminal"),
        [
            ("STATE_UNSPECIFIED", True, False),
            ("QUEUED", True, False),
            ("PLANNING", True, False),
            ("AWAITING_PLAN_APPROVAL", True, False),
            ("AWAITING_USER_FEEDBACK", True, False),
            ("IN_PROGRESS", True, False),
            ("PAUSED", True, False),
            ("FAILED", False, True),
            ("COMPLETED", False, True),
            ("", False, False),
            (None, False, False),
        ],
    )
    def test_normalize_state_and_active_terminal(self, raw, active, terminal):
        assert jules_cloud.is_active_state(raw) is active
        assert jules_cloud.is_terminal_state(raw) is terminal

    def test_sanitized_error_omits_key_and_body(self):
        opener = FakeOpener([_error_response(500, {"message": "boom", "activityDescription": "secret"})])
        with pytest.raises(jules_cloud.JulesCloudError) as exc_info:
            jules_cloud.list_sessions(_FAKE_KEY, opener=opener.open)
        text = str(exc_info.value)
        assert _FAKE_KEY not in text
        assert "boom" not in text
        assert "activityDescription" not in text
        assert "secret" not in text

    def test_sanitized_error_omits_prompt_when_launching(self, monkeypatch):
        opener = _launch_opener(_error_response(400, {"message": "bad prompt"}))
        _grant_hub(monkeypatch)
        result = jules_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="secret prompt text",
            opener=opener.open,
        )
        assert not result.ok
        assert result.reason == "submit-failed"
        text = json.dumps(
            {
                "reason": result.reason,
                "session_id": result.session_id,
                "source_name": result.source_name,
                "starting_branch": result.starting_branch,
            }
        )
        assert "secret prompt text" not in text
        assert "bad prompt" not in text

    def test_activities_redacted_in_error(self):
        opener = FakeOpener([_error_response(500, {"activities": [{"description": "secret activity"}]})])
        with pytest.raises(jules_cloud.JulesCloudError) as exc_info:
            jules_cloud.list_activities("sess-1", _FAKE_KEY, opener=opener.open)
        text = str(exc_info.value)
        assert "secret activity" not in text
        assert "activities" not in text

    def test_oversized_response_raises_bounded_error(self):
        opener = FakeOpener([_raw_response(b"x" * (64 * 1024 + 2))])
        with pytest.raises(jules_cloud.JulesCloudError) as exc_info:
            jules_cloud.list_sessions(_FAKE_KEY, opener=opener.open)
        assert "size limit" in str(exc_info.value)

    def test_malformed_response_raises_bounded_error(self):
        opener = FakeOpener([_raw_response(b"not-json")])
        with pytest.raises(jules_cloud.JulesCloudError) as exc_info:
            jules_cloud.list_sessions(_FAKE_KEY, opener=opener.open)
        assert "valid JSON" in str(exc_info.value)


class TestLaunchGate:
    def test_admission_denial_makes_zero_provider_mutation(self, monkeypatch):
        opener = _launch_opener()
        monkeypatch.setattr(
            fleet_client,
            "admit_cloud",
            lambda *a, **k: fleet_client.CloudDecision(False, "refused", holder="h1"),
        )
        result = jules_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=opener.open,
        )
        assert not result.ok
        assert result.reason == "refused"
        # The read-only source probe is allowed; nothing was created.
        assert opener.posts == []
        assert len(opener.gets) == 1

    def test_missing_source_makes_zero_admission_and_zero_provider_mutation(self, monkeypatch):
        opener = _launch_opener(sources=_sources_page(_source("someone", "else")))
        recorded = _grant_hub(monkeypatch)
        result = jules_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=opener.open,
        )
        assert not result.ok
        assert result.reason == "source-not-found"
        assert recorded["admit"] == []
        assert recorded["bind"] == []
        assert recorded["release"] == []
        assert opener.posts == []

    def test_source_lookup_failure_makes_zero_admission(self, monkeypatch):
        opener = FakeOpener([_error_response(500, {"message": "boom"})])
        recorded = _grant_hub(monkeypatch)
        result = jules_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=opener.open,
        )
        assert not result.ok
        assert result.reason == "source-lookup-failed"
        assert recorded["admit"] == []
        assert opener.posts == []

    def test_unlisted_branch_makes_zero_admission_and_zero_provider_mutation(self, monkeypatch):
        opener = _launch_opener(sources=_sources_page(_source(branches=("main",))))
        recorded = _grant_hub(monkeypatch)
        result = jules_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            starting_branch="does-not-exist",
            opener=opener.open,
        )
        assert not result.ok
        assert result.reason == "bad-branch"
        assert recorded["admit"] == []
        assert opener.posts == []

    def test_admission_then_create_uses_exact_source_name_and_binds_session_id(self, monkeypatch):
        opener = _launch_opener(_json_response({"id": "sess-new", "state": "QUEUED"}))
        recorded = _grant_hub(monkeypatch)
        result = jules_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=opener.open,
        )
        assert result.ok
        assert result.session_id == "sess-new"
        assert result.source_name == "sources/github-owner-repo"
        assert result.starting_branch == "main"
        assert recorded["bind"][0]["kwargs"]["provider_task_id"] == "sess-new"

        assert len(opener.posts) == 1
        body = json.loads(opener.posts[0].data)
        assert body["sourceContext"] == {
            "source": "sources/github-owner-repo",
            "githubRepoContext": {"startingBranch": "main"},
        }
        # The invalid literal "USER" must never appear as sourceContext.source.
        assert body["sourceContext"]["source"] != "USER"
        assert body["requirePlanApproval"] is True
        assert "automationMode" not in body

    def test_source_probe_precedes_admission_and_post(self, monkeypatch):
        order: list[str] = []
        opener = FakeOpener([_json_response(_sources_page(_source())), _json_response({"id": "sess-order"})])

        def tracking_opener(req, timeout=None):
            order.append(f"{req.method} {req.full_url.split('?')[0].rsplit('/', 1)[-1]}")
            return opener(req, timeout)

        def fake_admit(*args, **kwargs):
            order.append("admit")
            return fleet_client.CloudDecision(True, "ok", lease={"lease_id": "lease-1"}, holder="h1")

        monkeypatch.setattr(fleet_client, "admit_cloud", fake_admit)
        monkeypatch.setattr(fleet_client, "bind_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
        monkeypatch.setattr(fleet_client, "release_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
        jules_cloud.launch_agent(_FAKE_KEY, repo="owner/repo", prompt="hi", opener=tracking_opener)
        assert order == ["GET sources", "admit", "POST sessions"]

    def test_lease_label_carries_no_prompt_text(self, monkeypatch):
        opener = _launch_opener(_json_response({"id": "sess-label", "state": "QUEUED"}))
        recorded = _grant_hub(monkeypatch)
        prompt = "rotate the production credentials for acme corp"
        jules_cloud.launch_agent(_FAKE_KEY, repo="owner/repo", prompt=prompt, opener=opener.open)

        admit_kwargs = recorded["admit"][0]["kwargs"]
        label = admit_kwargs["label"]
        expected_digest = cloud_tracker.prompt_hash(prompt).removeprefix("sha256:")[:12]
        assert label == f"jules:owner/repo@{expected_digest}"
        assert len(label) <= cloud_tracker.LEASE_LABEL_MAX
        for word in prompt.split():
            assert word not in label
        assert prompt not in json.dumps(admit_kwargs)

    def test_submit_failure_releases_unbound_lease(self, monkeypatch):
        opener = _launch_opener(_error_response(500, {"message": "boom"}))
        recorded = _grant_hub(monkeypatch)
        result = jules_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=opener.open,
        )
        assert not result.ok
        assert result.reason == "submit-failed"
        assert recorded["release"]
        assert recorded["release"][0]["args"][0] == "lease-1"
        assert recorded["release"][0]["kwargs"].get("state") == "submit-failed"

    @pytest.mark.parametrize(
        "failure",
        [
            "malformed",
            "oversized",
            "no-id",
            OSError("network down"),
            TimeoutError("deadline"),
            urllib_error.URLError("timed out"),
        ],
        ids=["malformed", "oversized", "no-id", "oserror", "timeout", "urlerror"],
    )
    def test_ambiguous_post_holds_lease_issues_one_post_and_returns_uncertain(self, monkeypatch, failure):
        if failure == "malformed":
            after = _raw_response(b"not-json")
        elif failure == "oversized":
            after = _raw_response(b"x" * (64 * 1024 + 2))
        elif failure == "no-id":
            after = _json_response({"state": "QUEUED"})
        else:
            after = failure
        opener = _launch_opener(after)
        recorded = _grant_hub(monkeypatch)
        result = jules_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=opener.open,
        )
        assert not result.ok
        assert result.reason == "uncertain"
        # The lease is held: ambiguity never releases capacity.
        assert recorded["release"] == []
        assert recorded["bind"] == []
        # And ambiguity is never retried: exactly one POST was issued.
        assert len(opener.posts) == 1
        assert opener.responses == []

    def test_bind_failure_holds_lease_and_reports_session_id(self, monkeypatch):
        opener = _launch_opener(_json_response({"id": "sess-bind", "state": "QUEUED"}))
        recorded = _grant_hub(monkeypatch)
        monkeypatch.setattr(fleet_client, "bind_cloud", lambda *a, **k: fleet_client.CloudDecision(False, "hub-down"))
        result = jules_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=opener.open,
        )
        assert not result.ok
        assert result.reason == "bind-failed"
        assert result.session_id == "sess-bind"
        assert recorded["release"] == []

    def test_missing_lease_id_makes_zero_provider_mutation(self, monkeypatch):
        opener = _launch_opener()
        recorded = _grant_hub(monkeypatch, lease_id=None)
        result = jules_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=opener.open,
        )
        assert not result.ok
        assert result.reason == "no-lease"
        assert opener.posts == []
        assert recorded["bind"] == []
        assert recorded["release"] == []

    def test_successful_bind_registers_ids_hash_and_private_holder(self, tmp_path: Path, monkeypatch):
        opener = _launch_opener(_json_response({"id": "sess-new", "state": "QUEUED"}))
        _grant_hub(monkeypatch)
        prompt = "implement the private feature"
        result = jules_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt=prompt,
            title="feature work",
            starting_branch="dev",
            opener=opener.open,
            register_target=tmp_path,
            label="jules-task",
        )
        assert result.ok
        assert result.session_id == "sess-new"
        assert "holder" not in result.__dataclass_fields__
        assert not hasattr(result, "holder")
        entries = cloud_tracker.load_registry(tmp_path)["entries"]
        assert len(entries) == 1
        entry = entries[0]
        assert entry["provider"] == "jules"
        assert entry["task_id"] == "sess-new"
        assert entry["session_id"] == "sess-new"
        assert entry["label"] == "jules-task"
        assert entry["prompt_hash"] == cloud_tracker.prompt_hash(prompt)
        assert entry["expected_artifact"] == {"kind": "diff"}
        assert entry["lease_holder"] == "h1"
        assert prompt not in json.dumps(entry)
        body = json.loads(opener.posts[0].data)
        assert body["title"] == "feature work"
        assert body["sourceContext"]["githubRepoContext"]["startingBranch"] == "dev"

    def test_submit_failure_does_not_register(self, tmp_path: Path, monkeypatch):
        opener = _launch_opener(_error_response(500, {"message": "boom"}))
        _grant_hub(monkeypatch)
        result = jules_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=opener.open,
            register_target=tmp_path,
            label="jules-fail",
        )
        assert not result.ok
        assert result.reason == "submit-failed"
        assert cloud_tracker.load_registry(tmp_path)["entries"] == []

    def test_register_failure_after_bind_returns_tracking_failed_and_holds_lease(self, tmp_path: Path, monkeypatch):
        opener = _launch_opener(_json_response({"id": "sess-live", "state": "QUEUED"}))
        recorded = _grant_hub(monkeypatch)
        monkeypatch.setattr(
            cloud_tracker,
            "register",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )
        result = jules_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="secret launch prompt",
            opener=opener.open,
            register_target=tmp_path,
            label="jules-track",
        )
        assert not result.ok
        assert result.reason == "tracking-failed"
        assert result.session_id == "sess-live"
        assert not hasattr(result, "holder")
        assert "h1" not in json.dumps(result.__dict__)
        assert "secret launch prompt" not in json.dumps(result.__dict__)
        assert _FAKE_KEY not in json.dumps(result.__dict__)
        assert recorded["release"] == []
        assert cloud_tracker.load_registry(tmp_path)["entries"] == []

    def test_unwritable_registry_makes_zero_provider_mutation(self, tmp_path: Path, monkeypatch):
        opener = _launch_opener(_json_response({"id": "sess-skip", "state": "QUEUED"}))
        recorded = _grant_hub(monkeypatch)
        monkeypatch.setattr(
            jules_cloud.os,
            "open",
            lambda *a, **k: (_ for _ in ()).throw(OSError("readonly")),
        )
        result = jules_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=opener.open,
            register_target=tmp_path,
            label="jules-readonly",
        )
        assert not result.ok
        assert result.reason == "registry-unwritable"
        assert opener.calls == []
        assert recorded["admit"] == []
        assert recorded["bind"] == []

    @pytest.mark.parametrize("live_bytes", [b"{not-json", b'{"legacy": true}\n'])
    def test_registry_preflight_leaves_live_registry_bytes_unchanged(self, tmp_path: Path, live_bytes: bytes):
        registry = cloud_tracker.registry_path(tmp_path)
        registry.parent.mkdir(parents=True)
        registry.write_bytes(live_bytes)

        assert jules_cloud._preflight_registry(tmp_path)
        assert registry.read_bytes() == live_bytes

    def test_registry_preflight_does_not_clobber_concurrent_entry(self, tmp_path: Path, monkeypatch):
        registry = cloud_tracker.registry_path(tmp_path)
        registry.parent.mkdir(parents=True)
        concurrent_bytes = b'{"concurrent": true}\n'
        real_fsync = jules_cloud.os.fsync

        def write_concurrent_then_sync(descriptor: int) -> None:
            registry.write_bytes(concurrent_bytes)
            real_fsync(descriptor)

        monkeypatch.setattr(jules_cloud.os, "fsync", write_concurrent_then_sync)

        assert jules_cloud._preflight_registry(tmp_path)
        assert registry.read_bytes() == concurrent_bytes

    def test_registry_preflight_does_not_unlink_a_colliding_sidecar(self, tmp_path: Path, monkeypatch):
        registry_dir = cloud_tracker.registry_path(tmp_path).parent
        registry_dir.mkdir(parents=True)
        sidecar = registry_dir / ".registry-preflight-collision"
        sidecar.write_bytes(b"concurrent sidecar")
        monkeypatch.setattr(jules_cloud, "uuid4", lambda: type("FixedUuid", (), {"hex": "collision"})())

        assert not jules_cloud._preflight_registry(tmp_path)
        assert sidecar.read_bytes() == b"concurrent sidecar"

    def test_auto_create_pr_default_off(self, monkeypatch):
        opener = _launch_opener(_json_response({"id": "sess-pr", "state": "QUEUED"}))
        _grant_hub(monkeypatch)
        jules_cloud.launch_agent(_FAKE_KEY, repo="owner/repo", prompt="hi", opener=opener.open)
        body = json.loads(opener.posts[0].data)
        assert body.get("prompt") == "hi"
        assert body.get("requirePlanApproval") is True
        assert "automationMode" not in body

    def test_explicit_opt_in_enables_auto_create_pr(self, monkeypatch):
        opener = _launch_opener(_json_response({"id": "sess-opt", "state": "QUEUED"}))
        _grant_hub(monkeypatch)
        jules_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            auto_create_pr=True,
            opener=opener.open,
        )
        body = json.loads(opener.posts[0].data)
        assert body.get("automationMode") == "AUTO_CREATE_PR"
        assert body.get("requirePlanApproval") is True

    def test_body_nesting_with_title_and_validated_starting_branch(self, monkeypatch):
        opener = _launch_opener(_json_response({"id": "sess-nested", "state": "PLANNING"}))
        _grant_hub(monkeypatch)
        jules_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="implement feature",
            title="feature work",
            starting_branch="dev",
            opener=opener.open,
        )
        body = json.loads(opener.posts[0].data)
        assert body["title"] == "feature work"
        assert body["sourceContext"]["source"] == "sources/github-owner-repo"
        assert body["sourceContext"]["githubRepoContext"]["startingBranch"] == "dev"
        assert body["prompt"] == "implement feature"

    def test_approve_plan_accepts_empty_success_body(self):
        opener = FakeOpener([_raw_response(b"")])
        assert jules_cloud.approve_plan("sess-1", _FAKE_KEY, opener=opener.open) == {}
        req = opener.calls[0]
        assert req.full_url.endswith("/sessions/sess-1:approvePlan")
        assert req.data == b"{}"
        assert req.method == "POST"

    def test_approve_plan_accepts_empty_json_object_body(self):
        opener = FakeOpener([_json_response({})])
        assert jules_cloud.approve_plan("sess-1", _FAKE_KEY, opener=opener.open) == {}

    def test_approve_plan_returns_nothing_from_a_populated_body(self):
        opener = FakeOpener([_json_response({"plan": "PRIVATE", "prompt": "PRIVATE"})])
        assert jules_cloud.approve_plan("sess-1", _FAKE_KEY, opener=opener.open) == {}

    def test_bad_repo_rejects_before_admission_and_before_any_call(self, monkeypatch):
        admit_calls: list[dict[str, Any]] = []
        opener = FakeOpener([])
        monkeypatch.setattr(
            fleet_client,
            "admit_cloud",
            lambda *a, **k: admit_calls.append(k) or fleet_client.CloudDecision(True, "ok"),
        )
        result = jules_cloud.launch_agent(
            _FAKE_KEY,
            repo="http://evil.com/owner/repo",
            prompt="hi",
            opener=opener.open,
        )
        assert not result.ok
        assert result.reason == "bad-repo"
        assert admit_calls == []
        assert opener.calls == []

    def test_repo_normalization(self):
        assert jules_cloud.normalize_repo("owner/repo") == "https://github.com/owner/repo"
        assert jules_cloud.normalize_repo("https://github.com/owner/repo") == "https://github.com/owner/repo"
        assert jules_cloud.normalize_repo("https://github.com/owner/repo/") == "https://github.com/owner/repo"
        assert jules_cloud.normalize_repo("http://github.com/owner/repo") is None
        assert jules_cloud.normalize_repo("https://gitlab.com/owner/repo") is None
        assert jules_cloud.normalize_repo("file:///etc/passwd") is None
        assert jules_cloud.normalize_repo("owner/repo/extra") is None
        assert jules_cloud.normalize_repo("") is None

    def test_split_repo(self):
        assert jules_cloud.split_repo("owner/repo") == ("owner", "repo")
        assert jules_cloud.split_repo("https://github.com/owner/repo") == ("owner", "repo")
        assert jules_cloud.split_repo("https://gitlab.com/owner/repo") is None


class TestTrackerIntegration:
    def test_observe_jules_cloud_tasks_maps_sanitized_inventory(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("JULES_API_KEY", _FAKE_KEY)
        monkeypatch.setattr(
            jules_cloud,
            "list_sessions",
            lambda *a, **k: [
                {"id": "sess-1", "state": "in_progress"},
                {"id": "sess-2", "state": "completed"},
            ],
        )
        tasks = cloud_tracker.observe_jules_cloud_tasks(tmp_path)
        assert tasks == {
            "sess-1": {"state": "in_progress"},
            "sess-2": {"state": "completed"},
        }

    def test_observe_jules_cloud_tasks_stays_bounded_on_api_failure(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("JULES_API_KEY", _FAKE_KEY)
        monkeypatch.setattr(
            jules_cloud,
            "list_sessions",
            lambda *a, **k: (_ for _ in ()).throw(jules_cloud.JulesCloudError("boom")),
        )
        cloud_tracker.register(
            tmp_path,
            provider="jules",
            task_id="sess-existing",
            label="jules:owner/repo@0011223344ff",
            prompt_hash="sha256:x",
            dispatched_at="2026-08-12T20:00:00Z",
        )
        tasks = cloud_tracker.observe_jules_cloud_tasks(tmp_path)
        assert tasks == {}
        now = datetime(2026, 8, 12, 20, 0, 0, tzinfo=timezone.utc)
        payload = cloud_tracker.status_payload(
            tmp_path,
            now=now,
            provider_tasks=tasks,
            github={"branches": [], "prs": []},
            cursor_wired=False,
        )
        row = next(r for r in payload["entries"] if r["task_id"] == "sess-existing")
        assert row["classification"] == "pending"

    def test_status_payload_shows_jules_wired_when_key_present(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("JULES_API_KEY", _FAKE_KEY)
        monkeypatch.setattr(cloud_tracker, "observe_jules_cloud_tasks", lambda *a, **k: {})
        payload = cloud_tracker.status_payload(tmp_path)
        assert payload["sources"]["jules"]["wired"] is True
        assert payload["sources"]["jules"]["authority"] == "alpha REST"

    def test_status_payload_shows_jules_unwired_when_key_absent(self, monkeypatch, tmp_path: Path):
        monkeypatch.delenv("JULES_API_KEY", raising=False)
        monkeypatch.setattr(
            cloud_tracker,
            "observe_jules_cloud_tasks",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("must not be called")),
        )
        payload = cloud_tracker.status_payload(tmp_path)
        assert payload["sources"]["jules"]["wired"] is False
        assert payload["sources"]["jules"]["authority"] == "unwired"

    def test_sync_releases_terminal_jules_session(self, monkeypatch, tmp_path: Path):
        cloud_tracker.register(
            tmp_path,
            provider="jules",
            task_id="sess-1",
            label="jules:owner/repo@0011223344ff",
            prompt_hash="sha256:aa",
            dispatched_at="2026-08-12T20:00:00Z",
        )
        release_calls: list[dict[str, Any]] = []

        def capture_release(lease_id: str, *, state: str | None = None, holder: str | None = None):
            release_calls.append({"lease_id": lease_id, "state": state, "holder": holder})
            return fleet_client.CloudDecision(True, "ok")

        monkeypatch.setattr(fleet_client, "release_cloud", capture_release)
        payload = cloud_tracker.sync_payload(
            tmp_path,
            provider_tasks={"sess-1": {"state": "completed"}},
            github={"branches": [], "prs": []},
            cursor_wired=False,
            hub_leases=[{"lease_id": "lease-1", "provider_task_id": "sess-1"}],
        )
        assert payload["counts"]["released"] == 1
        assert release_calls
        assert release_calls[0]["lease_id"] == "lease-1"
