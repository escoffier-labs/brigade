"""Apply admitted verify corrections through the hub's transactional decision API."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Sequence

from . import memory_hub_client as hubc
from . import memory_proposal_io as io
from . import memory_verify_eval as evaluation
from . import memory_verify_gate as gate
from . import memory_verify_seat as seats


JOURNAL = gate.ROOT / "applications.json"


def _reviewer(target: Path, verifier: hubc.MemoryHubClient) -> hubc.MemoryHubClient | None:
    from . import fleet_home_guard

    config = hubc._user_config(fleet_home_guard.fleet_config_path(strict=True))
    table = config.get("memory_verify", {})
    if not isinstance(table, dict):
        raise ValueError("verify_review_credential_invalid")
    raw = os.environ.get("BRIGADE_MEMORY_VERIFY_REVIEW_TOKEN_FILE") or table.get("review_token_file")
    if raw is None:
        return None
    if not isinstance(raw, str) or not Path(raw).is_absolute():
        raise ValueError("verify_review_credential_invalid")
    settings = hubc.load_settings(target, credential_pair=(verifier.hub_url, Path(raw)))
    return hubc.client_for(settings)


def automated(target: Path, ident: str) -> bool:
    raw = io.read_bytes(target / JOURNAL, absent=True, private=True)
    return bool(raw and ident in json.loads(raw))


def apply_pending(
    target: Path, state: dict[str, Any], client: hubc.MemoryHubClient, *, pair: Sequence[seats.Seat], now_ms: int
) -> list[dict[str, Any]]:
    from . import memory_verify_show as show
    from .memory_verify_cmd import _binding
    from .memory_verify_run import MAX_NEW_PROPOSALS

    admitted = gate.allowed(target, pair=pair, binding=state["binding"], now_ms=now_ms)
    results: list[dict[str, Any]] = []
    if not admitted:
        return results
    reviewer = _reviewer(target, client)
    if reviewer is None:
        return results
    source_status, review_status = client.call("status"), reviewer.call("status")
    hubc.refuse_admin(review_status)
    if (
        _binding(client.identity, source_status.get("principal"), source_status.get("log_epoch")) != state["binding"]
        or not review_status.get("principal")
        or review_status["principal"] == source_status.get("principal")
        or review_status.get("log_epoch") != source_status.get("log_epoch")
        or reviewer.identity != client.identity
    ):
        return results
    raw = io.read_bytes(target / JOURNAL, absent=True, private=True)
    journal = json.loads(raw) if raw else {}
    journal = {
        key: value for key, value in journal.items() if now_ms - value.get("created_ms", now_ms) <= 35 * gate.DAY_MS
    }
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
                shown = reviewer.call("proposal_show", proposal_id=ident)
                if shown.get("state") != "pending" or "policy_digest" not in shown:
                    continue
                # Re-read authority after the potentially slow evidence checks.
                if not classes <= gate.allowed(target, pair=pair, binding=state["binding"], now_ms=now_ms):
                    continue
                from .memory_verify_packets import atomic_write

                if ident not in journal and len(journal) >= 4096:
                    continue  # Retain replay ids for the hub proposal retention window.
                event = journal.setdefault(
                    ident,
                    {
                        "client_event_id": hubc.event_id(),
                        "created_ms": now_ms,
                        "decision_sha256": gate._read(target)["digest"],
                    },
                )
                with io.directory(target / gate.ROOT, create=True, private=True) as fd:
                    atomic_write(fd, JOURNAL.name, io.json_bytes(journal))
                reply = reviewer.call(
                    "decide",
                    proposal_id=ident,
                    decision="accept",
                    expected_policy_digest=shown["policy_digest"],
                    base_epoch=source_status["log_epoch"],
                    client_event_id=event["client_event_id"],
                )
                results.append({"proposal_id": ident, "state": reply.get("state", "unknown")})
            except (hubc.HubRefusal, hubc.HubUnavailable, OSError, ValueError, KeyError, TypeError):
                # A missing grant, stale revision or changed policy leaves the proposal intact.
                results.append({"proposal_id": ident, "state": "proposal-only"})
    return results
