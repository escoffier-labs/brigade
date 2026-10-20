"""Resolve the memory stores an MCP session reads and writes.

Two stores exist. The repo store is an explicit --target, or the nearest
Brigade-wired repo above the server's cwd (never the home directory). The user
store lives under BRIGADE_HOME so memory works outside any repo. Repo results
take precedence; an invalid repo memory-care config drops the repo store and is
reported instead of failing the server.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import memory_cmd
from .claude_hooks.paths import is_operator_home
from .fleet_client import brigade_home
from .wiring import resolve_wired_target

REPO = "repo"
USER = "user"
USER_HANDOFF_INBOX = "memory/handoffs"
HTTP_TOKEN_NAME = "mcp-http.token"


@dataclass(frozen=True)
class MemoryStore:
    origin: str
    root: Path
    config: memory_cmd.MemoryCareConfig


@dataclass(frozen=True)
class Resolution:
    stores: tuple[MemoryStore, ...]
    repo_root: Path | None
    repo_wired: bool
    repo_config_error: str | None


def user_root() -> Path:
    return brigade_home().expanduser().resolve()


def http_token_path() -> Path:
    return user_root() / HTTP_TOKEN_NAME


def _repo_root(target: Path | None, cwd: Path) -> tuple[Path | None, bool]:
    if target is not None:
        root = target.expanduser().resolve()
        return root, (root / ".brigade" / "config.json").is_file()
    found = resolve_wired_target(str(cwd), harness=None)
    if found is None or is_operator_home(found):
        return None, False
    return found.resolve(), True


def resolve(*, target: Path | None = None, cwd: Path | None = None) -> Resolution:
    repo_root, wired = _repo_root(target, cwd if cwd is not None else Path.cwd())
    user = user_root()
    stores: list[MemoryStore] = []
    error: str | None = None
    if repo_root is not None and repo_root != user:
        try:
            config = memory_cmd.load_config(repo_root) or memory_cmd.MemoryCareConfig()
        except ValueError as exc:
            error = str(exc)
        else:
            stores.append(MemoryStore(REPO, repo_root, config))
    stores.append(MemoryStore(USER, user, memory_cmd.MemoryCareConfig()))
    return Resolution(tuple(stores), repo_root, wired, error)
