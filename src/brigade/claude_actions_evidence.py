"""Pure offline run evidence for a caller-confirmed Claude Action workflow.

Confirmation belongs to workflow discovery. Names, execution success and PR
associations cannot confirm an action, a completed review or current PR coverage.
This module performs no discovery, IO, persistence or cloud admission.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, cast
from urllib.parse import urlsplit

RunStatus = Literal["queued", "in_progress", "completed", "requested", "waiting", "pending", "unknown"]
RunConclusion = Literal[
    "success", "failure", "cancelled", "skipped", "timed_out", "neutral", "action_required", "stale", "unknown"
]
HeadRelation = Literal["current-head", "stale-head", "unknown"]
RunnerKind = Literal["github-hosted", "self-hosted", "unknown"]
AssociationState = Literal["complete", "partial", "unknown"]

MAX_PULL_REQUESTS = 20
MAX_PROVIDER_ID = (1 << 63) - 1
_STATUSES = {"queued", "in_progress", "completed", "requested", "waiting", "pending"}
_CONCLUSIONS = {"success", "failure", "cancelled", "skipped", "timed_out", "neutral", "action_required", "stale"}
_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9_.-]{1,100}")
_SHA = re.compile(r"[0-9a-fA-F]{40}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)")


class ActionEvidenceError(ValueError):
    """Required identity or caller provenance is invalid. Never echoes payloads."""


def _text(value: object, limit: int) -> str | None:
    if not isinstance(value, str) or not value or len(value) > limit:
        return None
    if any(unicodedata.category(char).startswith("C") for char in value):
        return None
    return value


def _repository(value: object) -> str | None:
    text = _text(value, 140)
    if text is None or _REPOSITORY.fullmatch(text) is None:
        return None
    repo = text.split("/")[1]
    if repo in {".", ".."} or repo.lower().endswith(".git"):
        return None
    return text.lower()


def _provider_id(value: object) -> int | None:
    if type(value) is not int or not 1 <= value <= MAX_PROVIDER_ID:
        return None
    return value


def _sha(value: object) -> str | None:
    text = _text(value, 40)
    return text.lower() if text is not None and _SHA.fullmatch(text) else None


def _timestamp(value: object) -> str | None:
    text = _text(value, 64)
    if text is None or _TIMESTAMP.fullmatch(text) is None:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


@dataclass(frozen=True)
class ConfirmedWorkflow:
    """Trusted caller attests this exact definition revision applies to the run.

    Constructing this record validates identity syntax, not action discovery.
    Revision is the full definition commit SHA, never a branch or action ref.
    """

    repository: str
    workflow_id: int
    revision: str

    def __post_init__(self) -> None:
        repo = _repository(self.repository)
        revision = _sha(self.revision)
        if repo is None or _provider_id(self.workflow_id) is None or revision is None:
            raise ActionEvidenceError("confirmation requires repository, numeric workflow ID and definition commit SHA")
        object.__setattr__(self, "repository", repo)
        object.__setattr__(self, "revision", revision)


@dataclass(frozen=True)
class PullRequestAssociation:
    """Provider association only. Head and base may differ from the run SHA."""

    number: int
    head_sha: str | None
    base_sha: str | None
    url: str


@dataclass(frozen=True)
class ActionRunEvidence:
    """One attempt snapshot, separate from review records and local capacity."""

    workflow: ConfirmedWorkflow
    run_id: int
    run_attempt: int
    event: str | None
    branch: str | None
    head_sha: str | None
    head_relation: HeadRelation
    status: RunStatus
    conclusion: RunConclusion
    observed_at: str
    created_at: str | None
    started_at: str | None
    updated_at: str | None
    html_url: str | None
    api_url: str | None
    pull_requests: tuple[PullRequestAssociation, ...]
    pull_requests_state: AssociationState
    pull_requests_truncated: bool
    runner_kind: RunnerKind
    provider: Literal["github-actions"] = "github-actions"
    local_node_id: None = None
    review_state: Literal["unknown"] = "unknown"
    findings_state: Literal["unknown"] = "unknown"
    merge_readiness: Literal["unknown"] = "unknown"

    @property
    def identity(self) -> tuple[str, int, int]:
        """Canonical repository plus provider run ID and attempt, not observation time."""
        return self.workflow.repository, self.run_id, self.run_attempt


def _run_url(value: object, repository: str, run_id: int, attempt: int, *, api: bool) -> str | None:
    text = _text(value, 2048)
    if text is None:
        return None
    try:
        parsed = urlsplit(text)
    except ValueError:
        return None
    host = "api.github.com" if api else "github.com"
    prefix = "/repos" if api else ""
    path = f"{prefix}/{repository}/actions/runs/{run_id}"
    if (
        parsed.scheme != "https"
        or parsed.netloc != host
        or parsed.query
        or parsed.fragment
        or "?" in text
        or "#" in text
        or parsed.path.lower() not in {path, f"{path}/attempts/{attempt}"}
    ):
        return None
    return f"https://{host}{parsed.path.lower()}"


def _associations(value: object, repository: str) -> tuple[tuple[PullRequestAssociation, ...], AssociationState, bool]:
    if not isinstance(value, list):
        return (), "unknown", False
    truncated = len(value) > MAX_PULL_REQUESTS
    partial = truncated
    rows: list[PullRequestAssociation] = []
    seen: set[int] = set()
    for raw in value[:MAX_PULL_REQUESTS]:
        if not isinstance(raw, Mapping):
            partial = True
            continue
        number = _provider_id(raw.get("number"))
        if number is None or number in seen:
            partial = True
            continue
        seen.add(number)
        head, base = raw.get("head"), raw.get("base")
        rows.append(
            PullRequestAssociation(
                number=number,
                head_sha=_sha(head.get("sha")) if isinstance(head, Mapping) else None,
                base_sha=_sha(base.get("sha")) if isinstance(base, Mapping) else None,
                url=f"https://github.com/{repository}/pull/{number}",
            )
        )
    return tuple(rows), "partial" if partial else "complete", truncated


def normalize_run(
    run: Mapping[str, object],
    *,
    workflow: ConfirmedWorkflow,
    observed_at: str,
    current_head_sha: str | None = None,
    runner_kind: RunnerKind = "unknown",
) -> ActionRunEvidence:
    """Normalize one bounded REST run fixture without inferring configuration.

    ``workflow`` must be confirmed for this run by a trusted discovery caller.
    ``current_head_sha`` is the caller's current comparison head. A matching SHA
    does not attest review coverage, including merge and pull_request_target runs.
    ``runner_kind`` requires explicit caller confirmation, never a name heuristic.
    Missing required identity rejects the record. Optional malformed metadata is
    omitted or unknown. No raw titles, actor data, logs or transcripts survive.
    """
    if not isinstance(workflow, ConfirmedWorkflow) or not isinstance(run, Mapping):
        raise ActionEvidenceError("run requires a confirmed workflow and an object")
    run_id = _provider_id(run.get("id"))
    attempt = _provider_id(run.get("run_attempt"))
    if run_id is None or attempt is None:
        raise ActionEvidenceError("run ID and attempt must be bounded positive integers")
    repository = run.get("repository")
    if (
        not isinstance(repository, Mapping)
        or _repository(repository.get("full_name")) != workflow.repository
        or _provider_id(run.get("workflow_id")) != workflow.workflow_id
    ):
        raise ActionEvidenceError("run repository and workflow ID must match confirmation")
    observed = _timestamp(observed_at)
    if observed is None:
        raise ActionEvidenceError("observation time must be a timezone-aware timestamp")
    if runner_kind not in ("github-hosted", "self-hosted", "unknown"):
        raise ActionEvidenceError("runner hosting must be explicit or unknown")

    status_text = _text(run.get("status"), 32)
    status = cast(RunStatus, status_text if status_text in _STATUSES else "unknown")
    conclusion_text = _text(run.get("conclusion"), 32)
    conclusion = cast(
        RunConclusion, conclusion_text if status == "completed" and conclusion_text in _CONCLUSIONS else "unknown"
    )
    head = _sha(run.get("head_sha"))
    current = _sha(current_head_sha)
    relation: HeadRelation = "unknown"
    if head is not None and current is not None:
        relation = "current-head" if head == current else "stale-head"
    prs, prs_state, prs_truncated = _associations(run.get("pull_requests"), workflow.repository)
    event = _text(run.get("event"), 64)
    if event is not None and re.fullmatch(r"[a-z][a-z0-9_]*", event) is None:
        event = None
    return ActionRunEvidence(
        workflow=workflow,
        run_id=run_id,
        run_attempt=attempt,
        event=event,
        branch=_text(run.get("head_branch"), 255),
        head_sha=head,
        head_relation=relation,
        status=status,
        conclusion=conclusion,
        observed_at=observed,
        created_at=_timestamp(run.get("created_at")),
        started_at=_timestamp(run.get("run_started_at")),
        updated_at=_timestamp(run.get("updated_at")),
        html_url=_run_url(run.get("html_url"), workflow.repository, run_id, attempt, api=False),
        api_url=_run_url(run.get("url"), workflow.repository, run_id, attempt, api=True),
        pull_requests=prs,
        pull_requests_state=prs_state,
        pull_requests_truncated=prs_truncated,
        runner_kind=runner_kind,
    )
