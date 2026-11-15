"""Fail-closed lesson capture admission, independent of worker instructions."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from . import lesson_activity as activity, localio, memory_proposal_io as io
from .handoff_ingest_rules import safe_card_name, sha256_hex

FORBIDDEN = re.compile(
    r"\b(?:unresolved|dead[ -]end|transient|infrastructure failure)\b|"
    r"\b(?:tool|cli|command|adapter|provider)\b[^.\n]{0,60}\b(?:broken|unusable|does not work|doesn't work)\b|"
    r"\b(?:is|was|remains)\s+(?:broken|unusable)\b",
    re.I,
)


def read_card(target: Path, name: str) -> bytes | None:
    return io.read_bytes(target / "memory/cards" / name, limit=8192, absent=True)


def passing_receipt(target: Path, relative: str) -> dict[str, Any]:
    io.relative(relative)
    if not re.fullmatch(r"\.brigade/work/verify-runs/[A-Za-z0-9_-]+/receipt\.json", relative):
        raise ValueError("lesson requires a local verification receipt")
    receipt = json.loads(io.text(target / relative, limit=256 * 1024) or "")
    if not isinstance(receipt, dict):
        raise ValueError("invalid lesson evidence")
    commands = receipt.get("commands")
    if (
        receipt.get("status") != "completed"
        or not receipt.get("completed_at")
        or not isinstance(commands, list)
        or not commands
        or any(
            not isinstance(c, dict)
            or c.get("status") != "completed"
            or type(c.get("exit_code")) is not int
            or c["exit_code"] != 0
            for c in commands
        )
    ):
        raise ValueError("lesson evidence must be a completed passing verification")
    digests = receipt.get("digests")
    if not isinstance(digests, dict) or digests.get("receipt_sha256") != localio.canonical_json_digest(
        receipt, exclude_keys={"digests"}
    ):
        raise ValueError("lesson receipt digest mismatch")
    return receipt


def admit(target: Path, candidate: Any, events: list[dict[str, Any]], reads: dict[str, str]) -> dict[str, Any]:
    if not isinstance(candidate, dict):
        raise ValueError("lesson must be an object")
    kind = candidate.get("kind")
    if kind not in {"verified-fix", "worked-workaround", "user-correction"}:
        raise ValueError("lesson kind is not admissible")
    if (
        candidate.get("resolved") is not True
        or candidate.get("transient") is not False
        or candidate.get("infrastructure") is not False
        or candidate.get("tool_broken") is not False
    ):
        raise ValueError("lesson capture policy rejected failure claims")
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
    if kind == "user-correction":
        if event["kind"] != "UserPromptSubmit":
            raise ValueError("corrections must quote a user prompt")
    else:
        if event["kind"] != "PostToolUse":
            raise ValueError("fixes require a successful tool observation")
        reference = candidate.get("evidence")
        if not isinstance(reference, str):
            raise ValueError("lesson requires verification evidence")
        if reference not in event["text"]:
            raise ValueError("lesson evidence must be observed in this session")
        passing_receipt(target, reference)
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
        key: candidate[key]
        for key in ("kind", "title", "summary", "content", "target_card", "action", "quote", "event")
    } | {"evidence": candidate.get("evidence"), "expected_sha": reads.get(card)}
