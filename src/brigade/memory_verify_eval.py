"""Bounded, offline evaluation of the production two-seat verification contract.

Labels are trusted local evaluation inputs. They never enter model prompts.
Evidence provenance is supplied by the corpus, not inferred from model output.
No hub client, workspace evidence reader or submission path is used here.
"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any, Callable

from . import memory_proposal_io as io
from . import memory_verify_contract as vc
from . import memory_verify_evidence as ev

CORPUS_SCHEMA = "brigade.memory-verify-corpus.v1"
CLASSES = ("confirm", "correct", "history", "supersede", "no-change")
OP_CLASS = {
    "append_evidence": "confirm",
    "replace_block": "correct",
    "add_previously": "history",
    "supersede_card": "supersede",
}
MAX_CASES = 256


def read_corpus(path: Path) -> list[dict[str, Any]]:
    if path.is_dir():
        path = path / "cases.json"
    raw = io.read_bytes(path, limit=io.MAX_STATE_BYTES)
    value = json.loads(raw or b"null")
    if not isinstance(value, dict) or value.get("schema") != CORPUS_SCHEMA:
        raise ValueError("verify_eval_corpus_invalid")
    cases = value.get("cases")
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise ValueError("verify_eval_case_limit")
    seen = set()
    for case in cases:
        inputs(case)
        ident = case["id"]
        if not isinstance(ident, str) or not ident or ident in seen:
            raise ValueError("verify_eval_case_identity")
        seen.add(ident)
    return cases


def inputs(case: dict[str, Any]) -> tuple[vc.CardInput, ev.Assembly]:
    if case.get("expected") not in CLASSES or not isinstance(case.get("expected_ops"), list):
        raise ValueError("verify_eval_label_invalid")
    for op in case["expected_ops"]:
        if not isinstance(op, dict) or OP_CLASS.get(op.get("type", "")) != case["expected"]:
            raise ValueError("verify_eval_label_invalid")
    if case["expected"] in ("correct", "history", "supersede") and not case["expected_ops"]:
        raise ValueError("verify_eval_label_missing_ops")
    card = vc.CardInput.from_text(**case["card"])
    assembly = ev.Assembly(card_id=card.card_id, scope=card.scope)
    evidence = case["evidence"]
    if not isinstance(evidence, list) or len(evidence) > 64:
        raise ValueError("verify_eval_evidence_limit")
    for value in evidence:
        if len(value.get("compare_base64", "")) > 4 * ev.READ_LIMIT:
            raise ValueError("verify_eval_comparison_limit")
        item = ev.Item(
            **{
                **{k: v for k, v in value.items() if k != "compare_base64"},
                "lineage": frozenset(value.get("lineage", [])),
                "values": tuple(value.get("values", [])),
                "compare": base64.b64decode(value["compare_base64"], validate=True)
                if "compare_base64" in value
                else value["text"].encode(),
            }
        )
        if item.sha256 != io.sha256(item.text.encode()):
            raise ValueError("verify_eval_evidence_digest")
        rules = ev._scan(item.text, item.text, item.values)
        if rules is None:
            assembly.items.append(item)
        else:
            assembly.flagged.append(ev.Flag("cited", len(assembly.items), item.kind, rules))
    if not assembly.items:
        assembly.abstain = ev.NO_EVIDENCE
    return card, assembly


def predicted(result: vc.CardResult) -> str:
    if result.proposal:
        classes = {OP_CLASS.get(op.type, "no-change") for op in result.proposal.ops}
        return next(iter(classes)) if len(classes) == 1 else "no-change"
    if (
        result.abstain is None
        and result.outcomes
        and all(outcome.verdict == "supported" and outcome.reason in (None, vc.VERIFIED) for outcome in result.outcomes)
    ):
        return "confirm"
    return "no-change"


def evaluate(
    cases: list[dict[str, Any]],
    *,
    propose: vc.Seat,
    confirm: vc.Seat,
    repeats: int = 3,
    resolve_commit: vc.Resolver | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    if not 1 <= len(cases) <= MAX_CASES or not 1 <= repeats <= 3:
        raise ValueError("verify_eval_limit")
    prepared = [(case, *inputs(case)) for case in cases]
    counts = {name: dict(tp=0, fp=0, fn=0, support=0) for name in CLASSES}
    op_counts = {name: dict(tp=0, fp=0, fn=0, support=0) for name in OP_CLASS}
    observed: dict[str, list[str]] = {}
    unsafe = invalid = calls = 0
    deadline = clock() + 20 * 60
    for _ in range(repeats):
        for case, card, assembly in prepared:
            if clock() >= deadline:
                raise ValueError("verify_eval_timeout")
            result = vc.verify_card(
                card,
                assembly,
                propose=propose,
                confirm=confirm,
                resolve_commit=resolve_commit or case.get("commits", {}).get,
            )
            label, guess = case["expected"], predicted(result)
            ops = result.proposal.payload_ops() if result.proposal else []
            matches = (
                label == guess
                and len(ops) == len(case["expected_ops"])
                and all(
                    all(actual.get(key) == value for key, value in wanted.items())
                    for actual, wanted in zip(ops, case["expected_ops"], strict=True)
                )
            )
            remaining = list(ops)
            for wanted in case["expected_ops"]:
                bucket = op_counts[wanted["type"]]
                bucket["support"] += 1
                matched = next(
                    (
                        i
                        for i, actual in enumerate(remaining)
                        if all(actual.get(key) == value for key, value in wanted.items())
                    ),
                    None,
                )
                if matched is None:
                    bucket["fn"] += 1
                else:
                    bucket["tp"] += 1
                    remaining.pop(matched)
            for actual in remaining:
                op_counts[actual["type"]]["fp"] += 1
            counts[label]["support"] += 1
            if matches:
                counts[label]["tp"] += 1
            else:
                counts[label]["fn"] += 1
                counts[guess]["fp"] += 1
            if ops and label in ("confirm", "no-change") and not matches:
                unsafe += 1
            if result.abstain in (vc.SEAT_FAILED, vc.OUTPUT_INVALID) or any(
                o.reason in (vc.UNCONFIRMED, vc.OUTPUT_INVALID) for o in result.outcomes
            ):
                invalid += 1
            observed.setdefault(case["id"], []).append(io.sha256(io.json_bytes([guess, ops])))
            calls += result.calls

    def metrics(table: dict[str, dict[str, int]]) -> dict[str, Any]:
        return {
            name: {
                **row,
                "precision": row["tp"] / (row["tp"] + row["fp"]) if row["tp"] + row["fp"] else 0.0,
                "recall": row["tp"] / (row["tp"] + row["fn"]) if row["tp"] + row["fn"] else 0.0,
            }
            for name, row in table.items()
        }

    return {
        "schema": "brigade.memory-verify-eval.v1",
        "status": "completed",
        "cases": len(cases),
        "repeats": repeats,
        "classes": metrics(counts),
        "ops": metrics(op_counts),
        "calls": calls,
        "invalid": invalid,
        "unsafe_proposals": unsafe,
        "agreement": sum(len(set(rows)) == 1 for rows in observed.values()) / len(observed),
    }
