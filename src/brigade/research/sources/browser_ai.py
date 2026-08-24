# src/brigade/research/sources/browser_ai.py
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

DISCOVERY_PROMPT = """Use browser research to find sources for this query.
Query: {query}
Return ONLY a JSON array with at most {limit} objects.
Each object must contain https url, title, and snippet strings."""


class BrowserAiDiscoveryError(RuntimeError):
    pass


_MAX_DECORATED_OUTPUT_CHARS = 256_000


def _parse_discovery_payload(raw: str) -> Any:
    """Read Oracle's final standalone JSON array from bounded decorated stdout."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError as original_error:
        decoder = json.JSONDecoder()
        payload: Any | None = None
        decorated = raw[-_MAX_DECORATED_OUTPUT_CHARS:]
        for match in re.finditer(r"(?m)^[ \t]*\[", decorated):
            try:
                candidate, _end = decoder.raw_decode(decorated, match.end() - 1)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, list):
                payload = candidate
        if payload is not None:
            return payload
        raise original_error


class BrowserAiProvider:
    trust = "browser-ai"
    source_type = "browser-ai"
    source_id = "gemini-browser"

    def __init__(self, *, backend: Any, lane: str, timeout: int = 180) -> None:
        self.backend = backend
        self.lane = lane
        self.timeout = max(1, int(timeout))
        self.requested_model = getattr(backend, "requested_model", None)
        self.observed_model = getattr(backend, "observed_model", None) or "unverified"
        self._pages: dict[str, dict[str, Any]] = {}

    def bind_timeout(self, seconds: float) -> None:
        self.timeout = min(int(self.timeout), max(1, int(float(seconds))))

    def search(self, query: str, limit: int) -> list[dict[str, str]]:
        raw = self.backend.complete(
            [{"role": "user", "content": DISCOVERY_PROMPT.format(query=query, limit=limit)}],
            max_tokens=4096,
            timeout=self.timeout,
            deadline_ceiling=True,
        )
        self.observed_model = getattr(self.backend, "observed_model", None) or "unverified"
        try:
            parsed = _parse_discovery_payload(raw)
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
            # Narrow each field individually so mypy sees str (all() does not narrow).
            if not isinstance(url, str) or not url.strip():
                raise BrowserAiDiscoveryError("browser AI discovery result is missing url, title, or snippet")
            if not isinstance(title, str) or not title.strip():
                raise BrowserAiDiscoveryError("browser AI discovery result is missing url, title, or snippet")
            if not isinstance(snippet, str) or not snippet.strip():
                raise BrowserAiDiscoveryError("browser AI discovery result is missing url, title, or snippet")
            parsed_url = urlparse(url)
            if parsed_url.scheme != "https" or not parsed_url.hostname:
                raise BrowserAiDiscoveryError("browser AI discovery URL must use https")
            if len(snippet) > 12_000:
                raise BrowserAiDiscoveryError("browser AI discovery snippet exceeds 12000 characters")
            self._pages[url] = {"success": True, "content": snippet, "title": title}
            hits.append({"url": url, "title": title, "trust": self.trust})
        return hits

    def fetch(self, url: str) -> dict[str, Any]:
        return dict(self._pages.get(url, {"success": False, "content": "", "title": ""}))
