"""Translate only deterministic slice-two candidates into reviewed typed deltas."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import memory_proposals as legacy
from . import memory_proposal_v2 as v2
from . import memory_proposal_v2_ops as delta
from . import memory_reflect


@legacy._boundary(2)
def propose_payload(*, target: Path, candidate_id: str, reason: str) -> dict[str, Any]:
    target = legacy._target(target)
    legacy._reason(reason)
    candidate = memory_reflect.inspect_payload(target=target, command="show", candidate_id=candidate_id)
    run_id = None
    for run in memory_reflect._runs(target)[0]:
        try:
            if any(c == candidate for c in memory_reflect._candidates(target, run)):
                run_id = run["run_id"]
                break
        except (OSError, ValueError):
            continue
    if run_id is None:
        raise legacy.ProposalError("candidate run not found")
    names = list(candidate["card_paths"].values())
    care, _ = legacy._configuration(target)
    sources, _ = v2._sources(target, care, names)
    for source in sources:
        if candidate["base_sha256"].get(source["id"]) != source["sha256"]:
            raise legacy.ProposalError("candidate base hash drift")
    if candidate["contrary_ids"] or candidate["review_evidence_ids"] or candidate["unresolved_evidence_ids"]:
        raise legacy.ProposalError("no valid deterministic ops for unresolved or contrary evidence")
    if candidate["kind"] not in ("stale-newer-evidence", "reinforce", "duplicate"):
        raise legacy.ProposalError("no valid deterministic ops for candidate kind")
    ops = []
    translated = sources[:1] if candidate["kind"] == "duplicate" else sources
    for source in translated:
        anchored = delta.anchor_text(source["text"], source["path"])
        addressable = delta.blocks(anchored)
        if not addressable:
            continue
        block = addressable[0]
        base = dict(card_path=source["path"], block_id=block.block_id, expected_block_hash=block.sha256)
        refs = candidate["evidence_ids"]
        if candidate["kind"] == "stale-newer-evidence" or set(refs) - set(source["metadata"].get("evidence", [])):
            append = dict(type="append_evidence", **base, evidence_ids=refs)
            if candidate["kind"] == "stale-newer-evidence":
                stamp = v2._consolidated_at(target, source, refs)
                if stamp:
                    append["last_consolidated"] = stamp
            ops.append(append)
        if candidate["kind"] == "duplicate" and source == sources[0]:
            ops.extend(dict(type="merge_exact_duplicates", **base, other_card=other["path"]) for other in sources[1:])
    if not ops:
        raise legacy.ProposalError("no valid delta ops remain")
    # The candidate digest and all source bases were checked before proposing.
    # Reviewed anchoring and evidence deltas share one exact preview and decision.
    return v2.create_payload(
        target=target,
        source_paths=names,
        ops=ops,
        anchor_blocks=True,
        reason=reason,
        reflect={"run_id": run_id, "candidate_id": candidate_id},
        expected_source_sha256={source["path"]: candidate["base_sha256"][source["id"]] for source in sources},
        # A duplicate candidate must merge; a proposal that drops the merge would leave it open.
        strict_ops=candidate["kind"] == "duplicate",
    )
