# src/brigade/research/sources/web.py
from __future__ import annotations
import json
import subprocess
from typing import Any, Callable, Dict, List, Optional
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


class PageforgeProvider:
    trust = "web"

    def __init__(self, command: list[str], db_path: Optional[str] = None, timeout: int = 120) -> None:
        if not command or not isinstance(command, list) or not all(isinstance(part, str) for part in command):
            raise ValueError("missing search.pageforge_command; configure a non-empty list of argv strings")
        self.command = list(command)
        self.db_path = db_path
        self.timeout = timeout

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        argv = self.command + args
        if self.db_path:
            argv += ["--db", self.db_path]
        return subprocess.run(
            argv,
            text=True,
            capture_output=True,
            timeout=self.timeout,
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
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, KeyError, TypeError, ValueError):
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
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, TypeError, ValueError) as exc:
            return {"success": False, "content": "", "title": "", "error": str(exc)}

        if len(str(result["content"])) >= 200:
            return result
        try:
            fallback = PlaywrightProvider().fetch(url)
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
