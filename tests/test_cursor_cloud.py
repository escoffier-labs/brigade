"""Tests for the Cursor Cloud public-beta adapter (#Task3)."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock
from urllib import request as urllib_request

import pytest

from brigade import cloud_tracker, cursor_cloud, fleet_client


_FAKE_KEY = "cursor-api-key-deadbeef"


def _build_basic_token(key: str) -> str:
    return base64.b64encode(f"{key}:".encode()).decode()


def _json_response(body: dict) -> MagicMock:
    response = MagicMock()
    response.status = 200
    response.__enter__ = MagicMock(return_value=response)
    response.__exit__ = MagicMock(return_value=False)
    response.read.return_value = json.dumps(body).encode("utf-8")
    return response


def _error_response(code: int, body: dict | None = None) -> MagicMock:
    from urllib import error as urllib_error

    response = MagicMock()
    response.status = code
    response.read.return_value = (json.dumps(body) if body else "").encode("utf-8")
    err = urllib_error.HTTPError("https://api.cursor.com/v1/agents", code, "err", {}, None)
    err.fp = response  # type: ignore[attr-defined]
    return err


class FakeOpener:
    """Records calls and returns queued responses."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[urllib_request.Request] = []

    def __call__(self, req: urllib_request.Request, timeout: float | None = None):
        self.calls.append(req)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def open(self, req: urllib_request.Request, timeout: float | None = None):
        return self(req, timeout)


class TestInventory:
    def test_list_agents_builds_basic_auth_without_leaking_key(self):
        opener = FakeOpener(
            [
                _json_response({"items": [{"id": "agent-1", "name": "a", "latestRunId": "run-1"}], "nextCursor": None}),
                _json_response({"id": "run-1", "status": "FINISHED"}),
            ]
        )
        agents = cursor_cloud.list_agents(_FAKE_KEY, opener=opener.open, max_pages=1, max_items=10)
        assert len(agents) == 1
        list_req = opener.calls[0]
        auth_header = list_req.get_header("Authorization")
        assert auth_header.startswith("Basic ")
        assert _FAKE_KEY not in auth_header
        assert _build_basic_token(_FAKE_KEY) in auth_header

    def test_list_agents_paginates_until_exhausted_or_bounded(self):
        opener = FakeOpener(
            [
                _json_response({"items": [{"id": "a1", "latestRunId": "r1"}], "nextCursor": "c1"}),
                _json_response({"id": "r1", "status": "RUNNING"}),
                _json_response({"items": [{"id": "a2", "latestRunId": "r2"}], "nextCursor": None}),
                _json_response({"id": "r2", "status": "FINISHED"}),
            ]
        )
        agents = cursor_cloud.list_agents(_FAKE_KEY, opener=opener.open, max_pages=5, max_items=10)
        assert len(agents) == 2
        assert {a["id"] for a in agents} == {"a1", "a2"}
        assert "cursor=c1" in opener.calls[2].full_url

    def test_list_agents_respects_max_pages_and_max_items(self):
        opener = FakeOpener(
            [
                _json_response({"items": [{"id": "a1", "latestRunId": "r1"}], "nextCursor": "c1"}),
                _json_response({"id": "r1", "status": "RUNNING"}),
                _json_response({"items": [{"id": "a2", "latestRunId": "r2"}], "nextCursor": "c2"}),
                _json_response({"id": "r2", "status": "RUNNING"}),
            ]
        )
        agents = cursor_cloud.list_agents(_FAKE_KEY, opener=opener.open, max_pages=1, max_items=10)
        assert len(agents) == 1
        assert agents[0]["id"] == "a1"
        assert len(opener.calls) == 2  # one list page + one run fetch

    def test_list_agents_resolves_latest_run_state_via_run_endpoint(self):
        opener = FakeOpener(
            [
                _json_response({"items": [{"id": "a1", "latestRunId": "r1"}], "nextCursor": None}),
                _json_response({"id": "r1", "status": "RUNNING"}),
            ]
        )
        agents = cursor_cloud.list_agents(_FAKE_KEY, opener=opener.open)
        assert agents[0]["latestRunState"] == "running"
        run_req = opener.calls[1]
        assert run_req.full_url.endswith("/v1/agents/a1/runs/r1")

    def test_list_agents_active_only_filters_terminal_states(self):
        opener = FakeOpener(
            [
                _json_response(
                    {
                        "items": [{"id": "a1", "latestRunId": "r1"}, {"id": "a2", "latestRunId": "r2"}],
                        "nextCursor": None,
                    }
                ),
                _json_response({"id": "r1", "status": "RUNNING"}),
                _json_response({"id": "r2", "status": "FINISHED"}),
            ]
        )
        agents = cursor_cloud.list_agents(_FAKE_KEY, opener=opener.open, active_only=True)
        assert len(agents) == 1
        assert agents[0]["id"] == "a1"

    def test_list_agents_skips_run_fetch_when_missing_latest_run_id(self):
        opener = FakeOpener(
            [
                _json_response({"items": [{"id": "a1", "latestRunId": None}], "nextCursor": None}),
            ]
        )
        agents = cursor_cloud.list_agents(_FAKE_KEY, opener=opener.open)
        assert len(agents) == 1
        assert agents[0]["latestRunState"] is None
        assert len(opener.calls) == 1


class TestStateAndErrors:
    @pytest.mark.parametrize(
        ("raw", "expected", "active", "terminal"),
        [
            ("CREATING", "creating", True, False),
            ("RUNNING", "running", True, False),
            ("FINISHED", "finished", False, True),
            ("ERROR", "error", False, True),
            ("CANCELLED", "cancelled", False, True),
            ("EXPIRED", "expired", False, True),
            ("", None, False, False),
            (None, None, False, False),
        ],
    )
    def test_normalize_state(self, raw, expected, active, terminal):
        assert cursor_cloud.normalize_state(raw) == expected
        assert cursor_cloud.is_active_state(expected) is active
        assert cursor_cloud.is_terminal_state(expected) is terminal

    def test_sanitized_error_omits_key_header_and_body(self):
        opener = FakeOpener([_error_response(500, {"message": "boom", "presignedArtifactUrl": "https://secret"})])
        with pytest.raises(cursor_cloud.CursorCloudError) as exc_info:
            cursor_cloud.list_agents(_FAKE_KEY, opener=opener.open)
        text = str(exc_info.value)
        assert _FAKE_KEY not in text
        assert "Authorization" not in text
        assert "boom" not in text
        assert "presignedArtifactUrl" not in text
        assert "https://secret" not in text

    def test_sanitized_error_omits_prompt_when_launching(self, monkeypatch):
        opener = FakeOpener([_error_response(400, {"message": "bad prompt"})])
        monkeypatch.setattr(
            fleet_client,
            "admit_cloud",
            lambda *a, **k: fleet_client.CloudDecision(
                granted=True, reason="ok", lease={"lease_id": "lease-1"}, holder="h1"
            ),
        )
        monkeypatch.setattr(
            fleet_client, "bind_cloud", lambda *a, **k: fleet_client.CloudDecision(granted=True, reason="ok")
        )
        monkeypatch.setattr(
            fleet_client, "release_cloud", lambda *a, **k: fleet_client.CloudDecision(granted=True, reason="ok")
        )
        result = cursor_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="secret prompt text",
            opener=opener.open,
        )
        assert not result.ok
        text = str(result)
        assert "secret prompt text" not in text
        assert "bad prompt" not in text

    def test_request_honors_deadline(self):
        captured: list[float | None] = []

        def record_timeout(req, timeout=None):
            captured.append(timeout)
            raise Exception("network down")

        with pytest.raises(cursor_cloud.CursorCloudError):
            cursor_cloud.list_agents(_FAKE_KEY, opener=record_timeout, deadline=2.5)
        assert captured == [2.5]


class TestLaunchGate:
    def test_admission_denial_makes_zero_provider_calls(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            fleet_client,
            "admit_cloud",
            lambda *a, **k: fleet_client.CloudDecision(False, "refused", holder="h1"),
        )

        def fake_opener(req, timeout=None):
            calls.append(req)
            return _json_response({})

        result = cursor_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=fake_opener,
        )
        assert not result.ok
        assert result.reason == "refused"
        assert calls == []

    def test_admission_then_create_binds_agent_and_run_ids(self, monkeypatch):
        opener = FakeOpener(
            [
                _json_response(
                    {
                        "agent": {"id": "agent-new", "latestRunId": "run-new", "repos": [{"url": "owner/repo"}]},
                        "run": {"id": "run-new", "status": "CREATING"},
                    }
                ),
            ]
        )
        bind_calls = []
        monkeypatch.setattr(
            fleet_client,
            "admit_cloud",
            lambda *a, **k: fleet_client.CloudDecision(True, "ok", lease={"lease_id": "lease-1"}, holder="h1"),
        )
        monkeypatch.setattr(
            fleet_client,
            "bind_cloud",
            lambda *a, **k: bind_calls.append(k) or fleet_client.CloudDecision(True, "ok"),
        )
        monkeypatch.setattr(fleet_client, "release_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
        result = cursor_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=opener.open,
        )
        assert result.ok
        assert result.agent_id == "agent-new"
        assert result.run_id == "run-new"
        assert bind_calls
        assert bind_calls[0]["provider_task_id"] == "agent-new"

    def test_submit_failure_releases_unbound_lease(self, monkeypatch):
        opener = FakeOpener([_error_response(500, {"message": "boom"})])
        release_calls = []

        def capture_release(*args, **kwargs):
            release_calls.append({"args": args, "kwargs": kwargs})
            return fleet_client.CloudDecision(True, "ok")

        monkeypatch.setattr(
            fleet_client,
            "admit_cloud",
            lambda *a, **k: fleet_client.CloudDecision(True, "ok", lease={"lease_id": "lease-1"}, holder="h1"),
        )
        monkeypatch.setattr(fleet_client, "bind_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
        monkeypatch.setattr(fleet_client, "release_cloud", capture_release)
        result = cursor_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=opener.open,
        )
        assert not result.ok
        assert result.reason == "submit-failed"
        assert release_calls
        assert release_calls[0]["args"][0] == "lease-1"

    def test_transport_timeout_holds_lease_and_returns_uncertain(self, monkeypatch):
        from urllib import error as urllib_error

        opener = FakeOpener([urllib_error.URLError("timed out")])
        release_calls = []

        def capture_release(*args, **kwargs):
            release_calls.append({"args": args, "kwargs": kwargs})
            return fleet_client.CloudDecision(True, "ok")

        monkeypatch.setattr(
            fleet_client,
            "admit_cloud",
            lambda *a, **k: fleet_client.CloudDecision(True, "ok", lease={"lease_id": "lease-1"}, holder="h1"),
        )
        monkeypatch.setattr(fleet_client, "bind_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
        monkeypatch.setattr(fleet_client, "release_cloud", capture_release)
        result = cursor_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=opener.open,
        )
        assert not result.ok
        assert result.reason == "uncertain"
        assert release_calls == []

    def test_auto_create_pr_default_off(self, monkeypatch):
        create_calls: list[dict] = []

        def capture_create(req, timeout=None):
            create_calls.append({"url": req.full_url, "data": req.data})
            return _json_response(
                {
                    "agent": {"id": "agent-pr", "latestRunId": "run-pr", "repos": [{"url": "owner/repo"}]},
                    "run": {"id": "run-pr", "status": "CREATING"},
                }
            )

        monkeypatch.setattr(
            fleet_client,
            "admit_cloud",
            lambda *a, **k: fleet_client.CloudDecision(True, "ok", lease={"lease_id": "lease-1"}, holder="h1"),
        )
        monkeypatch.setattr(fleet_client, "bind_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
        monkeypatch.setattr(fleet_client, "release_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
        cursor_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=capture_create,
        )
        assert create_calls
        body = json.loads(create_calls[0]["data"])
        assert body.get("prompt") == {"text": "hi"}
        assert body.get("repos") == [{"url": "https://github.com/owner/repo"}]
        assert body.get("autoCreatePR") is False
        assert "autoMerge" not in body
        assert body.get("agentId", "").startswith("bc-")

    def test_explicit_opt_in_can_enable_pr(self, monkeypatch):
        create_calls: list[dict] = []

        def capture_create(req, timeout=None):
            create_calls.append({"data": req.data})
            return _json_response(
                {
                    "agent": {"id": "agent-opt", "latestRunId": "run-opt", "repos": [{"url": "owner/repo"}]},
                    "run": {"id": "run-opt", "status": "CREATING"},
                }
            )

        monkeypatch.setattr(
            fleet_client,
            "admit_cloud",
            lambda *a, **k: fleet_client.CloudDecision(True, "ok", lease={"lease_id": "lease-1"}, holder="h1"),
        )
        monkeypatch.setattr(fleet_client, "bind_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
        monkeypatch.setattr(fleet_client, "release_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
        cursor_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            auto_create_pr=True,
            opener=capture_create,
        )
        body = json.loads(create_calls[0]["data"])
        assert body.get("autoCreatePR") is True
        assert "autoMerge" not in body
        assert body.get("agentId", "").startswith("bc-")

    def test_client_agent_id_is_sent_and_preserved_on_success(self, monkeypatch):
        create_calls: list[dict] = []

        def capture_create(req, timeout=None):
            create_calls.append({"data": req.data})
            return _json_response(
                {
                    "agent": {"id": "agent-sent", "latestRunId": "run-sent"},
                    "run": {"id": "run-sent", "status": "CREATING"},
                }
            )

        monkeypatch.setattr(
            fleet_client,
            "admit_cloud",
            lambda *a, **k: fleet_client.CloudDecision(True, "ok", lease={"lease_id": "lease-1"}, holder="h1"),
        )
        monkeypatch.setattr(fleet_client, "bind_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
        monkeypatch.setattr(fleet_client, "release_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
        result = cursor_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=capture_create,
        )
        assert result.ok
        assert result.agent_id == "agent-sent"
        body = json.loads(create_calls[0]["data"])
        assert body["agentId"].startswith("bc-")

    def test_malformed_response_after_post_holds_lease_and_returns_uncertain(self, monkeypatch):
        response = MagicMock()
        response.status = 200
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        response.read.return_value = b"not-json"
        release_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        def capture_release(*args, **kwargs):
            release_calls.append((args, kwargs))
            return fleet_client.CloudDecision(True, "ok")

        monkeypatch.setattr(
            fleet_client,
            "admit_cloud",
            lambda *a, **k: fleet_client.CloudDecision(True, "ok", lease={"lease_id": "lease-1"}, holder="h1"),
        )
        monkeypatch.setattr(fleet_client, "bind_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
        monkeypatch.setattr(fleet_client, "release_cloud", capture_release)
        result = cursor_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=lambda req, timeout=None: response,
        )
        assert not result.ok
        assert result.reason == "uncertain"
        assert result.agent_id and result.agent_id.startswith("bc-")
        assert release_calls == []

    def test_oversized_response_after_post_holds_lease_and_returns_uncertain(self, monkeypatch):
        response = MagicMock()
        response.status = 200
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        response.read.return_value = b"x" * (64 * 1024 + 2)
        release_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        def capture_release(*args, **kwargs):
            release_calls.append((args, kwargs))
            return fleet_client.CloudDecision(True, "ok")

        monkeypatch.setattr(
            fleet_client,
            "admit_cloud",
            lambda *a, **k: fleet_client.CloudDecision(True, "ok", lease={"lease_id": "lease-1"}, holder="h1"),
        )
        monkeypatch.setattr(fleet_client, "bind_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
        monkeypatch.setattr(fleet_client, "release_cloud", capture_release)
        result = cursor_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=lambda req, timeout=None: response,
        )
        assert not result.ok
        assert result.reason == "uncertain"
        assert release_calls == []

    def test_oserror_after_post_holds_lease_and_returns_uncertain(self, monkeypatch):
        release_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        def capture_release(*args, **kwargs):
            release_calls.append((args, kwargs))
            return fleet_client.CloudDecision(True, "ok")

        def raise_oserror(req, timeout=None):
            raise OSError("network down")

        monkeypatch.setattr(
            fleet_client,
            "admit_cloud",
            lambda *a, **k: fleet_client.CloudDecision(True, "ok", lease={"lease_id": "lease-1"}, holder="h1"),
        )
        monkeypatch.setattr(fleet_client, "bind_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
        monkeypatch.setattr(fleet_client, "release_cloud", capture_release)
        result = cursor_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=raise_oserror,
        )
        assert not result.ok
        assert result.reason == "uncertain"
        assert result.agent_id and result.agent_id.startswith("bc-")
        assert release_calls == []

    def test_timeout_error_after_post_holds_lease_and_returns_uncertain(self, monkeypatch):
        release_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

        def capture_release(*args, **kwargs):
            release_calls.append((args, kwargs))
            return fleet_client.CloudDecision(True, "ok")

        def raise_timeout(req, timeout=None):
            raise TimeoutError("deadline")

        monkeypatch.setattr(
            fleet_client,
            "admit_cloud",
            lambda *a, **k: fleet_client.CloudDecision(True, "ok", lease={"lease_id": "lease-1"}, holder="h1"),
        )
        monkeypatch.setattr(fleet_client, "bind_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
        monkeypatch.setattr(fleet_client, "release_cloud", capture_release)
        result = cursor_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=raise_timeout,
        )
        assert not result.ok
        assert result.reason == "uncertain"
        assert result.agent_id and result.agent_id.startswith("bc-")
        assert release_calls == []

    def test_no_provider_call_without_admitted_lease_id(self, monkeypatch):
        calls: list[urllib_request.Request] = []

        def fake_opener(req, timeout=None):
            calls.append(req)
            return _json_response({"agent": {"id": "a", "latestRunId": "r"}, "run": {"id": "r"}})

        monkeypatch.setattr(
            fleet_client,
            "admit_cloud",
            lambda *a, **k: fleet_client.CloudDecision(True, "ok", lease={"lease_id": None}, holder="h1"),
        )
        monkeypatch.setattr(fleet_client, "bind_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
        monkeypatch.setattr(fleet_client, "release_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
        result = cursor_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt="hi",
            opener=fake_opener,
        )
        assert not result.ok
        assert result.reason == "no-lease"
        assert calls == []

    def test_repo_normalization(self):
        assert cursor_cloud.normalize_repo("owner/repo") == "https://github.com/owner/repo"
        assert cursor_cloud.normalize_repo("https://github.com/owner/repo") == "https://github.com/owner/repo"
        assert cursor_cloud.normalize_repo("https://github.com/owner/repo/") == "https://github.com/owner/repo"
        assert cursor_cloud.normalize_repo("http://github.com/owner/repo") is None
        assert cursor_cloud.normalize_repo("https://gitlab.com/owner/repo") is None
        assert cursor_cloud.normalize_repo("file:///etc/passwd") is None
        assert cursor_cloud.normalize_repo("owner/repo/extra") is None
        assert cursor_cloud.normalize_repo("") is None

    def test_bad_repo_rejects_before_admission(self, monkeypatch):
        admit_calls: list[dict[str, Any]] = []
        monkeypatch.setattr(
            fleet_client,
            "admit_cloud",
            lambda *a, **k: admit_calls.append(k) or fleet_client.CloudDecision(True, "ok"),
        )
        result = cursor_cloud.launch_agent(
            _FAKE_KEY,
            repo="http://evil.com/owner/repo",
            prompt="hi",
        )
        assert not result.ok
        assert result.reason == "bad-repo"
        assert admit_calls == []


class TestRegistryContract:
    def test_cursor_cloud_register_and_adopt_accepted(self, tmp_path: Path):
        entry = cloud_tracker.register(
            tmp_path,
            provider="cursor-cloud",
            task_id="agent-1",
            label="cursor-task",
            prompt_hash="sha256:aa",
        )
        assert entry["provider"] == "cursor-cloud"
        adopted = cloud_tracker.adopt(
            tmp_path,
            provider="cursor-cloud",
            branch="cursor/adopted",
            label="adopted",
        )
        assert adopted["provider"] == "cursor-cloud"
        assert adopted["source"] == "adopt-branch"


class TestLeaseLabelPrivacy:
    def test_lease_label_carries_no_prompt_text(self, monkeypatch):
        opener = FakeOpener(
            [
                _json_response(
                    {
                        "agent": {"id": "agent-label", "latestRunId": "run-label"},
                        "run": {"id": "run-label", "status": "CREATING"},
                    }
                ),
            ]
        )
        admit_calls: list[dict[str, Any]] = []

        def capture_admit(*args, **kwargs):
            admit_calls.append(kwargs)
            return fleet_client.CloudDecision(True, "ok", lease={"lease_id": "lease-1"}, holder="h1")

        monkeypatch.setattr(fleet_client, "admit_cloud", capture_admit)
        monkeypatch.setattr(fleet_client, "bind_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))
        monkeypatch.setattr(fleet_client, "release_cloud", lambda *a, **k: fleet_client.CloudDecision(True, "ok"))

        prompt = "rotate the production credentials for acme corp"
        result = cursor_cloud.launch_agent(
            _FAKE_KEY,
            repo="owner/repo",
            prompt=prompt,
            opener=opener.open,
        )
        assert result.ok
        label = admit_calls[0]["label"]
        expected_digest = cloud_tracker.prompt_hash(prompt).removeprefix("sha256:")[:12]
        assert label == f"cursor-cloud:owner/repo@{expected_digest}"
        assert len(label) <= cloud_tracker.LEASE_LABEL_MAX
        for word in prompt.split():
            assert word not in label
        assert prompt not in json.dumps(admit_calls)
