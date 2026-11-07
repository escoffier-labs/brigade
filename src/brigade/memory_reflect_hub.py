"""Scheduled hub reflection opens proposals and never decides or applies them."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from . import memory_hub_client as hubc
from . import memory_hub_store as store
from . import memory_hub_sync as sync
from . import memory_proposal_io as io
from . import memory_proposal_v2_ops as delta
from . import memory_reflect_candidates as derive
from .inbox_lock import held_file_lock

STATE = ".brigade/memory/reflect/schedule.json"
FAILURES = 3


def schedule_status(target: Path) -> dict[str, Any]:
    try:
        state = io.read_object(target / STATE, private=True)
    except FileNotFoundError:
        return {"consecutive_failures": 0, "paused": False}
    count = state.get("consecutive_failures")
    if type(count) is not int or count < 0 or state.get("paused") != (count >= FAILURES):
        raise ValueError("invalid reflect schedule state")
    return state


def care_status_payload(target: Path, care: dict[str, Any]) -> dict[str, Any]:
    try:
        state = schedule_status(target)
        reflect = {k: state[k] for k in ("consecutive_failures", "paused")}
    except (OSError, ValueError):
        reflect = {"paused": True, "state_error": "reflect schedule state is unreadable"}
    return {**care, "reflect": reflect}


def _save(target: Path, state: dict[str, Any]) -> None:
    path = target / STATE
    temporary = path.with_name(f".{uuid.uuid4().hex}.tmp")
    io.write_exclusive(temporary, io.json_bytes(state))
    with io.directory(path.parent, private=True) as parent:
        try:
            os.replace(temporary.name, path.name, src_dir_fd=parent, dst_dir_fd=parent)
            os.fsync(parent)
        finally:
            try:
                os.unlink(temporary.name, dir_fd=parent)
            except FileNotFoundError:
                pass


def _client(target: Path) -> tuple[hubc.HubSettings, hubc.MemoryHubClient]:
    settings = hubc.load_settings(target)
    if not settings.enabled:
        raise ValueError("hub reflection requires an enabled [hub] configuration")
    return settings, hubc.client_for(settings)


def _ops(heads: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Only exact duplicate claims with existing block anchors can be translated.

    Preserve opaque bytes, including code indentation. The hub remains the
    authority for validation, classification and rights on both target cards.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    skipped = 0
    for head in sorted(heads, key=lambda h: (h["scope"], h["card_id"])):
        text = head["text"]
        if hubc.content_hash(text) != head["content_hash"]:
            raise ValueError("hub reflect snapshot hash mismatch")
        body = delta.claim_body(text)
        if body.strip():
            groups.setdefault((head["scope"], body), []).append(head)
    ops = []
    for group in groups.values():
        if len(group) < 2:
            continue
        keeper = group[0]
        try:
            blocks = delta.blocks(keeper["text"])
            block = next((b for b in blocks if b.block_id), None)
            if block is None:
                raise ValueError("duplicate keeper has no pinned block")
            for other in group[1:]:
                # Match the hub's strict JSON metadata lists before submission.
                for card in (keeper, other):
                    header = card["text"].split("\n---\n", 1)[0] if card["text"].startswith("---\n") else ""
                    for name in ("evidence", "aliases"):
                        values = [
                            line.partition(":")[2].strip()
                            for line in header.splitlines()
                            if line.startswith(name + ":")
                        ]
                        if len(values) > 1:
                            raise ValueError("ambiguous duplicate metadata")
                        if values:
                            refs = json.loads(values[0])
                            if (
                                not isinstance(refs, list)
                                or len(refs) > 256
                                or not all(isinstance(r, str) and len(r.encode()) <= 1024 for r in refs)
                            ):
                                raise ValueError("invalid duplicate metadata")
                ops.append(
                    {
                        "type": "merge_exact_duplicates",
                        "scope": keeper["scope"],
                        "card_id": keeper["card_id"],
                        "base_revision": keeper["revision"],
                        "block_id": block.block_id,
                        "expected_block_hash": block.sha256,
                        "other_card": {
                            "scope": other["scope"],
                            "card_id": other["card_id"],
                            "base_revision": other["revision"],
                        },
                    }
                )
        except ValueError:
            skipped += len(group) - 1
    return ops, skipped


def _reflect(
    target: Path,
    settings: hubc.HubSettings,
    client: hubc.MemoryHubClient,
    state: dict[str, Any],
    *,
    scope: str | None,
    limit: int,
    dry_run: bool,
) -> dict[str, Any]:
    if not 1 <= limit <= 100:
        raise ValueError("--limit must be from 1 to 100")
    if scope is not None and scope not in settings.read_scopes:
        raise ValueError("--scope must be a configured hub read scope")
    report = sync.pull(target, client, settings)
    if report["warnings"] or report.get("seq") is None:
        raise ValueError("hub reflect requires a complete fresh replica")
    with store.locked(target):
        replica = store.replica(target)
        if (
            not replica.belongs_to(client.identity, report["principal"])
            or replica.meta.get("log_epoch") != report["log_epoch"]
            or replica.meta.get("seq") != report["seq"]
        ):
            raise ValueError("hub reflect replica changed during the run")
        heads = list(replica.readable_heads(settings.read_scopes).values())
    if len(heads) > io.MAX_FILES or sum(len(h["text"].encode()) for h in heads) > io.MAX_CORPUS_BYTES:
        raise ValueError("hub reflect corpus limit exceeded")
    if scope:
        heads = [h for h in heads if h["scope"] == scope]
    ops, skipped = _ops(heads)
    if not ops and skipped:
        raise ValueError("no valid deterministic hub ops remain")
    binding = io.sha256(
        io.json_bytes([client.identity, report["principal"], report["log_epoch"], list(settings.read_scopes)])
    )
    previous = state.get("submissions", {}) if state.get("binding") == binding else {}
    submissions: dict[str, Any] = {}
    proposals = []
    for op in ops[:limit]:
        digest = io.sha256(io.json_bytes(op))
        pending = dict(previous.get(digest) or {"client_event_id": hubc.event_id()})
        submissions[digest] = pending
        if dry_run:
            continue
        state.update(binding=binding, submissions={**previous, **submissions})
        # Publish the id before the call so a lost response replays the same request.
        _save(target, state)
        if "proposal_id" not in pending:
            opened = client.call(
                "propose", base_epoch=report["log_epoch"], client_event_id=pending["client_event_id"], ops=[op]
            )
            if not isinstance(opened.get("proposal_id"), str) or opened.get("state") not in ("pending", "held"):
                raise ValueError("hub returned no open reflect proposal")
            pending["proposal_id"] = opened["proposal_id"]
            _save(target, state)
        proposals.append({"proposal_id": pending["proposal_id"]})
    if not dry_run:
        state.update(binding=binding, submissions=submissions)
    return {
        "status": "completed",
        "backend": "hub",
        "dry_run": dry_run,
        "candidate_count": len(ops[:limit]),
        "skipped_ops": skipped,
        "ops": ops[:limit],
        "proposals": proposals,
        "seq": report["seq"],
    }


def run_payload(*, target: Path, scope: str | None, limit: int, dry_run: bool, scheduled: bool) -> dict[str, Any]:
    derive.configuration(target)  # Refuse state overlapping a canonical card root.
    lock = target / ".brigade/memory/reflect/schedule.lock"
    io.protect_file(lock)
    with held_file_lock(lock, deadline_seconds=1):
        state = schedule_status(target)
        if scheduled and state["paused"]:
            return {"status": "paused", "schedule": {k: state[k] for k in ("consecutive_failures", "paused")}}
        try:
            settings, client = _client(target)
            payload = _reflect(target, settings, client, state, scope=scope, limit=limit, dry_run=dry_run)
        except (hubc.HubConfigError, hubc.HubUnavailable, hubc.HubRefusal, OSError, ValueError) as exc:
            # Never retain hub-supplied text, card bytes or credential-bearing errors.
            error = exc.code if isinstance(exc, hubc.HubRefusal) else "hub reflect run failed"
            payload = {"status": "failed", "error": error}
        count = state["consecutive_failures"] + 1 if payload["status"] == "failed" else 0
        state.update(consecutive_failures=count, paused=count >= FAILURES, last_status=payload["status"])
        _save(target, state)
        payload["schedule"] = {k: state[k] for k in ("consecutive_failures", "paused")}
        return payload


def rollback_payload(*, target: Path, proposal_id: str) -> dict[str, Any]:
    settings, client = _client(target)
    status = sync.read_status(client, settings)  # Refuse admin and let the hub check target rights.
    result = client.call(
        "rollback", proposal_id=proposal_id, base_epoch=status["log_epoch"], client_event_id=hubc.event_id()
    )
    return {"status": "proposed", "proposal": result}
