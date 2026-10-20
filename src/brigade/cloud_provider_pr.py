"""Resolve provider-reported pull requests for the cloud tracker (#1794).

Jules names its working branches freely (no ``jules/`` prefix), so the tracker's
branch-prefix scan never sees the pull request a finished session opened. The
session record does carry ``pull_request_url``; this module looks that PR up
directly, the way the claude-cloud path binds by exact URL, so the tracker can
classify a merged PR as ``landed`` and record its head branch.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable

# Upper bound on ``gh pr view`` calls per observation. Each call is one process,
# so the walk stays bounded however many sessions the registry holds.
MAX_PROVIDER_PR_LOOKUPS = 25

_PR_URL = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/[1-9][0-9]{0,18}", re.IGNORECASE)

RunText = Callable[..., "tuple[int, str, str]"]


def valid_pull_request_url(value: object) -> str | None:
    """Return ``value`` when it is an exact canonical GitHub PR URL, else None."""
    if isinstance(value, str) and _PR_URL.fullmatch(value):
        return value
    return None


def matching_pull_request(provider_info: object, github: dict[str, Any]) -> dict[str, Any] | None:
    """The snapshot PR whose URL equals the one the provider reported, if any."""
    if not isinstance(provider_info, dict):
        return None
    url = valid_pull_request_url(provider_info.get("pull_request_url"))
    prs = github.get("prs") if isinstance(github, dict) else None
    if url is None or not isinstance(prs, list):
        return None
    for pr in prs:
        if isinstance(pr, dict) and isinstance(pr.get("url"), str) and pr["url"].lower() == url.lower():
            return pr
    return None


def resolve_pull_requests(
    target: Path,
    tasks: dict[str, Any],
    registered_task_ids: set[str],
    github: dict[str, Any],
    run_text: RunText,
) -> None:
    """Append provider-reported PRs missing from ``github["prs"]``, in place.

    Only sessions that are registered locally are looked up, each URL at most
    once, and any failed or malformed lookup is skipped, never raised.
    """
    prs = github.get("prs")
    if not isinstance(prs, list):
        return
    known = {str(pr["url"]).lower() for pr in prs if isinstance(pr, dict) and isinstance(pr.get("url"), str)}
    lookups = 0
    for task_id, info in tasks.items():
        if task_id not in registered_task_ids or not isinstance(info, dict):
            continue
        url = valid_pull_request_url(info.get("pull_request_url"))
        if url is None or url.lower() in known:
            continue
        if lookups >= MAX_PROVIDER_PR_LOOKUPS:
            break
        lookups += 1
        known.add(url.lower())
        code, stdout, _ = run_text(
            ["gh", "pr", "view", url, "--json", "number,state,headRefName,url,isDraft,headRefOid"], cwd=target
        )
        if code != 0:
            continue
        try:
            data = json.loads(stdout)
        except (ValueError, TypeError):
            continue
        head = data.get("headRefName") if isinstance(data, dict) else None
        if not isinstance(head, str) or not head:
            continue
        # The lookup must answer for the URL that was asked about.
        if not isinstance(data.get("url"), str) or data["url"].lower() != url.lower():
            continue
        prs.append(
            {
                "head": head,
                "state": data.get("state"),
                "number": data.get("number"),
                "url": data["url"],
                "isDraft": data.get("isDraft"),
                "headRefOid": data.get("headRefOid"),
            }
        )
