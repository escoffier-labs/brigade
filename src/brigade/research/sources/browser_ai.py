# src/brigade/research/sources/browser_ai.py
from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

DISCOVERY_PROMPT = """Use browser research to find sources for this query.
Query: {query}
Return ONLY a JSON array with at most {limit} objects.
Each object must contain https url, title, and snippet strings."""


class BrowserAiDiscoveryError(RuntimeError):
    pass


class BrowserAiProvider:
    trust = "browser-ai"
    source_type = "browser-ai"
    source_id = "gemini-browser"

    def __init__(self, *, backend: Any, lane: str) -> None:
        self.backend = backend
        self.lane = lane
        self.requested_model = getattr(backend, "requested_model", None)
        self.observed_model = getattr(backend, "observed_model", None) or "unverified"
        self._pages: dict[str, dict[str, Any]] = {}

    def search(self, query: str, limit: int) -> list[dict[str, str]]:
        raw = self.backend.complete(
            [{"role": "user", "content": DISCOVERY_PROMPT.format(query=query, limit=limit)}],
            max_tokens=4096,
            timeout=180,
        )
        self.observed_model = getattr(self.backend, "observed_model", None) or "unverified"
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BrowserAiDiscoveryError("browser AI discovery returned invalid JSON") from exc
        if not isinstance(parsed, list) or len(parsed) > limit:
            raise BrowserAiDiscoveryError("browser AI discovery returned an invalid result count")
        hits: list[dict[str, str]] = []
        for item in parsed:
            if not isinstance(item, dict):
                raise BrowserAiDiscoveryError("browser AI discovery result must be an object")
            url = item.get("url")
            title = item.get("title")
            snippet = item.get("snippet")
            if not all(isinstance(value, str) and value.strip() for value in (url, title, snippet)):
                raise BrowserAiDiscoveryError("browser AI discovery result is missing url, title, or snippet")
            parsed = urlparse(url)
            if parsed.scheme != "https" or not parsed.hostname:
                raise BrowserAiDiscoveryError("browser AI discovery URL must use https")
            if len(snippet) > 12_000:
                raise BrowserAiDiscoveryError("browser AI discovery snippet exceeds 12000 characters")
            self._pages[url] = {"success": True, "content": snippet, "title": title}
            hits.append({"url": url, "title": title, "trust": self.trust})
        return hits

    def fetch(self, url: str) -> dict[str, Any]:
        return dict(self._pages.get(url, {"success": False, "content": "", "title": ""}))
