"""The attest verdicts required by main's first-parent history (#2589).

Each commit that touches the manifest scope needs its own trusted push run and a
completed attest job on every attempt. The merge queue can land a batch in one push.
An in-scope commit inside that batch still needs its own verdict, so it counts as
missing after the grace window. Hurt can only restore approved text.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from . import verify_manifest
from .outcome_ledger_actions import MISSING, GhApi, parse_time, tip_verdict, trial_row, trusted_runs
from .outcome_ledger_git import Repo

MANIFESTS_DIR = "verify/manifests"
# How long a commit may wait for its attest run to appear before the absence counts against it.
ATTEST_GRACE = dt.timedelta(hours=2)


@dataclass(frozen=True)
class Pending:
    """A commit whose verdict is not final yet, and why."""

    sha: str
    why: str


@dataclass(frozen=True)
class ScopedCommit:
    sha: str
    has_manifest: bool


@dataclass(frozen=True)
class ScopedHistory:
    """In-scope commits and the start index of the latest cohort, if it restarted."""

    commits: tuple[ScopedCommit, ...]
    restart_at: int | None


@dataclass(frozen=True)
class TrialPlan:
    trials: tuple[dict[str, Any], ...]
    pending: tuple[Pending, ...]
    restarted: bool


def _scope_manifest(
    raw: bytes | None, expected: verify_manifest.VerifyManifest
) -> verify_manifest.VerifyManifest | None:
    """Read a historical manifest for the same subject, if usable."""
    if raw is None:
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        manifest = verify_manifest.manifest_from_payload(payload)
    except (UnicodeDecodeError, ValueError):
        return None
    if (
        manifest.manifest_id != expected.manifest_id
        or manifest.artifact_kind != expected.artifact_kind
        or manifest.artifact_id != expected.artifact_id
        or manifest.subject_path != expected.subject_path
    ):
        return None
    return manifest


def scope_history(repo: Repo, chain: Sequence[str], manifest: verify_manifest.VerifyManifest) -> ScopedHistory:
    """Find in-scope commits and the latest cohort boundary, independent of Actions evidence.

    A pin-changing commit is scoped by the previous manifest and belongs to the new cohort.
    A missing or unusable manifest leaves the last usable scope in force.
    """
    if not chain:
        return ScopedHistory((), None)
    path = f"{MANIFESTS_DIR}/{manifest.manifest_id}.json"
    active_raw = repo.show(f"{chain[0]}^1", path)
    active = _scope_manifest(active_raw, manifest)
    commits: list[ScopedCommit] = []
    restart_at = None
    for sha in chain:
        raw = repo.show(sha, path)
        current = active if raw == active_raw else _scope_manifest(raw, manifest)
        scope = active or current or manifest
        if current is not None and raw != active_raw:
            restart_at = len(commits)
            active, active_raw = current, raw
        if any(verify_manifest.path_in_manifest_scope(scope, path) for path in repo.changed_paths(f"{sha}^1", sha)):
            commits.append(ScopedCommit(sha, current is not None))
    return ScopedHistory(tuple(commits), restart_at)


def is_missing_trial(row: Mapping[str, Any]) -> bool:
    return row.get("outcome") == "hurt" and row.get("reason") == MISSING


def _missing_trial(sha: str) -> dict[str, Any]:
    return {"sha": sha, "outcome": "hurt", "reason": MISSING, "jobs": []}


def _verdict_at(
    gh: GhApi,
    slug: str,
    repo_id: int,
    repo: Repo,
    commit: ScopedCommit,
    manifest_id: str,
    ref: dt.datetime,
) -> dict[str, Any] | Pending:
    sha = commit.sha
    runs = trusted_runs(gh, slug, repo_id, sha) if commit.has_manifest else []
    if not runs:
        if ref - (parse_time(repo.commit_time(sha)) or ref) < ATTEST_GRACE:
            return Pending(sha, "has no attest run yet and is inside the grace window")
        return _missing_trial(sha)
    verdict = tip_verdict(gh, slug, runs, manifest_id, sha, ref, required=True)
    if verdict.pending:
        return Pending(sha, "has an attest verdict still running")
    return trial_row(sha, verdict) or _missing_trial(sha)


def plan_trials(
    gh: GhApi,
    slug: str,
    repo_id: int,
    repo: Repo,
    *,
    manifest: verify_manifest.VerifyManifest,
    chain: Sequence[str],
    ref: dt.datetime,
    fingerprint_at: Callable[[str], str | None],
) -> TrialPlan:
    """Plan per-commit verdicts as of ref, retaining missing evidence across pin changes."""
    scope = scope_history(repo, chain, manifest)
    trials: list[dict[str, Any]] = []
    pending: list[Pending] = []
    for index, commit in enumerate(scope.commits):
        result = _verdict_at(gh, slug, repo_id, repo, commit, manifest.manifest_id, ref)
        superseded = scope.restart_at is not None and index < scope.restart_at
        if isinstance(result, Pending):
            if not superseded:
                pending.append(result)
                continue
            result = _missing_trial(commit.sha)
        if not superseded or is_missing_trial(result):
            trials.append({"fingerprint": fingerprint_at(commit.sha), **result})
    return TrialPlan(tuple(trials), tuple(pending), scope.restart_at is not None)
