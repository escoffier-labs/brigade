"""Scheduled hub reflection opens proposals and never decides or applies them."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import replace
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
MAX_SCOPE_CARDS = 50_000
MAX_SCOPE_BYTES = 64 * 1024 * 1024
# Also bound the replica before loading candidate heads, independently of workspace settings.
MAX_REPLICA_BYTES = 64 * 1024 * 1024
REPLICA_FILE_SLACK = 1024 * 1024


STATE_ERROR = "invalid reflect schedule state. Inspect and remove .brigade/memory/reflect/schedule.json to recover"


class ReflectConfigError(ValueError):
    """A fixed diagnostic that carries no credential or hub-supplied text."""


class ReflectError(ValueError):
    """A local, fixed reason that is safe for scheduled logs."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code


def _digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _event_id(value: object) -> bool:
    return isinstance(value, str) and hubc.event_id_ms(value) is not None


def _code(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,127}", value) is not None


def schedule_status(target: Path) -> dict[str, Any]:
    try:
        state = io.read_object(target / STATE, private=True)
        count = state.get("consecutive_failures")
        if (
            type(count) is not int
            or count < 0
            or type(state.get("paused")) is not bool
            or state["paused"] != (count >= FAILURES)
        ):
            raise ValueError(STATE_ERROR)
        if "binding" in state and not _digest(state["binding"]):
            raise ValueError(STATE_ERROR)
        submissions = state.get("submissions", {})
        if not isinstance(submissions, dict) or (submissions and "binding" not in state):
            raise ValueError(STATE_ERROR)
        for digest, pending in submissions.items():
            if (
                not _digest(digest)
                or not isinstance(pending, dict)
                or set(pending) - {"client_event_id", "proposal_id", "refused"}
                or not _event_id(pending.get("client_event_id"))
                or ("proposal_id" in pending and not _event_id(pending["proposal_id"]))
                or ("refused" in pending and not _code(pending["refused"]))
                or ("proposal_id" in pending and "refused" in pending)
            ):
                raise ValueError(STATE_ERROR)
        if "last_status" in state and state["last_status"] not in ("completed", "failed"):
            raise ValueError(STATE_ERROR)
        return state
    except FileNotFoundError:
        return {"consecutive_failures": 0, "paused": False}
    except (OSError, ValueError, UnicodeError, RecursionError):
        raise ValueError(STATE_ERROR) from None


def schedule_summary(target: Path) -> dict[str, Any]:
    try:
        state = schedule_status(target)
        return {k: state[k] for k in ("consecutive_failures", "paused")}
    except ValueError:
        return {"paused": True, "state_error": STATE_ERROR}


def care_status_payload(target: Path, care: dict[str, Any]) -> dict[str, Any]:
    return {**care, "reflect": schedule_summary(target)}


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
        raise ReflectConfigError("hub reflection requires an enabled [hub] configuration")
    if settings.source not in (hubc.SOURCE_ENV, hubc.SOURCE_TABLE):
        raise ReflectConfigError(
            "reflect requires explicit hub memory settings: set [memory_hub] in fleet.toml "
            "or both BRIGADE_MEMORY_HUB_URL and BRIGADE_MEMORY_HUB_TOKEN_FILE"
        )
    return settings, hubc.client_for(settings)


def _ops(heads: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Only exact duplicate claims with existing block anchors can be translated.

    Preserve opaque bytes, including code indentation. The hub remains the
    authority for validation, classification and rights on both target cards.
    """
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    skipped: dict[str, int] = {}
    for head in sorted(heads, key=lambda h: (h["scope"], h["card_id"])):
        text = head["text"]
        if hubc.content_hash(text) != head["content_hash"]:
            raise ReflectError("hub_reflect_hash_mismatch", "hub reflect snapshot hash mismatch")
        body = delta.claim_body(text)
        if body.strip():
            groups.setdefault((head["scope"], body), []).append(head)
    ops = []
    for group in groups.values():
        if len(group) < 2:
            continue
        count = len(group) - 1
        if any(card.get("scan_state") != "clean" for card in group):
            skipped["non_clean"] = skipped.get("non_clean", 0) + count
            continue
        trusts = {card.get("trust") for card in group}
        if len(trusts) != 1:
            skipped["mixed_trust"] = skipped.get("mixed_trust", 0) + count
            continue
        if not trusts <= {"agent-unreviewed", "reviewed", "imported", "policy-promoted"}:
            skipped["unknown_trust"] = skipped.get("unknown_trust", 0) + count
            continue
        keeper = group[0]
        group_ops = []
        reason = "missing_pin"
        try:
            blocks = delta.blocks(keeper["text"])
            block = next((b for b in blocks if b.block_id), None)
            if block is None:
                raise ValueError("duplicate keeper has no pinned block")
            reason = "invalid_metadata"
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
                group_ops.append(
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
            skipped[reason] = skipped.get(reason, 0) + count
            continue
        ops.extend(group_ops)
    return ops, skipped


def _check_replica_files(target: Path, *, scope: str | None, dry_run: bool) -> None:
    """Refuse oversized on-disk snapshots before the replica loader can parse them."""
    for directory in (store.REPLICA, store.BUILDING):
        root = store.root(target) / directory
        try:
            size = (root / "heads.json").stat(follow_symlinks=False).st_size
        except FileNotFoundError:
            size = 0
        if size > MAX_REPLICA_BYTES + REPLICA_FILE_SLACK:
            raise ReflectError(
                "hub_reflect_replica_limit",
                "hub reflect on-disk replica exceeds the 64 MiB ceiling plus 1 MiB serialization slack. Snapshot was not parsed",
            )
        if scope is not None and not dry_run:
            meta = store.read_json(root / "meta.json", max_bytes=store.limits.MAX_META_BYTES)
            scopes = meta.get("read_scopes", []) if isinstance(meta, dict) else []
            if not isinstance(scopes, list) or not all(isinstance(name, str) for name in scopes):
                raise ReflectError("hub_reflect_replica_invalid", "hub reflect replica has invalid read scope metadata")
            if any(name != scope for name in scopes):
                raise ReflectError(
                    "hub_reflect_dedicated_replica_required",
                    "scoped reflection requires a dedicated replica. See docs/memory-operations.md#dedicated-reflect-folder. Existing scopes were left unchanged",
                )


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
        raise ReflectError("hub_reflect_limit_invalid", "--limit must be from 1 to 100")
    if scope is not None and scope not in settings.read_scopes:
        raise ReflectError("hub_reflect_scope_invalid", "--scope must be a configured hub read scope")
    with store.locked(target):
        _check_replica_files(target, scope=scope, dry_run=dry_run)
    configured_scopes = settings.read_scopes
    settings = replace(
        settings,
        read_scopes=(scope,) if scope else configured_scopes,
        replica_max_bytes=min(settings.replica_max_bytes, MAX_REPLICA_BYTES),
    )
    report = sync.pull(target, client, settings)
    warnings = list(report["warnings"])
    # Dry runs retain the existing permission to refresh a replica's scope selection.
    if dry_run and scope and report.get("dropped"):
        dropped = f"dropped {report['dropped']} replicated card(s) in scopes this credential can no longer read"
        warnings = [warning for warning in warnings if warning != dropped]
    if report.get("bytes", 0) > settings.replica_max_bytes or any(
        "not replicated yet" in warning for warning in warnings
    ):
        raise ReflectError(
            "hub_reflect_replica_limit",
            f"hub reflect replica byte cap exceeded ({settings.replica_max_bytes} bytes). Narrow read_scopes or adjust replica_max_mb within the 64 MiB reflect ceiling",
        )
    if warnings or report.get("seq") is None:
        raise ReflectError("hub_reflect_replica_incomplete", "hub reflect requires a complete fresh replica")

    def read_heads() -> list[dict[str, Any]]:
        with store.locked(target):
            _check_replica_files(target, scope=scope, dry_run=dry_run)
            replica = store.replica(target)
            if (
                not replica.belongs_to(client.identity, report["principal"])
                or replica.meta.get("log_epoch") != report["log_epoch"]
                or replica.meta.get("seq") != report["seq"]
            ):
                raise ReflectError("hub_reflect_replica_changed", "hub reflect replica changed during the run")
            if not set(settings.read_scopes) <= set(replica.meta.get("read_scopes") or []):
                raise ReflectError("hub_reflect_scope_unreadable", "hub reflect cannot read every selected scope")
            return list(replica.readable_heads(settings.read_scopes).values())

    heads = read_heads()

    def check_bounds() -> None:
        counts: dict[str, int] = {}
        sizes: dict[str, int] = {}
        for head in heads:
            name = head["scope"]
            counts[name] = counts.get(name, 0) + 1
            sizes[name] = sizes.get(name, 0) + len(head["text"].encode())
            if counts[name] > MAX_SCOPE_CARDS or sizes[name] > MAX_SCOPE_BYTES:
                raise ReflectError(
                    "hub_reflect_scope_limit",
                    f"hub reflect per-scope bound exceeded ({MAX_SCOPE_CARDS} cards or {MAX_SCOPE_BYTES} text bytes)",
                )

    check_bounds()
    if any("scan_state" not in head for head in heads):
        sync.refresh_scan_metadata(target, client, settings)
        heads = read_heads()
        check_bounds()
    ops = []
    skipped: dict[str, int] = {}
    for name in settings.read_scopes:
        scope_ops, scope_skips = _ops([head for head in heads if head["scope"] == name])
        ops.extend(scope_ops)
        for reason, count in scope_skips.items():
            skipped[reason] = skipped.get(reason, 0) + count
    binding = io.sha256(
        io.json_bytes([client.identity, report["principal"], report["log_epoch"], list(configured_scopes)])
    )
    previous = dict(state.get("submissions", {})) if state.get("binding") == binding else {}
    remaining = []
    for op in ops:
        digest = io.sha256(io.json_bytes(op))
        remembered = previous.get(digest, {})
        if remembered.get("proposal_id"):
            skipped["already_proposed"] = skipped.get("already_proposed", 0) + 1
        elif remembered.get("refused"):
            skipped["refused"] = skipped.get("refused", 0) + 1
        else:
            remaining.append((digest, op))
    selected = remaining[:limit]
    proposals = []
    refused = []
    error = None
    failed_ops = 0
    if not dry_run:
        # Preserve unprocessed submissions under this binding across scope and limit changes.
        state.update(binding=binding, submissions=previous)
        for digest, op in selected:
            for attempt in range(2):
                pending = dict(previous.get(digest) or {"client_event_id": hubc.event_id()})
                previous[digest] = pending
                # Publish the id before the call so a lost response replays the same request.
                _save(target, state)
                try:
                    opened = client.call(
                        "propose", base_epoch=report["log_epoch"], client_event_id=pending["client_event_id"], ops=[op]
                    )
                except hubc.HubRefusal as exc:
                    code = exc.code if _code(exc.code) else "hub_refused"
                    refused.append({"op_digest": digest, "code": code})
                    if exc.denies_access:
                        error = code
                        break
                    if code in ("memory_client_event_expired", "memory_client_event_conflict"):
                        previous.pop(digest)
                        _save(target, state)
                        if attempt == 0:
                            continue  # Re-mint and retry this op once, within this run.
                    elif (
                        400 <= exc.status < 500
                        and exc.status not in (408, 429)
                        and exc.body.get("retryable") is not True
                    ):
                        pending["refused"] = code
                        _save(target, state)
                    failed_ops += 1
                    break
                except hubc.HubUnavailable:
                    error = "hub_unavailable"
                    break
                if not _event_id(opened.get("proposal_id")) or opened.get("state") not in ("pending", "held"):
                    raise ReflectError("hub_reflect_proposal_invalid", "hub returned no open reflect proposal")
                pending["proposal_id"] = opened["proposal_id"]
                _save(target, state)
                proposals.append({"proposal_id": pending["proposal_id"]})
                break
            if error is not None:
                break
        # Only final refusals of ops attempted this run count, never cached skips or recovered ids.
        if selected and failed_ops == len(selected) and error is None:
            error = "all_ops_refused"
    payload = {
        "status": "failed" if error else "completed",
        "backend": "hub",
        "dry_run": dry_run,
        "candidate_count": len(selected),
        "skipped_ops": sum(skipped.values()),
        "skipped_reasons": skipped,
        "ops": [op for _, op in selected],
        "proposals": proposals,
        "refused_ops": refused,
        "seq": report["seq"],
    }
    if error:
        payload["error"] = error
    return payload


def run_payload(*, target: Path, scope: str | None, limit: int, dry_run: bool, scheduled: bool) -> dict[str, Any]:
    derive.configuration(target)  # Refuse state overlapping a canonical card root.
    lock = target / ".brigade/memory/reflect/schedule.lock"
    io.protect_file(lock)
    with held_file_lock(lock, deadline_seconds=1):
        state = schedule_status(target)
        if scheduled and not dry_run and state["paused"]:
            return {"status": "paused", "schedule": {k: state[k] for k in ("consecutive_failures", "paused")}}
        try:
            settings, client = _client(target)
            with store.limits.bounded(max_cards=MAX_SCOPE_CARDS, max_bytes=MAX_REPLICA_BYTES + REPLICA_FILE_SLACK):
                payload = _reflect(target, settings, client, state, scope=scope, limit=limit, dry_run=dry_run)
        except (hubc.HubConfigError, hubc.HubUnavailable, hubc.HubRefusal, OSError, ValueError) as exc:
            # Never retain hub-supplied text, card bytes or credential-bearing errors.
            if isinstance(exc, store.limits.ReplicaLimitError):
                code = "hub_reflect_scope_limit" if exc.kind == "cards" else "hub_reflect_replica_limit"
                reason = f"hub reflect per-replica {exc.kind} bound exceeded before snapshot decoding or saving"
            elif isinstance(exc, ReflectError):
                code, reason = exc.code, str(exc)
            elif isinstance(exc, ReflectConfigError):
                code, reason = "hub_reflect_config", str(exc)
            elif isinstance(exc, hubc.HubRefusal):
                code = exc.code if _code(exc.code) else "hub_refused"
                reason = "hub refused the reflect request"
            elif isinstance(exc, hubc.HubUnavailable):
                code, reason = "hub_unavailable", "hub reflection could not reach the hub"
            elif isinstance(exc, hubc.HubConfigError):
                code, reason = "hub_reflect_config", "invalid hub memory configuration or replica metadata"
            elif isinstance(exc, OSError):
                code, reason = "hub_reflect_io", "hub reflect could not safely read or write local state"
            else:
                code, reason = "hub_reflect_invalid", "hub reflect received invalid state or data"
            payload = {
                "status": "failed",
                "code": code,
                "error": reason if isinstance(exc, ReflectConfigError) else code,
                "reason": reason,
            }
        if not dry_run:
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
