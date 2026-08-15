from __future__ import annotations

import json

import pytest

from brigade.research.sources.browser_ai import BrowserAiDiscoveryError, BrowserAiProvider


class FakeBackend:
    requested_model = "gemini-3.1-pro"
    observed_model = "unverified"

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls = 0

    def complete(self, _messages, **_kwargs) -> str:
        self.calls += 1
        return self.response


def _provider(response: str) -> tuple[BrowserAiProvider, FakeBackend]:
    backend = FakeBackend(response)
    return BrowserAiProvider(backend=backend, lane="gemini_browser"), backend


def test_browser_ai_provider_returns_untrusted_sources() -> None:
    provider, _backend = _provider('[{"url":"https://example.test/a","title":"A","snippet":"Claim A"}]')

    hits = provider.search("query", limit=3)
    page = provider.fetch(hits[0]["url"])

    assert hits[0]["trust"] == "browser-ai"
    assert page == {"success": True, "content": "Claim A", "title": "A"}
    assert provider.requested_model == "gemini-3.1-pro"
    assert provider.observed_model == "unverified"


def test_fetch_does_not_call_backend_again() -> None:
    provider, backend = _provider('[{"url":"https://example.test/a","title":"A","snippet":"Claim A"}]')

    hits = provider.search("query", limit=1)
    assert backend.calls == 1
    page = provider.fetch(hits[0]["url"])
    assert backend.calls == 1
    assert page["success"] is True


@pytest.mark.parametrize(
    ("response", "match"),
    [
        ("not-json", "invalid JSON"),
        ('{"url":"https://example.test/a"}', "invalid result count"),
        (
            json.dumps([{"url": f"https://example.test/{i}", "title": "T", "snippet": "S"} for i in range(3)]),
            "invalid result count",
        ),
        ('["not-an-object"]', "must be an object"),
        (
            '[{"url":"https://example.test/a","title":"A","snippet":""}]',
            "missing url, title, or snippet",
        ),
        (
            '[{"url":null,"title":"A","snippet":"S"}]',
            "missing url, title, or snippet",
        ),
        (
            '[{"url":"https://example.test/a","title":123,"snippet":"S"}]',
            "missing url, title, or snippet",
        ),
        (
            '[{"url":"https://example.test/a","title":"  ","snippet":"S"}]',
            "missing url, title, or snippet",
        ),
        (
            '[{"url":"http://example.test/a","title":"A","snippet":"S"}]',
            "must use https",
        ),
        (
            '[{"url":"https:///no-host","title":"A","snippet":"S"}]',
            "must use https",
        ),
        (
            json.dumps([{"url": "https://example.test/a", "title": "A", "snippet": "x" * 12001}]),
            "exceeds 12000",
        ),
    ],
)
def test_browser_ai_provider_rejects_invalid_payloads(response: str, match: str) -> None:
    provider, _backend = _provider(response)
    with pytest.raises(BrowserAiDiscoveryError, match=match):
        provider.search("query", limit=2)


def test_browser_ai_bind_timeout_caps_backend_request_with_deadline_ceiling() -> None:
    class RecordingBackend(FakeBackend):
        def __init__(self) -> None:
            super().__init__('[{"url":"https://example.test/a","title":"A","snippet":"Claim A"}]')
            self.kwargs: dict[str, object] = {}

        def complete(self, _messages, **kwargs) -> str:
            self.kwargs = dict(kwargs)
            return super().complete(_messages, **kwargs)

    backend = RecordingBackend()
    provider = BrowserAiProvider(backend=backend, lane="gemini_browser", timeout=180)
    provider.bind_timeout(7.4)
    provider.search("query", limit=1)

    assert provider.timeout == 7
    assert backend.kwargs["timeout"] == 7
    assert backend.kwargs["deadline_ceiling"] is True
