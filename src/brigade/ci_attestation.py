"""CI attestation for scoreable manifest trials (#2779).

A trial is *attested* only when a GitHub Actions run on ``main`` reran its verify
manifest on the merged commit and passed. Everything the scored agent can write
(receipts, hook state, work sessions, the cache below) may name which trial to
look up, and may veto, but never supplies the pass. The pass comes from the
GitHub API, read through ``gh api``.

All of these must hold, otherwise the trial stays ``local``:

* the receipt names a head commit, a same-repo branch and a manifest;
* that head is a commit of a merged pull request into ``main``;
* the pull request's merge commit is an ancestor of ``origin/main``;
* a completed check run named ``outcome-attest/<manifest>`` exists on that merge
  commit, was written by the ``github-actions`` app and concluded ``success``;
* the check run's suite belongs to a ``push`` run on ``main`` of the attestor
  workflow file, on that same commit.

Commit statuses are never read. A personal token can write a status, and it cannot
write an Actions check run.

Every failure mode (no ``gh``, no network, bad JSON, a rate limit, a missing field)
returns "not attested". Nothing here raises into a caller.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import localio

ATTEST_WORKFLOW_PATH = ".github/workflows/outcome-attest.yml"
ATTEST_CHECK_PREFIX = "outcome-attest/"
ATTEST_APP_SLUG = "github-actions"
ATTEST_BRANCH = "main"
DISABLE_ENV = "BRIGADE_CI_ATTESTATION"
CACHE_RELATIVE_PATH = Path(".brigade") / "memory-care" / "outcome" / "ci-attestation-cache.json"
CACHE_VERSION = 1
CACHE_MAX_BYTES = 64 * 1024
CACHE_MAX_ENTRIES = 256
NEGATIVE_TTL_SECONDS = 6 * 3600
GH_TIMEOUT_SECONDS = 20
_PR_COMMITS_LIMIT = 100
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REMOTE_RE = re.compile(r"github\.com[:/]+([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")

GhApi = Callable[[str], Any]


@dataclass(frozen=True)
class Attestation:
    attested: bool
    reason: str
    commit: str | None = None


def not_attested(reason: str) -> Attestation:
    return Attestation(False, reason)


class GhUnavailable(Exception):
    """The transport failed (no gh, no network, timeout, API error)."""


class GhNotFound(Exception):
    """The API answered 404: the commit or pull request is not on the remote."""


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


def _git(target: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            ["git", "-C", str(target), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def repo_slug(target: Path) -> str | None:
    """Return ``owner/name`` for the ``origin`` GitHub remote, or None."""
    completed = _git(target, "config", "--get", "remote.origin.url")
    if completed is None or completed.returncode != 0:
        return None
    match = _REMOTE_RE.search(completed.stdout.strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def commit_on_origin_main(target: Path, sha: str) -> bool:
    """Local veto: the commit must be an ancestor of ``origin/main`` in this clone."""
    completed = _git(target, "merge-base", "--is-ancestor", sha, f"origin/{ATTEST_BRANCH}")
    return completed is not None and completed.returncode == 0


def receipt_claims(receipt: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Return (manifest_id, head_sha, branch) the receipt claims, or None. These are claims, not proof."""
    binding = receipt.get("subject_binding") or receipt.get("binding")
    manifest_binding = binding.get("manifest_binding") if isinstance(binding, dict) else None
    manifest_id = manifest_binding.get("manifest_id") if isinstance(manifest_binding, dict) else None
    git = receipt.get("git")
    head = git.get("head") if isinstance(git, dict) else None
    branch = git.get("branch") if isinstance(git, dict) else None
    if not (isinstance(manifest_id, str) and manifest_id and isinstance(head, str) and isinstance(branch, str)):
        return None
    if not _SHA_RE.match(head) or not branch:
        return None
    return manifest_id, head, branch


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _items(payload: Any, key: str | None = None) -> list[dict[str, Any]]:
    raw = payload.get(key) if key and isinstance(payload, dict) else payload
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _pr_for_head(gh: GhApi, slug: str, head: str, branch: str) -> list[dict[str, Any]]:
    """Merged same-repo PRs into main whose own commits include ``head``. Local branch name may only veto."""
    found: list[dict[str, Any]] = []
    for pr in _items(gh(f"repos/{slug}/commits/{head}/pulls?per_page=30")):
        base = _obj(pr.get("base"))
        pr_head = _obj(pr.get("head"))
        pr_repo = _obj(pr_head.get("repo"))
        merge_sha = pr.get("merge_commit_sha")
        if not pr.get("merged_at") or base.get("ref") != ATTEST_BRANCH:
            continue
        if not (isinstance(merge_sha, str) and _SHA_RE.match(merge_sha)) or merge_sha == head:
            continue
        if pr_repo.get("full_name") != slug or pr_head.get("ref") != branch:
            continue
        number = pr.get("number")
        count = pr.get("commits")
        if not isinstance(number, int) or not isinstance(count, int) or count > _PR_COMMITS_LIMIT:
            continue
        shas = {c.get("sha") for c in _items(gh(f"repos/{slug}/pulls/{number}/commits?per_page={_PR_COMMITS_LIMIT}"))}
        if head in shas:
            found.append(pr)
    return found


def _confirm_check_run(gh: GhApi, slug: str, commit: str, manifest_id: str) -> Attestation:
    check_name = f"{ATTEST_CHECK_PREFIX}{manifest_id}"
    checks = _items(gh(f"repos/{slug}/commits/{commit}/check-runs?filter=latest&per_page=100"), "check_runs")
    named = [c for c in checks if c.get("name") == check_name]
    if not named:
        return not_attested("no_check_run")
    runs = _items(
        gh(f"repos/{slug}/actions/runs?head_sha={commit}&event=push&branch={ATTEST_BRANCH}&per_page=100"),
        "workflow_runs",
    )
    for check in named:
        app = _obj(check.get("app"))
        suite = _obj(check.get("check_suite"))
        if app.get("slug") != ATTEST_APP_SLUG or check.get("head_sha") != commit:
            continue
        if check.get("status") != "completed" or check.get("conclusion") != "success":
            return not_attested("check_not_success")
        for run in runs:
            path = str(run.get("path") or "").split("@", 1)[0]
            repo = _obj(run.get("head_repository"))
            if (
                run.get("check_suite_id") == suite.get("id")
                and suite.get("id") is not None
                and path == ATTEST_WORKFLOW_PATH
                and run.get("head_branch") == ATTEST_BRANCH
                and run.get("event") == "push"
                and run.get("head_sha") == commit
                and repo.get("full_name") == slug
            ):
                return Attestation(True, "attested", commit)
        return not_attested("run_not_attestor")
    return not_attested("check_not_github_actions")


def verify(target: Path, claims: tuple[str, str, str], slug: str, gh: GhApi) -> Attestation:
    """Decide from the API alone. Raises ``GhUnavailable`` when the transport fails."""
    manifest_id, head, branch = claims
    if branch in {ATTEST_BRANCH, "HEAD"}:
        return not_attested("branch_not_a_pr_branch")
    prs = _pr_for_head(gh, slug, head, branch)
    if not prs:
        return not_attested("no_merged_pr")
    last = not_attested("no_merged_pr")
    for pr in prs:
        commit = str(pr["merge_commit_sha"])
        if not commit_on_origin_main(target, commit):
            last = not_attested("commit_not_on_origin_main")
            continue
        result = _confirm_check_run(gh, slug, commit, manifest_id)
        if result.attested:
            return result
        last = result
    return last


def _load_cache(path: Path) -> dict[str, Any]:
    try:
        if path.is_symlink() or path.stat().st_size > CACHE_MAX_BYTES:
            return {}
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    entries = payload.get("entries") if isinstance(payload, dict) and payload.get("version") == CACHE_VERSION else None
    return entries if isinstance(entries, dict) else {}


def _fresh_negative(entry: Any, now: dt.datetime) -> bool:
    """Only a recent ``not_attested`` entry is trusted. The cache is forgeable, so it can only veto."""
    if not isinstance(entry, dict) or entry.get("result") != "not_attested":
        return False
    try:
        checked = dt.datetime.fromisoformat(str(entry.get("checked_at")))
    except ValueError:
        return False
    if checked.tzinfo is None:
        return False
    return 0 <= (now - checked).total_seconds() < NEGATIVE_TTL_SECONDS


def _store_negative(path: Path, entries: dict[str, Any], key: str, reason: str, now: dt.datetime) -> None:
    fresh = {k: v for k, v in entries.items() if _fresh_negative(v, now)}
    fresh[key] = {"result": "not_attested", "reason": reason, "checked_at": now.isoformat()}
    for stale in sorted(fresh, key=lambda k: str(fresh[k].get("checked_at")))[: max(0, len(fresh) - CACHE_MAX_ENTRIES)]:
        del fresh[stale]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        localio.write_text_atomic(path, json.dumps({"version": CACHE_VERSION, "entries": fresh}, sort_keys=True))
    except OSError:
        pass


class Attestor:
    """Resolve attestation for receipts in one workspace, memoised for the life of the instance."""

    def __init__(
        self,
        target: Path,
        *,
        gh_api: GhApi | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.target = target
        self._gh = gh_api or default_gh_api
        self._now = now or (lambda: dt.datetime.now(dt.timezone.utc))
        self._memo: dict[str, Attestation] = {}
        self._offline = False

    def attest(self, receipt: Mapping[str, Any]) -> Attestation:
        try:
            return self._attest(receipt)
        except Exception:  # noqa: BLE001 - attestation must never break rank or reconcile
            return not_attested("error")

    def _attest(self, receipt: Mapping[str, Any]) -> Attestation:
        if os.environ.get(DISABLE_ENV, "").strip().lower() in {"0", "off", "false", "no"}:
            return not_attested("disabled")
        claims = receipt_claims(receipt)
        if claims is None:
            return not_attested("receipt_missing_claims")
        slug = repo_slug(self.target)
        if slug is None:
            return not_attested("no_github_origin")
        key = f"{slug}|{claims[2]}|{claims[1]}|{claims[0]}"
        if key in self._memo:
            return self._memo[key]
        cache_path = self.target / CACHE_RELATIVE_PATH
        entries = _load_cache(cache_path)
        now = self._now()
        # A cached "attested" is never read: it must be re-confirmed from the API below.
        if _fresh_negative(entries.get(key), now):
            return self._memo.setdefault(key, not_attested(str(entries[key].get("reason") or "cached")))
        if self._offline:
            return not_attested("offline")
        try:
            result = verify(self.target, claims, slug, self._gh)
        except GhNotFound:
            result = not_attested("not_found")
        except GhUnavailable:
            self._offline = True
            return not_attested("offline")
        self._memo[key] = result
        if not result.attested:
            _store_negative(cache_path, entries, key, result.reason, now)
        return result
