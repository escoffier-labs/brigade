# tests/test_research_web.py
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
