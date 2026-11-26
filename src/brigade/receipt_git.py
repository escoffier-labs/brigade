"""Repository URLs and Git metadata used in receipt exports."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse


def _github_host(host: str | None) -> bool:
    return bool(host and (host == "github.com" or host.endswith(".github.com")))


def _strip_git_suffix(repo: str) -> str:
    return repo[:-4] if repo.endswith(".git") else repo


def _github_remote_parts(remote: str) -> tuple[str, str, str] | None:
    parsed = urlparse(remote)
    if parsed.scheme == "https" and _github_host(parsed.hostname):
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) == 2:
            return parsed.hostname or "", parts[0], _strip_git_suffix(parts[1])
        return None
    match = re.match(r"^git@([^:]+):([^/]+)/(.+)$", remote)
    if match and _github_host(match.group(1)):
        return match.group(1), match.group(2), _strip_git_suffix(match.group(3))
    return None


def _receipt_git(payload: dict[str, Any]) -> dict[str, Any] | None:
    git = payload.get("git")
    if not isinstance(git, dict):
        return None
    head = git.get("head")
    if not isinstance(head, str) or not head:
        return None
    copied = {key: git[key] for key in ("head", "branch", "dirty_files") if key in git}
    return copied if copied else None


def _metadata_with_git(metadata: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    git = _receipt_git(payload)
    if git is not None:
        metadata["git"] = git
    return metadata
