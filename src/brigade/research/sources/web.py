# src/brigade/research/sources/web.py
from __future__ import annotations
from typing import Any, Callable, Dict, List, Optional
from urllib import request
from urllib.parse import quote_plus


class PlaywrightUnavailable(RuntimeError):
    pass


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright  # type: ignore

        return sync_playwright
    except Exception:
        return None


def _with_page(fn: Callable[[Any], Any]) -> Any:
    sp = _import_playwright()
    if sp is None:
        raise PlaywrightUnavailable(
            "Playwright not installed. Run: pip install 'brigade[research]' && playwright install chromium"
        )
    with sp() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            return fn(page)
        finally:
            browser.close()


class PlaywrightProvider:
    """Zero-API web tier: drives a headless browser to search and read pages."""

    SEARCH_URL = "https://duckduckgo.com/html/?q={q}"
    trust = "browser"

    def search(self, query: str, limit: int) -> List[Dict[str, str]]:
        url = self.SEARCH_URL.format(q=quote_plus(query))

        def _run(page):
            page.goto(url, timeout=20000)
            anchors = page.query_selector_all("a.result__a")
            out = []
            for a in anchors[:limit]:
                href = a.get_attribute("href")
                if href:
                    out.append({"url": href, "title": a.inner_text()})
            return out

        return _with_page(_run)

    def fetch(self, url: str) -> Dict[str, Any]:
        def _run(page):
            page.goto(url, timeout=20000)
            return {"success": True, "content": page.inner_text("body"), "title": ""}

        try:
            return _with_page(_run)
        except Exception as e:
            return {"success": False, "content": "", "title": "", "error": str(e)}


class SearxngProvider:
    trust = "web"

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")

    def search(self, query: str, limit: int) -> List[Dict[str, str]]:
        import json
        from urllib import request

        u = f"{self.base_url}/search?q={quote_plus(query)}&format=json"
        with request.urlopen(u, timeout=15) as r:
            data = json.loads(r.read().decode())
        return [{"url": x.get("url", ""), "title": x.get("title", "")} for x in data.get("results", [])[:limit]]

    def fetch(self, url: str) -> Dict[str, Any]:
        return PlaywrightProvider().fetch(url)


class YouComProvider:
    trust = "web"

    def __init__(self, api_key: Optional[str] = None, base_url: str = "https://api.you.com") -> None:
        self.api_key = api_key or ""
        self.base_url = base_url.rstrip("/")

    def search(self, query: str, limit: int) -> List[Dict[str, str]]:
        import json

        params = {
            "query": query,
            "count": str(max(1, min(limit, 100))),
            "livecrawl": "web",
        }
        u = f"{self.base_url}/v1/agents/search?" + "&".join(
            f"{k}={quote_plus(v)}" for k, v in params.items()
        )
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        req = request.Request(u, headers=headers)
        with request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        results = data.get("results", {}) if isinstance(data, dict) else {}
        hits = []
        for bucket in ("web", "news"):
            for item in results.get(bucket, []) if isinstance(results, dict) else []:
                if not isinstance(item, dict):
                    continue
                hits.append(
                    {
                        "url": str(item.get("url", "")),
                        "title": str(item.get("title", "")),
                    }
                )
                if len(hits) >= limit:
                    return hits
        return hits

    def fetch(self, url: str) -> Dict[str, Any]:
        return PlaywrightProvider().fetch(url)


def build_provider(name: Optional[str], settings: Dict[str, Any]):
    name = (name or settings.get("research_search_provider") or "playwright").strip()
    if name in ("playwright", "browser", ""):
        return PlaywrightProvider()
    if name == "searxng":
        return SearxngProvider(settings["searxng_url"])
    if name in ("youcom", "you.com", "ydc"):
        return YouComProvider(api_key=settings.get("youcom_api_key") or settings.get("ydc_api_key"))
    raise ValueError(f"unknown search provider: {name}")
