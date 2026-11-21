"""Read-only lookup of the CI result for a trial (#2779). Display only.

``CiObserver.observed`` answers one question: did the ``outcome-attest/<manifest>``
check run on the merge commit of the receipt's pull request pass? The answer is for
people reading ``outcome rank`` and the work brief. It is never an input to
promotion, eligibility, a band or the ratchet decision.

State on the workspace host cannot authorize an unattended write. Everything this
module reads on the host (PATH, the ``gh`` config, receipts, the git remote) is
forgeable by a same-user agent, so a "seen passing" answer can only be shown. The
scheduled CI job that makes promotion decisions is a separate change.

The lookup goes through ``gh api`` and stays fail-closed and fail-quiet: any error
(no ``gh``, no network, bad JSON, a rate limit, a missing field) means "not
observed", and nothing here raises into a caller.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

ATTEST_CHECK_PREFIX = "outcome-attest/"
ATTEST_APP_SLUG = "github-actions"
ATTEST_BRANCH = "main"
DISABLE_ENV = "BRIGADE_CI_ATTESTATION"
GH_TIMEOUT_SECONDS = 20
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REMOTE_RE = re.compile(r"github\.com[:/]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")

GhApi = Callable[[str], Any]


class GhUnavailable(Exception):
    """The transport failed (no gh, no network, timeout, API error)."""


class GhNotFound(Exception):
    """The API answered 404: the commit is not on the remote."""


def default_gh_api(endpoint: str) -> Any:
    """Read one GitHub REST endpoint through ``gh api``. Raise ``GhUnavailable`` on any failure."""
    try:
        completed = subprocess.run(  # noqa: S603 - fixed argv, endpoint is not a shell string
            ["gh", "api", "--method", "GET", endpoint],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GhUnavailable(type(exc).__name__) from exc
    if completed.returncode != 0:
        if "HTTP 404" in completed.stderr:
            raise GhNotFound(endpoint)
        raise GhUnavailable(f"gh exit {completed.returncode}")
    try:
        return json.loads(completed.stdout)
    except ValueError as exc:
        raise GhUnavailable("invalid json") from exc


def repo_slug(target: Path) -> str | None:
    """Return ``owner/name`` for the ``origin`` GitHub remote, or None."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(target), "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    match = _REMOTE_RE.search(completed.stdout.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def receipt_claims(receipt: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return (manifest_id, head_sha) the receipt names, or None."""
    binding = receipt.get("subject_binding") or receipt.get("binding")
    manifest_binding = binding.get("manifest_binding") if isinstance(binding, dict) else None
    manifest_id = manifest_binding.get("manifest_id") if isinstance(manifest_binding, dict) else None
    git = receipt.get("git")
    head = git.get("head") if isinstance(git, dict) else None
    if not (isinstance(manifest_id, str) and manifest_id and isinstance(head, str) and _SHA_RE.match(head)):
        return None
    return manifest_id, head


def _items(payload: Any, key: str | None = None) -> list[dict[str, Any]]:
    raw = payload.get(key) if key and isinstance(payload, dict) else payload
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _merge_commits(gh: GhApi, slug: str, head: str) -> list[str]:
    """Merge commits of the merged pull requests into main that contain ``head``."""
    found: list[str] = []
    for pr in _items(gh(f"repos/{slug}/commits/{head}/pulls?per_page=30")):
        base = pr.get("base")
        merge_sha = pr.get("merge_commit_sha")
        if not pr.get("merged_at") or not isinstance(base, dict) or base.get("ref") != ATTEST_BRANCH:
            continue
        if isinstance(merge_sha, str) and _SHA_RE.match(merge_sha):
            found.append(merge_sha)
    return found


def _check_passed(gh: GhApi, slug: str, commit: str, manifest_id: str) -> bool:
    """True when every ``outcome-attest/<manifest>`` run from the Actions app on ``commit`` succeeded."""
    name = f"{ATTEST_CHECK_PREFIX}{manifest_id}"
    runs = _items(gh(f"repos/{slug}/commits/{commit}/check-runs?filter=latest&per_page=100"), "check_runs")
    matching = [
        run
        for run in runs
        if run.get("name") == name
        and run.get("head_sha") == commit
        and isinstance(run.get("app"), dict)
        and run["app"].get("slug") == ATTEST_APP_SLUG
    ]
    return bool(matching) and all(
        run.get("status") == "completed" and run.get("conclusion") == "success" for run in matching
    )


class CiObserver:
    """Look up the CI result for receipts in one workspace, memoised for the life of the instance."""

    def __init__(self, target: Path, *, gh_api: GhApi | None = None) -> None:
        self.target = target
        self._gh = gh_api or default_gh_api
        self._memo: dict[str, bool] = {}
        self._offline = False

    def observed(self, receipt: Mapping[str, Any]) -> bool:
        """Whether CI was seen passing for this receipt's trial. Display only."""
        try:
            return self._observed(receipt)
        except Exception:  # noqa: BLE001 - a display lookup must never break rank or reconcile
            return False

    def _observed(self, receipt: Mapping[str, Any]) -> bool:
        if os.environ.get(DISABLE_ENV, "").strip().lower() in {"0", "off", "false", "no"}:
            return False
        claims = receipt_claims(receipt)
        slug = repo_slug(self.target) if claims else None
        if claims is None or slug is None or self._offline:
            return False
        manifest_id, head = claims
        key = f"{slug}|{head}|{manifest_id}"
        if key in self._memo:
            return self._memo[key]
        try:
            result = any(
                _check_passed(self._gh, slug, commit, manifest_id) for commit in _merge_commits(self._gh, slug, head)
            )
        except GhNotFound:
            result = False
        except GhUnavailable:
            self._offline = True
            return False
        self._memo[key] = result
        return result
