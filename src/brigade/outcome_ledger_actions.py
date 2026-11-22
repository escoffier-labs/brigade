"""Trial and review evidence for the outcome ledger, read from the Actions API (#2589).

The ledger trusts no check run and no commit status. A trial is a job of the
``outcome-attest`` workflow run on a main commit. Jobs belong to a workflow run and
cannot be created through the checks API, so a forged check run never becomes a trial.

A workflow run counts only when GitHub reports all of: the attest workflow file path,
the ``push`` event, the ``main`` branch, the commit asked about, and the same
repository as head and base. Every attempt of a run is read. Only a success helps. A
failure or a timeout hurts, and so does every other conclusion, with the conclusion
kept as the reason. When a commit needs a verdict and an attempt has no completed job
for it, that is hurt too, with the reason ``missing``. Absent evidence never helps.

Every read fails closed. A payload that is not the expected shape, a field that is
missing or has the wrong type, a page that is cut short, or a run with more attempts
than the cap raises ``EvidenceError``. Nothing here turns unreadable evidence into
"no runs", because that would hide a failure.

Owner review is a visibility control. Agents on the host work under the owner's
GitHub identity, so the review can be forged by a same-user agent. The check records
that an owner review sits on a merged PR in public history. It is not an access wall.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeGuard

from .outcome_ledger_git import is_sha

ATTEST_WORKFLOW_PATH = ".github/workflows/outcome-attest.yml"
ATTEST_WORKFLOW_FILE = "outcome-attest.yml"
ATTEST_JOB_PREFIX = "outcome-attest/"
ATTEST_EVENT = "push"
ATTEST_BRANCH = "main"
PER_PAGE = 100
MAX_ATTEMPTS = 50
MAX_PAGES = 50
# One table for every conclusion. A hang is a regression too, so a worse revision that
# times out cannot dodge a rollback by never failing. Any value not listed is hurt.
CONCLUSIONS: dict[str, tuple[str, str | None]] = {
    "success": ("helped", None),
    "failure": ("hurt", "failure"),
    "timed_out": ("hurt", "timed_out"),
}
# Other conclusions keep their own name as the recorded reason. Anything unknown is "other",
# so the ledger never stores free text from the API.
OTHER_HURT_REASONS = frozenset({"cancelled", "skipped", "neutral", "stale", "action_required", "startup_failure"})
MISSING = "missing"
# When several reasons apply, the earliest in this order is recorded.
REASON_ORDER = (
    "failure",
    "timed_out",
    "cancelled",
    "skipped",
    "neutral",
    "stale",
    "action_required",
    "startup_failure",
    "other",
    MISSING,
)

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


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def classify(conclusion: str) -> tuple[str, str | None]:
    """Whether a completed job's conclusion helped or hurt, and the reason a hurt one is recorded with."""
    known = CONCLUSIONS.get(conclusion)
    if known is not None:
        return known
    return "hurt", conclusion if conclusion in OTHER_HURT_REASONS else "other"


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
        if not _is_int(total) or total < 0:
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
    """Attest workflow runs for ``sha`` that pass every identity check, with ``status`` and ``run_attempt`` validated."""
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
            and _is_int(run.get("id"))
            and _repo_id(run, "repository") == repo_id
            and _repo_id(run, "head_repository") == repo_id
        ):
            if not isinstance(run.get("status"), str):
                raise EvidenceError("run has no readable status")
            attempts = run.get("run_attempt")
            if not _is_int(attempts) or attempts < 1:
                raise EvidenceError("run has no readable run_attempt")
            if attempts > MAX_ATTEMPTS:
                # Reading only a prefix would let a failure in a later attempt go unseen.
                raise EvidenceError(f"run has {attempts} attempts, more than the {MAX_ATTEMPTS} the ledger reads")
            kept.append(run)
    return kept


def _attempt_jobs(gh: GhApi, slug: str, run: dict[str, Any], name: str, sha: str, attempt: int) -> list[dict[str, Any]]:
    """The jobs named ``name`` that belong to this run, commit and attempt. Fields are read strictly."""
    found: list[dict[str, Any]] = []
    for job in _paged(gh, f"repos/{slug}/actions/runs/{run['id']}/attempts/{attempt}/jobs", "jobs"):
        if job.get("name") != name:
            continue
        if not (
            _is_int(job.get("id"))
            and _is_int(job.get("run_id"))
            and _is_int(job.get("run_attempt"))
            and isinstance(job.get("head_sha"), str)
            and isinstance(job.get("status"), str)
        ):
            raise EvidenceError("an attest job has an unreadable field")
        if job["run_id"] == run["id"] and job["head_sha"] == sha and job["run_attempt"] == attempt:
            found.append(job)
    return found


@dataclass(frozen=True)
class TipVerdict:
    """What the attest runs of one commit say as of ``ref``.

    ``pending`` means a run or job has not finished, or finished after ``ref``. ``outcome`` is
    None when nothing needs recording: no job, and none was required.
    """

    outcome: str | None
    reason: str | None
    jobs: tuple[dict[str, Any], ...]
    pending: bool
    has_job: bool


def tip_verdict(
    gh: GhApi,
    slug: str,
    runs: Sequence[dict[str, Any]],
    manifest_id: str,
    sha: str,
    ref: dt.datetime,
    *,
    required: bool,
) -> TipVerdict:
    """Read every attempt of every run on ``sha``. ``required`` makes an attempt without a job hurt.

    An attempt with no completed ``outcome-attest/<manifest>`` job counts against the text
    when ``required``, so deleting or cancelling a run never turns a missing verdict into a
    pass. Success is the only helped conclusion.
    """
    name = f"{ATTEST_JOB_PREFIX}{manifest_id}"
    jobs: list[dict[str, Any]] = []
    reasons: set[str] = set()
    pending = False
    for run in runs:
        if run["status"] != "completed":
            pending = True
        for attempt in range(1, run["run_attempt"] + 1):
            found = _attempt_jobs(gh, slug, run, name, sha, attempt)
            if not found:
                if required:
                    reasons.add(MISSING)
                continue
            for job in found:
                if job["status"] != "completed":
                    pending = True
                    continue
                completed = parse_time(job.get("completed_at"))
                conclusion = job.get("conclusion")
                if not (isinstance(conclusion, str) and completed):
                    # A finished attest job whose verdict cannot be read may be the failure.
                    raise EvidenceError("a completed attest job has no readable verdict")
                if completed > ref:
                    pending = True
                    continue
                outcome, reason = classify(conclusion)
                if outcome == "hurt" and reason is not None:
                    reasons.add(reason)
                jobs.append(
                    {
                        "run_id": run["id"],
                        "attempt": attempt,
                        "job_id": job["id"],
                        "conclusion": conclusion,
                        "completed_at": completed.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                )
    jobs.sort(key=lambda job: (job["run_id"], job["attempt"], job["job_id"]))
    if reasons:
        reason = next(item for item in REASON_ORDER if item in reasons)
        return TipVerdict("hurt", reason, tuple(jobs), pending, bool(jobs))
    if jobs:
        return TipVerdict("helped", None, tuple(jobs), pending, True)
    return TipVerdict(None, None, (), pending, False)


def trial_row(sha: str, verdict: TipVerdict) -> dict[str, Any] | None:
    """The canonical trial row for a verdict, or None while it is pending or empty. Every row has the same keys."""
    if verdict.pending or verdict.outcome is None:
        return None
    return {"sha": sha, "outcome": verdict.outcome, "reason": verdict.reason, "jobs": [dict(j) for j in verdict.jobs]}


def trial_for_commit(
    gh: GhApi,
    slug: str,
    repo_id: int,
    manifest_id: str,
    sha: str,
    cutoff: dt.datetime,
    *,
    required: bool = False,
) -> dict[str, Any] | None:
    """The trial for one commit's own runs, or None when it has no run, is pending, or needs no verdict."""
    runs = trusted_runs(gh, slug, repo_id, sha)
    if not runs:
        return None
    return trial_row(sha, tip_verdict(gh, slug, runs, manifest_id, sha, cutoff, required=required))


def has_completed_verdict(gh: GhApi, slug: str, repo_id: int, manifest_id: str, sha: str, now: dt.datetime) -> bool:
    """Whether a trusted attest job already finished on ``sha``, with any conclusion. Used for staleness."""
    runs = trusted_runs(gh, slug, repo_id, sha)
    return bool(runs) and tip_verdict(gh, slug, runs, manifest_id, sha, now, required=False).has_job


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
