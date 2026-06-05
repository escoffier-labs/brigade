# src/brigade/research/sources/cli.py
from __future__ import annotations

import hashlib
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, List


class CliSourceProvider:
    """Configured foreground CLI research source.

    Commands are local operator configuration, not built-in vendor contracts.
    Each argv item may include `{query}`. Output is captured and passed through
    the normal research extractor as untrusted tool output.
    """

    def __init__(self, *, source_id: str, command: list[str], cwd: Path, timeout: int = 120, trust: str = "cli") -> None:
        self.source_id = source_id
        self.command = command
        self.cwd = cwd
        self.timeout = timeout
        self.trust = trust if trust in {"cli", "browser", "web"} else "cli"
        self._cache: dict[str, dict[str, str]] = {}

    def _argv(self, query: str) -> list[str]:
        return [part.replace("{query}", query) for part in self.command]

    def search(self, query: str, limit: int) -> List[Dict[str, str]]:
        if limit < 1:
            return []
        argv = self._argv(query)
        try:
            proc = subprocess.run(
                argv,
                cwd=self.cwd,
                text=True,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            content = f"{self.source_id} failed for query {query!r}: {exc}"
            success = "false"
        else:
            content = "\n".join(part for part in (proc.stdout.strip(), proc.stderr.strip()) if part)
            success = "true" if proc.returncode == 0 else "false"
            if not content:
                content = f"{self.source_id} returned no output for query {query!r}."
        digest = hashlib.sha256(f"{self.source_id}\0{query}\0{content}".encode()).hexdigest()[:16]
        url = f"cli://{self.source_id}/{digest}"
        self._cache[url] = {"content": content, "success": success, "query": query}
        return [{"url": url, "title": f"{self.source_id}: {query}", "trust": self.trust}]

    def fetch(self, url: str) -> Dict[str, Any]:
        item = self._cache.get(url)
        if item is None:
            return {"success": False, "content": "", "title": "", "error": "cli source output not found"}
        return {"success": item["success"] == "true", "content": item["content"], "title": item["query"]}


def _command_parts(value: Any) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return [item for item in value if item]
    if isinstance(value, str) and value.strip():
        return shlex.split(value)
    return []


def build_providers(adapters: list[dict[str, Any]], *, target: Path) -> list[CliSourceProvider]:
    providers: list[CliSourceProvider] = []
    for item in adapters:
        if item.get("enabled", True) is False:
            continue
        if str(item.get("type") or "").strip().lower() != "cli":
            continue
        source_id = str(item.get("id") or item.get("name") or "cli-source").strip()
        command = _command_parts(item.get("command") or item.get("argv"))
        if not source_id or not command:
            continue
        cwd_value = item.get("cwd")
        cwd = Path(cwd_value).expanduser() if isinstance(cwd_value, str) and cwd_value.strip() else target
        if not cwd.is_absolute():
            cwd = target / cwd
        timeout = item.get("timeout")
        timeout_int = int(timeout) if isinstance(timeout, int) and timeout > 0 else 120
        providers.append(
            CliSourceProvider(
                source_id=source_id,
                command=command,
                cwd=cwd,
                timeout=timeout_int,
                trust=str(item.get("trust") or "cli"),
            )
        )
    return providers
