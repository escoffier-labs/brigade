"""Digest-bound, unattended per-class go/no-go records. No record means no apply."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Sequence

from . import memory_proposal_io as io
from . import memory_hub_client as hubc
from . import memory_verify_contract as vc
from . import memory_verify_eval as evaluation
from . import memory_verify_seat as seats

ROOT = Path(".brigade/memory/verify")
POLICY = ROOT / "policy.json"
CORPUS = ROOT / "eval/cases.json"
FEEDBACK = ROOT / "eval/feedback.json"
RECORD = ROOT / "decision.json"
DEFAULT_POLICY = json.loads((Path(__file__).parent / "fixtures/memory-verify-policy.v1.json").read_bytes())
DAY_MS = 86_400_000


def policy_bytes(target: Path) -> bytes:
    raw = io.read_bytes(target / POLICY, absent=True)
    return raw if raw is not None else io.json_bytes(DEFAULT_POLICY)


def policy(target: Path) -> tuple[dict[str, Any], str]:
    raw = policy_bytes(target)
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != set(DEFAULT_POLICY) or value["schema"] != DEFAULT_POLICY["schema"]:
        raise ValueError("verify_eval_policy_invalid")
    for key in ("precision", "recall", "agreement", "feedback_accept_rate"):
        if type(value[key]) not in (float, int) or not 0 <= value[key] <= 1:
            raise ValueError("verify_eval_policy_invalid")
    for key, low, high in (
        ("min_cases", 1, 128),
        ("min_feedback", 0, 128),
        ("repeats", 1, 3),
        ("consecutive_passes", 1, 10),
        ("max_age_days", 1, 30),
    ):
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError("verify_eval_policy_invalid")
    if type(value["auto_apply"]) is not bool:
        raise ValueError("verify_eval_policy_invalid")
    return value, io.sha256(raw)


def binding_digest(target: Path, pair: Sequence[seats.Seat], binding: str) -> str:
    _, digest = policy(target)
    blobs = [io.read_bytes(target / name, absent=True) for name in (CORPUS, FEEDBACK)]
    return io.sha256(
        io.json_bytes(
            {
                "policy": digest,
                "inputs": [io.sha256(b or b"") for b in blobs],
                "prompt": vc.PROMPT_SHA256,
                "hub": binding,
                "seats": [[s.name, s.model, s.fingerprint] for s in pair],
                "harness": "v1",
                "implementation": [
                    io.sha256(Path(__file__).with_name(name).read_bytes())
                    for name in sorted(p.name for p in Path(__file__).parent.glob("memory_verify_*.py"))
                    + ["memory_hub_client.py"]
                ],
            }
        )
    )


def record(
    target: Path,
    report: dict[str, Any],
    *,
    pair: Sequence[seats.Seat],
    binding: str,
    now_ms: int,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    settings, policy_digest = policy(target)
    bound = binding_digest(target, pair, binding)
    # Only a hub-authenticated previous admission may advance the streak.
    old = (previous or {}).get("record", {})
    previous = (
        old.get("classes", {})
        if old.get("binding_sha256") == bound
        and 0 <= now_ms - (previous or {}).get("created_ms", -DAY_MS * 31) <= settings["max_age_days"] * DAY_MS
        else {}
    )
    classes = {}
    for name in evaluation.CLASSES:
        row = report.get("classes", {}).get(name, {})
        reasons = []
        if report.get("status") != "completed" or report.get("invalid", 1):
            reasons.append("evaluation_invalid")
        if report.get("unsafe_proposals", 1):
            reasons.append("healthy_card_or_adversarial_proposal")
        if row.get("support", 0) < settings["min_cases"] * settings["repeats"]:
            reasons.append("insufficient_cases")
        op_type = next((op for op, label in evaluation.OP_CLASS.items() if label == name), None)
        if op_type is not None:
            op_row = report.get("ops", {}).get(op_type, {})
            if op_row.get("support", 0) < settings["min_cases"] * settings["repeats"]:
                reasons.append("insufficient_operation_cases")
            if any(op_row.get(metric, 0) < settings[metric] for metric in ("precision", "recall")):
                reasons.append("operation_metrics")
        for metric in ("precision", "recall"):
            if row.get(metric, 0) < settings[metric]:
                reasons.append(metric)
        if report.get("agreement", 0) < settings["agreement"]:
            reasons.append("agreement")
        counts = report.get("feedback", {}).get(name, {})
        accepted, rejected = counts.get("accepted", 0), counts.get("rejected", 0)
        if accepted + rejected < settings["min_feedback"]:
            reasons.append("insufficient_feedback")
        if accepted + rejected and accepted / (accepted + rejected) < settings["feedback_accept_rate"]:
            reasons.append("feedback_accept_rate")
        streak = previous.get(name, {}).get("streak", 0) + 1 if not reasons else 0
        if streak < settings["consecutive_passes"] and not reasons:
            reasons.append("consecutive_passes")
        classes[name] = {"decision": "no-go" if reasons else "go", "reasons": reasons, "streak": streak}
    value = {
        "schema": "brigade.memory-verify-decision.v1",
        "binding_sha256": bound,
        "policy_sha256": policy_digest,
        "created_ms": now_ms,
        "classes": classes,
        "report_sha256": io.sha256(io.json_bytes(report)),
        "report": report,
    }
    value["digest"] = io.sha256(io.json_bytes(value))
    # Atomic replacement publishes either the complete old record or the complete new one.
    from .memory_verify_packets import atomic_write

    with io.directory(target / ROOT, create=True, private=True) as fd:
        atomic_write(fd, RECORD.name, io.json_bytes(value))
    return value


def allowed(
    target: Path, *, pair: Sequence[seats.Seat], binding: str, now_ms: int, client: hubc.MemoryHubClient | None = None
) -> set[str]:
    try:
        settings, policy_digest = policy(target)
        if client is None:
            return set()
        admission = client.call("status").get("verify_evaluation") or {}
        value = admission.get("record", {})
        if (
            not settings["auto_apply"]
            or value.get("schema") != "brigade.memory-verify-decision.v1"
            or admission.get("evaluator") != "admin"
            or value.get("policy_sha256") != policy_digest
            or value.get("binding_sha256") != binding_digest(target, pair, binding)
            or not 0 <= now_ms - admission["created_ms"] <= settings["max_age_days"] * DAY_MS
        ):
            return set()
        return {name for name in evaluation.CLASSES if value["classes"][name]["decision"] == "go"}
    except (OSError, ValueError, KeyError, TypeError, hubc.HubUnavailable, hubc.HubRefusal):
        return set()


def run(
    target: Path, *, pair: Sequence[seats.Seat], binding: str, now_ms: int, previous: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Evaluate with isolated seats, then persist a decision even when evaluation fails."""
    from . import memory_verify_run as runner

    settings, _ = policy(target)
    start = binding_digest(target, pair, binding)
    try:
        cases = evaluation.read_corpus(target / CORPUS)
        feedback = io.read_bytes(target / FEEDBACK, absent=True, private=True)
        labeled = json.loads(feedback) if feedback else {"cases": [], "counts": {}}
        cases.extend(labeled["cases"])
        with runner.WORK_AREA(target) as area:
            calls = runner.Calls(area)
            report = evaluation.evaluate(
                cases, propose=calls.seat(pair[0]), confirm=calls.seat(pair[1]), repeats=settings["repeats"]
            )
            if calls.failed() is not None:
                report["invalid"] += 1
        report["feedback"] = labeled["counts"]
        if start != binding_digest(target, pair, binding):
            raise ValueError("verify_eval_inputs_changed")
    except (OSError, ValueError, KeyError, TypeError, AttributeError, RecursionError, runner.SeatStop):
        report = {"status": "failed", "code": "verify_eval_unavailable"}
    return record(target, report, pair=pair, binding=binding, now_ms=now_ms, previous=previous)


def evaluator(target: Path, verifier: hubc.MemoryHubClient) -> hubc.MemoryHubClient:
    """Only the explicit gate command loads the separate admin evaluator credential."""
    from . import fleet_home_guard

    settings = hubc._user_config(fleet_home_guard.fleet_config_path(strict=True))
    raw = os.environ.get("BRIGADE_MEMORY_VERIFY_EVAL_TOKEN_FILE") or settings.get("memory_verify", {}).get(
        "eval_token_file"
    )
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise ValueError("verify_eval_admin_credential_required")
    return hubc.client_for(hubc.load_settings(target, credential_pair=(verifier.hub_url, Path(raw))))
