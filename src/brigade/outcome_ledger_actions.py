"""Trial and review evidence for the outcome ledger, read from the Actions API (#2589).

The ledger trusts no check run and no commit status. A trial is a job of the
``outcome-attest`` workflow run on a main commit. Jobs belong to a workflow run and
cannot be created through the checks API, so a forged check run never becomes a trial.

A workflow run counts only when GitHub reports all of: the attest workflow file path,
the ``push`` event, the ``main`` branch, the commit asked about, and the same
repository as head and base. Every attempt of a run is read, and any failed or timed
out attempt makes the commit a hurt trial, so a rerun cannot launder a failure.

Every read fails closed. A payload that is not the expected shape, a page that is
cut short, or a run with more attempts than the cap raises ``EvidenceError``. Nothing
here turns unreadable evidence into "no runs", because that would hide a failure.

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
MAX_PAGES = 50
# A job that did not pass for a reason the commit controls. A hang is a regression too,
# so a worse revision that times out cannot dodge a rollback by never failing.
HURT_CONCLUSIONS = frozenset({"failure", "timed_out"})
HELPED_CONCLUSIONS = frozenset({"success"})

GhApi = Callable[[str], Any]


class EvidenceError(Exception):
    """The Actions API answered in a way the ledger cannot read safely."""


def parse_time(value: object) -> dt.datetime | None:
    """Parse an API or ledger UTC timestamp, or None when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _rows(payload: Any, key: str) -> list[dict[str, Any]]:
    """The list under ``key``. A missing key, a non-list or a non-object row raises."""
    raw = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise EvidenceError(f"unreadable {key} payload")
    return raw


def _paged(gh: GhApi, endpoint: str, key: str) -> list[dict[str, Any]]:
    """Every row of a list endpoint. A page that cannot reach ``total_count`` raises."""
    joiner = "&" if "?" in endpoint else "?"
    rows: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        payload = gh(f"{endpoint}{joiner}per_page={PER_PAGE}&page={page}")
        batch = _rows(payload, key)
        total = payload.get("total_count")
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            raise EvidenceError(f"{key} payload has no total_count")
        rows.extend(batch)
        if len(rows) >= total:
            return rows
        if not batch:
            raise EvidenceError(f"{key} pages ended before total_count")
    raise EvidenceError(f"{key} has more than {MAX_PAGES} pages")


def _repo_id(run: dict[str, Any], field: str) -> object:
    value = run.get(field)
    return value.get("id") if isinstance(value, dict) else None


def trusted_runs(gh: GhApi, slug: str, repo_id: int, sha: str) -> list[dict[str, Any]]:
    """Attest workflow runs for ``sha`` that pass every identity check."""
    endpoint = (
        f"repos/{slug}/actions/workflows/{ATTEST_WORKFLOW_FILE}/runs"
        f"?event={ATTEST_EVENT}&branch={ATTEST_BRANCH}&head_sha={sha}"
    )
    kept: list[dict[str, Any]] = []
    for run in _paged(gh, endpoint, "workflow_runs"):
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
    if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
        raise EvidenceError("run has no readable run_attempt")
    if attempts > MAX_ATTEMPTS:
        # Reading only a prefix would let a failure in a later attempt go unseen.
        raise EvidenceError(f"run has {attempts} attempts, more than the {MAX_ATTEMPTS} the ledger reads")
    rows: list[dict[str, Any]] = []
    for attempt in range(1, attempts + 1):
        jobs = _paged(gh, f"repos/{slug}/actions/runs/{run['id']}/attempts/{attempt}/jobs", "jobs")
        for job in jobs:
            if not (
                job.get("name") == name
                and job.get("status") == "completed"
                and job.get("run_id") == run["id"]
                and job.get("head_sha") == sha
                and job.get("run_attempt", attempt) == attempt
            ):
                continue
            completed = parse_time(job.get("completed_at"))
            if not (isinstance(job.get("id"), int) and isinstance(job.get("conclusion"), str) and completed):
                # A finished attest job whose verdict cannot be read may be the failure.
                raise EvidenceError("a completed attest job has no readable verdict")
            if completed <= cutoff:
                rows.append(
                    {
                        "attempt": attempt,
                        "job_id": job["id"],
                        "conclusion": job["conclusion"],
                        "completed_at": completed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                )
    return rows


def push_tip_evidence(
    gh: GhApi,
    slug: str,
    repo_id: int,
    manifest_id: str,
    sha: str,
    cutoff: dt.datetime,
) -> tuple[bool, dict[str, Any] | None]:
    """Whether ``sha`` is the tip of a push that ran attest, and its trial when a verdict finished by ``cutoff``.

    ``outcome-attest`` plans over ``before..after`` of a push, so its jobs carry the head of
    the whole push range. Only a push tip has a trusted run. Any failed or timed out attempt
    makes the trial hurt, and cancelled or skipped jobs count for nothing. One trial per
    commit, however many runs or attempts exist.
    """
    jobs: list[dict[str, Any]] = []
    runs = trusted_runs(gh, slug, repo_id, sha)
    for run in runs:
        jobs.extend({"run_id": run["id"], **job} for job in attest_jobs(gh, slug, run, manifest_id, sha, cutoff))
    conclusions = {job["conclusion"] for job in jobs}
    if conclusions & HURT_CONCLUSIONS:
        outcome = "hurt"
    elif conclusions & HELPED_CONCLUSIONS:
        outcome = "helped"
    else:
        return bool(runs), None
    jobs.sort(key=lambda job: (job["run_id"], job["attempt"], job["job_id"]))
    return True, {"sha": sha, "outcome": outcome, "jobs": jobs}


def verdict_pending(gh: GhApi, slug: str, repo_id: int, manifest_id: str, sha: str) -> bool:
    """Whether a trusted attest run on ``sha`` has not finished, or its ``outcome-attest/<manifest>`` job has not.

    A ledger computed while a verdict is still running would freeze the commit out of its
    evidence once it merges, so a pending verdict makes the ledger stale.
    """
    name = f"{ATTEST_JOB_PREFIX}{manifest_id}"
    for run in trusted_runs(gh, slug, repo_id, sha):
        if run.get("status", "completed") != "completed":
            return True
        attempts = run.get("run_attempt")
        if not isinstance(attempts, int) or isinstance(attempts, bool) or attempts < 1:
            raise EvidenceError("run has no readable run_attempt")
        if attempts > MAX_ATTEMPTS:
            raise EvidenceError(f"run has {attempts} attempts, more than the {MAX_ATTEMPTS} the ledger reads")
        jobs = _paged(gh, f"repos/{slug}/actions/runs/{run['id']}/attempts/{attempts}/jobs", "jobs")
        if any(job.get("name") == name and job.get("status") != "completed" for job in jobs):
            return True
    return False


def trial_for_commit(
    gh: GhApi,
    slug: str,
    repo_id: int,
    manifest_id: str,
    sha: str,
    cutoff: dt.datetime,
) -> dict[str, Any] | None:
    """The trial for one commit, or None when no trusted job finished a verdict by ``cutoff``."""
    return push_tip_evidence(gh, slug, repo_id, manifest_id, sha, cutoff)[1]


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
        if not isinstance(reviews, list) or not all(isinstance(row, dict) for row in reviews):
            raise EvidenceError("unreadable reviews payload")
        rows = reviews
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
