"""Text-free proposal decisions for the verify pass. No decision applies an op."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import memory_fairness as fairness
from . import memory_proposal_io as io

VERDICTS = frozenset({"contradicted", "changed", "history", "superseded"})


def valid_record(value: Any) -> bool:
    """Strict metadata only, safe to persist in the verify state."""
    return (
        isinstance(value, dict)
        and set(value) == {"revision", "evidence", "verdicts"}
        and type(value["revision"]) is int
        and value["revision"] >= 0
        and isinstance(value["evidence"], str)
        and len(value["evidence"]) == 64
        and all(c in "0123456789abcdef" for c in value["evidence"])
        and isinstance(value["verdicts"], list)
        and 0 < len(value["verdicts"]) <= len(VERDICTS)
        and all(isinstance(v, str) and v in VERDICTS for v in value["verdicts"])
        and len(set(value["verdicts"])) == len(value["verdicts"])
    )


def valid_counts(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and set(value) <= VERDICTS
        and all(
            isinstance(counts, dict)
            and set(counts) == {"accepted", "rejected"}
            and all(type(n) is int and n >= 0 for n in counts.values())
            for counts in value.values()
        )
    )


def record(manifest: bytes, packet: bytes) -> dict[str, Any] | None:
    """Metadata of the corrected blocks, derived from the exact staged proposal."""
    bound, shown = json.loads(manifest), json.loads(packet)
    blocks = {op["block_id"] for op in shown["ops"]}
    value = {
        "revision": bound["card"]["base_revision"],
        "evidence": shown.get("evidence_sha256"),
        "verdicts": sorted({b["verdict"] for b in bound["blocks"] if b["block_id"] in blocks} & VERDICTS),
    }
    return value if valid_record(value) else None


def decide(state: dict[str, Any], entry: dict[str, Any]) -> None:
    """Fold a refreshed decision into state with its pending removal in the same save."""
    decision = entry.pop("decision", None)
    review = entry.pop("review", None)
    if decision not in ("applied", "rejected") or review is None:
        return
    label = "accepted" if decision == "applied" else "rejected"
    counts = state.setdefault("decisions", {})
    for verdict in review["verdicts"]:
        counts.setdefault(verdict, {"accepted": 0, "rejected": 0})[label] += 1
    if decision == "rejected":
        entry["rejected"] = review
        entry["verdict"] = "rejected_" + (review["verdicts"][0] if len(review["verdicts"]) == 1 else "correction")


def unchanged(entry: dict[str, Any], revision: int, digest: str | None) -> bool:
    rejected = entry.get("rejected")
    return rejected is not None and rejected["revision"] == revision and rejected["evidence"] == digest


def legacy_record(target: Path, proposal_id: str, scope: str, key: str, entry: dict[str, Any]) -> dict[str, Any] | None:
    """Recover old pending metadata from its private packet, never emitting its text.

    Earlier runs kept revision and evidence on the card entry. No hash read
    is needed for local feedback, which cannot apply a correction or reach a
    connector. Review display still requires the complete authenticated chain.
    """
    from . import memory_verify_packets as packets

    try:
        data = io.read_bytes(target / packets.PACKETS / f"{proposal_id}.json", limit=packets.PACKET_CAP, private=True)
        if data is None:
            return None
        shown = json.loads(data)
        card = shown["card"]
        if (
            shown["schema"] != "brigade.memory-verify-packet.v1"
            or card["scope"] != scope
            or fairness.key_digest(scope, [card["card_id"]]) != key
            or card["base_revision"] != entry.get("revision")
        ):
            return None
        shown["evidence_sha256"] = entry.get("evidence")
        bound = {"card": card, "blocks": shown["outcomes"]}
        return record(io.json_bytes(bound), io.json_bytes(shown))
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return None


MAX_CASES = 128
# Readers load the feedback file through io.read_bytes, which refuses files over
# MAX_INPUT_BYTES, so the written file must stay under that size.
MAX_FEEDBACK_BYTES = io.MAX_INPUT_BYTES


def bounded_feedback(values: list[dict[str, Any]], limit: int) -> bytes | None:
    """Serialize feedback, evicting the oldest cases until it fits in ``limit`` bytes."""
    values = list(values)
    while values:
        counts: dict[str, dict[str, int]] = {}
        for row in values:
            bucket = counts.setdefault(row["source_class"], {"accepted": 0, "rejected": 0})
            bucket["accepted" if row["decision"] == "applied" else "rejected"] += 1
        payload = io.json_bytes({"cases": values, "counts": counts})
        if len(payload) <= limit:
            return payload
        values.pop(0)
    return None


def retain_case(target: Path, proposal_id: str, decision: str, shown: dict[str, Any]) -> None:
    """Keep bounded private labels from authenticated packets, excluding self-decisions.

    Legacy packets lack replayable inputs and supply counts only. The caller
    excludes hub-recorded automatic decisions, preventing automatic corrections
    from grading themselves.
    """
    from . import memory_verify_contract as contract
    from . import memory_verify_eval as evaluation, memory_verify_gate as gate
    from . import memory_verify_packets as packets, memory_verify_show as show

    try:
        manifest, raw = show._local(target, proposal_id)
        listed = shown.get("verifier_manifests", [])
        if not any(m.get("artifact_sha256") == io.sha256(manifest) for m in listed):
            return
        if json.loads(manifest)["packet_sha256"] != io.sha256(raw):
            return
        packet = json.loads(raw)
        case = packet.get("evaluation_case")
        if not case:
            return
        classes = {evaluation.OP_CLASS[op["type"]] for op in case["expected_ops"]}
        if len(classes) != 1:
            return
        source_class = next(iter(classes))
        case.update(
            id=proposal_id,
            expected=source_class if decision == "applied" else "no-change",
            source_class=source_class,
            decision=decision,
        )
        if decision == "rejected":
            case["expected_ops"] = []
        evaluation.inputs(case)
        old = io.read_bytes(target / gate.FEEDBACK, absent=True, private=True)
        values = json.loads(old)["cases"] if old else []
        values = [v for v in values if v["id"] != proposal_id] + [case]
        payload = bounded_feedback(values[-MAX_CASES:], MAX_FEEDBACK_BYTES)
        if payload is None:
            return  # One case alone exceeds what every feedback reader accepts.
        with io.directory((target / gate.FEEDBACK).parent, create=True, private=True) as fd:
            packets.atomic_write(fd, gate.FEEDBACK.name, payload)
    except (OSError, ValueError, KeyError, TypeError, contract.Abstain):
        # Packet retention, legacy data or a case that cannot be replayed on its
        # own makes a label unavailable. Never let it stop the verification run.
        return
