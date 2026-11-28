from __future__ import annotations

from typing import Any

import pytest

from brigade import memory_hub_client as hubc
from brigade import memory_hub_search as search


class FakeHub:
    def __init__(self, identity: str, *, reject_sessions: bool = False, reject_all: bool = False) -> None:
        self.identity = identity
        self.reject_sessions = reject_sessions
        self.reject_all = reject_all
        self.calls: list[dict[str, Any]] = []

    def call(self, action: str, *, timeout: float, **fields: Any) -> dict[str, Any]:
        assert action == "search"
        self.calls.append(fields)
        if self.reject_all or (self.reject_sessions and "session_id" in fields):
            raise hubc.HubRefusal(400, {"code": "memory_query_invalid"})
        return {"matches": [{"card_id": "result"}]}


@pytest.fixture(autouse=True)
def session_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search, "_HUBS_WITHOUT_SEARCH_SESSIONS", set())
    monkeypatch.setattr(
        "brigade.memory_retrieval_eval.live.interactive_session",
        lambda: {"session_id": "session-test", "session_kind": "interactive"},
    )


def run_search(hub: FakeHub) -> dict[str, Any]:
    return search._hub_search(
        hub, query="query", limit=10, query_source="explicit", scopes=("team",), deadline=float("inf")
    )


def test_old_hub_retries_without_session_fields_and_remembers() -> None:
    hub = FakeHub("old-session-hub", reject_sessions=True)

    assert run_search(hub)["matches"]
    assert len(hub.calls) == 2
    assert hub.calls[0]["session_id"] == "session-test"
    assert "session_id" not in hub.calls[1]

    assert run_search(hub)["matches"]
    assert len(hub.calls) == 3
    assert "session_id" not in hub.calls[2]


def test_new_hub_accepts_session_fields_without_retry() -> None:
    hub = FakeHub("new-session-hub")

    assert run_search(hub)["matches"]
    assert len(hub.calls) == 1
    assert hub.calls[0]["session_kind"] == "interactive"


def test_invalid_request_without_session_fields_is_not_retried() -> None:
    search._HUBS_WITHOUT_SEARCH_SESSIONS.add("invalid-without-session-hub")
    hub = FakeHub("invalid-without-session-hub", reject_all=True)

    with pytest.raises(hubc.HubRefusal, match="memory_query_invalid"):
        run_search(hub)

    assert len(hub.calls) == 1
    assert "session_id" not in hub.calls[0]


def test_invalid_session_bearing_request_does_not_disable_sessions() -> None:
    hub = FakeHub("modern-invalid-query-hub", reject_all=True)

    with pytest.raises(hubc.HubRefusal, match="memory_query_invalid"):
        run_search(hub)

    assert len(hub.calls) == 2
    assert hub.calls[0]["session_id"] == "session-test"
    assert "session_id" not in hub.calls[1]
    assert hub.identity not in search._HUBS_WITHOUT_SEARCH_SESSIONS

    hub.reject_all = False
    assert run_search(hub)["matches"]
    assert hub.calls[2]["session_id"] == "session-test"


def test_exhausted_retry_budget_preserves_original_refusal(monkeypatch: pytest.MonkeyPatch) -> None:
    hub = FakeHub("exhausted-session-hub", reject_sessions=True)
    refusal = hubc.HubRefusal(400, {"code": "memory_query_invalid"})

    def refuse(action: str, *, timeout: float, **fields: Any) -> dict[str, Any]:
        hub.calls.append(fields)
        monkeypatch.setattr(search, "_monotonic", lambda: 1.0)
        raise refusal

    monkeypatch.setattr(hub, "call", refuse)
    monkeypatch.setattr(search, "_monotonic", lambda: 0.0)

    with pytest.raises(hubc.HubRefusal) as caught:
        search._hub_search(hub, query="query", limit=10, query_source="explicit", scopes=("team",), deadline=1.0)

    assert caught.value is refusal
    assert len(hub.calls) == 1
    assert hub.identity not in search._HUBS_WITHOUT_SEARCH_SESSIONS
