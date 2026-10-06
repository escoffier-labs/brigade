"""Pure, bounded offline campaign plans. No dispatch or completion authority."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any

MAX_MEMBERS = 256
MAX_OBSERVATIONS = 1024
MAX_INPUT_BYTES = 256 * 1024
PREVIEW_KIND = "fleet-campaign-preview"
_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")


class PreviewError(ValueError):
    """Fixed refusal codes deliberately contain no input values."""


def opaque_key(value: object) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value) or value.lower() == "latest":
        raise PreviewError("opaque_reference_required")
    return value


def _fingerprint(value: object) -> str:
    if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
        raise PreviewError("fingerprint_required")
    return value


@dataclass(frozen=True)
class Binding:
    """Exact existing authority references, supplied rather than resolved here."""

    repo_id: str
    authority_kind: str
    authority_id: str
    authority_fingerprint: str
    spec_fingerprint: str

    def __post_init__(self) -> None:
        opaque_key(self.repo_id)
        opaque_key(self.authority_id)
        if self.authority_kind not in {"task", "action"}:
            raise PreviewError("binding_refused")
        _fingerprint(self.authority_fingerprint)
        _fingerprint(self.spec_fingerprint)


@dataclass(frozen=True)
class Observation:
    """Supplied/unverified evidence, never a Hub or task-ledger snapshot."""

    repo_id: str
    authority_id: str
    authority_fingerprint: str
    task_status: str
    execution_status: str
    attempt_id: str
    fresh: bool
    complete: bool
    job_key: str | None
    artifact_key: str | None

    def __post_init__(self) -> None:
        for value in (self.repo_id, self.authority_id, self.attempt_id):
            opaque_key(value)
        for reference in (self.job_key, self.artifact_key):
            if reference is not None:
                opaque_key(reference)
        _fingerprint(self.authority_fingerprint)
        if self.task_status not in {
            "pending",
            "in_progress",
            "blocked",
            "deferred",
            "done",
            "cancelled",
            "dismissed",
            "unknown",
        } or self.execution_status not in {"unknown", "running", "succeeded", "failed"}:
            raise PreviewError("observation_refused")
        if type(self.fresh) is not bool or type(self.complete) is not bool:
            raise PreviewError("observation_refused")


def parse_bindings(raw: object) -> tuple[Binding, ...]:
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_MEMBERS:
        raise PreviewError("bindings_refused")
    fields = set(Binding.__dataclass_fields__)
    result = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != fields:
            raise PreviewError("binding_refused")
        result.append(Binding(**item))
    return tuple(result)


def parse_observations(raw: object) -> tuple[Observation, ...]:
    if not isinstance(raw, list) or len(raw) > MAX_OBSERVATIONS:
        raise PreviewError("observations_refused")
    fields = set(Observation.__dataclass_fields__)
    result = []
    for item in raw:
        if not isinstance(item, dict) or set(item) != fields:
            raise PreviewError("observation_refused")
        result.append(Observation(**item))
    return tuple(result)


def _canonical(bindings: Sequence[Binding]) -> list[dict[str, Any]]:
    if not 1 <= len(bindings) <= MAX_MEMBERS or any(not isinstance(b, Binding) for b in bindings):
        raise PreviewError("bindings_refused")
    if len({b.repo_id for b in bindings}) != len(bindings):
        raise PreviewError("duplicate_repository_binding")
    return [asdict(b) for b in sorted(bindings, key=lambda b: b.repo_id)]


def _digest(bindings: list[dict[str, Any]]) -> str:
    data = json.dumps(bindings, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(data.encode("ascii")).hexdigest()


def _compare_prior(prior: object, campaign_id: str, fingerprint: str) -> str:
    if prior is None:
        return "not_checked"
    if (
        not isinstance(prior, dict)
        or prior.get("kind") != PREVIEW_KIND
        or type(prior.get("preview_version")) is not int
    ):
        raise PreviewError("prior_preview_refused")
    if prior["preview_version"] != 1:
        raise PreviewError("prior_preview_refused")
    try:
        prior_id = opaque_key(prior.get("campaign_id"))
        prior_bindings = _canonical(parse_bindings(prior.get("bindings")))
        prior_fingerprint = _fingerprint(prior.get("membership_fingerprint"))
    except PreviewError:
        raise PreviewError("prior_preview_refused") from None
    if _digest(prior_bindings) != prior_fingerprint:
        raise PreviewError("prior_preview_refused")
    if prior_id != campaign_id or prior_fingerprint != fingerprint:
        raise PreviewError("prior_preview_conflict")
    return "matched"


def _member(binding: Binding, observations: Sequence[Observation]) -> dict[str, Any]:
    # Identical repeats do not inflate counts; separate attempts are evidence only.
    records = sorted({json.dumps(asdict(o), sort_keys=True) for o in observations if o.repo_id == binding.repo_id})
    evidence = [json.loads(record) for record in records]
    condition = "missing"
    if evidence:
        condition = "unverified"
        if any(
            o["authority_id"] != binding.authority_id or o["authority_fingerprint"] != binding.authority_fingerprint
            for o in evidence
        ):
            condition = "mismatch"
        elif any(not o["complete"] for o in evidence):
            condition = "incomplete"
        elif any(not o["fresh"] for o in evidence):
            condition = "stale"
        elif len({o["task_status"] for o in evidence}) > 1 or len({o["attempt_id"] for o in evidence}) < len(evidence):
            condition = "conflict"
    return {
        "repo_id": binding.repo_id,
        "task_state": "unknown",
        "launch_safety": "unknown",
        "observation_provenance": "supplied_unverified" if evidence else "none",
        "observation_count": len(evidence),
        "evidence_condition": condition,
        "observations": evidence,
    }


def preview(
    *,
    campaign_id: str,
    repo_ids: Sequence[str],
    bindings: Sequence[Binding],
    observations: Sequence[Observation] = (),
    prior: object = None,
) -> dict[str, Any]:
    """Project immutable membership and unverified evidence without IO.

    Terminal exclusion needs matched authoritative task state. This offline
    slice supplies none, so every member stays unknown and a resume candidate.
    """
    opaque_key(campaign_id)
    if not 1 <= len(repo_ids) <= MAX_MEMBERS:
        raise PreviewError("membership_refused")
    selected = {opaque_key(repo_id) for repo_id in repo_ids}
    canonical = _canonical(bindings)
    if {b.repo_id for b in bindings} != selected:
        raise PreviewError("membership_binding_mismatch")
    if len(observations) > MAX_OBSERVATIONS or any(not isinstance(o, Observation) for o in observations):
        raise PreviewError("observations_refused")
    fingerprint = _digest(canonical)
    comparison = _compare_prior(prior, campaign_id, fingerprint)
    members = [_member(binding, observations) for binding in sorted(bindings, key=lambda b: b.repo_id)]
    return {
        "kind": PREVIEW_KIND,
        "preview_version": 1,
        "read_only": True,
        "campaign_id": campaign_id,
        "membership_fingerprint": fingerprint,
        "bindings": canonical,
        "binding_provenance": "supplied_unverified",
        "prior_comparison": comparison,
        "conflict_scope": "explicit_prior_only",
        "members": members,
        "resume_candidates": sorted(selected),
        "summary": {"total": len(members), "unknown": len(members), "terminal": 0, "done": False},
    }
