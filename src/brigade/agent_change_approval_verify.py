"""Approval-reference policy adaptation for agent-change indexes."""

from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import approval, approval_v2, approval_verification, attestation_input, localio, run_journal

_APPROVAL_LOCATOR_RE = re.compile(r"^\.brigade/runs/([A-Za-z0-9._-]+)/approvals/([0-9a-f]{32})\.json$")


@dataclass(frozen=True)
class _LoadedApproval:
    nonce: str
    path: Path
    envelope: Mapping[str, Any]
    statement: Mapping[str, Any]
    statement_bytes: bytes
    payload_sha256: str
    envelope_sha256: str


def _safe_approval_reference(
    *, target: Path, run_id: str, ref: Mapping[str, Any]
) -> tuple[str, Path | None, str | None]:
    """Return an approval file only after its full locator and path are safe."""
    locator = ref.get("locator")
    match = _APPROVAL_LOCATOR_RE.fullmatch(locator) if isinstance(locator, str) else None
    if match is None or match.group(1) != run_id:
        return "", None, "malformed-locator"
    nonce = match.group(2)
    supplied_nonce = ref.get("nonce")
    if supplied_nonce is not None and supplied_nonce != nonce:
        return nonce, None, "nonce-conflict"
    root = target / ".brigade"
    runs = root / "runs"
    run_dir = runs / run_id
    approvals = run_dir / "approvals"
    path = approvals / f"{nonce}.json"
    try:
        resolved_target = target.resolve()
        for candidate in (root, runs, run_dir, approvals, path):
            if candidate.is_symlink() or not candidate.is_relative_to(resolved_target):
                return nonce, None, "symlink-refused"
        if not approvals.is_dir() or not path.is_file() or not path.resolve().is_relative_to(resolved_target):
            return nonce, None, "missing"
    except OSError:
        return nonce, None, "unreadable"
    return nonce, path, None


def _safe_journal_path(target: Path, run_id: str) -> Path | None:
    """Validate every lexical journal parent before taking its coordination lock."""
    root = target / ".brigade"
    runs = root / "runs"
    run_dir = runs / run_id
    events = run_dir / "events"
    journal = events / "lifecycle.jsonl"
    try:
        resolved_target = target.resolve()
        for candidate in (root, runs, run_dir, events, journal):
            if candidate.is_symlink() or not candidate.is_relative_to(resolved_target):
                return None
        if not events.is_dir() or not journal.is_file() or not journal.resolve().is_relative_to(resolved_target):
            return None
    except OSError:
        return None
    return journal


def _load_selected_approval(path: Path, nonce: str) -> _LoadedApproval | None:
    try:
        envelope = attestation_input.read_json_object(path)
        statement, statement_bytes = approval._decode_statement(envelope)
    except (OSError, approval.ApprovalError, attestation_input.AttestationInputError):
        return None
    if statement is None or statement_bytes is None:
        return None
    return _LoadedApproval(
        nonce,
        path,
        envelope,
        statement,
        statement_bytes,
        hashlib.sha256(statement_bytes).hexdigest(),
        localio.canonical_json_digest(envelope),
    )


def _event_for_reference(events: Sequence[run_journal.RunEvent], *, nonce: str) -> run_journal.RunEvent | None:
    matches = [
        event
        for event in events
        if event.event_type == "approval"
        and event.payload.get("nonce") == nonce
        and event.payload.get("attestation_path") == f"approvals/{nonce}.json"
    ]
    return matches[0] if len(matches) == 1 else None


def _mark_invalid(observation: dict[str, Any]) -> None:
    observation["binding"] = "conflicted"
    if observation.get("policy_outcome") != "fail":
        observation["policy_outcome"] = "unevaluated"
    observation["approval_state"] = "unavailable"


def _merge_policy(observation: dict[str, Any], outcome: str) -> None:
    """Never improve an existing integrity or policy refusal observation."""
    if observation.get("binding") == "conflicted" or observation.get("rederivation") == "failed":
        return
    if observation.get("policy_outcome") == "fail":
        return
    observation["policy_outcome"] = outcome


def _current_requester_meta(
    *, target: Path, run_dir: Path, events: Sequence[run_journal.RunEvent], run_meta: Mapping[str, Any], version: int
) -> tuple[Mapping[str, Any] | None, approval_v2.RequesterIdentity | None]:
    """Use authenticated request evidence, never the mutable run projection."""
    try:
        requester = approval_v2.verify_recorded_request(target, run_dir, events)
    except (approval_v2.ApprovalV2Error, OSError):
        return None, None
    copied = copy.deepcopy(dict(run_meta))
    copied.pop("requester_principal", None)
    copied.pop("requester_keyid", None)
    if requester is not None and version == 1:
        copied["requester_principal"] = requester.principal
        copied["requester_keyid"] = requester.keyid
    return copied, requester


def verify_approval_references(
    *,
    target: Path,
    run_id: str | None,
    index_tree: str | None,
    references: Sequence[object],
    observations: list[dict[str, Any]],
) -> None:
    """Attach exact-event approval policy results while holding one journal lock."""
    if run_id is None:
        return
    approval_indexes = [
        index
        for index, ref in enumerate(references)
        if isinstance(ref, Mapping) and ref.get("kind") == "human-approval"
    ]
    if not approval_indexes:
        return
    run_dir = target / ".brigade" / "runs" / run_id
    journal_path = _safe_journal_path(target, run_id)
    if journal_path is None:
        for index in approval_indexes:
            _mark_invalid(observations[index])
        return
    try:
        with run_journal.journal_mutation(journal_path):
            report = run_journal.read_journal_bounded(journal_path)
            if report.partial_tail is not None or report.chain_errors:
                raise run_journal.RunJournalError("journal is corrupt")
            loaded: dict[int, tuple[run_journal.RunEvent, _LoadedApproval]] = {}
            for index in approval_indexes:
                ref, observation = references[index], observations[index]
                assert isinstance(ref, Mapping)
                nonce, path, reason = _safe_approval_reference(target=target, run_id=run_id, ref=ref)
                loaded_artifact = _load_selected_approval(path, nonce) if path is not None else None
                event = _event_for_reference(report.events, nonce=nonce) if loaded_artifact is not None else None
                if loaded_artifact is None or event is None or reason is not None:
                    _mark_invalid(observation)
                    continue
                if (
                    ref.get("payloadSha256") != loaded_artifact.payload_sha256
                    or ref.get("envelopeSha256") != loaded_artifact.envelope_sha256
                ):
                    _mark_invalid(observation)
                    continue
                try:
                    validated = approval_verification.validate_approval_artifact(
                        target=target,
                        run_dir=run_dir,
                        report=report,
                        event=event,
                        decision_value=event.payload.get("decision")
                        if isinstance(event.payload.get("decision"), str)
                        else None,
                        envelope=loaded_artifact.envelope,
                        statement=loaded_artifact.statement,
                        statement_bytes=loaded_artifact.statement_bytes,
                        strict_v1=True,
                    )
                except (approval.ApprovalError, approval_v2.ApprovalV2Error, OSError):
                    _mark_invalid(observation)
                    continue
                if validated.tree_fingerprint != index_tree:
                    _mark_invalid(observation)
                    continue
                loaded[index] = (event, loaded_artifact)

            duplicate_events = {
                event.sequence
                for event, _artifact in loaded.values()
                if sum(candidate.sequence == event.sequence for candidate, _ in loaded.values()) > 1
            }
            for index, (event, _artifact) in tuple(loaded.items()):
                if event.sequence in duplicate_events:
                    _mark_invalid(observations[index])
                    del loaded[index]

            latest = next((event for event in reversed(report.events) if event.event_type == "approval"), None)
            try:
                run_meta = attestation_input.read_json_object(run_dir / "run.json")
            except (OSError, attestation_input.AttestationInputError):
                run_meta = None
            context = approval._verification_context(target)
            context.live_tree = index_tree
            context.live_tree_state = "index-pinned"
            for index, (event, loaded_artifact) in loaded.items():
                observation = observations[index]
                if event is not latest:
                    observation["approval_state"] = "historical"
                    _merge_policy(observation, "unevaluated")
                    continue
                if not isinstance(run_meta, Mapping):
                    _mark_invalid(observation)
                    continue
                validated_artifact = approval_verification.validate_approval_artifact(
                    target=target,
                    run_dir=run_dir,
                    report=report,
                    event=event,
                    decision_value=event.payload.get("decision")
                    if isinstance(event.payload.get("decision"), str)
                    else None,
                    envelope=loaded_artifact.envelope,
                    statement=loaded_artifact.statement,
                    statement_bytes=loaded_artifact.statement_bytes,
                    strict_v1=True,
                )
                current_meta, requester = _current_requester_meta(
                    target=target,
                    run_dir=run_dir,
                    events=report.events,
                    run_meta=run_meta,
                    version=validated_artifact.version,
                )
                decision = event.payload.get("decision") if isinstance(event.payload.get("decision"), str) else None
                observation["approval_state"] = "current"
                if current_meta is None:
                    _mark_invalid(observation)
                    continue
                verified = approval_verification.evaluate_current_approval(
                    target=target,
                    run_dir=run_dir,
                    events=report.events,
                    event=event,
                    decision_value=decision,
                    prior_approvals=approval._prior_approvals(report.events),
                    artifact=validated_artifact,
                    run_meta=current_meta,
                    context=context,
                    now=None,
                )
                if verified.detail == "APPROVAL-STALE" or verified.status in {"APPROVAL-INVALID", "APPROVAL-STALE"}:
                    _mark_invalid(observation)
                elif requester is None:
                    _merge_policy(observation, "fail" if decision in {"deny", "hold"} else "unevaluated")
                elif verified.status == "APPROVED":
                    _merge_policy(observation, "pass")
                elif verified.status in {"SOD-VIOLATION", "APPROVAL-EXPIRED", "DENIED", "HELD"}:
                    _merge_policy(observation, "fail")
                else:
                    _merge_policy(observation, "unevaluated")

            reread = run_journal.read_journal_bounded(journal_path)
            changed = reread.partial_tail is not None or reread.chain_errors or reread.events != report.events
            for index, (_event, artifact) in loaded.items():
                ref = references[index]
                assert isinstance(ref, Mapping)
                nonce, path, reason = _safe_approval_reference(target=target, run_id=run_id, ref=ref)
                fresh = _load_selected_approval(path, nonce) if path == artifact.path and reason is None else None
                if fresh is None or (fresh.payload_sha256, fresh.envelope_sha256) != (
                    artifact.payload_sha256,
                    artifact.envelope_sha256,
                ):
                    changed = True
            if changed:
                for index in approval_indexes:
                    _mark_invalid(observations[index])
    except (OSError, run_journal.RunJournalError, approval.ApprovalError, approval_v2.ApprovalV2Error):
        for index in approval_indexes:
            _mark_invalid(observations[index])
