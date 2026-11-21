"""Trial and review evidence for the outcome ledger, read from the Actions API (#2589).

The ledger trusts no check run and no commit status. A trial is a job of the
``outcome-attest`` workflow run on a main commit. Jobs belong to a workflow run and
cannot be created through the checks API, so a forged check run never becomes a trial.

A workflow run counts only when GitHub reports all of: the attest workflow file path,
the ``push`` event, the ``main`` branch, the commit asked about, and the same
repository as head and base. Every attempt of a run is read, and any failed attempt
makes the commit a hurt trial, so a rerun cannot launder a failure.

Owner review is a visibility control. Agents on the host work under the owner's
GitHub identity, so the review can be forged by a same-user agent. The check records
that an owner review sits on a merged PR in public history. It is not an access wall.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from typing import Any

from .outcome_ledger_git import is_sha

ATTEST_WORKFLOW_PATH = ".github/workflows/outcome-attest.yml"
ATTEST_WORKFLOW_FILE = "outcome-attest.yml"
ATTEST_JOB_PREFIX = "outcome-attest/"
ATTEST_EVENT = "push"
ATTEST_BRANCH = "main"
PER_PAGE = 100
MAX_ATTEMPTS = 50

GhApi = Callable[[str], Any]


def parse_time(value: object) -> dt.datetime | None:
    """Parse an API or ledger UTC timestamp, or None when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _items(payload: Any, key: str) -> list[dict[str, Any]]:
    raw = payload.get(key) if isinstance(payload, dict) else None
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _repo_id(run: dict[str, Any], field: str) -> object:
    value = run.get(field)
    return value.get("id") if isinstance(value, dict) else None


def trusted_runs(gh: GhApi, slug: str, repo_id: int, sha: str) -> list[dict[str, Any]]:
    """Attest workflow runs for ``sha`` that pass every identity check."""
    endpoint = (
        f"repos/{slug}/actions/workflows/{ATTEST_WORKFLOW_FILE}/runs"
        f"?event={ATTEST_EVENT}&branch={ATTEST_BRANCH}&head_sha={sha}&per_page={PER_PAGE}"
    )
    kept: list[dict[str, Any]] = []
    for run in _items(gh(endpoint), "workflow_runs"):
        if (
            run.get("path") == ATTEST_WORKFLOW_PATH
            and run.get("event") == ATTEST_EVENT
            and run.get("head_branch") == ATTEST_BRANCH
            and run.get("head_sha") == sha
            and isinstance(run.get("id"), int)
            and _repo_id(run, "repository") == repo_id
            and _repo_id(run, "head_repository") == repo_id
        ):
            kept.append(run)
    return kept


def attest_jobs(
    gh: GhApi,
    slug: str,
    run: dict[str, Any],
    manifest_id: str,
    sha: str,
    cutoff: dt.datetime,
) -> list[dict[str, Any]]:
    """Completed ``outcome-attest/<manifest>`` jobs of every attempt of ``run`` finished by ``cutoff``."""
    name = f"{ATTEST_JOB_PREFIX}{manifest_id}"
    attempts = run.get("run_attempt")
    if not isinstance(attempts, int) or attempts < 1:
        return []
    rows: list[dict[str, Any]] = []
    for attempt in range(1, min(attempts, MAX_ATTEMPTS) + 1):
        payload = gh(f"repos/{slug}/actions/runs/{run['id']}/attempts/{attempt}/jobs?per_page={PER_PAGE}")
        for job in _items(payload, "jobs"):
            completed = parse_time(job.get("completed_at"))
            if (
                job.get("name") == name
                and job.get("status") == "completed"
                and job.get("run_id") == run["id"]
                and job.get("head_sha") == sha
                and job.get("run_attempt", attempt) == attempt
                and isinstance(job.get("id"), int)
                and isinstance(job.get("conclusion"), str)
                and completed is not None
                and completed <= cutoff
            ):
                rows.append(
                    {
                        "attempt": attempt,
                        "job_id": job["id"],
                        "conclusion": job["conclusion"],
                        "completed_at": completed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                )
    return rows


def trial_for_commit(
    gh: GhApi,
    slug: str,
    repo_id: int,
    manifest_id: str,
    sha: str,
    cutoff: dt.datetime,
) -> dict[str, Any] | None:
    """The trial for one main commit, or None when no trusted job finished a verdict by ``cutoff``.

    Any failed attempt makes the commit a hurt trial. Cancelled, skipped and timed-out jobs
    count for nothing. One trial per commit, however many runs or attempts exist.
    """
    jobs: list[dict[str, Any]] = []
    run_ids: list[int] = []
    for run in trusted_runs(gh, slug, repo_id, sha):
        found = attest_jobs(gh, slug, run, manifest_id, sha, cutoff)
        if found:
            run_ids.append(run["id"])
            jobs.extend({"run_id": run["id"], **job} for job in found)
    conclusions = {job["conclusion"] for job in jobs}
    if "failure" in conclusions:
        outcome = "hurt"
    elif "success" in conclusions:
        outcome = "helped"
    else:
        return None
    jobs.sort(key=lambda job: (job["run_id"], job["attempt"], job["job_id"]))
    return {"sha": sha, "outcome": outcome, "jobs": jobs}


def has_completed_verdict(gh: GhApi, slug: str, repo_id: int, manifest_id: str, sha: str, now: dt.datetime) -> bool:
    """Whether a trusted attest job already gave a pass or fail verdict on ``sha``. Used for staleness."""
    return trial_for_commit(gh, slug, repo_id, manifest_id, sha, now) is not None


def owner_review_ok(
    gh: GhApi,
    slug: str,
    pr_number: int,
    owners: Sequence[str],
) -> bool:
    """An APPROVED review on the PR's current head commit from a login in ``owners``.

    Only each reviewer's latest decision counts, so a later change request cancels an approval.
    """
    pull = gh(f"repos/{slug}/pulls/{pr_number}")
    head = pull.get("head") if isinstance(pull, dict) else None
    head_sha = head.get("sha") if isinstance(head, dict) else None
    if not is_sha(head_sha):
        return False
    allowed = {login.lower() for login in owners}
    latest: dict[str, tuple[str, object]] = {}
    page = 1
    while True:
        reviews = gh(f"repos/{slug}/pulls/{pr_number}/reviews?per_page={PER_PAGE}&page={page}")
        rows = [row for row in reviews if isinstance(row, dict)] if isinstance(reviews, list) else []
        for row in rows:
            user = row.get("user")
            login = user.get("login") if isinstance(user, dict) else None
            state = row.get("state")
            if isinstance(login, str) and state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                latest[login.lower()] = (state, row.get("commit_id"))
        if len(rows) < PER_PAGE:
            break
        page += 1
    return any(
        login in allowed and state == "APPROVED" and commit == head_sha for login, (state, commit) in latest.items()
    )
