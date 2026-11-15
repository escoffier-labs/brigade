"""Apply admitted verify corrections through the hub's transactional decision API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Sequence

from . import memory_hub_client as hubc
from . import memory_proposal_io as io
from . import memory_verify_eval as evaluation
from . import memory_verify_gate as gate
from . import memory_verify_seat as seats


JOURNAL = gate.ROOT / "applications.json"


def automated(reply: dict[str, Any]) -> bool:
    """Feedback classification comes only from the hub's immutable decision."""
    return reply.get("state") == "applied" and reply.get("decision_mode") == "automatic"


def apply_pending(
    target: Path, state: dict[str, Any], client: hubc.MemoryHubClient, *, pair: Sequence[seats.Seat], now_ms: int
) -> list[dict[str, Any]]:
    from . import memory_verify_show as show
    from .memory_verify_cmd import _binding
    from .memory_verify_run import MAX_NEW_PROPOSALS

    admitted = gate.allowed(target, pair=pair, binding=state["binding"], now_ms=now_ms, client=client)
    results: list[dict[str, Any]] = []
    if not admitted:
        return results
    source_status = client.call("status")
    if _binding(client.identity, source_status.get("principal"), source_status.get("log_epoch")) != state["binding"]:
        return results
    for scope in state["scopes"].values():
        for entry in scope.get("cards", {}).values():
            ident = entry.get("pending")
            if not ident or len(results) >= MAX_NEW_PROPOSALS:
                continue
            try:
                # Authenticate the packet and re-resolve its evidence before deciding.
                checked = show.show_payload(target, ident)
                if checked["findings"] or any(
                    item["file_status"] in ("changed", "missing") for item in checked["evidence"]
                ):
                    continue
                _, packet = show._local(target, ident)
                ops = json.loads(packet)["ops"]
                classes = {evaluation.OP_CLASS.get(op["type"]) for op in ops}
                if not ops or None in classes or not classes <= admitted:
                    continue
                shown = client.call("proposal_show", proposal_id=ident)
                if shown.get("state") != "pending" or "policy_digest" not in shown:
                    continue
                # Workspace records are diagnostics. Only hub admission can authorize this action.
                _, policy_digest = gate.policy(target)
                reply = client.call(
                    "verify_auto_apply",
                    proposal_id=ident,
                    # _propose appends exactly one manifest binding to the packet's corrections.
                    # The hub authenticates that binding and admits correction classes separately.
                    expected_op_types=[op["type"] for op in ops] + ["append_evidence"],
                    binding_sha256=gate.binding_digest(target, pair, state["binding"]),
                    policy_sha256=policy_digest,
                    base_epoch=source_status["log_epoch"],
                    client_event_id=hubc.event_id(),
                )
                results.append(
                    {
                        "proposal_id": ident,
                        "state": reply.get("state", "unknown"),
                        "decision_mode": reply.get("decision_mode"),
                    }
                )
                if automated(reply):
                    try:
                        # A failed call never marks a proposal automatic. This cache is audit-only.
                        from .memory_verify_packets import atomic_write

                        raw = io.read_bytes(target / JOURNAL, absent=True, private=True)
                        journal = json.loads(raw) if raw else {}
                        journal = dict(list(journal.items())[-4095:])
                        journal[ident] = {"created_ms": now_ms, "decision_mode": reply["decision_mode"]}
                        with io.directory(target / gate.ROOT, create=True, private=True) as fd:
                            atomic_write(fd, JOURNAL.name, io.json_bytes(journal))
                    except (OSError, ValueError, TypeError):
                        results[-1]["journal"] = "unavailable"
            except (hubc.HubRefusal, hubc.HubUnavailable, OSError, ValueError, KeyError, TypeError):
                # A missing grant, stale revision or changed policy leaves the proposal intact.
                results.append({"proposal_id": ident, "state": "proposal-only"})
    return results
