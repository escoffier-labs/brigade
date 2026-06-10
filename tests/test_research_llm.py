# tests/test_research_llm.py
from brigade.research import llm


class FakeAgent:
    def __init__(self, name, cli=None, endpoint=None, model=None, role="researcher", headers=None):
        self.name, self.cli, self.endpoint, self.model, self.role, self.headers = (
            name,
            cli,
            endpoint,
            model,
            role,
            headers,
        )


class FakeRoster:
    def __init__(self, agents):
        self._a = agents

    def find_role(self, role):
        return next((a for a in self._a if a.role == role), None)


def test_resolve_cli_backend(monkeypatch):
    r = FakeRoster([FakeAgent("chef", cli="codex", role="researcher")])
    monkeypatch.setattr(llm, "_run_cli", lambda cli, prompt, timeout: f"[{cli}] ok")
    backend = llm.resolve_backend(r)
    assert backend.complete([{"role": "user", "content": "hi"}]) == "[codex] ok"


def test_resolve_http_backend(monkeypatch):
    r = FakeRoster([FakeAgent("api", endpoint="http://x/v1", model="m", role="researcher")])
    captured = {}

    def fake_post(url, payload, headers, timeout):
        captured["url"] = url
        return {"choices": [{"message": {"content": "http-answer"}}]}

    monkeypatch.setattr(llm, "_http_post_json", fake_post)
    backend = llm.resolve_backend(r)
    assert backend.complete([{"role": "user", "content": "hi"}]) == "http-answer"
    assert captured["url"].endswith("/chat/completions")


def test_no_researcher_raises():
    import pytest

    with pytest.raises(llm.NoResearcherError):
        llm.resolve_backend(FakeRoster([]))
