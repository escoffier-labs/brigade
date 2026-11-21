"""Which trials a stretch of main history must have, and what each one says (#2589).

``outcome-attest`` runs once per push to main and plans over ``before..after`` of that
push. The merge queue can land several commits in one push, so a run belongs to the tip
of a group of commits and carries the verdict for all of them. The ledger cannot take
"no run" to mean "nothing to attest", because a run deleted or cancelled through the
Actions API looks exactly like that. So the expected verdicts come from git:

- Walk the first-parent commits after the seed tip, up to ``covers``.
- A commit with a trusted push run of the attest workflow is a push tip. Every commit
  since the previous tip belongs to that tip's group. A deleted run therefore folds its
  commits into the next tip's group, and that tip's run lacks the job.
- When any commit of a group touches the manifest scope, the tip's runs must hold a
  completed ``outcome-attest/<manifest>`` job on every attempt. Anything else is hurt.
- Commits after the last tip with a run wait out ``ATTEST_GRACE``, then count as hurt
  with the reason ``missing``.

Hurt only ever restores approved text, so failing toward hurt is the safe direction.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from . import verify_manifest
from .outcome_ledger_actions import MISSING, EvidenceError, GhApi, parse_time, tip_verdict, trial_row, trusted_runs
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
class TrialPlan:
    trials: tuple[dict[str, Any], ...]
    pending: tuple[Pending, ...]


def _scope_manifest(repo: Repo, sha: str, manifest_id: str, pin: str) -> verify_manifest.VerifyManifest | None:
    """The manifest at ``sha`` when it is the pinned one, else None. The pin makes the bytes the checked ones."""
    raw = repo.show(sha, f"{MANIFESTS_DIR}/{manifest_id}.json")
    if raw is None or hashlib.sha256(raw).hexdigest() != pin:
        return None
    try:
        return verify_manifest.manifest_from_payload(json.loads(raw.decode("utf-8")))
    except (UnicodeDecodeError, ValueError) as exc:
        raise EvidenceError("the pinned manifest cannot be read") from exc


def _touches_scope(repo: Repo, manifest: verify_manifest.VerifyManifest, sha: str) -> bool:
    """Whether this commit changed a path the planner would match to the manifest. Same logic as the planner."""
    return any(verify_manifest.path_in_manifest_scope(manifest, path) for path in repo.changed_paths(f"{sha}^1", sha))


def plan_trials(
    gh: GhApi,
    slug: str,
    repo_id: int,
    repo: Repo,
    *,
    manifest_id: str,
    pin: str,
    chain: Sequence[str],
    ref: dt.datetime,
    fingerprint_at: Callable[[str], str | None],
) -> TrialPlan:
    """The trials for ``chain`` (first-parent commits, oldest first) as seen at ``ref``.

    ``fingerprint_at(sha)`` returns the skill bundle fingerprint at a commit, or None when it
    holds no skill content. A group with no fingerprint records nothing.
    """
    trials: list[dict[str, Any]] = []
    pending: list[Pending] = []
    group: list[str] = []
    for sha in chain:
        group.append(sha)
        runs = trusted_runs(gh, slug, repo_id, sha)
        if not runs:
            continue
        members, group = group, []
        manifest = _scope_manifest(repo, sha, manifest_id, pin)
        if manifest is None:
            continue
        required = any(_touches_scope(repo, manifest, member) for member in members)
        verdict = tip_verdict(gh, slug, runs, manifest_id, sha, ref, required=required)
        if verdict.pending:
            pending.append(Pending(sha, "has an attest verdict still running"))
            continue
        row = trial_row(sha, verdict)
        fingerprint = fingerprint_at(sha) if row is not None else None
        if row is not None and fingerprint is not None:
            trials.append({"fingerprint": fingerprint, **row})
    if group:
        tail = group[-1]
        manifest = _scope_manifest(repo, tail, manifest_id, pin)
        in_scope = [member for member in group if manifest is not None and _touches_scope(repo, manifest, member)]
        if in_scope:
            newest = max(parse_time(repo.commit_time(member)) or ref for member in in_scope)
            if ref - newest < ATTEST_GRACE:
                pending.append(Pending(tail, "has no attest run yet and is inside the grace window"))
            else:
                fingerprint = fingerprint_at(tail)
                if fingerprint is not None:
                    trials.append(
                        {"fingerprint": fingerprint, "sha": tail, "outcome": "hurt", "reason": MISSING, "jobs": []}
                    )
    return TrialPlan(tuple(trials), tuple(pending))
