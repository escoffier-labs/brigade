# src/brigade/research/sources/web.py
from __future__ import annotations
import json
import subprocess
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote_plus


class PlaywrightUnavailable(RuntimeError):
    pass


class ProviderDeadlineExhausted(TimeoutError):
    pass


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright  # type: ignore

        return sync_playwright
    except Exception:
        return None


def _with_page(fn: Callable[[Any], Any], *, timeout_ms: int = 20000) -> Any:
    sp = _import_playwright()
    if sp is None:
        raise PlaywrightUnavailable(
            "Playwright not installed. Run: pip install 'brigade[research]' && playwright install chromium"
        )
    timeout_ms = max(1, int(timeout_ms))
    with sp() as p:
        browser = p.chromium.launch(headless=True, timeout=timeout_ms)
        try:
            page = browser.new_page()
            page.set_default_timeout(timeout_ms)
            page.set_default_navigation_timeout(timeout_ms)
            return fn(page)
        finally:
            try:
                browser.close()
            except Exception:
                # Close must not hang discovery past the engine deadline.
                pass


class PlaywrightProvider:
    """Zero-API web tier: drives a headless browser to search and read pages."""

    SEARCH_URL = "https://duckduckgo.com/html/?q={q}"
    trust = "browser"

    def __init__(self, timeout_ms: int = 20000) -> None:
        self.timeout_ms = max(1, int(timeout_ms))
        self._deadline_mono: float | None = None

    def bind_timeout(self, seconds: float) -> None:
        ms = max(1, int(float(seconds) * 1000))
        self.timeout_ms = min(self.timeout_ms, ms)
        self._deadline_mono = time.monotonic() + (self.timeout_ms / 1000.0)

    def _remaining_ms(self) -> int:
        if self._deadline_mono is None:
            return self.timeout_ms
        remaining = self._deadline_mono - time.monotonic()
        if remaining <= 0:
            raise ProviderDeadlineExhausted("playwright provider deadline exhausted")
        return max(1, int(remaining * 1000))

    def search(self, query: str, limit: int) -> List[Dict[str, str]]:
        try:
            timeout_ms = self._remaining_ms()
        except ProviderDeadlineExhausted:
            return []
        url = self.SEARCH_URL.format(q=quote_plus(query))

        def _run(page):
            page.goto(url, timeout=timeout_ms)
            anchors = page.query_selector_all("a.result__a")
            out = []
            for a in anchors[:limit]:
                href = a.get_attribute("href")
                if href:
                    out.append({"url": href, "title": a.inner_text()})
            return out

        return _with_page(_run, timeout_ms=timeout_ms)

    def fetch(self, url: str) -> Dict[str, Any]:
        try:
            timeout_ms = self._remaining_ms()
        except ProviderDeadlineExhausted as exc:
            return {"success": False, "content": "", "title": "", "error": str(exc)}

        def _run(page):
            page.goto(url, timeout=timeout_ms)
            return {"success": True, "content": page.inner_text("body"), "title": ""}

        try:
            return _with_page(_run, timeout_ms=timeout_ms)
        except Exception as e:
            return {"success": False, "content": "", "title": "", "error": str(e)}


class SearxngProvider:
    trust = "web"

    def __init__(self, base_url: str, timeout_ms: int = 15000) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_ms = max(1, int(timeout_ms))
        self._deadline_mono: float | None = None

    def bind_timeout(self, seconds: float) -> None:
        ms = max(1, int(float(seconds) * 1000))
        self.timeout_ms = min(self.timeout_ms, ms)
        self._deadline_mono = time.monotonic() + (self.timeout_ms / 1000.0)

    def _remaining_ms(self) -> int:
        if self._deadline_mono is None:
            return self.timeout_ms
        remaining = self._deadline_mono - time.monotonic()
        if remaining <= 0:
            raise ProviderDeadlineExhausted("searxng provider deadline exhausted")
        return max(1, int(remaining * 1000))

    def search(self, query: str, limit: int) -> List[Dict[str, str]]:
        import socket
        import urllib.error
        from urllib import request

        try:
            timeout_ms = self._remaining_ms()
        except ProviderDeadlineExhausted:
            return []
        u = f"{self.base_url}/search?q={quote_plus(query)}&format=json"
        # Pass the exact positive remaining budget (may be sub-second); do not floor to 1s.
        timeout_s = timeout_ms / 1000.0
        try:
            with request.urlopen(u, timeout=timeout_s) as r:
                data = json.loads(r.read().decode())
        except (
            OSError,
            socket.timeout,
            TimeoutError,
            urllib.error.URLError,
            UnicodeDecodeError,
            json.JSONDecodeError,
            ValueError,
        ):
            return []
        if not isinstance(data, dict):
            return []
        results = data.get("results", [])
        if not isinstance(results, list):
            return []
        return [
            {"url": str(item.get("url", "")), "title": str(item.get("title", ""))}
            for item in results[:limit]
            if isinstance(item, dict)
        ]

    def fetch(self, url: str) -> Dict[str, Any]:
        try:
            timeout_ms = self._remaining_ms()
        except ProviderDeadlineExhausted as exc:
            return {"success": False, "content": "", "title": "", "error": str(exc)}
        return PlaywrightProvider(timeout_ms=timeout_ms).fetch(url)


class PageforgeProvider:
    trust = "web"

    def __init__(self, command: list[str], db_path: Optional[str] = None, timeout: int = 120) -> None:
        if not command or not isinstance(command, list) or not all(isinstance(part, str) for part in command):
            raise ValueError("missing search.pageforge_command; configure a non-empty list of argv strings")
        self.command = list(command)
        self.db_path = db_path
        self.timeout = timeout
        self._deadline_mono: float | None = None

    def bind_timeout(self, seconds: float) -> None:
        self.timeout = min(int(self.timeout), max(1, int(float(seconds))))
        self._deadline_mono = time.monotonic() + float(self.timeout)

    def _remaining_seconds(self) -> float:
        if self._deadline_mono is None:
            return float(self.timeout)
        remaining = self._deadline_mono - time.monotonic()
        if remaining <= 0:
            raise ProviderDeadlineExhausted("pageforge provider deadline exhausted")
        return remaining

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        remaining = self._remaining_seconds()
        argv = self.command + args
        if self.db_path:
            argv += ["--db", self.db_path]
        return subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=remaining,
            check=False,
            shell=False,
        )

    @staticmethod
    def _error(proc: subprocess.CompletedProcess[str], fallback: str) -> str:
        message = "\n".join(part for part in (proc.stderr.strip(), proc.stdout.strip()) if part)
        return message or fallback

    def search(self, query: str, limit: int) -> List[Dict[str, str]]:
        if limit < 1:
            return []
        try:
            proc = self._run(["search_web", query, "--limit", str(limit), "--format", "json", "--compact"])
            if proc.returncode != 0:
                return []
            data = json.loads(proc.stdout)
            results = data.get("results", []) if isinstance(data, dict) and data.get("ok", True) is not False else []
            if not isinstance(results, list):
                return []
            return [
                {"url": str(item["url"]), "title": str(item.get("title") or "")}
                for item in results[:limit]
                if isinstance(item, dict) and item.get("url")
            ]
        except (
            OSError,
            subprocess.TimeoutExpired,
            ProviderDeadlineExhausted,
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ):
            return []

    def fetch(self, url: str) -> Dict[str, Any]:
        try:
            proc = self._run(["ingest_url", url, "--compact"])
            if proc.returncode != 0:
                return {
                    "success": False,
                    "content": "",
                    "title": "",
                    "error": self._error(proc, "pageforge ingest failed"),
                }
            data = json.loads(proc.stdout)
            if not isinstance(data, dict) or data.get("ok", True) is False:
                return {"success": False, "content": "", "title": "", "error": "pageforge ingest failed"}
            page = data.get("page", {})
            if not isinstance(page, dict):
                page = {}
            page_id = data.get("pageId") or page.get("pageId")
            if page_id is None:
                return {
                    "success": False,
                    "content": "",
                    "title": "",
                    "error": "pageforge ingest response missing pageId",
                }
            title = str(page.get("title") or "")

            proc = self._run(["get", str(page_id), "--format", "markdown"])
            if proc.returncode != 0:
                return {
                    "success": False,
                    "content": "",
                    "title": "",
                    "error": self._error(proc, "pageforge get failed"),
                }
            result: Dict[str, Any] = {"success": True, "content": proc.stdout, "title": title}
        except (
            OSError,
            subprocess.TimeoutExpired,
            ProviderDeadlineExhausted,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ) as exc:
            return {"success": False, "content": "", "title": "", "error": str(exc)}

        if len(str(result["content"])) >= 200:
            return result
        try:
            remaining = self._remaining_seconds()
        except ProviderDeadlineExhausted:
            return result
        try:
            fallback = PlaywrightProvider(timeout_ms=max(1, int(remaining * 1000))).fetch(url)
        except Exception:
            return result
        if fallback.get("success") and len(str(fallback.get("content") or "")) >= 200:
            return fallback
        return result


def build_provider(name: Optional[str], settings: Dict[str, Any]):
    name = (name or settings.get("research_search_provider") or "playwright").strip()
    if name in ("playwright", "browser", ""):
        return PlaywrightProvider()
    if name == "searxng":
        return SearxngProvider(settings["searxng_url"])
    if name == "pageforge":
        if "pageforge_command" not in settings:
            raise ValueError("missing search.pageforge_command; configure the PageForge argv prefix")
        timeout = settings.get("pageforge_timeout", 120)
        timeout_int = int(timeout) if isinstance(timeout, int) and timeout > 0 else 120
        return PageforgeProvider(
            settings["pageforge_command"],
            db_path=settings.get("pageforge_db_path"),
            timeout=timeout_int,
        )
    raise ValueError(f"unknown search provider: {name}")
