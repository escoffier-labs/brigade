"""Resolve the memory stores an MCP session reads and writes.

Two stores exist. The repo store is an explicit --target, or the nearest
directory above the server's cwd holding .brigade/config.json or
.brigade/memory-care.toml (never the home directory). The user
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
REPO_MARKERS = (".brigade/config.json", ".brigade/memory-care.toml")


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


def _nearest_marker(cwd: Path) -> Path | None:
    current = cwd.expanduser().resolve()
    for candidate in (current, *current.parents):
        if is_operator_home(candidate):
            return None
        if any((candidate / marker).is_file() for marker in REPO_MARKERS):
            return candidate
    return None


def _repo_root(target: Path | None, cwd: Path) -> tuple[Path | None, bool]:
    if target is not None:
        root = target.expanduser().resolve()
        # Wired only when the config loads, the same rule automatic discovery uses.
        wired = resolve_wired_target(str(root), harness=None)
        return root, wired is not None and wired.resolve() == root
    wired = resolve_wired_target(str(cwd), harness=None)
    wired_root = wired.resolve() if wired is not None and not is_operator_home(wired) else None
    marker = _nearest_marker(cwd)
    if marker is None:
        return wired_root, wired_root is not None
    return marker, marker == wired_root


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
