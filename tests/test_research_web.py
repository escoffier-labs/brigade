# tests/test_research_web.py
import json
import subprocess

import pytest
from brigade.research.sources import web


def test_playwright_missing_gives_actionable_error(monkeypatch):
    monkeypatch.setattr(web, "_import_playwright", lambda: None)
    prov = web.PlaywrightProvider()
    with pytest.raises(web.PlaywrightUnavailable) as e:
        prov.search("hello", 3)
    assert "pip install" in str(e.value)


def test_playwright_search_parses_results(monkeypatch):
    class FakePage:
        def goto(self, url, **kw):
            self.url = url

        def query_selector_all(self, sel):
            class A:
                def __init__(s, href, txt):
                    s._h, s._t = href, txt

                def get_attribute(s, n):
                    return s._h

                def inner_text(s):
                    return s._t

            return [A("https://ex.com/1", "Result One"), A("https://ex.com/2", "Result Two")]

        def inner_text(self, sel):
            return "page body text"

    monkeypatch.setattr(web, "_with_page", lambda fn: fn(FakePage()))
    prov = web.PlaywrightProvider()
    hits = prov.search("q", 2)
    assert hits[0]["url"] == "https://ex.com/1" and hits[0]["title"] == "Result One"


def test_provider_factory_default_is_playwright():
    prov = web.build_provider(None, {})
    assert isinstance(prov, web.PlaywrightProvider)
    assert prov.trust == "browser"


def test_pageforge_requires_non_empty_string_command():
    with pytest.raises(ValueError) as exc:
        web.PageforgeProvider([])
    assert "pageforge_command" in str(exc.value)

    with pytest.raises(ValueError):
        web.PageforgeProvider(["node", 123])


def test_pageforge_search_returns_titles_and_urls(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        payload = {
            "ok": True,
            "results": [
                {"url": "https://example.com/a", "title": "A"},
                {"url": "https://example.com/b", "title": "B"},
                {"url": "https://example.com/c", "title": "C"},
            ],
        }
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(web.subprocess, "run", fake_run)
    prov = web.PageforgeProvider(["pageforge"], db_path="/tmp/pageforge.db")

    assert prov.search("test query", 2) == [
        {"url": "https://example.com/a", "title": "A"},
        {"url": "https://example.com/b", "title": "B"},
    ]
    assert calls[0][0] == [
        "pageforge",
        "search_web",
        "test query",
        "--limit",
        "2",
        "--format",
        "json",
        "--compact",
        "--db",
        "/tmp/pageforge.db",
    ]
    assert calls[0][1]["shell"] is False


def test_pageforge_search_errors_return_empty(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="search failed")

    monkeypatch.setattr(web.subprocess, "run", fake_run)
    prov = web.PageforgeProvider(["pageforge"])

    assert prov.search("test query", 2) == []


def test_pageforge_fetch_ingests_then_reads_markdown(monkeypatch):
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[1] == "ingest_url":
            payload = {"ok": True, "pageId": "page-1", "page": {"title": "Example title"}}
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="Long markdown content " * 20, stderr="")

    monkeypatch.setattr(web.subprocess, "run", fake_run)
    prov = web.PageforgeProvider(["pageforge"])

    result = prov.fetch("https://example.com")

    assert result["success"] is True
    assert result["title"] == "Example title"
    assert result["content"].startswith("Long markdown content")
    assert calls == [
        ["pageforge", "ingest_url", "https://example.com", "--compact"],
        ["pageforge", "get", "page-1", "--format", "markdown"],
    ]


def test_pageforge_fetch_failure_returns_error(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="ingest failed")

    monkeypatch.setattr(web.subprocess, "run", fake_run)
    prov = web.PageforgeProvider(["pageforge"])

    result = prov.fetch("https://example.com")

    assert result["success"] is False
    assert result["content"] == ""
    assert "ingest failed" in result["error"]


def test_pageforge_fetch_uses_playwright_for_thin_content(monkeypatch):
    def fake_run(argv, **kwargs):
        if argv[1] == "ingest_url":
            payload = {"ok": True, "pageId": 7, "page": {"title": "Short"}}
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="short", stderr="")

    class FakePlaywright:
        def fetch(self, url):
            return {"success": True, "content": "Fallback content " * 20, "title": "Fallback"}

    monkeypatch.setattr(web.subprocess, "run", fake_run)
    monkeypatch.setattr(web, "PlaywrightProvider", FakePlaywright)
    prov = web.PageforgeProvider(["pageforge"])

    result = prov.fetch("https://example.com")

    assert result["success"] is True
    assert result["title"] == "Fallback"
    assert result["content"].startswith("Fallback content")


def test_pageforge_fetch_keeps_thin_pageforge_success_when_fallback_fails(monkeypatch):
    def fake_run(argv, **kwargs):
        if argv[1] == "ingest_url":
            payload = {"ok": True, "pageId": 7, "page": {"title": "Short"}}
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="short", stderr="")

    class FakePlaywright:
        def fetch(self, url):
            raise web.PlaywrightUnavailable("missing")

    monkeypatch.setattr(web.subprocess, "run", fake_run)
    monkeypatch.setattr(web, "PlaywrightProvider", FakePlaywright)
    prov = web.PageforgeProvider(["pageforge"])

    result = prov.fetch("https://example.com")

    assert result == {"success": True, "content": "short", "title": "Short"}


def test_provider_factory_builds_pageforge():
    prov = web.build_provider(
        "pageforge",
        {"pageforge_command": ["node", "/opt/pageforge/bin/pageforge.js"], "pageforge_db_path": "/tmp/pf.db"},
    )

    assert isinstance(prov, web.PageforgeProvider)
    assert prov.command == ["node", "/opt/pageforge/bin/pageforge.js"]
    assert prov.db_path == "/tmp/pf.db"
