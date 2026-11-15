"""Fail-closed lesson capture admission, independent of worker instructions."""

from __future__ import annotations

import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from . import lesson_activity as activity, lesson_io as io, receipt_signing, verify_trial
from .handoff_ingest_rules import safe_card_name, sha256_hex

FORBIDDEN = re.compile(
    r"\b(?:unresolved|dead[ -]end|infrastructure failure|gave up|couldn.t get)\b|"
    r"\b(?:tool|cli|command|adapter|provider)\b[^.\n]{0,60}\b(?:broken|unusable|does not work|doesn't work)\b|"
    r"\b(?:is|was|remains)\s+(?:broken|unusable)\b",
    re.I,
)


def read_card(target: Path, name: str) -> bytes | None:
    return io.read_bytes(target / "memory/cards" / name, limit=8192, absent=True)


def verified_receipt(
    target: Path, relative: str, *, content: str | None, card: str, events: list[dict[str, Any]], earliest: float
) -> dict[str, Any]:
    io.relative(relative)
    if not re.fullmatch(r"\.brigade/work/verify-runs/[A-Za-z0-9_-]+/receipt\.json", relative):
        raise ValueError("lesson requires a local verification receipt")
    observed = next(
        (
            e
            for e in events
            if e["kind"] in ({"PostToolUse", "PostToolUseFailure"} if content is None else {"PostToolUse"})
            and e.get("verify_command") is True
            and e.get("verify_run_id") == Path(relative).parent.name
        ),
        None,
    )
    if observed is None:
        raise ValueError("lesson receipt must come from brigade work verify run")
    receipt = json.loads(io.text(target / relative, limit=256 * 1024) or "")
    if not isinstance(receipt, dict):
        raise ValueError("invalid lesson evidence")
    if not receipt_signing.authentic_workspace_receipt(target, receipt):
        raise ValueError("lesson requires an authentic workspace verification receipt")
    if receipt.get("run_id") != Path(relative).parent.name or receipt.get("target") != str(target.resolve()):
        raise ValueError("lesson receipt run or workspace mismatch")
    harness = receipt.get("harness_session")
    if harness is not None and (
        not isinstance(harness, dict)
        or harness.get("harness") != "claude"
        or not isinstance(harness.get("fingerprint"), str)
        or harness["fingerprint"] != observed.get("harness_session_fingerprint")
    ):
        raise ValueError("lesson receipt harness session mismatch")
    producer_session = receipt.get("producer_work_session_id")
    if (
        not isinstance(producer_session, str)
        or not producer_session
        or producer_session != observed.get("work_session_id")
    ):
        raise ValueError("lesson receipt producer must match the reviewed work session")
    # Inspect the patch with descriptor-bound I/O before the shared projector.
    patch = target / relative.replace("receipt.json", "changes.patch")
    binding = receipt.get("subject_binding")
    if not isinstance(binding, dict):
        raise ValueError("lesson receipt requires subject binding")
    with tempfile.TemporaryDirectory(prefix="brigade-lesson-evidence-") as staging:
        staged_patch = Path(staging) / "changes.patch"
        if binding.get("binding_mode") == "patch_backed":
            if binding.get("patch_binding", {}).get("subject_path") != f"memory/cards/{card}":
                raise ValueError("lesson receipt subject path mismatch")
            data = io.read_bytes(patch, limit=4 * 1024 * 1024)
            if data is None:
                raise ValueError("lesson receipt patch is missing")
            io.write(staged_patch, data, exclusive=True)
        projection = verify_trial.project_trial(
            receipt, target=target, patch_path=staged_patch, require_live_subject=content is not None
        )
    if not projection.eligible:
        raise ValueError("lesson receipt is not eligible: " + projection.reason)
    if binding.get("artifact_kind") != "card" or binding.get("artifact_id") != Path(card).stem:
        raise ValueError("lesson receipt subject mismatch")
    content_binding = (
        binding.get("patch_binding", {}).get("subject_hash")
        if binding.get("binding_mode") == "patch_backed"
        else binding.get("content_fingerprint")
    )
    if content is not None and content_binding != "sha256:" + sha256_hex(content.encode()):
        raise ValueError("lesson receipt content fingerprint mismatch")
    verifier = binding.get("verifier_identity", {})
    producer = binding.get("producer_binding", {})
    identity = verifier.get("session_id") if isinstance(verifier, dict) else None
    sessions = {e.get("session_id") for e in events if e.get("session_id")}
    if not isinstance(verifier, dict) or not isinstance(producer, dict):
        raise ValueError("invalid lesson verifier identity")
    producer_id = producer.get("work_session_id")
    if producer_id is not None and producer_id != producer_session:
        raise ValueError("lesson receipt producer binding mismatch")
    if (
        not isinstance(identity, str)
        or not identity
        or not sessions
        or sha256_hex(identity.encode()) in sessions
        or identity == producer_session
    ):
        raise ValueError("lesson verifier identity must differ from the work session")
    try:
        completed = datetime.fromisoformat(receipt["completed_at"].replace("Z", "+00:00"))
        if completed.tzinfo is None or completed.timestamp() < earliest:
            raise ValueError("stale lesson receipt")
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError("invalid lesson receipt completion time") from exc
    receipt["_completed"] = completed.timestamp()
    receipt["_observation_session"] = observed.get("session_id")
    return receipt


def passing_receipt(receipt: dict[str, Any]) -> bool:
    commands = receipt.get("commands", [])
    return (
        receipt.get("status") == "completed"
        and bool(commands)
        and all(
            c.get("status") == "completed" and type(c.get("exit_code")) is int and c["exit_code"] == 0 for c in commands
        )
    )


def admit(target: Path, candidate: Any, events: list[dict[str, Any]], reads: dict[str, str]) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise ValueError("lesson must be an object")
    kind = candidate.get("kind")
    if kind not in {"verified-fix", "worked-workaround", "user-correction"}:
        raise ValueError("lesson kind is not admissible")
    if (
        any(candidate.get(key) is True for key in ("transient", "infrastructure", "tool_broken"))
        or candidate.get("resolved") is False
    ):
        raise ValueError("lesson contains an excluded claim")
    for key in ("title", "summary", "content", "quote", "target_card"):
        value = candidate.get(key)
        if not isinstance(value, str) or not value.strip() or len(value.encode()) > 8192:
            raise ValueError(f"invalid lesson {key}")
    text = "\n".join(candidate[key] for key in ("title", "summary", "content", "quote"))
    activity.scanned(text)
    if FORBIDDEN.search(text):
        raise ValueError("lesson contains an excluded claim")
    event = next((e for e in events if e["id"] == candidate.get("event")), None)
    if event is None or candidate["quote"] not in event["text"]:
        raise ValueError("lesson resolution must quote this session digest")
    quote = candidate["quote"]
    words = re.findall(r"\w+", quote.casefold())
    if len(quote.encode()) < 24 or len(words) < 5:
        raise ValueError("lesson quote must have at least 24 bytes and five tokens")
    content_words = set(re.findall(r"\w+", candidate["content"].casefold()))
    if len(set(words) & content_words) < max(5, len(set(words)) * 0.8):
        raise ValueError("lesson content must derive from its resolution quote")
    if kind == "user-correction":
        if event["kind"] != "UserPromptSubmit":
            raise ValueError("corrections must quote a user prompt")
    else:
        if event["kind"] != "PostToolUse":
            raise ValueError("fixes require a successful tool observation")
        reference = candidate.get("evidence")
        failure_reference = candidate.get("failure_evidence")
        if not isinstance(reference, str) or not isinstance(failure_reference, str):
            raise ValueError("lesson requires failing and passing verification evidence")
        timestamps = [e.get("earliest_unreviewed_at", e.get("at")) for e in events]
        if not timestamps or any(type(at) not in (int, float) for at in timestamps):
            raise ValueError("lesson evidence requires timestamped session observations")
        earliest = min(float(at) for at in timestamps if isinstance(at, (int, float)))
        passed = verified_receipt(
            target,
            reference,
            content=candidate["content"],
            card=candidate["target_card"],
            events=events,
            earliest=earliest,
        )
        failed = verified_receipt(
            target, failure_reference, content=None, card=candidate["target_card"], events=events, earliest=earliest
        )
        if (
            not passing_receipt(passed)
            or failed.get("status") != "failed"
            or not any(
                c.get("failure_class") == "verification" and c.get("exit_code") not in (0, None)
                for c in failed.get("commands", [])
            )
            or failed["_completed"] >= passed["_completed"]
            or not isinstance(failed.get("tree_fingerprint"), str)
            or not failed["tree_fingerprint"]
            or not isinstance(passed.get("tree_fingerprint"), str)
            or not passed["tree_fingerprint"]
            or failed["tree_fingerprint"] == passed["tree_fingerprint"]
            or failed.get("producer_work_session_id") != passed.get("producer_work_session_id")
            or failed.get("_observation_session") != passed.get("_observation_session")
            or failed.get("verify_manifest_id") != passed.get("verify_manifest_id")
        ):
            raise ValueError("lesson requires a verified fix with a changed tree, not a cleared transient failure")
    card = candidate["target_card"]
    if not safe_card_name(card):
        raise ValueError("unsafe lesson card")
    action = candidate.get("action")
    current = read_card(target, card)
    if action == "update-card":
        if card not in reads or current is None or sha256_hex(current) != reads[card]:
            raise ValueError("lesson update requires an unchanged card read in this run")
    elif action != "create-card" or current is not None:
        raise ValueError("lesson must create a new card or update a read card")
    return {
        key: candidate[key] for key in ("kind", "title", "summary", "content", "target_card", "action", "event")
    } | {"evidence": candidate.get("evidence") if kind != "user-correction" else None, "expected_sha": reads.get(card)}
