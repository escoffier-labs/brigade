"""Scheduled hub reflection opens proposals and never decides or applies them."""

from __future__ import annotations

import itertools
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import memory_fairness as fairness
from . import memory_hub_client as hubc
from . import memory_hub_store as store
from . import memory_hub_sync as sync
from . import memory_proposal_io as io
from . import memory_proposal_v2_ops as delta
from . import memory_reflect_candidates as derive
from . import memory_reflect_links as links
from . import memory_reflect_near as near
from .inbox_lock import held_file_lock

STATE = ".brigade/memory/reflect/schedule.json"
FAILURES = 3
MAX_SCOPE_CARDS = 50_000
MAX_SCOPE_BYTES = 64 * 1024 * 1024
# Also bound the replica before loading candidate heads, independently of workspace settings.
MAX_REPLICA_BYTES = 64 * 1024 * 1024
REPLICA_FILE_SLACK = 1024 * 1024


# Hub quota refusals end proposing for this run without counting as a failure.
QUOTA_REFUSALS = hubc.QUOTA_REFUSALS
DUPLICATE_PENDING = "memory_proposal_duplicate_pending"
# Fair scheduling lanes. Keys are 128-bit digests of a scope and card ids, never card text.
CLUSTER_KEY = re.compile(r"[0-9a-f]{32}")
NEAR_LANE = "near_clusters"
LINK_LANE = "link_clusters"
LANES = (NEAR_LANE, LINK_LANE)
# After exact merges the two lanes alternate. The lane that goes first flips after a run that serves a proposal.
FIRST_LANE = "first_lane"

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


def valid_submission(digest: object, pending: object) -> bool:
    """One S19d ledger entry: the request id published before `propose`, then its proposal id or refusal."""
    return (
        _digest(digest)
        and isinstance(pending, dict)
        and not set(pending) - {"client_event_id", "proposal_id", "refused"}
        and _event_id(pending.get("client_event_id"))
        and not ("proposal_id" in pending and not _event_id(pending["proposal_id"]))
        and not ("refused" in pending and not _code(pending["refused"]))
        and not ("proposal_id" in pending and "refused" in pending)
    )


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
        if not all(valid_submission(digest, pending) for digest, pending in submissions.items()):
            raise ValueError(STATE_ERROR)
        for lane in LANES:
            schedule = state.get(lane, {})
            if not isinstance(schedule, dict) or (schedule and "binding" not in state):
                raise ValueError(STATE_ERROR)
            for scope_name, entries in schedule.items():
                if (
                    not isinstance(scope_name, str)
                    or not scope_name
                    or len(scope_name) > 512
                    or not isinstance(entries, dict)
                    or len(entries) > _lane_cap(lane)
                ):
                    raise ValueError(STATE_ERROR)
                for key, entry in entries.items():
                    if (
                        CLUSTER_KEY.fullmatch(key) is None
                        or not isinstance(entry, list)
                        or len(entry) != 2
                        or any(type(value) is not int or value < 0 for value in entry)
                    ):
                        raise ValueError(STATE_ERROR)
        if state.get(FIRST_LANE, NEAR_LANE) not in LANES:
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


def _now_ms() -> int:
    return int(time.time() * 1000)


def _submission_digest(ops: list[dict[str, Any]]) -> str:
    # A single op keeps its historical digest, so remembered submissions survive upgrades.
    return io.sha256(io.json_bytes(ops[0] if len(ops) == 1 else ops))


def _cluster_key(ops: list[dict[str, Any]]) -> str:
    """Name a submission by scope and card ids, so rewrites keep its key."""
    ids = {op["card_id"] for op in ops} | {op["other_card"]["card_id"] for op in ops if "other_card" in op}
    return fairness.key_digest(ops[0]["scope"], ids)


def _cluster_cap() -> int:
    """Most near-duplicate keys one scope can yield, so every observed key gets an entry.

    Clusters are disjoint and every submission names at least two of its
    cluster's cards, so a scope has at most half its card bound in keys.
    """
    return MAX_SCOPE_CARDS // 2


def _link_cluster_cap() -> int:
    """Most link-repair keys one scope can yield. A submission can name a single card."""
    return MAX_SCOPE_CARDS


def _lane_cap(lane: str) -> int:
    return (_cluster_cap if lane == NEAR_LANE else _link_cluster_cap)()


def _redacted(op: dict[str, Any]) -> dict[str, Any]:
    """An op for a report. Replacement wording is card text, so reports never carry it."""
    return {key: value for key, value in op.items() if key != "text"}


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


# Outcomes of one `submit`.
OPENED, DUPLICATE, QUOTA, FAILED, DENIED, UNAVAILABLE = (
    "opened",
    "duplicate",
    "quota",
    "failed",
    "denied",
    "unavailable",
)


@dataclass(frozen=True)
class Submitted:
    outcome: str
    proposal_id: str | None = None
    # A fixed hub code for DENIED and UNAVAILABLE.
    code: str | None = None
    # Hub codes of the refusals this submission met, in order. Never hub text.
    refusals: tuple[str, ...] = ()


def submit(
    client: hubc.MemoryHubClient,
    ledger: dict[str, dict[str, Any]],
    digest: str,
    ops: list[dict[str, Any]],
    *,
    base_epoch: str,
    save: Callable[[], None],
) -> Submitted:
    """Open one proposal through the S19d request id ledger.

    `ledger[digest]` holds the request id, published through `save` before the
    call so a lost response replays the same request, then the proposal id or
    the final refusal code. An expired or conflicting id is re-minted once.
    Quota refusals and hub unavailability are outcomes, not exceptions, and a
    duplicate pending proposal records the existing id. Raises ReflectError when
    the hub answers with no open proposal.
    """
    refusals: list[str] = []
    for attempt in range(2):
        pending = dict(ledger.get(digest) or {"client_event_id": hubc.event_id()})
        ledger[digest] = pending
        save()
        try:
            opened = client.call("propose", base_epoch=base_epoch, client_event_id=pending["client_event_id"], ops=ops)
        except hubc.HubRefusal as exc:
            code = exc.code if _code(exc.code) else "hub_refused"
            if exc.status == 429 and code in QUOTA_REFUSALS:
                return Submitted(QUOTA, code=code, refusals=tuple(refusals))
            if exc.status == 409 and code == DUPLICATE_PENDING:
                existing = exc.body.get("proposal_id")
                if _event_id(existing):
                    pending["proposal_id"] = existing
                else:
                    pending["refused"] = code
                save()
                return Submitted(DUPLICATE, proposal_id=pending.get("proposal_id"), refusals=tuple(refusals))
            refusals.append(code)
            if exc.denies_access:
                return Submitted(DENIED, code=code, refusals=tuple(refusals))
            if code in ("memory_client_event_expired", "memory_client_event_conflict"):
                ledger.pop(digest)
                save()
                if attempt == 0:
                    continue  # Re-mint and retry this op once, within this run.
            elif 400 <= exc.status < 500 and exc.status not in (408, 429) and exc.body.get("retryable") is not True:
                pending["refused"] = code
                save()
            return Submitted(FAILED, code=code, refusals=tuple(refusals))
        except hubc.HubUnavailable:
            return Submitted(UNAVAILABLE, code="hub_unavailable", refusals=tuple(refusals))
        if not _event_id(opened.get("proposal_id")) or opened.get("state") not in ("pending", "held"):
            raise ReflectError("hub_reflect_proposal_invalid", "hub returned no open reflect proposal")
        pending["proposal_id"] = opened["proposal_id"]
        save()
        return Submitted(OPENED, proposal_id=pending["proposal_id"], refusals=tuple(refusals))
    raise AssertionError("unreachable")  # pragma: no cover


def _check_replica_files(target: Path, *, scope: str | None, dry_run: bool) -> None:
    """Refuse oversized on-disk snapshots before the replica loader can parse them."""
    for directory in (store.REPLICA, store.BUILDING):
        root = store.root(target) / directory
        size = 0
        # Every snapshot generation, legacy name included, without trusting meta.json to name it.
        for path in (root / store.LEGACY_HEADS, *(root.glob("heads-*.json") if root.is_dir() else ())):
            try:
                size = max(size, path.stat(follow_symlinks=False).st_size)
            except FileNotFoundError:
                pass
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
    try:
        near_limit, near_cap = near.settings(target)
        link_cap = links.settings(target)
    except near.ThresholdError as exc:
        raise ReflectConfigError(str(exc)) from None
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
    # Each submission is one propose request. Exact merges stay one op each and
    # always come before every near-duplicate and link-repair submission, so they get the quota first.
    exact: list[list[dict[str, Any]]] = []
    similar: list[list[dict[str, Any]]] = []
    relinked: list[list[dict[str, Any]]] = []
    skipped: dict[str, int] = {}
    budget = near.Budget()
    link_stats = {"links": 0, "broken": 0, "repairs": 0}
    link_index = links.Index(heads)
    for name in settings.read_scopes:
        scoped = [head for head in heads if head["scope"] == name]
        scope_ops, scope_skips = _ops(scoped)
        exact.extend([op] for op in scope_ops)
        # The near pass runs after the exact pass and leaves exact groups to it.
        near_ops, near_skips = near.ops(scoped, limit=near_limit, budget=budget)
        similar.extend(near_ops)
        # The link pass runs last and resolves each link only within its own scope.
        link_ops, link_skips = links.ops(scoped, index=link_index, stats=link_stats)
        relinked.extend(link_ops)
        for reason, count in (*scope_skips.items(), *near_skips.items(), *link_skips.items()):
            skipped[reason] = skipped.get(reason, 0) + count
    binding = io.sha256(
        io.json_bytes([client.identity, report["principal"], report["log_epoch"], list(configured_scopes)])
    )
    previous = dict(state.get("submissions", {})) if state.get("binding") == binding else {}

    def unremembered(submissions: list[list[dict[str, Any]]]) -> list[tuple[str, list[dict[str, Any]]]]:
        fresh = []
        for ops in submissions:
            digest = _submission_digest(ops)
            remembered = previous.get(digest, {})
            if remembered.get("proposal_id"):
                skipped["already_proposed"] = skipped.get("already_proposed", 0) + 1
            elif remembered.get("refused"):
                skipped["refused"] = skipped.get("refused", 0) + 1
            else:
                fresh.append((digest, ops))
        return fresh

    # Fair scheduling: least recently proposed cluster first, then oldest first seen, then key.
    # Keys are card ids, so rewriting members never resets a cluster's place in line.
    # Entries are [first_seen_ms, proposed_ms] per lane, scope and key, with 0 for never proposed.
    now = _now_ms()
    lane_key: dict[str, tuple[str, str, str]] = {}
    tables: dict[str, dict[str, dict[str, list[int]]]] = {}
    for lane, submissions in ((NEAR_LANE, similar), (LINK_LANE, relinked)):
        known = state.get(lane, {}) if state.get("binding") == binding else {}
        table: dict[str, dict[str, list[int]]] = {}
        for ops in submissions:
            name, key = ops[0]["scope"], _cluster_key(ops)
            lane_key[_submission_digest(ops)] = (lane, name, key)
            entry = known.get(name, {}).get(key)
            table.setdefault(name, {})[key] = list(entry) if entry else [now, 0]
        tables[lane] = table

    priority = fairness.priority

    # Keys not seen this run are dropped. The cap never binds for observed keys, but if it
    # did, keys are admitted in scheduling order, so the most recently served go first.
    for lane, table in tables.items():
        tables[lane] = {
            name: dict(sorted(entries.items(), key=lambda item: priority(*item))[: _lane_cap(lane)])
            for name, entries in table.items()
        }

    def turn(ops: list[dict[str, Any]]) -> tuple[int, int, str]:
        lane, name, key = lane_key[_submission_digest(ops)]
        return priority(key, tables[lane].get(name, {}).get(key, [now, 0]))

    pending_similar = unremembered(sorted(similar, key=turn))
    pending_links = unremembered(sorted(relinked, key=turn))
    # Cap new near-duplicate and link-repair proposals per run so crafted cards cannot drain the pending quota.
    # A cluster with a remembered proposal keeps its place but does not use a slot.
    for reason, lane_pending, cap in (
        ("near_deferred", pending_similar, near_cap),
        ("link_deferred", pending_links, link_cap),
    ):
        deferred = sum(len(ops) for _, ops in lane_pending[cap:])
        if deferred:
            skipped[reason] = skipped.get(reason, 0) + deferred
    # Round robin so neither lane can starve the other under --limit or a quota refusal.
    first = state.get(FIRST_LANE, NEAR_LANE)
    lanes = [pending_similar[:near_cap], pending_links[:link_cap]]
    if first == LINK_LANE:
        lanes.reverse()
    alternating = [item for group in itertools.zip_longest(*lanes) for item in group if item is not None]
    remaining = unremembered(exact) + alternating
    selected = remaining[:limit]
    proposals = []
    refused: list[dict[str, str]] = []
    error = None
    failed_ops = 0
    attempted = len(selected)
    if not dry_run:
        # Preserve unprocessed submissions under this binding across scope and limit changes.
        state.update(binding=binding, submissions=previous, **tables)

        served_any = False

        def served(digest: str) -> None:
            nonlocal served_any
            lane, name, key = lane_key.get(digest, ("", "", ""))
            served_any = served_any or bool(lane)
            if key in tables.get(lane, {}).get(name, {}):
                tables[lane][name][key][1] = now

        for position, (digest, ops) in enumerate(selected):
            sent = submit(
                client, previous, digest, ops, base_epoch=report["log_epoch"], save=lambda: _save(target, state)
            )
            refused.extend({"op_digest": digest, "code": code} for code in sent.refusals)
            if sent.outcome == QUOTA:
                # A full quota is back pressure, not a failure: stop and leave the rest for later runs.
                skipped["proposal_quota"] = skipped.get("proposal_quota", 0) + len(selected) - position
                attempted = position
                break
            if sent.outcome in (DENIED, UNAVAILABLE):
                error = sent.code
                break
            if sent.outcome == FAILED:
                failed_ops += 1
                continue
            if sent.proposal_id is not None:
                served(digest)
                _save(target, state)
            if sent.outcome == DUPLICATE:
                skipped["duplicate_pending"] = skipped.get("duplicate_pending", 0) + 1
            else:
                proposals.append({"proposal_id": sent.proposal_id})
        if served_any:
            state[FIRST_LANE] = NEAR_LANE if first == LINK_LANE else LINK_LANE
        # Only final refusals of ops attempted this run count, never cached skips, recovered ids or quota stops.
        if attempted and failed_ops == attempted and error is None:
            error = "all_ops_refused"
    payload = {
        "status": "failed" if error else "completed",
        "backend": "hub",
        "dry_run": dry_run,
        "candidate_count": len(selected),
        "skipped_ops": sum(skipped.values()),
        "skipped_reasons": skipped,
        "ops": [_redacted(op) for _, ops in selected for op in ops],
        "proposals": proposals,
        "refused_ops": refused,
        "seq": report["seq"],
        "near_duplicate": {"threshold": near_limit, "max_proposals": near_cap, **budget.stats},
        "link_repair": {"max_proposals": link_cap, **link_stats},
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
